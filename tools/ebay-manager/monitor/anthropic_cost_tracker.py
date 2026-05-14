"""Anthropic API 消費額の日次監視と閾値アラート.

api_call_log テーブルから日次/月次コストを集計し、指定閾値を超えたら
Discord 通知。Streamlit ダッシュボードにも組み込んで可視化する。

使用例:
    from monitor.anthropic_cost_tracker import (
        get_today_cost, get_month_cost, notify_if_over_threshold,
    )
    today = get_today_cost()
    if today > 1.0:
        notify_if_over_threshold(daily_budget=1.0)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# 日次/月次の予算 (USD) — 環境変数 or settings.json で上書き可能にしたければ拡張余地
DAILY_BUDGET_USD: float = 2.0
MONTHLY_BUDGET_USD: float = 30.0
ALERT_AT_RATIO: float = 0.80  # 予算の 80% で通知


@dataclass
class CostSnapshot:
    today_usd: float
    month_to_date_usd: float
    yesterday_usd: float
    today_calls: int
    month_calls: int
    top_operation_today: Optional[str]
    top_operation_cost_today: float
    daily_budget: float
    monthly_budget: float
    daily_ratio: float
    monthly_ratio: float

    @property
    def daily_over(self) -> bool:
        return self.today_usd >= self.daily_budget * ALERT_AT_RATIO

    @property
    def monthly_over(self) -> bool:
        return self.month_to_date_usd >= self.monthly_budget * ALERT_AT_RATIO


def _sum_cost(since_datetime: datetime) -> tuple[float, int]:
    with get_conn() as c:
        r = c.execute(
            """SELECT COALESCE(SUM(cost_usd), 0), COUNT(*)
               FROM api_call_log
               WHERE provider = 'anthropic' AND called_at >= ?""",
            (since_datetime.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        if r is None:
            return 0.0, 0
        cost = float(r[0] or 0.0)
        calls = int(r[1] or 0)
        return cost, calls


def get_today_cost() -> float:
    start = datetime.combine(date.today(), datetime.min.time())
    cost, _ = _sum_cost(start)
    return cost


def get_month_cost() -> float:
    today = date.today()
    start = datetime.combine(today.replace(day=1), datetime.min.time())
    cost, _ = _sum_cost(start)
    return cost


def get_yesterday_cost() -> float:
    today = date.today()
    y_start = datetime.combine(today - timedelta(days=1), datetime.min.time())
    y_end = datetime.combine(today, datetime.min.time())
    with get_conn() as c:
        r = c.execute(
            """SELECT COALESCE(SUM(cost_usd), 0)
               FROM api_call_log
               WHERE provider = 'anthropic'
                 AND called_at >= ? AND called_at < ?""",
            (y_start.strftime("%Y-%m-%d %H:%M:%S"),
             y_end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        return float(r[0] or 0.0) if r else 0.0


def get_top_operation_today() -> tuple[Optional[str], float]:
    start = datetime.combine(date.today(), datetime.min.time())
    with get_conn() as c:
        rows = c.execute(
            """SELECT operation, COALESCE(SUM(cost_usd), 0) AS c
               FROM api_call_log
               WHERE provider = 'anthropic' AND called_at >= ?
               GROUP BY operation
               ORDER BY c DESC LIMIT 1""",
            (start.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        if not rows:
            return None, 0.0
        op, c = rows[0][0], float(rows[0][1] or 0.0)
        return op, c


def get_snapshot(daily_budget: Optional[float] = None,
                 monthly_budget: Optional[float] = None) -> CostSnapshot:
    """日次/月次の消費状況をまとめて返す."""
    daily = daily_budget or DAILY_BUDGET_USD
    monthly = monthly_budget or MONTHLY_BUDGET_USD

    today_cost = get_today_cost()
    month_cost = get_month_cost()
    y_cost = get_yesterday_cost()
    top_op, top_op_cost = get_top_operation_today()

    # call counts
    with get_conn() as c:
        today_start = datetime.combine(date.today(), datetime.min.time())
        month_start = datetime.combine(date.today().replace(day=1), datetime.min.time())
        r = c.execute(
            """SELECT
                 (SELECT COUNT(*) FROM api_call_log
                    WHERE provider='anthropic' AND called_at >= ?) AS today_calls,
                 (SELECT COUNT(*) FROM api_call_log
                    WHERE provider='anthropic' AND called_at >= ?) AS month_calls""",
            (today_start.strftime("%Y-%m-%d %H:%M:%S"),
             month_start.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        today_calls = int(r[0] or 0)
        month_calls = int(r[1] or 0)

    return CostSnapshot(
        today_usd=today_cost,
        month_to_date_usd=month_cost,
        yesterday_usd=y_cost,
        today_calls=today_calls,
        month_calls=month_calls,
        top_operation_today=top_op,
        top_operation_cost_today=top_op_cost,
        daily_budget=daily,
        monthly_budget=monthly,
        daily_ratio=today_cost / daily if daily > 0 else 0.0,
        monthly_ratio=month_cost / monthly if monthly > 0 else 0.0,
    )


def format_alert_message(snap: CostSnapshot) -> str:
    """Discord 通知用メッセージを生成."""
    flags = []
    if snap.daily_over:
        flags.append(f"DAILY {snap.daily_ratio*100:.0f}% ({snap.today_usd:.2f}/{snap.daily_budget:.2f})")
    if snap.monthly_over:
        flags.append(f"MONTHLY {snap.monthly_ratio*100:.0f}% ({snap.month_to_date_usd:.2f}/{snap.monthly_budget:.2f})")
    hdr = "【Claude API 消費アラート】 " + " / ".join(flags) if flags else "【Claude API 日次レポート】"
    lines = [
        hdr,
        f"今日: ${snap.today_usd:.4f} ({snap.today_calls} calls)",
        f"月初来: ${snap.month_to_date_usd:.4f} ({snap.month_calls} calls)",
        f"昨日: ${snap.yesterday_usd:.4f}",
    ]
    if snap.top_operation_today:
        lines.append(f"最大消費機能 (今日): {snap.top_operation_today} ${snap.top_operation_cost_today:.4f}")
    return "\n".join(lines)


def _get_discord_webhook() -> Optional[str]:
    """config.yaml or settings から Discord webhook URL を取得."""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return (cfg.get("discord") or {}).get("webhook_url")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"config read failed: {e}")
    return None


def notify_if_over_threshold(
    daily_budget: Optional[float] = None,
    monthly_budget: Optional[float] = None,
    force_send: bool = False,
) -> bool:
    """閾値超過時に Discord 通知. force_send=True なら閾値未満でも送る (日次レポート用).

    Returns:
        送信した場合 True, 送信しなかった場合 False.
    """
    snap = get_snapshot(daily_budget, monthly_budget)
    should_send = force_send or snap.daily_over or snap.monthly_over
    if not should_send:
        return False
    webhook = _get_discord_webhook()
    if not webhook:
        logger.warning("Discord webhook not configured, skipping cost alert")
        return False
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook)
        return bool(notifier.send_message(format_alert_message(snap)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"discord notify failed: {e}")
        return False


if __name__ == "__main__":
    snap = get_snapshot()
    print(format_alert_message(snap))
    print()
    print(f"daily_over: {snap.daily_over}")
    print(f"monthly_over: {snap.monthly_over}")
