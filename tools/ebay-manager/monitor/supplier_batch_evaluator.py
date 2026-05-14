#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W94: 仕入先候補 batch 評価 facade.

`monitor.claude_evaluator.evaluate_match` のリアルタイム単件評価に対し、
N 件の評価リクエストを Anthropic Message Batches API で一括処理する。

設計核心:
  - **既存 evaluate_match は無改変** (W86 A/B test scripts 後方互換維持).
    `_SYSTEM_PROMPT` / `DYNAMIC_PROMPT_TEMPLATE` / `_parse_response` /
    `_build_past_judgments_block` を import 再利用 (E3 facade 設計).
  - **50% off** + **1h prompt cache stack** で月 cost 55-60% 削減目標.
  - **3-tier fallback**: batch 内 errored → 通常 API retry → DLQ
    (`supplier_eval_pending` table) → Discord 通知.

呼出経路:
  - ① supplier_sweep (02:00 daily): targets >= min_batch_size なら本 module、
    未満なら通常 API for-loop.
  - ② inventory_check Pattern 1 daemon: ~5 件/listing で min_batch_size 未満
    が大半 → 当面 batch 化対象外 (config flag default False).
  - ③ MonoDeck UI 手動実行: 強制 OFF (本 module を呼ばない).

詳細: `data/system_improvements.json` id=181 / W94 entry.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from monitor.claude_evaluator import (
    CLAUDE_MODEL,
    DYNAMIC_PROMPT_TEMPLATE,
    EvaluationResult,
    _SYSTEM_PROMPT,
    _build_past_judgments_block,
    _parse_response,
)

logger = logging.getLogger(__name__)


# Batch API SLA: 公式 24h、実測大半 1h 以内。hard timeout は config から上書き可。
_DEFAULT_POLL_INTERVAL_SEC = 60
_DEFAULT_HARD_TIMEOUT_SEC = 4 * 3600  # 4h. SLA 超過 sentinel として扱い、未完了は DLQ.

# cache_control の TTL. 1h ephemeral で write cost 2x だが 100 件 batch で十分回収.
# (5min default だと 60 件目以降で cache 失効リスク).
_CACHE_TTL_1H = "1h"

# batch 化のメリット閾値. min 未満は overhead が割引を上回るので呼出側で通常 API に流す.
DEFAULT_MIN_BATCH_SIZE = 10


@dataclass
class BatchItem:
    """1 件分の評価入力. evaluate_match と同等のフィールド + custom_id 用 key."""
    custom_id: str  # 戻り値 mapping 用. caller が付与 (例: f"{eid}-{plat}-{idx}").
    ebay_title: str
    candidate_title: str
    platform: str
    price_jpy: Optional[int]
    url: str
    ebay_image_url: Optional[str] = None
    candidate_image_url: Optional[str] = None
    sku: Optional[str] = None
    ebay_item_id: Optional[str] = None
    knowledge_block: str = ""  # 呼出側で find_related_knowledge 済の整形済 text


@dataclass
class BatchResult:
    """batch 全体の集計. 個別結果は results dict (custom_id -> EvaluationResult)."""
    batch_id: str
    results: dict[str, EvaluationResult]
    submitted: int = 0
    succeeded: int = 0
    errored: int = 0
    fallback_used: int = 0  # 通常 API fallback で復旧した件数
    pending_dlq: int = 0  # supplier_eval_pending に積んだ件数
    cache_read_total: int = 0
    cache_write_total: int = 0
    duration_sec: float = 0.0
    timeout: bool = False  # hard_timeout で打切ったか (Q0: 必ず log + Discord 通知)
    error_message: Optional[str] = None
    pending_custom_ids: list[str] = field(default_factory=list)
    # batch 経路で reject/error → realtime fallback で復旧した cid 集合.
    # caller (task_supplier_sweep) は eval_model ラベルを batch vs batch-fallback に分離する.
    # Q0 silent skip 防止: DB 集計時に「batch だが実は realtime」を見分けられるように.
    fallback_custom_ids: set[str] = field(default_factory=set)


