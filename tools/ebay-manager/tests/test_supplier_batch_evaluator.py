#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W94 Phase 4: supplier_batch_evaluator pytest (mock + 小規模 E2E).

Q0 4 段防御層 (no_api_key / submit_fail / hard_timeout / item_errored) を
全部カバー + cache_control 構造 (BP1 system / BP2 knowledge) を verify.

mock 粒度: `_get_client()` 関数 mock に統一 (anthropic SDK 全体 mock より surgical).
DB 依存: monkeypatch で `_build_past_judgments_block` を空 string 返却に短絡.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from monitor import supplier_batch_evaluator as sbe
from monitor.claude_evaluator import EvaluationResult


# ─── shared fixtures ───

def _make_item(custom_id: str = "eid1|mercari|0", knowledge_block: str = "") -> sbe.BatchItem:
    return sbe.BatchItem(
        custom_id=custom_id,
        ebay_title="KEYENCE XG-X2700 Image Sensor",
        candidate_title="KEYENCE XG-X2700",
        platform="mercari",
        price_jpy=50000,
        url="https://jp.mercari.com/item/m12345",
        ebay_image_url=None,
        candidate_image_url=None,
        sku="ebayme_m12345",
        ebay_item_id="eid1",
        knowledge_block=knowledge_block,
    )


@pytest.fixture(autouse=True)
def _stub_past_judgments(monkeypatch):
    """_build_past_judgments_block は DB 依存 → 全 test で空 string 短絡."""
    monkeypatch.setattr(sbe, "_build_past_judgments_block", lambda *a, **k: "")


def _mock_succeeded_result(custom_id: str, score: int = 90):
    """Anthropic batch result (succeeded type) の最小 mock."""
    msg = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text=f'{{"match_score": {score}, "reasoning": "test"}}',
        )],
        usage=SimpleNamespace(
            cache_read_input_tokens=100,
            cache_creation_input_tokens=10,
        ),
    )
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=msg),
    )


def _mock_errored_result(custom_id: str):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="errored", error="rate_limit"),
    )


# ─── 1. no_api_key → 全件 error (Q0 silent skip 防御) ───

def test_evaluate_batch_no_api_key_returns_errors_for_all(monkeypatch):
    monkeypatch.setattr(sbe, "_get_client", lambda: None)
    items = [_make_item(f"id{i}") for i in range(3)]

    result = sbe.evaluate_batch(items)

    assert result.errored == 3
    assert result.submitted == 3
    assert result.error_message == "ANTHROPIC_API_KEY not set"
    for r in result.results.values():
        assert r.match_score == 0
        assert r.error == "ANTHROPIC_API_KEY not set"


# ─── 2. submit failure → Tier 2 fallback (全件 realtime) ───

def test_evaluate_batch_submit_failure_triggers_realtime_fallback(monkeypatch):
    class _BoomClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests):
                    raise Exception("simulated submit failure")
    monkeypatch.setattr(sbe, "_get_client", lambda: _BoomClient())

    fallback_calls = []
    def _stub_fallback(item, model):
        fallback_calls.append(item.custom_id)
        return EvaluationResult(match_score=80, reasoning="fb ok")
    monkeypatch.setattr(sbe, "_fallback_to_realtime", _stub_fallback)

    items = [_make_item(f"id{i}") for i in range(2)]
    result = sbe.evaluate_batch(items)

    assert "submit failed" in (result.error_message or "")
    assert result.fallback_used == 2
    assert result.errored == 0
    assert len(fallback_calls) == 2
    # H-1 (5/3 incident): submit reject 後 realtime fallback で復旧した cid を Set に保持.
    # caller はこれで「batch ラベル」と「実 realtime 経路」の見分けが可能.
    assert result.fallback_custom_ids == {"id0", "id1"}


# ─── 3. hard_timeout → Tier 3 DLQ ───

def test_evaluate_batch_hard_timeout_persists_to_dlq(monkeypatch):
    class _SlowBatch:
        id = "msgbatch_slow"
        processing_status = "in_progress"
    class _SlowClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests):
                    return _SlowBatch()
                @staticmethod
                def retrieve(bid):
                    return _SlowBatch()
    monkeypatch.setattr(sbe, "_get_client", lambda: _SlowClient())
    monkeypatch.setattr(sbe.time, "sleep", lambda s: None)  # poll の時間を縮める

    persist_calls = []
    def _stub_persist(custom_ids, batch_id, reason):
        persist_calls.append((tuple(custom_ids), batch_id, reason))
        return len(custom_ids)
    monkeypatch.setattr(sbe, "_persist_pending", _stub_persist)

    items = [_make_item(f"id{i}") for i in range(2)]
    # hard_timeout=0 で 1 周目に即 timeout 判定させる
    result = sbe.evaluate_batch(items, hard_timeout_sec=0)

    assert result.timeout is True
    assert result.pending_dlq == 2
    assert result.errored == 2
    assert persist_calls and persist_calls[0][2] == "hard_timeout"


# ─── 4. mixed results: 1 件 succeeded + 1 件 errored → fallback ───

