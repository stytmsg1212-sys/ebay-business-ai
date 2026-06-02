@echo off
REM eBay KB remote connector launcher (OAuth / Plan B) -- calls the PowerShell script.
REM Double-click this. One window opens, prints the claude.ai connector URL, then runs.
REM Keep the window open while consulting. ASCII only (cmd reads .bat as CP932).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_kb_connector.ps1"
pause
