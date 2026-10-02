<#
.SYNOPSIS  Run the inbox pipeline (python -m llmwiki.pipeline). Files go in <WIKI_DATA_DIR>\inbox\<space>\.
.EXAMPLE   .\scripts\run-pipeline.ps1 -Once          # scan, process, exit (exit code 1 if something failed)
.EXAMPLE   .\scripts\run-pipeline.ps1 -Supervise     # watcher with restart loop + rotating logs (service use)
#>
[CmdletBinding()]
param(
    [switch]$Once,
    [double]$Interval = 5.0,
    [string]$EnvFile = '',
    [switch]$Supervise,
    [string]$LogDir = '',
    [int]$KeepDays = 14
)
. (Join-Path $PSScriptRoot '_common.ps1')
$py = Initialize-RunEnvironment -EnvFile $EnvFile
$pyArgs = @('-m', 'llmwiki.pipeline')
if ($Once) { $pyArgs += '--once' }
$pyArgs += @('--interval', ([string]::Format([System.Globalization.CultureInfo]::InvariantCulture, '{0}', $Interval)))
if (-not $LogDir) { $LogDir = Join-Path $script:RepoRoot 'logs' }
if ($Supervise) { Invoke-Supervised -Python $py -PyArgs $pyArgs -Name 'pipeline' -LogDir $LogDir -KeepDays $KeepDays; exit 0 }
& $py @pyArgs
exit $LASTEXITCODE
