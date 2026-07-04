"""W137 (2026-05-17): ebay_listing_snapshot.fetch_listing_snapshot 単体.

GetItem 実 XML からの SKU/価格/送料/3 profile ID 抽出と、
通信/parse/Ack 異常時の Q0 (raise せず ok=False + error) を固定。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_OK_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>356364841116</ItemID>
    <SKU>stock:01</SKU>
    <StartPrice currencyID="USD">148.0</StartPrice>
    <SellingStatus><CurrentPrice currencyID="USD">148.0</CurrentPrice></SellingStatus>
    <ShippingDetails>
      <ShippingServiceOptions>
        <ShippingServiceCost currencyID="USD">31.6</ShippingServiceCost>
        <ShippingServiceAdditionalCost currencyID="USD">0.0</ShippingServiceAdditionalCost>
      </ShippingServiceOptions>
    </ShippingDetails>
    <SellerProfiles>
      <SellerShippingProfile>
        <ShippingProfileID>377279110023</ShippingProfileID>
        <ShippingProfileName>DDP_0.5-1kg_$030_1day</ShippingProfileName>
      </SellerShippingProfile>
      <SellerReturnProfile>
        <ReturnProfileID>359243687023</ReturnProfileID>
      </SellerReturnProfile>
      <SellerPaymentProfile>
        <PaymentProfileID>359244671023</PaymentProfileID>
      </SellerPaymentProfile>
    </SellerProfiles>
  </Item>
</GetItemResponse>"""

_FAIL_ACK_XML = """<?xml version="1.0"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><LongMessage>Invalid item ID</LongMessage></Errors>
</GetItemResponse>"""


def _resp(text):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock(return_value=None)
    return r


def _call():
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot
    return fetch_listing_snapshot("356364841116", "A", "D", "C", "v^T")


def test_parses_all_fields():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_OK_XML)):
        s = _call()
    assert s.ok is True and s.ack == "Success"
    assert s.sku == "stock:01"
    assert s.start_price_usd == 148.0
    assert s.ship_cost_usd == 31.6
    assert s.ship_additional_usd == 0.0
    assert s.shipping_profile_id == "377279110023"
    assert s.return_profile_id == "359243687023"
    assert s.payment_profile_id == "359244671023"


_OVERRIDE_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>356364841116</ItemID>
    <SKU>stock</SKU>
    <StartPrice currencyID="USD">148.0</StartPrice>
    <ShippingDetails>
      <ShippingServiceOptions>
        <ShippingServiceCost currencyID="USD">31.6</ShippingServiceCost>
      </ShippingServiceOptions>
    </ShippingDetails>
    <ShippingServiceCostOverrideList>
      <ShippingServiceCostOverride>
        <ShippingServiceType>Domestic</ShippingServiceType>
        <ShippingServicePriority>1</ShippingServicePriority>
        <ShippingServiceCost currencyID="USD">29.0</ShippingServiceCost>
        <ShippingServiceAdditionalCost currencyID="USD">2.0</ShippingServiceAdditionalCost>
      </ShippingServiceCostOverride>
    </ShippingServiceCostOverrideList>
  </Item>
</GetItemResponse>"""


def test_override_container_preferred_over_shipping_options():
    """override 在れば実効値として ShippingServiceOptions(BP default) でなく
    ShippingServiceCostOverrideList の Domestic を採る (Codex HIGH 2026-05-17)."""
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_OVERRIDE_XML)):
        s = _call()
    assert s.ok is True
    assert s.ship_cost_usd == 29.0      # override 優先 (BP default 31.6 でない)
    assert s.ship_additional_usd == 2.0


def test_no_override_falls_back_to_shipping_options():
    """override コンテナ無し時は ShippingServiceOptions を採る (fallback)."""
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_OK_XML)):
        s = _call()
    assert s.ship_cost_usd == 31.6


def test_parseerror_returns_ok_false_not_raise():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post",
                      return_value=_resp("<GetItemResponse><Item>")):
        s = _call()
    assert s.ok is False
    assert "parse" in (s.error or "").lower()


def test_fail_ack_returns_ok_false():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_FAIL_ACK_XML)):
        s = _call()
    assert s.ok is False and s.ack == "Failure"
    assert "Invalid item ID" in (s.error or "")


def test_http_error_returns_ok_false():
    import monitor.ebay_listing_snapshot as m
    import httpx as _h
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post",
                      side_effect=_h.ConnectError("boom")):
        s = _call()
    assert s.ok is False
    assert "通信エラー" in (s.error or "")


# ── #44 Wave2 (2026-07-04): title/condition_description/item_specifics/picture_count ──

_SPECIFICS_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>356364841116</ItemID>
    <SKU>stock:01</SKU>
    <Title>Sony WH-1000XM5 Wireless Headphones Black</Title>
    <StartPrice currencyID="USD">148.0</StartPrice>
    <ConditionID>3000</ConditionID>
    <ConditionDescription>Tested and fully working (2026-07). Minor cosmetic wear.</ConditionDescription>
    <PictureDetails>
      <PictureURL>https://i.ebayimg.com/images/g/abc/s-l500.jpg</PictureURL>
      <PictureURL>https://i.ebayimg.com/images/g/def/s-l500.jpg</PictureURL>
    </PictureDetails>
    <ItemSpecifics>
      <NameValueList>
        <Name>Brand</Name>
        <Value>Sony</Value>
      </NameValueList>
      <NameValueList>
        <Name>Country of Origin</Name>
        <Value>Japan</Value>
      </NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>"""


def test_request_xml_includes_item_specifics_flag():
    """#44 Wave2: outbound GetItem request に
    <IncludeItemSpecifics>true</IncludeItemSpecifics> が含まれる (実 API probe
    で確定した必須要素、data/tmp/coo_scan.py 参照)。#44 G2 で共通ビルダー
    `_build_get_item_xml` (monitor/ebay_client.py) 側が既定送出するよう
    修正済のため、fetch_listing_snapshot は追加パラメータ無しでこれを満たす。"""
    import monitor.ebay_listing_snapshot as m
    captured = {}

    def _fake_post(url, content=None, **kwargs):
        captured["xml"] = content.decode("utf-8") if isinstance(content, bytes) else content
        return _resp(_SPECIFICS_XML)

    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", side_effect=_fake_post):
        _call()
    assert "<IncludeItemSpecifics>true</IncludeItemSpecifics>" in captured["xml"]


def test_parses_title_condition_description_specifics_and_pictures():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_SPECIFICS_XML)):
        s = _call()
    assert s.ok is True
    assert s.title == "Sony WH-1000XM5 Wireless Headphones Black"
    assert s.condition_description == (
        "Tested and fully working (2026-07). Minor cosmetic wear."
    )
    assert s.item_specifics == {"Brand": ["Sony"], "Country of Origin": ["Japan"]}
    assert s.picture_count == 2


def test_title_and_specifics_default_to_empty_when_absent():
    """Title/ItemSpecifics/PictureDetails が応答に無い場合でも壊れない
    (item_specifics=空dict, picture_count=0)."""
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_OK_XML)):
        s = _call()
    assert s.item_specifics == {}
    assert s.picture_count == 0
