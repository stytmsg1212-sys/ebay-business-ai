"""定時実行ヘルスチェック — 期待されたタスクが実行されていない場合に Discord 即時通知.

2026-04-25 hour ドリフト事故対応で新設.
daily_relist が 5 日間サイレントスキップされていたが、誰も気づかなかった.
これを再発させないため、各 batch 終了後に「期待された slot に成功完了ログがあるか」
を照合し、欠落していれば Discord webhook で即時アラートを送る.

実行タイミング (daily_scheduler.setup_scheduler 参照):
    04:00 / 12:00 / 16:00 / 19:00 / 23:00 — 各 batch 終了後
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _send_discord_alert(webhook_url: str, fresh_missed: list[dict]) -> bool:
    """欠落タスクのうち本日初通知のものを Discord に送る."""
    if not webhook_url or not fresh_missed:
        return False
    try:
        import httpx
    except ImportError as e:
        logger.error(f"httpx import 失敗: {e}")
        return False

    fields = []
    for m in fresh_missed[:20]:  # Discord embed field 25 件制限
        h = m.get("expected_hour")
        slot_str = f"{int(h):02d}:00 batch" if h is not None else "毎batch"
        fields.append({
            "name": f"[警告] {m.get('display_name') or m.get('task_key')}",
            "value": f"key=`{m.get('task_key')}` / slot={slot_str}",
            "inline": False,
        })
    embed = {
        "title": "[緊急] 定時実行 欠落検知",
        "description": (
            f"本日 expected されたタスク {len(fresh_missed)} 件が未完了です. "
            "scheduler.log と MonoDeck「定時実行」タブを確認してください. "
            "(同 task は本日中は再通知しません)"
        ),
        "color": 0xD84C38,  # alert red
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
    }
    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:
        logger.error(f"Discord 通知送信エラー: {e}")
        return False


def run_scheduler_health_check(config: dict) -> dict:
    """本日の expected vs executed を照合し、欠落あれば通知 (本日初通知のみ)."""
    try:
        from monitor.task_execution_log import find_missed_tasks, claim_alert_dedupe
    except ImportError as e:
        logger.error(f"task_execution_log import 失敗: {e}")
        return {"success": False, "message": f"import error: {e}"}

    missed = find_missed_tasks(datetime.now(), config=config)
    if not missed:
        logger.info("定時実行ヘルスチェック: 欠落なし")
        return {
            "success": True,
            "missed_count": 0,
            "message": "all expected tasks completed",
        }

    logger.warning(
        f"定時実行ヘルスチェック: 欠落 {len(missed)} 件: "
        f"{[(m['task_key'], m.get('expected_hour')) for m in missed]}"
    )

    # 本日初通知のものだけ抽出 (H-5 対応 dedupe).
    fresh_missed: list[dict] = []
    suppressed = 0
    for m in missed:
        try:
            fresh = claim_alert_dedupe(
                task_key=m["task_key"],
                expected_hour=int(m["expected_hour"]),
            )
        except Exception as e:  # noqa: BLE001 — DB 失敗で全 alert を止めるのは本末転倒
            logger.warning(f"alert dedupe DB error ({m['task_key']}): {e}")
            fresh = True  # フェールセーフで通知側に倒す
        if fresh:
            fresh_missed.append(m)
        else:
            suppressed += 1

    sent = False
    if fresh_missed:
        webhook_url = (config.get("discord") or {}).get("webhook_url") or ""
        sent = _send_discord_alert(webhook_url, fresh_missed)
    return {
        "success": True,
        "missed_count": len(missed),
        "fresh_count": len(fresh_missed),
        "suppressed_count": suppressed,
        "missed": missed,
        "discord_sent": sent,
        "message": (
            f"missed {len(missed)} tasks "
            f"(fresh={len(fresh_missed)}, suppressed={suppressed}), "
            f"discord_sent={sent}"
        ),
    }
