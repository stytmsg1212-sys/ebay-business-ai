"""W120 (Amazon/楽天 supplier 追加) + W121 (価格変動検知) 回帰テスト.

設計: code-architect ブループリント (2026-05-12) 準拠.
スコープ:
  - migration v38: monitored_items に 4 列 + 楽天 sold_out/no_page text 補完
  - price_extractor.py: Amazon / 楽天 HTML 抽出
  - task_inventory_check.py の 3 helper (_update_price / _evaluate_restock / _fetch_and_store)
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_mi(sku: str, source_url: str, last_status: str = "unknown",
               baseline: int = None, current: int = None,
               alert_state: str = None) -> int:
    """monitored_items に 1 行 insert. 返り値は id."""
    from monitor.database import get_conn
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, last_status, is_active,
                baseline_price_jpy, current_price_jpy, price_alert_state)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (sku, source_url, last_status, baseline, current, alert_state),
        )
        return cur.lastrowid


# =============================================================================
# Migration v38: monitored_items 列追加 + 楽天 text 補完
# =============================================================================

def test_v38_monitored_items_has_4_new_columns(tmp_db):
    """baseline_price_jpy / current_price_jpy / baseline_at / price_alert_state 列が存在."""
    from monitor.database import get_conn
    with get_conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(monitored_items)").fetchall()}
    for required in ("baseline_price_jpy", "current_price_jpy",
                     "baseline_at", "price_alert_state"):
        assert required in cols, f"v38 で {required} 列が追加されていない"


def test_v38_schema_version(tmp_db):
    """user_version >= 38."""
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 38, f"schema_ver={ver} < 38"


def test_v38_init_db_idempotent(tmp_db):
    """init_db 2 回連続でデータ保持 (Q2 冪等性)."""
    from monitor.database import get_conn, init_db
    item_id = _insert_mi("ebayAM_test", "https://amzn/test", baseline=12345, current=12345)
    init_db()  # 再実行
    with get_conn() as c:
        row = c.execute(
            "SELECT baseline_price_jpy FROM monitored_items WHERE id=?",
            (item_id,),
        ).fetchone()
    assert row and row["baseline_price_jpy"] == 12345, "v38 冪等性違反"


def test_v38_rakuten_text_filled(tmp_db):
    """楽天市場 site_config の sold_out_text / no_page_text が補完される."""
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT sold_out_text, no_page_text FROM site_configs WHERE convert_url='ebayRT_'"
        ).fetchone()
    assert row is not None, "楽天市場 site_config 不在"
    assert row["sold_out_text"], "v38 で sold_out_text が空のまま"
    assert row["no_page_text"], "v38 で no_page_text が空のまま"


# =============================================================================
# price_extractor.py: Amazon / 楽天 HTML 抽出
# =============================================================================

def test_extract_price_rakuten_meta_itemprop():
    """楽天 meta[itemprop=price] パターンを抽出."""
    from monitor.price_extractor import extract_price_rakuten
    html = '<meta itemprop="price" content="3980">'
    assert extract_price_rakuten(html) == 3980


def test_extract_price_rakuten_class_price2_fallback():
    """楽天 class=price2 fallback パターンを抽出."""
    from monitor.price_extractor import extract_price_rakuten
    html = '<div class="price2"><span>12,800</span> 円</div>'
    assert extract_price_rakuten(html) == 12800


def test_extract_price_rakuten_returns_none_when_no_match():
    """selector 不一致なら None (silent skip でなく明示)."""
    from monitor.price_extractor import extract_price_rakuten
    assert extract_price_rakuten("<html><body>no price here</body></html>") is None


def test_extract_price_amazon_offscreen_yen():
    """Amazon a-offscreen ￥ パターン."""
    from monitor.price_extractor import extract_price_amazon
    html = '<span class="a-offscreen">￥4,580</span>'
    assert extract_price_amazon(html) == 4580


def test_extract_price_amazon_a_price_whole():
    """Amazon a-price-whole パターン."""
    from monitor.price_extractor import extract_price_amazon
    html = 'class="a-price-whole">9,800<'
    assert extract_price_amazon(html) == 9800


def test_extract_price_amazon_priceamount_json():
    """Amazon JSON-LD priceAmount."""
    from monitor.price_extractor import extract_price_amazon
    html = '"priceAmount":1980.0'
    assert extract_price_amazon(html) == 1980


def test_extract_price_amazon_returns_none_when_no_match():
    """selector 全部 miss なら None."""
    from monitor.price_extractor import extract_price_amazon
    assert extract_price_amazon("<html>no price</html>") is None


def test_extract_price_routes_by_sku_prefix():
    """SKU prefix で正しく site 振り分け."""
    from monitor.price_extractor import extract_price
    html_rt = '<meta itemprop="price" content="500">'
    html_am = '<span class="a-offscreen">￥1,000</span>'
    assert extract_price(html_rt, "ebayRT_abc123") == 500
    assert extract_price(html_am, "ebayAM_xyz789") == 1000
    # 対象外 prefix
    assert extract_price(html_am, "ebayme_test") is None
    assert extract_price(html_am, "stock:01") is None


def test_extract_price_empty_inputs():
    """空 input は None."""
    from monitor.price_extractor import extract_price
    assert extract_price("", "ebayAM_test") is None
    assert extract_price("<html/>", "") is None


# =============================================================================
# task_inventory_check helpers: baseline / surge / drop / restock
# =============================================================================

def test_update_price_baseline_first_record(tmp_db):
    """初回取得時に baseline=current で固定、state='normal'."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_test", "https://amzn/test")
    _update_price_and_evaluate_alert(item_id, 5000)

    with get_conn() as c:
        row = c.execute(
            "SELECT baseline_price_jpy, current_price_jpy, baseline_at, price_alert_state "
            "FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["baseline_price_jpy"] == 5000
    assert row["current_price_jpy"] == 5000
    assert row["baseline_at"] is not None
    assert row["price_alert_state"] == "normal"


def test_update_price_surge_at_plus_5_percent(tmp_db):
    """ちょうど +5% で surge (W193: ±5% に統一)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayRT_test", "https://item.rakuten/test",
                         baseline=10000, current=10000, alert_state="normal")
    _update_price_and_evaluate_alert(item_id, 10500)  # +5.0%
    with get_conn() as c:
        row = c.execute(
            "SELECT current_price_jpy, price_alert_state FROM monitored_items WHERE id=?",
            (item_id,)
        ).fetchone()
    assert row["current_price_jpy"] == 10500
    assert row["price_alert_state"] == "surge"


def test_update_price_drop_at_minus_5_percent(tmp_db):
    """ちょうど -5% で drop (W193: ±5% に統一)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_test", "https://amzn/test",
                         baseline=10000, current=10000, alert_state="normal")
    _update_price_and_evaluate_alert(item_id, 9500)  # -5.0%
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "drop"


