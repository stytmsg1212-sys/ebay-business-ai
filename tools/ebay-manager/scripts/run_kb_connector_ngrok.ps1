# eBay KB remote connector launcher (ngrok STATIC domain edition, for claude.ai)
# - Stable URL that does NOT change across restarts (free ngrok static domain).
#   => claude.ai connector URL stays valid; no re-paste / re-auth churn.
# - Prereq (one-time, user):
#     1. ngrok account (free) -> dashboard.ngrok.com
#     2. ngrok config add-authtoken <TOKEN>          (this script checks it)
#     3. claim free static domain (Domains tab) and write it (host only, no https://)
#        into: tools\ebay-manager\data\kb_ngrok_domain.txt
# ASCII only (Windows PowerShell 5.1 reads .ps1 as ANSI; no Japanese here).
$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\gucch\projects\claude'
$port = 8765
Set-Location $repo

# --- resolve ngrok.exe (PATH may not be refreshed in current shell) ---
$ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok) {
  $cand = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Ngrok.Ngrok_*\ngrok.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($cand) { $ngrok = $cand.FullName }
}
if (-not $ngrok) { Write-Host 'ERROR: ngrok.exe not found. Install: winget install --id Ngrok.Ngrok -e'; Read-Host 'Enter to exit'; exit 1 }

# --- read static domain ---
$domainFile = Join-Path $repo 'tools\ebay-manager\data\kb_ngrok_domain.txt'
if (-not (Test-Path $domainFile)) { Write-Host "ERROR: $domainFile not found. Write your ngrok static domain (host only) there."; Read-Host 'Enter to exit'; exit 1 }
$domain = (Get-Content $domainFile -Raw).Trim()
if (-not $domain -or $domain -match '://') { Write-Host "ERROR: domain file must contain host only (e.g. foo-bar.ngrok-free.app), got: '$domain'"; Read-Host 'Enter to exit'; exit 1 }

# --- verify authtoken configured ---
$probe = & $ngrok config check 2>&1 | Out-String
if ($probe -match 'authtoken' -and $probe -match 'empty|not') {
  Write-Host 'ERROR: ngrok authtoken not configured. Run: ngrok config add-authtoken <TOKEN>'; Read-Host 'Enter to exit'; exit 1
}

Write-Host '=== Freeing port 8765 + stopping stray ngrok (idempotent restart) ==='
try {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
} catch { }
# kill stray ngrok agents (free plan allows 1 session; double-tunnel would conflict)
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Free plan: the assigned static dev domain IS the default endpoint, and the agent
# cannot pass --domain/--url for it. So start without it and read the assigned URL
# back from the local ngrok API, then assert it matches the expected domain (catches
# any future reassignment so the connector URL never silently drifts).
Write-Host "=== Starting ngrok tunnel (expecting stable domain: $domain) ==="
$log = Join-Path $env:TEMP 'kb_ngrok.log'
Remove-Item $log -ErrorAction SilentlyContinue
$tun = Start-Process -FilePath $ngrok `
  -ArgumentList 'http', "$port", '--log', $log `
  -PassThru -WindowStyle Hidden

$public = $null
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  try {
    $api = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2 -ErrorAction Stop
    $u = ($api.tunnels | Where-Object { $_.public_url -match '^https' } | Select-Object -First 1).public_url
    if ($u) { $public = $u; break }
  } catch { }
}
if (-not $public) {
  Write-Host 'ERROR: ngrok tunnel did not come online. log:'
  Get-Content $log -ErrorAction SilentlyContinue | Select-Object -Last 20
  if ($tun -and -not $tun.HasExited) { Stop-Process -Id $tun.Id -Force -ErrorAction SilentlyContinue }
  Read-Host 'Enter to exit'; exit 1
}
if ($public -ne "https://$domain") {
  Write-Host "WARNING: assigned URL ($public) != expected (https://$domain)."
  Write-Host "  Update tools\ebay-manager\data\kb_ngrok_domain.txt and re-register in claude.ai."
}
$url = $public
$env:KB_MCP_BASE_URL = $url
Write-Host ''
Write-Host '============================================================'
Write-Host '  STABLE connector URL (register ONCE in claude.ai):'
Write-Host "      $url/mcp"
Write-Host ''
Write-Host '  This URL is fixed - no re-paste needed after restarts.'
Write-Host '  Keep this process running while consulting (or use the'
Write-Host '  scheduled task for always-on).'
Write-Host '============================================================'
Write-Host ''

try {
  python tools\ebay-manager\scripts\kb_mcp_server.py
} finally {
  if ($tun -and -not $tun.HasExited) { Stop-Process -Id $tun.Id -Force -ErrorAction SilentlyContinue }
  Write-Host 'Server and ngrok tunnel stopped.'
}
