"""W209 unit tests: AI ニュース深掘り (関連度スコア + Opus 深掘り + X fetcher).

カバレッジ:
- migration v60 冪等性 (init_db 2 回連続でデータ保持)
- score_relevance mock 経路 (Haiku を呼ばずに score 付与)
- deep_dive_article の budget gate (remaining<=0 → None)
- save_news_action_report の rowcount 動作 (新規→id 返却, 重複 url→None)
- fetch_x_entries: enabled=false の時 [] 返却
- run_news_check Phase 2/3 統合: score=0 で深掘り 0 件
- run_news_check Phase 3 統合: score>=60 が候補で deep_dive_article mock → 1 件保存

実 API は叩かない (mock / monkeypatch のみ)。
"""
from __future__ import annotations

import sqlite3

import pytest

from monitor import database as db


# ─────────────────────────────────────────────
# migration v60 冪等性
# ─────────────────────────────────────────────

def test_v60_idempotent_init_db_preserves_news_action_reports(tmp_path):
    """init_db 2 回連続でも news_action_reports のデータが消えないこと.

    Q2 db-migration-rules.md: ALTER は try/except OperationalError、
    CREATE は IF NOT EXISTS、DROP/DELETE は init_db に書かない。
    """
    db.init_db()
    # サンプル 1 件 INSERT (relevance_score / axis 列も含めて確認)
    rid = db.save_news_action_report(
        news_item_id=None,
        title="Test article",
        url="https://example.com/test-w209",
        axis="a",
        relevance_score=85,
        summary_ja="サマリ",
        target_module="claude_summarizer.py",
        integration_ja="プロンプト改善で対応",
        benefit_ja="要約品質 +10%",
        effort_estimate="S",
        confidence="high",
        model="claude-opus-4-8",
        cost_usd=0.012,
    )
    assert rid is not None and rid > 0
    # 再 init_db = 冪等のはず
    db.init_db()
    rows = db.get_news_action_reports_recent(days=7, limit=10)
    assert any(r["url"] == "https://example.com/test-w209" for r in rows), (
        "init_db 2 回連続でデータが消失 = 冪等性違反"
    )


def test_v60_news_items_has_relevance_columns():
    """news_items に relevance_score / relevance_axis 列が追加されていること."""
    db.init_db()
    with db.get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(news_items)").fetchall()}
    assert "relevance_score" in cols
    assert "relevance_axis" in cols


def test_v60_schema_version_bumped_to_60():
    """PRAGMA user_version が 60 まで bump されていること."""
    db.init_db()
    with db.get_conn() as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 60, f"schema_ver={ver} (60 以上を期待)"


# ─────────────────────────────────────────────
# save_news_action_report の rowcount 動作
# ─────────────────────────────────────────────

def test_save_news_action_report_returns_id_on_new():
    """新規 url で INSERT 成功時は lastrowid を返す."""
    db.init_db()
    rid = db.save_news_action_report(
        news_item_id=None, title="A", url="https://example.com/a-w209",
        axis="b", relevance_score=70, summary_ja="s", target_module="t",
        integration_ja="i", benefit_ja="b", effort_estimate="M",
        confidence="medium", model="claude-opus-4-8", cost_usd=0.005,
    )
    assert isinstance(rid, int) and rid > 0


def test_save_news_action_report_returns_none_on_duplicate_url():
    """UNIQUE(url) 違反時は IGNORE 経由で None を返す (silent skip ではなく
    呼出側が "既存だった" と判別可能)."""
    db.init_db()
    rid1 = db.save_news_action_report(
        news_item_id=None, title="A", url="https://example.com/dup-w209",
        axis="a", relevance_score=80, summary_ja="s", target_module="t",
        integration_ja="i", benefit_ja="b", effort_estimate="S",
        confidence="high", model="claude-opus-4-8", cost_usd=0.005,
    )
    assert rid1 is not None
    rid2 = db.save_news_action_report(
        news_item_id=None, title="A again", url="https://example.com/dup-w209",
        axis="a", relevance_score=80, summary_ja="s", target_module="t",
        integration_ja="i", benefit_ja="b", effort_estimate="S",
        confidence="high", model="claude-opus-4-8", cost_usd=0.005,
    )
    assert rid2 is None, "duplicate url で rid が返ってきた = UNIQUE 制約未適用"


# ─────────────────────────────────────────────
# score_relevance: Haiku を呼ばず monkeypatch で score 付与する経路
# ─────────────────────────────────────────────