def test_evaluate_batch_errored_item_falls_back(monkeypatch):
    class _DoneBatch:
        id = "msgbatch_done"
        processing_status = "ended"

    succeeded_r = _mock_succeeded_result("id0", score=85)
    errored_r = _mock_errored_result("id1")

    class _OkClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests):
                    return _DoneBatch()
                @staticmethod
                def retrieve(bid):
                    return _DoneBatch()
                @staticmethod
                def results(bid):
                    return iter([succeeded_r, errored_r])
    monkeypatch.setattr(sbe, "_get_client", lambda: _OkClient())

    fallback_calls = []
    def _stub_fallback(item, model):
        fallback_calls.append(item.custom_id)
        return EvaluationResult(match_score=70, reasoning="fb")
    monkeypatch.setattr(sbe, "_fallback_to_realtime", _stub_fallback)

    items = [_make_item("id0"), _make_item("id1")]
    result = sbe.evaluate_batch(items)

    assert result.succeeded == 1
    assert result.fallback_used == 1
    assert result.pending_dlq == 0
    assert fallback_calls == ["id1"]
    assert result.results["id0"].match_score == 85
    assert result.results["id1"].match_score == 70
    # H-1: per-item errored で fallback した cid のみ集合に入る (succeeded は入らない).
    assert result.fallback_custom_ids == {"id1"}


# ─── 5. all succeeded → cache_read 集計 ───

def test_evaluate_batch_all_succeeded_aggregates_cache(monkeypatch):
    class _DoneBatch:
        id = "msgbatch_x"
        processing_status = "ended"

    res_iter = [_mock_succeeded_result(f"id{i}", score=90) for i in range(3)]

    class _OkClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests):
                    return _DoneBatch()
                @staticmethod
                def retrieve(bid):
                    return _DoneBatch()
                @staticmethod
                def results(bid):
                    return iter(res_iter)
    monkeypatch.setattr(sbe, "_get_client", lambda: _OkClient())

    items = [_make_item(f"id{i}") for i in range(3)]
    result = sbe.evaluate_batch(items)

    assert result.succeeded == 3
    assert result.errored == 0
    assert result.fallback_used == 0
    assert result.cache_read_total == 300  # 100 × 3
    assert result.cache_write_total == 30   # 10 × 3


# ─── 6. BP1: system に cache_control 1h あり ───

def test_build_batch_request_system_has_1h_cache_control():
    item = _make_item()
    req = sbe._build_batch_request(item, model="claude-opus-4-7")

    sys_block = req["params"]["system"][0]
    assert sys_block["type"] == "text"
    assert sys_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ─── 6b. Sonnet 5 移行 (2026-07-01): batch request に effort=high が入る ───

def test_build_batch_request_has_effort_high():
    """money-direct 仕入先評価 batch は output_config.effort=high (realtime と品質統一)."""
    item = _make_item()
    req = sbe._build_batch_request(item, model="claude-sonnet-5")
    assert req["params"]["output_config"] == {"effort": "high"}


# ─── 7. BP2: knowledge_block 有 → 別 text block + 1h cache ───

def test_build_user_content_with_kb_has_separate_cached_block():
    item = _make_item(knowledge_block="動画 KB: PIONEER は要動作確認")
    content = sbe._build_user_content(item)

    # 最後の text block が KB + cache_control
    kb_block = content[-1]
    assert kb_block["type"] == "text"
    assert kb_block["text"] == "動画 KB: PIONEER は要動作確認"
    assert kb_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ─── 8. BP2: knowledge_block 無 → cache_control 無 (単一 text block) ───

def test_build_user_content_no_kb_no_cache_control():
    item = _make_item(knowledge_block="")
    content = sbe._build_user_content(item)

    # knowledge_block 無し = 単一 text block のみ (cache_control なし)
    text_blocks = [c for c in content if c.get("type") == "text"]
    assert len(text_blocks) == 1
    assert "cache_control" not in text_blocks[0]


# ─── RT-2 (code-reviewer H-2): DLQ persist 失敗時に痕跡が残ること ───

def test_evaluate_batch_dlq_persist_failure_logged_via_persist_pending(monkeypatch, caplog):
    """_persist_pending が 0 を返す (= DLQ INSERT 全失敗) ケース.

    実装側 (_persist_pending L229-235) で logger.error する設計のため、本 test では
    BatchResult.pending_dlq=0 + pending_custom_ids 非空 + timeout=True を verify する.
    呼出側 (task_supplier_sweep) はこの組合せで Discord 通知を組み立てる前提.
    """
    class _SlowBatch:
        id = "msgbatch_dlq"
        processing_status = "in_progress"
    class _SlowClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests): return _SlowBatch()
                @staticmethod
                def retrieve(bid): return _SlowBatch()
    monkeypatch.setattr(sbe, "_get_client", lambda: _SlowClient())
    monkeypatch.setattr(sbe.time, "sleep", lambda s: None)
    # _persist_pending が 0 返却 (= DLQ 全失敗) を simulate
    monkeypatch.setattr(sbe, "_persist_pending", lambda ids, bid, reason: 0)

    items = [_make_item(f"id{i}") for i in range(2)]
    result = sbe.evaluate_batch(items, hard_timeout_sec=0)

    assert result.timeout is True
    assert result.pending_dlq == 0  # DLQ 失敗
    assert len(result.pending_custom_ids) == 2  # 痕跡は pending_custom_ids に残る
    assert "hard_timeout" in (result.error_message or "")


