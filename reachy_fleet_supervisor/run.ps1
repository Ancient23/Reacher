# run.ps1 — launch the Reachy Fleet Supervisor (OpenAI Realtime voice + Claude Code delegation)
#
# Usage (in your own terminal, from the app directory):
#   .\run.ps1
#
# Prereqs: robot powered on + USB connected, and OPENAI_API_KEY set with Realtime access:
#   setx OPENAI_API_KEY "sk-..."

$ErrorActionPreference = "Stop"
$AppDir = $PSScriptRoot
$Venv   = Join-Path $AppDir ".venv\Scripts"
$Py     = Join-Path $Venv "python.exe"
$Daemon = Join-Path $Venv "reachy-mini-daemon.exe"
$Src    = Join-Path $AppDir "src"

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = $Src
if (-not $env:OPENAI_API_KEY) {
  $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
}
if (-not $env:OPENAI_API_KEY) {
  Write-Warning 'OPENAI_API_KEY is not set — the Realtime voice will fail. Run: setx OPENAI_API_KEY "sk-..."'
}

# 1) Stop any stale daemon (frees COM3 + port 8000)
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Get-Process reachy-mini-daemon -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# 2) Start the daemon and wait until its HTTP API answers
Write-Host "Starting Reachy Mini daemon..."
$daemonProc = Start-Process -FilePath $Daemon -PassThru -WindowStyle Minimized
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  try {
    if ((Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/daemon/status" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
      $ready = $true; break
    }
  } catch {}
  Start-Sleep -Milliseconds 750
}
if (-not $ready) {
  Write-Error "Daemon did not become ready. Check the robot is powered on (motor power) and USB connected."
  if ($daemonProc -and -not $daemonProc.HasExited) { Stop-Process -Id $daemonProc.Id -Force -ErrorAction SilentlyContinue }
  exit 1
}
Start-Sleep -Seconds 2
Write-Host "Daemon ready. Launching Reachy — talk to it! (Ctrl+C to stop.)"

# 3) Run the app in the foreground (stays attached to this terminal).
#    The daemon is stopped automatically when you exit.
try {
  & $Py -m reachy_fleet_supervisor.main @args
} finally {
  if ($daemonProc -and -not $daemonProc.HasExited) { Stop-Process -Id $daemonProc.Id -Force -ErrorAction SilentlyContinue }
}
