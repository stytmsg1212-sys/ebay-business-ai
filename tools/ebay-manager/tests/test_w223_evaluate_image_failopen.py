#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W223 step1 HIGH-1 回帰テスト: 画像 URL fetch 失敗時の text-only degrade.

eBay/候補画像 URL が無効 (404/期限切れ) で Anthropic が画像取得に失敗すると
BadRequestError(400) になる。これを match_score=0 のまま返すと、呼出側
(task_supplier_candidate_search) が全候補を threshold 未満で skip し、その listing の
仕入先候補が 0 件化する silent 機能停止に陥る (eBay 画像は listing 共通 1 本 + 30 日
cache のため影響が広い)。本テストは「画像つき 400 → 画像を外して 1 回再評価」を固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _bad_request(msg: str = "could not fetch image from url"):
    resp = httpx.Response(
        400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return anthropic.BadRequestError(msg, response=resp, body=None)


def _ok_msg(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def _patch_common(monkeypatch):
    from monitor import claude_evaluator
    monkeypatch.setattr(claude_evaluator, "_rate_limit_wait", lambda *a, **k: None)
    return claude_evaluator


def test_image_fetch_error_degrades_to_text(monkeypatch):
    """画像つき 400 → 画像を外して再評価し、テキスト結果で復帰する。"""
    ce = _patch_common(monkeypatch)
    calls = []

    def _create(**kw):
        calls.append(kw)
        has_image = any(
            b.get("type") == "image" for b in kw["messages"][0]["content"]
        )
        if has_image:
            raise _bad_request()
        return _ok_msg('{"match_score": 82, "reasoning": "text-only ok"}')

    monkeypatch.setattr(
        ce, "_get_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )

    r = ce.evaluate_match(
        ebay_title="Sony WF-1000XM5",
        candidate_title="Sony WF-1000XM5",
        platform="mercari",
        price_jpy=18000,
        url="https://jp.mercari.com/item/x",
        ebay_image_url="https://i.ebayimg.com/INVALID.jpg",
        candidate_image_url=None,
        test_mode=True,
    )

    assert r.match_score == 82, "画像 fetch 失敗でもテキスト評価で復帰すべき"
    assert len(calls) == 2, "画像なしで 1 回だけ retry されるべき"
    # 1 回目は画像あり、2 回目は画像なし
    assert any(b.get("type") == "image" for b in calls[0]["messages"][0]["content"])
    assert not any(b.get("type") == "image" for b in calls[1]["messages"][0]["content"])


def test_text_only_400_does_not_retry(monkeypatch):
    """画像が無い request の 400 は retry せず match_score=0 (無限ループ防止)。"""
    ce = _patch_common(monkeypatch)
    calls = []

    def _create(**kw):
        calls.append(kw)
        raise _bad_request("prompt too long")

    monkeypatch.setattr(
        ce, "_get_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )

    r = ce.evaluate_match(
        ebay_title="X", candidate_title="X", platform="mercari",
        price_jpy=1000, url="https://jp.mercari.com/item/y",
        ebay_image_url=None, candidate_image_url=None, test_mode=True,
    )

    assert r.match_score == 0
    assert r.error is not None
    assert len(calls) == 1, "画像なしの 400 は retry しない"


def test_image_retry_still_failing_returns_zero(monkeypatch):
    """画像を外した再評価でも 400 が続くなら match_score=0 (1 回だけ retry)。"""
    ce = _patch_common(monkeypatch)
    calls = []

    def _create(**kw):
        calls.append(kw)
        raise _bad_request()

    monkeypatch.setattr(
        ce, "_get_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )

    r = ce.evaluate_match(
        ebay_title="X", candidate_title="X", platform="mercari",
        price_jpy=1000, url="https://jp.mercari.com/item/z",
        ebay_image_url="https://i.ebayimg.com/INVALID.jpg",
        candidate_image_url=None, test_mode=True,
    )

    assert r.match_score == 0
    assert len(calls) == 2, "1 回 retry した後は諦める (無限ループしない)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
