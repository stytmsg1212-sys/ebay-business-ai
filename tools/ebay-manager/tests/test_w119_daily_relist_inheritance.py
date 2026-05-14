"""W119 daily_relist の継承カラム拡張 regression test.

出典: 2026-05-11 W119 ふりかえりで silent skip 発見.
旧実装は weight/size/source のみ継承し、以下を毎回 reset していた:
  - search_keyword / search_keyword_source / search_keyword_generated_at (W119)
  - purchase_yen / lp_min_price / lp_breakeven_usd (W98 最安値)
  - primary_market / us_buyer_ratio / market_analysis_at / market_sample_size (W110 市場)

加えて competitor_products.our_item_id の追従なし → W183 競合登録が relist 時に孤立し
値下げ pipeline が機能停止する金銭損失リスクがあった.

本 test は `inherit_listing_on_relist()` が全継承列 + 全関連テーブル更新を 1 トランザクション
で正しく実施することを保証する.
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


def _insert_old_listing_full(ebay_item_id: str) -> dict:
    """継承対象列を全て埋めた OLD listing を挿入. 戻り値は期待値 dict."""
    from monitor.database import get_conn

    expected = {
        "weight_g": 500,
        "weight_source": "haiku_estimate",
        "weight_confidence": "high",
        "weight_estimated_at": "2026-05-01 12:00:00",
        "length_cm": 20.0,
        "width_cm": 15.0,
        "height_cm": 10.0,
        "includes": "Power cable / Manual",
        "warranty": "30 days",
        "source": "yahoo_auction",
        "source_url": "https://example.com/source",
        "classification": "audio_av",
        "purchase_yen": 3500.0,
        "search_keyword": "Maxell MXCP-P100",
        "search_keyword_source": "opus_batch",
        "search_keyword_generated_at": "2026-05-10 23:30:00",
        "lp_min_price": 89.99,
        "lp_breakeven_usd": 72.50,
        "primary_market": "US_only",
        "us_buyer_ratio": 0.85,
        "market_analysis_at": "2026-05-09 02:30:00",
        "market_sample_size": 47,
        "quantity_ebay": 2,
    }
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings (
                ebay_item_id, sku, title, is_ended, quantity_ebay,
                weight_g, weight_source, weight_confidence, weight_estimated_at,
                length_cm, width_cm, height_cm, includes, warranty,
                source, source_url, classification, purchase_yen,
                search_keyword, search_keyword_source, search_keyword_generated_at,
                lp_min_price, lp_breakeven_usd,
                primary_market, us_buyer_ratio, market_analysis_at, market_sample_size
            ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ebay_item_id, "stock:01", "Maxell MXCP-P100 Black",
                expected["quantity_ebay"],
                expected["weight_g"], expected["weight_source"],
                expected["weight_confidence"], expected["weight_estimated_at"],
                expected["length_cm"], expected["width_cm"], expected["height_cm"],
                expected["includes"], expected["warranty"],
                expected["source"], expected["source_url"], expected["classification"],
                expected["purchase_yen"],
                expected["search_keyword"], expected["search_keyword_source"],
                expected["search_keyword_generated_at"],
                expected["lp_min_price"], expected["lp_breakeven_usd"],
                expected["primary_market"], expected["us_buyer_ratio"],
                expected["market_analysis_at"], expected["market_sample_size"],
            ),
        )
    return expected


def test_inherit_all_w119_columns(tmp_db):
    """W119 検索ワード 3 列が新 ItemID に継承される."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_001")
    result = inherit_listing_on_relist(
        old_item_id="old_item_001",
        new_item_id="new_item_001",
        sku="stock:01",
        title="Maxell MXCP-P100 Black",
        current_price=145.0,
    )
    assert result["inherited_columns"] == 1

    with get_conn() as c:
        new = dict(c.execute(
            "SELECT search_keyword, search_keyword_source, search_keyword_generated_at "
            "FROM ebay_listings WHERE ebay_item_id=?",
            ("new_item_001",),
        ).fetchone())
    assert new["search_keyword"] == "Maxell MXCP-P100"
    assert new["search_keyword_source"] == "opus_batch"
    assert new["search_keyword_generated_at"] == "2026-05-10 23:30:00"


def test_inherit_lowest_price_columns_preserves_user_floor(tmp_db):
    """W98 最安値設定 (purchase_yen / lp_min_price / lp_breakeven_usd) が新 ItemID に継承される.

    最も重要なのは lp_min_price (user 手動設定の floor).
    継承漏れだと W183 が breakeven (より低い) を floor 採用 → 想定外安値値下げ = 金銭損失.
    """
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_002")
    inherit_listing_on_relist("old_item_002", "new_item_002", "stock:01",
                              "Maxell MXCP-P100 Black", 145.0)

    with get_conn() as c:
        new = dict(c.execute(
            "SELECT purchase_yen, lp_min_price, lp_breakeven_usd "
            "FROM ebay_listings WHERE ebay_item_id=?",
            ("new_item_002",),
        ).fetchone())
    assert new["purchase_yen"] == 3500.0
    assert new["lp_min_price"] == 89.99, (
        "user 設定の lp_min_price が継承されていない. "
        "W183 floor 算定で breakeven 採用に倒れ金銭損失リスク."
    )
    assert new["lp_breakeven_usd"] == 72.50