def test_score_relevance_returns_safe_default_when_anthropic_missing(monkeypatch):
    """ANTHROPIC_API_KEY 削除時に raise せず安全 default を返す.

    本テストは生実装を検証するため、conftest の autouse patch を一旦戻す
    (test 内 monkeypatch.setattr で再度本物の関数を namespace に戻す).
    """
    import monitor.news_relevance as nr_module
    # 生実装 (autouse fixture が patch 前) を取り戻す
    # → conftest fixture が import 後に setattr した module attr を、
    #    本テスト内で「生関数」へ書き戻す
    real_score = nr_module.score_relevance.__wrapped__ if hasattr(
        nr_module.score_relevance, "__wrapped__"
    ) else None
    # __wrapped__ は存在しないので、module 再読み込みで生関数を取得
    import importlib
    fresh = importlib.reload(nr_module)
    # reload 後、conftest の patch が解除された状態の関数を test 内で再呼出
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = fresh.score_relevance("Some title", source="X")
    assert res["relevance_score"] == 0
    assert res["axis"] == "none"
    assert "ANTHROPIC_API_KEY" in res["reason_ja"]


def test_score_relevance_empty_title_returns_zero():
    """title 空入力 → score=0 / axis='none' (深掘り対象外).

    生実装の早期 return 経路 (API call 前) を検証.
    """
    import monitor.news_relevance as nr_module
    import importlib
    fresh = importlib.reload(nr_module)
    res = fresh.score_relevance("", source="X")
    assert res["relevance_score"] == 0
    assert res["axis"] == "none"


# ─────────────────────────────────────────────
# deep_dive_article の budget gate
# ─────────────────────────────────────────────

