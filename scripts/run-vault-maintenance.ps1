<#
.SYNOPSIS  Upkeep of the personal second-brain vault: (1) backup snapshot, (2) vault_lint. Writes a report next to the backups.
           Exit code 1 when the backup failed or vault_lint found errors, so a scheduler/you can notice.
.EXAMPLE   .\scripts\run-vault-maintenance.ps1
.EXAMPLE   .\scripts\run-vault-maintenance.ps1 -BackupDir D:\second-brain-backup -Keep 60
.NOTES     Default backup folder: env WIKI_VAULT_BACKUP_DIR, else <repo parent>\second-brain-backup.
           Report: <BackupDir>\last-maintenance.txt (UTF-8). This script never runs vault_lint --accept: the raw-file
           baseline is only updated by you, on purpose, after you add new raw notes. The backup folder holds the same plain
           text notes as the vault: keep it somewhere only you can read.
#>
[CmdletBinding()]
param(
    [string]$BackupDir = '',
    [int]$Keep = 30
)
. (Join-Path $PSScriptRoot '_common.ps1')
$root = $script:RepoRoot
Set-Location -LiteralPath $root
$py = Get-RepoPython
Initialize-PythonPath $py
if (-not $BackupDir) {
    if ($env:WIKI_VAULT_BACKUP_DIR) { $BackupDir = $env:WIKI_VAULT_BACKUP_DIR }
    else { $BackupDir = Join-Path (Split-Path -Parent $root) 'second-brain-backup' }
}
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # the python tools print UTF-8 (Korean file names)

$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # native stderr must not abort the script
$backupOut = & $py -m llmwiki.vault_backup backup --vault $root --dest $BackupDir --keep $Keep 2>&1 | Out-String
$backupCode = $LASTEXITCODE
$lintOut = & $py -m llmwiki.vault_lint --vault $root 2>&1 | Out-String
$lintCode = $LASTEXITCODE
$ErrorActionPreference = $prev

$ok = ($backupCode -eq 0 -and $lintCode -eq 0)
$text = @(
    ('time:    ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
    ('result:  ' + $(if ($ok) { 'OK' } else { 'ATTENTION NEEDED' })),
    '',
    ('--- backup (exit ' + $backupCode + ') ---'), $backupOut.TrimEnd(),
    '',
    ('--- vault_lint (exit ' + $lintCode + '; 0 = no errors, 1 = errors, 2 = folder missing) ---'), $lintOut.TrimEnd(),
    ''
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $BackupDir 'last-maintenance.txt'), $text, (New-Object System.Text.UTF8Encoding($true)))
Write-Output $text
if ($ok) { exit 0 } else { exit 1 }
