# Dexter3 Grok v1.0 live loop - XAUUSD only (label dexter3:grok-v1.0:scalper)
# Lock: data/runtime/dexter3_grok_loop.lock
# Peer lane: ops/dexter3_xau_v16_loop.ps1 (parallel, separate lock)
param(
    [switch]$Foreground
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Lock = Join-Path $Root "data\runtime\dexter3_grok_loop.lock"
$StdoutLog = Join-Path $Root "data\runtime\dexter3_grok_stdout.log"
$StderrLog = Join-Path $Root "data\runtime\dexter3_grok_stderr.log"
$PythonExe = if (Test-Path "C:\Python312\python.exe") { "C:\Python312\python.exe" } else { "python" }

function Test-LoopAlive {
    if (-not (Test-Path $Lock)) { return $false }
    $raw = (Get-Content $Lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return $false }
    $pidVal = 0
    if (-not [int]::TryParse(([string]$raw).Trim(), [ref]$pidVal)) { return $false }
    return [bool](Get-Process -Id $pidVal -ErrorAction SilentlyContinue)
}

if (Test-LoopAlive) {
    $oldPid = (Get-Content $Lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    Write-Host "[dexter3-grok] already running pid=$oldPid - exit"
    exit 0
}

if (Test-Path $Lock) {
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:DEXTER3_LIVE = "1"
$env:DEXTER3_HUNT = "1"
$env:DEXTER3_FAST_TICK_SEC = "8"
$env:DEXTER3_MAX_VOLUME_UNITS = "10"
$env:DEXTER3_MAX_ENTRIES_PER_DAY = "60"
$env:DEXTER3_DAILY_LOSS_BASKETS = "3"
$env:DEXTER3_OM_LADDER_CSV = "0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25"
$env:DEXTER3_ANTICHASE_ENABLED = "1"
$env:DEXTER3_PULLBACK_ENABLED = "1"
$env:DEXTER3_SMART_EXIT_ENABLED = "0"
$env:DEXTER3_CAPITAL_USD = "1000"
$env:DEXTER3_DAILY_TARGET_USD = "30"
$env:DEXTER3_DAILY_LOSS_USD = "15"
$env:DEXTER3_BASE_RISK_FRAC = "0.004"
$env:DEXTER3_MAX_RISK_FRAC = "0.010"
$env:DEXTER3_MODE = "grok"

Write-Host "[dexter3-grok] MCP health check (auto-restart if down)..."
& $PythonExe "$Root\scripts\ctrader_mcp_watchdog.py" --quiet --restart
if ($LASTEXITCODE -ne 0) {
    Write-Host "[dexter3-grok] WARNING: cTrader MCP still unhealthy after auto-restart"
}

Write-Host "[dexter3-grok] starting XAUUSD live at $(Get-Date -Format o)"
if ($Foreground) {
    & $PythonExe -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live --grok
    exit $LASTEXITCODE
}

$runtimeDir = Join-Path $Root "data\runtime"
if (-not (Test-Path $runtimeDir)) {
    New-Item -Path $runtimeDir -ItemType Directory -Force | Out-Null
}

$p = Start-Process -FilePath $PythonExe `
    -ArgumentList @("-X", "utf8", "-m", "dexter3.shadow_runner", "--symbols", "XAUUSD", "--loop", "--poll-sec", "20", "--live", "--grok") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru
Write-Host "[dexter3-grok] started detached wrapper_pid=$($p.Id) (stdout=$StdoutLog)"
exit 0
