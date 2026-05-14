"""W98 最安値チェック helpers の回帰テスト.

検証対象:
- get_listing_market_displays: 4 layer 優先度 + json_each path で大量 IDs 動作 (H5)
- refresh_competitor_pricing: ループ内 connection 開閉なし (H3)
- compute_breakeven_price_usd: KeyError/RuntimeError raise (H7)
- update_listing_breakeven: 想定外例外を warning ログ + None 戻り (H7)
- fetch_supplier_purchase_yen: 上書き時 logger.warning, 30%以上で error (H-A2)
- upsert_listing_competitors: 再 active 化で旧 owner rule デフォルト化 (H2)
- 仕入価格 0 と None の区別 (H6)
"""
from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(conn, eid, sku="ebay_test_001", primary_market=None,
                    purchase_yen=None, weight_g=1000, source_url=None):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, primary_market, "
        "purchase_yen, weight_g, source_url, is_ended) "
        "VALUES (?, ?, 'test', ?, ?, ?, ?, 0)",
        (eid, sku, primary_market, purchase_yen, weight_g, source_url)
    )


# ─────────────────────────────────
# H5: get_listing_market_displays
# ─────────────────────────────────

def test_get_listing_market_displays_priority_4_layers(tmp_db):
    """final > proposed > analysis > listing の優先度"""
    from monitor.database import get_conn
    from monitor.lowest_price import get_listing_market_displays

    with get_conn() as c:
        # listing A: ebay_listings.primary_market のみ
        _insert_listing(c, "200000000001", primary_market="unknown")
        # listing B: + market_analysis (newer)
        _insert_listing(c, "200000000002", primary_market="unknown")
        c.execute(
            "INSERT INTO market_analysis (ebay_item_id, sku, day_range, total_sold, "
            "us_count, non_us_count, total_sellers, primary_market, scraped_at, source) "
            "VALUES (?, 'test', 90, 10, 5, 5, 5, 'mixed_global', '2026-05-01', 'test')",
            ("200000000002",)
        )
        # listing C: + pending_market_changes
        _insert_listing(c, "200000000003", primary_market="unknown")
        c.execute(
            "INSERT INTO market_analysis (ebay_item_id, sku, day_range, total_sold, "
            "us_count, non_us_count, total_sellers, primary_market, scraped_at, source) "
            "VALUES (?, 'test', 90, 10, 5, 5, 5, 'mixed_global', '2026-05-01', 'test')",
            ("200000000003",)
        )
        c.execute(
            "INSERT INTO pending_market_changes (ebay_item_id, sku, proposed_market, "
            "proposed_at, market_analysis_id) VALUES (?, 'test', 'global_only', '2026-05-01', 0)",
            ("200000000003",)
        )
        # listing D: + market_strategy_decisions (final / approved)
        _insert_listing(c, "200000000004", primary_market="unknown")
        c.execute(
            "INSERT INTO pending_market_changes (ebay_item_id, sku, proposed_market, "
            "proposed_at, market_analysis_id) VALUES (?, 'test', 'global_only', '2026-05-01', 0)",
            ("200000000004",)
        )
        c.execute(
            "INSERT INTO market_strategy_decisions (sku, ebay_item_id, final_market, "
            "action, decided_at) VALUES ('test', ?, 'US_only', 'approved', '2026-05-02')",
            ("200000000004",)
        )

    result = get_listing_market_displays([
        "200000000001", "200000000002", "200000000003", "200000000004"
    ])
    assert result["200000000001"] == "unknown"      # Layer 4
    assert result["200000000002"] == "mixed_global"  # Layer 3
    assert result["200000000003"] == "global_only"   # Layer 2
    assert result["200000000004"] == "US_only"       # Layer 1 (highest)


