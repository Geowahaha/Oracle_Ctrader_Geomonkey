# Long-running: when cTrader Desktop opens (or is already up) and MCP is healthy,
# ensure both XAU Dexter3 lanes are running (V1.6 + Grok v1.0).
#
# - Detects cTrader process up/down edges
# - Waits for MCP health after open (does NOT kill cTrader during warm-up)
# - Starts missing lanes only (respects locks)
# - Single-instance via data/runtime/dexter3_xau_ctrader_open_watcher.lock
#
# Disable: DEXTER3_XAU_PARALLEL_AUTOSTART=0 in .env.local
param(
    [string]$Root = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed",
    [string]$PythonExe = "C:\Python312\python.exe",
    [string]$LogFile = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed\data\runtime\dexter3_xau_ctrader_open_watcher.log",
    [int]$PollSec = 10,
    [int]$McpWarmupSec = 120,
    [int]$McpCheckSec = 180,
    [int]$StartCooldownSec = 45,
    [switch]$Once
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

function Test-CTraderRunning {
    $names = @("cTrader", "Ctrader", "cTrader64")
    foreach ($n in $names) {
        $p = Get-Process -Name $n -ErrorAction SilentlyContinue
        if ($p) { return $true }
    }
    $any = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'cTrader' }
    return [bool]$any
}

function Test-McpHealthy {
    param([string]$Py, [string]$RepoRoot)
    $script = Join-Path $RepoRoot "scripts\ctrader_mcp_watchdog.py"
    & $Py $script --quiet
    return ($LASTEXITCODE -eq 0)
}

function Invoke-McpRestart {
    param([string]$Py, [string]$RepoRoot)
    $script = Join-Path $RepoRoot "scripts\ctrader_mcp_watchdog.py"
    & $Py $script --quiet --restart
    return ($LASTEXITCODE -eq 0)
}

function Start-BothLanesIfNeeded {
    param([string]$RepoRoot)
    $watchdog = Join-Path $RepoRoot "ops\dexter3_xau_parallel_watchdog.ps1"
    if (-not (Test-Path -LiteralPath $watchdog)) {
        Write-Log -Level "ERROR" -Message "parallel watchdog missing: $watchdog"
        return $false
    }
    $psExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $p = Start-Process -FilePath $psExe -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $watchdog
    ) -WorkingDirectory $RepoRoot -WindowStyle Hidden -Wait -PassThru
    return ($p.ExitCode -eq 0)
}

function Acquire-WatcherLock {
    param([string]$LockPath)
    $dir = Split-Path -Parent $LockPath
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -Path $dir -ItemType Directory -Force | Out-Null
    }
    if (Test-Path -LiteralPath $LockPath) {
        $old = Get-LockPid -LockPath $LockPath
        if ($old -gt 0 -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
            if ($old -ne $PID) {
                Write-Log -Level "INFO" -Message "watcher already running pid=$old - exit"
                exit 0
            }
        }
        Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    }
    Set-Content -LiteralPath $LockPath -Value "$PID" -Encoding ASCII
}

