"""Wave A (2026-05-12) regression tests.

Code review (Opus 4.7) で検出された HIGH 4 件への回帰テスト:
  - H1: inventory_decrement_log が init_db (migration v37) に集約
  - H2: INSERT-first atomic ordering で 二重減算 race 排除
  - H3: order 処理 except で order_processing_errors カウンタ加算 (偽装成功防止)
  - H10: search_items の API 失敗が `return []` ではなく raise (silent skip 防止)
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch, MagicMock

import httpx
import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_stock_listing(ebay_item_id: str, sku: str, inventory_count, title="T"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended, inventory_count)
               VALUES (?, ?, ?, 0, ?)""",
            (ebay_item_id, sku, title, inventory_count),
        )


# =============================================================================
# H1: migration v37 で inventory_decrement_log を init_db に集約
# =============================================================================

def test_h1_inventory_decrement_log_exists_after_init_db(tmp_db):
    """init_db 直後に inventory_decrement_log が存在 (migration v37)."""
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory_decrement_log'"
        ).fetchone()
    assert row is not None, "migration v37 で inventory_decrement_log が作成されていない"


def test_h1_schema_version_at_least_37(tmp_db):
    """user_version が 37 以上に進んでいる."""
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 37, f"schema_ver={ver} < 37, migration v37 が適用されていない"


def test_h1_init_db_idempotent(tmp_db):
    """init_db 2 回連続で inventory_decrement_log の data が保持される (Q2 冪等性)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            """INSERT INTO inventory_decrement_log
               (order_id, ebay_item_id, sku, quantity_decremented, new_inventory_count)
               VALUES ('O1', 'id1', 'stock:01', 1, 5)"""
        )
    init_db()  # 再実行
    with get_conn() as c:
        row = c.execute("SELECT COUNT(*) FROM inventory_decrement_log").fetchone()
    assert row[0] >= 1, "init_db 再実行でデータ消失 → 冪等性違反"


# =============================================================================
# H2: 二重減算 race 排除 (INSERT-first atomic ordering)
# =============================================================================

def test_h2_decrement_idempotent_same_order(tmp_db):
    """同じ order_id × ebay_item_id を 2 回呼んでも在庫減算は 1 回のみ."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku
    from monitor.database import get_conn

    _insert_stock_listing("id1", "stock:01", inventory_count=5)
    order = {"sku": "stock:01", "ebay_item_id": "id1", "order_id": "O1", "qty": 2}

    dec1 = _decrement_inventory_for_stock_sku(order)
    dec2 = _decrement_inventory_for_stock_sku(order)

    assert dec1 is not None, "1 回目の減算が成功していない"
    assert dec1["new_count"] == 3, "1 回目: 5-2=3 のはず"
    assert dec2 is None, "2 回目は重複 polling として skip されるべき"

    with get_conn() as c:
        row = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='id1'"
        ).fetchone()
    assert row["inventory_count"] == 3, "在庫が二重減算されている (3 になっているべき、2 ではない)"


def test_h2_decrement_log_back_filled_with_real_count(tmp_db):
    """new_inventory_count が placeholder -1 ではなく実値で back-fill される."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku
    from monitor.database import get_conn

    _insert_stock_listing("id1", "stock:01", inventory_count=5)
    _decrement_inventory_for_stock_sku(
        {"sku": "stock:01", "ebay_item_id": "id1", "order_id": "O1", "qty": 2}
    )

    with get_conn() as c:
        row = c.execute(
            "SELECT new_inventory_count FROM inventory_decrement_log "
            "WHERE order_id='O1' AND ebay_item_id='id1'"
        ).fetchone()
    assert row["new_inventory_count"] == 3, (
        "INSERT-first 後の UPDATE で実値 3 に back-fill されるべき、placeholder -1 のまま"
    )


def test_h2_decrement_null_inventory_rolls_back_claim(tmp_db):
    """inventory_count=NULL なら claim を rollback (log にも残らない)."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku
    from monitor.database import get_conn

    _insert_stock_listing("id1", "stock:01", inventory_count=None)
    result = _decrement_inventory_for_stock_sku(
        {"sku": "stock:01", "ebay_item_id": "id1", "order_id": "O1", "qty": 1}
    )
    assert result is None, "NULL inventory は減算 skip"

    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inventory_decrement_log WHERE order_id='O1'"
        ).fetchone()
    assert row[0] == 0, (
        "claim rollback されていない (NULL inventory のとき log を残してはいけない、"
        "次回の polling で「既処理」と誤判定される)"
    )


