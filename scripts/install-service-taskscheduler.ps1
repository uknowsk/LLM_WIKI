<#
.SYNOPSIS
  Register the wiki web server and the pipeline watcher as Windows Scheduled Tasks that start at boot and run as a
  service account. Idempotent (re-running replaces the two tasks). Supports -WhatIf (prints, changes nothing).
  Requires an elevated PowerShell. THIS SCRIPT CHANGES THE MACHINE: run it only on the target server, on purpose.

.DESCRIPTION
  Tasks created:  <Prefix>-Web       -> scripts\run-web.ps1 -Supervise
                  <Prefix>-Pipeline  -> scripts\run-pipeline.ps1 -Supervise
  Each task: trigger = At startup, no time limit, runs whether or not anyone is logged on, restarts on failure
  (Task Scheduler restart policy) AND the -Supervise loop restarts python itself after any exit.
  Logs: <LogDir>\web-yyyyMMdd.out.log/.err.log, pipeline-*.log, *-supervisor.log (daily files; files older than
  -KeepDays are deleted each time the process (re)starts).

  The service account needs: read on the repo folder, read/write on WIKI_DATA_DIR and LogDir, read on the .env
  file and on any *_FILE secret files. Give it NO interactive-logon right and NO other privileges.
  Prefer a dedicated domain account or a gMSA (pass -ServiceAccount 'DOMAIN\gmsa-wiki$'; no password is asked).
  NT AUTHORITY\SYSTEM works but is far more privileged than needed - avoid it.

  Verify after install:  Get-ScheduledTask -TaskName 'LLMWiki-*' ; Start-ScheduledTask -TaskName 'LLMWiki-Web'
  Remove:                .\scripts\install-service-taskscheduler.ps1 -Uninstall

  ALTERNATIVE (not scripted here, nothing is downloaded by this repo): NSSM (https://nssm.cc) can wrap
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\run-web.ps1` as a real Windows service
  with its own stdout/stderr rotation and restart-delay settings. It must be approved/brought in by IT first.

.EXAMPLE
  .\scripts\install-service-taskscheduler.ps1 -ServiceAccount 'CORP\svc-wiki' -LogDir D:\wiki-logs -WhatIf
  .\scripts\install-service-taskscheduler.ps1 -ServiceAccount 'CORP\svc-wiki' -LogDir D:\wiki-logs
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$ServiceAccount = '',
    [pscredential]$Credential,
    [string]$LogDir = '',
    [string]$EnvFile = '',
    [int]$KeepDays = 14,
    [string]$Prefix = 'LLMWiki',
    [switch]$SkipPipeline,
    [switch]$Uninstall
)
. (Join-Path $PSScriptRoot '_common.ps1')
$root = $script:RepoRoot
if (-not $LogDir) { $LogDir = Join-Path $root 'logs' }
if (-not $EnvFile) { $EnvFile = Join-Path $root '.env' }

$tasks = @(@{ Name = "$Prefix-Web"; Script = 'run-web.ps1' })
if (-not $SkipPipeline) { $tasks += @{ Name = "$Prefix-Pipeline"; Script = 'run-pipeline.ps1' } }

if ($Uninstall) {
    foreach ($t in $tasks) {
        if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($t.Name, 'Unregister scheduled task')) {
                Stop-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
                Write-Host "[service] removed $($t.Name)"
            }
        } else { Write-Host "[service] $($t.Name) not present" }
    }
    return
}

if (-not $ServiceAccount) { throw 'Pass -ServiceAccount (e.g. CORP\svc-wiki or CORP\gmsa-wiki$).' }
$builtin = @('NT AUTHORITY\SYSTEM', 'NT AUTHORITY\LOCAL SERVICE', 'NT AUTHORITY\NETWORK SERVICE')
$isBuiltin = $builtin -contains $ServiceAccount  # -contains is case-insensitive
$isGmsa = $ServiceAccount.EndsWith('$')
if ($ServiceAccount -match 'SYSTEM') { Write-Warning 'SYSTEM is over-privileged for this app; use a dedicated low-privilege account.' }

$plain = $null
if (-not $isBuiltin -and -not $isGmsa) {
    if ($PSCmdlet.ShouldProcess($ServiceAccount, 'ask for the account password')) {
        if (-not $Credential) { $Credential = Get-Credential -UserName $ServiceAccount -Message 'Password of the service account (not stored by this script)' }
        $plain = $Credential.GetNetworkCredential().Password
    }
}

foreach ($t in $tasks) {
    $script = Join-Path $root ("scripts\" + $t.Script)
    $argText = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Supervise -LogDir "{1}" -KeepDays {2} -EnvFile "{3}"' -f $script, $LogDir, $KeepDays, $EnvFile
    $exists = [bool](Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue)
    $verb = if ($exists) { 'Replace' } else { 'Create' }
    if (-not $PSCmdlet.ShouldProcess($t.Name, "$verb scheduled task (run as $ServiceAccount, at startup)")) { continue }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argText -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    if ($isBuiltin) {
        $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType ServiceAccount -RunLevel Limited
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    } elseif ($isGmsa) {
        $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings `
            -User $ServiceAccount -Password $plain -RunLevel Limited -Force | Out-Null
    }
    Write-Host "[service] done: $verb $($t.Name) (logs: $LogDir)"
}
$plain = $null
Write-Host '[service] done. Start now with: Start-ScheduledTask -TaskName ''LLMWiki-Web'' (and LLMWiki-Pipeline)'
