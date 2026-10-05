<#
.SYNOPSIS  Start the personal (single-user) LLM wiki: python -m llmwiki.personal. Loads .env.personal (WIKI_* only), never prints secrets.
.EXAMPLE   .\scripts\run-personal.ps1                 # starts the app and opens the browser with the single-use launch URL
.EXAMPLE   .\scripts\run-personal.ps1 -Check          # connectivity / folders / context check only (exit code 1 on FAIL)
.EXAMPLE   .\scripts\run-personal.ps1 -NoBrowser -Port 8765
#>
[CmdletBinding()]
param(
    [string]$EnvFile = '',
    [switch]$Check,
    [switch]$NoBrowser,
    [int]$Port = 0
)
. (Join-Path $PSScriptRoot '_common.ps1')
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env.personal' }
$py = Initialize-RunEnvironment -EnvFile $EnvFile
$pyArgs = @('-m', 'llmwiki.personal')
if ($Check) { $pyArgs += '--check' }
if ($NoBrowser) { $pyArgs += '--no-browser' }
if ($Port -gt 0) { $pyArgs += @('--port', "$Port") }
# Run in the foreground (no log redirection): the launch URL is printed once, to this console only. It works for ONE
# exchange (first GET /); if the browser did not open, restart the app to get a new URL.
& $py @pyArgs
exit $LASTEXITCODE
