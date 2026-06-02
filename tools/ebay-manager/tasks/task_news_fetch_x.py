#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W209 Phase 1 拡張: X (Grok x_search) fetcher.

task_news_check の _FETCHER_BY_TYPE に 'x' として登録される。
config/x_news_sources.json で enabled / handles / model / daily_query_cap を制御。

設計判断:
- enabled=false 時は呼ばれても [] を返す (W209 初期状態)
- engagement / likes でソートしない (parody 排除)
- 1 日 1-2 クエリ制限 = daily_query_cap でガード (api_budget_log 経由)
- xAI API キー不在 / 通信失敗時は warning + [] (Q0 silent skip 防止)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
_CONFIG_PATH = BASE_DIR / "config" / "x_news_sources.json"


def load_x_config() -> dict:
    """config/x_news_sources.json をロード。欠落時は enabled=False で安全側へ。"""
    if not _CONFIG_PATH.exists():
        return {"enabled": False, "handles": []}
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            f"x_news_sources.json 読込失敗 ({e}) = enabled=False で fallback"
        )
        return {"enabled": False, "handles": []}
    return cfg


def _today_xai_query_count() -> int:
    """当日の xAI provider 呼出回数を api_budget_log から集計。

    daily_query_cap のガードに使う。1 ハンドル = 1 query で集計。
    """
    from monitor.database import get_conn
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_budget_log "
                "WHERE date = ? AND provider = 'xai' AND context = 'news_x'",
                (today,),
            ).fetchone()
        return int(row[0] or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_today_xai_query_count 失敗: {e}")
        return 0


def fetch_x_entries(source: Optional[dict] = None) -> list[dict]:
    """X (Grok) handle 1 件 or 全 handle を fetch し、RSS/Reddit/HN と同型の
    entries (list of dict) を返す。

    task_news_check の `_FETCHER_BY_TYPE['x']` から呼ばれる時の引数 source は
    NEWS_SOURCES から渡される 1 source 辞書だが、本 W209 設計では X は
    "config/x_news_sources.json から動的に handles を回す" 単一エントリと扱う。
    source 辞書には {'name': 'X (Grok)', 'type': 'x'} 程度しか入らないため、
    内部で config をロードして全 handle を順に処理する。

    Returns:
        list of {'title', 'url', 'source', 'source_type'='x',
                 'source_handle'='@xxx', 'published_at'}.
        enabled=false / API キー無し / daily_cap 到達時は [].
    """
    cfg = load_x_config()
    if not cfg.get("enabled"):
        logger.info("fetch_x_entries: x_news_sources.enabled=false = skip")
        return []

    handles = cfg.get("handles") or []
    if not handles:
        logger.warning("fetch_x_entries: handles 空 = skip")
        return []

    model = cfg.get("model") or "grok-4-1-fast-non-reasoning"
    max_per_handle = int(cfg.get("max_per_handle") or 10)
    daily_cap = int(cfg.get("daily_query_cap") or 2)

    # daily cap ガード (Q0: 痕跡を残す)
    already = _today_xai_query_count()
    remaining = max(0, daily_cap - already)
    if remaining <= 0:
        logger.warning(
            f"fetch_x_entries: 当日 xAI query cap 到達 "
            f"(already={already} cap={daily_cap}) = skip"
        )
        return []

    # daily_cap を超えない範囲で handle を処理
    to_process = handles[:remaining]

    from monitor.xai_wrapper import search_x_posts
    from monitor.database import add_api_cost

    entries: list[dict] = []
    for h in to_process:
        handle = (h.get("handle") or "").lstrip("@")
        if not handle:
            continue
        # 技術 tip 限定 query: from:{handle} + 雑談除外
        query = (
            f"from:{handle} (Claude OR Anthropic OR LLM OR agent OR MCP OR "
            f"prompt OR API OR scraping OR ebay OR eBay) -is:retweet"
        )
        try:
            posts = search_x_posts(
                query, model=model, max_results=max_per_handle,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"fetch_x_entries handle={handle} 失敗: "
                f"{type(e).__name__}: {e}"
            )
            posts = []

        # cost / cap 計上 (handle 単位 = 1 query): _total_cost_usd は posts[0] にだけ持つ。
        # HIGH-1 fix (2026-06-02 code-reviewer): search_x_posts を叩いた事実を
        # **posts 空でも必ず 1 query として記録**する。x_search はヒット0/抽出失敗でも
        # xAI 側は課金され得るが、旧コードは `if posts:` で空応答時に記録を skip して
        # いたため _today_xai_query_count が増えず、翌 handle/翌バッチで daily_query_cap
        # が消費されず青天井に API を叩くリスクがあった。cost 0 でも1行記録で cap を消費。
        total_cost = float(posts[0].get("_total_cost_usd") or 0.0) if posts else 0.0
        try:
            add_api_cost("xai", total_cost, context="news_x")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"add_api_cost (news_x) 失敗: {e}")

        for p in posts:
            entries.append({
                "title": p.get("title") or p.get("text") or "",
                "url": p.get("url") or "",
                "source": f"X (@{handle})",
                "source_type": "x",
                "source_handle": f"@{handle}",
                "published_at": p.get("published_at") or "",
                "_axis_hint": h.get("axis_hint") or "a",
            })

    logger.info(
        f"fetch_x_entries: handles={len(to_process)}/{len(handles)} "
        f"entries={len(entries)}"
    )
    return entries