def test_update_price_normal_within_threshold(tmp_db):
    """±5% 未満 (例 +4%、旧 3% 閾値なら surge になる値) は normal."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_test", "https://amzn/test",
                         baseline=10000, current=10000, alert_state="normal")
    _update_price_and_evaluate_alert(item_id, 10400)  # +4.0% (新閾値内、旧閾値超)
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "normal"


def test_update_price_baseline_zero_defensive(tmp_db):
    """baseline=0 (異常値) なら state='normal' で防御."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_test", "https://amzn/test",
                         baseline=0, current=0, alert_state="normal")
    _update_price_and_evaluate_alert(item_id, 5000)
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state, current_price_jpy FROM monitored_items WHERE id=?",
            (item_id,)
        ).fetchone()
    # 異常値防御で normal 維持、current は更新される
    assert row["price_alert_state"] == "normal"
    assert row["current_price_jpy"] == 5000


def test_update_price_missing_listing_no_op(tmp_db):
    """存在しない item_id では何も起きない (例外も発生しない)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    _update_price_and_evaluate_alert(999999, 1000)  # no row matched
    # 例外なく完了すれば PASS


def test_evaluate_restock_alerts_detects_unavailable_to_available(tmp_db):
    """last_status='在庫無' → 新 status='在庫有' で restock alert を立てる."""
    from tasks.task_inventory_check import _evaluate_restock_alerts
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_test", "https://amzn/test", last_status="在庫無")
    results = [{"id": item_id, "status": "在庫有", "sku": "ebayAM_test"}]
    n = _evaluate_restock_alerts(results)
    assert n == 1

    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "restock"


def test_evaluate_restock_alerts_skips_non_restock_transitions(tmp_db):
    """在庫有→在庫有 / 在庫有→在庫無 / 在庫無→在庫無 は restock 立てない."""
    from tasks.task_inventory_check import _evaluate_restock_alerts

    id1 = _insert_mi("ebayAM_a", "url1", last_status="在庫有")
    id2 = _insert_mi("ebayAM_b", "url2", last_status="在庫有")
    id3 = _insert_mi("ebayAM_c", "url3", last_status="在庫無")
    results = [
        {"id": id1, "status": "在庫有", "sku": "ebayAM_a"},  # 維持 (skip)
        {"id": id2, "status": "在庫無", "sku": "ebayAM_b"},  # 在庫切れ遷移 (skip)
        {"id": id3, "status": "在庫無", "sku": "ebayAM_c"},  # 維持 (skip)
    ]
    n = _evaluate_restock_alerts(results)
    assert n == 0


def test_evaluate_restock_alerts_no_id_in_result(tmp_db):
    """results に id 含まれない行は skip (例外なし)."""
    from tasks.task_inventory_check import _evaluate_restock_alerts
    results = [{"status": "在庫有", "sku": "ebayAM_test"}]  # id 不在
    n = _evaluate_restock_alerts(results)
    assert n == 0


# =============================================================================
# Wave E (2026-05-12 code-review #3 fix): H2-H10 regression
# =============================================================================

def test_h2_rakuten_zero_returns_none():
    """H2: meta itemprop=0 は None (baseline 0 sticky 防止)."""
    from monitor.price_extractor import extract_price_rakuten
    assert extract_price_rakuten('<meta itemprop="price" content="0">') is None


def test_h2_rakuten_class_price2_zero_returns_none():
    """H2: class price2 でも 0 は None."""
    from monitor.price_extractor import extract_price_rakuten
    assert extract_price_rakuten(
        '<div class="price2"><span>0</span> 円</div>'
    ) is None


def test_h3_restock_state_preserved_within_threshold(tmp_db):
    """H3: 現 state='restock' + price ±3% 未満 → state は restock 維持、current のみ更新."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_t", "https://amzn/t",
                         baseline=10000, current=10000, alert_state="restock")
    _update_price_and_evaluate_alert(item_id, 10100)  # +1.0% (閾値内)
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state, current_price_jpy FROM monitored_items WHERE id=?",
            (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "restock", "restock state が price 評価で上書きされている"
    assert row["current_price_jpy"] == 10100, "current_price は更新されているべき"


def test_h3_restock_state_preserved_even_with_surge(tmp_db):
    """H3: 現 state='restock' + price +5% (surge ライン) でも restock 維持."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    from monitor.database import get_conn

    item_id = _insert_mi("ebayRT_t", "https://item.rakuten/t",
                         baseline=10000, current=10000, alert_state="restock")
    _update_price_and_evaluate_alert(item_id, 10500)  # +5.0%
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?",
            (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "restock", "restock が surge に上書きされている"


def test_h4_restock_auto_expires_after_24h(tmp_db):
    """H4: last_check が 24h 以上前の restock は normal に自動降格."""
    from tasks.task_inventory_check import _evaluate_restock_alerts
    from monitor.database import get_conn

    # last_check を 25h 前にして restock state を作る
    item_id = _insert_mi("ebayAM_t", "url", last_status="在庫有", alert_state="restock")
    with get_conn() as c:
        c.execute(
            "UPDATE monitored_items SET last_check = datetime('now', '-25 hours') "
            "WHERE id=?", (item_id,)
        )
    # 空 results で _evaluate_restock_alerts 呼出 → 24h 経過の restock が降格
    _evaluate_restock_alerts([])
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "normal", "24h 経過 restock が降格されていない"


def test_h4_restock_within_24h_not_expired(tmp_db):
    """H4: last_check が 23h 前の restock は降格しない (24h 閾値)."""
    from tasks.task_inventory_check import _evaluate_restock_alerts
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_t", "url", last_status="在庫有", alert_state="restock")
    with get_conn() as c:
        c.execute(
            "UPDATE monitored_items SET last_check = datetime('now', '-23 hours') "
            "WHERE id=?", (item_id,)
        )
    _evaluate_restock_alerts([])
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "restock", "24h 未満で restock が早期降格"


def test_h9_fetch_and_store_prices_has_jitter_sleep(tmp_db, monkeypatch):
    """H9: bot 検知緩和の jitter sleep が _fetch_and_store_prices 内に挟まる."""
    import time
    import tasks.task_inventory_check as tic

    sleep_calls = []
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    # 2 件 target を作成 (1 件目は sleep なし、2 件目以降は sleep あり想定)
    _insert_mi("ebayAM_a", "https://invalid-url-test-1.example/")
    _insert_mi("ebayRT_b", "https://invalid-url-test-2.example/")
    results = [
        {"id": 1, "sku": "ebayAM_a"},
        {"id": 2, "sku": "ebayRT_b"},
    ]
    # 失敗するが sleep call は記録される (httpx は invalid URL で例外)
    tic._fetch_and_store_prices(results, {})  # W193: config 引数追加
    # 2 件 target → 1 回 sleep (idx>0 のときのみ)
    assert len(sleep_calls) >= 1, "_fetch_and_store_prices に sleep が無い (H9)"
    # 値域確認 (1.5-3.5s)
    for s in sleep_calls:
        assert 1.5 <= s <= 3.5, f"sleep 値 {s} が想定 jitter 範囲外"


def test_h10_default_site_configs_rakuten_has_filled_text():
    """H10: DEFAULT_SITE_CONFIGS の楽天市場 entry に sold_out / no_page text が埋まっている."""
    from monitor.database import DEFAULT_SITE_CONFIGS
    rakuten = next(
        (c for c in DEFAULT_SITE_CONFIGS if c.get("convert_url") == "ebayRT_"), None
    )
    assert rakuten is not None, "DEFAULT_SITE_CONFIGS に楽天市場 entry がない"
    assert rakuten["sold_out_text"], "楽天 sold_out_text が空 (H10)"
    assert rakuten["no_page_text"], "楽天 no_page_text が空 (H10)"