function Release-WatcherLock {
    param([string]$LockPath)
    try {
        if ((Test-Path -LiteralPath $LockPath) -and ((Get-Content -LiteralPath $LockPath -ErrorAction SilentlyContinue | Select-Object -First 1) -eq "$PID")) {
            Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

# --- main ---
if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root not found: $Root"
}
Set-Location -LiteralPath $Root

$lockPathWatcher = Join-Path $Root "data\runtime\dexter3_xau_ctrader_open_watcher.lock"
$lockPathV16 = Join-Path $Root "data\runtime\dexter3_loop.lock"
$lockPathGrok = Join-Path $Root "data\runtime\dexter3_grok_loop.lock"
$envLocal = Join-Path $Root ".env.local"

Acquire-WatcherLock -LockPath $lockPathWatcher
try {
    $py = Resolve-PythonExe -Preferred $PythonExe
} catch {
    Write-Log -Level "ERROR" -Message $_.Exception.Message
    Release-WatcherLock -LockPath $lockPathWatcher
    exit 1
}

Write-Log -Level "INFO" -Message ("cTrader-open watcher started poll={0}s warmup={1}s pid={2}" -f $PollSec, $McpWarmupSec, $PID)

$ctraderWasUp = $false
$ctraderUpSince = $null
$lastStartAttempt = [datetime]::MinValue
$lastHeartbeat = [datetime]::MinValue
$lastMcpCheck = [datetime]::MinValue

while ($true) {
    $autostartRaw = Get-EnvFileValue -Path $envLocal -Key "DEXTER3_XAU_PARALLEL_AUTOSTART"
    if ($autostartRaw -and (Test-EnvToggleFalse -Value $autostartRaw)) {
        Write-Log -Level "INFO" -Message ("disabled by DEXTER3_XAU_PARALLEL_AUTOSTART={0} - watcher exit" -f $autostartRaw)
        break
    }

    $ctraderUp = Test-CTraderRunning
    $v16Alive = Test-LockAlive -LockPath $lockPathV16
    $grokAlive = Test-LockAlive -LockPath $lockPathGrok
    $bothAlive = ($v16Alive -and $grokAlive)
    $now = Get-Date

    if ($ctraderUp -and -not $ctraderWasUp) {
        $ctraderUpSince = $now
        Write-Log -Level "INFO" -Message ("cTrader OPEN detected (edge up) v16={0} grok={1}" -f $v16Alive, $grokAlive)
    }
    if (-not $ctraderUp -and $ctraderWasUp) {
        Write-Log -Level "WARN" -Message "cTrader CLOSED detected (edge down) - lanes left as-is"
        $ctraderUpSince = $null
    }
    $ctraderWasUp = $ctraderUp

    if ($ctraderUp -and -not $bothAlive) {
        $elapsedWarm = 0
        if ($ctraderUpSince) { $elapsedWarm = [int](($now - $ctraderUpSince).TotalSeconds) }

        $cooldownOk = (($now - $lastStartAttempt).TotalSeconds -ge $StartCooldownSec)
        if ($cooldownOk) {
            $mcpOk = Test-McpHealthy -Py $py -RepoRoot $Root
            if (-not $mcpOk) {
                if ($elapsedWarm -ge $McpWarmupSec) {
                    Write-Log -Level "WARN" -Message ("MCP unhealthy after {0}s warmup - trying mcp --restart" -f $elapsedWarm)
                    $mcpOk = Invoke-McpRestart -Py $py -RepoRoot $Root
                } else {
                    Write-Log -Level "INFO" -Message ("cTrader up but MCP not ready yet ({0}s/{1}s) - wait" -f $elapsedWarm, $McpWarmupSec)
                }
            }

            if ($mcpOk) {
                Write-Log -Level "INFO" -Message ("MCP healthy - starting missing lanes (v16={0} grok={1})" -f $v16Alive, $grokAlive)
                $lastStartAttempt = $now
                $ok = Start-BothLanesIfNeeded -RepoRoot $Root
                if ($ok) {
                    Write-Log -Level "INFO" -Message "lane ensure OK"
                } else {
                    Write-Log -Level "ERROR" -Message "lane ensure failed (see parallel watchdog log)"
                }
            }
        }
    } elseif ($ctraderUp -and $bothAlive) {
        if (($now - $lastMcpCheck).TotalSeconds -ge $McpCheckSec) {
            $lastMcpCheck = $now
            $mcpOk = Test-McpHealthy -Py $py -RepoRoot $Root
            if (-not $mcpOk) {
                $elapsedWarm = 0
                if ($ctraderUpSince) { $elapsedWarm = [int](($now - $ctraderUpSince).TotalSeconds) }
                if ($elapsedWarm -ge $McpWarmupSec) {
                    Write-Log -Level "WARN" -Message ("MCP unhealthy while both lanes alive - trying mcp --restart")
                    $mcpOk = Invoke-McpRestart -Py $py -RepoRoot $Root
                    if ($mcpOk) {
                        Write-Log -Level "INFO" -Message "MCP recovered while both lanes alive"
                    } else {
                        Write-Log -Level "WARN" -Message "MCP recovery failed or skipped by watchdog cooldown"
                    }
                } else {
                    Write-Log -Level "INFO" -Message ("both lanes alive but MCP warmup not complete ({0}s/{1}s) - skip health restart" -f $elapsedWarm, $McpWarmupSec)
                }
            }
        }
        if (($now - $lastHeartbeat).TotalSeconds -ge 300) {
            $p1 = Get-LockPid -LockPath $lockPathV16
            $p2 = Get-LockPid -LockPath $lockPathGrok
            Write-Log -Level "OK" -Message ("cTrader up + both lanes alive v16={0} grok={1}" -f $p1, $p2)
            $lastHeartbeat = $now
        }
    } else {
        if (($now - $lastHeartbeat).TotalSeconds -ge 600) {
            Write-Log -Level "INFO" -Message ("waiting for cTrader open (lanes v16={0} grok={1})" -f $v16Alive, $grokAlive)
            $lastHeartbeat = $now
        }
    }

    if ($Once) { break }
    Start-Sleep -Seconds ([Math]::Max(3, $PollSec))
}

Release-WatcherLock -LockPath $lockPathWatcher
Write-Log -Level "INFO" -Message "cTrader-open watcher stopped"
exit 0
