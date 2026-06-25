"""予算アラート: 1 日 3 回 (06:00 / 12:00 / 19:00) Anthropic API 予算状況を Discord 通知.

監視対象:
  - 本日累計 (api_call_log の cost_usd 合計)
  - 月間累計 (4/26 時点で $15.47/$35 = Tier 1 上限の 44% 等)
  - 主要消費内訳 (operation 別 top 5)
  - 月末予測 (現ペース×残日数 vs 残予算)

Discord 通知レベル:
  - 緑 (nominal):   月間使用率 < 60% かつ 残日数で持ちそう
  - 黄 (caution):   月間使用率 60-85% または 残日数の 70% 程度で枯渇予測
  - 赤 (alert):     月間使用率 > 85% または 残日数より早く枯渇予測

Method A (Research 脳 subprocess) は Max 内のため本 alert の対象外.
本日の Anthropic console 表示と一致するのは api_call_log 合計のみ.
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor.db"

# Tier 1 デフォルト. settings.json で上書き可能 (Tier 2 化したら $400 等).
DEFAULT_MONTHLY_LIMIT_USD = 35.0


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _today_spend() -> tuple[float, list[dict]]:
    """本日 (00:00-now) の API 消費合計 + operation 別内訳."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        total_row = c.execute(
            "SELECT ROUND(SUM(cost_usd), 4) AS total FROM api_call_log "
            "WHERE date(called_at) = ?",
            (today,),
        ).fetchone()
        total = float(total_row["total"] or 0.0)
        breakdown = c.execute(
            """SELECT model, operation, COUNT(*) AS calls,
                      ROUND(SUM(cost_usd), 4) AS cost
               FROM api_call_log
               WHERE date(called_at) = ?
               GROUP BY model, operation
               ORDER BY cost DESC LIMIT 5""",
            (today,),
        ).fetchall()
    return total, [dict(r) for r in breakdown]


def _month_to_date_spend() -> float:
    """月初から現在までの API 消費合計."""
    month_start = datetime.now().strftime("%Y-%m-01")
    with _conn() as c:
        row = c.execute(
            "SELECT ROUND(SUM(cost_usd), 4) AS total FROM api_call_log "
            "WHERE date(called_at) >= ?",
            (month_start,),
        ).fetchone()
    return float(row["total"] or 0.0)


def _days_until_month_reset() -> int:
    """月末リセット (翌月 1 日) までの日数 (本日含む)."""
    now = datetime.now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    end_of_month = now.replace(day=last_day, hour=23, minute=59, second=59)
    days = (end_of_month - now).days + 1
    return max(days, 1)


def _resolve_monthly_limit(config: Optional[dict]) -> float:
    """settings.json (もしくは config) から月間上限を取得. なければ Tier 1 デフォルト $35."""
    if config and isinstance(config, dict):
        billing = config.get("anthropic_billing") or {}
        limit = billing.get("monthly_limit_usd")
        if limit:
            try:
                return float(limit)
            except (TypeError, ValueError):
                pass
    return DEFAULT_MONTHLY_LIMIT_USD


def _build_alert(config: Optional[dict] = None, scheduled_hour: int = 0) -> dict:
    """alert 本体を構築. dict を返す."""
    today_total, breakdown = _today_spend()
    month_total = _month_to_date_spend()
    monthly_limit = _resolve_monthly_limit(config)
    pct = (month_total / monthly_limit * 100) if monthly_limit > 0 else 0
    remaining = monthly_limit - month_total
    days_left = _days_until_month_reset()
    daily_avg = month_total / max(datetime.now().day, 1)
    forecast_total = daily_avg * (datetime.now().day + days_left - 1) if days_left else month_total
    will_exceed = forecast_total > monthly_limit

    # 重大度判定
    if pct >= 85 or will_exceed:
        severity = "alert"
        color = 0xD84C38  # red
        emoji = "[CRITICAL]"
    elif pct >= 60:
        severity = "caution"
        color = 0xC89B2A  # amber
        emoji = "[WARN]"
    else:
        severity = "nominal"
        color = 0x6B7A5C  # sage
        emoji = "[OK]"

    return {
        "scheduled_hour": scheduled_hour,
        "today_total_usd": today_total,
        "month_total_usd": month_total,
        "monthly_limit_usd": monthly_limit,
        "monthly_pct": pct,
        "remaining_usd": remaining,
        "days_left": days_left,
        "daily_avg_usd": daily_avg,
        "forecast_total_usd": forecast_total,
        "will_exceed": will_exceed,
        "severity": severity,
        "color": color,
        "emoji": emoji,
        "breakdown": breakdown,
    }


