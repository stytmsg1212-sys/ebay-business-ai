# scheduler_restart_w133.ps1
# Manual scheduler restart to deploy W133 F4 (in-stock management) code.
# Per USER_MANUAL.md section 1-2. ASCII-only (this machine is JP Windows +
# PowerShell 5.1: a BOM-less non-ASCII .ps1 is mis-decoded as CP932).
# Avoids slow Get-CimInstance; uses fast Get-Process -Id for the sanity check.
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File <this file>
$ErrorActionPreference = 'Stop'
$root    = 'C:\Users\gucch\projects\claude\tools\ebay-manager'
$pidFile = Join-Path $root 'data\scheduler.pid'
$logFile = Join-Path $root 'logs\scheduler.log'

# (1) Read current PID
$oldPid = (Get-Content $pidFile -Raw).Trim()
Write-Output "OLD_PID=$oldPid"

# (2) Sanity: confirm that PID is python (Get-Process is faster than Get-CimInstance)
$proc = $null
try { $proc = Get-Process -Id $oldPid -ErrorAction Stop } catch {}
if ($proc) {
    Write-Output "OLD_PROC: name=$($proc.ProcessName) Id=$($proc.Id)"
    if ($proc.ProcessName -notlike 'python*') {
        Write-Output "ABORT: PID $oldPid is not python ($($proc.ProcessName)). Stop (avoid wrong kill)"
        exit 2
    }
    Stop-Process -Id $oldPid -Force
    Write-Output "STOPPED $oldPid"
} else {
    Write-Output "OLD_PROC: not running (stale pid). Continue to fresh start"
}

# (3) Wait for full process death + OS lock release (poll up to 15s)
$dead = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $still = $null
    try { $still = Get-Process -Id $oldPid -ErrorAction Stop } catch {}
    if (-not $still) { Write-Output "DEAD after $($i + 1)s"; $dead = $true; break }
}
if (-not $dead -and $proc) {
    Write-Output "WARN: PID $oldPid still alive after 15s. Will start anyway but lock may conflict"
}

# (4) Fresh start (UTF-8 / Hidden / PassThru)
$env:PYTHONUNBUFFERED  = '1'
$env:PYTHONIOENCODING  = 'utf-8'
$new = Start-Process -FilePath python -ArgumentList 'daily_scheduler.py' -WorkingDirectory $root -WindowStyle Hidden -PassThru
Write-Output "STARTED launcher PID=$($new.Id)"

# (5) Verify: scheduler.pid updated + startup log line present
Start-Sleep -Seconds 8
$newPid = (Get-Content $pidFile -Raw).Trim()
Write-Output "NEW_PID(scheduler.pid)=$newPid"
if ($newPid -eq $oldPid) {
    Write-Output "WARN: scheduler.pid still old PID. New process may have failed to acquire lock"
}
Write-Output "=== scheduler.log tail 14 ==="
Get-Content $logFile -Tail 14 -Encoding UTF8
