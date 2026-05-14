#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14 customs_kb + product_resolver unit tests."""
from __future__ import annotations

import json

import pytest

from monitor.customs_kb import (
    ManufacturerInfo, HTSInfo,
    lookup_manufacturer, lookup_hts,
    propose_kb_entry, list_pending_kb, reject_kb_entry,
)
from monitor.customs_product_resolver import (
    ResolvedProduct, _extract_ebay_item_id,
)


# ─────────────────────────────
# lookup_manufacturer
# ─────────────────────────────

def test_lookup_manufacturer_onyx_boox():
    r = lookup_manufacturer("ONYX BOOX Leaf2 White 7-inch E-Ink Reader")
    assert r is not None
    assert r.brand == "ONYX BOOX"
    assert "SKT" in r.name
    assert "Osaka" in r.address or "大阪" in r.address
    assert r.is_distributor is True


def test_lookup_manufacturer_razer_keeps_us_hq():
    r = lookup_manufacturer("Razer HyperFlux Gaming Mouse Pad")
    assert r is not None
    assert r.brand == "Razer"
    assert "Irvine" in r.address  # 米国本社住所
    assert r.is_distributor is False


def test_lookup_manufacturer_hiragana_brand():
    r = lookup_manufacturer("ルクルーゼ mini cocotte")
    assert r is not None
    assert r.brand == "Le Creuset"


def test_lookup_manufacturer_unknown_returns_none():
    assert lookup_manufacturer("Unknown SuperBrand XYZ") is None
    assert lookup_manufacturer("") is None


# ─────────────────────────────
# lookup_hts
# ─────────────────────────────

def test_lookup_hts_e_reader():
    r = lookup_hts("ONYX BOOX Leaf2 E-Ink Reader")
    assert r is not None
    assert r.code == "8543.70.9200"
    assert r.duty == "Free"


def test_lookup_hts_by_category_hint():
    r = lookup_hts("some title", categories=["e-reader"])
    assert r is not None
    assert r.code == "8543.70.9200"


def test_lookup_hts_otdr_specific():
    r = lookup_hts("YOKOGAWA AQ7275 OTDR Optical Time Domain Reflectometer")
    assert r is not None
    assert r.code == "9031.41.0000"


def test_lookup_hts_fallback_to_generic():
    r = lookup_hts("some random unlinked product")
    assert r is not None
    # 該当なしで generic-electrical-apparatus に落ちる
    assert r.category == "generic-electrical-apparatus"


# ─────────────────────────────
# Tier 3 承認フロー
# ─────────────────────────────

def test_propose_and_reject_pending():
    from monitor.database import get_conn
    try:
        pid = propose_kb_entry(
            kind="manufacturer",
            brand_or_category="_test_brand_xyz",
            proposed_json={"distributor_name": "Test Corp"},
            source_url="https://example.com/test",
        )
        assert pid > 0
        # list に出るか
        pending = list_pending_kb()
        assert any(p["id"] == pid for p in pending)
        # 同じ brand で propose しても重複 insert されない (idempotent)
        pid2 = propose_kb_entry(
            kind="manufacturer",
            brand_or_category="_test_brand_xyz",
            proposed_json={"distributor_name": "Test Corp v2"},
        )
        assert pid == pid2
        # reject
        assert reject_kb_entry(pid) is True
        # reject 済は list に出ない
        assert not any(p["id"] == pid for p in list_pending_kb())
    finally:
        with get_conn() as c:
            c.execute(
                "DELETE FROM customs_kb_pending WHERE brand_or_category = ?",
                ("_test_brand_xyz",),
            )


# ─────────────────────────────
# product_resolver (Gmail 不要な単体関数)
# ─────────────────────────────

def test_extract_ebay_item_id():
    text = "Re: Item sold - #357618434395 ONYX BOOX"
    assert _extract_ebay_item_id(text) == "357618434395"


def test_extract_ebay_item_id_no_hash():
    text = "Order reference 358178581550 shipped"
    assert _extract_ebay_item_id(text) == "358178581550"


def test_extract_ebay_item_id_too_short_returns_none():
    assert _extract_ebay_item_id("id 12345") is None
    assert _extract_ebay_item_id("") is None


def test_resolved_product_dataclass():
    r = ResolvedProduct(
        ebay_item_id="123456789012", sku="ebayyh_x1", title="test",
        source="gmail_sold", confidence="high",
    )
    assert r.ebay_item_id == "123456789012"
    assert r.confidence == "high"
