#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W13 X ベース AI ニュース取得: 3 ソース fetcher (X / Reddit / HN)

code-reviewer H-6 指摘対応:
  - 明示的 User-Agent, per-source skip (1 つ 403 で全停止しない)
  - Reddit は old.reddit.com/.json (new API より寛容)
  - HN は Algolia Search API (公式、寛容)

返り値は共通の NewsRaw スキーマで統一し、pipeline に渡す.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from monitor.x_news_sources import XSourcesConfig
from monitor.xai_wrapper import (
    XaiBudgetExceeded, XaiCallError, XaiConfigError, call_search_x,
)

logger = logging.getLogger(__name__)


@dataclass
class NewsRaw:
    """取得直後の生データ共通スキーマ (dedupe/classifier へ渡す前の正規形)."""
    source_type: str                  # 'x' / 'reddit' / 'hn'
    source_handle: str                # @AnthropicAI / r/ClaudeAI / 'hn'
    url: str
    title: str
    raw_content: str = ""
    engagement_count: int = 0
    published_at: Optional[str] = None     # ISO-8601
    extra: dict = field(default_factory=dict)


_HTTPX_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# ─────────────────────────────────────────────
# X (search-x 経由)
# ─────────────────────────────────────────────

def fetch_x(cfg: XSourcesConfig) -> list[NewsRaw]:
    """X/Twitter から tweet を取得. 各 query ごとに 1 回 call_search_x.

    search-x の --json 出力を正規化して NewsRaw を生成.
    1 つの query が失敗しても他は続行.
    """
    if not cfg.x_enabled:
        return []
    results: list[NewsRaw] = []
    for q in cfg.x_queries:
        try:
            data = call_search_x(
                query=q.keyword,
                days=q.days,
                handles=cfg.x_handles or None,
                exclude_handles=cfg.x_exclude_handles or None,
                daily_cost_cap_usd=cfg.xai_daily_cap_usd,
                context=f"x_news_check:{q.keyword[:30]}",
            )
        except XaiBudgetExceeded as e:
            logger.warning(f"X fetch halted (budget): {e}")
            break  # これ以上の query は全 skip (budget 超過)
        except (XaiCallError, XaiConfigError) as e:
            logger.warning(f"X fetch failed for '{q.keyword}': {e}")
            continue
        # search-x --json 構造: citations / posts / 自由 JSON
        posts = _extract_x_posts(data)
        for p in posts:
            results.append(NewsRaw(
                source_type="x",
                source_handle=p.get("handle") or "",
                url=p.get("url") or "",
                title=p.get("text", "")[:200],
                raw_content=p.get("text", ""),
                engagement_count=int(p.get("likes", 0) or 0),
                published_at=p.get("date") or None,
                extra={"query": q.keyword},
            ))
    return results


def _extract_x_posts(data: dict) -> list[dict]:
    """search-x (Grok Responses API 形式) の出力から tweet list を抽出.

    2026-04-24 確認した実 response 構造:
      data = {
        "output": [
          {"type": "custom_tool_call", ...},  # x_semantic_search / x_keyword_search
          ...,
          {"type": "message", "content": [
            {"type": "output_text",
             "text": "markdown with tweets",
             "annotations": [{"type": "url_citation",
                              "url": "https://x.com/i/status/...",
                              "start_index": 158, "end_index": 207,
                              "title": "1"}]}
          ]}
        ]
      }

    annotations の URL を正として抽出し、url 周辺の markdown テキストを
    tweet 本文として切り出す. likes/date は Grok summary 内に時折入るが
    構造化されていないため 0/None default.
    """
    # 旧形式 (posts/tweets/results/citations) の skill が戻してきた場合も救済
    for key in ("posts", "tweets", "results", "citations"):
        v = data.get(key)
        if isinstance(v, list):
            return [_normalize_x_item(p) for p in v if isinstance(p, dict)]

    # Grok Responses API 形式
    output = data.get("output") or []
    if not isinstance(output, list):
        return []
    for chunk in output:
        if not isinstance(chunk, dict) or chunk.get("type") != "message":
            continue
        content = chunk.get("content") or []
        if not isinstance(content, list):
            continue
        for seg in content:
            if not isinstance(seg, dict) or seg.get("type") != "output_text":
                continue
            text = seg.get("text") or ""
            annotations = seg.get("annotations") or []
            if not annotations:
                continue
            return _parse_grok_annotations(text, annotations)
    return []


