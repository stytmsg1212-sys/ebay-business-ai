@echo off
REM ============================================================
REM  eBay KB リモートコネクタ起動 (claude.ai 用 / B案)
REM  KB読み取り専用MCPサーバ + Cloudflare quick tunnel を起動する。
REM  使い方: このbatをダブルクリック → 2つの黒い窓が開く。
REM   1) KB-MCP-Server 窓 = サーバ (閉じない)
REM   2) KB-Tunnel 窓 = トンネル。表示される
REM        https://xxxx.trycloudflare.com
REM      の末尾に /mcp を付けた URL を claude.ai のカスタムコネクタに登録。
REM      例: https://xxxx.trycloudflare.com/mcp
REM  ※ この quick tunnel の URL は起動ごとに変わる(お試し用)。
REM    安定URLが欲しい場合は Cloudflare 名前付きトンネル(要アカウント)に移行。
REM  ※ 相談中は2窓とも開いたまま。PCスリープ/窓を閉じると claude.ai から繋がらない。
REM ============================================================
cd /d C:\Users\gucch\projects\claude

echo === KB MCP サーバを起動します (127.0.0.1:8765) ===
start "KB-MCP-Server" cmd /k python tools\ebay-manager\scripts\kb_mcp_server.py

REM サーバ起動を少し待つ
timeout /t 4 /nobreak >nul

echo === Cloudflare トンネルを起動します ===
start "KB-Tunnel" cmd /k cloudflared tunnel --url http://127.0.0.1:8765

echo.
echo ------------------------------------------------------------
echo  KB-Tunnel 窓に表示される https://xxxx.trycloudflare.com を確認し、
echo  末尾に /mcp を付けて claude.ai のカスタムコネクタに登録してください。
echo  (例: https://xxxx.trycloudflare.com/mcp)
echo  相談が終わったら両方の窓を閉じれば停止します。
echo ------------------------------------------------------------
echo.
pause
