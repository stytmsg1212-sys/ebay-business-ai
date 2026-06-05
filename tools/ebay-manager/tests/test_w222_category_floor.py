#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W222: カテゴリ別 FVF floor 配線のテスト (Phase 1 = 列追加 + 配線、floor は別 apply)。

検証:
  - migration v65 で ebay_listings.category_id 追加 + 冪等
  - upsert_ebay_listing が category_id を COALESCE 保存 (None は既存維持)
  - update_listing_breakeven が listing の category_id を compute に渡す (NULL は 58248)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_migration_v65_category_id_and_idempotent():
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    init_db()
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert "category_id" in cols
    assert ver >= 65
    # 冪等: 記録後 init_db 再実行でも保持
    upsert_ebay_listing("v65eid", "stock1", category_id=112529)
    init_db()
    with get_conn() as c:
        row = c.execute("SELECT category_id FROM ebay_listings WHERE ebay_item_id='v65eid'").fetchone()
    assert row[0] == 112529


def test_upsert_category_id_coalesce_preserve_on_none():
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    init_db()
    upsert_ebay_listing("cat1", "stock1", title="T", category_id=112529)
    with get_conn() as c:
        assert c.execute("SELECT category_id FROM ebay_listings WHERE ebay_item_id='cat1'").fetchone()[0] == 112529
    # None で再 upsert → 既存維持 (COALESCE)
    upsert_ebay_listing("cat1", "stock1", title="T2", category_id=None)
    with get_conn() as c:
        assert c.execute("SELECT category_id FROM ebay_listings WHERE ebay_item_id='cat1'").fetchone()[0] == 112529
    # 新 category_id で更新
    upsert_ebay_listing("cat1", "stock1", title="T3", category_id=14990)
    with get_conn() as c:
        assert c.execute("SELECT category_id FROM ebay_listings WHERE ebay_item_id='cat1'").fetchone()[0] == 14990


def test_upsert_category_id_zero_treated_as_none():
    """category_id=0 (leaf でない / 抽出失敗) は None 扱いで既存維持。"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    init_db()
    upsert_ebay_listing("cat0", "stock1", category_id=112529)
    upsert_ebay_listing("cat0", "stock1", category_id=0)  # 0 → COALESCE 既存維持
    with get_conn() as c:
        assert c.execute("SELECT category_id FROM ebay_listings WHERE ebay_item_id='cat0'").fetchone()[0] == 112529


def test_update_breakeven_uses_category_when_flag_on(monkeypatch):
    """flag ON で listing の実カテゴリを compute に渡す。"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    import monitor.lowest_price as lp
    init_db()
    upsert_ebay_listing("be1", "stock1", category_id=112529)
    with get_conn() as c:
        c.execute("UPDATE ebay_listings SET purchase_yen=3000, weight_g=300 WHERE ebay_item_id='be1'")

    captured = {}
    monkeypatch.setattr(lp, "compute_breakeven_price_usd",
                        lambda **kw: captured.update(kw) or 42.0)
    lp.update_listing_breakeven("be1", {"exchange_rate": 155.0, "use_category_fvf_floor": True})
    assert captured.get("category_id") == 112529, "flag ON = 実カテゴリを compute に渡す"


def test_update_breakeven_flag_off_uses_58248_despite_category(monkeypatch):
    """⚠️money-direct gate: flag OFF (default) は category_id があっても 58248 固定 (floor 不変)。"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    import monitor.lowest_price as lp
    init_db()
    upsert_ebay_listing("be3", "stock1", category_id=112529)  # 実カテゴリ backfill 済
    with get_conn() as c:
        c.execute("UPDATE ebay_listings SET purchase_yen=3000, weight_g=300 WHERE ebay_item_id='be3'")

    captured = {}
    monkeypatch.setattr(lp, "compute_breakeven_price_usd",
                        lambda **kw: captured.update(kw) or 42.0)
    # flag 無し (default OFF)
    lp.update_listing_breakeven("be3", {"exchange_rate": 155.0})
    assert captured.get("category_id") == 58248, "flag OFF は backfill 済でも 58248 (ゲート)"


def test_update_breakeven_null_category_falls_back_58248(monkeypatch):
    """flag ON + category_id NULL の listing は 58248 fallback。"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing
    import monitor.lowest_price as lp
    init_db()
    upsert_ebay_listing("be2", "stock1")  # category_id 未指定 = NULL
    with get_conn() as c:
        c.execute("UPDATE ebay_listings SET purchase_yen=3000, weight_g=300 WHERE ebay_item_id='be2'")

    captured = {}
    monkeypatch.setattr(lp, "compute_breakeven_price_usd",
                        lambda **kw: captured.update(kw) or 42.0)
    lp.update_listing_breakeven("be2", {"exchange_rate": 155.0, "use_category_fvf_floor": True})
    assert captured.get("category_id") == 58248, "NULL は 58248 fallback (後方互換)"


def test_get_item_details_batch_extracts_category_id(monkeypatch):
    """bulk sync 経路: get_item_details_batch が GetItem の PrimaryCategory を返す。

    これが無いと ebay_sync bulk path の category_id が常に None で daily sync で
    永久 backfill されない (code-reviewer W222 HIGH-1)。
    """
    import monitor.ebay_client as ec

    _xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Ack>Success</Ack>'
        '<Item><WatchCount>5</WatchCount><HitCount>100</HitCount>'
        '<SellingStatus><QuantitySold>2</QuantitySold></SellingStatus>'
        '<PrimaryCategory><CategoryID>112529</CategoryID></PrimaryCategory>'
        '</Item></GetItemResponse>'
    )

    class _Resp:
        text = _xml
        def raise_for_status(self):
            return None

    monkeypatch.setattr(ec, "_resolve_active_token", lambda t: t)
    monkeypatch.setattr(ec.httpx, "post", lambda *a, **k: _Resp())

    out = ec.get_item_details_batch(["123456"], "app", "dev", "cert", "tok")
    assert out["123456"]["category_id"] == 112529
    assert out["123456"]["watch_count"] == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
