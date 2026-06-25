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
import sys
import zipfile
from pathlib import Path

import pytest


def _make_sqlite(path: Path, rows: list[str]) -> None:
    """Create a small SQLite db with a `note` table holding the given rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS note (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO note (body) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


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


# ── Round 1 hardening: data-loss, integrity, and isolation guarantees ──────


def test_bad_integrity_check_aborts_and_writes_nothing(tmp_path, monkeypatch):
    """A failed quick_check must raise and publish no archive anywhere."""
    import mempalace.backup as backup_mod
    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"

    # Force the snapshot integrity check to report corruption.
    def _bad_snapshot(src, dst):
        import shutil

        shutil.copy2(src, dst)
        return "row 5 missing from index note_idx"

    monkeypatch.setattr(backup_mod, "_snapshot_sqlite", _bad_snapshot)

    with pytest.raises(BackupError):
        create_backup(cfg, [dest])

    assert _archives(dest) == []
    # No partial/temp leftovers either.
    assert list(dest.glob("*.part")) == [] if dest.exists() else True


def test_manifest_sha256_matches_archived_bytes(tmp_path):
    """Every MANIFEST entry's sha256/size must match the actual zip bytes."""
    import hashlib

    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"
    create_backup(cfg, [dest])

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
        for entry in manifest["files"]:
            data = zf.read(entry["path"])
            assert entry["size"] == len(data), entry["path"]
            assert entry["sha256"] == hashlib.sha256(data).hexdigest(), entry["path"]
    assert all(v == "ok" for v in manifest["sqlite_checks"].values())
    assert manifest["file_count"] == len(manifest["files"])