def test_deep_dive_article_returns_none_when_budget_zero(monkeypatch):
    """budget_remaining_usd <= 0 で None 返却 + 痕跡 (warning) を残す.

    Opus API を絶対に呼ばないことを担保 (anthropic Client を patch).
    生実装を検証するため、conftest の autouse patch を reload で外す.
    """
    import monitor.news_deep_dive as ndd_module
    import importlib
    ndd = importlib.reload(ndd_module)

    called = {"opus": False}

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            called["opus"] = True

        class messages:
            @staticmethod
            def create(**kw):
                raise AssertionError("Opus が呼ばれた = budget gate 失敗")

    monkeypatch.setattr(ndd, "anthropic",
                        type("M", (), {"Anthropic": _FakeAnthropic}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    item = {
        "title": "Test deep dive", "url": "", "source": "X",
        "relevance_score": 80, "axis": "a", "summary_ja": "",
    }
    result = ndd.deep_dive_article(item, budget_remaining_usd=0.0)
    assert result is None
    assert called["opus"] is False, "budget=0 で Anthropic client が初期化された"


def test_deep_dive_article_returns_none_for_empty_title():
    """title 空 → None (Q0: 痕跡 warning + 自然除外)."""
    import monitor.news_deep_dive as ndd_module
    import importlib
    ndd = importlib.reload(ndd_module)
    item = {"title": "  ", "url": "https://x", "source": "X"}
    assert ndd.deep_dive_article(item, budget_remaining_usd=0.45) is None


# ─────────────────────────────────────────────
# fetch_x_entries: enabled=false で空配列
# ─────────────────────────────────────────────

def test_fetch_x_entries_returns_empty_when_disabled(monkeypatch, tmp_path):
    """config/x_news_sources.json の enabled=false で [] を返す.

    config を tmp に差し替えてテスト隔離.
    """
    from tasks import task_news_fetch_x as tfx

    cfg_path = tmp_path / "x_news_sources.json"
    cfg_path.write_text(
        '{"enabled": false, "handles": [{"handle": "AnthropicAI", "axis_hint": "a"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tfx, "_CONFIG_PATH", cfg_path)

    entries = tfx.fetch_x_entries({"name": "X", "type": "x"})
    assert entries == []


def test_fetch_x_entries_returns_empty_when_no_handles(monkeypatch, tmp_path):
    """enabled=true でも handles 空なら [] (API を呼ばない)."""
    from tasks import task_news_fetch_x as tfx

    cfg_path = tmp_path / "x_news_sources.json"
    cfg_path.write_text('{"enabled": true, "handles": []}', encoding="utf-8")
    monkeypatch.setattr(tfx, "_CONFIG_PATH", cfg_path)

    entries = tfx.fetch_x_entries({"name": "X", "type": "x"})
    assert entries == []


def test_fetch_x_entries_respects_daily_cap(monkeypatch, tmp_path):
    """daily_query_cap を超えていたら [] (api_budget_log を mock)."""
    from tasks import task_news_fetch_x as tfx

    cfg_path = tmp_path / "x_news_sources.json"
    cfg_path.write_text(
        '{"enabled": true, "daily_query_cap": 2, '
        '"handles": [{"handle": "AnthropicAI", "axis_hint": "a"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tfx, "_CONFIG_PATH", cfg_path)
    # 既に 2 回呼ばれた状態をシミュレート
    monkeypatch.setattr(tfx, "_today_xai_query_count", lambda: 2)

    called = {"search": False}
    monkeypatch.setattr(
        "monitor.xai_wrapper.search_x_posts",
        lambda *a, **kw: called.__setitem__("search", True) or [],
    )

    entries = tfx.fetch_x_entries({"name": "X", "type": "x"})
    assert entries == []
    assert called["search"] is False, "cap 到達なのに search_x_posts が呼ばれた"


def test_fetch_x_entries_counts_cap_even_on_empty_posts(monkeypatch, tmp_path):
    """HIGH-1 回帰 (2026-06-02): search_x_posts が [] を返しても cap 消費が
    記録されること。課金されたが posts 空のケースで add_api_cost が呼ばれず
    _today_xai_query_count が増えない = daily_query_cap 青天井課金を防ぐ。"""
    from tasks import task_news_fetch_x as tfx

    cfg_path = tmp_path / "x_news_sources.json"
    cfg_path.write_text(
        '{"enabled": true, "daily_query_cap": 2, '
        '"handles": [{"handle": "AnthropicAI", "axis_hint": "a"}, '
        '{"handle": "claudeai", "axis_hint": "a"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tfx, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(tfx, "_today_xai_query_count", lambda: 0)
    # 課金されたが空応答 (x_search ヒット0 / 抽出失敗) をシミュレート
    monkeypatch.setattr("monitor.xai_wrapper.search_x_posts", lambda *a, **kw: [])

    cost_calls: list = []
    monkeypatch.setattr(
        "monitor.database.add_api_cost",
        lambda provider, cost, context=None: cost_calls.append((provider, context)),
    )

    entries = tfx.fetch_x_entries({"name": "X", "type": "x"})
    assert entries == []
    # 空応答でも 2 handle 分の cap 消費が記録されること (修正前は 0 件 = FAIL)
    news_x_calls = [c for c in cost_calls if c == ("xai", "news_x")]
    assert len(news_x_calls) == 2, (
        f"空応答時に cap 消費が記録されていない (news_x calls={len(news_x_calls)})"
    )


# ─────────────────────────────────────────────
# run_news_check 統合: Phase 2/3 が score=0 で深掘り 0 件
# ─────────────────────────────────────────────

def _patch_summarizer_off(monkeypatch):
    """claude_summarizer.summarize_news を no-op."""
    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_news",
        lambda title, source="": None,
        raising=False,
    )


def test_run_news_check_phase3_skips_when_all_scores_below_threshold(
    monkeypatch, tmp_path
):
    """全 item の relevance_score=0 → 深掘り 0 件 + skipped_reason 明示 (Q0)."""
    from tasks import task_news_check as t
    import os
    monkeypatch.chdir(tmp_path)
    db.init_db()  # v60 schema が必要 (news_items.relevance_score 列)
    _patch_summarizer_off(monkeypatch)

    # 全 fetcher 経路で 1 件ずつ返す
    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "AAA", "url": "https://r1", "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [
        {"title": "BBB", "url": "https://r2", "published_at": "",
         "extra_score": 100},
    ])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [
        {"title": "CCC", "url": "https://r3", "published_at": "",
         "extra_points": 200},
    ])
    monkeypatch.setattr(t, "fetch_x_entries", lambda src: [])

    # score_relevance を全部 0 / 'none' で patch (Haiku を呼ばない)
    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 0, "axis": "none", "reason_ja": "low rel",
        },
    )

    # deep_dive_article は呼ばれてはいけない
    called = {"opus": False}
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: called.__setitem__(
            "opus", True
        ) or None,
    )

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["deep_dive_count"] == 0
    assert "no candidates" in (result.get("deep_dive_skipped_reason") or ""), (
        result.get("deep_dive_skipped_reason")
    )
    assert called["opus"] is False, "score=0 なのに Opus が呼ばれた"


