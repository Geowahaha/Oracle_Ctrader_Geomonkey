# Keep both XAU Dexter3 lanes alive in parallel:
#   V1.6  -> data/runtime/dexter3_loop.lock
#   Grok  -> data/runtime/dexter3_grok_loop.lock
#
# Does NOT start second instances while lock PIDs are alive.
# Disable without unregistering tasks: set DEXTER3_XAU_PARALLEL_AUTOSTART=0 in .env.local
param(
    [string]$Root = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed",
    [string]$PythonExe = "C:\Python312\python.exe",
    [string]$LogFile = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed\data\runtime\dexter3_xau_parallel_watchdog.log",
    [int]$PostStartWaitSec = 8
)

$ErrorActionPreference = "Continue"

function Write-Log {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts [$Level] $Message"
    $dir = Split-Path -Parent $LogFile
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -Path $dir -ItemType Directory -Force | Out-Null
    }
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
    Write-Host $line
}

function Get-EnvFileValue {
    param([string]$Path, [string]$Key)
    if ((-not $Path) -or (-not $Key) -or (-not (Test-Path $Path))) { return $null }
    try {
        $pattern = '^\s*' + [Regex]::Escape($Key) + '\s*=\s*(.*)\s*$'
        foreach ($line in Get-Content -Path $Path -ErrorAction Stop) {
            $text = [string]$line
            if ($text -match '^\s*#') { continue }
            $m = [Regex]::Match($text, $pattern)
            if ($m.Success) {
                return ([string]$m.Groups[1].Value).Trim().Trim('"').Trim("'")
            }
        }
    } catch {
        return $null
    }
    return $null
}

function Test-EnvToggleFalse {
    param([string]$Value)
    $token = ([string]$Value).Trim().ToLowerInvariant()
    return @("0", "false", "no", "off") -contains $token
}

function Resolve-PythonExe {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path $Preferred)) { return $Preferred }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) { return $cmd.Source }
    throw "python executable not found. Preferred='$Preferred'"
}

function Get-LockPid {
    param([string]$LockPath)
    if ([string]::IsNullOrWhiteSpace($LockPath)) { return 0 }
    if (-not (Test-Path -LiteralPath $LockPath)) { return 0 }
    $raw = (Get-Content -LiteralPath $LockPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return 0 }
    $parsed = 0
    if (-not [int]::TryParse(([string]$raw).Trim(), [ref]$parsed)) { return 0 }
    return $parsed
}

