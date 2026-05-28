"""W149 (2026-05-22): eBay 売却注文取得 + fulfillment 自動ひも付け — DB 層 + matcher 回帰.

設計書: .company/engineering/docs/2026-05-21-W149-ebay-orders-fetch-fulfillment-link-design.md v2

- migration v47 (sales_history.ebay_order_id + fulfillment_order_link
  + sales_history_fetch_failures) + v48 (UNIQUE INDEX 入れ替え (ebay_order_id,
  ebay_item_id) 複合キー、buyer まとめ買い 1 注文 N 商品の silent line 消失防止)
  の冪等性 (Q2: init_db() 2 回連続でデータ保持).
- 自己修復 (テーブル不在 + ver<48 → 再作成、W140 v44 / W148 v46 と同型 idiom).
- add_sale dedupe: 同 (ebay_order_id, ebay_item_id) 2 回目 = sale_id=0,
  + total_sold_count 累計 +1 のみ. 同 ebay_order_id 異 ebay_item_id は両方 INSERT.
- add_sale 後方互換 (ebay_order_id=None で旧 API 動作 = 既存呼出影響なし).
- link_unmatched FIFO (同 ebay_item_id で sales 古い順 × fulfillment 古い順 で 1:1).
- 時系列ガード (fulfillment.confirmed_at < sales.sold_at の組は match しない).
- link_one_by_sale (polling 直後の 1 件 realtime matching).
- link_one (confirm_purchase 末尾用、ebay_order_id 含む dict 返却).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_mod


def _insert_listing(conn, ebay_item_id: str, sku: str = "ebayme_test", title: str = "Test"):
    """ebay_listings 行を最小作成 (total_sold_count UPDATE のため)."""
    conn.execute(
        """INSERT INTO ebay_listings
           (ebay_item_id, sku, title, current_price, total_sold_count, total_revenue_usd)
           VALUES (?,?,?,?,?,?)""",
        (ebay_item_id, sku, title, 100.0, 0, 0.0),
    )


def _insert_fulfillment(conn, ebay_item_id: str, confirmed_at: str,
                        gmail_id: str = "g1"):
    """purchase_confirmation_log に fulfillment 行追加. id を返す."""
    cur = conn.execute(
        """INSERT INTO purchase_confirmation_log
           (gmail_id, ebay_item_id, quantity_added, confirmed_at, fulfillment_kind)
           VALUES (?,?,?,?,'fulfillment')""",
        (gmail_id, ebay_item_id, 1, confirmed_at),
    )
    return cur.lastrowid


# ---------- migration v47 idempotency & self-heal ----------

def test_v47_v48_idempotent_init_db_twice_retains_data(tmp_db):
    """Q2: データ投入後 init_db() 再実行で sales_history.ebay_order_id 列 +
    fulfillment_order_link + sales_history_fetch_failures + 複合 UNIQUE INDEX が消えない."""
    from monitor.database import get_conn, add_sale

    with get_conn() as c:
        _insert_listing(c, "ITM001")
    sid = add_sale(
        ebay_item_id="ITM001", sku="ebayme_test", title="Test",
        sold_price_usd=10.0, sold_at="2026-05-01T00:00:00",
        ebay_order_id="ORD001",
    )
    assert sid > 0

    with get_conn() as c:
        c.execute(
            "INSERT INTO sales_history_fetch_failures (ebay_order_id, last_error) "
            "VALUES (?, ?)",
            ("ORD999", "test error"),
        )

    tmp_db.init_db()  # 再実行

    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = [r[1] for r in c.execute("PRAGMA table_info(sales_history)").fetchall()]
        sh_count = c.execute("SELECT COUNT(*) FROM sales_history").fetchone()[0]
        fol_exists = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='fulfillment_order_link'"
        ).fetchone()[0]
        shff_count = c.execute(
            "SELECT COUNT(*) FROM sales_history_fetch_failures"
        ).fetchone()[0]
        idx_v48 = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_sales_history_order_item'"
        ).fetchone()[0]
        idx_v47_dropped = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_sales_history_ebay_order_id'"
        ).fetchone()[0]
    assert ver >= 48
    assert "ebay_order_id" in cols
    assert sh_count == 1
    assert fol_exists == 1
    assert shff_count == 1
    assert idx_v48 == 1, "v48 複合 UNIQUE INDEX (ebay_order_id, ebay_item_id) 不在"
    assert idx_v47_dropped == 0, "v47 単独 UNIQUE INDEX が v48 で drop されていない"


def test_v47_v48_self_heals_when_tables_missing(tmp_db):
    """過去に v47/v48 CREATE が失敗 (ver<48 + 必須 table 不在) → init_db で再作成 + ver=48.
    版数だけ進み永久欠落する事象を排除 (W140 v44 / W148 v46 と同型)."""
    from monitor.database import get_conn

    with get_conn() as c:
        c.execute("DROP TABLE fulfillment_order_link")
        c.execute("DROP TABLE sales_history_fetch_failures")
        c.execute("DROP INDEX IF EXISTS idx_sales_history_order_item")
        c.execute("PRAGMA user_version = 46")  # v47 未適用を再現

    tmp_db.init_db()  # ver<47 → v47 block 再突入 → v48 block も実行

    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        n = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('fulfillment_order_link','sales_history_fetch_failures')"
        ).fetchone()[0]
        idx_v48 = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_sales_history_order_item'"
        ).fetchone()[0]
    assert ver == 55  # cascade: init_db で v47/v48 block 完走後 v55 (W183 HEAD) まで進む
    assert n == 2
    assert idx_v48 == 1


# ---------- add_sale dedupe + backward compat ----------

def test_add_sale_dedupe_same_order_same_item(tmp_db):
    """同 (ebay_order_id, ebay_item_id) を 2 回 add_sale → 2 回目 = 0 (v48 複合 UNIQUE 衝突 skip)。
    total_sold_count は +1 のみ (INSERT 成立時のみ累計 UPDATE)."""
    from monitor.database import get_conn, add_sale

    with get_conn() as c:
        _insert_listing(c, "ITM002")

    sid1 = add_sale(
        ebay_item_id="ITM002", sku="ebayme_x", title="X",
        sold_price_usd=20.0, sold_at="2026-05-01T00:00:00",
        ebay_order_id="ORD002",
    )
    sid2 = add_sale(
        ebay_item_id="ITM002", sku="ebayme_x", title="X",
        sold_price_usd=20.0, sold_at="2026-05-01T00:00:00",
        ebay_order_id="ORD002",  # 同 (order_id, item_id)
    )
    assert sid1 > 0
    assert sid2 == 0

    with get_conn() as c:
        total = c.execute(
            "SELECT total_sold_count FROM ebay_listings WHERE ebay_item_id=?",
            ("ITM002",),
        ).fetchone()[0]
        rev = c.execute(
            "SELECT total_revenue_usd FROM ebay_listings WHERE ebay_item_id=?",
            ("ITM002",),
        ).fetchone()[0]
    assert total == 1
    assert abs(rev - 20.0) < 1e-6


def test_add_sale_same_order_different_item_both_insert(tmp_db):
    """buyer まとめ買い: 同 ebay_order_id 異 ebay_item_id 2 件 → 両方 INSERT 成立.
    v47 の単独 UNIQUE(ebay_order_id) だと 2 件目 silent skip = line item 消失 だった
    (Phase D dry-run で 101→100 silent loss を発見). v48 複合キーで両方保持."""
    from monitor.database import get_conn, add_sale

    with get_conn() as c:
        _insert_listing(c, "ITMA", sku="ebayme_a", title="Item A")
        _insert_listing(c, "ITMB", sku="ebayme_b", title="Item B")

    sid_a = add_sale(
        ebay_item_id="ITMA", sku="ebayme_a", title="Item A",
        sold_price_usd=15.0, sold_at="2026-05-01T00:00:00",
        ebay_order_id="ORD_BULK",
    )
    sid_b = add_sale(
        ebay_item_id="ITMB", sku="ebayme_b", title="Item B",
        sold_price_usd=25.0, sold_at="2026-05-01T00:00:00",
        ebay_order_id="ORD_BULK",  # 同 order_id 異 item_id
    )
    assert sid_a > 0
    assert sid_b > 0
    assert sid_a != sid_b

    with get_conn() as c:
        sh_count = c.execute(
            "SELECT COUNT(*) FROM sales_history WHERE ebay_order_id=?",
            ("ORD_BULK",),
        ).fetchone()[0]
        total_a = c.execute(
            "SELECT total_sold_count FROM ebay_listings WHERE ebay_item_id=?",
            ("ITMA",),
        ).fetchone()[0]
        total_b = c.execute(
            "SELECT total_sold_count FROM ebay_listings WHERE ebay_item_id=?",
            ("ITMB",),
        ).fetchone()[0]
    assert sh_count == 2, "同 order_id 異 item_id の 2 件が sales_history に保持されていない"
    assert total_a == 1
    assert total_b == 1


def test_add_sale_backward_compat_none_ebay_order_id(tmp_db):
    """旧 API: ebay_order_id=None なら INSERT 必ず成立 + total_sold_count +1.
    既存呼出への影響なしを担保."""
    from monitor.database import get_conn, add_sale

    with get_conn() as c:
        _insert_listing(c, "ITM003")

    sid = add_sale(
        ebay_item_id="ITM003", sku="ebayme_y", title="Y",
        sold_price_usd=30.0, sold_at="2026-05-01T00:00:00",
        # ebay_order_id 引数なし = None
    )
    assert sid > 0

    with get_conn() as c:
        total = c.execute(
            "SELECT total_sold_count FROM ebay_listings WHERE ebay_item_id=?",
            ("ITM003",),
        ).fetchone()[0]
    assert total == 1


# ---------- matcher: link_unmatched FIFO ----------

def test_link_unmatched_fifo_one_to_one(tmp_db):
    """同 ebay_item_id で sales=[s1<s2], fulfillment=[f1<f2] → s1-f1, s2-f2 で 1:1."""
    from monitor.database import get_conn, add_sale
    from monitor.fulfillment_order_matcher import link_unmatched

    with get_conn() as c:
        _insert_listing(c, "ITM004")

    s1 = add_sale(
        ebay_item_id="ITM004", sku="ebayme_z", title="Z",
        sold_price_usd=10.0, sold_at="2026-05-01T10:00:00",
        ebay_order_id="ORDS1",
    )
    s2 = add_sale(
        ebay_item_id="ITM004", sku="ebayme_z", title="Z",
        sold_price_usd=12.0, sold_at="2026-05-02T10:00:00",
        ebay_order_id="ORDS2",
    )
    with get_conn() as c:
        f1 = _insert_fulfillment(c, "ITM004", "2026-05-03T10:00:00", gmail_id="g1")
        f2 = _insert_fulfillment(c, "ITM004", "2026-05-04T10:00:00", gmail_id="g2")

    count = link_unmatched()
    assert count == 2

    with get_conn() as c:
        links = c.execute(
            "SELECT purchase_confirmation_log_id, sales_history_id FROM fulfillment_order_link "
            "ORDER BY id ASC"
        ).fetchall()
    pairs = [(r[0], r[1]) for r in links]
    assert (f1, s1) in pairs
    assert (f2, s2) in pairs


def test_link_unmatched_timeline_guard(tmp_db):
    """fulfillment.confirmed_at < sales.sold_at (仕入が売却より先) は match しない."""
    from monitor.database import get_conn, add_sale
    from monitor.fulfillment_order_matcher import link_unmatched

    with get_conn() as c:
        _insert_listing(c, "ITM005")
        # 仕入が先 (5/1) なのに売却が後 (5/10) = 別物
        _insert_fulfillment(c, "ITM005", "2026-05-01T10:00:00", gmail_id="g_early")

    add_sale(
        ebay_item_id="ITM005", sku="ebayme_w", title="W",
        sold_price_usd=10.0, sold_at="2026-05-10T10:00:00",
        ebay_order_id="ORDS5",
    )

    count = link_unmatched()
    assert count == 0  # 時系列ガード発火


# ---------- matcher: link_one_by_sale (polling 直後 realtime) ----------

def test_link_one_by_sale_matches_next_fulfillment(tmp_db):
    """polling 直後の sale_id を、直後に登録された fulfillment と match."""
    from monitor.database import get_conn, add_sale
    from monitor.fulfillment_order_matcher import link_one_by_sale

    with get_conn() as c:
        _insert_listing(c, "ITM006")

    sid = add_sale(
        ebay_item_id="ITM006", sku="ebayme_v", title="V",
        sold_price_usd=10.0, sold_at="2026-05-01T10:00:00",
        ebay_order_id="ORDS6",
    )
    with get_conn() as c:
        fid = _insert_fulfillment(c, "ITM006", "2026-05-02T10:00:00", gmail_id="g6")

    matched_fid = link_one_by_sale(sid)
    assert matched_fid == fid

    # 2 回目呼出は既マッチで None
    matched_again = link_one_by_sale(sid)
    assert matched_again is None


# ---------- matcher: link_one (confirm_purchase 末尾) ----------

def test_link_one_returns_dict_with_ebay_order_id(tmp_db):
    """confirm_purchase 末尾用: dict に ebay_order_id 含む."""
    from monitor.database import get_conn, add_sale
    from monitor.fulfillment_order_matcher import link_one

    with get_conn() as c:
        _insert_listing(c, "ITM007")

    sid = add_sale(
        ebay_item_id="ITM007", sku="ebayme_u", title="U",
        sold_price_usd=10.0, sold_at="2026-05-01T10:00:00",
        ebay_order_id="ORDS7",
    )
    with get_conn() as c:
        fid = _insert_fulfillment(c, "ITM007", "2026-05-02T10:00:00", gmail_id="g7")

    result = link_one("ITM007")
    assert result is not None
    assert result["purchase_confirmation_log_id"] == fid
    assert result["sale_id"] == sid
    assert result["ebay_order_id"] == "ORDS7"


def test_link_one_returns_none_when_no_match(tmp_db):
    """fulfillment 0 件 → None (silent skip でなく明示 None)."""
    from monitor.database import get_conn, add_sale
    from monitor.fulfillment_order_matcher import link_one

    with get_conn() as c:
        _insert_listing(c, "ITM008")
    add_sale(
        ebay_item_id="ITM008", sku="ebayme_t", title="T",
        sold_price_usd=10.0, sold_at="2026-05-01T10:00:00",
        ebay_order_id="ORDS8",
    )

    result = link_one("ITM008")
    assert result is None


# ---------- HIGH-1 (code-reviewer Phase D): paid_time 空 skip ----------

def test_task_order_alert_skips_unpaid_orders(tmp_db, monkeypatch):
    """HIGH-1: GetOrders は OrderStatus=Active (未払い 13 日以内) も返す.
    paid_time 空のまま sales_history に INSERT すると sold_at='' で:
      (a) 商品管理 sold_at DESC 並び順で先頭固定 → W149 主目的破綻
      (b) matcher の sold_at <= confirmed_at が文字列比較で常に True → 時系列ガード崩壊
    paid_time 空は skip して次回 polling で paid 後に取込 (UNIQUE 複合キーで衝突防止)."""
    from monitor.database import get_conn
    from tasks import task_order_alert

    with get_conn() as c:
        _insert_listing(c, "ITM_UNPAID")
        _insert_listing(c, "ITM_PAID")

    fake_orders = {
        "success": True,
        "orders": [
            # 未払い注文 (skip 対象)
            {"order_id": "ORD_UNPAID", "ebay_item_id": "ITM_UNPAID", "sku": "ebayme_u",
             "title": "Unpaid item", "qty": 1, "item_price_usd": 10.0,
             "shipping_usd": 5.0, "paid_time": "", "buyer_country": "US"},
            # 支払済 (取込対象)
            {"order_id": "ORD_PAID", "ebay_item_id": "ITM_PAID", "sku": "ebayme_p",
             "title": "Paid item", "qty": 1, "item_price_usd": 20.0,
             "shipping_usd": 7.0, "paid_time": "2026-05-22T01:00:00.000Z",
             "buyer_country": "US"},
        ],
    }

    monkeypatch.setattr(task_order_alert, "_get_credentials",
                        lambda: ("a", "d", "c", "u"))
    monkeypatch.setattr("monitor.ebay_client.get_orders",
                        lambda *a, **k: fake_orders)

    result = task_order_alert.run_order_alert_check(config={}, num_days=1)
    assert result["success"]
    assert result["sales_recorded"] == 1  # 支払済 1 件のみ

    with get_conn() as c:
        sh_count = c.execute("SELECT COUNT(*) FROM sales_history").fetchone()[0]
        sold_at_empty = c.execute(
            "SELECT COUNT(*) FROM sales_history WHERE sold_at = ''"
        ).fetchone()[0]
        unpaid_rec = c.execute(
            "SELECT COUNT(*) FROM sales_history WHERE ebay_order_id = ?",
            ("ORD_UNPAID",),
        ).fetchone()[0]
    assert sh_count == 1, "支払済 1 件のみ sales_history に入っているべき"
    assert sold_at_empty == 0, "sold_at='' 行は絶対に作らない (並び順破壊 + 時系列ガード崩壊)"
    assert unpaid_rec == 0, "未払い ORD_UNPAID は sales_history に存在してはならない"


def test_task_order_alert_paid_after_repolling_inserts(tmp_db, monkeypatch):
    """未払いで 1 回目 skip → paid_time 入った 2 回目 polling で INSERT 成立 (UNIQUE 衝突なし).
    v48 複合 UNIQUE INDEX (ebay_order_id, ebay_item_id) は同 order_id で重複検出するが、
    1 回目は INSERT 自体走らない (paid_time 空で continue) ので 2 回目は素通り."""
    from monitor.database import get_conn
    from tasks import task_order_alert

    with get_conn() as c:
        _insert_listing(c, "ITM_LATE_PAID")

    unpaid_orders = {
        "success": True,
        "orders": [
            {"order_id": "ORD_LP", "ebay_item_id": "ITM_LATE_PAID", "sku": "ebayme_l",
             "title": "Late paid", "qty": 1, "item_price_usd": 30.0,
             "shipping_usd": 5.0, "paid_time": "", "buyer_country": "US"},
        ],
    }
    paid_orders = {
        "success": True,
        "orders": [
            {"order_id": "ORD_LP", "ebay_item_id": "ITM_LATE_PAID", "sku": "ebayme_l",
             "title": "Late paid", "qty": 1, "item_price_usd": 30.0,
             "shipping_usd": 5.0, "paid_time": "2026-05-22T02:00:00.000Z",
             "buyer_country": "US"},
        ],
    }

    monkeypatch.setattr(task_order_alert, "_get_credentials",
                        lambda: ("a", "d", "c", "u"))

    # 1 回目 (未払い): skip
    monkeypatch.setattr("monitor.ebay_client.get_orders",
                        lambda *a, **k: unpaid_orders)
    result1 = task_order_alert.run_order_alert_check(config={}, num_days=1)
    assert result1["sales_recorded"] == 0

    # 2 回目 (支払済): INSERT 成立
    monkeypatch.setattr("monitor.ebay_client.get_orders",
                        lambda *a, **k: paid_orders)
    result2 = task_order_alert.run_order_alert_check(config={}, num_days=1)
    assert result2["sales_recorded"] == 1

    with get_conn() as c:
        row = c.execute(
            "SELECT sold_at FROM sales_history WHERE ebay_order_id = ?",
            ("ORD_LP",),
        ).fetchone()
    assert row is not None
    assert row[0] == "2026-05-22T02:00:00.000Z"
