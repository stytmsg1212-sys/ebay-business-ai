# streamlit_start.ps1
# Fresh-start MonoDeck Streamlit the SAME proven way the scheduler is launched
# (Start-Process python -ArgumentList ... ; works around Git-Bash exec/PATH
# quirks that made `streamlit run` exit 127). Captures stdout/stderr to log
# files so an app.py import error (W133/W134 runtime risk) is visible.
# ASCII-only (JP Windows + PS 5.1 mis-decodes BOM-less non-ASCII .ps1).
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File <this file>
$ErrorActionPreference = 'Stop'
$root   = 'C:\Users\gucch\projects\claude\tools\ebay-manager'
$outLog = Join-Path $root 'logs\streamlit_start.out'
$errLog = Join-Path $root 'logs\streamlit_start.err'

# Fresh log files each start
Set-Content -Path $outLog -Value '' -Encoding UTF8
Set-Content -Path $errLog -Value '' -Encoding UTF8

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Launch via run_monodeck.py: it sets platform._wmi=None BEFORE importing
# streamlit, working around the Py3.13 WMI win32_ver() hang on this machine.
$pyArgs = @('-u','run_monodeck.py')
$p = Start-Process -FilePath python -ArgumentList $pyArgs `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Write-Output "STREAMLIT_PID=$($p.Id)"

# Poll port 8501 LISTENING up to ~50s (streamlit cold start on this slow box)
$bound = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Seconds 1
    $alive = $null
    try { $alive = Get-Process -Id $p.Id -ErrorAction Stop } catch {}
    if (-not $alive) { Write-Output "PROC_EXITED after $($i + 1)s (startup failed)"; break }
    $lis = (netstat -ano | Select-String ':8501\s' | Select-String 'LISTENING')
    if ($lis) { Write-Output "PORT_8501_LISTENING after $($i + 1)s"; $bound = $true; break }
}
if (-not $bound) { Write-Output "NOT_BOUND within 50s (still importing or hung)" }

Write-Output "=== streamlit_start.out (tail 25) ==="
if (Test-Path $outLog) { Get-Content $outLog -Tail 25 -Encoding UTF8 }
Write-Output "=== streamlit_start.err (tail 30) ==="
if (Test-Path $errLog) { Get-Content $errLog -Tail 30 -Encoding UTF8 }