def test_archive_includes_hnsw_index(tmp_path):
    """The rebuildable-but-valuable HNSW .bin files must be archived."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"
    create_backup(cfg, [dest])

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        names = set(zf.namelist())
    assert "palace/4572b2d0-seg/data_level0.bin" in names
    assert "palace/4572b2d0-seg/header.bin" in names


def test_keep_zero_or_negative_is_rejected(tmp_path):
    """keep<=0 is a footgun (silently keeps all); it must be rejected."""
    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    dest = tmp_path / "dest"

    for bad in (0, -1):
        with pytest.raises(BackupError):
            create_backup(cfg, [dest], keep=bad)


def test_destination_inside_palace_is_rejected(tmp_path):
    """A dest within the live tree would get swept into future backups."""
    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    inside_palace = cfg / "palace" / "backups"
    inside_config = cfg / "backups"

    for bad in (inside_palace, inside_config, cfg, cfg / "palace"):
        with pytest.raises(BackupError):
            create_backup(cfg, [bad])


def test_knowledge_graph_resolved_from_relocated_palace(tmp_path):
    """When the KG lives under the palace (relocated palace), capture it.

    Mirrors mcp_server/fact_checker which place the KG at
    <palace>/knowledge_graph.sqlite3 when the palace is relocated. The
    backup must not silently look only in config_dir.
    """
    from mempalace.backup import create_backup

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    palace = tmp_path / "relocated_palace"
    palace.mkdir()
    _make_sqlite(palace / "chroma.sqlite3", ["drawer one"])
    # KG sits next to the palace, NOT under config_dir.
    _make_sqlite(palace / "knowledge_graph.sqlite3", ["Alice knows Bob"])

    dest = tmp_path / "dest"
    result = create_backup(cfg, [dest], palace_path=palace)

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        names = set(zf.namelist())
        rows = _rows(zf.read("knowledge_graph.sqlite3"), tmp_path)
    assert "knowledge_graph.sqlite3" in names
    assert "Alice knows Bob" in rows
    assert result.kg_included is True


def test_snapshot_handles_wal_with_missing_shm(tmp_path):
    """An unclean shutdown (WAL present, -shm deleted) must still back up.

    A strictly read-only open cannot rebuild the -shm and would fail exactly
    when un-checkpointed words are at stake.
    """
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    chroma = cfg / "palace" / "chroma.sqlite3"

    conn = sqlite3.connect(str(chroma))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO note (body) VALUES ('uncheckpointed word')")
        conn.commit()
    finally:
        conn.close()
    # Simulate unclean shutdown: WAL stays, -shm is gone.
    shm = cfg / "palace" / "chroma.sqlite3-shm"
    if shm.exists():
        shm.unlink()

    dest = tmp_path / "dest"
    create_backup(cfg, [dest])

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        rows = _rows(zf.read("palace/chroma.sqlite3"), tmp_path)
    assert "uncheckpointed word" in rows


def test_partial_archive_cleaned_up_on_dest_failure(tmp_path, monkeypatch):
    """If a dest copy fails, no archive or .part is left, and BackupError raised.

    Critically, an earlier successful dest must NOT have been pruned, so a
    failed run can never destroy the last good backup.
    """
    import mempalace.backup as backup_mod
    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    good = tmp_path / "good"
    bad = tmp_path / "bad"

    # Seed an existing good archive so we can prove retention never ran.
    good.mkdir()
    sentinel = good / "mempalace-backup-20000101T000000Z.zip"
    sentinel.write_bytes(b"old-but-precious")

    real_copy = backup_mod.shutil.copy2

    def _flaky_copy(src, dst, *a, **k):
        # First dest (good) copies fine; second dest (bad) fails mid-write.
        if Path(dst).parent.name == "bad":
            Path(dst).write_bytes(b"truncated")  # leave a partial behind
            raise OSError("simulated disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(backup_mod.shutil, "copy2", _flaky_copy)

    with pytest.raises(BackupError):
        create_backup(cfg, [good, bad], keep=1)

    # No new visible archive in either dest; no .part leftovers.
    assert list(bad.glob("*.part")) == []
    assert list(good.glob("*.part")) == []
    assert _archives(bad) == []
    # The pre-existing good archive survives (retention did not run on failure).
    assert sentinel.exists()


# ── Round 2 hardening: promotion atomicity, no-mutation, path/KG resolution ──


def test_promotion_failure_leaves_no_part_and_raises_backup_error(tmp_path, monkeypatch):
    """If the atomic promote (rename) fails for a dest, clean up and raise.

    A raw OSError must not escape, and no `.part` may be left behind in any
    not-yet-promoted destination.
    """
    import pathlib

    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"

    real_replace = pathlib.Path.replace

    def _flaky_replace(self, target):
        if Path(target).parent.name == "d2":
            raise OSError("rename blocked (file locked)")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", _flaky_replace)

    with pytest.raises(BackupError):
        create_backup(cfg, [d1, d2], keep=1)

    assert list(d1.glob("*.part")) == []
    assert list(d2.glob("*.part")) == []


def test_wal_live_files_are_not_mutated_by_snapshot(tmp_path):
    """Snapshotting a WAL database must not touch the live db or its sidecars."""
    import hashlib

    from mempalace.backup import _snapshot_sqlite

    src = tmp_path / "chroma.sqlite3"
    holder = sqlite3.connect(str(src))
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT)")
        holder.execute("INSERT INTO note (body) VALUES ('live word')")
        holder.commit()  # lands in -wal; holder kept open so it stays there

        def fp(p: Path):
            return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

        triplet = [src, tmp_path / "chroma.sqlite3-wal", tmp_path / "chroma.sqlite3-shm"]
        before = [fp(p) for p in triplet]

        dst = tmp_path / "snap.sqlite3"
        assert _snapshot_sqlite(src, dst) == "ok"

        after = [fp(p) for p in triplet]
    finally:
        holder.close()

    assert before == after, "live database/sidecars were modified by the snapshot"
    rows = _rows(dst.read_bytes(), tmp_path)
    assert "live word" in rows


def test_default_layout_prefers_config_dir_kg_over_stale_palace_kg(tmp_path):
    """In the default layout (palace == config_dir/palace), the config_dir KG
    is authoritative even if a stale palace-side KG file also exists."""
    from mempalace.backup import create_backup

    cfg = _build_config_dir(tmp_path / "home")
    # _build_config_dir already wrote the live KG at cfg/knowledge_graph.sqlite3.
    # Plant a STALE palace-side KG that must NOT be the one captured.
    _make_sqlite(cfg / "palace" / "knowledge_graph.sqlite3", ["STALE do not use"])

    dest = tmp_path / "dest"
    create_backup(cfg, [dest])

    with zipfile.ZipFile(_archives(dest)[0]) as zf:
        rows = _rows(zf.read("knowledge_graph.sqlite3"), tmp_path)
    assert "Alice parent_of Max" in rows
    assert "STALE do not use" not in rows


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive FS guard")
def test_dest_inside_palace_rejected_case_insensitively(tmp_path):
    """On Windows a mixed-case dest inside the palace must still be rejected."""
    from mempalace.backup import BackupError, create_backup

    cfg = _build_config_dir(tmp_path / "home")
    palace = cfg / "palace"
    # Same location, deliberately mangled case in the palace portion.
    mixed = Path(str(palace).upper()) / "backups"

    with pytest.raises(BackupError):
        create_backup(cfg, [mixed])
