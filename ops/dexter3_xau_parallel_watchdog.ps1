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

$aliveLaneV16 = Test-LockAlive -LockPath $lockPathV16
$aliveLaneGrok = Test-LockAlive -LockPath $lockPathGrok

if ((-not $aliveLaneV16) -or (-not $aliveLaneGrok)) {
    Write-Log -Level "INFO" -Message ("MCP preflight lane_alive v16={0} grok={1}" -f $aliveLaneV16, $aliveLaneGrok)
    & $py (Join-Path $Root "scripts\ctrader_mcp_watchdog.py") --quiet --restart
    if ($LASTEXITCODE -ne 0) {
        Write-Log -Level "WARN" -Message ("MCP still unhealthy after watchdog restart exit={0}; still trying lane starts" -f $LASTEXITCODE)
    }
} else {
    Write-Log -Level "OK" -Message "both lanes already alive - skip MCP preflight"
}

$okLaneV16 = Start-LaneIfNeeded -Name "v16" -LockPath $lockPathV16 -LauncherPath $launcherPathV16 -RepoRoot $Root -WaitSec $PostStartWaitSec
$okLaneGrok = Start-LaneIfNeeded -Name "grok" -LockPath $lockPathGrok -LauncherPath $launcherPathGrok -RepoRoot $Root -WaitSec $PostStartWaitSec

if ($okLaneV16 -and $okLaneGrok) { exit 0 }
exit 2
