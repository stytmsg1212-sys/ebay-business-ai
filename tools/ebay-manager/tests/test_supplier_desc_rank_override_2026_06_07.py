#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先/URL直接 description 生成で、手動指定ランクが AI 判定で上書きされない回帰テスト。

出典: 2026-06-07 user repro (item 358274830101): 商品エディタで状態=B にしたのに
URL直接生成で AI 判定 C に上書きされ修正不能。根治: rank_override_code を渡すと
その rank が generate_listing に渡る (AI 再判定しない) ことを固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tabs import _supplier_description_pipeline as pipe  # noqa: E402


def _fake_product():
    return SimpleNamespace(
        title_ja="テスト商品", platform="amazon", url="http://x",
        price_jpy=1000, condition_ja=None, includes_ja=None,
        weight_hint_g=None, description_ja=None, image_urls=[],
    )


def _fake_generated():
    return SimpleNamespace(
        ebay_description="<div>desc</div>", title_en="Test", generate_error=None,
    )


def _run(rank_override_code):
    """generate_supplier_description を実行し、generate_listing に渡された
    rank.rank_code を返す。"""
    captured = {}

    def _fake_generate_listing(*a, **kw):
        captured["rank_code"] = kw["rank"].rank_code
        return _fake_generated()

    with patch("monitor.listing_generator.generate_listing",
               side_effect=_fake_generate_listing), \
         patch("monitor.database.get_description_templates",
               return_value=[{"id": 1, "is_default": 1}]), \
         patch("monitor.database.get_description_template",
               return_value={"body": "<div>{{product_name}}</div>"}):
        res = pipe.generate_supplier_description(
            candidate_id=0,
            candidate_url="http://x",
            in_stock=False,
            prefetched_product=_fake_product(),
            rank_override_code=rank_override_code,
        )
    return res, captured


class TestRankOverrideHonored:
    def test_manual_rank_b_used_not_ai(self):
        """rank_override_code='B' → generate_listing に rank_code='B' が渡る
        (AI classify_rank は呼ばれない = 上書きされない)。"""
        with patch("monitor.rank_classifier.classify_rank") as mock_classify:
            res, captured = _run("B")
        assert res["success"] is True
        assert captured["rank_code"] == "B", "手動ランクBが渡っていない (AI上書きの疑い)"
        assert res["rank_code"] == "B"
        mock_classify.assert_not_called()

    def test_none_falls_back_to_ai(self):
        """rank_override_code=None → classify_rank (AI判定) にフォールバック。"""
        ai_rank = SimpleNamespace(
            rank_code="C", rank_label="Fair", rank_jp="使用感あり",
            ebay_condition_id="3000", confidence=0.8, reasoning="ai",
        )
        with patch("monitor.rank_classifier.classify_rank", return_value=ai_rank) as mock_classify:
            res, captured = _run(None)
        assert captured["rank_code"] == "C"
        mock_classify.assert_called_once()
