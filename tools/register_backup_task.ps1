<#
.SYNOPSIS
    Register (or update) a daily Windows Scheduled Task that runs the MemPalace
    backup wrapper (mempal_backup.ps1).

.DESCRIPTION
    Creates a task named "MemPalace Backup" that runs once a day at -Time using
    the S4U logon type, so it runs whether or not you are logged on - without
    storing a password. SSH key auth to the NAS still works because the task
    runs as you and can read your ~/.ssh profile.

    PRIVACY NOTE: -OutDir is where your verbatim memory archive is written. If
    you point it at a OneDrive-synced folder it will be uploaded to the cloud;
    if you pass -NasAlias it is also pushed to that host. Both are your explicit
    choice — see the privacy note in mempal_backup.ps1.

    Re-running this script updates the existing task in place.

.EXAMPLE
    # Local-only daily backup:
    pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1 -OutDir D:\Backups\MemPalace

    # Local + NAS, custom time:
    pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1 -OutDir D:\Backups\MemPalace -NasAlias enterprise -Time 03:15
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,
    [int]$Keep          = 14,
    [string]$NasAlias   = "",
    [int]$NasKeep       = 30,
    [string]$Time       = "02:30",
    [int]$TimeLimitHours = 2,
    [string]$TaskName   = "MemPalace Backup",
    [string]$ScriptPath = (Join-Path $PSScriptRoot "mempal_backup.ps1")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ScriptPath)) {
    throw "Backup script not found: $ScriptPath"
}

# Prefer PowerShell 7 (pwsh) if present, else fall back to Windows PowerShell.
$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
$pwsh = if ($pwshCmd) { $pwshCmd.Source } else { "powershell.exe" }

# Build the wrapper arguments. Quote paths to survive spaces.
$wrapperArgs = "-OutDir `"$OutDir`" -Keep $Keep"
if ($NasAlias) {
    $wrapperArgs += " -NasAlias `"$NasAlias`" -NasKeep $NasKeep"
}

$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $wrapperArgs"

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# Run as the current user, without stored password, whether logged on or not.
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U -RunLevel Limited

# Wake/catch-up friendly settings: run a missed task once the machine is back,
# allow running on battery, and cap runtime so a hung backup cannot linger.
# The cap must comfortably exceed the time to zip + upload a large palace; size
# it via -TimeLimitHours rather than a hard-coded 1h that could kill an scp.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours $TimeLimitHours)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Daily SQLite-safe MemPalace backup (local-first; off-machine destinations are user-chosen)." `
    -Force | Out-Null

Write-Output "Registered scheduled task '$TaskName' (daily at $Time)."
Write-Output "Runner: $pwsh"
Write-Output "Script: $ScriptPath $wrapperArgs"
Write-Output "Run now to test:  Start-ScheduledTask -TaskName '$TaskName'"