def _send_discord(webhook_url: str, alert: dict) -> bool:
    """Discord webhook で alert を送信."""
    if not webhook_url:
        return False
    try:
        import httpx
    except ImportError as e:
        logger.error(f"httpx import 失敗: {e}")
        return False

    fields = [
        {
            "name": "本日の API 消費",
            "value": f"${alert['today_total_usd']:.4f}",
            "inline": True,
        },
        {
            "name": "月間累計",
            "value": (
                f"${alert['month_total_usd']:.2f} / ${alert['monthly_limit_usd']:.0f} "
                f"({alert['monthly_pct']:.0f}%)"
            ),
            "inline": True,
        },
        {
            "name": "残予算 / リセットまで",
            "value": f"${alert['remaining_usd']:.2f} / {alert['days_left']} 日",
            "inline": True,
        },
        {
            "name": "月末予測",
            "value": (
                f"${alert['forecast_total_usd']:.2f} "
                + ("(超過予測)" if alert["will_exceed"] else "(範囲内)")
            ),
            "inline": False,
        },
    ]

    if alert["breakdown"]:
        bd_lines = []
        for b in alert["breakdown"][:5]:
            bd_lines.append(
                f"• {b['model']} / {b['operation']}: "
                f"${b['cost']:.3f} ({b['calls']} calls)"
            )
        fields.append({
            "name": "本日 主要消費 Top 5",
            "value": "\n".join(bd_lines)[:1000],
            "inline": False,
        })

    desc_extra = ""
    if alert["will_exceed"]:
        desc_extra = (
            f"\n\n月末予測 ${alert['forecast_total_usd']:.2f} > 上限 "
            f"${alert['monthly_limit_usd']:.0f} 超過リスク. "
            "Tier 2 アップグレード or 一時節約 (Opus → Haiku) を推奨."
        )
    elif alert["severity"] == "caution":
        desc_extra = (
            "\n\n月間使用率が 60%超. 残日数とのバランスを監視中."
        )

    embed = {
        "title": f"{alert['emoji']} 予算アラート ({alert['scheduled_hour']:02d}:00)",
        "description": (
            f"Anthropic API クレジット使用状況 (DB 試算).\n"
            f"※ DB 試算は cache 効果未考慮で Console 実値の 1.5-2 倍程度過大計上の傾向あり.\n"
            f"確実な値は **https://console.anthropic.com/** の「コスト」を確認.{desc_extra}"
        ),
        "color": alert["color"],
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
        "footer": {
            "text": "task_budget_alert.py / settings.json の anthropic_billing.monthly_limit_usd で上限調整可能"
        },
    }

    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:
        logger.error(f"Discord 送信エラー: {e}")
        return False


def run_budget_alert(config: Optional[dict] = None) -> dict:
    """daily_scheduler から 06:00/12:00/19:00 の cron で呼ばれる."""
    scheduled_hour = datetime.now().hour
    alert = _build_alert(config, scheduled_hour=scheduled_hour)
    # board#22: 予算アラートは system ch (未設定なら既定 ch に fallback)
    from notifiers.discord_notifier import resolve_webhook
    webhook = resolve_webhook("system")
    sent = _send_discord(webhook, alert)
    logger.info(
        f"budget_alert: severity={alert['severity']} "
        f"month=${alert['month_total_usd']:.2f}/${alert['monthly_limit_usd']:.0f} "
        f"({alert['monthly_pct']:.0f}%) discord_sent={sent}"
    )
    return {
        "success": True,
        "severity": alert["severity"],
        "today_usd": alert["today_total_usd"],
        "month_usd": alert["month_total_usd"],
        "monthly_pct": alert["monthly_pct"],
        "discord_sent": sent,
        "message": (
            f"予算 alert 送信: 月 ${alert['month_total_usd']:.2f}/"
            f"${alert['monthly_limit_usd']:.0f} ({alert['severity']})"
        ),
    }


if __name__ == "__main__":
    import json
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # config 読込
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    result = run_budget_alert(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
