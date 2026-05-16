# streamlit_kill_8501.ps1
# Kill the Streamlit process listening on port 8501 so MonoDeck can be
# fresh-started with W134 Step1 + W133 UI + DB v40 code.
# ASCII-only (JP Windows + PS 5.1 mis-decodes BOM-less non-ASCII .ps1).
# Single Stop-Process (no /T tree kill: streamlit 1.x is one process; tree
# enumeration via WMI times out on this slow machine).
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File <this file>
$ErrorActionPreference = 'Stop'

# (1) Find PID listening on 8501 via netstat (fast; avoids Get-NetTCPConnection)
$line = (netstat -ano | Select-String ':8501\s' | Select-String 'LISTENING' | Select-Object -First 1)
if (-not $line) { Write-Output 'NO_LISTENER_8501 (port already free)'; exit 0 }
$stPid = ($line.ToString().Trim() -split '\s+')[-1]
Write-Output "LISTENER_PID=$stPid"

# (2) Sanity: confirm it is python
$proc = $null
try { $proc = Get-Process -Id $stPid -ErrorAction Stop } catch {}
if (-not $proc) { Write-Output "PID $stPid vanished. Treat as free"; exit 0 }
Write-Output "PROC name=$($proc.ProcessName) Id=$($proc.Id)"
if ($proc.ProcessName -notlike 'python*') {
    Write-Output "ABORT: PID $stPid is not python ($($proc.ProcessName))"
    exit 2
}

# (3) Kill + poll until port free (up to 15s)
Stop-Process -Id $stPid -Force
Write-Output "STOPPED $stPid"
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $still = (netstat -ano | Select-String ':8501\s' | Select-String 'LISTENING')
    if (-not $still) { Write-Output "PORT_8501_FREE after $($i + 1)s"; exit 0 }
}
Write-Output "WARN: port 8501 still LISTENING after 15s"
exit 3
