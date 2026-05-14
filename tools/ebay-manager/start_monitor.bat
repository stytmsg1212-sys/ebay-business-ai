@echo off
cd /d "%~dp0"
echo eBay 在庫監視を開始します...
python run_monitor.py
pause