def _get_client() -> Optional[anthropic.Anthropic]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _build_user_content(item: BatchItem) -> list[dict]:
    """1 件分の user content (画像 2 枚 + dynamic_text + past_judgments + knowledge).

    cache_control の付与方針 (W94 v1, H-4 docstring 訂正 2026-05-02):
      - knowledge_block 有り → 別 text block + 1h ephemeral cache (BP2)
      - knowledge_block 無し → 単一 text block (cache_control 無し、BP1 system のみ依存)
      - past_judgments_block は dynamic_text と同 block に inline (cache 対象外、
        後続 W で計測ベースに分離検討)
    """
    content: list[dict] = []
    if item.ebay_image_url:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": item.ebay_image_url},
        })
    if item.candidate_image_url:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": item.candidate_image_url},
        })

    dynamic_text = DYNAMIC_PROMPT_TEMPLATE.format(
        ebay_title=item.ebay_title,
        platform=item.platform,
        candidate_title=item.candidate_title or "(不明)",
        price_jpy=item.price_jpy if item.price_jpy is not None else "不明",
        url=item.url,
    )
    past_judgments_block = _build_past_judgments_block(
        item.sku, item.ebay_title, ebay_item_id=item.ebay_item_id,
    )

    full_dynamic = dynamic_text + past_judgments_block
    if item.knowledge_block:
        # BP2: knowledge_block を別 text block に分離 + 1h cache_control.
        # 同 SKU 系の評価で knowledge が再利用される確率が高い (例: PIONEER 系の 5 候補).
        content.append({"type": "text", "text": full_dynamic})
        content.append({
            "type": "text",
            "text": item.knowledge_block,
            "cache_control": {"type": "ephemeral", "ttl": _CACHE_TTL_1H},
        })
    else:
        content.append({"type": "text", "text": full_dynamic})

    return content


def _build_batch_request(item: BatchItem, model: str) -> dict:
    """1 件 BatchItem → Anthropic Batch request dict."""
    return {
        "custom_id": item.custom_id,
        "params": {
            "model": model,
            "max_tokens": 800,
            "system": [{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                # BP1: system に 1h cache. batch 内 99 件 hit で write 2x cost を回収.
                "cache_control": {"type": "ephemeral", "ttl": _CACHE_TTL_1H},
            }],
            "messages": [{
                "role": "user",
                "content": _build_user_content(item),
            }],
        },
    }


def _extract_result_from_message(msg) -> EvaluationResult:
    """Anthropic message → EvaluationResult 変換 (evaluate_match L504-512 と同一処理)."""
    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    result = _parse_response(text)
    usage = msg.usage
    result.cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    result.cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return result


def _fallback_to_realtime(item: BatchItem, model: str) -> EvaluationResult:
    """batch errored 個別 request の通常 API retry (Tier 2 fallback).

    Q0: 失敗時は必ず error フィールド非空で返す (silent skip 禁止).
    """
    from monitor.claude_evaluator import evaluate_match
    try:
        return evaluate_match(
            ebay_title=item.ebay_title,
            candidate_title=item.candidate_title,
            platform=item.platform,
            price_jpy=item.price_jpy,
            url=item.url,
            ebay_image_url=item.ebay_image_url,
            candidate_image_url=item.candidate_image_url,
            sku=item.sku,
            ebay_item_id=item.ebay_item_id,
            model=model,
        )
    except Exception as e:
        logger.warning(f"fallback_to_realtime failed for {item.custom_id}: {e}")
        return EvaluationResult(
            match_score=0,
            reasoning="fallback API error",
            error=f"fallback: {e}",
        )


