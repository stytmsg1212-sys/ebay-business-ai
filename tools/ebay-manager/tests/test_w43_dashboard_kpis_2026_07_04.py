"""#43 DASHBOARD 刷新: get_dashboard_kpis() 単体テスト (2026-07-04).

conftest.py の autouse fixture (`_isolate_monitor_db`) により各テストは
専用 tmp DB を使うため、本番 DB 汚染の心配なく絶対値で assert できる
(test_w258_nav_badge_counts.py の実 DB 共有 delta パターンとは異なり、
tmp DB なので厳密な絶対値比較が可能)。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _utc_iso(dt: datetime) -> str:
    """eBay PaidTime 相当の UTC ISO 文字列 (末尾 'Z') を生成。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_get_dashboard_kpis_returns_all_keys():
    from monitor.database import init_db, get_dashboard_kpis
    init_db()
    result = get_dashboard_kpis()
    for key in (
        "today_sales_usd", "today_sales_count",
        "week_sales_usd", "week_sales_count",
        "task_24h_completed", "task_24h_failed", "task_24h_rate",
        "customs_pending", "request_board_awaiting",
        "notification_unread_critical", "action_needed_total",
        # #43 fix (2026-07-04): 情報消失 2 点の最小復元
        "own_stock_out", "own_stock_unset", "price_restock",
    ):
        assert key in result, f"{key} が結果に含まれていない"


def test_get_dashboard_kpis_today_and_week_sales():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    now_utc = datetime.now(timezone.utc)

    with get_conn() as c:
        # 本日 (JST) 売上 1 件
        c.execute(
            """INSERT INTO sales_history
               (ebay_item_id, sku, title, sold_price_usd, sold_at)
               VALUES (?,?,?,?,?)""",
            ("w43_today_item", "ebayyh_w43_1", "W43 Today Item", 25.0,
             _utc_iso(now_utc)),
        )
        # 10 日前 (今週の範囲外) の売上 1 件 → today/week どちらにも入らない
        c.execute(
            """INSERT INTO sales_history
               (ebay_item_id, sku, title, sold_price_usd, sold_at)
               VALUES (?,?,?,?,?)""",
            ("w43_old_item", "ebayyh_w43_2", "W43 Old Item", 99.0,
             _utc_iso(now_utc - timedelta(days=10))),
        )

    result = get_dashboard_kpis()
    assert result["today_sales_count"] == 1
    assert result["today_sales_usd"] == 25.0
    # week は today を含む (>= 今週月曜)
    assert result["week_sales_count"] >= 1
    assert result["week_sales_usd"] >= 25.0
    # 10日前の売上は today にも week にも計上されない
    assert result["today_sales_usd"] != 99.0


