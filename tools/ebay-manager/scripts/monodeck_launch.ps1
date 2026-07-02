# monodeck_launch.ps1 (W307, 2026-07-02)
# Desktop-shortcut entry point for MonoDeck.
# Idempotent: if port 8501 is already LISTENING just open the browser;
# otherwise fresh-start via the proven streamlit_start.ps1, then open browser.
# ASCII-only (JP Windows + PS 5.1 mis-decodes BOM-less non-ASCII .ps1).
$ErrorActionPreference = 'Stop'
$root = 'C:\Users\gucch\projects\claude\tools\ebay-manager'
$url  = 'http://localhost:8501'

$lis = (netstat -ano | Select-String ':8501\s' | Select-String 'LISTENING')
if ($lis) {
    Write-Output "MonoDeck already running -> open browser"
} else {
    Write-Output "MonoDeck not running -> fresh start (cold start may take ~50s)"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\streamlit_start.ps1')
}
Start-Process $url
