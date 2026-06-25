"""
backup.py — SQLite-safe, full-fidelity backups of a MemPalace.

A MemPalace lives under a config directory (default ``~/.mempalace``) and is
split across two SQLite databases plus a handful of plain files:

  * ``palace/chroma.sqlite3``    — the verbatim drawer store (source of truth)
  * ``palace/<segment>/*.bin``   — the HNSW vector index (rebuildable)
  * ``knowledge_graph.sqlite3``  — the temporal entity-relationship graph
  * ``config.json`` / ``identity.txt`` / ``wing_config.json`` / ``people_map.json``
  * ``wal/write_log.jsonl``      — the application write-ahead log

Backing up SQLite by copying the ``.sqlite3`` file alone is unsafe: recently
written rows can live in the ``-wal`` sidecar and a plain copy either misses
them or captures a torn page. This module uses the SQLite online backup API
(``Connection.backup``) to take a *consistent* snapshot that folds in the WAL,
honouring MemPalace's promise to never lose a word.

The live palace is only ever read — nothing here mutates user data, satisfying
the incremental-only / never-destroy design principle.
"""

import hashlib
import json
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from .version import __version__ as _MEMPALACE_VERSION
except Exception:  # pragma: no cover - version module should always import
    _MEMPALACE_VERSION = "unknown"


ARCHIVE_PREFIX = "mempalace-backup-"
ARCHIVE_SUFFIX = ".zip"

# Plain (non-SQLite) files copied verbatim from the config dir, when present.
_CONFIG_FILES = (
    "config.json",
    "identity.txt",
    "wing_config.json",
    "people_map.json",
)

# Directory names under the config dir that hold transient runtime state we
# deliberately exclude: live locks, the stale repair leftover, and hook logs.
_EXCLUDED_DIRS = {"locks", "palace.backup", "hook_state", "__pycache__"}

# File suffixes never worth archiving: SQLite sidecars (the backup API already
# folds their contents into the snapshot) and rollback journals.
_EXCLUDED_SUFFIXES = ("-shm", "-wal", "-journal")


class BackupError(RuntimeError):
    """Raised when a backup cannot be produced safely."""


@dataclass
class BackupResult:
    """Outcome of a successful :func:`create_backup` call."""

    archive_name: str
    size_bytes: int
    dest_paths: list[str]
    file_count: int
    sqlite_checks: dict[str, str] = field(default_factory=dict)
    pruned: list[str] = field(default_factory=list)


def _utc_stamp(now: Optional[datetime]) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _snapshot_sqlite(src: Path, dst: Path) -> str:
    """Take a consistent snapshot of ``src`` into ``dst`` via the backup API.

    Returns the ``PRAGMA quick_check`` result on the snapshot ("ok" when the
    copy is sound). Opening the source read-only guarantees the live database
    is never written to.
    """
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            rows = dst_conn.execute("PRAGMA quick_check").fetchall()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    messages = [str(r[0]) for r in rows if r]
    if messages == ["ok"]:
        return "ok"
    return "; ".join(messages) or "unknown"


def _iter_extra_files(palace: Path) -> Iterable[Path]:
    """Yield non-SQLite files under ``palace`` (HNSW index, sentinels)."""
    for path in sorted(palace.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "chroma.sqlite3":
            continue
        if path.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(palace).parts):
            continue
        yield path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stage(config_dir: Path, palace: Path, stage: Path) -> dict[str, str]:
    """Populate ``stage`` with a full, consistent copy of the palace.

    Returns the SQLite quick_check results keyed by archive-relative path.
    """
    checks: dict[str, str] = {}

    (stage / "palace").mkdir(parents=True, exist_ok=True)
    checks["palace/chroma.sqlite3"] = _snapshot_sqlite(
        palace / "chroma.sqlite3", stage / "palace" / "chroma.sqlite3"
    )

    kg = config_dir / "knowledge_graph.sqlite3"
    if kg.exists():
        checks["knowledge_graph.sqlite3"] = _snapshot_sqlite(kg, stage / "knowledge_graph.sqlite3")

    # HNSW index + any sentinel files under palace/.
    for path in _iter_extra_files(palace):
        rel = Path("palace") / path.relative_to(palace)
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    # Plain config files.
    for name in _CONFIG_FILES:
        src = config_dir / name
        if src.exists():
            shutil.copy2(src, stage / name)

    # Application write-ahead log.
    wal = config_dir / "wal" / "write_log.jsonl"
    if wal.exists():
        (stage / "wal").mkdir(exist_ok=True)
        shutil.copy2(wal, stage / "wal" / "write_log.jsonl")

    return checks


