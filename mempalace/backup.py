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
import os
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
    kg_included: bool = False


def _utc_stamp(now: Optional[datetime]) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _snapshot_sqlite(src: Path, dst: Path) -> str:
    """Take a consistent snapshot of ``src`` into ``dst`` via the backup API.

    Returns the ``PRAGMA quick_check`` result on the snapshot ("ok" when the
    copy is sound).

    To guarantee the *live* database is never mutated, we first filesystem-copy
    the ``.sqlite3`` file together with its ``-wal``/``-shm`` sidecars into a
    private temp dir, then run the online backup API against that COPY. Opening
    the live file with a writable connection risks a close-time WAL checkpoint
    (when the backup is the sole connection) that would rewrite the user's data;
    copying the byte triplet sidesteps that entirely while still folding the WAL
    into the snapshot — and it also lets SQLite rebuild a missing ``-shm`` on the
    throwaway copy rather than the original.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="mempalace_snap_") as snap_tmp:
            tmp_src = Path(snap_tmp) / src.name
            # Copy the db and any live sidecars (order doesn't matter; SQLite
            # reconciles them when the copy is opened).
            for suffix in ("", "-wal", "-shm"):
                side = src.parent / (src.name + suffix)
                if side.exists():
                    shutil.copy2(side, Path(snap_tmp) / (src.name + suffix))

            src_conn = sqlite3.connect(str(tmp_src))
            try:
                dst_conn = sqlite3.connect(str(dst))
                try:
                    src_conn.backup(dst_conn)
                    rows = dst_conn.execute("PRAGMA quick_check").fetchall()
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
    except sqlite3.Error as e:
        raise BackupError(f"failed to snapshot {src}: {e}") from e

    messages = [str(r[0]) for r in rows if r]
    if messages == ["ok"]:
        return "ok"
    return "; ".join(messages) or "unknown"


def _iter_extra_files(palace: Path) -> Iterable[Path]:
    """Yield non-SQLite files under ``palace`` (HNSW index, sentinels).

    Symlinks are skipped so a link planted inside the palace cannot drag
    arbitrary off-palace files into the archive (privacy-by-architecture).
    """
    for path in sorted(palace.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if path.name == "chroma.sqlite3":
            continue
        if path.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        # Never re-capture our own archives (e.g. a dest accidentally inside
        # the palace) — that would balloon every subsequent backup.
        if path.name.startswith(ARCHIVE_PREFIX) and path.name.endswith(ARCHIVE_SUFFIX):
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


def _resolve_kg(config_dir: Path, palace: Path) -> "Path | None":
    """Locate the knowledge graph DB, preferring the palace-relative path.

    The KG lives at ``<palace>/knowledge_graph.sqlite3`` whenever the server
    runs with a relocated ``--palace`` (see mcp_server/fact_checker), and at
    ``<config_dir>/knowledge_graph.sqlite3`` for the default layout.

    We mirror the runtime's relocation intent rather than picking purely on
    existence: in the default layout the config-dir KG is authoritative even if
    a stale palace-side file lingers; only a genuinely relocated palace makes
    the palace-side KG primary.
    """
    relocated = palace.resolve(strict=False) != (config_dir / "palace").resolve(strict=False)
    if relocated:
        order = (palace / "knowledge_graph.sqlite3", config_dir / "knowledge_graph.sqlite3")
    else:
        order = (config_dir / "knowledge_graph.sqlite3", palace / "knowledge_graph.sqlite3")
    for candidate in order:
        if candidate.exists():
            return candidate
    return None


def _stage(config_dir: Path, palace: Path, stage: Path) -> dict[str, str]:
    """Populate ``stage`` with a full, consistent copy of the palace.

    Returns the SQLite quick_check results keyed by archive-relative path.
    """
    checks: dict[str, str] = {}

    (stage / "palace").mkdir(parents=True, exist_ok=True)
    checks["palace/chroma.sqlite3"] = _snapshot_sqlite(
        palace / "chroma.sqlite3", stage / "palace" / "chroma.sqlite3"
    )

    kg = _resolve_kg(config_dir, palace)
    if kg is not None:
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


def _is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or lives underneath it.

    Uses ``os.path.normcase`` + ``commonpath`` so the check is correct on
    case-insensitive filesystems (Windows) even when the destination does not
    yet exist on disk (so ``Path.resolve`` cannot canonicalise its case).
    """
    c = os.path.normcase(os.path.abspath(str(child)))
    p = os.path.normcase(os.path.abspath(str(parent)))
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        # Different drives / UNC roots — cannot be contained.
        return False


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
    if keep is not None and keep < 1:
        raise BackupError(f"keep must be >= 1 (got {keep}); omit it to disable pruning")

    chroma = palace / "chroma.sqlite3"
    if not chroma.exists():
        raise BackupError(f"no palace found: {chroma} does not exist")

    # A destination inside the live tree would be swept into the next backup
    # (recursive bloat) and is almost certainly a mistake. Refuse it.
    for dest in dests:
        if dest.is_symlink():
            raise BackupError(f"destination is a symlink, refusing: {dest}")
        if _is_within(dest, palace) or _is_within(dest, config_dir):
            raise BackupError(
                f"destination {dest} is inside the live palace/config dir; "
                "choose a location outside it"
            )

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

        # Phase A — copy to a hidden ``.part`` in every destination and verify.
        # Nothing visible (and no pruning) happens until every copy succeeds,
        # so a mid-run failure can never leave a corrupt archive under the
        # canonical name nor delete an older good one.
        staged: list[tuple[Path, Path, Path]] = []  # (dest, part, final)
        try:
            for dest in dests:
                dest.mkdir(parents=True, exist_ok=True)
                part = dest / (archive_name + ".part")
                final = dest / archive_name
                shutil.copy2(staged_archive, part)
                if part.stat().st_size != size:
                    raise BackupError(f"size mismatch after copy to {part}")
                staged.append((dest, part, final))
        except Exception as e:
            # Remove every ``.part`` we created (or were mid-writing) so a
            # failed run leaves no garbage and never disturbs existing backups.
            for dest in dests:
                (dest / (archive_name + ".part")).unlink(missing_ok=True)
            if isinstance(e, BackupError):
                raise
            raise BackupError(f"failed to write backup to a destination: {e}") from e

        # Phase B — atomically promote each ``.part`` to its final name. If a
        # promotion fails, remove every not-yet-promoted ``.part`` and report
        # via BackupError (a raw OSError must never escape, and no stray
        # ``.part`` may be left behind). Already-promoted dests keep their good
        # archive — partial promotion is acceptable but must be surfaced.
        dest_paths: list[str] = []
        promoted: set[Path] = set()
        try:
            for _dest, part, final in staged:
                part.replace(final)
                promoted.add(final)
                dest_paths.append(str(final))
        except OSError as e:
            for _dest, part, final in staged:
                if final not in promoted:
                    part.unlink(missing_ok=True)
            raise BackupError(
                f"failed to finalise backup (promoted {len(promoted)} of "
                f"{len(staged)} destinations): {e}"
            ) from e

        # Phase C — retention prune (only now that every dest has a good copy).
        pruned: list[str] = []
        if keep is not None:
            for dest, _part, _final in staged:
                pruned.extend(_prune(dest, keep))

        kg_included = "knowledge_graph.sqlite3" in checks

    return BackupResult(
        archive_name=archive_name,
        size_bytes=size,
        dest_paths=dest_paths,
        file_count=file_count,
        sqlite_checks=checks,
        pruned=pruned,
        kg_included=kg_included,
    )


