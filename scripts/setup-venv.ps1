<#
.SYNOPSIS  Create .venv and install the project: pip install -e .[dev,prod]  (dev = pytest, prod = waitress).
.NOTES     NEEDS A PIP INDEX. On a company network without internet, pass the internal mirror:
             .\scripts\setup-venv.ps1 -IndexUrl https://pypi.corp.example/simple -TrustedHost pypi.corp.example
           (or pre-set PIP_INDEX_URL / pip.ini). If no index is reachable, nothing can be installed and this fails.
           The core has no runtime dependencies. Optional: -ExtraPackages pypdf  (text-layer PDF extraction; it is
           NOT listed in pyproject.toml, so without it PDF parsing raises "pypdf is not installed").
           This script only creates .venv inside the repo and installs packages into it.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Python = 'python',
    [string]$IndexUrl = '',
    [string]$TrustedHost = '',
    [string[]]$ExtraPackages = @()
)
. (Join-Path $PSScriptRoot '_common.ps1')
Set-Location -LiteralPath $script:RepoRoot
$venvPy = Join-Path $script:RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    if ($PSCmdlet.ShouldProcess('.venv', "create virtual environment with '$Python'")) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed (is '$Python' a Python 3.11+ interpreter?)" }
    }
} else {
    Write-Host '[setup] .venv already exists; reusing it'
}
$pipArgs = @('-m', 'pip', 'install')
if ($IndexUrl) { $pipArgs += @('--index-url', $IndexUrl) }
if ($TrustedHost) { $pipArgs += @('--trusted-host', $TrustedHost) }
if ($PSCmdlet.ShouldProcess('.venv', 'pip install -e .[dev,prod]')) {
    & $venvPy @pipArgs '-e' '.[dev,prod]'
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed: check the index URL / internal mirror and proxy settings' }
    if ($ExtraPackages.Count -gt 0) {
        & $venvPy @pipArgs @ExtraPackages
        if ($LASTEXITCODE -ne 0) { throw 'pip install of extra packages failed' }
    }
    & $venvPy -c "import sys, llmwiki; print('python', sys.version.split()[0], '- llmwiki importable')"
}
Write-Host '[setup] next: copy config\env.example to .env, fill it in, then .\scripts\run-doctor.ps1'
