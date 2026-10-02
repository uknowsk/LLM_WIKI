<#
.SYNOPSIS  Run the test suite. Does NOT load .env and clears WIKI_* variables from this process (tests must not
           depend on, or reach, real endpoints). Extra pytest arguments are passed through.
.EXAMPLE   .\scripts\run-tests.ps1
.EXAMPLE   .\scripts\run-tests.ps1 tests\test_web_leak.py tests\test_engine_leak.py
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PytestArgs)
. (Join-Path $PSScriptRoot '_common.ps1')
Set-Location -LiteralPath $script:RepoRoot
Get-ChildItem Env: | Where-Object { $_.Name -like 'WIKI_*' } | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
$py = Get-RepoPython
Initialize-PythonPath -Python $py
if (-not $PytestArgs -or $PytestArgs.Count -eq 0) { $PytestArgs = @('-q') }
& $py -m pytest @PytestArgs
exit $LASTEXITCODE
