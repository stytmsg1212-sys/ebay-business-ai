"""2026-05-17: ParseError ガード回帰 (Codex finding 3/4).

※ _apply_to_ebay の SKU 配線/部分成功テストは W137 再設計 (pre/post
GetItem snapshot ベース) に伴い `test_w137_apply_to_ebay_2026_05_17.py`
へ移行。本ファイルは ebay_client の ParseError ガード (streamlit 非依存・
W137 再設計の影響を受けない) のみを保持する。

- revise_item_sku / _call_trading_api の ET.fromstring が HTTP200+非XML body
  で ParseError を関数外へ伝播し UI/scheduler をクラッシュさせない
  (success:False で graceful、revise_inventory_quantity の F5 と同型)。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_resp(text: str):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock(return_value=None)
    return r


def test_revise_item_sku_parseerror_returns_false_not_raise():
    """HTTP200 + 真に malformed XML body で success:False (no raise).

    ※ '<html>x</html>' は well-formed XML なので別経路 (Unknown error) に
    なる。未閉じ root で確実に ET.fromstring を ParseError 化する。
    """
    import monitor.ebay_client as ec
    with patch.object(ec, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(ec.httpx, "post",
                      return_value=_fake_resp("<html><body>maintenance")):
        res = ec.revise_item_sku("ITEM1", "stock:02", "A", "D", "C", "v^T")
    assert res["success"] is False
    assert "parse" in res["message"].lower()


def test_call_trading_api_parseerror_returns_false_not_raise():
    """共有ラッパ _call_trading_api も非XML body で success:False (no raise)."""
    import monitor.ebay_client as ec
    with patch.object(ec, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(ec.httpx, "post",
                      return_value=_fake_resp("502 Bad Gateway")):
        res = ec._call_trading_api(
            "ReviseFixedPriceItem", "<x/>", "A", "D", "C", "v^T",
        )
    assert res["success"] is False
    assert "parse" in res["message"].lower()
    assert res.get("raw") == "502 Bad Gateway"
