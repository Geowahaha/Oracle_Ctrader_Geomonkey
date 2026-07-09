# Dexter3 V1.6 live loop - XAUUSD only (label dexter3:fable:m5h-v1)
# Lock: data/runtime/dexter3_loop.lock
# Peer lane: ops/dexter3_xau_grok_loop.ps1 (parallel, separate lock)
param(
    [switch]$Foreground
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Lock = Join-Path $Root "data\runtime\dexter3_loop.lock"
$StdoutLog = Join-Path $Root "data\runtime\dexter3_v16_stdout.log"
$StderrLog = Join-Path $Root "data\runtime\dexter3_v16_stderr.log"
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
    Write-Host "[dexter3-v16] already running pid=$oldPid - exit"
    exit 0
}

if (Test-Path $Lock) {
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:DEXTER3_LIVE = "1"
$env:DEXTER3_HUNT = "1"
$env:DEXTER3_FABLE_VERSION = "v1.8-size-the-edge"
$env:DEXTER3_FAST_TICK_SEC = "8"
$env:DEXTER3_MAX_VOLUME_UNITS = "10"
$env:DEXTER3_MAX_ENTRIES_PER_DAY = "60"
$env:DEXTER3_DAILY_LOSS_BASKETS = "3"
$env:DEXTER3_OM_LADDER_CSV = "0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25"
$env:DEXTER3_ANTICHASE_ENABLED = "1"
$env:DEXTER3_PULLBACK_ENABLED = "1"
$env:DEXTER3_SMART_EXIT_ENABLED = "0"
$env:DEXTER3_CAPITAL_USD = "1000"
$env:DEXTER3_DAILY_TARGET_USD = "100"
$env:DEXTER3_DAILY_LOSS_USD = "50"
$env:DEXTER3_V16_PROFIT_CONTROLS_ENABLED = "1"
$env:DEXTER3_V16_COOLDOWN_ENABLED = "0"
$env:DEXTER3_V16_GREEN_THRESHOLD_USD = "20"
$env:DEXTER3_V16_HOUSE_THRESHOLD_USD = "30"
$env:DEXTER3_V16_HOUSE_FLOOR_USD = "20"
$env:DEXTER3_V16_WEAK_MULT = "0.25"
$env:DEXTER3_V16_WINNER_MULT = "2.0"
$env:DEXTER3_V16_HOUSE_MULT = "3.0"
$env:DEXTER3_V16_MAX_EDGE_MULT = "3.0"
# V1.8 size-the-edge (size-policy race 2026-07-09: P0 $42.9/day -> P5 $68.6/day
# at base $12; base 1.75% of $1000 = $17.5 lifts P5 to ~$100/day expected.
# Downside stays bounded by DAILY_LOSS_USD=50 + max_risk_frac 2.5% cap).
$env:DEXTER3_V18_WINNER_BOOST_ENABLED = "1"
$env:DEXTER3_V18_CHASE_RESCUE_ENABLED = "1"
$env:DEXTER3_V18_B_TIER_ENABLED = "1"
$env:DEXTER3_BASE_RISK_FRAC = "0.0175"
Remove-Item Env:DEXTER3_MODE -ErrorAction SilentlyContinue

Write-Host "[dexter3-v16] MCP health check (auto-restart if down)..."
& $PythonExe "$Root\scripts\ctrader_mcp_watchdog.py" --quiet --restart
if ($LASTEXITCODE -ne 0) {
    Write-Host "[dexter3-v16] WARNING: cTrader MCP still unhealthy after auto-restart"
}

Write-Host "[dexter3-v16] starting XAUUSD live at $(Get-Date -Format o)"
if ($Foreground) {
    & $PythonExe -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live
    exit $LASTEXITCODE
}

$runtimeDir = Join-Path $Root "data\runtime"
if (-not (Test-Path $runtimeDir)) {
    New-Item -Path $runtimeDir -ItemType Directory -Force | Out-Null
}

$p = Start-Process -FilePath $PythonExe `
    -ArgumentList @("-X", "utf8", "-m", "dexter3.shadow_runner", "--symbols", "XAUUSD", "--loop", "--poll-sec", "20", "--live") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru
Write-Host "[dexter3-v16] started detached wrapper_pid=$($p.Id) (stdout=$StdoutLog)"
exit 0