def _write_manifest(stage: Path, checks: dict[str, str], stamp: str) -> int:
    """Write MANIFEST.json describing every staged file. Returns file count."""
    files = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        rel = path.relative_to(stage).as_posix()
        files.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})

    manifest = {
        "format": "mempalace-backup/1",
        "created_at": stamp,
        "mempalace_version": _MEMPALACE_VERSION,
        "hostname": socket.gethostname(),
        "sqlite_checks": checks,
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    (stage / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return len(files)


def _zip_stage(stage: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())


def _prune(dest: Path, keep: int) -> list[str]:
    """Delete all but the ``keep`` newest archives in ``dest``.

    Archive names are timestamp-sortable (``...-YYYYMMDDTHHMMSSZ.zip``), so a
    lexical sort orders them chronologically. Returns the names pruned.
    """
    archives = sorted(dest.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}"), key=lambda p: p.name)
    pruned: list[str] = []
    for old in archives[:-keep] if keep > 0 else []:
        old.unlink()
        pruned.append(old.name)
    return pruned


def create_backup(
    config_dir,
    dest_dirs,
    *,
    palace_path=None,
    keep: Optional[int] = None,
    now: Optional[datetime] = None,
) -> BackupResult:
    """Create a single timestamped backup archive in each destination dir.

    Args:
        config_dir: MemPalace config directory (e.g. ``~/.mempalace``).
        dest_dirs: One or more directories to receive the ``.zip`` archive.
        palace_path: Override the ChromaDB palace dir (defaults to
            ``config_dir/palace``).
        keep: When set, retain only the ``keep`` newest archives per
            destination, pruning older ones after the new archive lands.
        now: Injectable timestamp for deterministic archive names (tests).

    Returns:
        :class:`BackupResult` describing the archive and where it landed.

    Raises:
        BackupError: if the palace is missing or a SQLite snapshot fails its
            integrity check (no partial archive is published in that case).
    """
    config_dir = Path(config_dir)
    palace = Path(palace_path) if palace_path else config_dir / "palace"
    dests = [Path(d) for d in dest_dirs]
    if not dests:
        raise BackupError("at least one destination directory is required")

    chroma = palace / "chroma.sqlite3"
    if not chroma.exists():
        raise BackupError(f"no palace found: {chroma} does not exist")

    stamp = _utc_stamp(now)
    archive_name = f"{ARCHIVE_PREFIX}{stamp}{ARCHIVE_SUFFIX}"

    with tempfile.TemporaryDirectory(prefix="mempalace_backup_") as tmp:
        tmp = Path(tmp)
        stage = tmp / "stage"
        stage.mkdir()

        checks = _stage(config_dir, palace, stage)
        bad = {k: v for k, v in checks.items() if v != "ok"}
        if bad:
            raise BackupError(
                "SQLite integrity check failed; backup aborted to avoid "
                f"shipping a corrupt snapshot: {bad}"
            )

        file_count = _write_manifest(stage, checks, stamp)

        staged_archive = tmp / archive_name
        _zip_stage(stage, staged_archive)
        size = staged_archive.stat().st_size

        dest_paths: list[str] = []
        pruned: list[str] = []
        for dest in dests:
            dest.mkdir(parents=True, exist_ok=True)
            final = dest / archive_name
            shutil.copy2(staged_archive, final)
            if final.stat().st_size != size:
                raise BackupError(f"size mismatch after copy to {final}")
            dest_paths.append(str(final))
            if keep is not None:
                pruned.extend(_prune(dest, keep))

    return BackupResult(
        archive_name=archive_name,
        size_bytes=size,
        dest_paths=dest_paths,
        file_count=file_count,
        sqlite_checks=checks,
        pruned=pruned,
    )


__all__ = ["create_backup", "BackupResult", "BackupError"]