def _parse_grok_annotations(text: str, annotations: list[dict]) -> list[dict]:
    """Grok の output_text + annotations を tweet list に正規化.

    annotations[i] = {url, start_index, end_index, title}
    同一 URL の複数エントリは 1 件に統合. tweet 本文は URL 直前の段落、
    handle は markdown 中の (@username) パターン or URL path から抽出.
    """
    import re as _re
    seen: dict[str, dict] = {}

    # URL を 1 個ずつ処理. 重複 URL はスキップ.
    url_positions: list[tuple[int, str]] = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        url = ann.get("url") or ""
        if not url:
            continue
        # start_index は citation marker [[N]](url) 開始位置
        start_idx = int(ann.get("start_index", 0) or 0)
        url_positions.append((start_idx, url))

    # 位置順にソートし、隣接 URL 間を tweet 1 件のブロックとする
    url_positions.sort()

    for i, (pos, url) in enumerate(url_positions):
        if url in seen:
            continue
        # ブロック境界: 前の URL の後〜このURL の start_index
        prev_end = url_positions[i - 1][0] + len(annotations[i - 1].get("url", "")) \
            if i > 0 else 0
        # より現実的には markdown の "- **" や "**@" を境界に使うが、
        # 最初の簡易版として annotation 間の区間を採用
        block_start = max(0, pos - 400)
        block = text[block_start:pos]

        # markdown 装飾除去
        block_clean = _re.sub(r"\[\[\d+\]\]\([^)]+\)", "", block)
        block_clean = _re.sub(r"\[(\d+)\]\([^)]+\)", r"[\1]", block_clean)
        block_clean = block_clean.replace("**", "").replace("*", "")
        block_clean = _re.sub(r"\s+", " ", block_clean).strip()

        # handle 抽出: markdown 内の (@xxx) or @xxx を優先、失敗したら URL
        handle = ""
        # 末尾 300 文字内で最後に見つかった (@xxx) を採用 (tweet 本文直前の著者情報)
        hmatches = _re.findall(r"\(@([A-Za-z0-9_]+)\)", block_clean)
        if hmatches:
            handle = "@" + hmatches[-1]
        else:
            # @xxx 単体 (カッコなし)
            hmatches2 = _re.findall(r"(?<![\w])@([A-Za-z0-9_]{2,30})", block_clean)
            if hmatches2:
                handle = "@" + hmatches2[-1]
            else:
                # URL path から
                m = _re.search(r"https?://(?:x|twitter)\.com/([^/]+)/status/", url)
                if m and m.group(1).lower() != "i":
                    handle = "@" + m.group(1)

        # tweet 本文: block_clean の末尾 ~250 文字 (直前=より近い内容)
        excerpt = block_clean[-250:] if len(block_clean) > 250 else block_clean

        seen[url] = {
            "url": url,
            "handle": handle,
            "text": excerpt,
            "likes": 0,
            "date": None,
        }
    return list(seen.values())


def _normalize_x_item(p: dict) -> dict:
    """旧形式 (dict 構造で tweet) の正規化 (fallback)."""
    url = p.get("url") or p.get("link") or ""
    handle = p.get("handle") or p.get("author") or p.get("username") or ""
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    return {
        "url": url,
        "handle": handle,
        "text": p.get("text") or p.get("content") or p.get("title") or "",
        "likes": int(p.get("likes", 0) or p.get("like_count", 0) or 0),
        "date": p.get("date") or p.get("created_at"),
    }


# ─────────────────────────────────────────────
# Reddit (public .json)
# ─────────────────────────────────────────────

