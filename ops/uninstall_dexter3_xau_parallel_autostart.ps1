$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$vbs = Join-Path $startup "Dexter3-XAU-Parallel-Autostart.vbs"
if (Test-Path $vbs) { Remove-Item $vbs -Force; Write-Host "Removed $vbs" } else { Write-Host "Not found: $vbs" }

$lock = Join-Path $Root "data\runtime\dexter3_xau_ctrader_open_watcher.lock"
if (Test-Path $lock) {
  $raw = Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1
  $pidVal = 0
  if ([int]::TryParse(([string]$raw).Trim(), [ref]$pidVal)) {
    if (Get-Process -Id $pidVal -ErrorAction SilentlyContinue) {
      try { Stop-Process -Id $pidVal -Force -ErrorAction Stop; Write-Host "Stopped watcher pid=$pidVal" } catch { Write-Host "Could not stop pid=$pidVal : $_" }
    }
  }
  Remove-Item $lock -Force -ErrorAction SilentlyContinue
  Write-Host "Removed watcher lock"
}

foreach ($n in @("Dexter3-XAU-Parallel-Autostart","Dexter3-XAU-Parallel-Health")) {
  try { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue; Write-Host "Unregistered task $n" } catch {}
}
Write-Host "Done."
