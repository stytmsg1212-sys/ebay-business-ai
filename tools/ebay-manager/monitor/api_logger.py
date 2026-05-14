#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 呼出ログ (Anthropic / Google Gemini 両対応)

各タスクの Claude/Gemini 呼出直後に `log_api_call()` を呼んで DB に記録。
記録内容: provider, model, operation, token使用量, duration, success/error, 推定コスト。

後続の エージェント監視ダッシュボードで稼働率/月間コストを集計する。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# モデル別のトークン単価 ($/1M tokens) — 2026-04時点
# Anthropic: https://www.anthropic.com/pricing
# Gemini:    https://ai.google.dev/gemini-api/docs/pricing
_PRICING = {
    # Anthropic Claude
    "claude-opus-4-7": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    # Gemini
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.0, "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.0, "cache_write": 0.0},
}


def _estimate_cost_usd(model: str, in_tok: int, out_tok: int,
                       cache_r: int = 0, cache_w: int = 0,
                       is_batch: bool = False) -> float:
    """モデル別の推定コスト (USD)。未知モデルは 0.0。

    is_batch=True なら Anthropic Message Batches API の 50% 割引を適用.
    公式 docs: "These multipliers stack with the Batch API discount" → input / output /
    cache_read / cache_write 全 token に一律 0.5 倍.
    """
    p = _PRICING.get(model)
    if not p:
        return 0.0
    total = (
        in_tok * p["input"] / 1_000_000
        + out_tok * p["output"] / 1_000_000
        + cache_r * p["cache_read"] / 1_000_000
        + cache_w * p["cache_write"] / 1_000_000
    )
    if is_batch:
        total *= 0.5
    return total


def log_api_call(
    provider: str,
    model: str,
    operation: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    duration_ms: Optional[int] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    is_batch: Optional[bool] = None,
) -> None:
    """DB に API コール記録を残す。失敗しても処理は続行。

    is_batch:
      - None (default): operation 名末尾 '_batch' で auto-detect (caller の渡し忘れ silent
        regression 防止、W108 の bug が再発しない構造).
      - True / False: 明示指定 (auto-detect を上書き、特殊な命名規約に従わない caller 用).
    """
    if is_batch is None:
        is_batch = operation.endswith("_batch")

    try:
        cost = _estimate_cost_usd(
            model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            is_batch=is_batch,
        )
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO api_call_log
                   (provider, model, operation,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    duration_ms, success, error_message, cost_usd, is_batch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider, model, operation,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    duration_ms, 1 if success else 0, error_message, cost,
                    1 if is_batch else 0,
                ),
            )
            conn.commit()  # isolation_level 依存を排除、確実に永続化
        finally:
            conn.close()
    except Exception as e:
        # 呼出元処理を止めないため except は維持。ただし silent skip 防止のため
        # warning レベルで surface (Q0 silent-skip-prevention.md)。
        # 痕跡が無いと「API は call されているのに log が増えない」が検出不能になる
        # (2026-05-02 timezone bug 調査時に本パターンの危険性を確認)。
        logger.warning(f"log_api_call failed: {type(e).__name__}: {e}")


class _Timer:
    """`with _Timer() as t:` で t.duration_ms を計測する軽量ヘルパ。"""
    def __enter__(self):
        self._start = time.time()
        self.duration_ms = 0
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration_ms = int((time.time() - self._start) * 1000)
        return False


def log_anthropic_response(
    operation: str, model: str, response, duration_ms: Optional[int] = None,
    success: bool = True, error_message: Optional[str] = None,
    is_batch: Optional[bool] = None,
) -> None:
    """Anthropic messages.create の response オブジェクトから usage を抽出して記録。

    is_batch は log_api_call に pass-through (operation 末尾 '_batch' で auto-detect).
    """
    in_tok = out_tok = cache_r = cache_w = 0
    try:
        if response is not None and hasattr(response, "usage"):
            u = response.usage
            in_tok = int(getattr(u, "input_tokens", 0) or 0)
            out_tok = int(getattr(u, "output_tokens", 0) or 0)
            cache_r = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            cache_w = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    except Exception:
        pass

    log_api_call(
        provider="anthropic",
        model=model,
        operation=operation,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_r,
        cache_write_tokens=cache_w,
        duration_ms=duration_ms,
        success=success,
        error_message=error_message,
        is_batch=is_batch,
    )


def log_gemini_response(
    operation: str, model: str, response, duration_ms: Optional[int] = None,
    success: bool = True, error_message: Optional[str] = None,
) -> None:
    """Gemini generate_content の response から usage_metadata を抽出して記録。"""
    in_tok = out_tok = 0
    try:
        if response is not None and hasattr(response, "usage_metadata"):
            u = response.usage_metadata
            in_tok = int(getattr(u, "prompt_token_count", 0) or 0)
            out_tok = int(getattr(u, "candidates_token_count", 0) or 0)
    except Exception:
        pass

    log_api_call(
        provider="google",
        model=model,
        operation=operation,
        input_tokens=in_tok,
        output_tokens=out_tok,
        duration_ms=duration_ms,
        success=success,
        error_message=error_message,
    )
