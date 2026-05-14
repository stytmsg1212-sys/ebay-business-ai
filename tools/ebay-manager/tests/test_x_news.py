#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W13 X news パイプラインの unit tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from monitor.x_news_fetchers import NewsRaw
from monitor.x_news_pipeline import (
    _normalize_title, _title_similarity, dedupe_local,
)
from monitor.x_news_sources import (
    XSourcesConfigError, load_sources,
)


# ─────────────────────────────
# sources config loader
# ─────────────────────────────

def _write_json(obj: dict) -> Path:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(obj, f)
    f.close()
    return Path(f.name)


def test_load_sources_valid():
    path = _write_json({
        "x": {"enabled": True, "handles": ["@a"],
              "queries": [{"keyword": "x", "days": 1}]},
        "reddit": {"enabled": True, "subreddits": [
            {"name": "ClaudeAI", "listing": "hot", "limit": 10}]},
        "hn": {"enabled": False, "queries": []},
        "budget": {"xai_daily_cap_usd": 1.0, "anthropic_daily_cap_usd": 0.5},
    })
    cfg = load_sources(path)
    assert cfg.x_enabled is True
    assert len(cfg.x_handles) == 1
    assert cfg.x_queries[0].keyword == "x"
    assert cfg.x_queries[0].days == 1
    assert cfg.reddit_subs[0].name == "ClaudeAI"
    assert cfg.hn_enabled is False
    assert cfg.xai_daily_cap_usd == 1.0
    path.unlink()


def test_load_sources_handles_over_limit():
    """x.handles > 10 should raise (search-x API limit)."""
    path = _write_json({"x": {"handles": [f"@u{i}" for i in range(11)]}})
    with pytest.raises(XSourcesConfigError, match="handles"):
        load_sources(path)
    path.unlink()


def test_load_sources_missing_keyword():
    path = _write_json({"x": {"queries": [{"days": 2}]}})
    with pytest.raises(XSourcesConfigError, match="keyword"):
        load_sources(path)
    path.unlink()


def test_load_sources_invalid_listing():
    path = _write_json({
        "reddit": {"subreddits": [
            {"name": "x", "listing": "BAD", "limit": 5}
        ]},
    })
    with pytest.raises(XSourcesConfigError, match="listing"):
        load_sources(path)
    path.unlink()


# ─────────────────────────────
# dedupe
# ─────────────────────────────

def test_normalize_title_removes_punct_and_case():
    assert _normalize_title("Hello, World!") == "hello world"
    assert _normalize_title("  AI   News   ") == "ai news"


def test_title_similarity_identical_is_1():
    assert _title_similarity("Claude Opus 4.7", "Claude Opus 4.7") == 1.0


def test_title_similarity_different_is_low():
    # 全く別のトピック
    assert _title_similarity(
        "Claude Opus released",
        "Tariff news today in US"
    ) < 0.5


def test_dedupe_url_match_keeps_higher_engagement():
    items = [
        NewsRaw(source_type="x", source_handle="@a",
                url="https://x.com/1", title="A", engagement_count=10),
        NewsRaw(source_type="x", source_handle="@b",
                url="https://x.com/1", title="A dup", engagement_count=100),
    ]
    kept = dedupe_local(items)
    assert len(kept) == 1
    assert kept[0].engagement_count == 100


def test_dedupe_title_similar_removed():
    items = [
        NewsRaw(source_type="x", source_handle="@a",
                url="https://x.com/1",
                title="Claude Opus 4.7 released today",
                engagement_count=100),
        NewsRaw(source_type="reddit", source_handle="r/ClaudeAI",
                url="https://reddit.com/r/ClaudeAI/2",
                title="Claude Opus 4.7 released today!!!",
                engagement_count=50),
    ]
    kept = dedupe_local(items)
    assert len(kept) == 1
    # engagement 高い方が残る
    assert kept[0].engagement_count == 100


def test_dedupe_distinct_titles_both_kept():
    items = [
        NewsRaw(source_type="x", source_handle="@a",
                url="https://x.com/1", title="Claude Opus 4.7",
                engagement_count=100),
        NewsRaw(source_type="x", source_handle="@b",
                url="https://x.com/2", title="Tariff policy update US",
                engagement_count=80),
    ]
    kept = dedupe_local(items)
    assert len(kept) == 2


def test_dedupe_no_url_still_dedupe_by_title():
    items = [
        NewsRaw(source_type="x", source_handle="@a",
                url="", title="GPT-5 released today",
                engagement_count=50),
        NewsRaw(source_type="hn", source_handle="hn",
                url="", title="GPT-5 released today!",
                engagement_count=30),
    ]
    kept = dedupe_local(items)
    assert len(kept) == 1


# ─────────────────────────────
# xai_wrapper sanity
# ─────────────────────────────

def test_xai_wrapper_missing_key_raises(monkeypatch):
    from monitor.xai_wrapper import XaiConfigError, call_search_x
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(XaiConfigError, match="XAI_API_KEY"):
        call_search_x("anything")


# ─────────────────────────────
# DB helpers (api_budget_log, save_news_item_v2)
# ─────────────────────────────

