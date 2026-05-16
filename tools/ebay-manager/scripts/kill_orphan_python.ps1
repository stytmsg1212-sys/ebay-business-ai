# kill_orphan_python.ps1
# Kill every python/pythonw EXCEPT the scheduler (PID arg, default 1948).
# Used to clear hung streamlit launch attempts. ASCII-only.
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File <file> [keepPid]
param([int]$KeepPid = 1948)
$ErrorActionPreference = 'SilentlyContinue'
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $KeepPid } |
    ForEach-Object {
        Write-Output ("KILL {0} (CPU={1})" -f $_.Id, [math]::Round($_.CPU, 2))
        Stop-Process -Id $_.Id -Force
    }
Start-Sleep -Seconds 2
Write-Output "--- remaining python (KeepPid=$KeepPid should survive) ---"
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Select-Object Id, StartTime | Format-Table -AutoSize | Out-String
$lis = (netstat -ano | Select-String ':8501\s')
if ($lis) { Write-Output "PORT_8501:`n$lis" } else { Write-Output "PORT_8501_FREE" }
