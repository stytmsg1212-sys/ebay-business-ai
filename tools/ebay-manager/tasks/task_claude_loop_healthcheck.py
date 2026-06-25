"""W131 P5 claude_loop_healthcheck: claude auto-restart loop の watcher-of-watcher.

出典: 2026-05-16 W131 -NoExit regression fix セッションで判明したリスク:
  - claude-loop 自体が 1h 20 回 crash loop guard で suicide
  - logon しないまま PC 起動で Startup folder shortcut 未発火
  - SessionStart hook は user が新セッション開始する時しか発火しない

scheduler は logon 後に起動して 24/7 動くので、claude-loop の watcher として最適.
30 分ごとに heartbeat 鮮度を確認し、stale なら start-claude-loop.ps1 を spawn.
Discord 通知 (R-11) で user に視認確認を促す.

Phase 1 (SessionStart hook): 即時 detect、user 介入のタイミングで recovery
Phase 2 (本 task): 30 分定期 detect、user 不在時も自動 recovery
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path("C:/Users/gucch/.claude/scripts/claude-loop.heartbeat")
KILLSWITCH_FILE = Path("C:/Users/gucch/.claude/scripts/claude-loop.STOP")
LOOP_SCRIPT = "C:/Users/gucch/.claude/scripts/start-claude-loop.ps1"
STALE_THRESHOLD_SEC = 60


# Codex Round 1 fix LOW-6 (2026-05-16): PATH 解決依存を避け絶対 path を使う.
# %SystemRoot% は通常 'C:\Windows', os.environ で fallback.
_SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL_EXE = rf"{_SYSTEM_ROOT}\System32\WindowsPowerShell\v1.0\powershell.exe"

# Codex Round 1 fix HIGH-2 (2026-05-16): spawn 後 verify の timeout / interval
SPAWN_VERIFY_TIMEOUT_SEC = 20
SPAWN_VERIFY_POLL_SEC = 2


def _spawn_loop_with_verify(prev_hb_mtime: float) -> tuple[bool, str]:
    """start-claude-loop.ps1 を spawn し、heartbeat 更新 + プロセス生存で verify.

    Codex Round 1 fix HIGH-2 (2026-05-16):
    Popen 成功 = RECOVERED は false positive (powershell.exe 起動だけで script 即 exit
    でも True になる). spawn 前 hb mtime と比較し、20 秒以内に hb が更新され、かつ
    Popen が生存中であれば真の RECOVERED.

    Args:
        prev_hb_mtime: spawn 前の heartbeat mtime (float). 不在なら -1.

    Returns:
        (success, pid_or_reason_str)
    """
    if sys.platform != "win32":
        return (False, "non-Windows platform")
    try:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        p = subprocess.Popen(
            [
                POWERSHELL_EXE,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                LOOP_SCRIPT,
            ],
            creationflags=CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError, PermissionError) as e:
        return (False, f"{type(e).__name__}: {e}")

    # Codex Round 2 HIGH (2026-05-16): hb mtime 更新だけでは false positive (Get-Command
    # claude 失敗 / Start-Process 失敗でも powershell が phase=starting の hb を書く).
    # `child=<pid>` 付きの hb (= claude 起動成功 + polling iteration 到達) を待つ.
    pid = p.pid
    deadline = time.monotonic() + SPAWN_VERIFY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if p.poll() is not None:
            return (False, f"PID={pid} exited early (rc={p.returncode})")
        if HEARTBEAT_FILE.exists():
            cur_mtime = HEARTBEAT_FILE.stat().st_mtime
            if cur_mtime > prev_hb_mtime:
                hb = HEARTBEAT_FILE.read_text(encoding="utf-8", errors="ignore")
                if "child=" in hb:
                    return (True, f"PID={pid} (hb has child=)")
                # phase=starting 等の child 無し hb は spawn 成功 signal にしない
        time.sleep(SPAWN_VERIFY_POLL_SEC)

    if p.poll() is None:
        return (False, f"PID={pid} alive but no child= hb within {SPAWN_VERIFY_TIMEOUT_SEC}s")
    return (False, f"PID={pid} exited (rc={p.returncode}) and no child= hb")


def _get_webhook_url(config: Optional[dict]) -> str:
    """Discord webhook URL を解決. task_morning_discovery と同じ 3 段優先順.

    1. config['discord']['webhook_url']
    2. config['discord_webhook_url']
    3. fallback: config/schedule_config.json
    """
    if config:
        wh = (
            (config.get("discord") or {}).get("webhook_url")
            or config.get("discord_webhook_url")
            or ""
        )
        if wh:
            return wh
    try:
        import json
        sched_cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
        if sched_cfg_path.exists():
            with sched_cfg_path.open(encoding="utf-8") as f:
                sched_cfg = json.load(f)
            return (sched_cfg.get("discord") or {}).get("webhook_url") or ""
    except (OSError, ValueError) as e:
        logger.warning(f"schedule_config.json load 失敗: {e}")
    return ""


def _notify_discord(config: dict, status: str, detail: str) -> None:
    """Discord に health check 結果を通知 (R-11 視認 verify 前提).

    送信失敗は log のみ、task の success には影響させない (notifier 死で
    healthcheck 自体が止まる事故を回避).
    """
    # board#22: ループ健全性は system ch (未設定なら既定 ch に fallback)
    from notifiers.discord_notifier import resolve_webhook
    webhook = resolve_webhook("system")
    if not webhook:
        logger.warning("Discord webhook 未設定、healthcheck 通知 skip")
        return
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook, bypass_env=True)
        color = 0xE74C3C if status in ("DOWN", "RECOVERY_FAILED") else 0x2ECC71
        embed = {
            "title": f"[claude-loop] {status}",
            "description": detail,
            "color": color,
        }
        # Codex Round 1 fix MEDIUM-5 (2026-05-16): send_message bool 戻り値 check
        sent = notifier.send_message(f"claude-loop {status}", embed=embed)
        if not sent:
            logger.warning(
                f"Discord 通知失敗: send_message returned False "
                f"(status={status}, detail={detail[:80]})"
            )
    except Exception as e:
        logger.warning(f"Discord 通知失敗 (healthcheck 自体は続行): {type(e).__name__}: {e}")


def run(config: Optional[dict] = None) -> dict:
    """claude_loop_healthcheck entry point.

    Returns:
        {"success": bool, "status": "ALIVE"|"RECOVERED"|"DOWN"|"SKIPPED",
         "hb_age_sec": float, "message": str}
    """
    config = config or {}

    if KILLSWITCH_FILE.exists():
        # user 意図的停止 = 通知も recovery もしない
        logger.info("claude_loop_healthcheck: KillSwitch active, skip")
        return {
            "success": True,
            "status": "SKIPPED",
            "hb_age_sec": -1.0,
            "message": "KillSwitch active (user intentional stop)",
        }

    if not HEARTBEAT_FILE.exists():
        ok, info = _spawn_loop_with_verify(prev_hb_mtime=-1.0)
        msg = f"heartbeat 不在 → auto-recovery {'成功' if ok else '失敗'} ({info})"
        logger.warning(f"claude_loop_healthcheck: {msg}")
        _notify_discord(
            config,
            "RECOVERED" if ok else "RECOVERY_FAILED",
            msg,
        )
        return {
            "success": ok,
            "status": "RECOVERED" if ok else "DOWN",
            "hb_age_sec": -1.0,
            "message": msg,
        }

    prev_hb_mtime = HEARTBEAT_FILE.stat().st_mtime
    hb_age_sec = time.time() - prev_hb_mtime
    hb_content = HEARTBEAT_FILE.read_text(encoding="utf-8", errors="ignore").strip()

    if hb_age_sec <= STALE_THRESHOLD_SEC:
        logger.info(
            f"claude_loop_healthcheck: ALIVE (hb age={hb_age_sec:.0f}s, {hb_content[:80]})"
        )
        # ALIVE は Discord 通知しない (毎 30 分 spam 防止)
        return {
            "success": True,
            "status": "ALIVE",
            "hb_age_sec": hb_age_sec,
            "message": f"alive ({hb_content[:80]})",
        }

    # stale → auto-recovery (Codex HIGH-2 fix: spawn 後 heartbeat 更新まで verify)
    ok, info = _spawn_loop_with_verify(prev_hb_mtime=prev_hb_mtime)
    msg = (
        f"heartbeat stale ({hb_age_sec:.0f}s > {STALE_THRESHOLD_SEC}s) → "
        f"auto-recovery {'成功' if ok else '失敗'} ({info})\n"
        f"last hb: {hb_content[:120]}"
    )
    logger.warning(f"claude_loop_healthcheck: {msg}")
    _notify_discord(
        config,
        "RECOVERED" if ok else "RECOVERY_FAILED",
        msg,
    )
    return {
        "success": ok,
        "status": "RECOVERED" if ok else "DOWN",
        "hb_age_sec": hb_age_sec,
        "message": msg,
    }