def fetch_reddit(cfg: XSourcesConfig) -> list[NewsRaw]:
    """Reddit /r/{sub}/{listing}.json を old.reddit.com から取得.

    429/403 は source 単位で skip. min_score 未満は除外.
    """
    if not cfg.reddit_enabled:
        return []
    results: list[NewsRaw] = []
    headers = {"User-Agent": cfg.reddit_user_agent}
    for sub in cfg.reddit_subs:
        url = f"https://old.reddit.com/r/{sub.name}/{sub.listing}.json"
        try:
            with httpx.Client(timeout=_HTTPX_TIMEOUT, headers=headers) as client:
                r = client.get(url, params={"limit": sub.limit})
            if r.status_code in (403, 429):
                logger.warning(
                    f"Reddit r/{sub.name}: HTTP {r.status_code} (skip)"
                )
                continue
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"Reddit r/{sub.name} failed: {e}")
            continue
        for child in data.get("data", {}).get("children", []):
            d = child.get("data") or {}
            score = int(d.get("score") or 0)
            if score < cfg.reddit_min_score:
                continue
            permalink = d.get("permalink") or ""
            results.append(NewsRaw(
                source_type="reddit",
                source_handle=f"r/{sub.name}",
                url=f"https://www.reddit.com{permalink}" if permalink else "",
                title=(d.get("title") or "")[:200],
                raw_content=(d.get("selftext") or "")[:1000],
                engagement_count=score,
                published_at=_epoch_to_iso(d.get("created_utc")),
                extra={"subreddit": sub.name, "num_comments": d.get("num_comments", 0)},
            ))
    return results


def _epoch_to_iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


# ─────────────────────────────────────────────
# HN (Algolia Search API)
# ─────────────────────────────────────────────

def fetch_hn(cfg: XSourcesConfig) -> list[NewsRaw]:
    """Hacker News Algolia Search API から取得.

    https://hn.algolia.com/api
    stories のみ (Ask/Show/Job は除外).
    """
    if not cfg.hn_enabled:
        return []
    results: list[NewsRaw] = []
    base = "https://hn.algolia.com/api/v1/search"
    for q in cfg.hn_queries:
        params = {
            "query": q.keyword,
            "tags": "story",
            "numericFilters": f"created_at_i>{_days_ago_epoch(q.days)},points>={cfg.hn_min_points}",
            "hitsPerPage": cfg.hn_max_per_query,
        }
        try:
            with httpx.Client(timeout=_HTTPX_TIMEOUT) as client:
                r = client.get(base, params=params)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"HN fetch failed for '{q.keyword}': {e}")
            continue
        for hit in data.get("hits", []):
            results.append(NewsRaw(
                source_type="hn",
                source_handle="hn",
                url=hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}",
                title=(hit.get("title") or "")[:200],
                raw_content=(hit.get("story_text") or "")[:1000],
                engagement_count=int(hit.get("points") or 0),
                published_at=hit.get("created_at"),
                extra={
                    "query": q.keyword,
                    "objectID": hit.get("objectID"),
                    "num_comments": hit.get("num_comments", 0),
                },
            ))
    return results


def _days_ago_epoch(days: int) -> int:
    """N 日前の UNIX epoch (UTC)."""
    from datetime import timedelta
    return int((datetime.now(tz=timezone.utc) - timedelta(days=int(days))).timestamp())


# ─────────────────────────────────────────────
# 統合 fetch (pipeline から呼ばれる)
# ─────────────────────────────────────────────

def fetch_all(cfg: XSourcesConfig) -> list[NewsRaw]:
    """全ソースから取得. 1 つ失敗しても他は続行.

    並列化は将来検討 (今は逐次、pytest 安定性優先).
    """
    all_items: list[NewsRaw] = []
    for fn, label in [(fetch_x, "X"), (fetch_reddit, "Reddit"), (fetch_hn, "HN")]:
        try:
            items = fn(cfg)
            logger.info(f"{label} fetcher: {len(items)} items")
            all_items.extend(items)
        except Exception as e:  # noqa: BLE001  (top level skip, 致命エラー回避)
            logger.error(f"{label} fetcher failed: {e}", exc_info=True)
    return all_items


__all__ = [
    "NewsRaw",
    "fetch_x", "fetch_reddit", "fetch_hn", "fetch_all",
]