def test_inherit_w110_market_columns(tmp_db):
    """W110 市場分析結果 (primary_market 等) が新 ItemID に継承される."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_003")
    inherit_listing_on_relist("old_item_003", "new_item_003", "stock:01",
                              "Maxell MXCP-P100 Black", 145.0)

    with get_conn() as c:
        new = dict(c.execute(
            "SELECT primary_market, us_buyer_ratio, market_analysis_at, market_sample_size "
            "FROM ebay_listings WHERE ebay_item_id=?",
            ("new_item_003",),
        ).fetchone())
    assert new["primary_market"] == "US_only"
    assert new["us_buyer_ratio"] == 0.85
    assert new["market_analysis_at"] == "2026-05-09 02:30:00"
    assert new["market_sample_size"] == 47


def test_inherit_physical_attrs_still_works(tmp_db):
    """既存の物理属性継承 (weight/size/source/includes/warranty/classification) が壊れていない."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_004")
    inherit_listing_on_relist("old_item_004", "new_item_004", "stock:01",
                              "Maxell MXCP-P100 Black", 145.0)

    with get_conn() as c:
        new = dict(c.execute(
            "SELECT weight_g, weight_source, length_cm, width_cm, height_cm, "
            "includes, warranty, source, source_url, classification, quantity_ebay "
            "FROM ebay_listings WHERE ebay_item_id=?",
            ("new_item_004",),
        ).fetchone())
    assert new["weight_g"] == 500
    assert new["weight_source"] == "haiku_estimate"
    assert new["length_cm"] == 20.0
    assert new["width_cm"] == 15.0
    assert new["height_cm"] == 10.0
    assert new["includes"] == "Power cable / Manual"
    assert new["warranty"] == "30 days"
    assert new["source"] == "yahoo_auction"
    assert new["source_url"] == "https://example.com/source"
    assert new["classification"] == "audio_av"
    assert new["quantity_ebay"] == 2


