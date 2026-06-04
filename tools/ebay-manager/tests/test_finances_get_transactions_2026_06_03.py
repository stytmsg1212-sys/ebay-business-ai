"""W219 段1-2 (2026-06-03): eBay Finances API `get_transactions` + `parse_sale_fees`

実 API は叩かない. mock response で parse / pagination / Q0 失敗時の挙動を担保.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ─── parse_sale_fees: schema 仮定の解凍 ─────────────────────────────

def test_parse_sale_fees_basic_full_schema():
    from monitor.ebay_client import parse_sale_fees
    txn = {
        "transactionId": "TXN-1",
        "transactionType": "SALE",
        "transactionDate": "2026-05-01T03:14:00.000Z",
        "orderId": "12-34567-89012",
        "amount": {"value": "120.00", "currency": "USD"},
        "totalFeeAmount": {"value": "16.20", "currency": "USD"},
        "orderLineItems": [
            {
                "lineItemId": "LI-1",
                "itemId": "v1|123456789012|0",
                "marketplaceFees": [
                    {"feeType": "FINAL_VALUE_FEE",
                     "amount": {"value": "12.00", "currency": "USD"}},
                    {"feeType": "INTERNATIONAL_FEE",
                     "amount": {"value": "1.20", "currency": "USD"}},
                    {"feeType": "AD_FEE",
                     "amount": {"value": "2.40", "currency": "USD"}},
                    {"feeType": "REGULATORY_OPERATING_FEE",
                     "amount": {"value": "0.60", "currency": "USD"}},
                ],
            },
        ],
    }
    out = parse_sale_fees(txn)
    assert out["order_id"] == "12-34567-89012"
    assert out["transaction_id"] == "TXN-1"
    assert out["amount_usd"] == 120.00
    assert out["total_fee_usd"] == 16.20
    # eBay legacy item id を v1|...|0 から抽出
    assert out["line_items"][0]["item_id"] == "123456789012"
    # 集約 fees_by_type
    assert out["fees_by_type"]["FINAL_VALUE_FEE"] == 12.00
    assert out["fees_by_type"]["INTERNATIONAL_FEE"] == 1.20
    assert out["fees_by_type"]["AD_FEE"] == 2.40
    assert out["fees_by_type"]["REGULATORY_OPERATING_FEE"] == 0.60
    # 行から再集計した total = totalFeeAmount と一致 (sanity)
    assert abs(out["fee_total_from_lines_usd"] - 16.20) < 1e-6


def test_parse_sale_fees_empty_or_missing_keys_returns_zero_not_crash():
    """Q0: 想定外 schema でも crash せず 0 で返す (silent skip にしない、log は呼び出し側)."""
    from monitor.ebay_client import parse_sale_fees
    # 完全に空
    out = parse_sale_fees({})
    assert out["order_id"] == ""
    assert out["amount_usd"] == 0.0
    assert out["total_fee_usd"] == 0.0
    assert out["line_items"] == []
    assert out["fees_by_type"] == {}
    # 部分欠落: amount/total はあるが marketplaceFees 無し
    txn = {
        "orderId": "O1",
        "amount": {"value": "50.00"},
        "totalFeeAmount": {"value": "0.00"},
        "orderLineItems": [
            {"itemId": "v1|999888777666|0", "marketplaceFees": []},
        ],
    }
    out = parse_sale_fees(txn)
    assert out["amount_usd"] == 50.0
    assert out["line_items"][0]["item_id"] == "999888777666"
    assert out["line_items"][0]["fees_by_type"] == {}


def test_parse_sale_fees_multi_line_items_aggregates_correctly():
    """1 order に複数商品 (line item) があるケース: fees_by_type は合算される."""
    from monitor.ebay_client import parse_sale_fees
    txn = {
        "orderId": "O-MULTI",
        "amount": {"value": "200.00"},
        "totalFeeAmount": {"value": "20.00"},
        "orderLineItems": [
            {
                "itemId": "111111111111",
                "marketplaceFees": [
                    {"feeType": "FINAL_VALUE_FEE", "amount": {"value": "8.00"}},
                ],
            },
            {
                "itemId": "222222222222",
                "marketplaceFees": [
                    {"feeType": "FINAL_VALUE_FEE", "amount": {"value": "10.00"}},
                    {"feeType": "AD_FEE", "amount": {"value": "2.00"}},
                ],
            },
        ],
    }
    out = parse_sale_fees(txn)
    assert out["fees_by_type"]["FINAL_VALUE_FEE"] == 18.00
    assert out["fees_by_type"]["AD_FEE"] == 2.00
    assert out["fee_total_from_lines_usd"] == 20.00
    assert len(out["line_items"]) == 2


# ─── get_transactions: pagination + auth error 経路 ─────────────────

def _mock_resp(status: int, body):
    """軽量 mock. httpx.Response 互換 (status_code / json / text / .text)."""
    r = MagicMock()
    r.status_code = status
    if isinstance(body, dict):
        r.json.return_value = body
        import json as _json
        r.text = _json.dumps(body)
    else:
        r.json.side_effect = ValueError("not json")
        r.text = str(body)
    return r


def test_get_transactions_paginates_until_short_page():
    """limit=2 で 2 ページ目が 1 件 (=limit 未満) → 終了. 合計 3 件取得."""
    from monitor import ebay_client as ec
    client = MagicMock()
    client.get.side_effect = [
        _mock_resp(200, {
            "transactions": [
                {"transactionId": "A"},
                {"transactionId": "B"},
            ],
            "total": 3,
        }),
        _mock_resp(200, {
            "transactions": [{"transactionId": "C"}],
            "total": 3,
        }),
    ]
    with patch.object(ec.httpx, "Client", return_value=client):
        out = ec.get_transactions(
            "2026-05-01T00:00:00.000Z",
            "2026-05-02T00:00:00.000Z",
            limit=2,
            access_token="TOKEN_OVERRIDE",
        )
    assert out["success"] is True
    assert out["fetched"] == 3
    assert out["pages"] == 2
    assert out["truncated"] is False
    assert out["errors"] == []
    assert [t["transactionId"] for t in out["transactions"]] == ["A", "B", "C"]


def test_get_transactions_401_auth_fail_not_silent_success():
    """Q0: scope 未 consent / token 失効 で空成功を返さない."""
    from monitor import ebay_client as ec
    client = MagicMock()
    client.get.return_value = _mock_resp(401, '{"error":"invalid_token"}')
    with patch.object(ec.httpx, "Client", return_value=client):
        out = ec.get_transactions(
            "2026-05-01T00:00:00.000Z",
            "2026-05-02T00:00:00.000Z",
            limit=200,
            access_token="BAD_TOKEN",
        )
    assert out["success"] is False
    assert out["last_status"] == 401
    assert out["fetched"] == 0
    assert any("auth" in e.lower() or "401" in e for e in out["errors"])


def test_get_transactions_429_rate_limit_returns_partial_not_fake_success():
    """Q0: rate limit 時、最初の 1 ページは保持しつつ success=False."""
    from monitor import ebay_client as ec
    client = MagicMock()
    client.get.side_effect = [
        _mock_resp(200, {
            "transactions": [
                {"transactionId": "A"},
                {"transactionId": "B"},
            ],
        }),
        _mock_resp(429, '{"errors":[{"message":"rate limit"}]}'),
    ]
    with patch.object(ec.httpx, "Client", return_value=client):
        out = ec.get_transactions(
            "2026-05-01T00:00:00.000Z",
            "2026-05-02T00:00:00.000Z",
            limit=2,
            access_token="TOKEN",
        )
    assert out["success"] is False
    assert out["last_status"] == 429
    # 1 ページ分は保持
    assert out["fetched"] == 2
    assert any("429" in e or "rate limit" in e.lower() for e in out["errors"])


def test_parse_non_sale_charge_promoted_listings_debit_with_item_ref():
    """W219 2026-06-03 実観測: NON_SALE_CHARGE = Promoted Listings fee."""
    from monitor.ebay_client import parse_non_sale_charge
    txn = {
        "transactionId": "FEE-7561359420110_11",
        "transactionType": "NON_SALE_CHARGE",
        "amount": {"value": "2.0", "currency": "USD"},
        "bookingEntry": "DEBIT",
        "transactionDate": "2026-06-02T21:10:35.277Z",
        "transactionMemo": "Promoted Listings - Priority fee",
        "feeType": "PREMIUM_AD_FEES",
        "references": [
            {"referenceId": "358212419810", "referenceType": "ITEM_ID"},
        ],
    }
    out = parse_non_sale_charge(txn)
    assert out["fee_type"] == "PREMIUM_AD_FEES"
    assert out["memo"] == "Promoted Listings - Priority fee"
    assert out["amount_usd_debit"] == 2.0
    assert out["amount_usd_credit"] == 0.0
    assert out["item_id"] == "358212419810"
    assert out["order_id_ref"] == ""


def test_parse_non_sale_charge_credit_entry_separates_amount():
    """CREDIT (eBay → seller への戻し) は debit=0 / credit に分けて記録."""
    from monitor.ebay_client import parse_non_sale_charge
    txn = {
        "transactionId": "X",
        "transactionType": "NON_SALE_CHARGE",
        "amount": {"value": "1.50", "currency": "USD"},
        "bookingEntry": "CREDIT",
        "feeType": "SOME_REFUND",
        "references": [],
    }
    out = parse_non_sale_charge(txn)
    assert out["amount_usd_debit"] == 0.0
    assert out["amount_usd_credit"] == 1.50
    assert out["item_id"] == ""


def test_get_transactions_no_token_returns_failure_not_empty_success():
    """access_token=None かつ get_valid_access_token も None なら success=False."""
    from monitor import ebay_client as ec
    with patch("monitor.ebay_oauth_refresh.get_valid_access_token",
               return_value=None):
        out = ec.get_transactions(
            "2026-05-01T00:00:00.000Z",
            "2026-05-02T00:00:00.000Z",
        )
    assert out["success"] is False
    assert out["fetched"] == 0
    assert any("token" in e.lower() for e in out["errors"])
