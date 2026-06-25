"""v81 売却リアルタイムアクション (sold_action_log / _process_sold_actions / consumer) のテスト。

カバー: claim 二重発火防止 / 無在庫→supplier_trigger / 有在庫は非トリガー /
Q0 空キー / webhook 未設定でも claim 成立 / sweep consumer 取込 / 冪等 migration。
"""
import sqlite3
import pytest

from monitor import database as db
from monitor.database import init_db, get_conn, record_sold_action


@pytest.fixture
def fresh_db():
    # conftest の autouse _isolate_monitor_db が DB_PATH を tmp に隔離済。schema 作成のみ。
    init_db()
    yield


def test_v81_migration_idempotent(fresh_db):
    """init_db 2 回でも sold_action_log とデータが保持される (db-migration-rules)。"""
    with get_conn() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] >= 81
    record_sold_action("O1", "I1", "sold_notify", "stock01", 2)
    init_db()  # 再実行
    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM sold_action_log WHERE ebay_order_id='O1'"
        ).fetchone()[0]
    assert n == 1, "init_db 再実行でデータ消失 = 冪等性違反"


def test_record_sold_action_claim_dedup(fresh_db):
    """同一 order×item×kind は 1 回だけ True (二重発火防止)。"""
    assert record_sold_action("O1", "I1", "sold_notify") is True
    assert record_sold_action("O1", "I1", "sold_notify") is False  # 重複
    # 別 kind は独立に claim 可
    assert record_sold_action("O1", "I1", "supplier_trigger") is True


def test_record_sold_action_empty_key_no_claim(fresh_db):
    """空 order/item は claim せず False (Q0: silent skip 禁止 = warning は出る)。"""
    assert record_sold_action("", "I1", "sold_notify") is False
    assert record_sold_action("O1", "", "sold_notify") is False
    with get_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM sold_action_log").fetchone()[0]
    assert n == 0


def test_process_sold_actions_nostock_triggers_supplier(fresh_db):
    """無在庫 (ebay* SKU) 売却は supplier_trigger を claim、有在庫はしない。"""
    import tasks.task_order_alert as toa
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, sku, title) "
            "VALUES ('I_NO','ebayyh_x','無在庫W'),('I_ST','stock01','有在庫W')"
        )
    # 無在庫: supplier_triggered=True, webhook 未設定で notified=False (claim は成立)
    r = toa._process_sold_actions(
        {"ebay_item_id": "I_NO", "order_id": "O_NO", "sku": "ebayyh_x"}, "", None
    )
    assert r["supplier_triggered"] is True
    assert r["notified"] is False  # webhook 未設定
    # 二重 polling: 両方 False
    r2 = toa._process_sold_actions(
        {"ebay_item_id": "I_NO", "order_id": "O_NO", "sku": "ebayyh_x"}, "", None
    )
    assert r2 == {"notified": False, "supplier_triggered": False}
    # 有在庫: supplier_triggered=False
    r3 = toa._process_sold_actions(
        {"ebay_item_id": "I_ST", "order_id": "O_ST", "sku": "stock01"}, "", 3
    )
    assert r3["supplier_triggered"] is False


def test_process_sold_actions_empty_item_q0(fresh_db):
    """ItemID 欠落注文は評価不能で両方 False (SKU fallback 禁止 / Q0)。"""
    import tasks.task_order_alert as toa
    r = toa._process_sold_actions({"order_id": "X", "sku": "ebay"}, "", None)
    assert r == {"notified": False, "supplier_triggered": False}


def test_sold_notify_stock_null_residual_traced(fresh_db, monkeypatch):
    """HIGH-1: 有在庫 stock SKU で残N不明 (new_count=None) でも claim 成立 +
    Discord に「在庫残: 不明」を明示 (silent 欠落させない)。"""
    import tasks.task_order_alert as toa
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, sku, title) "
            "VALUES ('I_NULL','stock01','在庫未設定W')"
        )
    captured = {}
    monkeypatch.setattr(
        toa, "_send_discord",
        lambda wh, embed: captured.update(embed) or True,
    )
    r = toa._process_sold_actions(
        {"ebay_item_id": "I_NULL", "order_id": "O_NULL", "sku": "stock01"},
        "http://hook", new_count=None,
    )
    assert r["notified"] is True
    assert "在庫残: 不明" in captured.get("description", ""), "残N不明を明示"
    # claim は成立 (二重防止維持)、new_quantity は NULL (audit)
    with get_conn() as c:
        row = c.execute(
            "SELECT new_quantity FROM sold_action_log "
            "WHERE ebay_order_id='O_NULL' AND action_kind='sold_notify'"
        ).fetchone()
    assert row is not None and row["new_quantity"] is None


def test_sold_notify_uses_order_title_for_intl(fresh_db, monkeypatch):
    """eBaymag各国版 (ebay_listings 未登録) の売却でも、注文データ(GetOrders)の
    title を使って商品名を出す (ItemID 表示に degrade させない)。"""
    import tasks.task_order_alert as toa
    captured = {}
    monkeypatch.setattr(
        toa, "_send_discord",
        lambda wh, embed: captured.update(embed) or True,
    )
    # ebay_listings に登録なし (各国版相当)、order に title あり
    r = toa._process_sold_actions(
        {"ebay_item_id": "358663691082", "order_id": "O_INTL", "sku": "stock",
         "title": "Google Pixel Tablet Charging Speaker Dock GA03944-US"},
        "http://hook", new_count=None,
    )
    assert r["notified"] is True
    assert "Google Pixel Tablet" in captured.get("description", ""), \
        "各国版売却でも注文データの商品名を出す"


def test_sold_trigger_consumer_picks_up(fresh_db):
    """無在庫 supplier_trigger claim を sweep consumer が拾い、候補ありは除外。"""
    import tasks.task_supplier_sweep as tss
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, sku, title) "
            "VALUES ('I_A','ebayyh_a','A'),('I_B','ebayyh_b','B')"
        )
    record_sold_action("O_A", "I_A", "supplier_trigger", "ebayyh_a")
    record_sold_action("O_B", "I_B", "supplier_trigger", "ebayyh_b")
    # I_B には claim 後の候補あり → consumer から除外される
    with get_conn() as c:
        c.execute(
            "INSERT INTO supplier_candidates "
            "(ebay_item_id, sku, candidate_url, match_score, status, created_at) "
            "VALUES ('I_B','ebayyh_b','http://x',80,'pending',datetime('now','+1 minute'))"
        )
    tgt = dict(tss._fetch_sold_trigger_targets(10))
    assert "I_A" in tgt, "未候補の無在庫売却は拾う"
    assert "I_B" not in tgt, "候補ありは二重探索防止で除外"
