"""
test_backup.py — Tests for the SQLite-safe palace backup module.

A backup must:
  * produce a single timestamped zip archive in each destination directory,
  * capture a *consistent* snapshot of every SQLite database (folding in any
    uncommitted WAL), so no recently-filed memory is lost,
  * never mutate the live palace (incremental-only / never-destroy),
  * carry a MANIFEST.json describing what was captured, and
  * prune old archives per a retention policy.
"""

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest


def _make_sqlite(path: Path, rows: list[str], *, wal: bool = False) -> None:
    """Create a small SQLite db with a `note` table holding the given rows.

    When ``wal=True``, the connection is left in WAL journal mode with the
    rows committed but *not* checkpointed, leaving them in the -wal sidecar —
    the exact condition a naive file copy would miss.
    """
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS note (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO note (body) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close() if not wal else None
    if wal:
        # Keep the connection open so the WAL is not auto-checkpointed on close.
        # Caller relies on the rows living in <path>-wal.
        return conn  # type: ignore[return-value]


def _build_config_dir(root: Path) -> Path:
    """Build a minimal ~/.mempalace-shaped directory tree and return it."""
    cfg = root / ".mempalace"
    palace = cfg / "palace"
    palace.mkdir(parents=True)
    (cfg / "wal").mkdir()

    # ChromaDB source of truth (SQLite) + a fake HNSW segment dir.
    _make_sqlite(palace / "chroma.sqlite3", ["drawer one", "drawer two"])
    seg = palace / "4572b2d0-seg"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"\x00\x01\x02hnsw")
    (seg / "header.bin").write_bytes(b"hdr")

    # Knowledge graph SQLite.
    _make_sqlite(cfg / "knowledge_graph.sqlite3", ["Alice parent_of Max"])

    # Config + identity + app WAL.
    (cfg / "config.json").write_text(json.dumps({"palace_path": str(palace)}), encoding="utf-8")
    (cfg / "identity.txt").write_text("I am MemPalace.", encoding="utf-8")
    (cfg / "wing_config.json").write_text(json.dumps({"wings": []}), encoding="utf-8")
    (cfg / "wal" / "write_log.jsonl").write_text('{"op":"add"}\n', encoding="utf-8")
    return cfg


def _archives(dest: Path) -> list[Path]:
    return sorted(dest.glob("mempalace-backup-*.zip"))


def test_creates_single_archive_with_manifest_and_data(tmp_path):
    """Tracer bullet: backup writes one zip containing the data + a manifest."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"

    result = create_backup(cfg, [dest])

    archives = _archives(dest)
    assert len(archives) == 1, "exactly one archive expected"
    assert result.archive_name == archives[0].name

    with zipfile.ZipFile(archives[0]) as zf:
        names = set(zf.namelist())
    assert "MANIFEST.json" in names
    assert "palace/chroma.sqlite3" in names
    assert "knowledge_graph.sqlite3" in names
    assert "config.json" in names


def _rows(db_bytes: bytes, tmp_path: Path) -> list[str]:
    """Write db bytes to a temp file and read back the `note` table."""
    p = tmp_path / "extracted.sqlite3"
    p.write_bytes(db_bytes)
    conn = sqlite3.connect(str(p))
    try:
        return [r[0] for r in conn.execute("SELECT body FROM note ORDER BY id")]
    finally:
        conn.close()


def test_snapshot_captures_uncommitted_wal_rows(tmp_path):
    """Rows living only in the -wal sidecar must survive into the backup.

    This is the core safety guarantee: a naive file copy of chroma.sqlite3
    would miss WAL-resident rows and silently lose recently-filed memory.
    """
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    chroma = cfg / "palace" / "chroma.sqlite3"

    # Open the live db in WAL mode and write rows WITHOUT checkpointing, so
    # they live in chroma.sqlite3-wal, not the main file. Keep the connection
    # open across the backup so nothing auto-checkpoints.
    live = sqlite3.connect(str(chroma))
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO note (body) VALUES ('wal-only drawer')")
        live.commit()
        assert (cfg / "palace" / "chroma.sqlite3-wal").exists()

        dest = tmp_path / "dest"
        create_backup(cfg, [dest])
    finally:
        live.close()

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        rows = _rows(zf.read("palace/chroma.sqlite3"), tmp_path)

    assert "wal-only drawer" in rows


def _fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Map each file under root to (size, sha256) for tamper detection."""
    import hashlib

    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = (
                p.stat().st_size,
                hashlib.sha256(p.read_bytes()).digest(),
            )
    return out