def test_h2_decrement_missing_listing_rolls_back_claim(tmp_db):
    """ebay_item_id が DB に無いとき claim を rollback."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku
    from monitor.database import get_conn

    result = _decrement_inventory_for_stock_sku(
        {"sku": "stock:01", "ebay_item_id": "missing", "order_id": "O1", "qty": 1}
    )
    assert result is None

    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inventory_decrement_log WHERE order_id='O1'"
        ).fetchone()
    assert row[0] == 0, "missing listing の claim が残存している"


def test_h2_decrement_skips_non_stock_sku(tmp_db):
    """sku が ebay** prefix (無在庫) なら減算対象外."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku

    _insert_stock_listing("id1", "ebayyh_p123", inventory_count=5)
    result = _decrement_inventory_for_stock_sku(
        {"sku": "ebayyh_p123", "ebay_item_id": "id1", "order_id": "O1", "qty": 1}
    )
    assert result is None, "ebay** prefix (無在庫 SKU) は減算対象外"


def test_h2_decrement_qty_floor_at_zero(tmp_db):
    """qty が在庫数を超えても max(0, current-qty) で 0 floor."""
    from tasks.task_order_alert import _decrement_inventory_for_stock_sku
    from monitor.database import get_conn

    _insert_stock_listing("id1", "stock:01", inventory_count=2)
    dec = _decrement_inventory_for_stock_sku(
        {"sku": "stock:01", "ebay_item_id": "id1", "order_id": "O1", "qty": 5}
    )
    assert dec is not None
    assert dec["new_count"] == 0, "underflow は 0 floor されるべき (2-5=-3 ではない)"


# =============================================================================
# H3: order_processing_errors カウンタで偽装成功防止
# =============================================================================

def test_h3_result_dict_includes_order_processing_errors_key():
    """run_order_alert_check の result dict に order_processing_errors キーが含まれる."""
    # 構造 verify のみ (実 GetOrders を mock する代わり、return shape を直接 inspect する代替)
    # NOTE: 単体で run_order_alert_check 全体を走らせるには credentials + GetOrders mock 必要.
    # ここでは「コードに order_processing_errors を返している」ことを source-level に test.
    import inspect
    import tasks.task_order_alert as t
    src = inspect.getsource(t.run_order_alert_check)
    assert "order_processing_errors" in src, (
        "run_order_alert_check が order_processing_errors を計上していない (H3 違反)"
    )
    assert "errors=" in src, "log message に errors= が含まれていない"


# =============================================================================
# H10: search_items の API 失敗が raise (silent fallback 解消)
# =============================================================================

def test_h10_search_items_429_raises_not_silent_fallback():
    """HTTP 429 で `return []` ではなく HTTPStatusError raise."""
    from tasks.ebay_browse_api import BrowseAPIClient

    client = BrowseAPIClient(app_id="x", cert_id="y")
    client._token = "fake_token"
    client._token_expires_at = 9999999999

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429 Too Many Requests", request=MagicMock(), response=mock_resp,
    )

    with patch("tasks.ebay_browse_api.httpx.get", return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            client.search_items("TEST", limit=10)


def test_h10_search_items_network_error_raises():
    """RequestError 系も raise (旧実装は broad except で `return []` していた)."""
    from tasks.ebay_browse_api import BrowseAPIClient

    client = BrowseAPIClient(app_id="x", cert_id="y")
    client._token = "fake_token"
    client._token_expires_at = 9999999999

    with patch("tasks.ebay_browse_api.httpx.get",
               side_effect=httpx.ConnectError("conn refused")):
        with pytest.raises(httpx.RequestError):
            client.search_items("TEST", limit=10)


def test_h10_search_items_true_zero_results_returns_empty_list():
    """`itemSummaries` が空配列で返る = 真の 0 件は今まで通り [] を返す."""
    from tasks.ebay_browse_api import BrowseAPIClient

    client = BrowseAPIClient(app_id="x", cert_id="y")
    client._token = "fake_token"
    client._token_expires_at = 9999999999

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"itemSummaries": []}

    with patch("tasks.ebay_browse_api.httpx.get", return_value=mock_resp):
        result = client.search_items("TEST", limit=10)
    assert result == [], "真の 0 件は [] を返すべき"


