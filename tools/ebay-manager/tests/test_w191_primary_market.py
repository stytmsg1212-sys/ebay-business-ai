# -*- coding: utf-8 -*-
"""W191: 個別出品の新規出品時に出品区分 (primary_market) を選択する機能の単体テスト。

検証対象:
  - _build_current_draft_params が STEP5 選択値 (sel_primary_market) を価格 / 送料に反映:
      us_only      → 商品価格に関税を包含 (price + price*0.20)、米国送料 override = 0
      global_only  → 価格そのまま、米国送料 override = 0
      mixed_global → 価格そのまま、米国送料 override = price*0.20 (関税近似)
      未選択 (None) → "unknown" 安全側 (mixed_global 等価) で preview 計算
  - _compose_draft_record が選択した区分を下書きに永続化する。
  - _reset_step4_edit_widgets が区分 selectbox の widget-state を破棄する (再生成=強制再選択)。
"""
from __future__ import annotations

import pytest

import tabs.tab_individual_listing as tab


_SS = tab._SS


class _FakeST:
    """_build_current_draft_params / _compose_draft_record が触る最小 st。"""

    def __init__(self, session_state: dict):
        self.session_state = session_state

    def error(self, *_a, **_k):
        return None

    def warning(self, *_a, **_k):
        return None


@pytest.fixture
def base_state() -> dict:
    return {
        f"{_SS}scraped_product": {"image_urls": ["https://img/1.jpg"], "url": "https://supplier/x"},
        f"{_SS}reference_listing": None,
        f"{_SS}generated_listing": {
            "ebay_title": "Original Title",
            "ebay_description": "<p>BODY</p>",
            "ebay_category_id": "9355",
            "ebay_category_name": "Cell Phones",
            "item_specifics": {"Brand": "Sony", "MPN": "ABC-123"},
        },
        f"{_SS}rank_classification": {
            "rank_code": "A",
            "rank_label": "Excellent",
            "ebay_condition_id": "3000",
        },
        f"{_SS}selected_category_id": "9355",
        f"{_SS}selected_condition_id": "3000",
        f"{_SS}edited_title": "",
        f"{_SS}edited_description": "",
        f"{_SS}edited_item_specifics": None,
        f"{_SS}processed_image_urls": [],
        f"{_SS}selected_image_urls": ["https://img/1.jpg"],
        f"{_SS}sku": "ebayyh_p123",
        f"{_SS}price_usd": 100.0,
        f"{_SS}weight_g": 0,
        f"{_SS}in_stock": False,
        f"{_SS}selected_template_id": None,
        f"{_SS}reference_url": None,
        # sel_primary_market は意図的に未設定 (未選択 = 強制選択の既定)
    }


def test_unselected_market_defaults_unknown(monkeypatch, base_state):
    """未選択なら preview は "unknown" (安全側: 商品代そのまま + 送料に関税近似)。"""
    monkeypatch.setattr(tab, "st", _FakeST(base_state))
    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})
    assert params is not None
    assert params["primary_market"] == "unknown"
    assert params["listing_price_usd"] == 100.0
    assert params["shipping_cost_usd_override"] == 20.0


def test_us_only_includes_tariff_in_price(monkeypatch, base_state):
    """us_only は商品価格に関税を包含し、米国送料 override は 0。"""
    base_state[f"{_SS}sel_primary_market"] = "us_only"
    monkeypatch.setattr(tab, "st", _FakeST(base_state))
    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})
    assert params is not None
    assert params["primary_market"] == "us_only"
    assert params["listing_price_usd"] == 120.0
    assert params["shipping_cost_usd_override"] == 0.0


def test_global_only_no_tariff(monkeypatch, base_state):
    """global_only は価格そのまま + 米国送料 override 0 (関税上乗せなし)。"""
    base_state[f"{_SS}sel_primary_market"] = "global_only"
    monkeypatch.setattr(tab, "st", _FakeST(base_state))
    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})
    assert params is not None
    assert params["primary_market"] == "global_only"
    assert params["listing_price_usd"] == 100.0
    assert params["shipping_cost_usd_override"] == 0.0


def test_mixed_global_shipping_tariff(monkeypatch, base_state):
    """mixed_global は価格そのまま + 米国送料に関税近似 (price*0.20)。"""
    base_state[f"{_SS}sel_primary_market"] = "mixed_global"
    monkeypatch.setattr(tab, "st", _FakeST(base_state))
    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})
    assert params is not None
    assert params["primary_market"] == "mixed_global"
    assert params["listing_price_usd"] == 100.0
    assert params["shipping_cost_usd_override"] == 20.0


