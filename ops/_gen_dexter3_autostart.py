# One-shot generator for pure-ASCII PowerShell autostart scripts (UTF-8 BOM).
from pathlib import Path

ROOT = Path(__file__).resolve().parent

WATCHDOG = r"""# Keep both XAU Dexter3 lanes alive in parallel:
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
"""

V16 = r"""# Dexter3 V1.6 live loop - XAUUSD only (label dexter3:fable:m5h-v1)
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

$cmdLine = "`"$PythonExe`" -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live >> `"$StdoutLog`" 2>> `"$StderrLog`""
$p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $cmdLine) -WorkingDirectory $Root -WindowStyle Hidden -PassThru
Write-Host "[dexter3-v16] started detached wrapper_pid=$($p.Id) (stdout=$StdoutLog)"
exit 0
"""


REGISTER = r"""# Install Windows Scheduled Tasks for parallel XAU Dexter3 lanes (V1.6 + Grok v1.0).
#   Dexter3-XAU-Parallel-Autostart  - At logon (2 min delay best-effort)
#   Dexter3-XAU-Parallel-Health     - every 1 minute (revive dead lane only)
#
# Unregister:
#   Unregister-ScheduledTask -TaskName 'Dexter3-XAU-Parallel-Autostart' -Confirm:$false
#   Unregister-ScheduledTask -TaskName 'Dexter3-XAU-Parallel-Health' -Confirm:$false
param(
    [string]$TaskPrefix = "Dexter3-XAU-Parallel",
    [string]$Root = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed",
    [string]$PythonExe = "C:\Python312\python.exe",
    [switch]$RunAsSystem,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$defaultRoot = "D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed"
$defaultPythonExe = "C:\Python312\python.exe"

$watchdogScript = Join-Path $PSScriptRoot "dexter3_xau_parallel_watchdog.ps1"
$taskShimDir = Join-Path $env:LOCALAPPDATA "DexterTaskShims"
$taskShim = Join-Path $taskShimDir "dexter3_xau_parallel_watchdog.vbs"

if (-not (Test-Path $watchdogScript)) {
    throw "Watchdog script not found: $watchdogScript"
}

$psExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$wscriptExe = "$env:WINDIR\System32\wscript.exe"

$psArgs = @(
    "-NonInteractive",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $watchdogScript
)
if ($Root -ne $defaultRoot) {
    $psArgs += @("-Root", $Root)
}
if ($PythonExe -ne $defaultPythonExe) {
    $psArgs += @("-PythonExe", $PythonExe)
}

$psCmd = ((@($psExe) + $psArgs) | ForEach-Object {
    '"' + (($_ -as [string]) -replace '"', '""') + '"'
}) -join " "

if (-not (Test-Path $taskShimDir)) {
    New-Item -Path $taskShimDir -ItemType Directory -Force | Out-Null
}
$vbsBody = @"
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "$($psCmd -replace '"', '""')", 0, False
"@
Set-Content -Path $taskShim -Value $vbsBody -Encoding ASCII

$arg = "//B //nologo `"$taskShim`""
$action = New-ScheduledTaskAction -Execute $wscriptExe -Argument $arg

if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    # Interactive user: cTrader Desktop MCP is per-user session
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$autostartName = "$TaskPrefix-Autostart"
$healthName = "$TaskPrefix-Health"

try { Unregister-ScheduledTask -TaskName $autostartName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
try { Unregister-ScheduledTask -TaskName $healthName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
try { schtasks /Delete /F /TN $autostartName *> $null } catch {}
try { schtasks /Delete /F /TN $healthName *> $null } catch {}

$triggers = @()
if ($RunAsSystem) {
    $triggers += New-ScheduledTaskTrigger -AtStartup
    $triggers += New-ScheduledTaskTrigger -AtLogOn
} else {
    $triggers += New-ScheduledTaskTrigger -AtLogOn
}

$taskAutostart = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings
try {
    Register-ScheduledTask -TaskName $autostartName -InputObject $taskAutostart -Force | Out-Null
} catch {
    $taskCmd = "`"$wscriptExe`" //B //nologo `"$taskShim`""
    if ($RunAsSystem) {
        schtasks /Create /F /TN $autostartName /RU SYSTEM /SC ONSTART /TR $taskCmd *> $null
    } else {
        $ru = "$env:USERNAME"
        schtasks /Create /F /TN $autostartName /RU $ru /SC ONLOGON /RL LIMITED /TR $taskCmd *> $null
    }
}

try {
    $t = Get-ScheduledTask -TaskName $autostartName -ErrorAction Stop
    foreach ($tr in @($t.Triggers)) {
        try { $tr.Delay = "PT2M" } catch {}
    }
    Set-ScheduledTask -InputObject $t | Out-Null
} catch {
    # Delay is best-effort; Health task covers late cTrader start
}

$triggerRepeat = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).Date.AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$taskHealth = New-ScheduledTask -Action $action -Trigger $triggerRepeat -Principal $principal -Settings $settings
try {
    Register-ScheduledTask -TaskName $healthName -InputObject $taskHealth -Force | Out-Null
} catch {
    $taskCmd = "`"$wscriptExe`" //B //nologo `"$taskShim`""
    if ($RunAsSystem) {
        schtasks /Create /F /TN $healthName /RU SYSTEM /SC MINUTE /MO 1 /TR $taskCmd *> $null
    } else {
        $ru = "$env:USERNAME"
        schtasks /Create /F /TN $healthName /RU $ru /SC MINUTE /MO 1 /TR $taskCmd *> $null
    }
}

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $autostartName
    if (Get-ScheduledTask -TaskName $healthName -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $healthName
    } else {
        schtasks /Run /TN $healthName *> $null
    }
}

Write-Host "Installed tasks (XAU V1.6 + Grok parallel):"
Get-ScheduledTask -TaskName $autostartName, $healthName -ErrorAction SilentlyContinue |
    Select-Object TaskName, State |
    Format-Table -AutoSize

Write-Host "Manual run:"
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$watchdogScript`""
Write-Host "Disable without unregister:"
Write-Host "  set DEXTER3_XAU_PARALLEL_AUTOSTART=0 in .env.local"
Write-Host "Unregister:"
Write-Host "  Unregister-ScheduledTask -TaskName '$autostartName' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName '$healthName' -Confirm:`$false"
"""


def write_ps1(name: str, content: str) -> None:
    non = [c for c in content if ord(c) > 127]
    if non:
        raise SystemExit(f"{name} has non-ascii: {non[:10]!r}")
    path = ROOT / name
    path.write_text(content, encoding="utf-8-sig", newline="\r\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def main() -> None:
    write_ps1("dexter3_xau_parallel_watchdog.ps1", WATCHDOG)
    write_ps1("dexter3_xau_v16_loop.ps1", V16)
    write_ps1("register_dexter3_xau_parallel_tasks.ps1", REGISTER)


if __name__ == "__main__":
    main()
