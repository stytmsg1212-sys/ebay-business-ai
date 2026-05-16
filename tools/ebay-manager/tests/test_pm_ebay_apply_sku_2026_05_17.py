"""2026-05-17: 商品管理「eBay 反映」SKU 未反映バグ修正の回帰テスト.

背景: item 356364841116 で SKU/送料を設定し eBay 反映押下も eBay 未反映。
根因: _apply_to_ebay が価格+送料のみ送り SKU を eBay へ送っていなかった
(revise_item_sku 未配線)。本テストは修正後の挙動を固定:
  - SKU 変更検出 (form 入力 vs current_sku=DB値)
  - sku-rules 準拠 (stock*/ebay** 以外は自動正規化せず抑止、Q0 痕跡明示)
  - 価格/送料 と SKU の独立 API・透明な部分報告 (偽装成功禁止)
加えて Codex finding 3/4 の ParseError ガード (revise_item_sku /
_call_trading_api) を固定。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# Part 1: _apply_to_ebay の SKU 配線 (tab_product_management → streamlit import)
# =============================================================================

@pytest.fixture
def tpm(monkeypatch):
    """streamlit session_state を dict mock して tab_product_management を import."""
    import streamlit as st

    class _FakeSession(dict):
        pass

    monkeypatch.setattr(st, "session_state", _FakeSession())
    from tabs import tab_product_management as _tpm
    return _tpm


_CREDS = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "v^T"}


def _patch_creds(tpm):
    return patch.object(tpm, "get_ebay_credentials", return_value=_CREDS)


def test_sku_changed_valid_stock_pushed(tpm):
    """SKU を有効な stock 系へ変更 → revise_item_sku が呼ばれる."""
    editing = {"new_ebay_price": 100.0, "sku": "stock:02"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": True,
                                    "message": "SKU 変更"}) as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    assert res["success"] is True
    m_sku.assert_called_once()
    assert m_sku.call_args[0][1] == "stock:02"
    assert "SKU" in res["message"]


def test_sku_changed_offspec_uppercase_suppressed(tpm):
    """大文字 'STOCK' は off-spec → 自動正規化せず push 抑止 (sku-rules/Q0)."""
    editing = {"new_ebay_price": 100.0, "sku": "STOCK"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku") as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_sku.assert_not_called()
    assert res["success"] is False
    assert "規約外" in res["message"] and "抑止" in res["message"]


def test_sku_unchanged_not_pushed(tpm):
    """form SKU == current_sku → revise_item_sku を呼ばない."""
    editing = {"new_ebay_price": 100.0, "sku": "stock:01"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku") as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_sku.assert_not_called()
    assert res["success"] is True


def test_only_sku_changed_still_proceeds(tpm):
    """価格/送料なし・SKU のみ変更 → 早期 return せず SKU push."""
    editing = {"sku": "stock:99"}  # price/ship なし
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": True, "message": "ok"}) as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_ps.assert_not_called()           # 価格/送料 API は呼ばない
    m_sku.assert_called_once()
    assert res["success"] is True


def test_partial_price_ok_sku_fail_is_transparent(tpm):
    """価格/送料成功・SKU 失敗 → overall False かつ両方を透明報告 (Q0)."""
    editing = {"new_ebay_price": 100.0, "sku": "stock:02"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": False,
                                    "message": "API エラー: boom"}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    assert res["success"] is False
    assert "価格/送料" in res["message"] and "✅" in res["message"]
    assert "SKU" in res["message"] and "boom" in res["message"]


def test_partial_price_ok_sku_fail_returns_structured_flags(tpm):
    """HIGH-1 (2026-05-17): 価格/送料成功・SKU失敗時、構造化フラグ
    price_ship_ok=True / sku_ok=False を返す (handler が価格/送料の DB
    同期を分岐し eBay新価格/DB旧価格の永続乖離を防ぐ契約)."""
    editing = {"new_ebay_price": 100.0, "sku": "stock:02"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": False,
                                    "message": "API エラー: boom"}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    assert res["success"] is False
    assert res["price_ship_ok"] is True   # → handler が DB 同期する
    assert res["sku_ok"] is False


def test_offspec_sku_sets_sku_ok_false_price_ok_true(tpm):
    """off-spec SKU 抑止でも価格/送料成功なら price_ship_ok=True
    (handler が価格/送料 DB 同期 + SKU 警告に分岐できる)."""
    editing = {"new_ebay_price": 100.0, "sku": "STOCK"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "message": "px ok"}), \
         patch.object(tpm, "revise_item_sku") as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_sku.assert_not_called()
    assert res["success"] is False
    assert res["price_ship_ok"] is True
    assert res["sku_ok"] is False


def test_sku_only_success_price_not_attempted_flag_none(tpm):
    """SKU のみ変更・成功時、price_ship_ok は None (未試行) を返す."""
    editing = {"sku": "stock:99"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": True, "message": "ok"}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_ps.assert_not_called()
    assert res["success"] is True
    assert res["price_ship_ok"] is None
    assert res["sku_ok"] is True


def test_nothing_changed_returns_failure(tpm):
    """価格/送料/SKU いずれも変更なし → 明示 failure (silent skip 禁止)."""
    editing = {"sku": "stock:01"}
    with _patch_creds(tpm), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_item_sku") as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="stock:01")
    m_ps.assert_not_called()
    m_sku.assert_not_called()
    assert res["success"] is False
    assert "変更なし" in res["message"]


# =============================================================================
# Part 2: ParseError ガード (Codex finding 3/4、ebay_client = streamlit 非依存)
# =============================================================================

def _fake_resp(text: str):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock(return_value=None)
    return r


def test_revise_item_sku_parseerror_returns_false_not_raise():
    """HTTP200 + 非XML body で revise_item_sku は success:False を返す (no raise)."""
    import monitor.ebay_client as ec
    # 真に malformed (未閉じ root) で ET.fromstring を ParseError 化.
    # ※ '<html>x</html>' は well-formed XML なので別経路 (Unknown error) になる。
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