def test_compose_draft_record_persists_market(monkeypatch, base_state):
    """選択した区分が listing_drafts 保存用 record に載る。"""
    base_state[f"{_SS}sel_primary_market"] = "us_only"
    monkeypatch.setattr(tab, "st", _FakeST(base_state))
    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})
    record = tab._compose_draft_record(params, status="submitted")
    assert record["primary_market"] == "us_only"
    # us_only は price に関税包含した値が保存される
    assert record["listing_price_usd"] == 120.0


def test_reset_step4_widgets_pops_market(monkeypatch):
    """再生成 / ドラフト読込で生成結果に連動する widget-state が全て破棄される。

    本文 / 項目 / 区分に加え、タイトル text_area (input_edited_title) と
    本番出品の最終確認 checkbox (chk_confirm_production) も破棄されること。
    前者は別商品のタイトルで出品する事故、後者は前商品の確認チェックのまま
    即時公開する事故 (Codex HIGH) を防ぐ。"""
    state = {
        f"{_SS}input_edited_description": "<p>stale</p>",
        f"{_SS}dataeditor_item_specifics": [{"x": 1}],
        f"{_SS}input_edited_title": "Stale Title",
        f"{_SS}chk_confirm_production": True,
        f"{_SS}sel_primary_market": "us_only",
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._reset_step4_edit_widgets()
    assert f"{_SS}sel_primary_market" not in state
    assert f"{_SS}input_edited_description" not in state
    assert f"{_SS}dataeditor_item_specifics" not in state
    assert f"{_SS}input_edited_title" not in state
    assert f"{_SS}chk_confirm_production" not in state


def test_clear_from_step_resets_edit_widgets(monkeypatch):
    """HIGH-1/HIGH-3 回帰: URL 変更 / STEP3 変更等で呼ばれる _clear_from_step(1) が
    編集系 widget-state (本文 / 項目 / 出品区分) を確実に破棄する。
    区分が残留すると別商品に前商品の関税計算が漏れる (money-direct 事故)。"""
    state = {
        f"{_SS}sel_primary_market": "us_only",
        f"{_SS}input_edited_description": "<p>stale</p>",
        f"{_SS}dataeditor_item_specifics": [{"x": 1}],
        f"{_SS}input_edited_title": "Stale Title",
        f"{_SS}chk_confirm_production": True,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._clear_from_step(1)
    assert f"{_SS}sel_primary_market" not in state
    assert f"{_SS}input_edited_description" not in state
    assert f"{_SS}dataeditor_item_specifics" not in state
    assert f"{_SS}input_edited_title" not in state
    assert f"{_SS}chk_confirm_production" not in state


def test_load_draft_invalid_market_falls_back_to_none(monkeypatch):
    """HIGH-2 回帰: 選択肢に無い保存値は None (強制再選択) に倒す。
    読込は STEP5 selectbox 生成後にも呼ばれ得るので widget key を直接代入せず
    pending キーに退避する (生成済み widget への代入例外を回避)。"""
    state: dict = {f"{_SS}sel_primary_market": "us_only"}
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._load_draft_into_form({"id": 1, "primary_market": "bogus_market"})
    # widget key は読込時に pop されるだけ (直接代入しない)
    assert f"{_SS}sel_primary_market" not in state
    assert state[f"{_SS}pending_primary_market"] is None


def test_load_draft_valid_market_restored(monkeypatch):
    """正しい保存値は pending キーに退避され、STEP5 描画で復元される。"""
    state: dict = {}
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._load_draft_into_form({"id": 2, "primary_market": "global_only"})
    assert state[f"{_SS}pending_primary_market"] == "global_only"


def test_load_draft_null_market_forces_reselect(monkeypatch):
    """旧ドラフト (primary_market 未保存=None) は None → 強制再選択。"""
    state: dict = {}
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._load_draft_into_form({"id": 3})
    assert state[f"{_SS}pending_primary_market"] is None


def test_validate_primary_market_pure():
    """_validate_primary_market: 有効値は素通し、無効 / None は None。"""
    assert tab._validate_primary_market("us_only") == "us_only"
    assert tab._validate_primary_market("global_only") == "global_only"
    assert tab._validate_primary_market("bogus_market") is None
    assert tab._validate_primary_market(None) is None
    assert tab._validate_primary_market("") is None
