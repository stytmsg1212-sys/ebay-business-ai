#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W#33 v2 (2026-06-21): 商品管理 キーワード新着監視フォーム pre-fill 純関数の回帰テスト.

user が旧版 (site=yahoo 固定・価格設定なしの盲目 add) を「ゴミ」と却下 → 本物の
新規追加フォームに作り直した際の pre-fill ロジック (`_kw_prefill_values`) を固定する。

検証観点:
- 新規 (existing=None): keyword=商品タイトル先頭60字 / 下限なし=ON / 上限なし=OFF
  (本家 tab_keyword_watch._render_add_form と同じ既定、user 提示の画面と一致) / item_id=この商品
- 既存あり: その watch の値を復元。price_min/max が None または 0 は「なし」扱い
- ebay_item_id 欠落 watch は商品 eid に fallback
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tabs.tab_product_management import _kw_prefill_values  # noqa: E402

EID = "358702493295"


def test_prefill_new_uses_title_and_screenshot_defaults():
    long_title = "Astell&Kern A&norma SR35 High-Res DAP " * 3
    v = _kw_prefill_values(existing=None, title=long_title, eid=EID)
    assert v["keyword"] == long_title[:60]
    assert v["pmin"] == 0 and v["pmin_unset"] is True       # 下限なし=ON
    assert v["pmax"] == 0 and v["pmax_unset"] is False      # 上限なし=OFF (画面通り)
    assert v["memo"] == ""
    assert v["item_id"] == EID                              # 既定でこの商品


def test_prefill_existing_min_set_max_none():
    ex = {
        "id": 7, "keyword": "HIOKI DT4282",
        "price_min_jpy": 20000, "price_max_jpy": None,
        "memo": "採用後アクション", "ebay_item_id": "356894589481",
    }
    v = _kw_prefill_values(existing=ex, title="無関係タイトル", eid=EID)
    assert v["keyword"] == "HIOKI DT4282"
    assert v["pmin"] == 20000 and v["pmin_unset"] is False  # 下限あり
    assert v["pmax"] == 0 and v["pmax_unset"] is True       # 上限 None → なし
    assert v["memo"] == "採用後アクション"
    assert v["item_id"] == "356894589481"


def test_prefill_existing_both_set():
    ex = {
        "id": 8, "keyword": "KM5 HP1",
        "price_min_jpy": 5000, "price_max_jpy": 37000,
        "memo": "", "ebay_item_id": "357902623794",
    }
    v = _kw_prefill_values(existing=ex, title="t", eid=EID)
    assert v["pmin"] == 5000 and v["pmin_unset"] is False
    assert v["pmax"] == 37000 and v["pmax_unset"] is False


def test_prefill_price_zero_treated_as_unset():
    # DB に明示 0 が入った watch は「なし」扱い (_build_search_url が 0/None 同一視と一貫)
    ex = {
        "id": 1, "keyword": "k", "price_min_jpy": 0, "price_max_jpy": 0,
        "memo": None, "ebay_item_id": None,
    }
    v = _kw_prefill_values(existing=ex, title="t", eid=EID)
    assert v["pmin_unset"] is True and v["pmax_unset"] is True
    assert v["memo"] == ""           # None → "" 正規化
    assert v["item_id"] == EID       # ebay_item_id 欠落 → 商品 eid に fallback
