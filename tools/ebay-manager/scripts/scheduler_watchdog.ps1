# scheduler_watchdog.ps1
# Windows Task Scheduler から 5 分間隔で実行。
# daily_scheduler.py プロセスが落ちていたら再起動 + Discord 通知。
# 出典: 2026-04-30 PC sleep で 24h silent skip 事故 (Cal Rueb red flag #1 単一障害点)。
$ErrorActionPreference = 'Stop'
$BaseDir = 'C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager'
$LogFile = Join-Path $BaseDir 'logs\watchdog.log'
$LockFile = Join-Path $BaseDir '.watchdog_lock'

function Write-WatchdogLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content -Path $LogFile -Encoding UTF8
}

# 並列起動回避 (schtasks default で並列禁止だが念のため)
if (Test-Path $LockFile) {
    $age = (Get-Date) - (Get-Item $LockFile).LastWriteTime
    if ($age.TotalMinutes -lt 4) {
        Write-WatchdogLog "concurrent_run_skip lock_age=$([int]$age.TotalSeconds)s"
        exit 0
    }
}
New-Item -Path $LockFile -ItemType File -Force | Out-Null

try {
    # 2026-05-02 redesign: WMI Get-CimInstance hang 多発のため log mtime ベースに変更.
    # daily_scheduler は order_alert_check を 30 分毎に実行 = scheduler.log が常に
    # 最大 30 分以内に更新される。35 分超え更新無し = scheduler stuck/dead 推定.
    # 旧: 2026-05-02 05:49 〜 09:54 まで 4h watchdog log silent (WMI hang 推定).
    # 注意: 変数名は outer $LogFile (watchdog log) と区別する.
    # PowerShell は case-insensitive で $logFile = $LogFile 同一視 = bug 源.
    $SchedulerLog = Join-Path $BaseDir 'logs\scheduler.log'
    $alive = $false
    if (Test-Path $SchedulerLog) {
        $age = (Get-Date) - (Get-Item $SchedulerLog).LastWriteTime
        if ($age.TotalMinutes -lt 35) {
            Write-WatchdogLog "OK log_age=$([int]$age.TotalSeconds)s"
            $alive = $true
        } else {
            Write-WatchdogLog "ALERT log_stale_minutes=$([int]$age.TotalMinutes)"
        }
    } else {
        Write-WatchdogLog "ALERT log_file_missing path=$SchedulerLog"
    }
    if ($alive) { exit 0 }

    # Scheduler down -> restart
    Write-WatchdogLog "ALERT scheduler_down attempting_restart"
    $env:PYTHONUNBUFFERED = '1'
    $proc = Start-Process -FilePath 'python' -ArgumentList 'daily_scheduler.py' `
        -WorkingDirectory $BaseDir -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 5

    $verify = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if ($verify) {
        $status = 'RESTART_OK'
        Write-WatchdogLog "$status PID=$($proc.Id)"
    } else {
        $status = 'RESTART_FAILED'
        Write-WatchdogLog "$status PID_attempted=$($proc.Id)"
    }

    # Discord 通知 (webhook URL は config から取得、ハードコード禁止)
    $msg = "[Watchdog] Scheduler was DOWN at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -> $status (PID=$($proc.Id))"
    $pyArgs = @"
import sys, json
sys.path.insert(0, r'$BaseDir')
try:
    from notifiers.discord_notifier import DiscordNotifier
    cfg = json.load(open(r'$BaseDir\config\schedule_config.json', encoding='utf-8'))
    url = (cfg.get('discord') or {}).get('webhook_url')
    if url:
        DiscordNotifier(url).send_message('$msg')
        print('discord_sent')
    else:
        print('no_webhook_url_in_config')
except Exception as e:
    print(f'discord_error: {e}')
"@
    $discordOut = (python -c $pyArgs 2>&1 | Out-String).Trim()
    Write-WatchdogLog "discord: $discordOut"

} finally {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
}
