@echo off
REM ============================================================
REM  eBay KB remote connector launcher (for claude.ai / Plan B)
REM  Starts: (1) read-only KB MCP server  (2) Cloudflare quick tunnel
REM
REM  HOW TO USE:
REM   - Double-click this .bat. Two windows open.
REM   - "KB-MCP-Server" window  = the server (keep it open).
REM   - "KB-Tunnel" window       = shows a public URL like
REM         https://xxxx.trycloudflare.com
REM     Append /mcp and register it in claude.ai custom connector:
REM         https://xxxx.trycloudflare.com/mcp
REM   - Keep BOTH windows open while consulting. Closing/sleep = disconnect.
REM
REM  NOTE: quick tunnel URL changes every launch (trial use).
REM        For a stable URL, migrate to a Cloudflare named tunnel.
REM  (ASCII only: cmd reads .bat as CP932, so no Japanese here.)
REM ============================================================
cd /d C:\Users\gucch\projects\claude

echo === Starting KB MCP server (127.0.0.1:8765) ===
start "KB-MCP-Server" cmd /k python tools\ebay-manager\scripts\kb_mcp_server.py

REM wait a few seconds for the server to come up
timeout /t 4 /nobreak >nul

echo === Starting Cloudflare quick tunnel ===
start "KB-Tunnel" cmd /k cloudflared tunnel --url http://127.0.0.1:8765

echo.
echo ------------------------------------------------------------
echo  In the "KB-Tunnel" window, find the URL:
echo      https://xxxx.trycloudflare.com
echo  Append /mcp and register it in claude.ai custom connector:
echo      https://xxxx.trycloudflare.com/mcp
echo  Close both windows when you are done consulting.
echo ------------------------------------------------------------
echo.
pause
