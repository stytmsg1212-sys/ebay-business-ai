#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-04-25 self-source bug fix regression test."""
from __future__ import annotations

import pytest

from tasks.task_supplier_candidate_search import _normalize_url


def test_normalize_url_basic():
    assert _normalize_url("https://example.com/path") == "example.com/path"
    assert _normalize_url("HTTPS://EXAMPLE.COM/PATH") == "example.com/path"


def test_normalize_url_trailing_slash():
    assert _normalize_url("https://example.com/path/") == _normalize_url("https://example.com/path")


def test_normalize_url_query_stripped():
    assert _normalize_url("https://example.com/x?from=search") == _normalize_url("https://example.com/x")


def test_normalize_url_fragment_stripped():
    assert _normalize_url("https://example.com/x#section") == _normalize_url("https://example.com/x")


def test_normalize_url_yahoo_auction_subdomain():
    a = _normalize_url("https://page.auctions.yahoo.co.jp/jp/auction/x123")
    b = _normalize_url("https://auctions.yahoo.co.jp/jp/auction/x123")
    assert a == b


def test_normalize_url_www_prefix():
    assert _normalize_url("https://www.example.com/x") == _normalize_url("https://example.com/x")


def test_normalize_url_empty():
    assert _normalize_url("") == ""
    assert _normalize_url(None) == ""


def test_normalize_url_invalid_safe_fallback():
    out = _normalize_url("not a url at all")
    assert isinstance(out, str)


class _HitObj:
    pass


def _make_hit(platform, url, price=5000, title="x"):
    h = _HitObj()
    h.source_platform = platform
    h.url = url
    h.price_jpy = price
    h.title = title
    h.image_url = None
    return h


def _setup_common(monkeypatch, t, listing, hits, platform="paypay"):
    # 2026-05-01 W75 4b: get_ebay_listing_by_sku → get_ebay_listing_by_item_id
    # (canonical lookup key を ebay_item_id に変更).
    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(t, "load_settings", lambda: {})
    # W182 (2026-05-28): 在庫 gate を「常に available」固定で mock (本 test は self URL
    # 除外の単体検証であり、availability gate の挙動は test_w182_availability_gate.py
    # 側で別途網羅. mock 無いと task module の check_candidate_availability が実 HTTP fetch
    # を発火し、test 不安定 + 既存 self-exclude 経路の検証が壊れる).
    monkeypatch.setattr(
        t, "check_candidate_availability",
        lambda url, **_kw: {
            "status": "available", "signal": "test mock",
            "checked_at": "2026-05-28T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        t, "search_candidates_on_platform",
        lambda plat, kw, max_results=5: hits if plat == platform else [],
    )
    monkeypatch.setattr(
        t, "evaluate_candidate_with_claude",
        lambda h, t2, sku=None, ebay_item_id=None, **_kw:
            t.ScoredCandidate(hit=h, match_score=85, match_reasoning="ok"),
    )
    # profit を valid で返す (None だと unprofitable skip 経路に落ちる)
    monkeypatch.setattr(t, "_estimate_profit_for_candidate", lambda **kw: 5000.0)
    monkeypatch.setattr(
        t, "check_supplier_candidate_profitable",
        lambda profit_with_refund, purchase_yen: (True, {}),
    )
    saved = []
    monkeypatch.setattr(t, "add_supplier_candidate", lambda **kw: saved.append(kw) or 1)
    return saved


def test_run_search_excludes_self_source_url(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {
        "sku": "test_sku", "title": "T", "current_price": 100.0,
        "ebay_item_id": "1",
        "source_url": "https://paypayfleamarket.yahoo.co.jp/item/z400651116",
    }
    self_hit = _make_hit("paypay", "https://paypayfleamarket.yahoo.co.jp/item/z400651116")
    other_hit = _make_hit("paypay", "https://paypayfleamarket.yahoo.co.jp/item/different123")
    saved = _setup_common(monkeypatch, t, listing, [self_hit, other_hit], "paypay")
    r = t.run_supplier_candidate_search(
        ebay_item_id="1", sku="test_sku", config={},
        platforms=["paypay"], discovered_via="test",
    )
    assert r["excluded_self"] == 1
    assert r["found"] == 1
    saved_urls = {kw["candidate_url"] for kw in saved}
    assert "https://paypayfleamarket.yahoo.co.jp/item/z400651116" not in saved_urls
    assert "https://paypayfleamarket.yahoo.co.jp/item/different123" in saved_urls


def test_run_search_normalizes_url_for_comparison(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {
        "sku": "test_sku", "title": "T", "current_price": 100.0,
        "ebay_item_id": "1",
        "source_url": "https://paypayfleamarket.yahoo.co.jp/item/abc/",
    }
    hit = _make_hit("paypay", "https://paypayfleamarket.yahoo.co.jp/item/abc?from=search")
    _setup_common(monkeypatch, t, listing, [hit], "paypay")
    r = t.run_supplier_candidate_search(
        ebay_item_id="1", sku="test_sku", config={},
        platforms=["paypay"], discovered_via="test",
    )
    assert r["excluded_self"] == 1
    assert r["found"] == 0


def test_run_search_excludes_yahoo_subdomain_variants(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {
        "sku": "ebayyh_x123", "title": "Yahoo", "current_price": 100.0,
        "ebay_item_id": "1",
        "source_url": "https://page.auctions.yahoo.co.jp/jp/auction/x1234567",
    }
    self_hit_no_page = _make_hit(
        "yahoo_auctions",
        "https://auctions.yahoo.co.jp/jp/auction/x1234567",
    )
    _setup_common(monkeypatch, t, listing, [self_hit_no_page], "yahoo_auctions")
    r = t.run_supplier_candidate_search(
        ebay_item_id="1", sku="ebayyh_x123", config={},
        platforms=["yahoo_auctions"], discovered_via="test",
    )
    assert r["excluded_self"] == 1
    assert r["found"] == 0


def test_run_search_with_null_source_url_passes_through(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {
        "sku": "test_sku", "title": "T", "current_price": 100.0,
        "ebay_item_id": "1",
        "source_url": None,
    }
    hits = [
        _make_hit("paypay", "https://example.com/a"),
        _make_hit("paypay", "https://example.com/b"),
    ]
    _setup_common(monkeypatch, t, listing, hits, "paypay")
    r = t.run_supplier_candidate_search(
        ebay_item_id="1", sku="test_sku", config={},
        platforms=["paypay"], discovered_via="test",
    )
    assert r["excluded_self"] == 0
    assert r["found"] == 2
