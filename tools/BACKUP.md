# MemPalace backup & restore

`mempalace backup` is a first-class, **local-first** command: it writes a
SQLite-safe, full-fidelity archive of your palace to a directory you choose.
It never sends anything anywhere by itself.

The two PowerShell scripts in this folder are **optional, user-owned
automation** - they are not part of MemPalace core. They exist to schedule the
backup and (optionally) copy the archive off this machine.

## Privacy - read before scheduling

MemPalace stores your **verbatim** memory and is local-first by design: your
data never leaves your machine. The moment you point a backup at a OneDrive
folder, a NAS, or any network share, a complete copy of that verbatim memory
leaves the machine. **That is your explicit choice.** These tools:

- never default to a cloud location,
- refuse to run until you name a destination yourself (`-OutDir` is required),
- keep their log on local disk (`%LOCALAPPDATA%\MemPalace\backup.log`), not in a synced folder.

If you point `-OutDir` at a work/enterprise OneDrive tenant, your personal
memory lands in a space your employer controls. Choose deliberately.

## What's in an archive

A timestamped `mempalace-backup-YYYYMMDDTHHMMSSZ.zip` containing:

| Entry | What it is |
|-------|------------|
| `palace/chroma.sqlite3` | verbatim drawers - the source of truth |
| `palace/<segment>/*.bin` | HNSW vector index (rebuildable from chroma) |
| `knowledge_graph.sqlite3` | temporal entity-relationship graph |
| `config.json`, `identity.txt`, `wing_config.json`, `people_map.json` | config |
| `wal/write_log.jsonl` | application write-ahead log |
| `MANIFEST.json` | per-file SHA-256 + size, SQLite `quick_check` results, version, timestamp |

SQLite databases are captured through the online backup API after a
filesystem copy of the db + `-wal`/`-shm`, so uncommitted-WAL rows are folded
in and the **live palace is never mutated**. The archive is verified
(`quick_check`) before it is published; a failed check publishes nothing.

## Manual restore

Until a `mempalace restore` command exists, restore by hand:

1. **Stop the MCP server** and anything else touching the palace (it caches the
   index and pins SQLite connections).
2. **Verify the archive.** Extract it, and for every entry in `MANIFEST.json`
   recompute the SHA-256 and confirm it matches; the `sqlite_checks` should all
   read `"ok"`.
3. **Clean the target palace dir.** Delete any existing
   `chroma.sqlite3-wal`, `-shm`, `-journal` - the snapshot already folded the
   WAL in, and a leftover live `-wal` would corrupt the restored db on open.
4. **Lay files down:** `palace/*` into the palace dir; `knowledge_graph.sqlite3`
   and the config files into the config dir (`~/.mempalace` by default);
   `wal/write_log.jsonl` into `<config_dir>/wal/`.
5. **Regenerate the index:** run `mempalace repair` (after `mempalace repair
   status`). The archived `*.bin` HNSW index is advisory only - it can lag the
   chroma snapshot - so `repair` rebuilds it from `chroma.sqlite3`, the source
   of truth.
6. **Restart the server.**

## Scheduled backups (Windows)

`mempal_backup.ps1` runs `mempalace backup` into a local folder and, if you
pass `-NasAlias`, also uploads the newest archive to that SSH host (atomically:
temp name then rename) and prunes the host to `-NasKeep` archives. A NAS failure
is non-fatal - the local backup still counts as success (exit code `3` flags
"local OK, NAS failed").

**Where on the NAS does it land?** At the path given by `-NasPath`, which
defaults to `backups/mempalace` **relative to the SSH login's home directory**
on that host (e.g. for the `enterprise` alias / user `george`, that is
`~/backups/mempalace/` -> `/var/services/homes/george/backups/mempalace/` on a
typical Synology). Pass an absolute `-NasPath` (e.g. `/volume1/backups/mempalace`)
to put it elsewhere; the script creates the directory if missing.

**What is "S4U logon"?** S4U ("Service for User") is a Windows Scheduled-Task
logon type that lets the task run as you **without storing your password** and
**whether or not you are logged on**. It grants a local-only token: the task can
read your profile (so SSH key auth from `~/.ssh` still works) but cannot use your
credentials to reach password-protected network shares. Key-based `scp`/`ssh` to
the NAS is unaffected.

**Will it run if my laptop is off at the scheduled time?** Yes. The task is
registered with `-StartWhenAvailable`, so a run missed because the machine was
asleep/off fires as soon as the machine is next on. The time itself is fully
configurable via `-Time` (24h `HH:mm`); pick a time you are usually powered on,
or rely on the catch-up behaviour. It also runs on battery.

```powershell
# Daily backup at a time you choose (e.g. 12:30); catches up if the machine
# was off at that time:
pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1 `
    -OutDir D:\Backups\MemPalace -Time 12:30

# Local + NAS over the `enterprise` SSH alias, custom NAS path:
pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1 `
    -OutDir D:\Backups\MemPalace -NasAlias enterprise -NasPath /volume1/backups/mempalace -Time 03:15

# Test the wrapper once, by hand:
pwsh -ExecutionPolicy Bypass -File tools\mempal_backup.ps1 -OutDir D:\Backups\MemPalace
```

Exit codes from `mempal_backup.ps1`: `0` = local (+NAS if requested) succeeded;
`3` = local succeeded but the NAS copy did not; `1` = local backup failed.
