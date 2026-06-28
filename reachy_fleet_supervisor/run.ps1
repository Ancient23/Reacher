# run.ps1 — self-healing launcher for the Reachy Fleet Supervisor
#
# Starts the Reachy Mini daemon + the app, and AUTOMATICALLY RESTARTS BOTH if either
# dies — so a mid-session daemon drop ("Lost connection with the server") recovers on its
# own (the daemon re-wakes the robot to neutral). Press Ctrl+C to quit.
#
# Usage (from the app directory, in your own terminal):
#   .\run.ps1
#
# Prereqs: robot powered on + USB connected, and OPENAI_API_KEY set with Realtime access:
#   setx OPENAI_API_KEY "sk-..."

$ErrorActionPreference = "Continue"
$AppDir = $PSScriptRoot
$Venv   = Join-Path $AppDir ".venv\Scripts"
$Py     = Join-Path $Venv "python.exe"
$Daemon = Join-Path $Venv "reachy-mini-daemon.exe"
$Src    = Join-Path $AppDir "src"
$LogDir = Join-Path $AppDir ".logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = $Src
if (-not $env:OPENAI_API_KEY) { $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User") }
if (-not $env:OPENAI_API_KEY) {
  Write-Warning 'OPENAI_API_KEY is not set — the Realtime voice will fail. Run: setx OPENAI_API_KEY "sk-..."'
}

$script:DaemonProc = $null
$script:AppProc    = $null

function Stop-All {
  foreach ($p in @($script:AppProc, $script:DaemonProc)) {
    if ($p -and -not $p.HasExited) { try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {} }
  }
  Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  Get-Process reachy-mini-daemon -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

$ts = { Get-Date -Format "HH:mm:ss" }
$recent = New-Object System.Collections.Generic.Queue[datetime]

try {
  while ($true) {
    # crash-loop guard: > 4 restarts within 60s -> back off 30s
    $now = Get-Date
    $recent.Enqueue($now)
    while ($recent.Count -gt 0 -and ($now - $recent.Peek()) -gt [TimeSpan]::FromSeconds(60)) { [void]$recent.Dequeue() }
    if ($recent.Count -gt 4) { Write-Warning "$(& $ts) Restarting too often — pausing 30s."; Start-Sleep 30; $recent.Clear() }

    # free port 8000 + any stale daemon, let COM3 release
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
      ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Get-Process reachy-mini-daemon -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like '*reachy_fleet_supervisor.main*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 5

    Write-Host "$(& $ts) Starting daemon..."
    $script:DaemonProc = Start-Process -FilePath $Daemon `
      -ArgumentList @("--log-file", (Join-Path $LogDir "daemon.log")) `
      -RedirectStandardOutput (Join-Path $LogDir "daemon.out.log") `
      -RedirectStandardError  (Join-Path $LogDir "daemon.err.log") `
      -WindowStyle Hidden -PassThru

    # wait until the daemon's HTTP API answers
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
      if ($script:DaemonProc.HasExited) { break }
      try { if ((Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/daemon/status" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $ready = $true; break } } catch {}
      Start-Sleep -Milliseconds 750
    }
    if (-not $ready) {
      Write-Warning "$(& $ts) Daemon did not become ready (robot powered on? motors? USB?). Retrying..."
      Stop-All; Start-Sleep 3; continue
    }
    Start-Sleep -Seconds 2

    Write-Host "$(& $ts) Daemon ready. Launching Reachy — talk to it!  (Ctrl+C to quit)"
    $script:AppProc = Start-Process -FilePath $Py -ArgumentList @("-m", "reachy_fleet_supervisor.main") `
      -WorkingDirectory $Src `
      -RedirectStandardOutput (Join-Path $LogDir "app.out.log") `
      -RedirectStandardError  (Join-Path $LogDir "app.err.log") `
      -WindowStyle Hidden -PassThru

    # Watchdog: if EITHER the daemon or the app dies, restart both.
    while ($true) {
      Start-Sleep -Seconds 2
      if ($script:DaemonProc.HasExited) { Write-Warning "$(& $ts) Daemon exited — restarting daemon + app."; break }
      if ($script:AppProc.HasExited)    { Write-Warning "$(& $ts) App exited — restarting daemon + app.";    break }
    }
    Stop-All
    Start-Sleep -Seconds 2
  }
} finally {
  Write-Host "Shutting down..."
  Stop-All
}
