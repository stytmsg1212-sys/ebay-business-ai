"""W133 (2026-05-16) tests: task_order_alert への inventory_sync 配線.

検証対象:
  - 減算 → sync 呼出 → 在庫0/抑止/失敗で Discord embed 1 回
  - 二重 polling (重複 order) で減算 skip 痕跡 (dec=None で sync 呼ばれない)
  - 既存 HV/DDP の cadence 不変更 (K2: 追加のみ)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    import monitor.inventory_sync as inv_mod
    monkeypatch.setattr(inv_mod, "_oos_control_cache", None)
    yield db_path


def _insert_stock_listing(ebay_item_id, sku, inventory_count, title="T"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count, quantity_ebay)
               VALUES (?, ?, ?, 0, ?, 0)""",
            (ebay_item_id, sku, title, inventory_count),
        )


def _order(order_id, ebay_item_id, sku, qty=1):
    return {
        "order_id": order_id,
        "ebay_item_id": ebay_item_id,
        "sku": sku,
        "qty": qty,
        "item_price_usd": 50.0,
        "shipping_usd": 0.0,
        "buyer_country": "US",
        "title": "Test Widget",
    }


def test_decrement_calls_sync_and_zero_triggers_discord(tmp_db):
    _insert_stock_listing("Z1", "stock:01", 1, title="Zero Widget")
    from tasks import task_order_alert
    orders = [_order("ORD1", "Z1", "stock:01", qty=1)]

    sent_embeds = []

    def _fake_send(webhook, embed):
        sent_embeds.append(embed)
        return True

    with patch.object(task_order_alert, "_get_credentials",
                      return_value=("a", "d", "c", "t")), \
         patch("monitor.ebay_client.get_orders",
               return_value={"success": True, "orders": orders}), \
         patch("monitor.inventory_sync.sync_listing_quantity",
               return_value={"success": True, "skipped_zero_unsafe": False,
                             "message": "ok"}) as m_sync, \
         patch.object(task_order_alert, "_send_discord",
                      side_effect=_fake_send):
        res = task_order_alert.run_order_alert_check({}, num_days=1)

    assert res["success"] is True
    assert res["inventory_decrements"] == 1
    m_sync.assert_called_once_with("Z1")
    # 在庫0 化 → Discord embed 1 回 (在庫サマリ)
    inv_embeds = [
        e for e in sent_embeds if "在庫" in e.get("title", "")
    ]
    assert len(inv_embeds) == 1
    # 商品呼称: title + 末尾4桁、SKU を表示しない (CLAUDE.md)
    field_text = str(inv_embeds[0]["fields"])
    assert "stock:01" not in field_text, "Discord に SKU が表示されている"
    assert "Zero Widget" in field_text


def test_duplicate_order_no_sync_call(tmp_db):
    """同 order 二重 polling で 2 回目は減算 skip → sync 呼ばれない."""
    _insert_stock_listing("Z2", "stock:01", 10, title="Dup Widget")
    from tasks import task_order_alert
    orders = [_order("ORD2", "Z2", "stock:01", qty=2)]

    with patch.object(task_order_alert, "_get_credentials",
                      return_value=("a", "d", "c", "t")), \
         patch("monitor.ebay_client.get_orders",
               return_value={"success": True, "orders": orders}), \
         patch("monitor.inventory_sync.sync_listing_quantity",
               return_value={"success": True, "skipped_zero_unsafe": False,
                             "message": "ok"}) as m_sync, \
         patch.object(task_order_alert, "_send_discord", return_value=True):
        task_order_alert.run_order_alert_check({}, num_days=1)
        assert m_sync.call_count == 1
        # 2 回目 (重複 polling)
        task_order_alert.run_order_alert_check({}, num_days=1)
        assert m_sync.call_count == 1, "重複 polling で再 sync された (二重減算リスク)"

    from monitor.database import get_conn
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='Z2'"
        ).fetchone()["inventory_count"]
    assert inv == 8, "二重減算 (10-2-2) が起きた"


def test_sync_suppressed_zero_unsafe_triggers_discord(tmp_db):
    """在庫0 で sync 抑止 → Discord embed に抑止理由が出る."""
    _insert_stock_listing("Z3", "stock:01", 1, title="Unsafe Widget")
    from tasks import task_order_alert
    orders = [_order("ORD3", "Z3", "stock:01", qty=1)]
    sent = []

    with patch.object(task_order_alert, "_get_credentials",
                      return_value=("a", "d", "c", "t")), \
         patch("monitor.ebay_client.get_orders",
               return_value={"success": True, "orders": orders}), \
         patch("monitor.inventory_sync.sync_listing_quantity",
               return_value={"success": False, "skipped_zero_unsafe": True,
                             "message": "OOS未確認のため数量0 revise を抑止"}), \
         patch.object(task_order_alert, "_send_discord",
                      side_effect=lambda w, e: sent.append(e) or True):
        task_order_alert.run_order_alert_check({}, num_days=1)

    inv_embeds = [e for e in sent if "在庫" in e.get("title", "")]
    assert len(inv_embeds) == 1
    assert "抑止" in str(inv_embeds[0]["fields"])


def test_no_inventory_zero_no_discord(tmp_db):
    """在庫が0にならず sync 成功なら在庫 Discord embed は出ない."""
    _insert_stock_listing("Z4", "stock:01", 10, title="Plenty Widget")
    from tasks import task_order_alert
    orders = [_order("ORD4", "Z4", "stock:01", qty=1)]
    sent = []

    with patch.object(task_order_alert, "_get_credentials",
                      return_value=("a", "d", "c", "t")), \
         patch("monitor.ebay_client.get_orders",
               return_value={"success": True, "orders": orders}), \
         patch("monitor.inventory_sync.sync_listing_quantity",
               return_value={"success": True, "skipped_zero_unsafe": False,
                             "message": "ok"}), \
         patch.object(task_order_alert, "_send_discord",
                      side_effect=lambda w, e: sent.append(e) or True):
        task_order_alert.run_order_alert_check({}, num_days=1)

    inv_embeds = [e for e in sent if "在庫" in e.get("title", "")]
    assert len(inv_embeds) == 0