def test_api_budget_atomic_accumulation():
    from monitor.database import add_api_cost, get_todays_api_cost, get_conn
    provider = "_test_w13_provider"
    try:
        total = get_todays_api_cost(provider)
        assert total == 0.0
        c1 = add_api_cost(provider, 0.01, "t1")
        c2 = add_api_cost(provider, 0.02, "t2")
        assert abs(c1 - 0.01) < 1e-9
        assert abs(c2 - 0.03) < 1e-9
        assert abs(get_todays_api_cost(provider) - 0.03) < 1e-9
    finally:
        with get_conn() as c:
            c.execute("DELETE FROM api_budget_log WHERE provider = ?", (provider,))


def test_save_news_item_v2_url_dedup_and_engagement_update():
    from monitor.database import save_news_item_v2, get_conn
    url = "https://example.test/w13-unit-test-url-1"
    try:
        id1 = save_news_item_v2(
            source="test_w13", title="W13 test title 1", url=url,
            source_type="x", source_handle="@w13test",
            engagement_count=10,
        )
        assert id1 is not None
        id2 = save_news_item_v2(
            source="test_w13_dup", title="W13 test title dup", url=url,
            source_type="x", source_handle="@w13test",
            engagement_count=50,
        )
        assert id2 is None  # URL 一致で insert skip
        with get_conn() as c:
            row = c.execute(
                "SELECT engagement_count FROM news_items WHERE url = ?", (url,)
            ).fetchone()
            assert int(row[0]) == 50  # engagement 最大値に更新
    finally:
        with get_conn() as c:
            c.execute("DELETE FROM news_items WHERE url = ?", (url,))


# ─────────────────────────────
# H-W13-2 回帰テスト: budget 枯渇時 noise 化
# ─────────────────────────────

def test_passthrough_impact_level_is_noise():
    """code-reviewer H-W13-2: classifier skip 時は 'noise' を返すこと."""
    from monitor.x_news_pipeline import _passthrough
    raw = NewsRaw(
        source_type="x", source_handle="@test", url="https://x.com/test",
        title="Budget exhausted sample", engagement_count=0,
    )
    c = _passthrough(raw)
    assert c.impact_level == "noise", \
        "passthrough must set 'noise' so task skips DB save"
    assert c.extra.get("classifier_skipped") is True


def test_classify_batch_without_api_key_returns_noise(monkeypatch):
    """ANTHROPIC_API_KEY 未設定 → 全 item が noise 扱いで返る."""
    from monitor.x_news_pipeline import classify_batch
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    items = [
        NewsRaw(source_type="x", source_handle="@a",
                url="https://x.com/1", title="test 1"),
        NewsRaw(source_type="x", source_handle="@b",
                url="https://x.com/2", title="test 2"),
    ]
    results = classify_batch(items, daily_cap_usd=1.0)
    assert len(results) == 2
    for r in results:
        assert r.impact_level == "noise"
        assert r.extra.get("classifier_skipped") is True


# ─────────────────────────────
# Grok Responses API 形式のパース (2026-04-24 実応答確認後)
# ─────────────────────────────

def test_extract_x_posts_grok_responses_format():
    """Grok の output[].content[].annotations 形式を正しくパースする."""
    from monitor.x_news_fetchers import _extract_x_posts
    data = {
        "output": [
            {"type": "custom_tool_call", "name": "x_keyword_search"},
            {
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": (
                        "Here are real recent X posts about Claude. "
                        "[[1]](https://x.com/claudeai/status/2047661852) "
                        "Introducing Claude Design by Anthropic Labs: "
                        "make prototypes by talking to Claude. "
                        "[[2]](https://x.com/anthropic/status/2044785261) "
                        "Claude Opus 4.7 released today with improved "
                        "reasoning capabilities."
                    ),
                    "annotations": [
                        {"type": "url_citation",
                         "url": "https://x.com/claudeai/status/2047661852",
                         "start_index": 50, "end_index": 100, "title": "1"},
                        {"type": "url_citation",
                         "url": "https://x.com/anthropic/status/2044785261",
                         "start_index": 180, "end_index": 230, "title": "2"},
                    ],
                }],
            },
        ]
    }
    posts = _extract_x_posts(data)
    assert len(posts) == 2
    # ハンドル抽出チェック
    handles = {p["handle"] for p in posts}
    assert handles == {"@claudeai", "@anthropic"}
    # URL 保存チェック
    urls = {p["url"] for p in posts}
    assert "https://x.com/claudeai/status/2047661852" in urls
    assert "https://x.com/anthropic/status/2044785261" in urls


def test_extract_x_posts_empty_output_returns_empty():
    from monitor.x_news_fetchers import _extract_x_posts
    assert _extract_x_posts({"output": []}) == []
    assert _extract_x_posts({}) == []


def test_extract_x_posts_legacy_format_still_works():
    """旧 dict 形式 (posts/tweets) との後方互換."""
    from monitor.x_news_fetchers import _extract_x_posts
    data = {"posts": [
        {"url": "https://x.com/u/1", "handle": "u", "text": "hello"},
    ]}
    posts = _extract_x_posts(data)
    assert len(posts) == 1
    assert posts[0]["handle"] == "@u"