def test_get_dashboard_kpis_task_24h_rate():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    now = datetime.now()
    with get_conn() as c:
        c.execute(
            """INSERT INTO task_execution_log
               (task_key, display_name, batch_id, batch_hour, status,
                started_at, finished_at, success, expected_today)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            ("w43_task_a", "W43 Task A", "w43batch", 2, "completed",
             now, now, 1),
        )
        c.execute(
            """INSERT INTO task_execution_log
               (task_key, display_name, batch_id, batch_hour, status,
                started_at, finished_at, success, expected_today)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            ("w43_task_b", "W43 Task B", "w43batch", 2, "failed",
             now, now, 0),
        )
        # 24h より前 (対象外)
        c.execute(
            """INSERT INTO task_execution_log
               (task_key, display_name, batch_id, batch_hour, status,
                started_at, finished_at, success, expected_today)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            ("w43_task_c", "W43 Task C", "w43batch_old", 2, "completed",
             now - timedelta(hours=48), now - timedelta(hours=48), 1),
        )

    result = get_dashboard_kpis()
    assert result["task_24h_completed"] == 1
    assert result["task_24h_failed"] == 1
    assert result["task_24h_rate"] == 50.0


def test_get_dashboard_kpis_customs_pending():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        c.execute(
            """INSERT INTO customs_requests (gmail_id, carrier, status)
               VALUES (?,?,?)""",
            ("w43_gmail_1", "fedex", "detected"),
        )
        c.execute(
            """INSERT INTO customs_requests (gmail_id, carrier, status)
               VALUES (?,?,?)""",
            ("w43_gmail_2", "fedex", "sent"),  # 送信済 = 対象外
        )

    result = get_dashboard_kpis()
    assert result["customs_pending"] == 1


def test_get_dashboard_kpis_request_board_awaiting():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        c.execute(
            "INSERT INTO user_requests (title, status) VALUES (?,?)",
            ("W43 依頼A", "awaiting_check"),
        )
        c.execute(
            "INSERT INTO user_requests (title, status) VALUES (?,?)",
            ("W43 依頼B", "open"),  # 対象外
        )

    result = get_dashboard_kpis()
    assert result["request_board_awaiting"] == 1


def test_get_dashboard_kpis_notification_unread_critical():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        c.execute(
            """INSERT INTO notification_log (category, severity, title)
               VALUES ('system','critical','W43 critical')""",
        )
        c.execute(
            """INSERT INTO notification_log (category, severity, title)
               VALUES ('system','error','W43 error')""",
        )
        c.execute(
            """INSERT INTO notification_log (category, severity, title)
               VALUES ('system','info','W43 info (対象外)')""",
        )

    result = get_dashboard_kpis()
    assert result["notification_unread_critical"] == 2


def test_get_dashboard_kpis_own_stock_out_and_unset():
    """自社在庫 (stock% SKU) の 切れ (inventory_count=0) / 未入力 (NULL) 分離集計。

    #43 fix: 旧 DASHBOARD の「在庫通知」を右カラム 1 行に圧縮復元 (v2 モック復元)。
    ebay* / 他 prefix / is_ended=1 は集計対象外 (sku-rules.md 準拠の prefix filter)。
    """
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        # (1) stock% + inventory_count=0 → own_stock_out
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_out_1", "stock:01", "Own Out 1", 0, 0),
        )
        # (2) stock% + inventory_count=NULL → own_stock_unset
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_unset_1", "stock1", "Own Unset 1", 0, None),
        )
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_unset_2", "stock01", "Own Unset 2", None, None),
        )
        # (3) 対象外: is_ended=1 (終了 listing)
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_ended", "stock:99", "Own Ended", 1, 0),
        )
        # (4) 対象外: 無在庫 SKU (ebay*)
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_ebay", "ebayyh_p123", "Ebay SKU", 0, 0),
        )
        # (5) 対象外: inventory_count>0
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?,?,?,?,?)""",
            ("w43_own_in_stock", "stock:02", "Own In Stock", 0, 5),
        )

    result = get_dashboard_kpis()
    assert result["own_stock_out"] == 1
    assert result["own_stock_unset"] == 2


def test_get_dashboard_kpis_price_restock():
    """monitored_items.price_alert_state='restock' 件数 (v2 モック要素復元)。"""
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, is_active, price_alert_state)
               VALUES (?,?,?,?)""",
            ("w43_restock_1", "https://example.com/w43_restock_1", 1, "restock"),
        )
        c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, is_active, price_alert_state)
               VALUES (?,?,?,?)""",
            ("w43_restock_2", "https://example.com/w43_restock_2", 1, "restock"),
        )
        # 対象外: is_active=0
        c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, is_active, price_alert_state)
               VALUES (?,?,?,?)""",
            ("w43_restock_off", "https://example.com/w43_restock_off", 0, "restock"),
        )
        # 対象外: state=surge
        c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, is_active, price_alert_state)
               VALUES (?,?,?,?)""",
            ("w43_restock_surge", "https://example.com/w43_restock_surge", 1, "surge"),
        )

    result = get_dashboard_kpis()
    assert result["price_restock"] == 2


def test_get_dashboard_kpis_action_needed_total_sums():
    from monitor.database import init_db, get_conn, get_dashboard_kpis
    init_db()

    with get_conn() as c:
        c.execute(
            "INSERT INTO customs_requests (gmail_id, carrier, status) VALUES (?,?,?)",
            ("w43_sum_gmail", "dhl", "drafted"),
        )
        c.execute(
            "INSERT INTO user_requests (title, status) VALUES (?,?)",
            ("W43 依頼 sum", "awaiting_check"),
        )
        c.execute(
            "INSERT INTO notification_log (category, severity, title) "
            "VALUES ('system','critical','W43 sum critical')",
        )

    result = get_dashboard_kpis()
    assert result["action_needed_total"] == (
        result["customs_pending"]
        + result["request_board_awaiting"]
        + result["notification_unread_critical"]
    )
    assert result["action_needed_total"] == 3
