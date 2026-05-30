# -*- coding: utf-8 -*-
"""W190: 個別出品タブの VerifyAdd 失敗時編集→再試行ロジックの単体テスト。

検証対象 = tab_individual_listing._build_current_draft_params:
  STEP4 で編集した Description 本文 (edited_description) と Item Specifics
  (edited_item_specifics) が draft_params に反映されること。
  未編集時 ("" / None) は生成結果がそのまま流れること (退行防止)。
"""
from __future__ import annotations

import types

import pytest

import tabs.tab_individual_listing as tab


_SS = tab._SS


class _FakeST:
    """_build_current_draft_params / _resolve_listing_image_urls が触る最小 st。"""

    def __init__(self, session_state: dict):
        self.session_state = session_state

    def error(self, *_a, **_k):  # noqa: D401 - no-op
        return None

    def warning(self, *_a, **_k):
        return None


@pytest.fixture
def base_state() -> dict:
    """生成済み listing を持つ session_state の最小セット。"""
    return {
        f"{_SS}scraped_product": {"image_urls": ["https://img/1.jpg"]},
        f"{_SS}reference_listing": None,
        f"{_SS}generated_listing": {
            "ebay_title": "Original Title",
            "ebay_description": "<p>ORIGINAL BODY</p>",
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
    }


def test_unedited_uses_generated_values(monkeypatch, base_state):
    """未編集 (edited_description='' / edited_item_specifics=None) なら生成結果が流れる。"""
    monkeypatch.setattr(tab, "st", _FakeST(base_state))

    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})

    assert params is not None
    assert params["ebay_description"] == "<p>ORIGINAL BODY</p>"
    assert params["item_specifics"] == {"Brand": "Sony", "MPN": "ABC-123"}


def test_edited_description_overrides(monkeypatch, base_state):
    """edited_description があれば draft_params の description を上書きする。"""
    base_state[f"{_SS}edited_description"] = "<p>EDITED BODY no improper words</p>"
    monkeypatch.setattr(tab, "st", _FakeST(base_state))

    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})

    assert params is not None
    assert params["ebay_description"] == "<p>EDITED BODY no improper words</p>"
    # 未編集の specifics は生成結果のまま
    assert params["item_specifics"] == {"Brand": "Sony", "MPN": "ABC-123"}


def test_edited_item_specifics_overrides(monkeypatch, base_state):
    """edited_item_specifics (dict) があれば draft_params の item_specifics を上書きする。"""
    base_state[f"{_SS}edited_item_specifics"] = {"Brand": "Sony", "Model": "WH-1000XM5"}
    monkeypatch.setattr(tab, "st", _FakeST(base_state))

    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})

    assert params is not None
    assert params["item_specifics"] == {"Brand": "Sony", "Model": "WH-1000XM5"}
    # 未編集の description は生成結果のまま
    assert params["ebay_description"] == "<p>ORIGINAL BODY</p>"


def test_edited_empty_specifics_dict_overrides(monkeypatch, base_state):
    """全削除して空 dict にした場合も dict なので上書きする (eBay 側で必須項目欠落を検出させる)。"""
    base_state[f"{_SS}edited_item_specifics"] = {}
    monkeypatch.setattr(tab, "st", _FakeST(base_state))

    params = tab._build_current_draft_params(shipping_policy_id="SP1", settings={})

    assert params is not None
    assert params["item_specifics"] == {}


def test_clear_from_step_resets_edit_fields(monkeypatch):
    """_clear_from_step(3) で edited_description='' / edited_item_specifics=None にリセット。"""
    state = {
        f"{_SS}rank_classification": {"x": 1},
        f"{_SS}generated_listing": {"y": 2},
        f"{_SS}selected_category_id": "9355",
        f"{_SS}selected_condition_id": "3000",
        f"{_SS}edited_title": "stale title",
        f"{_SS}edited_description": "<p>stale</p>",
        f"{_SS}edited_item_specifics": {"Brand": "stale"},
        f"{_SS}shipping_policy_id": "SP1",
        f"{_SS}shipping_policy_label": "",
        f"{_SS}verify_result": {"ack": "Failure"},
        f"{_SS}add_result": None,
        f"{_SS}current_draft_id": 5,
        f"{_SS}pl_result": None,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))

    tab._clear_from_step(3)

    assert state[f"{_SS}edited_description"] == ""
    assert state[f"{_SS}edited_item_specifics"] is None
    assert state[f"{_SS}edited_title"] == ""
    assert state[f"{_SS}generated_listing"] is None


def test_load_draft_seeds_pending_title(monkeypatch):
    """商品切替の表示ズレ回帰: ドラフト読込はタイトルを pending キーに退避する。
    STEP4 描画冒頭で widget key へ反映され、前商品のタイトルが残らない
    (別商品のタイトルで出品する事故を防ぐ)。"""
    state: dict = {}
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._load_draft_into_form({"id": 9, "ebay_title": "ADVANTEST R3273 Analyzer"})
    assert state[f"{_SS}pending_edited_title"] == "ADVANTEST R3273 Analyzer"


def test_reset_step4_pops_pending_title(monkeypatch):
    """再生成 / URL 変更 / クリアでタイトルの仕込み値も破棄される (前商品の残留防止)。"""
    state = {f"{_SS}pending_edited_title": "Stale Pending Title"}
    monkeypatch.setattr(tab, "st", _FakeST(state))
    tab._reset_step4_edit_widgets()
    assert f"{_SS}pending_edited_title" not in state