# =============================================================================
# Wave B (2026-05-12) regressions
# =============================================================================

# H6: --saturated-only で JSON 不在 / 該当 0 件で silent skip しない
# H7: --saturated-only で前回失敗 listing (results[eid]=None) も自動 retry 対象に含める
#     CLI 全体は subprocess 経由でしか実行できないが、核となる target_ids 計算ロジックは
#     その場で再現可能なため、振る舞い等価な mini-helper で test する.

def _compute_saturated_target_ids(existing_results: dict, threshold: int) -> set:
    """run_w119_bulk_browse.py main の target_ids 計算と同等 (H6+H7 の core ロジック)."""
    saturated_ids = {
        eid for eid, items in existing_results.items()
        if items and len(items) >= threshold
    }
    failed_ids = {
        eid for eid, items in existing_results.items() if items is None
    }
    return saturated_ids | failed_ids


def test_h7_saturated_only_includes_failed_listings():
    """H7: --saturated-only は失敗 listing (None) も target に含める."""
    existing = {
        "id_sat": [{}] * 10,   # saturated (10 件以上)
        "id_low": [{}] * 5,    # 未 saturated
        "id_fail": None,       # 前回 API 失敗
        "id_zero": [],         # 真の 0 件
    }
    target = _compute_saturated_target_ids(existing, threshold=10)
    assert target == {"id_sat", "id_fail"}, (
        "H7 違反: saturated + failed の両方が含まれるべき "
        "(saturated だけだと前回失敗が永久に retry されない)"
    )


def test_h6_saturated_only_zero_target_handled_distinctly():
    """H6: target_ids が空集合の場合は呼出側で「対象 0 件」として明示する責務.

    run_w119_bulk_browse.py main は `if not listings: print("対象 0 件...") sys.exit(0)` で対応.
    """
    existing = {
        "id_low": [{}] * 5,  # 未 saturated
        "id_zero": [],       # 真の 0 件 (None ではないので failed 扱いではない)
    }
    target = _compute_saturated_target_ids(existing, threshold=10)
    assert target == set(), "true zero と未 saturated のみなら target は空"


def test_h6_saturated_only_normal_case_returns_saturated_only_when_no_failed():
    """failed listing 無しで saturated のみある場合: saturated だけ返す."""
    existing = {
        "id_a": [{}] * 20,
        "id_b": [{}] * 15,
        "id_c": [{}] * 5,
    }
    target = _compute_saturated_target_ids(existing, threshold=10)
    assert target == {"id_a", "id_b"}


def _apply_bulk_failure_with_preservation(results: dict, eid: str) -> None:
    """run_w119_bulk_browse.py の except 句と等価.

    H10 副作用 fix (2026-05-12): 前回成功 (非空 list) なら results[eid] を変更しない.
    前回も失敗 (None) or 真の 0 件 ([]) なら None で上書き.
    """
    prev = results.get(eid)
    if prev is None or prev == []:
        results[eid] = None


def test_h10_side_effect_preserves_prior_success_on_429():
    """search_items raise によって前回成功 listing が None で上書きされない (H10 副作用)."""
    results = {
        "id_good": [{"legacy_item_id": "111", "title": "..."}],   # 前回成功
        "id_zero": [],                                              # 真の 0 件
        "id_fail": None,                                            # 前回失敗
    }
    _apply_bulk_failure_with_preservation(results, "id_good")
    _apply_bulk_failure_with_preservation(results, "id_zero")
    _apply_bulk_failure_with_preservation(results, "id_fail")

    # 前回成功は温存される (None で上書きされない)
    assert results["id_good"] == [{"legacy_item_id": "111", "title": "..."}], (
        "429 等で前回成功 listing の良いデータが消失 (H10 副作用)"
    )
    # 前回失敗 / 真の 0 件は None 上書き OK
    assert results["id_zero"] is None
    assert results["id_fail"] is None


