@echo off
REM Chrome を CDP デバッグポート 9222 で起動 (既存 Chrome と独立)
REM
REM 重要: 既存の Chrome (普段使い) は閉じる必要なし.
REM 専用 user-data-dir を使うので別プロセスとして起動される.

setlocal

set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set PROFILE_DIR=C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager\data\.chrome_cdp_profile

echo ===========================================
echo Chrome CDP debug port 9222 起動
echo ===========================================
echo Chrome path: %CHROME_PATH%
echo Profile dir: %PROFILE_DIR%
echo.

REM Chrome の存在確認
if not exist "%CHROME_PATH%" (
    echo ERROR: Chrome not found at %CHROME_PATH%
    pause
    exit /b 1
)

REM 既にポート 9222 使用中なら警告
netstat -ano | findstr :9222 >nul
if %errorlevel% equ 0 (
    echo WARNING: Port 9222 が既に使用中です.
    echo 既存 CDP Chrome が動いている可能性あり.
    echo そのまま python scripts/terapeak_poc_cdp.py を実行できるかもしれません.
    echo.
    pause
)

echo 起動中... Chrome のウィンドウが開いたら以下を実施:
echo   1. eBay にログイン (普通のログインで OK)
echo   2. Terapeak Research Products に navigate
echo      https://www.ebay.com/sh/research/products?q=Audio-Technica+ATH-CKS330NC^&dayRange=90^&tabName=SOLD^&sellerCountry=Japan
echo   3. filter 適用確認
echo   4. 別ターミナルで python scripts/terapeak_poc_cdp.py 実行
echo.

REM start /B でバックグラウンド起動 + フラグで既存プロセス merging を回避
start "" "%CHROME_PATH%" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" --no-first-run --no-default-browser-check

REM 数秒待ってポート確認
timeout /t 3 /nobreak >nul
echo.
echo ポート 9222 LISTEN 確認:
netstat -ano | findstr :9222

echo.
echo Chrome 起動コマンド送信完了. ウィンドウが開いていない場合はもう一度 bat を実行してください.
pause
endlocal