function Test-LockAlive {
    param([string]$LockPath)
    $procId = Get-LockPid -LockPath $LockPath
    if ($procId -le 0) { return $false }
    return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

# --- heartbeat (decision-progress) check ------------------------------------
# PID-alive is not enough: a hung-but-alive lane (MCP outage / cTrader crash)
# went undetected for 3h49m on 2026-07-09 because the checks above only see
# the process, not whether it is still deciding M5 bars. The actual
# staleness logic (healthy/stale/missing + why) lives in
# scripts/dexter3_lane_heartbeat.py (unit-tested there) - this .ps1 just
# invokes it and reads its status field to decide healthy vs. escalate vs.
# "can't tell yet, don't touch it" (missing).
function Test-LaneHeartbeat {
    param([string]$PythonExe, [string]$HelperScript, [string]$StateFile)
    if (-not (Test-Path -LiteralPath $HelperScript)) {
        # Helper itself missing must never block the existing PID-alive path.
        return @{ Status = "healthy"; Detail = "heartbeat_helper_missing" }
    }
    $out = (& $PythonExe $HelperScript --state-file $StateFile 2>&1 | Out-String).Trim()
    $status = "missing"
    try {
        $parsed = $out | ConvertFrom-Json -ErrorAction Stop
        if ($parsed.status) { $status = [string]$parsed.status }
    } catch {
        # Unparsable helper output (crash/traceback) is treated the same as
        # "missing" below - never escalate on a signal we can't trust.
    }
    return @{ Status = $status; Detail = $out }
}

function Confirm-LaneHeartbeat {
    param(
        [string]$Name,
        [string]$LockPath,
        [string]$StateFile,
        [string]$PythonExe,
        [string]$HelperScript,
        [string]$McpWatchdogScript
    )
    $hb = Test-LaneHeartbeat -PythonExe $PythonExe -HelperScript $HelperScript -StateFile $StateFile
    if ($hb.Status -eq "healthy") {
        Write-Log -Level "OK" -Message "$Name heartbeat healthy - $($hb.Detail)"
        return
    }
    if ($hb.Status -ne "stale") {
        # "missing" (no state file yet, lane just launched and hasn't
        # completed its first M5 cycle; or a transient read hiccup): this is
        # the existing "can't confirm health yet" case already covered by
        # the PID-alive check elsewhere. Log it but do NOT escalate - a
        # legitimately-just-started lane must never get killed for not
        # having written its first heartbeat yet.
        Write-Log -Level "INFO" -Message "$Name heartbeat state unavailable (not escalating) - $($hb.Detail)"
        return
    }

    Write-Log -Level "WARN" -Message "$Name heartbeat STALE (pid alive but no decision progress) - $($hb.Detail)"

    # Weekend/market-closed guard: a stale heartbeat is only ACTIONABLE if the
    # MCP is ALSO unreachable. During the weekend XAU close (or a holiday /
    # session gap) no new M5 bars print, so last_seen_at legitimately stops
    # advancing and looks "stale" while the lane is perfectly healthy - cTrader
    # desktop stays up, MCP answers. Restart-storming a healthy-but-idle lane
    # every cycle over a weekend is exactly what caused the window-off-screen
    # incident. So: if MCP is healthy, treat stale as market-closed and do NOT
    # escalate. Only a stale heartbeat WITH an unreachable MCP is a real hang.
    & $PythonExe $McpWatchdogScript --quiet | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Log -Level "INFO" -Message "$Name heartbeat stale but MCP healthy (market likely closed/quiet) - not escalating"
        return
    }

    Write-Log -Level "WARN" -Message "$Name MCP also unreachable - running recovery..."
    & $PythonExe $McpWatchdogScript --quiet --restart | Out-Null
    Write-Log -Level "WARN" -Message "$Name ran MCP recovery after stale heartbeat + MCP down (mcp_watchdog exit=$LASTEXITCODE)"

    $hb2 = Test-LaneHeartbeat -PythonExe $PythonExe -HelperScript $HelperScript -StateFile $StateFile
    if ($hb2.Status -eq "healthy") {
        Write-Log -Level "INFO" -Message "$Name heartbeat recovered after MCP restart - $($hb2.Detail)"
        return
    }

    $procId = Get-LockPid -LockPath $LockPath
    Write-Log -Level "ERROR" -Message "$Name still STALE after MCP recovery - killing hung pid=$procId to force relaunch - $($hb2.Detail)"
    if ($procId -gt 0) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

function Start-LaneIfNeeded {
    param(
        [string]$Name,
        [string]$LockPath,
        [string]$LauncherPath,
        [string]$RepoRoot,
        [int]$WaitSec
    )
    if (Test-LockAlive -LockPath $LockPath) {
        $procId = Get-LockPid -LockPath $LockPath
        Write-Log -Level "OK" -Message "$Name healthy pid=$procId"
        return $true
    }

    if ([string]::IsNullOrWhiteSpace($LauncherPath) -or -not (Test-Path -LiteralPath $LauncherPath)) {
        Write-Log -Level "ERROR" -Message "$Name launcher missing: $LauncherPath"
        return $false
    }

    Write-Log -Level "WARN" -Message "$Name down - starting launcher"
    $psExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    Start-Process -FilePath $psExe -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$LauncherPath) -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null

    $wait = [Math]::Max(3, [int]$WaitSec)
    for ($i = 0; $i -lt $wait; $i++) {
        Start-Sleep -Seconds 1
        if (Test-LockAlive -LockPath $LockPath) {
            $procId = Get-LockPid -LockPath $LockPath
            Write-Log -Level "INFO" -Message "$Name restarted pid=$procId"
            return $true
        }
    }
    Write-Log -Level "ERROR" -Message "$Name start attempted but lock still dead after ${wait}s"
    return $false
}

