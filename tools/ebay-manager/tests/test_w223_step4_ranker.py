#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W223 step4: 一覧テキストスキャン ranker (supplier_candidate_ranker) のテスト.

検証:
  - 件数 <= max_keep は ranker を呼ばず全件 (API call なし)
  - API キー無し / API エラー / JSON parse 失敗 / keep 空 = 全件 fail-open (取りこぼし防止)
  - 正常時は keep_indices の subset を返す + 範囲外/非int を弾く
  - realtime/batch 統合: flag OFF=全件評価、flag ON=ranker 結果のみ vision 評価
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _fake_msg(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def _patch_client(monkeypatch, text):
    """ranker の anthropic.Anthropic() を fake 化し log_anthropic_response を no-op に。"""
    import monitor.supplier_candidate_ranker as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(r, "_ANTHROPIC_OK", True)

    def _create(**kw):
        return _fake_msg(text)
    monkeypatch.setattr(
        r.anthropic, "Anthropic",
        lambda *a, **k: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )
    monkeypatch.setattr("monitor.api_logger.log_anthropic_response",
                        lambda *a, **k: None, raising=False)


_CANDS = [{"title": f"item{i}", "price_jpy": 1000 + i} for i in range(5)]


def test_passthrough_when_few():
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    # n=2 <= max_keep=3 → 全件 (API 呼ばない)
    assert rank_candidates_for_vision("X", _CANDS[:2], max_keep=3) == [0, 1]


def test_no_api_key_failopen(monkeypatch):
    import monitor.supplier_candidate_ranker as r
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(r, "_ANTHROPIC_OK", True)
    # n=5 > max_keep=3 だが key 無し → 全件 fail-open
    assert r.rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4]


