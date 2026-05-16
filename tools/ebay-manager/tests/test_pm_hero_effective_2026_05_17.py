"""W137後 (2026-05-17): hero metrics ライブ試算 _hero_effective 回帰.

user 報告: 商品価格を変えても赤枠 (現在総額/損益分岐/現在粗利) が最新化
されない。原因: hero metrics が DB `p` の current_price/lp_breakeven_usd
のみ使用 (form 入力非反映)。修正: 編集フォーム入力 (st.session_state、
st.form は submit 時確定) を優先する純試算 (eBay/DB 非書込)。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def tpm(monkeypatch):
    import streamlit as st

    class _S(dict):
        pass
    ss = _S()
    monkeypatch.setattr(st, "session_state", ss)
    from tabs import tab_product_management as _tpm
    return _tpm, ss


_P = {
    "ebay_item_id": "ITEM1", "current_price": 72.22, "shipping_cost": 20.0,
    "lp_breakeven_usd": 65.41, "purchase_yen": 9000, "weight_g": 300,
    "length_cm": 10, "width_cm": 10, "height_cm": 10,
}


def test_no_form_input_uses_db(tpm):
    """session_state 空 → DB 値そのまま・preview False."""
    _tpm, _ = tpm
    e = _tpm._hero_effective(dict(_P))
    assert e["price"] == 72.22 and e["ship"] == 20.0
    assert e["be"] == 65.41 and e["preview"] is False


def test_form_price_ship_override_db(tpm):
    """form の商品価格/送料が DB を上書き・preview True (eBay/DB は書かない)."""
    _tpm, ss = tpm
    ss["pm_ebay_price_ITEM1"] = 65.0
    ss["pm_ebay_ship_ITEM1"] = 9.0
    e = _tpm._hero_effective(dict(_P))
    assert e["price"] == 65.0 and e["ship"] == 9.0
    assert e["preview"] is True
    # 現在総額 = 65 + 9 = 74 (DB 92.22 でない) を呼出側が使う


def test_form_cost_edit_recomputes_breakeven_live(tpm):
    """仕入価格/重量変更 → compute_breakeven_price_usd でライブ再試算."""
    _tpm, ss = tpm
    ss["pm_pyen_ITEM1"] = 4500
    ss["pm_weight_ITEM1"] = 180
    with patch.object(_tpm, "compute_breakeven_price_usd",
                      return_value=40.0) as m, \
         patch.object(_tpm, "_calc_settings", return_value={}):
        e = _tpm._hero_effective(dict(_P))
    m.assert_called_once()
    assert m.call_args.kwargs["purchase_yen"] == 4500.0
    assert m.call_args.kwargs["weight_g"] == 180.0
    assert e["be"] == 40.0 and e["preview"] is True


def test_breakeven_calc_exception_falls_back_to_db_be(tpm):
    """compute_breakeven_price_usd 例外 → DB の be に fallback (hero 非クラッシュ)."""
    _tpm, ss = tpm
    ss["pm_pyen_ITEM1"] = 4500
    ss["pm_weight_ITEM1"] = 180
    with patch.object(_tpm, "compute_breakeven_price_usd",
                      side_effect=RuntimeError("calc setup error")), \
         patch.object(_tpm, "_calc_settings", return_value={}):
        e = _tpm._hero_effective(dict(_P))
    assert e["be"] == 65.41          # DB fallback、例外を伝播しない


def test_form_price_zero_falls_back_no_false_preview(tpm):
    """f_price=0 (クリア) → price は DB fallback、preview False
    (DB 値表示なのに『試算プレビュー中』caption が出る不整合の防止)."""
    _tpm, ss = tpm
    ss["pm_ebay_price_ITEM1"] = 0.0
    e = _tpm._hero_effective(dict(_P))
    assert e["price"] == 72.22          # DB fallback
    assert e["preview"] is False        # 誤 preview にならない


def test_no_ebay_or_db_write(tpm):
    """純試算: get_conn / revise 系を一切呼ばない (整合性保持)."""
    _tpm, ss = tpm
    ss["pm_ebay_price_ITEM1"] = 65.0
    with patch.object(_tpm, "get_conn") as m_db:
        _tpm._hero_effective(dict(_P))
    m_db.assert_not_called()