if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root not found: $Root"
}
Set-Location -LiteralPath $Root

$envLocal = Join-Path $Root ".env.local"
$autostartRaw = Get-EnvFileValue -Path $envLocal -Key "DEXTER3_XAU_PARALLEL_AUTOSTART"
if ($autostartRaw -and (Test-EnvToggleFalse -Value $autostartRaw)) {
    Write-Log -Level "INFO" -Message "Autostart disabled by .env.local DEXTER3_XAU_PARALLEL_AUTOSTART=$autostartRaw"
    exit 0
}

try {
    $py = Resolve-PythonExe -Preferred $PythonExe
} catch {
    Write-Log -Level "ERROR" -Message $_.Exception.Message
    exit 1
}

$lockPathV16 = Join-Path $Root "data\runtime\dexter3_loop.lock"
$lockPathGrok = Join-Path $Root "data\runtime\dexter3_grok_loop.lock"
$launcherPathV16 = Join-Path $PSScriptRoot "dexter3_xau_v16_loop.ps1"
$launcherPathGrok = Join-Path $PSScriptRoot "dexter3_xau_grok_loop.ps1"
$stateFileV16 = Join-Path $Root "data\runtime\dexter3_shadow_state.json"
$stateFileGrok = Join-Path $Root "data\runtime\dexter3_grok_shadow_state.json"
$heartbeatHelper = Join-Path $Root "scripts\dexter3_lane_heartbeat.py"
$mcpWatchdogScript = Join-Path $Root "scripts\ctrader_mcp_watchdog.py"

$aliveLaneV16 = Test-LockAlive -LockPath $lockPathV16
$aliveLaneGrok = Test-LockAlive -LockPath $lockPathGrok

if ((-not $aliveLaneV16) -or (-not $aliveLaneGrok)) {
    Write-Log -Level "INFO" -Message ("MCP preflight lane_alive v16={0} grok={1}" -f $aliveLaneV16, $aliveLaneGrok)
    & $py $mcpWatchdogScript --quiet --restart
    if ($LASTEXITCODE -ne 0) {
        Write-Log -Level "WARN" -Message ("MCP still unhealthy after watchdog restart exit={0}; still trying lane starts" -f $LASTEXITCODE)
    }
} else {
    Write-Log -Level "OK" -Message "both lanes already alive - skip MCP preflight"
}

# Heartbeat check only applies to lanes whose PID is alive - a dead PID is
# already handled below by Start-LaneIfNeeded's existing relaunch path.
# Killing a stale-but-alive PID here makes Test-LockAlive/Start-LaneIfNeeded
# see it as dead and relaunch it through the exact same path it always has.
if ($aliveLaneV16) {
    Confirm-LaneHeartbeat -Name "v16" -LockPath $lockPathV16 -StateFile $stateFileV16 -PythonExe $py -HelperScript $heartbeatHelper -McpWatchdogScript $mcpWatchdogScript
}
if ($aliveLaneGrok) {
    Confirm-LaneHeartbeat -Name "grok" -LockPath $lockPathGrok -StateFile $stateFileGrok -PythonExe $py -HelperScript $heartbeatHelper -McpWatchdogScript $mcpWatchdogScript
}

$okLaneV16 = Start-LaneIfNeeded -Name "v16" -LockPath $lockPathV16 -LauncherPath $launcherPathV16 -RepoRoot $Root -WaitSec $PostStartWaitSec
$okLaneGrok = Start-LaneIfNeeded -Name "grok" -LockPath $lockPathGrok -LauncherPath $launcherPathGrok -RepoRoot $Root -WaitSec $PostStartWaitSec

if ($okLaneV16 -and $okLaneGrok) { exit 0 }
exit 2