def test_run_news_check_phase3_runs_for_high_score_items(monkeypatch, tmp_path):
    """relevance_score>=60 の item が deep_dive_article へ渡る経路 (mock).

    score>=60 で 1 件、深掘り mock が成功 → news_action_reports に 1 件保存。
    """
    from tasks import task_news_check as t
    import os
    monkeypatch.chdir(tmp_path)
    db.init_db()
    _patch_summarizer_off(monkeypatch)

    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "Introducing Claude Opus 5",
         "url": "https://example.com/opus5-w209",
         "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_x_entries", lambda src: [])

    # スコア >= 60 を返す
    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 85, "axis": "a",
            "reason_ja": "Claude 新モデル",
        },
    )

    # Opus を mock (実 API を呼ばずに JSON を返す)
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: {
            "summary_ja": "Opus 5 が出た",
            "target_module": "claude_summarizer.py",
            "integration_ja": "MODEL 定数を入れ替える",
            "benefit_ja": "要約品質向上",
            "effort_estimate": "S",
            "confidence": "high",
            "model": "claude-opus-4-8",
            "cost_usd": 0.02,
        },
    )

    # budget は十分残ってる状態を保証 (get_todays_api_cost_by_context を 0 で patch)
    monkeypatch.setattr(
        "monitor.database.get_todays_api_cost_by_context",
        lambda context, provider=None: 0.0,
    )

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["deep_dive_count"] == 1, (
        f"deep_dive_count={result['deep_dive_count']} / "
        f"skipped_reason={result.get('deep_dive_skipped_reason')!r}"
    )
    # news_action_reports に保存されたこと
    rows = db.get_news_action_reports_recent(days=1, limit=10)
    assert any(r["url"] == "https://example.com/opus5-w209" for r in rows)


def test_run_news_check_phase3_skips_when_budget_used_up(monkeypatch, tmp_path):
    """sub-budget (news_deep_dive) 0.45 を超えていたら深掘り skip + 痕跡."""
    from tasks import task_news_check as t
    import os
    monkeypatch.chdir(tmp_path)
    db.init_db()
    _patch_summarizer_off(monkeypatch)

    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "High score article",
         "url": "https://example.com/budget-w209",
         "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_x_entries", lambda src: [])

    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 90, "axis": "a", "reason_ja": "test",
        },
    )

    # 既に $1.00 消費した状態 (cap $0.45 を超過)
    monkeypatch.setattr(
        "monitor.database.get_todays_api_cost_by_context",
        lambda context, provider=None: 1.0,
    )

    # deep_dive_article は呼ばれない
    called = {"opus": False}
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: called.__setitem__(
            "opus", True
        ) or None,
    )

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["deep_dive_count"] == 0
    assert "budget" in (result.get("deep_dive_skipped_reason") or "")
    assert called["opus"] is False


def test_run_news_check_phase3_stops_before_overshoot_on_thin_margin(
    monkeypatch, tmp_path
):
    """HIGH-2 回帰 (2026-06-02): 残量が安全マージン ($0.15) 未満なら起動しない。

    used=$0.35 (残量 $0.10 < margin $0.15) で初期 gate が skip し、Opus を
    呼ばない = $0.45 を 1 件分オーバーシュートする経路を遮断。
    """
    from tasks import task_news_check as t
    monkeypatch.chdir(tmp_path)
    db.init_db()
    _patch_summarizer_off(monkeypatch)

    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "High score article",
         "url": "https://example.com/thin-margin-w209",
         "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_x_entries", lambda src: [])

    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 90, "axis": "a", "reason_ja": "test",
        },
    )
    # 残量 $0.10 (= 0.45 - 0.35) < margin $0.15 → 起動しない
    monkeypatch.setattr(
        "monitor.database.get_todays_api_cost_by_context",
        lambda context, provider=None: 0.35,
    )
    called = {"opus": False}
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: called.__setitem__(
            "opus", True
        ) or None,
    )

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["deep_dive_count"] == 0
    assert called["opus"] is False, "残量マージン未満なのに Opus が呼ばれた"
    assert "insufficient" in (result.get("deep_dive_skipped_reason") or ""), (
        result.get("deep_dive_skipped_reason")
    )


