"""回帰テスト (2026-06-20): 依頼ボード#32 — stock SKU かつ qty=0 を仕入先候補スイープ対象に追加.

不変条件:
1. stock+qty=0+active の listing が _fetch_stock_zero_targets で返る。
2. 無在庫 (ebay* SKU) の _fetch_sweep_targets は従来通り stock を除外する。
3. run_supplier_sweep が両対象を合算し run_supplier_candidate_search を呼ぶ。
4. match_score < 60 の候補は add_supplier_candidate に渡らない (supplier-matching-rules 準拠)。
5. last_supplier_search_at が設定済の stock listing は throttle 対象になりスキップされる。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# 1. _fetch_stock_zero_targets: stock+qty=0 が対象に含まれる
# ---------------------------------------------------------------------------

def test_fetch_stock_zero_targets_returns_stock_listings(tmp_path, monkeypatch):
    """stock+qty=0+active の listing が fetch_stock_zero_targets に返る。"""
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fetch_stock.db")
    db.init_db()

    with db.get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended) "
            "VALUES (?,?,?,?,?)",
            ("EID_STOCK1", "stock:01", "Test Product A", 0, 0),
        )
        # qty>0 は対象外
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended) "
            "VALUES (?,?,?,?,?)",
            ("EID_STOCK2", "stock:01", "Test Product B", 2, 0),
        )
        # is_ended=1 は対象外
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended) "
            "VALUES (?,?,?,?,?)",
            ("EID_STOCK3", "stock:01", "Test Product C", 0, 1),
        )
        # ebay* SKU は対象外 (無在庫の既存経路)
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended, "
            "source_out_of_stock_since, source_status) "
            "VALUES (?,?,?,?,?,datetime('now', '-5 days'),?)",
            ("EID_EBAY1", "ebayyh_p1234", "Ebay Product", 0, 0, "在庫無"),
        )

    from tasks.task_supplier_sweep import _fetch_stock_zero_targets
    monkeypatch.setattr("tasks.task_supplier_sweep.get_conn", db.get_conn)
    results = _fetch_stock_zero_targets(skip_if_searched_within_days=7, limit=10)

    eids = [eid for eid, _ in results]
    assert "EID_STOCK1" in eids, "stock+qty=0 が対象に入っていない"
    assert "EID_STOCK2" not in eids, "qty>0 が誤って対象に入っている"
    assert "EID_STOCK3" not in eids, "is_ended=1 が誤って対象に入っている"
    assert "EID_EBAY1" not in eids, "ebay* SKU が stock 経路に混入している"


# ---------------------------------------------------------------------------
# 2. _fetch_sweep_targets: stock* SKU は従来通り除外される (無在庫経路不変)
# ---------------------------------------------------------------------------

def test_fetch_sweep_targets_still_excludes_stock_sku(tmp_path, monkeypatch):
    """既存の _fetch_sweep_targets が stock* SKU を引き続き除外する (回帰)。"""
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fetch_oos.db")
    db.init_db()

    with db.get_conn() as c:
        # 無在庫 ebay* SKU (対象であるべき)
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended, "
            "source_out_of_stock_since, source_status) "
            "VALUES (?,?,?,?,?,datetime('now', '-5 days'),?)",
            ("EID_EBAY_OK", "ebayyh_p5678", "Ebay OOS Product", 1, 0, "在庫無"),
        )
        # stock SKU + OOS (対象外であるべき — stock は別経路)
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended, "
            "source_out_of_stock_since, source_status) "
            "VALUES (?,?,?,?,?,datetime('now', '-5 days'),?)",
            ("EID_STOCK_OOS", "stock:01", "Stock OOS Product", 0, 0, "在庫無"),
        )

    from tasks.task_supplier_sweep import _fetch_sweep_targets
    monkeypatch.setattr("tasks.task_supplier_sweep.get_conn", db.get_conn)
    results = _fetch_sweep_targets(
        oos_days_threshold=3, skip_if_searched_within_days=7, limit=10
    )
    eids = [eid for eid, _ in results]
    assert "EID_EBAY_OK" in eids, "無在庫 ebay* が対象から外れた (回帰)"
    assert "EID_STOCK_OOS" not in eids, "stock* が _fetch_sweep_targets に混入した (回帰)"


# ---------------------------------------------------------------------------
# 3. throttle: last_supplier_search_at 設定済の stock listing はスキップ
# ---------------------------------------------------------------------------

def test_fetch_stock_zero_targets_throttle_by_last_search(tmp_path, monkeypatch):
    """最近探索済みの stock listing は throttle でスキップされる。"""
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "throttle.db")
    db.init_db()

    with db.get_conn() as c:
        # 未探索 → 対象
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, is_ended) "
            "VALUES (?,?,?,?,?)",
            ("EID_NEW", "stock:01", "New Product", 0, 0),
        )
        # 最近探索済 → throttle でスキップ
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended, last_supplier_search_at) "
            "VALUES (?,?,?,?,?,datetime('now', '-1 days'))",
            ("EID_RECENT", "stock:01", "Recent Searched", 0, 0),
        )
        # 探索期限切れ (8日前) → 対象
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended, last_supplier_search_at) "
            "VALUES (?,?,?,?,?,datetime('now', '-8 days'))",
            ("EID_OLD", "stock:01", "Old Searched", 0, 0),
        )

    from tasks.task_supplier_sweep import _fetch_stock_zero_targets
    monkeypatch.setattr("tasks.task_supplier_sweep.get_conn", db.get_conn)
    results = _fetch_stock_zero_targets(skip_if_searched_within_days=7, limit=10)
    eids = [eid for eid, _ in results]
    assert "EID_NEW" in eids, "未探索が対象に入っていない"
    assert "EID_RECENT" not in eids, "直近探索済みが throttle されていない"
    assert "EID_OLD" in eids, "期限切れ (8日前) が対象に入っていない"


# ---------------------------------------------------------------------------
# 4. run_supplier_sweep が両経路を合算し search を呼ぶ
# ---------------------------------------------------------------------------

def test_run_supplier_sweep_calls_search_for_stock_zero(monkeypatch):
    """run_supplier_sweep が stock+qty=0 対象にも run_supplier_candidate_search を呼ぶ。"""
    import tasks.task_supplier_sweep as sweep_mod

    oos_target = ("EID_OOS1", "ebayyh_p1111")
    stock_target = ("EID_STOCK1", "stock:01")

    monkeypatch.setattr(sweep_mod, "_fetch_sweep_targets",
                        lambda oos, skip, lim: [oos_target])
    monkeypatch.setattr(sweep_mod, "_fetch_stock_zero_targets",
                        lambda skip, lim: [stock_target])
    monkeypatch.setattr(sweep_mod, "_mark_stock_search_attempt", lambda eid: None)

    calls_log = []

    def _mock_search(ebay_item_id, sku, config, discovered_via=""):
        calls_log.append((ebay_item_id, sku, discovered_via))
        return {"success": True, "found": 1, "persisted": 1, "message": "ok"}

    monkeypatch.setattr(sweep_mod, "run_supplier_candidate_search", _mock_search)
    monkeypatch.setattr(sweep_mod, "time", MagicMock(sleep=lambda s: None))

    cfg = {"tasks_enabled": {"supplier_sweep": {"sleep_between_skus_sec": 0}}}
    result = sweep_mod.run_supplier_sweep(cfg)

    assert result["success"] is True
    assert result["processed"] == 2

    eids_called = [c[0] for c in calls_log]
    assert "EID_OOS1" in eids_called, "無在庫が呼ばれていない"
    assert "EID_STOCK1" in eids_called, "stock在庫0が呼ばれていない"

    # discovered_via のラベル確認
    oos_via = next(c[2] for c in calls_log if c[0] == "EID_OOS1")
    stock_via = next(c[2] for c in calls_log if c[0] == "EID_STOCK1")
    assert oos_via == "pattern_2_batch", f"無在庫のdiscovered_via誤り: {oos_via}"
    assert stock_via == "pattern_2_stock_zero", f"stock在庫0のdiscovered_via誤り: {stock_via}"


# ---------------------------------------------------------------------------
# 5. match_score < 60 は add_supplier_candidate に渡らない (supplier-matching-rules)
# ---------------------------------------------------------------------------

def test_low_score_candidate_not_persisted(monkeypatch):
    """match_score=50 (<60) の候補が add_supplier_candidate に渡らない。"""
    import tasks.task_supplier_candidate_search as t

    listing = {
        "ebay_item_id": "EID_STOCK_LOW",
        "sku": "stock:01",
        "title": "Test Low Score Product",
        "source_url": None,
        "search_keyword": "Test Low Score",
        "current_price": 50.0,
        "weight_g": 500,
        "length_cm": 10, "width_cm": 10, "height_cm": 10,
        "category_id": 0,
    }

    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(t, "load_settings", lambda: {"exchange_rate": 157.0})
    monkeypatch.setattr(t, "get_ebay_image_url", lambda eid: None)
    monkeypatch.setattr(t, "check_candidate_availability",
                        lambda url, **_: {"status": "available"})
    monkeypatch.setattr(t, "get_recent_candidate_evaluation",
                        lambda eid, url: None)
    monkeypatch.setattr(t, "record_candidate_evaluation",
                        lambda *a, **kw: None)

    from tasks.task_supplier_candidate_search import CandidateHit, ScoredCandidate

    low_hit = CandidateHit(
        source_platform="mercari",
        url="https://mercari.com/item/low1",
        price_jpy=3000,
        title="unrelated product",
    )
    monkeypatch.setattr(t, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5:
                        [low_hit] if plat == "mercari" else [])

    monkeypatch.setattr(t, "evaluate_candidate_with_claude",
                        lambda h, ebay_title, **_kw:
                        ScoredCandidate(hit=h, match_score=50,
                                        match_reasoning="low similarity"))

    saved = []
    monkeypatch.setattr(t, "add_supplier_candidate",
                        lambda **kw: saved.append(kw) or 1)

    result = t.run_supplier_candidate_search(
        ebay_item_id="EID_STOCK_LOW",
        sku="stock:01",
        config={},
        discovered_via="pattern_2_stock_zero",
    )

    assert len(saved) == 0, f"match_score=50 (<60) が保存された: {saved}"
    assert result["persisted"] == 0


# ---------------------------------------------------------------------------
# 6. finally throttle: 検索が例外を投げても last_supplier_search_at が更新される (Q0)
#    (code-reviewer #32 HIGH-2 のテストギャップ補完。throttle が効かないと毎朝
#     再探索ループ = Anthropic 評価の課金反復になる)
# ---------------------------------------------------------------------------

def test_mark_stock_search_attempt_called_even_on_search_failure(monkeypatch):
    """run_supplier_candidate_search が例外を投げても finally で throttle マーカーが更新される。"""
    import tasks.task_supplier_sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "_fetch_sweep_targets", lambda oos, skip, lim: [])
    monkeypatch.setattr(sweep_mod, "_fetch_stock_zero_targets",
                        lambda skip, lim: [("EID_STOCK_X", "stock:01")])
    monkeypatch.setattr(sweep_mod, "time", MagicMock(sleep=lambda s: None))

    def _boom(ebay_item_id, sku, config, discovered_via=""):
        raise RuntimeError("search exploded")
    monkeypatch.setattr(sweep_mod, "run_supplier_candidate_search", _boom)

    marked: list[str] = []
    monkeypatch.setattr(sweep_mod, "_mark_stock_search_attempt",
                        lambda eid: marked.append(eid))

    cfg = {"tasks_enabled": {"supplier_sweep": {"sleep_between_skus_sec": 0}}}
    result = sweep_mod.run_supplier_sweep(cfg)

    assert "EID_STOCK_X" in marked, \
        "例外時に throttle マーカーが更新されていない (毎朝再探索 = 課金反復)"
    assert result["errors"] == 1, "例外が errors に計上されていない"
