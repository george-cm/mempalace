<#
.SYNOPSIS
    Register (or update) a daily Windows Scheduled Task that runs the MemPalace
    backup wrapper (mempal_backup.ps1).

.DESCRIPTION
    Creates a task named "MemPalace Backup" that runs once a day at -Time using
    the S4U logon type, so it runs whether or not you are logged on - without
    storing a password. SSH key auth to the NAS still works because the task
    runs as you and can read your ~/.ssh profile.

    Re-running this script updates the existing task in place.

.EXAMPLE
    pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1
    pwsh -ExecutionPolicy Bypass -File tools\register_backup_task.ps1 -Time 03:15
#>
[CmdletBinding()]
param(
    [string]$TaskName   = "MemPalace Backup",
    [string]$Time       = "02:30",
    [string]$ScriptPath = (Join-Path $PSScriptRoot "mempal_backup.ps1")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ScriptPath)) {
    throw "Backup script not found: $ScriptPath"
}

# Prefer PowerShell 7 (pwsh) if present, else fall back to Windows PowerShell.
$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
$pwsh = if ($pwshCmd) { $pwshCmd.Source } else { "powershell.exe" }

$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# Run as the current user, without stored password, whether logged on or not.
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U -RunLevel Limited

# Wake/catch-up friendly settings: run a missed task once the machine is back,
# allow running on battery, and cap runtime so a hung backup cannot linger.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Daily SQLite-safe MemPalace backup to OneDrive + NAS." `
    -Force | Out-Null

Write-Output "Registered scheduled task '$TaskName' (daily at $Time)."
Write-Output "Runner: $pwsh"
Write-Output "Script: $ScriptPath"
Write-Output "Run now to test:  Start-ScheduledTask -TaskName '$TaskName'"
