<#
.SYNOPSIS  Register (or remove) one Windows Scheduled Task that runs scripts\run-vault-maintenance.ps1 every day:
           vault backup snapshot + vault_lint. Idempotent (re-running replaces the task). Supports -WhatIf.
           THIS CHANGES THE MACHINE (one scheduled task for the current user, no admin rights, no password stored).
.EXAMPLE   .\scripts\install-vault-maintenance-task.ps1 -WhatIf
.EXAMPLE   .\scripts\install-vault-maintenance-task.ps1 -Time 21:00 -BackupDir D:\second-brain-backup -Keep 30
.EXAMPLE   .\scripts\install-vault-maintenance-task.ps1 -Uninstall
.NOTES     Runs as the current user while that user is logged on; if the PC was off at the planned time the task runs at the
           next opportunity (StartWhenAvailable). No model is called, so it costs no tokens.
           Check:  Get-ScheduledTask -TaskName LLMWiki-VaultMaintenance | Get-ScheduledTaskInfo
           Run now: Start-ScheduledTask -TaskName LLMWiki-VaultMaintenance ; then read <BackupDir>\last-maintenance.txt
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Time = '21:00',
    [string]$BackupDir = '',
    [int]$Keep = 30,
    [string]$TaskName = 'LLMWiki-VaultMaintenance',
    [switch]$Uninstall
)
. (Join-Path $PSScriptRoot '_common.ps1')
$root = $script:RepoRoot

if ($Uninstall) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'remove the scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Output "removed task ${TaskName} (backups already written are kept)"
    }
    return
}

if ($Time -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw "-Time must look like 21:00 (24-hour HH:mm), got '$Time'" }
if ($Keep -lt 1) { throw '-Keep must be at least 1' }
if (-not $BackupDir) {
    if ($env:WIKI_VAULT_BACKUP_DIR) { $BackupDir = $env:WIKI_VAULT_BACKUP_DIR }
    else { $BackupDir = Join-Path (Split-Path -Parent $root) 'second-brain-backup' }
}
$script = Join-Path $PSScriptRoot 'run-vault-maintenance.ps1'
$arg = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -BackupDir "' + $BackupDir + '" -Keep ' + $Keep

if ($PSCmdlet.ShouldProcess($TaskName, "register a daily task at $Time (backup to $BackupDir, keep $Keep, then vault_lint)")) {
    $action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $root
    $trigger  = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
        -Description 'Second-brain vault: backup snapshot + vault_lint (see scripts\run-vault-maintenance.ps1)' | Out-Null
    Write-Output "registered task ${TaskName}: daily at ${Time}, backup folder ${BackupDir}, keep ${Keep} snapshots"
}