def test_get_listing_market_displays_handles_large_id_set(tmp_db):
    """H5 regression: 1500 件 IDs で json_each path が SQLite placeholder limit を超えない"""
    from monitor.database import get_conn
    from monitor.lowest_price import get_listing_market_displays

    ids = [f"30{i:010d}" for i in range(1500)]
    with get_conn() as c:
        for eid in ids:
            _insert_listing(c, eid, primary_market="unknown")

    result = get_listing_market_displays(ids)
    assert len(result) == 1500
    assert all(v == "unknown" for v in result.values())


def test_get_listing_market_displays_empty_returns_empty(tmp_db):
    from monitor.lowest_price import get_listing_market_displays
    assert get_listing_market_displays([]) == {}


# ─────────────────────────────────
# H7: compute / update breakeven
# ─────────────────────────────────

def test_compute_breakeven_returns_none_on_invalid_input():
    """purchase_yen=0 or weight_g=0 で None"""
    from monitor.lowest_price import compute_breakeven_price_usd
    settings = {}  # corrupt settings (用無し、early return される)
    assert compute_breakeven_price_usd(0, 1000, 0, 0, 0, settings) is None
    assert compute_breakeven_price_usd(1000, 0, 0, 0, 0, settings) is None


def test_compute_breakeven_raises_runtime_error_on_corrupt_settings():
    """H7 regression: 設定 dict 不正 → 上限値計算失敗 → RuntimeError raise"""
    from monitor.lowest_price import compute_breakeven_price_usd
    with pytest.raises(RuntimeError):
        compute_breakeven_price_usd(
            purchase_yen=10000, weight_g=1000, length_cm=0, width_cm=0, height_cm=0,
            settings={}  # 必須 key 全欠落
        )


def test_update_listing_breakeven_logs_warning_on_corrupt_settings(tmp_db, caplog):
    """H7 regression: RuntimeError は warning ログ + DB に NULL 保存"""
    from monitor.database import get_conn
    from monitor.lowest_price import update_listing_breakeven

    with get_conn() as c:
        _insert_listing(c, "200000000010", purchase_yen=10000, weight_g=1000)

    with caplog.at_level(logging.WARNING, logger="monitor.lowest_price"):
        result = update_listing_breakeven("200000000010", {})  # corrupt settings
    assert result is None
    assert any("breakeven calc failed" in r.message for r in caplog.records)

    with get_conn() as c:
        row = c.execute(
            "SELECT lp_breakeven_usd FROM ebay_listings WHERE ebay_item_id=?",
            ("200000000010",)
        ).fetchone()
    assert row[0] is None


# ─────────────────────────────────
# H3: refresh_competitor_pricing
# ─────────────────────────────────

def test_refresh_competitor_pricing_no_competitors(tmp_db):
    """登録ライバル無し → 0/0 を返す"""
    from monitor.database import get_conn
    from monitor.lowest_price import refresh_competitor_pricing

    with get_conn() as c:
        _insert_listing(c, "200000000020")

    result = refresh_competitor_pricing("200000000020", {})
    assert result == {'fetched': 0, 'failed': 0}


def test_refresh_competitor_pricing_no_credentials_marks_all_failed(tmp_db):
    """Browse API credentials 不在 → 全件 failed (silent skip 防止)"""
    from monitor.database import get_conn
    from monitor.lowest_price import refresh_competitor_pricing, upsert_listing_competitors

    with get_conn() as c:
        _insert_listing(c, "200000000021")

    upsert_listing_competitors("200000000021", ["285999999001", "285999999002"])

    result = refresh_competitor_pricing("200000000021", {})  # credentials なし
    assert result == {'fetched': 0, 'failed': 2}


# ─────────────────────────────────
# H2: upsert re-activation rule reset
# ─────────────────────────────────