@dataclass
class VerifyResult:
    """Outcome of :func:`verify_backup`."""

    ok: bool
    errors: list[str]
    file_count: int


@dataclass
class RestoreResult:
    """Outcome of :func:`restore_archive`."""

    config_dir: str
    palace_path: str
    restored_files: int
    kg_restored: bool
    moved_aside: "str | None" = None


def verify_backup(archive_path) -> VerifyResult:
    """Check an archive against its MANIFEST: per-file size+sha256 and the
    recorded SQLite integrity results.

    Raises:
        BackupError: if the file is not a readable zip or lacks a MANIFEST.
    """
    archive_path = Path(archive_path)
    errors: list[str] = []
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = set(zf.namelist())
            if "MANIFEST.json" not in names:
                raise BackupError(f"{archive_path} has no MANIFEST.json — not a MemPalace backup")
            manifest = json.loads(zf.read("MANIFEST.json"))
            files = manifest.get("files", [])
            for entry in files:
                rel = entry["path"]
                if rel not in names:
                    errors.append(f"{rel}: listed in manifest but missing from archive")
                    continue
                data = zf.read(rel)
                if len(data) != entry.get("size"):
                    errors.append(
                        f"{rel}: size mismatch (manifest {entry.get('size')}, got {len(data)})"
                    )
                if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                    errors.append(f"{rel}: sha256 mismatch (content altered)")
            for db, res in manifest.get("sqlite_checks", {}).items():
                if res != "ok":
                    errors.append(f"{db}: recorded integrity check was {res!r}, not 'ok'")
    except zipfile.BadZipFile as e:
        raise BackupError(f"{archive_path} is not a valid zip archive: {e}") from e

    return VerifyResult(ok=not errors, errors=errors, file_count=len(files))


