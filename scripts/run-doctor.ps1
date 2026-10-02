<#
.SYNOPSIS  Self-diagnosis (python -m llmwiki.doctor). Read-only; prints no secrets or document text.
.EXAMPLE   .\scripts\run-doctor.ps1                      # full check (calls the chat LLM and embeddings)
.EXAMPLE   .\scripts\run-doctor.ps1 -SkipLlm -SkipEmbed  # offline check
.EXAMPLE   .\scripts\run-doctor.ps1 -ProbeContext        # sends up to ~250k chars to the LLM to find its context limit
#>
[CmdletBinding()]
param([switch]$SkipLlm, [switch]$SkipEmbed, [switch]$ProbeContext, [switch]$Json, [string]$EnvFile = '')
. (Join-Path $PSScriptRoot '_common.ps1')
$py = Initialize-RunEnvironment -EnvFile $EnvFile
$pyArgs = @('-m', 'llmwiki.doctor')
if ($SkipLlm) { $pyArgs += '--skip-llm' }
if ($SkipEmbed) { $pyArgs += '--skip-embed' }
if ($ProbeContext) { $pyArgs += '--probe-context' }
if ($Json) { $pyArgs += '--json' }
& $py @pyArgs
exit $LASTEXITCODE
