' EbayManager-SchedulerWatchdog invisible launcher (board #53, 2026-07-05)
' Purpose: Scheduled Task calls this VBS instead of powershell.exe directly.
' Reason: Interactive logon PowerShell flashes a console window for an instant
' even with -WindowStyle Hidden. wscript.exe has no console of its own, so
' running powershell.exe through WScript.Shell.Run with window style 0 suppresses it.
' Pattern follows ebay-scheduler-autostart.vbs (Startup folder).
' To revert: schtasks /Change /TN "EbayManager-SchedulerWatchdog" /TR "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\gucch\projects\claude\tools\ebay-manager\scripts\scheduler_watchdog.ps1"
' ASCII-only on purpose: JP Windows misdecodes BOM-less non-ASCII scripts (CP932).
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\gucch\projects\claude\tools\ebay-manager\scripts\scheduler_watchdog.ps1""", 0, False