def _persist_pending(custom_ids: list[str], batch_id: str, reason: str) -> int:
    """DLQ: hard_timeout / 全 fallback 失敗時に supplier_eval_pending に積む.

    Returns: 積んだ件数 (Q0: silent fail せず必ず logger.warning + return 値で報告).
    """
    if not custom_ids:
        return 0
    try:
        from monitor.database import get_conn
        with get_conn() as conn:
            for cid in custom_ids:
                conn.execute(
                    """INSERT OR IGNORE INTO supplier_eval_pending
                       (custom_id, batch_id, reason, created_at)
                       VALUES (?, ?, ?, datetime('now'))""",
                    (cid, batch_id, reason),
                )
        logger.warning(
            f"[supplier_batch] {len(custom_ids)} items moved to DLQ "
            f"(batch_id={batch_id}, reason={reason})"
        )
        return len(custom_ids)
    except Exception as e:
        # DLQ 自体が失敗 = Q0 silent skip 直結. 必ず log + 呼出側に伝搬する.
        logger.error(
            f"[supplier_batch] DLQ persist FAILED batch_id={batch_id} "
            f"items={len(custom_ids)} error={e}"
        )
        return 0


def evaluate_batch(
    items: list[BatchItem],
    model: Optional[str] = None,
    poll_interval_sec: int = _DEFAULT_POLL_INTERVAL_SEC,
    hard_timeout_sec: int = _DEFAULT_HARD_TIMEOUT_SEC,
) -> BatchResult:
    """N 件評価を Anthropic Message Batches API で一括処理.

    Args:
        items: 評価対象. custom_id は呼出側が一意付与 (例: f"{eid}-{plat}-{idx}").
        model: モデル ID. None で CLAUDE_MODEL (claude-opus-4-7).
        poll_interval_sec: poll 間隔.
        hard_timeout_sec: 超過時に未完了 request_id を DLQ.

    Returns: BatchResult. 個別結果は results[custom_id] で取得.

    Q0 防御:
      - submit 自体失敗 → BatchResult(error_message=非空) + 全件 fallback or DLQ
      - poll timeout → timeout=True + 未完了 ids を pending_custom_ids に保持
      - errored 個別 → fallback_to_realtime → 失敗なら DLQ
    """
    start_time = time.time()
    if not items:
        return BatchResult(batch_id="", results={}, submitted=0)

    _model = model or CLAUDE_MODEL
    client = _get_client()
    if not client:
        # API key 未設定 = 全件失敗扱い. silent skip 禁止 (Q0).
        results = {
            it.custom_id: EvaluationResult(
                match_score=0,
                reasoning="ANTHROPIC_API_KEY 未設定",
                error="ANTHROPIC_API_KEY not set",
            )
            for it in items
        }
        return BatchResult(
            batch_id="",
            results=results,
            submitted=len(items),
            errored=len(items),
            error_message="ANTHROPIC_API_KEY not set",
        )

    # Phase 1: batch submit
    requests = [_build_batch_request(it, _model) for it in items]
    try:
        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        logger.info(
            f"[supplier_batch] submitted batch_id={batch_id} items={len(items)} "
            f"model={_model}"
        )
    except Exception as e:
        # Tier 2: submit 失敗 → 全件通常 API fallback (silent skip 禁止)
        logger.warning(f"[supplier_batch] submit failed, fallback to realtime: {e}")
        results: dict[str, EvaluationResult] = {}
        fallback_cids: set[str] = set()
        for it in items:
            r = _fallback_to_realtime(it, _model)
            results[it.custom_id] = r
            if not r.error:
                fallback_cids.add(it.custom_id)
        return BatchResult(
            batch_id="",
            results=results,
            submitted=len(items),
            fallback_used=len(fallback_cids),
            errored=len(items) - len(fallback_cids),
            error_message=f"batch submit failed: {e}",
            duration_sec=time.time() - start_time,
            fallback_custom_ids=fallback_cids,
        )

    # Phase 2: poll until ended / timeout
    deadline = start_time + hard_timeout_sec
    while True:
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception as e:
            logger.warning(f"[supplier_batch] poll retrieve failed: {e}, retry in {poll_interval_sec}s")
            time.sleep(poll_interval_sec)
            if time.time() >= deadline:
                break
            continue

        if batch.processing_status == "ended":
            break
        if batch.processing_status in ("canceled", "expired"):
            logger.warning(
                f"[supplier_batch] batch {batch_id} status={batch.processing_status}"
            )
            break
        if time.time() >= deadline:
            logger.warning(
                f"[supplier_batch] hard_timeout exceeded batch_id={batch_id} "
                f"({hard_timeout_sec}s). 未完了 items を DLQ に積みます."
            )
            # timeout: 未完了の全 custom_id を DLQ
            pending_ids = [it.custom_id for it in items]
            dlq_n = _persist_pending(pending_ids, batch_id, "hard_timeout")
            return BatchResult(
                batch_id=batch_id,
                results={},
                submitted=len(items),
                errored=len(items),
                pending_dlq=dlq_n,
                pending_custom_ids=pending_ids,
                timeout=True,
                error_message=f"hard_timeout {hard_timeout_sec}s exceeded",
                duration_sec=time.time() - start_time,
            )
        time.sleep(poll_interval_sec)

    # Phase 3: results 取得 + 個別 errored は通常 API fallback
    results: dict[str, EvaluationResult] = {}
    errored_items: list[BatchItem] = []
    item_by_id = {it.custom_id: it for it in items}
    cache_read_total = 0
    cache_write_total = 0

    # H-5 (2026-05-02 code-reviewer): batch 成功 1 件 = api_call_log 1 row 記録.
    # 月 cost 集計の verify 経路 (現状 batch 経路は完全 invisible だった).
    from monitor.api_logger import log_anthropic_response
    try:
        for r in client.messages.batches.results(batch_id):
            cid = r.custom_id
            if r.result.type == "succeeded":
                eval_r = _extract_result_from_message(r.result.message)
                results[cid] = eval_r
                cache_read_total += eval_r.cache_read
                cache_write_total += eval_r.cache_write
                log_anthropic_response(
                    "candidate_evaluate_batch", _model, r.result.message,
                    success=True,
                )
            else:
                # type in ("errored", "canceled", "expired") → fallback 対象
                err_type = r.result.type
                err_detail = ""
                if err_type == "errored":
                    err_detail = str(getattr(r.result, "error", ""))[:200]
                logger.info(
                    f"[supplier_batch] item {cid} {err_type}: {err_detail}, "
                    f"realtime fallback 試行"
                )
                if cid in item_by_id:
                    errored_items.append(item_by_id[cid])
    except Exception as e:
        # results 取得自体失敗 = batch 全失敗扱い → 全件 fallback
        logger.warning(
            f"[supplier_batch] results fetch failed batch_id={batch_id}: {e}, "
            f"全件 realtime fallback"
        )
        errored_items = list(items)
        results.clear()

    succeeded = len(results)

    # Tier 2: errored 個別 → 通常 API fallback
    fallback_cids: set[str] = set()
    pending_ids: list[str] = []
    for it in errored_items:
        r = _fallback_to_realtime(it, _model)
        results[it.custom_id] = r
        if r.error:
            pending_ids.append(it.custom_id)
        else:
            fallback_cids.add(it.custom_id)

    # Tier 3: fallback も失敗 → DLQ
    dlq_n = _persist_pending(pending_ids, batch_id, "batch_errored_and_fallback_failed")

    duration = time.time() - start_time
    logger.info(
        f"[supplier_batch] completed batch_id={batch_id} duration={duration:.1f}s "
        f"submitted={len(items)} succeeded={succeeded} fallback={len(fallback_cids)} "
        f"dlq={dlq_n} cache_read={cache_read_total} cache_write={cache_write_total}"
    )
    # H-3 (2026-05-02 code-reviewer): errored は fallback で復旧した分を除外.
    # 「全件 batch errored だが全件 fallback 成功」を success=True 扱いに整合.
    final_errored = len(errored_items) - len(fallback_cids)
    return BatchResult(
        batch_id=batch_id,
        results=results,
        submitted=len(items),
        succeeded=succeeded,
        errored=final_errored,
        fallback_used=len(fallback_cids),
        pending_dlq=dlq_n,
        pending_custom_ids=pending_ids,
        fallback_custom_ids=fallback_cids,
        cache_read_total=cache_read_total,
        cache_write_total=cache_write_total,
        duration_sec=duration,
    )