def test_selects_subset(monkeypatch):
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    _patch_client(monkeypatch, '{"keep_indices": [0, 2]}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 2]


def test_clamps_invalid_indices(monkeypatch):
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    # 範囲外(99)/負(-1)/非int("x")/重複(0) を弾き昇順 dedup + 上限
    _patch_client(monkeypatch, '{"keep_indices": [0, 99, -1, "x", 0, 3]}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 3]


def test_bool_indices_excluded_failopen(monkeypatch):
    """Haiku が {"keep_indices":[true,false]} を返しても bool は int 扱いしない。

    Python は bool が int サブクラスのため isinstance(True,int)=True で index 1 と
    誤認し他候補が消失する。type(i) is int で除外 → valid 空 → 全件 fail-open。
    """
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    _patch_client(monkeypatch, '{"keep_indices": [true, false]}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4]


def test_bool_mixed_with_valid_int(monkeypatch):
    """true(=1扱い回避) と有効 int 2 が混在 → 2 のみ採用 (bool は無視)。"""
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    _patch_client(monkeypatch, '{"keep_indices": [true, 2]}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [2]


def test_empty_keep_failopen(monkeypatch):
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    # Haiku が「全件無関係」と返しても取りこぼし回避で全件通す
    _patch_client(monkeypatch, '{"keep_indices": []}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4]


def test_non_list_keep_indices_failopen(monkeypatch):
    """keep_indices が非 list (null / 数値) でも TypeError で落とさず全件 fail-open。"""
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    for payload in ('{"keep_indices": null}', '{"keep_indices": 5}',
                    '{"keep_indices": "0,1"}'):
        _patch_client(monkeypatch, payload)
        assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4], payload


def test_parse_fail_failopen(monkeypatch):
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    _patch_client(monkeypatch, "これはJSONじゃない")
    assert rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4]


def test_api_error_failopen(monkeypatch):
    import monitor.supplier_candidate_ranker as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(r, "_ANTHROPIC_OK", True)

    def _boom(**kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(
        r.anthropic, "Anthropic",
        lambda *a, **k: SimpleNamespace(messages=SimpleNamespace(create=_boom)),
    )
    assert r.rank_candidates_for_vision("X", _CANDS, max_keep=3) == [0, 1, 2, 3, 4]


def test_max_keep_caps_result(monkeypatch):
    from monitor.supplier_candidate_ranker import rank_candidates_for_vision
    # Haiku が 4 件返しても max_keep=2 に切詰め
    _patch_client(monkeypatch, '{"keep_indices": [0, 1, 2, 3]}')
    assert rank_candidates_for_vision("X", _CANDS, max_keep=2) == [0, 1]


# ─── realtime 統合 ───

class _Hit:
    def __init__(self, url, title="cand", price=5000):
        self.source_platform = "mercari"
        self.url = url
        self.price_jpy = price
        self.title = title
        self.image_url = None


def _setup_realtime(monkeypatch, t, listing, hits, eval_calls):
    from monitor.database import init_db
    init_db()
    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(t, "load_settings", lambda: {})
    monkeypatch.setattr(t, "check_candidate_availability",
                        lambda url, **_k: {"status": "available", "signal": "m",
                                           "checked_at": "2026-06-05T00:00:00+00:00"})
    monkeypatch.setattr(t, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5: hits if plat == "mercari" else [])

    def _fake_eval(h, ebay_title, ebay_image_url=None, sku=None, ebay_item_id=None, **_k):
        eval_calls.append(h.url)
        return t.ScoredCandidate(hit=h, match_score=70, match_reasoning="ok")
    monkeypatch.setattr(t, "evaluate_candidate_with_claude", _fake_eval)
    monkeypatch.setattr(t, "_estimate_profit_for_candidate", lambda **k: (5000.0, 4000.0))
    monkeypatch.setattr(t, "check_supplier_candidate_profitable",
                        lambda profit_with_refund, purchase_yen, profit_without_refund=None: (True, {}))
    monkeypatch.setattr(t, "add_supplier_candidate", lambda **kw: 1)


def test_realtime_ranker_off_evaluates_all(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {"sku": "ebayme_a", "title": "Sony A", "current_price": 100.0,
               "ebay_item_id": "800", "source_url": None}
    hits = [_Hit(f"https://jp.mercari.com/item/m{i}", title=f"c{i}") for i in range(4)]
    eval_calls = []
    _setup_realtime(monkeypatch, t, listing, hits, eval_calls)
    r = t.run_supplier_candidate_search(
        ebay_item_id="800", sku="ebayme_a", config={},  # flag 無し = OFF
        platforms=["mercari"], discovered_via="test",
    )
    assert len(eval_calls) == 4, "ranker OFF は全件 vision 評価"
    assert r["ranked_out"] == 0


def test_realtime_ranker_on_limits_vision(monkeypatch):
    from tasks import task_supplier_candidate_search as t
    listing = {"sku": "ebayme_b", "title": "Sony B", "current_price": 100.0,
               "ebay_item_id": "801", "source_url": None}
    hits = [_Hit(f"https://jp.mercari.com/item/n{i}", title=f"c{i}") for i in range(4)]
    eval_calls = []
    _setup_realtime(monkeypatch, t, listing, hits, eval_calls)
    # ranker は index 0 のみ vision 対象に選ぶ
    monkeypatch.setattr(t, "rank_candidates_for_vision",
                        lambda ebay_title, cands, max_keep=3: [0])
    cfg = {"tasks_enabled": {"supplier_sweep": {"use_candidate_ranker": True,
                                                "candidate_ranker_max_keep": 3}}}
    r = t.run_supplier_candidate_search(
        ebay_item_id="801", sku="ebayme_b", config=cfg,
        platforms=["mercari"], discovered_via="test",
    )
    assert len(eval_calls) == 1, "ranker ON は選ばれた 1 件のみ vision 評価"
    assert r["ranked_out"] == 3


# ─── batch 統合 ───

def test_batch_ranker_limits_batch_items(monkeypatch):
    """sweep batch: ranker ON で batch に積む候補が絞り込まれる。"""
    from monitor.database import init_db
    from tasks import task_supplier_sweep as s
    import monitor.supplier_batch_evaluator as sbe
    from monitor.claude_evaluator import EvaluationResult
    init_db()

    listing = {"sku": "ebayme_q", "title": "Item Q", "current_price": 100.0,
               "ebay_item_id": "960", "source_url": None}
    hits = [_Hit(f"https://jp.mercari.com/item/q{i}", title=f"c{i}") for i in range(4)]

    monkeypatch.setattr(s, "_fetch_sweep_targets", lambda *a, **k: [("960", "ebayme_q")])
    monkeypatch.setattr(s, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(s, "get_ebay_image_url", lambda eid: None)
    monkeypatch.setattr(s, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5: hits if plat == "mercari" else [])
    monkeypatch.setattr(s, "check_candidate_availability",
                        lambda url, **_k: {"status": "available", "signal": "m",
                                           "checked_at": "2026-06-05T00:00:00+00:00"})
    monkeypatch.setattr(s, "_estimate_profit_for_candidate", lambda **k: (5000.0, 4000.0))
    monkeypatch.setattr(s, "check_supplier_candidate_profitable",
                        lambda profit_with_refund, purchase_yen, profit_without_refund=None: (True, {}))
    monkeypatch.setattr(s, "load_settings", lambda: {})
    monkeypatch.setattr(s, "add_supplier_candidate", lambda **kw: 1)
    # ranker は 1 件のみ vision 対象に絞る
    monkeypatch.setattr(s, "rank_candidates_for_vision",
                        lambda ebay_title, cands, max_keep=3: [0])

    submitted_counts = []

    def _fake_batch(items, **k):
        submitted_counts.append(len(items))
        from monitor.supplier_batch_evaluator import BatchResult
        res = {it.custom_id: EvaluationResult(match_score=80, reasoning="ok") for it in items}
        return BatchResult(batch_id="b", results=res, submitted=len(items))
    monkeypatch.setattr(sbe, "evaluate_batch", _fake_batch)

    cfg = {"tasks_enabled": {"supplier_sweep": {
        "use_batch_api": True, "min_batch_size": 1,
        "use_candidate_ranker": True, "candidate_ranker_max_keep": 3}}}
    r = s.run_supplier_sweep_batch(cfg)

    assert submitted_counts == [1], "ranker で batch 投入が 4→1 件に絞られる"
    assert r["ranked_out"] == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
