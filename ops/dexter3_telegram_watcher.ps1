# Dexter3 Telegram watcher - zero-AI-token critical-event push to owner
# Lock: data/runtime/dexter3_telegram_watcher.lock
param(
    [switch]$Foreground
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Lock = Join-Path $Root "data\runtime\dexter3_telegram_watcher.lock"
$StdoutLog = Join-Path $Root "data\runtime\dexter3_telegram_watcher_stdout.log"
$StderrLog = Join-Path $Root "data\runtime\dexter3_telegram_watcher_stderr.log"
$PythonExe = if (Test-Path "C:\Python312\python.exe") { "C:\Python312\python.exe" } else { "python" }

function Test-WatcherAlive {
    if (-not (Test-Path $Lock)) { return $false }
    $raw = (Get-Content $Lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return $false }
    $pidVal = 0
    if (-not [int]::TryParse(([string]$raw).Trim(), [ref]$pidVal)) { return $false }
    return [bool](Get-Process -Id $pidVal -ErrorAction SilentlyContinue)
}

if (Test-WatcherAlive) {
    Write-Host "[tg-watcher] already running - exit"
    exit 0
}
if (Test-Path $Lock) { Remove-Item $Lock -Force -ErrorAction SilentlyContinue }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if ($Foreground) {
    & $PythonExe -X utf8 "$Root\ops\dexter3_telegram_watcher.py"
    exit $LASTEXITCODE
}

$p = Start-Process -FilePath $PythonExe `
    -ArgumentList @("-X", "utf8", "$Root\ops\dexter3_telegram_watcher.py") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru
Write-Host "[tg-watcher] started detached pid=$($p.Id)"
exit 0
