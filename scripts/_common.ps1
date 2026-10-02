# Shared helpers for scripts\*.ps1 (dot-sourced; PowerShell 5.1 compatible; ASCII only on purpose).
# Nothing here changes the machine except: process-level environment variables and creating the log folder.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Import-DotEnv {
    # Reads KEY=VALUE lines. SAFE: no evaluation, no variable expansion, no code execution.
    # Only names matching ^WIKI_[A-Z0-9_]+$ are accepted (so a .env cannot set PATH, PYTHONPATH, ...).
    # Empty values are skipped (the variable stays unset). Values are NEVER printed.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $count = 0
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if ($t.Length -eq 0 -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($k -cnotmatch '^WIKI_[A-Z0-9_]+$') { continue }
        if ($v.Length -ge 2) {
            $first = $v[0]; $last = $v[$v.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) { $v = $v.Substring(1, $v.Length - 2) }
        }
        if ($v.Length -eq 0) { continue }
        [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
        $count++
    }
    return $count
}

function Get-RepoPython {
    # Prefer the repo venv; fall back to python on PATH.
    $venv = Join-Path $script:RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { return $cmd.Source }
    throw 'python not found. Run scripts\setup-venv.ps1 first (or put Python 3.11+ on PATH).'
}

function Initialize-PythonPath {
    # Set PYTHONPATH=<repo>\src only when the llmwiki package is not importable (i.e. not pip-installed).
    param([string]$Python)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Python -c 'import llmwiki' 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    if (-not $ok) {
        $src = Join-Path $script:RepoRoot 'src'
        if ($env:PYTHONPATH) { $env:PYTHONPATH = $src + ';' + $env:PYTHONPATH } else { $env:PYTHONPATH = $src }
    }
    $global:LASTEXITCODE = 0
}

function Initialize-RunEnvironment {
    # Common prologue for run-*.ps1: cd to repo root, load .env, choose python, fix PYTHONPATH.
    param([string]$EnvFile)
    Set-Location -LiteralPath $script:RepoRoot
    if (-not $EnvFile) { $EnvFile = Join-Path $script:RepoRoot '.env' }
    $loaded = Import-DotEnv -Path $EnvFile
    Write-Host ("[wiki] env file: {0} ({1} WIKI_* variables loaded; values not shown)" -f $EnvFile, $loaded)
    $py = Get-RepoPython
    Initialize-PythonPath -Python $py
    return $py
}

function Remove-OldLogs {
    param([string]$LogDir, [int]$KeepDays)
    if (-not (Test-Path -LiteralPath $LogDir)) { return }
    $cut = (Get-Date).AddDays(-$KeepDays)
    Get-ChildItem -LiteralPath $LogDir -Filter '*.log' -File |
        Where-Object { $_.LastWriteTime -lt $cut } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

function Invoke-LoggedPython {
    # Runs python with stdout/stderr appended to <LogDir>\<Name>-yyyyMMdd.out.log / .err.log (daily rotation,
    # old files deleted after KeepDays). Returns the exit code.
    param([string]$Python, [string[]]$PyArgs, [string]$Name, [string]$LogDir, [int]$KeepDays = 14)
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Remove-OldLogs -LogDir $LogDir -KeepDays $KeepDays
    $stamp = Get-Date -Format 'yyyyMMdd'
    $out = Join-Path $LogDir ("{0}-{1}.out.log" -f $Name, $stamp)
    $err = Join-Path $LogDir ("{0}-{1}.err.log" -f $Name, $stamp)
    $quoted = ($PyArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $p = Start-Process -FilePath $Python -ArgumentList $quoted -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    return $p.ExitCode
}

function Invoke-Supervised {
    # Restart loop for scheduled-task use: restarts after DelaySeconds whenever python exits (any code).
    param([string]$Python, [string[]]$PyArgs, [string]$Name, [string]$LogDir, [int]$KeepDays = 14, [int]$DelaySeconds = 10)
    while ($true) {
        $code = Invoke-LoggedPython -Python $Python -PyArgs $PyArgs -Name $Name -LogDir $LogDir -KeepDays $KeepDays
        $line = "{0} [supervisor] {1} exited with code {2}; restarting in {3}s" -f (Get-Date -Format s), $Name, $code, $DelaySeconds
        Add-Content -LiteralPath (Join-Path $LogDir ("{0}-supervisor.log" -f $Name)) -Value $line
        Start-Sleep -Seconds $DelaySeconds
    }
}