# ─── RT-3 (code-reviewer H-3): batch 全 errored + fallback 全成功 → success 等価 ───

def test_evaluate_batch_all_errored_but_all_fallback_succeeded_treats_as_success(monkeypatch):
    """batch の全件が errored だが realtime fallback が全件成功 → BatchResult.errored=0."""
    class _DoneBatch:
        id = "msgbatch_all_errored"
        processing_status = "ended"
    res_iter = [_mock_errored_result(f"id{i}") for i in range(3)]

    class _OkClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests): return _DoneBatch()
                @staticmethod
                def retrieve(bid): return _DoneBatch()
                @staticmethod
                def results(bid): return iter(res_iter)
    monkeypatch.setattr(sbe, "_get_client", lambda: _OkClient())

    def _stub_fallback(item, model):
        return EvaluationResult(match_score=80, reasoning="fb ok")
    monkeypatch.setattr(sbe, "_fallback_to_realtime", _stub_fallback)

    items = [_make_item(f"id{i}") for i in range(3)]
    result = sbe.evaluate_batch(items)

    # H-3 修正後: errored は fallback で復旧した分を控除
    assert result.fallback_used == 3
    assert result.errored == 0  # 全件 fallback 成功 → 実質エラー無し
    assert result.submitted == 3
    # task_supplier_sweep の success 判定: errored < submitted AND not timeout AND dlq=0 → True
    assert (
        result.errored < result.submitted
        and not result.timeout
        and result.pending_dlq == 0
    )


# ─── H-5 verify: batch 成功時に api_call_log 記録される ───

def test_evaluate_batch_succeeded_logs_to_api_call_log(monkeypatch):
    """batch result の succeeded 1 件 = api_call_log 1 row 記録 (verify 経路)."""
    class _DoneBatch:
        id = "msgbatch_log"
        processing_status = "ended"
    res_iter = [_mock_succeeded_result(f"id{i}", score=90) for i in range(2)]

    class _OkClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests): return _DoneBatch()
                @staticmethod
                def retrieve(bid): return _DoneBatch()
                @staticmethod
                def results(bid): return iter(res_iter)
    monkeypatch.setattr(sbe, "_get_client", lambda: _OkClient())

    log_calls = []
    def _stub_log(operation, model, response, **kw):
        log_calls.append((operation, model, kw.get("success", True)))
    import monitor.api_logger as api_logger_mod
    monkeypatch.setattr(api_logger_mod, "log_anthropic_response", _stub_log)

    items = [_make_item(f"id{i}") for i in range(2)]
    sbe.evaluate_batch(items)

    # batch 成功 2 件 → api_call_log 2 row
    assert len(log_calls) == 2
    assert all(op == "candidate_evaluate_batch" for op, _, _ in log_calls)
    assert all(success for _, _, success in log_calls)


# ─── 11. M-1: results iteration 自体が raise → 全件 realtime fallback ───

def test_evaluate_batch_results_fetch_raises_triggers_full_fallback(monkeypatch):
    """results 取得 iteration 自体が例外を投げたら全件 realtime fallback。

    network error / SDK bug 等で client.messages.batches.results(bid) の
    iteration が途中で raise した場合、supplier_batch_evaluator の except 節で
    errored_items を全件に再設定し、Tier 2 fallback で個別評価する経路を固定化。

    既存の "個別 item errored" テストとの違い: 結果を 1 件も受け取れない (iteration 開始即 raise).
    """
    class _DoneBatch:
        id = "msgbatch_done"
        processing_status = "ended"

    class _RaisingResults:
        def __iter__(self):
            return self

        def __next__(self):
            raise RuntimeError("simulated network failure during results fetch")

    class _BrokenClient:
        class messages:
            class batches:
                @staticmethod
                def create(requests):
                    return _DoneBatch()

                @staticmethod
                def retrieve(bid):
                    return _DoneBatch()

                @staticmethod
                def results(bid):
                    return _RaisingResults()

    monkeypatch.setattr(sbe, "_get_client", lambda: _BrokenClient())

    fallback_calls = []

    def _stub_fallback(item, model):
        fallback_calls.append(item.custom_id)
        return EvaluationResult(match_score=60, reasoning="full_fallback")

    monkeypatch.setattr(sbe, "_fallback_to_realtime", _stub_fallback)

    items = [_make_item(f"id{i}") for i in range(3)]
    result = sbe.evaluate_batch(items)

    # Tier 1 結果が消えて Tier 2 fallback で全件埋まる
    assert result.succeeded == 0
    assert result.fallback_used == 3
    assert result.pending_dlq == 0
    assert sorted(fallback_calls) == ["id0", "id1", "id2"]
    assert result.fallback_custom_ids == {"id0", "id1", "id2"}
    # 全 item が fallback 結果で埋まる
    assert all(result.results[cid].match_score == 60 for cid in ["id0", "id1", "id2"])
