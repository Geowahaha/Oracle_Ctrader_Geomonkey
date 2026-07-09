# Install Startup entry for cTrader-open watcher (both XAU lanes).
# No admin required.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$watcher = Join-Path $PSScriptRoot "dexter3_xau_ctrader_open_watcher.ps1"
if (-not (Test-Path $watcher)) { throw "missing $watcher" }

$startup = [Environment]::GetFolderPath("Startup")
$vbs = Join-Path $startup "Dexter3-XAU-Parallel-Autostart.vbs"
$psExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$cmd = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watcher`""

$vbsBody = @"
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
' Start watcher soon after logon; it waits for cTrader open + MCP itself
WScript.Sleep 15000
sh.Run "$($cmd -replace '"', '""')", 0, False
"@
Set-Content -Path $vbs -Value $vbsBody -Encoding ASCII
Write-Host "Installed Startup watcher: $vbs"
Write-Host "Watcher script: $watcher"

$lock = Join-Path $Root "data\runtime\dexter3_xau_ctrader_open_watcher.lock"
$alive = $false
if (Test-Path $lock) {
  $raw = Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1
  $pidVal = 0
  if ([int]::TryParse(([string]$raw).Trim(), [ref]$pidVal)) {
    if (Get-Process -Id $pidVal -ErrorAction SilentlyContinue) {
      $alive = $true
      Write-Host "Watcher already running pid=$pidVal"
    }
  }
}
if (-not $alive) {
  Start-Process -FilePath $psExe -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-WindowStyle","Hidden","-File",$watcher) -WorkingDirectory $Root -WindowStyle Hidden
  Start-Sleep -Seconds 2
  if (Test-Path $lock) {
    Write-Host "Watcher started lock=$(Get-Content $lock -Raw)"
  } else {
    Write-Host "Watcher start issued (lock not yet written - check log)"
  }
}
Write-Host "Log: $Root\data\runtime\dexter3_xau_ctrader_open_watcher.log"
