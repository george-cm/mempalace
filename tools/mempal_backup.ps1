<#
.SYNOPSIS
    Personal, local automation that runs a SQLite-safe MemPalace backup and
    (optionally) pushes a copy to a Synology NAS over SSH.

.DESCRIPTION
    PRIVACY NOTE — READ THIS. MemPalace stores your verbatim memory and is
    local-first by design ("your data never leaves your machine"). This script
    is NOT part of MemPalace core; it is a user-owned convenience wrapper.
    Every destination you pass it (a OneDrive-synced folder, a NAS, a network
    share) copies your verbatim memory OFF this machine. That is your explicit
    choice. Nothing here defaults to a cloud location, and the script refuses
    to run until you name a destination yourself.

    Steps:
      1. `mempalace backup` writes a consistent, timestamped .zip into -OutDir
         and prunes -OutDir to the newest -Keep archives. (If -OutDir happens
         to be inside a OneDrive folder, OneDrive will sync it to the cloud —
         that is the user's decision, not a default of this tool.)
      2. If -NasAlias is given, the newest archive is uploaded to the NAS
         atomically (temp name then rename) and the NAS is pruned to -NasKeep.

    The live palace is only ever read — this never mutates your memory.

.PARAMETER OutDir
    REQUIRED. Local directory to write the archive into. No default — you must
    consciously choose where your verbatim memory is stored.

.PARAMETER NasAlias
    Optional SSH host/alias (e.g. an entry in ~/.ssh/config). When omitted, the
    NAS leg is skipped entirely and the backup is purely local.

.NOTES
    Designed to be invoked by Task Scheduler; see register_backup_task.ps1.
    The repo location is derived from this script's own path, so it is portable
    across machines/users.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,
    [int]$Keep        = 14,
    [string]$NasAlias = "",
    [string]$NasPath  = "backups/mempalace",
    [int]$NasKeep     = 30,
    [string]$RepoDir  = (Split-Path $PSScriptRoot -Parent),
    # Audit log lives on LOCAL disk (never a synced/cloud folder) so it is
    # always writable under non-interactive Task Scheduler sessions and never
    # leaks local paths/hostname to a cloud tenant.
    [string]$LogFile  = (Join-Path $env:LOCALAPPDATA "MemPalace\backup.log")
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line  = "$stamp [$Level] $Message"
    Write-Output $line
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $LogFile -Parent) | Out-Null
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    }
    catch {
        Write-Warning "Could not write log to ${LogFile}: $($_.Exception.Message)"
    }
}

# Track the two legs independently: a NAS hiccup must not mask a good local
# backup, and a failed local backup is always fatal.
$localOk = $false
$nasOk   = $true   # stays true when no NAS leg is requested

try {
    if (-not $OutDir) { throw "OutDir is empty. Pass -OutDir <local folder>." }
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    Write-Log "Backup started. OutDir=$OutDir Keep=$Keep Nas='$NasAlias' NasPath=$NasPath NasKeep=$NasKeep Repo=$RepoDir"

    # 1. Produce the archive locally (also prunes -OutDir to the newest -Keep).
    & uv run --project $RepoDir mempalace backup --out $OutDir --keep $Keep
    if ($LASTEXITCODE -ne 0) {
        Write-Log "mempalace backup exited with code $LASTEXITCODE" "ERROR"
        exit 1
    }
    $localOk = $true

    $archive = Get-ChildItem -Path $OutDir -Filter "mempalace-backup-*.zip" |
        Sort-Object Name | Select-Object -Last 1
    if (-not $archive) {
        Write-Log "No archive found in $OutDir after backup" "ERROR"
        exit 1
    }
    $sizeMb = [math]::Round($archive.Length / 1MB, 1)
    Write-Log "Local archive: $($archive.Name) ($sizeMb MB)"

    # 2. Optional NAS push (off-disk copy). Skipped entirely without -NasAlias.
    if ($NasAlias) {
        $nasOk = $false
        try {
            $name = $archive.Name
            $tmp  = "$NasPath/.$name.partial"

            & ssh -o LogLevel=ERROR $NasAlias "mkdir -p '$NasPath'"
            if ($LASTEXITCODE -ne 0) { throw "NAS mkdir failed ($LASTEXITCODE)" }

            # Upload to a temp name, then atomically rename, so a killed/cut
            # transfer never leaves a truncated archive under the real name.
            & scp -o LogLevel=ERROR $archive.FullName "${NasAlias}:${tmp}"
            if ($LASTEXITCODE -ne 0) { throw "NAS scp failed ($LASTEXITCODE)" }

            & ssh -o LogLevel=ERROR $NasAlias "mv -f '$tmp' '$NasPath/$name'"
            if ($LASTEXITCODE -ne 0) { throw "NAS rename failed ($LASTEXITCODE)" }
            Write-Log "Copied to ${NasAlias}:$NasPath/$name"

            # Prune NAS to the newest $NasKeep archives. POSIX/BusyBox-safe:
            # newest-first sort, skip the N to keep, delete the rest. (GNU-only
            # `head -n -N` is NOT used — Synology BusyBox rejects it.)
            $keepPlus = $NasKeep + 1
            $pruneCmd = "ls -1 '$NasPath'/mempalace-backup-*.zip 2>/dev/null | sort -r | tail -n +$keepPlus | xargs -r rm -f"
            & ssh -o LogLevel=ERROR $NasAlias $pruneCmd
            if ($LASTEXITCODE -ne 0) { Write-Log "NAS prune returned $LASTEXITCODE (non-fatal)" "WARN" }

            $nasOk = $true
        }
        catch {
            # A NAS failure is non-fatal: the local (primary) backup succeeded.
            Write-Log "NAS leg failed (local backup is intact): $($_.Exception.Message)" "WARN"
        }
    }

    if ($localOk -and $nasOk) {
        Write-Log "Backup complete (local + NAS)."
        exit 0
    }
    elseif ($localOk) {
        Write-Log "Backup complete locally; NAS copy did NOT succeed." "WARN"
        exit 3   # distinct code: local good, NAS failed
    }
}
catch {
    Write-Log "Unhandled error: $($_.Exception.Message)" "ERROR"
    exit 1
}
