"""W55 task_news_check INSERT silent skip 修正の regression test.

2026-04-30 W55: inserted += 1 無条件加算 (偽装 counter) + 関数全体 try/except Exception
+ 全 URL 失敗時 success=True で偽装成功. cursor.rowcount で実 INSERT 件数を集計し,
fetched_titles_total == 0 で RuntimeError raise (外部経路全滅検出).
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


def test_run_news_check_raises_when_all_urls_fail(temp_db, monkeypatch):
    """全 URL から fetch_html_titles が [] を返す = 外部経路全滅 → success=False.

    現状の outer except が RuntimeError を捕捉して success=False を返す.
    silent skip ("0 件 success=True") 偽装成功を防ぐ.

    本ケースは fetch 全滅で all_news=[] となり save_news_results に到達しないため,
    summarizer の patch は不要 (M-1: test 意図の明確化).
    """
    from tasks import task_news_check as t

    monkeypatch.setattr(t, "fetch_html_titles", lambda url, timeout=10: [])

    result = t.run_news_check({})

    assert result["success"] is False
    assert result.get("inserted_count") == 0
    # 例外メッセージは brittle なので key だけ確認
    assert "error" in result
