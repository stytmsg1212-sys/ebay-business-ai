"""W138 (2026-05-17): Account API shipping policy client 単体.

fetch_shipping_policies の正常解析と Q0 (通信/認証/parse/空 異常時に
raise せず ok=False+error)。urllib をモック。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

_OK_JSON = json.dumps({
    "fulfillmentPolicies": [
        {
            "fulfillmentPolicyId": "377279110023",
            "name": "DDP_0.5-1kg_$030_1day",
            "shippingOptions": [
                {"optionType": "DOMESTIC", "shippingServices": [
                    {"shippingServiceCode": "US_ExpeditedSppedPAK",
                     "sortOrderId": None}]},
                {"optionType": "INTERNATIONAL", "shippingServices": [
                    {"shippingServiceCode": "US_IntlExpressSpeedPAK"}]},
            ],
        },
        {
            "fulfillmentPolicyId": "365329085023",
            "name": "004 STOCK(1day) EXPEDITED 1.5 ~ 2.0kg",
            "shippingOptions": [
                {"optionType": "DOMESTIC", "shippingServices": [
                    {"shippingServiceCode": "US_X"}]},
            ],
        },
    ]
})


def _patch_token(tok="FAKE-TEST-TOKEN"):
    return patch(
        "monitor.ebay_oauth_refresh.get_valid_access_token",
        return_value=tok,
    )


def _urlopen_returning(body: str):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body.encode("utf-8")
    cm.__exit__.return_value = False
    return cm


def test_parses_policies_name_sorted():
    import monitor.ebay_account_policy as m
    with _patch_token(), \
         patch.object(m.urllib.request, "urlopen",
                      return_value=_urlopen_returning(_OK_JSON)):
        r = m.fetch_shipping_policies({})
    assert r.ok is True and r.error is None
    assert len(r.policies) == 2
    # name 昇順 (Q-5): "004 STOCK..." < "DDP_..."
    assert r.policies[0].name.startswith("004 STOCK")
    assert r.policies[1].policy_id == "377279110023"
    assert r.name_for("377279110023") == "DDP_0.5-1kg_$030_1day"
    assert r.name_for("nope") is None
    ddp = [p for p in r.policies if p.policy_id == "377279110023"][0]
    assert ddp.domestic_service_count == 1
    assert "US_ExpeditedSppedPAK" in ddp.service_names


def test_no_token_returns_ok_false():
    import monitor.ebay_account_policy as m
    with _patch_token(tok=None):
        r = m.fetch_shipping_policies({})
    assert r.ok is False and r.policies == []
    assert "token" in r.error.lower()


def test_http_error_returns_ok_false_not_raise():
    import monitor.ebay_account_policy as m
    with _patch_token(), \
         patch.object(m.urllib.request, "urlopen",
                      side_effect=OSError("403 Forbidden")):
        r = m.fetch_shipping_policies({})
    assert r.ok is False
    assert "通信エラー" in r.error


def test_non_json_returns_ok_false():
    import monitor.ebay_account_policy as m
    with _patch_token(), \
         patch.object(m.urllib.request, "urlopen",
                      return_value=_urlopen_returning("<html>maintenance</html>")):
        r = m.fetch_shipping_policies({})
    assert r.ok is False
    assert "JSON parse" in r.error


def test_empty_policies_returns_ok_false():
    import monitor.ebay_account_policy as m
    with _patch_token(), \
         patch.object(m.urllib.request, "urlopen",
                      return_value=_urlopen_returning(
                          json.dumps({"fulfillmentPolicies": []}))):
        r = m.fetch_shipping_policies({})
    assert r.ok is False
    assert "0 件" in r.error


def test_errors_payload_returns_ok_false():
    import monitor.ebay_account_policy as m
    with _patch_token(), \
         patch.object(m.urllib.request, "urlopen",
                      return_value=_urlopen_returning(
                          json.dumps({"errors": [{"message": "invalid scope"}]}))):
        r = m.fetch_shipping_policies({})
    assert r.ok is False
    assert "応答異常" in r.error