def restore_archive(archive_path, config_dir, *, palace_path=None, force=False) -> RestoreResult:
    """Restore a palace from a backup archive into ``config_dir``.

    Verifies the archive first (refusing a tampered one), refuses to clobber a
    non-empty palace unless ``force`` is set (moving the old palace aside rather
    than deleting it), lays down the verbatim store + KG + config, and clears
    any stale ``-wal``/``-shm``/``-journal`` sidecars that would corrupt the
    restored database.

    NOTE: the archived HNSW index is advisory; after restore, run
    ``mempalace repair`` to regenerate it from the restored ``chroma.sqlite3``.
    """
    archive_path = Path(archive_path)
    config_dir = Path(config_dir)
    palace = Path(palace_path) if palace_path else config_dir / "palace"

    verdict = verify_backup(archive_path)
    if not verdict.ok:
        raise BackupError(
            f"refusing to restore — archive failed verification: {'; '.join(verdict.errors)}"
        )

    palace_nonempty = palace.exists() and any(palace.iterdir())
    if palace_nonempty and not force:
        raise BackupError(
            f"target palace {palace} is not empty; pass force=True to overwrite "
            "(the existing palace will be moved aside, not deleted)"
        )

    with tempfile.TemporaryDirectory(prefix="mempalace_restore_") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(tmp)

        config_dir.mkdir(parents=True, exist_ok=True)

        # Move an existing palace aside (never destroy) before laying the new one.
        moved_aside: "str | None" = None
        if palace_nonempty:
            stamp = _utc_stamp(None)
            aside = palace.with_name(palace.name + f".pre-restore-{stamp}")
            palace.replace(aside)
            moved_aside = str(aside)

        shutil.copytree(tmp / "palace", palace, dirs_exist_ok=True)

        # Defensive: clear any sidecars so a stale -wal cannot corrupt the db.
        for suffix in _EXCLUDED_SUFFIXES:
            (palace / ("chroma.sqlite3" + suffix)).unlink(missing_ok=True)

        # Place the KG where the runtime expects it for this layout.
        kg_member = tmp / "knowledge_graph.sqlite3"
        kg_restored = False
        if kg_member.exists():
            relocated = palace.resolve(strict=False) != (config_dir / "palace").resolve(
                strict=False
            )
            kg_target = (palace if relocated else config_dir) / "knowledge_graph.sqlite3"
            kg_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kg_member, kg_target)
            kg_restored = True

        restored_files = sum(1 for p in palace.rglob("*") if p.is_file())

        for name in _CONFIG_FILES:
            member = tmp / name
            if member.exists():
                shutil.copy2(member, config_dir / name)
                restored_files += 1

        wal_member = tmp / "wal" / "write_log.jsonl"
        if wal_member.exists():
            (config_dir / "wal").mkdir(exist_ok=True)
            shutil.copy2(wal_member, config_dir / "wal" / "write_log.jsonl")
            restored_files += 1

    return RestoreResult(
        config_dir=str(config_dir),
        palace_path=str(palace),
        restored_files=restored_files,
        kg_restored=kg_restored,
        moved_aside=moved_aside,
    )


__all__ = [
    "create_backup",
    "verify_backup",
    "restore_archive",
    "BackupResult",
    "VerifyResult",
    "RestoreResult",
    "BackupError",
]
