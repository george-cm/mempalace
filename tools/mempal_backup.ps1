<#
.SYNOPSIS
    Run a SQLite-safe MemPalace backup to a local (OneDrive-synced) folder and
    push a copy to the Synology NAS.

.DESCRIPTION
    Two-destination backup:
      1. `mempalace backup` writes a consistent, timestamped .zip into -OutDir.
         When -OutDir lives under OneDrive, the archive syncs to the cloud
         automatically, giving an off-machine copy.
      2. The newest archive is copied to the NAS over SSH (off-disk, on-LAN),
         and both locations are pruned to their retention limits.

    The live palace is only ever read - this never mutates your memory.

.NOTES
    NAS access uses the `enterprise` SSH alias (configured in ~/.ssh/config).
    Designed to be invoked by Task Scheduler; see register_backup_task.ps1.
#>
[CmdletBinding()]
param(
    [string]$RepoDir = "C:\Users\George.Murga\projects\mempalace",
    [string]$OutDir  = (Join-Path $env:OneDrive "MemPalaceBackups"),
    [int]$Keep       = 14,
    [string]$NasAlias = "enterprise",
    [string]$NasPath  = "backups/mempalace",
    [int]$NasKeep     = 30,
    [string]$LogFile  = (Join-Path $env:OneDrive "MemPalaceBackups\backup.log")
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line  = "$stamp [$Level] $Message"
    Write-Output $line
    try { Add-Content -Path $LogFile -Value $line -Encoding utf8 } catch { }
}

try {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    Write-Log "Backup started. OutDir=$OutDir Keep=$Keep Nas=${NasAlias}:$NasPath NasKeep=$NasKeep"

    # 1. Produce the archive locally (also prunes -OutDir to -Keep).
    & uv run --project $RepoDir mempalace backup --out $OutDir --keep $Keep
    if ($LASTEXITCODE -ne 0) {
        Write-Log "mempalace backup exited with code $LASTEXITCODE" "ERROR"
        exit 1
    }

    $archive = Get-ChildItem -Path $OutDir -Filter "mempalace-backup-*.zip" |
        Sort-Object Name | Select-Object -Last 1
    if (-not $archive) {
        Write-Log "No archive found in $OutDir after backup" "ERROR"
        exit 1
    }
    $sizeMb = [math]::Round($archive.Length / 1MB, 1)
    Write-Log "Local archive: $($archive.Name) ($sizeMb MB)"

    # 2. Push to NAS over SSH (off-disk copy).
    & ssh -o LogLevel=ERROR $NasAlias "mkdir -p '$NasPath'"
    if ($LASTEXITCODE -ne 0) { Write-Log "NAS mkdir failed ($LASTEXITCODE)" "ERROR"; exit 1 }

    & scp -o LogLevel=ERROR $archive.FullName "${NasAlias}:${NasPath}/"
    if ($LASTEXITCODE -ne 0) { Write-Log "NAS scp failed ($LASTEXITCODE)" "ERROR"; exit 1 }
    Write-Log "Copied to ${NasAlias}:$NasPath/$($archive.Name)"

    # 3. Prune NAS to the newest $NasKeep archives.
    $pruneCmd = "ls -1 '$NasPath'/mempalace-backup-*.zip 2>/dev/null | sort | head -n -$NasKeep | xargs -r rm -f"
    & ssh -o LogLevel=ERROR $NasAlias $pruneCmd
    if ($LASTEXITCODE -ne 0) { Write-Log "NAS prune returned $LASTEXITCODE (non-fatal)" "WARN" }

    Write-Log "Backup complete."
    exit 0
}
catch {
    Write-Log "Unhandled error: $($_.Exception.Message)" "ERROR"
    exit 1
}
