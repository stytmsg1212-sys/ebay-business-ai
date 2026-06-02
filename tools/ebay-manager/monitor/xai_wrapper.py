#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W209: xAI (Grok) Responses API 最小 wrapper.

X (Twitter) からの技術 tip 取得を Responses API + x_search tool で実施。
旧 W13 の grok 経由 x-search 経路を、syndication 案を破棄して Responses API
へ作り直したもの。

公開 API:
- search_x_posts(query, *, model, max_results) -> List[Dict]
- _MODEL_PRICING (テスト / コスト計算で参照可)

設計判断:
- engagement / likes でソートしない (parody / sensationalist ノイズ排除のため)
- 関連度判定は news_relevance.score_relevance (Haiku) に委ねる
- 1 day budget gate は呼出側 (task_news_fetch_x) で実装
- 失敗時は [] を返し warning ログ (Q0: 痕跡を残す)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# .env ロード
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass


# xAI Grok モデルの token 単価 ($/1M tokens) — 2026-06 時点
# grok-4-1-fast-non-reasoning: input $0.20 / output $0.50
_MODEL_PRICING = {
    "grok-4-1-fast-non-reasoning": {"input": 0.20, "output": 0.50},
}

_XAI_ENDPOINT = "https://api.x.ai/v1/responses"
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _estimate_xai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """xAI コスト推定。未知モデルは 0.0 (warning 経由で可視化)."""
    p = _MODEL_PRICING.get(model)
    if not p:
        logger.warning(f"xai_wrapper: unknown model '{model}' = cost 0.0 と推定")
        return 0.0
    return (
        input_tokens * p["input"] / 1_000_000
        + output_tokens * p["output"] / 1_000_000
    )


def search_x_posts(
    query: str,
    *,
    model: str = "grok-4-1-fast-non-reasoning",
    max_results: int = 10,
) -> list[dict]:
    """xAI Responses API + x_search tool で X (Twitter) 検索を実施し、
    技術 tip 系のポストを取得する。

    Returns:
        list of {'title': str, 'url': str, 'author': str, 'text': str,
                 'published_at': str, '_cost_usd': float, '_model': str}.
        失敗 / 空応答時は [].

    呼出側 (task_news_fetch_x) は handles 別に query を組み立て、結果を
    fetch_x_entries の戻り値形式 (RSS/Reddit/HN と同型) に整形する。

    Q0 注意: 例外を握り潰さず warning + [] 返却にとどめ、他 source の続行を妨げない。
    """
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        logger.warning("XAI_API_KEY 未設定 = X search skip")
        return []

    # 1 回の input 文字列。技術 tip 限定で engagement 非加重を明示。
    instruction = (
        f"以下のクエリで X (Twitter) を検索し、関連する技術 tip 系の投稿を最大 "
        f"{int(max_results)} 件返してください。\n"
        f"engagement (likes/reposts) でソートせず、技術的に新規性のあるものを優先。\n"
        f"パロディ・煽動・スクショ雑談は除外。\n\n"
        f"クエリ: {query}\n\n"
        f"出力は厳密な JSON 配列 (```json フェンス禁止):\n"
        f'[{{"title": "投稿冒頭 60 字", "url": "https://x.com/...", '
        f'"author": "@handle", "text": "投稿本文 (~280 字)", '
        f'"published_at": "ISO8601 or 空文字"}}]'
    )

    body = {
        "model": model,
        "tools": [{"type": "x_search"}],
        "input": instruction,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(_XAI_ENDPOINT, headers=headers, json=body)
    except httpx.HTTPError as e:
        logger.warning(
            f"xai_wrapper search_x_posts HTTP 失敗: {type(e).__name__}: {e}"
        )
        return []

    if resp.status_code != 200:
        logger.warning(
            f"xai_wrapper HTTP {resp.status_code}: body={resp.text[:200]!r}"
        )
        return []

    try:
        data = resp.json()
    except ValueError as e:
        logger.warning(f"xai_wrapper JSON parse 失敗: {e}")
        return []

    # usage からトークン抽出 (Responses API の標準フィールド)
    usage = data.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cost_usd = _estimate_xai_cost(model, in_tok, out_tok)

    # output_text を抽出。Responses API は `output_text` を返すが、
    # ベンダー差で `output` array にも入る可能性あり、両対応。
    out_text = ""
    if isinstance(data.get("output_text"), str):
        out_text = data["output_text"]
    else:
        # output: [{"type": "message", "content": [{"type": "output_text", "text": ...}]}]
        for block in data.get("output") or []:
            for c in (block.get("content") or []):
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    out_text += c["text"]

    if not out_text:
        logger.warning("xai_wrapper: output_text 抽出失敗")
        return []

    # JSON 配列を抜き出す (前後に説明文が付くことがあるため)
    posts: list[dict] = []
    try:
        # 最初に出てくる '[' 〜 最後の ']' を greedy に切り出す
        i = out_text.find("[")
        j = out_text.rfind("]")
        if i >= 0 and j > i:
            posts = json.loads(out_text[i : j + 1])
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(
            f"xai_wrapper output JSON 配列 抽出 失敗: {e} / raw={out_text[:200]!r}"
        )
        return []

    if not isinstance(posts, list):
        return []

    # 正規化 + cost annotation
    out: list[dict] = []
    for p in posts[: int(max_results)]:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or p.get("text") or "").strip()[:200]
        if not title:
            continue
        out.append({
            "title": title,
            "url": str(p.get("url") or "").strip(),
            "author": str(p.get("author") or "").strip(),
            "text": str(p.get("text") or "").strip()[:1000],
            "published_at": str(p.get("published_at") or "").strip(),
            "_cost_usd": float(cost_usd) / max(len(posts), 1),  # 投稿あたり按分
            "_model": model,
            "_total_cost_usd": float(cost_usd),  # 呼出側 1 度きり計上用
        })
    return out