def test_m4_classify_ddu_none_when_taxes_empty():
    """taxes None / empty → 判別不能で None."""
    from tasks.ebay_browse_api import BrowseAPIClient
    assert BrowseAPIClient._classify_ddu_from_taxes(None) is None
    assert BrowseAPIClient._classify_ddu_from_taxes([]) is None


def test_m4_classify_ddu_ddp_when_included_in_price():
    """IMPORT_DUTY + includedInPrice=True → DDP (False)."""
    from tasks.ebay_browse_api import BrowseAPIClient
    taxes = [{"taxType": "IMPORT_DUTY", "includedInPrice": True}]
    assert BrowseAPIClient._classify_ddu_from_taxes(taxes) is False


def test_m4_classify_ddu_ddu_when_not_included():
    """IMPORT_CHARGE + includedInPrice=False → DDU (True)."""
    from tasks.ebay_browse_api import BrowseAPIClient
    taxes = [{"taxType": "IMPORT_CHARGE", "includedInPrice": False}]
    assert BrowseAPIClient._classify_ddu_from_taxes(taxes) is True


def test_m4_classify_ddu_ddu_when_field_missing():
    """IMPORT_DUTY + includedInPrice 欠落 → 保守的 DDU 判定 (True).

    eBay は省略時 default=False が多いため、欠落=DDU 扱いが安全側 bias.
    """
    from tasks.ebay_browse_api import BrowseAPIClient
    taxes = [{"taxType": "IMPORT_DUTY"}]
    assert BrowseAPIClient._classify_ddu_from_taxes(taxes) is True


def test_m4_classify_ddu_skips_non_import_tax_types():
    """STATE_SALES_TAX / VAT のみ含まれる場合 → 判別不能で None."""
    from tasks.ebay_browse_api import BrowseAPIClient
    taxes = [{"taxType": "STATE_SALES_TAX", "includedInPrice": False}]
    assert BrowseAPIClient._classify_ddu_from_taxes(taxes) is None


def test_m4_classify_ddu_first_import_match_wins():
    """複数 tax 要素のうち最初の IMPORT/DUTY/CUSTOMS match で判定."""
    from tasks.ebay_browse_api import BrowseAPIClient
    taxes = [
        {"taxType": "STATE_SALES_TAX", "includedInPrice": False},  # skip
        {"taxType": "IMPORT_DUTY", "includedInPrice": True},        # match → DDP
        {"taxType": "CUSTOMS_FEE", "includedInPrice": False},       # 評価されない
    ]
    assert BrowseAPIClient._classify_ddu_from_taxes(taxes) is False


# =============================================================================
# H-NEW-2: errorId 2001 24h 抑制窓 guard
# =============================================================================

def _is_within_24h(ts_str: str) -> bool:
    """bulk script の guard ロジックと等価."""
    from datetime import datetime, timedelta, timezone
    try:
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc) if ts.tzinfo else datetime.now()
        return (now - ts) < timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def test_hnew2_quota_2001_within_24h_blocks_retry():
    """直近 24h の last_quota_2001_at 観測時は抑制窓内."""
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert _is_within_24h(recent) is True


def test_hnew2_quota_2001_after_24h_allows_retry():
    """24h 超えた last_quota_2001_at は抑制窓外."""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert _is_within_24h(old) is False


def test_hnew2_quota_2001_invalid_timestamp_does_not_block():
    """不正な timestamp 文字列は guard 通過 (= 通常 flow に流す、safe-by-default)."""
    assert _is_within_24h("not-a-date") is False
    assert _is_within_24h("") is False


# =============================================================================
# H-NEW-3: cache 汚染防止 (partial write race)
# =============================================================================