def test_live_palace_is_not_mutated(tmp_path):
    """Backing up must never alter the live palace (incremental-only)."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    before = _fingerprint(cfg)

    create_backup(cfg, [tmp_path / "dest"])

    after = _fingerprint(cfg)
    assert after == before


def test_archive_lands_in_every_destination(tmp_path):
    """Every destination dir receives an identical archive."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    d1 = tmp_path / "onedrive"
    d2 = tmp_path / "nas"

    result = create_backup(cfg, [d1, d2])

    a1, a2 = _archives(d1), _archives(d2)
    assert len(a1) == 1 and len(a2) == 1
    assert a1[0].name == a2[0].name == result.archive_name
    assert a1[0].read_bytes() == a2[0].read_bytes()
    assert len(result.dest_paths) == 2


def test_retention_prunes_oldest_keeping_newest_n(tmp_path):
    """With keep=N, only the N newest archives remain in each destination."""
    from datetime import datetime, timezone

    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"

    stamps = [datetime(2026, 1, day, 3, 0, 0, tzinfo=timezone.utc) for day in (1, 2, 3, 4)]
    for ts in stamps:
        create_backup(cfg, [dest], now=ts, keep=2)

    remaining = [p.name for p in _archives(dest)]
    assert len(remaining) == 2
    # Newest two (Jan 3 and Jan 4) survive; older ones pruned.
    assert remaining == [
        "mempalace-backup-20260103T030000Z.zip",
        "mempalace-backup-20260104T030000Z.zip",
    ]


def test_excludes_sidecars_locks_and_stale_backup(tmp_path):
    """SQLite sidecars, lock files, and palace.backup must not be archived."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")

    # Noise that must be excluded.
    (cfg / "palace" / "chroma.sqlite3-wal").write_bytes(b"walnoise")
    (cfg / "palace" / "chroma.sqlite3-shm").write_bytes(b"shmnoise")
    (cfg / "locks").mkdir()
    (cfg / "locks" / "mine.lock").write_text("pid", encoding="utf-8")
    stale = cfg / "palace.backup"
    stale.mkdir()
    (stale / "chroma.sqlite3").write_bytes(b"old")

    dest = tmp_path / "dest"
    create_backup(cfg, [dest])

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        names = zf.namelist()

    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    assert not any("locks/" in n for n in names)
    assert not any("palace.backup" in n for n in names)


def test_missing_palace_raises_and_writes_nothing(tmp_path):
    """A missing palace must fail loudly without leaving a partial archive."""
    from mempalace.backup import BackupError, create_backup

    empty_cfg = tmp_path / "empty"
    empty_cfg.mkdir()
    dest = tmp_path / "dest"

    with pytest.raises(BackupError):
        create_backup(empty_cfg, [dest])

    assert not dest.exists() or _archives(dest) == []


def test_cli_backup_command_creates_archive(tmp_path):
    """The `mempalace backup` CLI entry point produces an archive in --out."""
    from argparse import Namespace

    from mempalace.cli import cmd_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"

    args = Namespace(
        out=[str(dest)],
        keep=None,
        config_dir=str(cfg),
        palace=str(cfg / "palace"),
    )
    cmd_backup(args)

    assert len(_archives(dest)) == 1
