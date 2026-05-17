"""W138 (2026-05-17): BP-only Revise 経路 + W136 override 経路 非回帰.

- `_build_revise_bp_only_xml` は <SellerProfiles>(3 ID) のみ出力、
  <StartPrice>/<ShippingServiceCostOverrideList> 非出力 (HIGH-1 訂正経路)。
- **W136 回帰**: `_build_revise_with_shipping_xml` の gate は不変
  (ship_cost_usd 指定時のみ override+SellerProfiles、None なら旧挙動)。
- `revise_shipping_profile` は早期 return gate を持たず BP のみで API 到達。
- shipping_id 欠落時は API 呼ばず success:False (Q0)。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── BP-only XML 構造 (HIGH-1/HIGH-2) ──

def test_bp_only_xml_emits_three_seller_profiles_no_override_no_price():
    from monitor.ebay_client import _build_revise_bp_only_xml
    xml = _build_revise_bp_only_xml(
        "ITEM1",
        {"payment_id": "PAY1", "return_id": "RET1", "shipping_id": "BP_NEW"},
    )
    assert "<SellerProfiles>" in xml
    assert "<ShippingProfileID>BP_NEW</ShippingProfileID>" in xml
    assert "<PaymentProfileID>PAY1</PaymentProfileID>" in xml
    assert "<ReturnProfileID>RET1</ReturnProfileID>" in xml
    # BP-only: 価格も override も出さない
    assert "<StartPrice" not in xml
    assert "ShippingServiceCostOverrideList" not in xml
    assert "<ItemID>ITEM1</ItemID>" in xml


def test_bp_only_xml_shipping_required_payment_return_optional():
    from monitor.ebay_client import _build_revise_bp_only_xml
    xml = _build_revise_bp_only_xml("ITEM1", {"shipping_id": "BP_X"})
    assert "<ShippingProfileID>BP_X</ShippingProfileID>" in xml
    assert "<SellerPaymentProfile>" not in xml
    assert "<SellerReturnProfile>" not in xml


# ── W136 override 経路 非回帰 (auto-HIGH) ──

def test_w136_override_path_unchanged_with_ship_cost():
    """ship_cost_usd 指定時は従来通り override + SellerProfiles 同梱 (gate 不変)."""
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "ITEM1", None, 12.50, 0.0,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "BP1"},
    )
    assert "<SellerProfiles>" in xml
    assert "ShippingServiceCostOverrideList" in xml
    assert "12.50" in xml
    assert "<ShippingServiceType>Domestic</ShippingServiceType>" in xml


def test_w136_override_path_no_seller_profiles_when_none():
    """seller_profiles なし = 旧挙動維持 (D1 後方互換、gate 不変)."""
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml("ITEM1", None, 12.50, 0.0)
    assert "<SellerProfiles>" not in xml
    assert "ShippingServiceCostOverrideList" in xml


# ── revise_shipping_profile: 早期return回避 + Q0 ──

def test_revise_shipping_profile_calls_api_not_early_return():
    """BP のみ (price/ship なし) でも _call_trading_api に到達する (HIGH-1)."""
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api",
                      return_value={"success": True, "ack": "Success",
                                    "raw": "<x/>"}) as m:
        res = ec.revise_shipping_profile(
            "ITEM1",
            {"payment_id": "P", "return_id": "R", "shipping_id": "BP_NEW"},
            "A", "D", "C", "v^T",
        )
    m.assert_called_once()
    assert m.call_args[0][0] == "ReviseFixedPriceItem"
    assert "BP_NEW" in m.call_args[0][1]      # XML に新 BP
    assert res["success"] is True


def test_revise_shipping_profile_warning_with_fatal_downgraded():
    """Ack=Warning でも Errors SeverityCode=Error は success:False に降格
    (W136/W183 と挙動統一、code-reviewer MEDIUM 2026-05-17)."""
    import monitor.ebay_client as ec
    raw = (
        '<ReviseFixedPriceItemResponse '
        'xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Ack>Warning</Ack>'
        '<Errors><SeverityCode>Error</SeverityCode>'
        '<ErrorCode>21916</ErrorCode>'
        '<LongMessage>Shipping policy not valid</LongMessage></Errors>'
        '</ReviseFixedPriceItemResponse>'
    )
    with patch.object(ec, "_call_trading_api",
                      return_value={"success": True, "ack": "Warning",
                                    "raw": raw}):
        res = ec.revise_shipping_profile(
            "ITEM1",
            {"payment_id": "P", "return_id": "R", "shipping_id": "BP_NEW"},
            "A", "D", "C", "v^T")
    assert res["success"] is False
    assert "21916" in res["message"]
    assert "SeverityCode=Error" in res["message"]


def test_revise_shipping_profile_requires_all_three_ids():
    """payment/return が欠けても API 呼ばず success:False (Codex HIGH 2026-05-17,
    SellerProfiles 3 ID 全必須)."""
    import monitor.ebay_client as ec
    for sp in (
        {"shipping_id": "BP", "return_id": "R"},      # payment 欠落
        {"shipping_id": "BP", "payment_id": "P"},     # return 欠落
        {"payment_id": "P", "return_id": "R"},        # shipping 欠落
    ):
        with patch.object(ec, "_call_trading_api") as m:
            res = ec.revise_shipping_profile(
                "ITEM1", sp, "A", "D", "C", "v^T")
        m.assert_not_called()
        assert res["success"] is False
        assert "SellerProfiles 不完全" in res["message"]


def test_revise_shipping_profile_missing_shipping_id_no_api_call():
    """shipping_id 欠落 → API 呼ばず success:False (Q0、不完全 SellerProfiles 送らない)."""
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api") as m:
        res = ec.revise_shipping_profile(
            "ITEM1", {"payment_id": "P"}, "A", "D", "C", "v^T",
        )
    m.assert_not_called()
    assert res["success"] is False
    assert "shipping_id" in res["message"]
