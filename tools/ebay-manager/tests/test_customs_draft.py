#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14 customs_draft_generator unit tests (Claude API はモック不要分のみ)."""
from __future__ import annotations

import pytest

from monitor.customs_draft_generator import (
    CARRIER_REPLY_MAP, _determine_recipients,
    _safe_excerpt, _serialize_hts, _serialize_mfg,
)
from monitor.customs_kb import HTSInfo, ManufacturerInfo
from monitor.customs_parser import ParsedRequest


def test_carrier_reply_map_has_all_3_carriers():
    for c in ("fedex", "dhl", "ups"):
        assert c in CARRIER_REPLY_MAP


def test_determine_recipients_fedex_with_osv_and_case_cc():
    parsed = ParsedRequest(
        tracking_number="870904145187",
        sender_osv_email="Jayson <jayson.lumbang.osv@fedex.com>",
        carrier_case_cc="5259134@fedex.com",
    )
    to_list, cc_list = _determine_recipients(
        "fedex", parsed, "Jayson <jayson.lumbang.osv@fedex.com>"
    )
    assert "paperwork@fedex.com" in to_list
    cc_lower = [c.lower() for c in cc_list]
    # OSV は extracted email address が入る
    assert any("jayson.lumbang.osv@fedex.com" in c for c in cc_lower)
    # case CC も含まれる
    assert any("5259134@fedex.com" in c for c in cc_lower)


def test_determine_recipients_rejects_non_carrier_domain():
    """攻撃者が偽の OSV アドレスを紛れ込ませても allow-list で弾かれる."""
    parsed = ParsedRequest(
        sender_osv_email="evil@attacker.com",
        carrier_case_cc="also_evil@notfedex.com",
    )
    to_list, cc_list = _determine_recipients(
        "fedex", parsed, "evil@attacker.com"
    )
    # TO は static paperwork@fedex.com のみ
    assert to_list == ["paperwork@fedex.com"]
    # CC には evil が混入しない
    joined = " ".join(cc_list).lower()
    assert "evil" not in joined
    assert "notfedex" not in joined


def test_determine_recipients_ups_importbrokerage():
    parsed = ParsedRequest()
    to_list, _ = _determine_recipients("ups", parsed, "")
    assert "importbrokerage@ups.com" in to_list


def test_serialize_mfg_handles_none():
    assert "miss" in _serialize_mfg(None).lower()


def test_serialize_mfg_formats_distributor():
    m = ManufacturerInfo(
        brand="ONYX BOOX", name="SKT Co., Ltd.",
        address="Osaka, Japan", is_distributor=True,
    )
    out = _serialize_mfg(m)
    assert "SKT" in out
    assert "is_japan_distributor: True" in out


def test_serialize_hts_handles_none():
    assert "miss" in _serialize_hts(None).lower()


def test_serialize_hts_formats():
    h = HTSInfo(category="e-reader", code="8543.70.9200",
                description="Electrical machines", duty="Free")
    out = _serialize_hts(h)
    assert "8543.70.9200" in out
    assert "Free" in out


def test_safe_excerpt_caps_at_1500():
    p = ParsedRequest(
        attachment_text_summaries=["A" * 2000, "B" * 1000]
    )
    out = _safe_excerpt(p)
    assert len(out) <= 1500


def test_generate_draft_returns_fallback_without_api_key(monkeypatch):
    """ANTHROPIC_API_KEY 未設定 → manual フラグ付き最小 draft."""
    from monitor.customs_draft_generator import generate_draft
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    parsed = ParsedRequest(tracking_number="TEST123")
    draft = generate_draft(
        carrier="fedex", parsed=parsed,
        product_title="ONYX BOOX Leaf2 E-Reader",
        ebay_item_id="357618434395",
    )
    assert draft.confidence == "low"
    assert any("ANTHROPIC_API_KEY" in r or "not invoked" in r or "not installed" in r
               for r in draft.manual_review_reasons)
    # KB hit (ONYX BOOX) は正しく解決されている
    assert draft.manufacturer_hit is not None
    assert draft.manufacturer_hit.brand == "ONYX BOOX"
    assert draft.hts_hit is not None
    assert draft.hts_hit.code == "8543.70.9200"
    # recipient は static で入る
    assert "paperwork@fedex.com" in draft.to_list
