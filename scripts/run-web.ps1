<#
.SYNOPSIS  Start the wiki web server (python -m llmwiki.web). Loads .env (WIKI_* only), never prints secrets.
.EXAMPLE   .\scripts\run-web.ps1 -Server waitress
.EXAMPLE   .\scripts\run-web.ps1 -Supervise -LogDir D:\wiki-logs     # restart loop + rotating log files (service use)
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'waitress', 'wsgiref')][string]$Server = '',
    [string]$EnvFile = '',
    [switch]$Supervise,
    [string]$LogDir = '',
    [int]$KeepDays = 14
)
. (Join-Path $PSScriptRoot '_common.ps1')
$py = Initialize-RunEnvironment -EnvFile $EnvFile
$pyArgs = @('-m', 'llmwiki.web')
if ($Server) { $pyArgs += @('--server', $Server) }
if (-not $LogDir) { $LogDir = Join-Path $script:RepoRoot 'logs' }
if ($Supervise) { Invoke-Supervised -Python $py -PyArgs $pyArgs -Name 'web' -LogDir $LogDir -KeepDays $KeepDays; exit 0 }
& $py @pyArgs
exit $LASTEXITCODE
