# -*- coding: utf-8 -*-
"""W191 (2026-05-30): 個別出品の出品成功時に primary_market を ebay_listings へ永続化。

検証対象 = monitor.database.upsert_ebay_listing の primary_market 反映:
  - INSERT 時に user が選んだ出品区分が ebay_item_id キーで保存される。
  - 'us_only' (UI lowercase) は 'US_only' (Terapeak / lint canonical) に正規化される。
  - primary_market を省略 / None で渡すと既存値を踏み潰さない (COALESCE 防護)。
  - 既存 listing の UPDATE 経路 (sku 変更 / 通常) でも区分が反映される。
  - listing 識別は ebay_item_id (sku-rules.md 準拠、SKU で特定しない)。

conftest.py の autouse fixture で DB は tmp_path に隔離済 (本番 monitor.db 非汚染)。
"""
from __future__ import annotations

import pytest

from monitor.database import (
    _normalize_primary_market,
    get_conn,
    get_ebay_listing_by_item_id,
    init_db,
    upsert_ebay_listing,
)


# ---------------------------------------------------------------------------
# _normalize_primary_market (pure)
# ---------------------------------------------------------------------------

def test_normalize_us_only_to_canonical():
    """UI lowercase 'us_only' は Terapeak 語彙 'US_only' に寄せる。"""
    assert _normalize_primary_market("us_only") == "US_only"
    assert _normalize_primary_market("US_only") == "US_only"
    assert _normalize_primary_market("US_ONLY") == "US_only"


def test_normalize_other_markets_passthrough():
    """他 3 区分は両経路とも lowercase で一致 = 素通し。"""
    assert _normalize_primary_market("mixed_global") == "mixed_global"
    assert _normalize_primary_market("global_only") == "global_only"
    assert _normalize_primary_market("unknown") == "unknown"


def test_normalize_none_and_empty():
    """None / 空文字は None (列に触れない signal)。"""
    assert _normalize_primary_market(None) is None
    assert _normalize_primary_market("") is None
    assert _normalize_primary_market("   ") is None


# ---------------------------------------------------------------------------
# upsert_ebay_listing 永続化 (DB)
# ---------------------------------------------------------------------------

def test_insert_persists_primary_market_us_only_normalized():
    """新規 INSERT で 'us_only' が 'US_only' として ebay_item_id キーに保存される。"""
    init_db()
    eid = "TESTW191_INS_US"
    upsert_ebay_listing(
        eid, sku="ebayyh_p191a", title="t", current_price=120.0,
        quantity_ebay=1, primary_market="us_only",
    )
    row = get_ebay_listing_by_item_id(eid)
    assert row is not None
    assert row["primary_market"] == "US_only"


def test_insert_persists_mixed_global():
    """mixed_global はそのまま保存される。"""
    init_db()
    eid = "TESTW191_INS_MIX"
    upsert_ebay_listing(
        eid, sku="ebayyh_p191b", title="t", current_price=100.0,
        quantity_ebay=1, primary_market="mixed_global",
    )
    assert get_ebay_listing_by_item_id(eid)["primary_market"] == "mixed_global"


def test_insert_none_market_leaves_null():
    """primary_market を渡さない既存呼出 (後方互換) は NULL のまま。"""
    init_db()
    eid = "TESTW191_INS_NONE"
    upsert_ebay_listing(eid, sku="ebayyh_p191c", title="t")
    assert get_ebay_listing_by_item_id(eid)["primary_market"] is None


def test_update_none_does_not_clobber_existing_market():
    """COALESCE 防護: 後続の primary_market なし upsert は既存区分を踏み潰さない。

    出品時に区分確定 → 次回 ebay_sync (primary_market を渡さない) が来ても維持。
    """
    init_db()
    eid = "TESTW191_UPD_KEEP"
    upsert_ebay_listing(
        eid, sku="ebayyh_p191d", title="t1", primary_market="global_only",
    )
    # ebay_sync 相当の再 upsert (区分を渡さない通常 UPDATE 経路)
    upsert_ebay_listing(eid, sku="ebayyh_p191d", title="t2", current_price=55.0)
    row = get_ebay_listing_by_item_id(eid)
    assert row["primary_market"] == "global_only", "区分が踏み潰されてはいけない"
    assert row["title"] == "t2"  # 他カラムは通常 UPDATE される


def test_update_overwrites_when_market_given():
    """新しい区分を渡せば既存値を更新する (user が出品区分を選び直すケース)。"""
    init_db()
    eid = "TESTW191_UPD_OVER"
    upsert_ebay_listing(eid, sku="ebayyh_p191e", title="t1", primary_market="unknown")
    upsert_ebay_listing(
        eid, sku="ebayyh_p191e", title="t1", primary_market="us_only",
    )
    assert get_ebay_listing_by_item_id(eid)["primary_market"] == "US_only"


def test_update_sku_changed_path_persists_market():
    """SKU 変更経路 (source_* reset) でも primary_market が反映される。"""
    init_db()
    eid = "TESTW191_SKU_CHG"
    upsert_ebay_listing(eid, sku="ebayyh_old", title="t1")
    # SKU を変更しつつ区分を渡す (sku_changed=True 経路)
    upsert_ebay_listing(
        eid, sku="ebayyh_new", title="t2", primary_market="us_only",
    )
    row = get_ebay_listing_by_item_id(eid)
    assert row["sku"] == "ebayyh_new"
    assert row["primary_market"] == "US_only"


def test_identification_is_ebay_item_id_not_sku():
    """sku-rules.md 準拠: 同 SKU を共有する別 listing でも区分は独立して保存される。

    有在庫 SKU (stock**) は複数 listing が共有するのが正常。primary_market は
    ebay_item_id 単位で保存され、SKU 共有で混線しないことを確認。
    """
    init_db()
    upsert_ebay_listing(
        "TESTW191_A", sku="stock01", title="A", primary_market="us_only",
    )
    upsert_ebay_listing(
        "TESTW191_B", sku="stock01", title="B", primary_market="global_only",
    )
    assert get_ebay_listing_by_item_id("TESTW191_A")["primary_market"] == "US_only"
    assert get_ebay_listing_by_item_id("TESTW191_B")["primary_market"] == "global_only"