def test_inherit_lifecycle_columns_NOT_carried_over(tmp_db):
    """ライフサイクル系 (watch_count / rank / is_ended 等) は OLD と独立して new で初期化."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_005")
    # OLD に watch_count / rank を別途設定
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET watch_count=15, rank='A', is_ended=0 "
            "WHERE ebay_item_id=?",
            ("old_item_005",),
        )

    inherit_listing_on_relist("old_item_005", "new_item_005", "stock:01",
                              "Maxell MXCP-P100 Black", 145.0)

    with get_conn() as c:
        new = dict(c.execute(
            "SELECT watch_count, rank, is_ended FROM ebay_listings WHERE ebay_item_id=?",
            ("new_item_005",),
        ).fetchone())
    # watch_count / rank は INSERT で値指定なし → NULL or default
    assert (new["watch_count"] or 0) == 0
    # rank は DB schema default 'C' (W7-A 「分類不可」相当), OLD の 'A' を継承していないこと
    assert new["rank"] != "A", f"OLD の rank='A' が継承された (継承禁止): {new['rank']!r}"
    # is_ended は新 listing なので NULL or 0 (active)
    assert (new["is_ended"] or 0) == 0


def test_competitor_products_our_item_id_updated(tmp_db):
    """W119 競合登録が新 ItemID に追従する (W183 値下げ pipeline 継続性).

    Round 1 silent skip: relist 時に competitor_products.our_item_id が古いままになり、
    W183 が「該当 listing の競合 0 件」と判定して値下げが止まる事故防止.
    """
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_006")
    # competitor_products に旧 listing の競合を 2 件登録
    with get_conn() as c:
        c.execute(
            """INSERT INTO competitor_products (our_item_id, competitor_item_id, is_active)
               VALUES (?, ?, 1), (?, ?, 1), (?, ?, 0)""",
            ("old_item_006", "competitor_a",
             "old_item_006", "competitor_b",
             "old_item_006", "competitor_c_inactive"),
        )

    result = inherit_listing_on_relist("old_item_006", "new_item_006", "stock:01",
                                       "Maxell MXCP-P100 Black", 145.0)
    assert result["competitor_rows"] == 2, "active 競合 2 件のみ追従されるべき"

    with get_conn() as c:
        active_new = c.execute(
            "SELECT competitor_item_id FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1 ORDER BY competitor_item_id",
            ("new_item_006",),
        ).fetchall()
        inactive_old = c.execute(
            "SELECT competitor_item_id FROM competitor_products "
            "WHERE our_item_id=? AND is_active=0",
            ("old_item_006",),
        ).fetchall()
    assert [r[0] for r in active_new] == ["competitor_a", "competitor_b"]
    # inactive は old のまま残る (履歴として)
    assert [r[0] for r in inactive_old] == ["competitor_c_inactive"]


def test_relist_history_recorded(tmp_db):
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_007")
    inherit_listing_on_relist("old_item_007", "new_item_007", "stock:01",
                              "Maxell MXCP-P100 Black", 145.0, end_reason="Incorrect")

    with get_conn() as c:
        history = c.execute(
            "SELECT old_item_id, new_item_id, sku, end_reason, success "
            "FROM relist_history WHERE old_item_id=?",
            ("old_item_007",),
        ).fetchone()
    assert history is not None
    assert history[0] == "old_item_007"
    assert history[1] == "new_item_007"
    assert history[2] == "stock:01"
    assert history[3] == "Incorrect"
    assert history[4] == 1


def test_supplier_candidates_only_pending_accepted_followed(tmp_db):
    """supplier_candidates: pending/accepted のみ追従、rejected/applied は履歴で触らない."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_008")
    with get_conn() as c:
        c.execute(
            """INSERT INTO supplier_candidates
               (ebay_item_id, sku, candidate_url, candidate_title, status)
               VALUES
               (?, ?, ?, ?, 'pending'),
               (?, ?, ?, ?, 'accepted'),
               (?, ?, ?, ?, 'rejected'),
               (?, ?, ?, ?, 'applied')""",
            (
                "old_item_008", "stock:01", "u1", "t1",
                "old_item_008", "stock:01", "u2", "t2",
                "old_item_008", "stock:01", "u3", "t3",
                "old_item_008", "stock:01", "u4", "t4",
            ),
        )

    result = inherit_listing_on_relist("old_item_008", "new_item_008", "stock:01",
                                       "Maxell MXCP-P100 Black", 145.0)
    assert result["supplier_rows"] == 2

    with get_conn() as c:
        new_count = c.execute(
            "SELECT COUNT(*) FROM supplier_candidates WHERE ebay_item_id=?",
            ("new_item_008",),
        ).fetchone()[0]
        old_remaining = dict(zip(
            ["pending", "accepted", "rejected", "applied"],
            [c.execute(
                "SELECT COUNT(*) FROM supplier_candidates WHERE ebay_item_id=? AND status=?",
                ("old_item_008", s),
            ).fetchone()[0] for s in ["pending", "accepted", "rejected", "applied"]]
        ))
    assert new_count == 2  # pending + accepted moved
    assert old_remaining["pending"] == 0
    assert old_remaining["accepted"] == 0
    assert old_remaining["rejected"] == 1  # 履歴維持
    assert old_remaining["applied"] == 1  # 履歴維持


def test_relist_history_records_success_per_parameter(tmp_db):
    """H1 fix (2026-05-11 code-reviewer): success=False を明示的に渡すと
    relist_history.success=0 で記録される (誤って 1 で記録される回帰防止).
    """
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    _insert_old_listing_full("old_item_success_param")
    # 通常呼出 (success default True)
    inherit_listing_on_relist("old_item_success_param", "new_item_succ_t",
                              "stock:01", "T", 100.0)
    # 明示 success=False
    _insert_old_listing_full("old_item_success_param_f")
    inherit_listing_on_relist("old_item_success_param_f", "new_item_succ_f",
                              "stock:01", "T", 100.0, success=False)

    with get_conn() as c:
        rows = c.execute(
            "SELECT old_item_id, success FROM relist_history "
            "WHERE old_item_id IN ('old_item_success_param', 'old_item_success_param_f')"
        ).fetchall()
    d = {r[0]: r[1] for r in rows}
    assert d["old_item_success_param"] == 1, "default は success=1"
    assert d["old_item_success_param_f"] == 0, "明示 success=False は 0 で記録"


def test_no_old_data_results_in_safe_defaults(tmp_db):
    """OLD listing が存在しない時、helper は safe defaults で INSERT し続行可能."""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    # OLD listing 無し (異常系)
    result = inherit_listing_on_relist(
        old_item_id="nonexistent",
        new_item_id="new_item_009",
        sku="stock:01",
        title="Test",
        current_price=100.0,
    )

    with get_conn() as c:
        new = c.execute(
            "SELECT search_keyword, lp_min_price, weight_g FROM ebay_listings "
            "WHERE ebay_item_id=?", ("new_item_009",),
        ).fetchone()
    # OLD が見つからなくても new は作成され、継承列は NULL or 0
    assert new is not None
    assert new[0] is None  # search_keyword
    assert new[1] is None  # lp_min_price
    assert new[2] == 0  # weight_g default