def test_upsert_reactivation_resets_old_owner_rule(tmp_db):
    """H2 regression: 別商品で再 active 化したライバルは price_rule デフォルト化"""
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    with get_conn() as c:
        _insert_listing(c, "200000000030")
        _insert_listing(c, "200000000031")

    # A 商品で登録 + カスタム rule
    upsert_listing_competitors("200000000030", ["285999999100"])
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET price_rule='competitor - 0.99', "
            "min_price=99.0, max_discount=99.0 WHERE competitor_item_id='285999999100'"
        )

    # A から削除 (inactive)
    upsert_listing_competitors("200000000030", [])

    # B 商品で再登録
    upsert_listing_competitors("200000000031", ["285999999100"])

    with get_conn() as c:
        row = c.execute(
            "SELECT our_item_id, price_rule, min_price, max_discount FROM competitor_products "
            "WHERE competitor_item_id='285999999100'"
        ).fetchone()
    assert row[0] == "200000000031"
    assert row[1] == "competitor - 0.01"
    assert row[2] == 0.0
    assert row[3] == 10.0


# ─────────────────────────────────
# H-A2: fetch_supplier_purchase_yen 上書き warning
# ─────────────────────────────────

def test_fetch_supplier_overwrite_logs_warning(tmp_db, caplog, monkeypatch):
    """H-A2 regression: 既存値の上書き時 logger.warning"""
    from monitor.database import get_conn
    from monitor.lowest_price import fetch_supplier_purchase_yen

    with get_conn() as c:
        _insert_listing(c, "200000000040", sku="ebayme_m12345", purchase_yen=10000,
                        source_url="https://jp.mercari.com/item/m12345")

    # scrape の stub: ¥11000 を返す (10% 上昇)
    class _StubScrape:
        price_jpy = 11000

    def _stub(url, timeout_sec=15):
        return _StubScrape()

    monkeypatch.setattr("monitor.supplier_scraper.scrape_supplier_url", _stub)

    with caplog.at_level(logging.WARNING, logger="monitor.lowest_price"):
        result = fetch_supplier_purchase_yen("200000000040")
    assert result == 11000
    # log message format: "purchase_yen overwrite: ... ¥10,000 → ¥11,000 (10.0%)"
    assert any("purchase_yen overwrite" in r.message and "10,000" in r.message
               and "11,000" in r.message
               for r in caplog.records)


def test_fetch_supplier_large_overwrite_logs_error(tmp_db, caplog, monkeypatch):
    """H-A2 regression: 30% 以上の変動で ERROR ログ"""
    from monitor.database import get_conn
    from monitor.lowest_price import fetch_supplier_purchase_yen

    with get_conn() as c:
        _insert_listing(c, "200000000041", sku="ebayme_m99999", purchase_yen=10000,
                        source_url="https://jp.mercari.com/item/m99999")

    class _StubScrape:
        price_jpy = 5000  # 50% 下落 (≥30%)

    def _stub(url, timeout_sec=15):
        return _StubScrape()

    monkeypatch.setattr("monitor.supplier_scraper.scrape_supplier_url", _stub)

    with caplog.at_level(logging.ERROR, logger="monitor.lowest_price"):
        result = fetch_supplier_purchase_yen("200000000041")
    assert result == 5000
    assert any("LARGE OVERWRITE" in r.message for r in caplog.records)


def test_fetch_supplier_skip_for_in_stock_sku(tmp_db, monkeypatch):
    """有在庫 (sku='stock**') では scrape 走らず None"""
    from monitor.database import get_conn
    from monitor.lowest_price import fetch_supplier_purchase_yen

    with get_conn() as c:
        _insert_listing(c, "200000000042", sku="stock:01",
                        source_url="https://jp.mercari.com/item/m12345")

    # scrape されないことを確認するため、呼ばれたら例外で fail
    def _should_not_be_called(*a, **kw):
        raise AssertionError("scrape は在庫品で呼ばれてはいけない")

    monkeypatch.setattr("monitor.supplier_scraper.scrape_supplier_url",
                        _should_not_be_called)

    assert fetch_supplier_purchase_yen("200000000042") is None
