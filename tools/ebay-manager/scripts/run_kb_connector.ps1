# eBay KB remote connector launcher (OAuth, for claude.ai / Plan B)
# - frees port, starts Cloudflare quick tunnel, auto-detects the public URL,
#   passes it to the MCP server as KB_MCP_BASE_URL (OAuth issuer), then runs the server.
# ASCII only (Windows PowerShell 5.1 reads .ps1 as ANSI; no Japanese here).
$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\gucch\projects\claude'
$port = 8765
Set-Location $repo

Write-Host '=== Freeing port 8765 (kill previous instance if any) ==='
try {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
} catch { }

Write-Host '=== Starting Cloudflare quick tunnel ==='
$log = Join-Path $env:TEMP 'kb_tunnel.log'
Remove-Item $log -ErrorAction SilentlyContinue
$tun = Start-Process -FilePath 'cloudflared' `
  -ArgumentList 'tunnel', '--url', "http://127.0.0.1:$port" `
  -RedirectStandardError $log -RedirectStandardOutput "$log.out" `
  -PassThru -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Seconds 1
  $txt = Get-Content $log, "$log.out" -Raw -ErrorAction SilentlyContinue
  if ($txt -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $url = $matches[0]; break }
}
if (-not $url) {
  Write-Host 'ERROR: could not detect tunnel URL. cloudflared log:'
  Get-Content $log, "$log.out" -ErrorAction SilentlyContinue
  Read-Host 'Press Enter to exit'
  exit 1
}

$env:KB_MCP_BASE_URL = $url
Write-Host ''
Write-Host '============================================================'
Write-Host '  Register THIS URL in claude.ai custom connector:'
Write-Host "      $url/mcp"
Write-Host ''
Write-Host '  Keep this window OPEN while consulting.'
Write-Host '  (URL changes if you restart; then re-paste it in claude.ai.)'
Write-Host '============================================================'
Write-Host ''

try {
  python tools\ebay-manager\scripts\kb_mcp_server.py
} finally {
  if ($tun -and -not $tun.HasExited) { Stop-Process -Id $tun.Id -Force -ErrorAction SilentlyContinue }
  Write-Host 'Server and tunnel stopped.'
}
