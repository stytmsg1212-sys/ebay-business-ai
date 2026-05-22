"""W55 task_news_check INSERT silent skip 修正の regression test + W154 統合版 update.

2026-04-30 W55: inserted += 1 無条件加算 (偽装 counter) + 関数全体 try/except Exception
+ 全 URL 失敗時 success=True で偽装成功. cursor.rowcount で実 INSERT 件数を集計し,
fetched_entries_total == 0 で RuntimeError raise (外部経路全滅検出).

2026-05-22 W154: 旧 fetch_html_titles → fetch_rss_entries / fetch_reddit_entries /
fetch_hn_entries に分割. test_run_news_check_raises は 3 fetcher を全て [] パッチ.
"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from monitor import database as db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """クリーンな SQLite を一時作成して news_items テーブルだけ用意する.

    schema は monitor/database.py L766-781 の v8 migration と整合させる.
    UNIQUE(source, title) 制約が rowcount=0 IGNORE 経路の根拠.
    """
    tmp_db = tmp_path / "monitor.db"
    monkeypatch.setattr(db, "DB_PATH", str(tmp_db))
    with sqlite3.connect(str(tmp_db)) as con:
        con.execute(
            """CREATE TABLE news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                title TEXT,
                url TEXT,
                summary_ja TEXT,
                impact_ja TEXT,
                impact_level TEXT,
                categories TEXT,
                published_at TEXT,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, title)
            )"""
        )
    return tmp_db


@pytest.fixture
def sample_news(tmp_path, monkeypatch):
    """news_items に渡す 3 件サンプル. file 出力先も tmp に逃がす."""
    monkeypatch.chdir(tmp_path)  # data/news/ が tmp 配下に切られるよう CWD を切替
    return [
        {
            "title": "Introducing Claude Opus 4.7",
            "source": "Anthropic News",
            "impact": "high",
            "matched_keyword": "introducing",
            "url": "https://www.anthropic.com/news/opus-4-7",
        },
        {
            "title": "Engineering: Claude Code performance update",
            "source": "Anthropic Engineering Blog",
            "impact": "medium",
            "matched_keyword": "performance",
            "url": "https://www.anthropic.com/engineering/cc-perf",
        },
        {
            "title": "New API: batch processing GA",
            "source": "Anthropic News",
            "impact": "high",
            "matched_keyword": "ga release",
            "url": "https://www.anthropic.com/news/batch-ga",
        },
    ]


def _patch_summarizer(monkeypatch, side_effect=None):
    """claude_summarizer.summarize_news を no-op or 失敗 で固定."""
    if side_effect is None:
        fake = lambda title, source="": None  # noqa: E731
    else:
        fake = mock.Mock(side_effect=side_effect)
    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_news", fake, raising=False
    )


def test_save_news_results_inserted_count_all_new(temp_db, sample_news, monkeypatch):
    """全件新規: rowcount=1 を len(news_items) 回 → inserted=len."""
    _patch_summarizer(monkeypatch)
    from tasks.task_news_check import save_news_results

    inserted = save_news_results(sample_news)
    assert inserted == len(sample_news)


def test_save_news_results_inserted_count_all_duplicate(temp_db, sample_news, monkeypatch):
    """同一 items を 2 回呼出: 2 回目は事前 SELECT で全件 continue → inserted=0.

    rowcount 集計の正しさだけでなく, "事前 SELECT による無 INSERT" でも
    inserted=0 になることを保証 (rowcount を加算しないため).
    """
    _patch_summarizer(monkeypatch)
    from tasks.task_news_check import save_news_results

    first = save_news_results(sample_news)
    assert first == len(sample_news), f"初回 INSERT 想定 {len(sample_news)} 件 / 実 {first} 件"
    inserted = save_news_results(sample_news)  # 2 回目: 全件事前 SELECT で skip
    assert inserted == 0


def test_save_news_results_enrichment_failure_does_not_break_insert(
    temp_db, sample_news, monkeypatch
):
    """regression: claude_summarizer raise でも INSERT は通り inserted=len.

    旧コードでは関数全体を try/except Exception でラップしていたため, 個別
    enrichment 失敗が全件 silent skip に転化していた.
    """
    _patch_summarizer(monkeypatch, side_effect=RuntimeError("claude down"))
    from tasks.task_news_check import save_news_results

    inserted = save_news_results(sample_news)
    assert inserted == len(sample_news)


def test_run_news_check_raises_when_all_sources_fail(temp_db, monkeypatch):
    """全 fetcher が [] を返す = 外部経路全滅 → success=False.

    W154 (2026-05-22): 旧 fetch_html_titles → fetch_rss/reddit/hn の 3 経路分割.
    現状の outer except が RuntimeError を捕捉して success=False を返す.
    silent skip ("0 件 success=True") 偽装成功を防ぐ (Q0).

    本ケースは fetch 全滅で all_news=[] となり save_news_results に到達しないため,
    summarizer の patch は不要 (M-1: test 意図の明確化).
    """
    from tasks import task_news_check as t

    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [])

    result = t.run_news_check({})

    assert result["success"] is False
    assert result.get("inserted_count") == 0
    # 例外メッセージは brittle なので key だけ確認
    assert "error" in result


# =============================================================================
# W154 新規 unit tests: fetcher 系 + impact assessment + happy path
# =============================================================================

def test_assess_impact_open_challenge_keyword_wins():
    """OPEN_CHALLENGE_KEYWORDS hit は high + challenge tag (IMPACT_KEYWORDS より優先)."""
    from tasks.task_news_check import assess_impact

    res = assess_impact("Seedream V4 enables multi-reference image composition")
    assert res["level"] == "high"
    assert res.get("challenge") == "CHAL-001"
    assert res["matched_keyword"] in ("seedream", "image composition", "multi-reference")


def test_assess_impact_high_keyword_match():
    """IMPACT_KEYWORDS['high'] の任意のキーワードヒット → high."""
    from tasks.task_news_check import assess_impact

    res = assess_impact("Anthropic announces new model with sonnet 5 capabilities")
    assert res["level"] == "high"


def test_assess_impact_no_match_returns_none():
    """関連キーワード無し → level='none'."""
    from tasks.task_news_check import assess_impact

    res = assess_impact("Weather forecast: sunny tomorrow")
    assert res["level"] == "none"
    assert res["matched_keyword"] is None


def test_filter_relevant_news_empty_keywords_passes_all():
    """source_keywords=[] (Tier 1 編集メディア feed) は全件 relevant 化."""
    from tasks.task_news_check import filter_relevant_news

    entries = [
        {"title": "Random news A", "url": "u1"},
        {"title": "Random news B", "url": "u2"},
    ]
    out = filter_relevant_news(entries, source_keywords=[])
    assert len(out) == 2
    # impact level / matched_keyword フィールドが追加されている
    assert all("impact" in o for o in out)


def test_filter_relevant_news_keyword_filter_works():
    """source_keywords が指定されたら一致するもののみ通る."""
    from tasks.task_news_check import filter_relevant_news

    entries = [
        {"title": "Claude Opus 4.7 released", "url": "u1"},
        {"title": "Random gardening tips", "url": "u2"},
        {"title": "Anthropic new API", "url": "u3"},
    ]
    out = filter_relevant_news(entries, source_keywords=["claude", "anthropic"])
    titles = [o["title"] for o in out]
    assert "Claude Opus 4.7 released" in titles
    assert "Anthropic new API" in titles
    assert "Random gardening tips" not in titles


# ---- fetch_rss_entries (httpx + feedparser mock) ----

class _FakeHttpxResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


class _FakeHttpxClient:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kwargs):
        return self._resp


def test_fetch_rss_entries_parses_minimal_rss(monkeypatch):
    """正常な RSS XML を渡したら entries が title / url / published_at で返る."""
    rss_xml = (
        b"<?xml version='1.0' encoding='UTF-8'?>\n"
        b"<rss version='2.0'><channel>\n"
        b"<title>Test Feed</title>\n"
        b"<item><title>Item A</title><link>https://example.com/a</link>"
        b"<pubDate>Wed, 22 May 2026 10:00:00 GMT</pubDate></item>\n"
        b"<item><title>Item B</title><link>https://example.com/b</link>"
        b"<pubDate>Wed, 22 May 2026 11:00:00 GMT</pubDate></item>\n"
        b"</channel></rss>"
    )
    from tasks import task_news_check as t

    # patch httpx.Client to return the static RSS bytes
    fake_resp = _FakeHttpxResponse(status_code=200, content=rss_xml)
    monkeypatch.setattr(t.httpx, "Client", lambda *a, **kw: _FakeHttpxClient(fake_resp))

    entries = t.fetch_rss_entries({"name": "Test", "url": "https://x", "type": "rss"})
    assert len(entries) == 2
    titles = [e["title"] for e in entries]
    assert "Item A" in titles
    assert "Item B" in titles
    assert entries[0]["url"].startswith("https://example.com/")
    assert entries[0]["published_at"]  # 非空


def test_fetch_rss_entries_http_error_returns_empty(monkeypatch):
    """HTTP 404 等 → 空 list (raise しない、他 source 続行できるため)."""
    from tasks import task_news_check as t

    fake_resp = _FakeHttpxResponse(status_code=404, content=b"")
    monkeypatch.setattr(t.httpx, "Client", lambda *a, **kw: _FakeHttpxClient(fake_resp))

    entries = t.fetch_rss_entries({"name": "Bad", "url": "https://x", "type": "rss"})
    assert entries == []


# ---- fetch_reddit_entries ----

def test_fetch_reddit_entries_filters_min_score(monkeypatch):
    """min_score 未満の post は除外される."""
    from tasks import task_news_check as t

    fake_data = {
        "data": {
            "children": [
                {"data": {"title": "High score post", "score": 100,
                          "permalink": "/r/ClaudeAI/x", "created_utc": 1716355200}},
                {"data": {"title": "Low score noise", "score": 10,
                          "permalink": "/r/ClaudeAI/y", "created_utc": 1716355300}},
            ]
        }
    }

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return fake_data

    class _Client(_FakeHttpxClient):
        def get(self, url, **kwargs):
            return _FakeResp()

    monkeypatch.setattr(t.httpx, "Client", lambda *a, **kw: _Client(None))

    entries = t.fetch_reddit_entries(
        {"name": "r/ClaudeAI", "subreddit": "ClaudeAI", "limit": 10}
    )
    titles = [e["title"] for e in entries]
    assert "High score post" in titles
    assert "Low score noise" not in titles


def test_fetch_reddit_entries_403_returns_empty(monkeypatch):
    """Reddit 403 (botban / rate limit) で空 list 返却、例外 raise しない."""
    from tasks import task_news_check as t

    class _FakeResp:
        status_code = 403

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class _Client(_FakeHttpxClient):
        def get(self, url, **kwargs):
            return _FakeResp()

    monkeypatch.setattr(t.httpx, "Client", lambda *a, **kw: _Client(None))

    entries = t.fetch_reddit_entries({"name": "r/ClaudeAI", "subreddit": "ClaudeAI"})
    assert entries == []


# ---- fetch_hn_entries ----

def test_fetch_hn_entries_happy_path(monkeypatch):
    """HN Algolia の hits[] が title / url / points 付きで返る."""
    from tasks import task_news_check as t

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": [
                {"title": "Claude Opus 4.7 launches",
                 "url": "https://www.anthropic.com/news/opus-4-7",
                 "points": 500, "created_at": "2026-05-22T10:00:00",
                 "objectID": "12345"},
                {"title": "Discussion: agent SDK best practices", "url": "",
                 "points": 120, "created_at": "2026-05-21T15:00:00",
                 "objectID": "12346"},
            ]}

    class _Client(_FakeHttpxClient):
        def get(self, url, **kwargs):
            return _FakeResp()

    monkeypatch.setattr(t.httpx, "Client", lambda *a, **kw: _Client(None))

    entries = t.fetch_hn_entries(
        {"name": "HN: claude", "query": "claude", "days": 3, "max": 15}
    )
    assert len(entries) == 2
    # objectID fallback URL
    assert entries[1]["url"].startswith("https://news.ycombinator.com/item?id=")
    assert entries[0]["extra_points"] == 500


# ---- run_news_check happy path with mocked fetchers ----

def test_run_news_check_happy_path_with_mocked_fetchers(temp_db, monkeypatch):
    """全 fetcher を patch して happy path: success=True, news_count > 0."""
    from tasks import task_news_check as t

    # tmp_path 配下に CWD して data/news/*.json 作成先を分離
    import os
    monkeypatch.chdir(os.path.dirname(temp_db))

    _patch_summarizer(monkeypatch)

    # RSS / Reddit / HN それぞれが 1 件ずつ返すように patch
    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "Introducing Claude Opus 4.7", "url": "https://x", "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [
        {"title": "Discussion: new agent SDK", "url": "https://reddit/x",
         "published_at": "", "extra_score": 100},
    ])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [
        {"title": "Anthropic announces enterprise tier", "url": "https://hn/x",
         "published_at": "", "extra_points": 200},
    ])

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["raw_count"] > 0
    assert result["news_count"] > 0
    assert "per_source" in result
    # 高影響キーワード ("introducing" / "enterprise") が hit している
    assert result["high_impact_count"] >= 1