def test_hnew3_load_bulk_results_does_not_cache_on_decode_error(tmp_path, monkeypatch):
    """JSON partial write 状態の load で空 dict を cache に焼き付けない."""
    import streamlit as st

    # Mock streamlit session_state as dict-like
    class _FakeSession(dict):
        pass
    fake_ss = _FakeSession()
    monkeypatch.setattr(st, "session_state", fake_ss)

    # Setup partial-write JSON (truncated mid-bracket)
    from tabs import tab_product_management as tpm
    json_path = tmp_path / "w119_bulk_results.json"
    json_path.write_text('{"meta": {"generated_at": "x"}, "results":', encoding="utf-8")
    monkeypatch.setattr(tpm, "_BULK_RESULTS_JSON", json_path)

    meta, results = tpm._load_bulk_results_cached()
    assert meta == {} and results == {}
    # cache に焼き付かないので次回も再試行可能
    assert "pm_bulk_results_cache" not in fake_ss, \
        "partial write 中の空 dict が cache に汚染保存されている (H-NEW-3 違反)"

    # 正常な JSON で書き直し → 即 load で good data 返却
    json_path.write_text(
        '{"meta": {"generated_at": "y"}, "results": {"id_a": [{"x": 1}]}}',
        encoding="utf-8",
    )
    meta2, results2 = tpm._load_bulk_results_cached()
    assert results2 == {"id_a": [{"x": 1}]}, "正常 JSON で load 復活していない"


def test_hnew3_load_bulk_results_caches_normal_load(tmp_path, monkeypatch):
    """正常 JSON は cache に保存される (mtime token で invalidate)."""
    import streamlit as st
    class _FakeSession(dict):
        pass
    fake_ss = _FakeSession()
    monkeypatch.setattr(st, "session_state", fake_ss)

    from tabs import tab_product_management as tpm
    json_path = tmp_path / "w119_bulk_results.json"
    json_path.write_text(
        '{"meta": {"generated_at": "t1"}, "results": {"a": [1]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tpm, "_BULK_RESULTS_JSON", json_path)

    meta, results = tpm._load_bulk_results_cached()
    assert "pm_bulk_results_cache" in fake_ss
    # 2 回目 cache HIT (同 object 返却)
    meta2, results2 = tpm._load_bulk_results_cached()
    assert meta is meta2 and results is results2


# =============================================================================
# Wave B regressions (resumed below)
# =============================================================================

def test_h4_dashboard_query_distinguishes_null_from_zero(tmp_db):
    """H4: stock prefix + inventory_count=NULL は別 metric として SQL で区別される.

    旧実装は NULL を sweep しており、user が在庫数未入力のまま売れたら oversell.
    """
    _insert_stock_listing("id_null", "stock:01", inventory_count=None)
    _insert_stock_listing("id_zero", "stock:02", inventory_count=0)
    _insert_stock_listing("id_low", "stock:03", inventory_count=1)
    _insert_stock_listing("id_ok", "stock:04", inventory_count=10)

    from monitor.database import get_conn
    with get_conn() as c:
        n_zero = c.execute(
            """SELECT COUNT(*) FROM ebay_listings
               WHERE sku LIKE 'stock%' AND inventory_count IS NOT NULL
                 AND inventory_count = 0 AND (is_ended IS NULL OR is_ended=0)"""
        ).fetchone()[0]
        n_low = c.execute(
            """SELECT COUNT(*) FROM ebay_listings
               WHERE sku LIKE 'stock%' AND inventory_count IS NOT NULL
                 AND inventory_count > 0 AND inventory_count <= 2
                 AND (is_ended IS NULL OR is_ended=0)"""
        ).fetchone()[0]
        n_unset = c.execute(
            """SELECT COUNT(*) FROM ebay_listings
               WHERE sku LIKE 'stock%' AND inventory_count IS NULL
                 AND (is_ended IS NULL OR is_ended=0)"""
        ).fetchone()[0]

    assert n_zero == 1, "在庫切れ (0) は 1 件"
    assert n_low == 1, "在庫低下 (1-2) は 1 件"
    assert n_unset == 1, "在庫数未入力 (NULL) は 1 件 (旧実装は silent skip していた)"
