#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W13 X ベース AI ニュース取得: sources config ローダ + validator

code-reviewer M-5 指摘に対応:
  - typo で scheduler クラッシュを防ぐため schema validation を実施
  - jsonschema 未インストールでも基本チェックは動くよう、stdlib のみで実装
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "x_news_sources.json"
)


@dataclass
class XQuery:
    keyword: str
    days: int = 2
    description: str = ""


@dataclass
class RedditSub:
    name: str
    listing: str = "hot"   # hot / new / top / rising
    limit: int = 15


@dataclass
class HNQuery:
    keyword: str
    days: int = 3


@dataclass
class XSourcesConfig:
    x_enabled: bool = True
    x_handles: list[str] = field(default_factory=list)
    x_exclude_handles: list[str] = field(default_factory=list)
    x_queries: list[XQuery] = field(default_factory=list)

    reddit_enabled: bool = True
    reddit_subs: list[RedditSub] = field(default_factory=list)
    reddit_min_score: int = 50
    reddit_user_agent: str = "ebay-manager/1.0 (W13 AI news digest)"

    hn_enabled: bool = True
    hn_queries: list[HNQuery] = field(default_factory=list)
    hn_min_points: int = 50
    hn_max_per_query: int = 15

    xai_daily_cap_usd: float = 2.0
    anthropic_daily_cap_usd: float = 1.0


class XSourcesConfigError(ValueError):
    """sources.json の構造不備."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise XSourcesConfigError(msg)


def _validate_str_list(v, name: str) -> list[str]:
    _require(isinstance(v, list), f"{name} must be a list, got {type(v).__name__}")
    for i, s in enumerate(v):
        _require(isinstance(s, str), f"{name}[{i}] must be str, got {type(s).__name__}")
    return list(v)


def load_sources(path: Optional[Path] = None) -> XSourcesConfig:
    """sources.json を読んで validate 済みの XSourcesConfig を返す.

    Raises:
        XSourcesConfigError: schema 不備 (typo / 型違い等)
        FileNotFoundError: ファイル不在
        json.JSONDecodeError: JSON 不正
    """
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"x_news_sources.json not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    _require(isinstance(raw, dict), "Top level must be an object")

    cfg = XSourcesConfig()

    # --- X ---
    x = raw.get("x") or {}
    _require(isinstance(x, dict), "x must be an object")
    cfg.x_enabled = bool(x.get("enabled", True))
    cfg.x_handles = _validate_str_list(x.get("handles", []), "x.handles")
    cfg.x_exclude_handles = _validate_str_list(
        x.get("exclude_handles", []), "x.exclude_handles"
    )
    _require(len(cfg.x_handles) <= 10,
             f"x.handles must be <= 10 (search-x API limit), got {len(cfg.x_handles)}")

    for i, q in enumerate(x.get("queries", []) or []):
        _require(isinstance(q, dict), f"x.queries[{i}] must be an object")
        kw = q.get("keyword")
        _require(isinstance(kw, str) and kw.strip(),
                 f"x.queries[{i}].keyword required")
        days = int(q.get("days", 2))
        _require(1 <= days <= 90,
                 f"x.queries[{i}].days must be 1..90, got {days}")
        cfg.x_queries.append(XQuery(keyword=kw, days=days,
                                     description=str(q.get("description", ""))))

    # --- Reddit ---
    r = raw.get("reddit") or {}
    _require(isinstance(r, dict), "reddit must be an object")
    cfg.reddit_enabled = bool(r.get("enabled", True))
    cfg.reddit_min_score = int(r.get("min_score", 50))
    cfg.reddit_user_agent = str(r.get("user_agent", cfg.reddit_user_agent))
    for i, s in enumerate(r.get("subreddits", []) or []):
        _require(isinstance(s, dict), f"reddit.subreddits[{i}] must be an object")
        name = s.get("name")
        _require(isinstance(name, str) and name.strip(),
                 f"reddit.subreddits[{i}].name required")
        listing = str(s.get("listing", "hot")).lower()
        _require(listing in ("hot", "new", "top", "rising"),
                 f"reddit.subreddits[{i}].listing invalid: {listing}")
        limit = int(s.get("limit", 15))
        _require(1 <= limit <= 100,
                 f"reddit.subreddits[{i}].limit must be 1..100")
        cfg.reddit_subs.append(RedditSub(name=name, listing=listing, limit=limit))

    # --- HN ---
    h = raw.get("hn") or {}
    _require(isinstance(h, dict), "hn must be an object")
    cfg.hn_enabled = bool(h.get("enabled", True))
    cfg.hn_min_points = int(h.get("min_points", 50))
    cfg.hn_max_per_query = int(h.get("max_per_query", 15))
    for i, q in enumerate(h.get("queries", []) or []):
        _require(isinstance(q, dict), f"hn.queries[{i}] must be an object")
        kw = q.get("keyword")
        _require(isinstance(kw, str) and kw.strip(),
                 f"hn.queries[{i}].keyword required")
        days = int(q.get("days", 3))
        _require(1 <= days <= 90,
                 f"hn.queries[{i}].days must be 1..90")
        cfg.hn_queries.append(HNQuery(keyword=kw, days=days))

    # --- Budget ---
    b = raw.get("budget") or {}
    _require(isinstance(b, dict), "budget must be an object")
    cfg.xai_daily_cap_usd = float(b.get("xai_daily_cap_usd", 2.0))
    cfg.anthropic_daily_cap_usd = float(b.get("anthropic_daily_cap_usd", 1.0))
    _require(cfg.xai_daily_cap_usd > 0, "budget.xai_daily_cap_usd must be > 0")
    _require(cfg.anthropic_daily_cap_usd > 0,
             "budget.anthropic_daily_cap_usd must be > 0")

    return cfg


__all__ = [
    "XSourcesConfig", "XQuery", "RedditSub", "HNQuery",
    "XSourcesConfigError", "load_sources", "DEFAULT_CONFIG_PATH",
]
