#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""URL 無し description AI 生成 (2026-07-04 user 恒久仕様) の回帰テスト。

出典: user 追加仕様 (バグ1 と同じ Description AI 生成まわり):
1. 引用元 URL が無くても「description に入れたい文言・指示」があれば生成できる
   (既存 listing 情報を代替コンテキストとして使う)。
2. 両方ある場合は両方を使う。
3. 矛盾時は「description に入れたい文言・指示」を優先 (プロンプトで明示)。
4. 両方空の場合のみ「URL か指示のどちらかを入力してください」と案内する。

3 モードを固定する回帰テスト:
    (a) URL のみ → 生成可 (従来通り)
    (b) 指示のみ → 生成可 (URL 必須エラーが出ない)
    (c) 両方     → プロンプトに両方 + 優先順位文言が含まれる
    (d) 両方空   → エラー (URL か指示のどちらかを入力してくださいと案内)
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


def _fake_scraped_product():
    return SimpleNamespace(
        title_ja="テスト商品 (スクレイプ)", platform="mercari", url="http://x",
        price_jpy=1000, condition_ja="美品", includes_ja=None,
        weight_hint_g=None, description_ja=None, image_urls=[],
    )


def _fake_generated():
    return SimpleNamespace(
        ebay_description="<div>desc</div>", title_en="Test", generate_error=None,
        item_specifics={}, condition_description="Tested and fully working.",
    )


def _patched(fake_generate_listing=None, product=None):
    """generate_listing / templates / (必要なら) resolve_product_from_url を mock する共通 context."""
    fake_gl = fake_generate_listing or (lambda *a, **kw: _fake_generated())
    patches = [
        patch("monitor.listing_generator.generate_listing", side_effect=fake_gl),
        patch("monitor.database.get_description_templates",
              return_value=[{"id": 1, "is_default": 1}]),
        patch("monitor.database.get_description_template",
              return_value={"body": "<div>{{product_name}}</div>"}),
    ]
    if product is not None:
        patches.append(
            patch("monitor.product_resolver.resolve_product_from_url", return_value=product)
        )
    return patches


def _apply_all(patches):
    for p in patches:
        p.start()
    return patches


def _stop_all(patches):
    for p in patches:
        p.stop()


# ─────────────────────────────────────────────────
# (a) URL のみ → 生成可 (従来通り、回帰確認)
# ─────────────────────────────────────────────────

def test_url_only_generation_still_works():
    captured = {}

    def _fake_generate_listing(*a, **kw):
        captured["extra_instructions"] = kw.get("extra_instructions")
        return _fake_generated()

    patches = _patched(_fake_generate_listing, product=_fake_scraped_product())
    _apply_all(patches)
    try:
        res = pipe.generate_supplier_description(
            candidate_id=0, candidate_url="http://x", in_stock=False,
        )
    finally:
        _stop_all(patches)

    assert res["success"] is True
    assert captured["extra_instructions"] is None


# ─────────────────────────────────────────────────
# (b) 指示のみ → 生成可 (URL 必須エラーが出ない)
# ─────────────────────────────────────────────────

def test_instructions_only_generation_succeeds_without_url():
    captured = {}

    def _fake_generate_listing(*a, **kw):
        captured["product"] = kw.get("product") if "product" in kw else a[0]
        captured["extra_instructions"] = kw.get("extra_instructions")
        return _fake_generated()

    # resolve_product_from_url は呼ばれてはいけない (URL が空のため)
    resolve_calls = []
    patches = [
        patch("monitor.listing_generator.generate_listing", side_effect=_fake_generate_listing),
        patch("monitor.database.get_description_templates",
              return_value=[{"id": 1, "is_default": 1}]),
        patch("monitor.database.get_description_template",
              return_value={"body": "<div>{{product_name}}</div>"}),
        patch("monitor.product_resolver.resolve_product_from_url",
              side_effect=lambda *a, **kw: resolve_calls.append(1) or _fake_scraped_product()),
    ]
    _apply_all(patches)
    try:
        res = pipe.generate_supplier_description(
            candidate_id=0,
            candidate_url="",
            in_stock=False,
            rank_override_code="A",
            extra_instructions="ギフト包装対応可と必ず書いて",
            existing_listing_context={
                "title": "Existing Listing Title",
                "condition_rank": "A",
                "listing_description": "old description text",
            },
        )
    finally:
        _stop_all(patches)

    assert res["success"] is True, res.get("message")
    assert resolve_calls == [], "URL 空なら resolve_product_from_url は呼ばれてはいけない"
    assert captured["extra_instructions"] == "ギフト包装対応可と必ず書いて"


def test_instructions_only_uses_existing_listing_context_as_product():
    """URL 無し時、existing_listing_context の title が product.title_ja に載ること."""
    from tabs._supplier_description_pipeline import _build_context_only_product
    product = _build_context_only_product({
        "title": "Existing Listing Title", "condition_rank": "B",
        "listing_description": "old desc",
    })
    assert product.title_ja == "Existing Listing Title"
    assert product.condition_ja == "B"
    assert product.description_ja == "old desc"
    assert product.url == ""


def test_context_only_product_handles_none_context():
    from tabs._supplier_description_pipeline import _build_context_only_product
    product = _build_context_only_product(None)
    assert product.title_ja is None
    assert product.condition_ja is None


# ─────────────────────────────────────────────────
# (c) 両方 → プロンプトに両方 + 優先順位文言が含まれる
# ─────────────────────────────────────────────────

def test_both_url_and_instructions_prompt_includes_priority_wording():
    from monitor.listing_generator import _compose_user_prompt

    class _Rank:
        rank_code, rank_label, rank_jp = "A", "Excellent", "Tested"
        ebay_condition_id, confidence, reasoning = "3000", 0.9, "ok"

    product = _fake_scraped_product()
    prompt = _compose_user_prompt(
        product, None, _Rank(),
        extra_instructions="バンドル品である点を強調",
    )
    # 両方の情報がプロンプトに含まれる
    assert "テスト商品 (スクレイプ)" in prompt  # 仕入先商品情報 (URL 由来)
    assert "バンドル品である点を強調" in prompt  # 追加指示
    # 矛盾時優先の明示指示
    assert "優先すること" in prompt


# ─────────────────────────────────────────────────
# (d) 両方空 → エラー (state 層のゲート)
# ─────────────────────────────────────────────────

def test_generate_description_via_ai_both_empty_returns_guidance_error():
    from tabs._finishing_panel_state import generate_description_via_ai
    res = generate_description_via_ai("", extra_instructions=None)
    assert res["success"] is False
    assert "URL" in res["message"]
    assert "文言" in res["message"] or "指示" in res["message"]


def test_generate_description_via_ai_extra_only_does_not_early_reject(monkeypatch):
    """URL 空でも extra_instructions があれば state 層のガードで弾かれないこと."""
    from tabs._finishing_panel_state import generate_description_via_ai
    import tabs._supplier_description_pipeline as pipe_mod

    captured = {}

    def _fake_generate(**kw):
        captured.update(kw)
        return {
            "success": True, "description_html": "<div>x</div>", "rank_code": "A",
            "title_en": "T", "item_specifics": {}, "condition_description": "ok",
            "message": "生成完了",
        }

    monkeypatch.setattr(pipe_mod, "generate_supplier_description", _fake_generate)

    res = generate_description_via_ai(
        "", extra_instructions="必ず書いて", existing_listing_context={"title": "X"},
    )
    assert res["success"] is True
    assert captured["candidate_url"] == ""
    assert captured["extra_instructions"] == "必ず書いて"
    assert captured["existing_listing_context"] == {"title": "X"}
