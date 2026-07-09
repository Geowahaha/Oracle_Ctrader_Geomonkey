# Install Windows Scheduled Tasks for parallel XAU Dexter3 lanes (V1.6 + Grok v1.0).
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