def test_run_news_check_phase3_fail_closed_on_budget_query_error(
    monkeypatch, tmp_path
):
    """Codex #2 回帰 (2026-06-02): budget 集計が例外なら fail-CLOSED で深掘り skip。

    予算を確認できない時に fail-open (used=0 で続行) すると手動 trigger 連投で
    日次上限を保証できない。「確認不能 = 課金しない」を assert。
    """
    from tasks import task_news_check as t
    monkeypatch.chdir(tmp_path)
    db.init_db()
    _patch_summarizer_off(monkeypatch)

    monkeypatch.setattr(t, "fetch_rss_entries", lambda src: [
        {"title": "High score", "url": "https://example.com/failclosed-w209",
         "published_at": ""},
    ])
    monkeypatch.setattr(t, "fetch_reddit_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_hn_entries", lambda src: [])
    monkeypatch.setattr(t, "fetch_x_entries", lambda src: [])
    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 90, "axis": "a", "reason_ja": "test",
        },
    )

    def _raise(context, provider=None):
        raise RuntimeError("DB locked")
    monkeypatch.setattr(
        "monitor.database.get_todays_api_cost_by_context", _raise,
    )
    called = {"opus": False}
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: called.__setitem__(
            "opus", True
        ) or None,
    )

    result = t.run_news_check({})

    assert result["success"] is True
    assert result["deep_dive_count"] == 0
    assert called["opus"] is False, "budget 確認不能なのに Opus が呼ばれた (fail-open)"
    assert "fail-closed" in (result.get("deep_dive_skipped_reason") or "")


def test_fetch_article_text_rejects_private_hosts():
    """Codex #3 回帰: SSRF 防御。private/loopback/link-local host を拒否。"""
    from monitor.news_deep_dive import _url_host_is_public
    # IP リテラルは DNS 不要で getaddrinfo がローカル解決 = 決定的
    assert _url_host_is_public("http://127.0.0.1/x") is False
    assert _url_host_is_public("http://10.0.0.5/x") is False
    assert _url_host_is_public("http://169.254.169.254/latest/meta-data") is False
    assert _url_host_is_public("http://192.168.1.1/x") is False
    assert _url_host_is_public("not-a-url") is False


def test_fetch_article_text_allows_public_host(monkeypatch):
    """SSRF 防御が public IP は通すこと (getaddrinfo を public IP に mock)."""
    import monitor.news_deep_dive as ndd
    monkeypatch.setattr(
        ndd.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert ndd._url_host_is_public("https://example.com/article") is True


# ─────────────────────────────────────────────
# news_items への relevance_score / axis 永続化
# ─────────────────────────────────────────────

def test_save_news_results_persists_relevance_columns(monkeypatch, tmp_path):
    """save_news_results が relevance_score / relevance_axis を news_items に書き込む."""
    db.init_db()
    monkeypatch.chdir(tmp_path)
    _patch_summarizer_off(monkeypatch)

    from tasks.task_news_check import save_news_results
    news = [{
        "title": "Score persist test",
        "source": "Anthropic News",
        "impact": "high",
        "matched_keyword": "introducing",
        "url": "https://example.com/persist-w209",
        "relevance_score": 72,
        "relevance_axis": "b",
    }]
    inserted = save_news_results(news)
    assert inserted == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT relevance_score, relevance_axis FROM news_items "
            "WHERE url = ?",
            ("https://example.com/persist-w209",),
        ).fetchone()
    assert row is not None
    assert row[0] == 72
    assert row[1] == "b"


# ─────────────────────────────────────────────
# get_todays_api_cost_by_context 動作
# ─────────────────────────────────────────────

def test_get_todays_api_cost_by_context_filters_correctly():
    """add_api_cost + get_todays_api_cost_by_context の往復が正しい."""
    db.init_db()
    db.add_api_cost("anthropic", 0.10, context="news_deep_dive")
    db.add_api_cost("anthropic", 0.05, context="news_relevance")
    db.add_api_cost("xai", 0.02, context="news_x")

    assert db.get_todays_api_cost_by_context("news_deep_dive") == pytest.approx(0.10)
    assert db.get_todays_api_cost_by_context("news_relevance") == pytest.approx(0.05)
    assert db.get_todays_api_cost_by_context("news_x") == pytest.approx(0.02)
    # provider filter
    assert db.get_todays_api_cost_by_context(
        "news_deep_dive", provider="anthropic"
    ) == pytest.approx(0.10)
    assert db.get_todays_api_cost_by_context(
        "news_deep_dive", provider="xai"
    ) == pytest.approx(0.0)
