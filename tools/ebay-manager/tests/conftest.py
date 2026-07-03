"""pytest 共通 fixture.

2026-05-25 追加: 全テストで `monitor.database.DB_PATH` を tmp_path 配下に隔離.
本番 `data/monitor.db` への汚染防止 (5/05〜5/24 で `simulated task crash` 64 件
偽 failed が `task_execution_log` に蓄積されていた事故対応).

2026-06-02 追加 (W209): score_relevance / deep_dive_article を no-op patch.
全テストで Haiku / Opus の実 API call を block (CI 安定性 + API budget 保護).
個別 test は monkeypatch で再上書き可能.

詳細: `.claude/rules/db-migration-rules.md` (本番 DB 直接書込原則禁止) /
      `.claude/rules/silent-skip-prevention.md` Q0 (健康 alert noise = silent skip 検知能力低下).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_monitor_db(monkeypatch, tmp_path):
    """`monitor.database.DB_PATH` を test 専用 tmp dir に強制差し替え.

    `get_conn()` は呼び出し時に module 変数 `DB_PATH` を `str()` で評価するため、
    monkeypatch で module attr を上書きすれば既存テストの import を変えずに
    本番 DB を遮断できる. test 内で `init_db()` を呼ぶケースに備え dir は事前作成.
    """
    test_db = tmp_path / "monitor.db"
    test_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("monitor.database.DB_PATH", test_db)
    # W267: 依頼ボード回答の検知イベント (JSONL 追記) も tmp に隔離
    monkeypatch.setattr(
        "monitor.database.BOARD_ANSWER_EVENTS_PATH",
        tmp_path / "board_answer_events.jsonl",
    )


@pytest.fixture(autouse=True)
def _isolate_discord_category_webhooks(monkeypatch):
    """board#22 (2026-06-25): カテゴリ別 Discord webhook env を全テストで隔離.

    `.env` に DISCORD_ORDER/INVENTORY/RESEARCH/PRICING/SYSTEM 等の専用 webhook が
    設定されると、resolve_webhook(category) がそれを返すため、「カテゴリは既定 ch に
    fallback する」前提の既存テストが実 .env を拾って落ちる (test_discord_uses_env_webhook 等).
    全テストでカテゴリ env を削除し resolve_webhook(category)→DISCORD_WEBHOOK_URL へ
    fallback を強制 = 決定的化. 個別 test がカテゴリ webhook を見たい時は再 setenv で上書き可.
    DISCORD_WEBHOOK_URL (既定) は各 test が明示設定するため触らない.
    """
    for _cat_env in (
        "DISCORD_ORDER_WEBHOOK_URL", "DISCORD_INVENTORY_WEBHOOK_URL",
        "DISCORD_RESEARCH_WEBHOOK_URL", "DISCORD_PRICING_WEBHOOK_URL",
        "DISCORD_SYSTEM_WEBHOOK_URL", "DISCORD_RIVAL_WEBHOOK_URL",
        "DISCORD_KEYWORD_WEBHOOK_URL",
    ):
        monkeypatch.delenv(_cat_env, raising=False)


@pytest.fixture(autouse=True)
def _block_news_ai_calls(monkeypatch):
    """W209: score_relevance / deep_dive_article の実 API call を block.

    テスト独立性 + Anthropic budget 保護のため、デフォルトで no-op 化:
    - score_relevance → score=0 / axis='none' (深掘り対象から自然除外)
    - deep_dive_article → None (Q0: silent skip ではなく budget gate と同等の挙動)

    個別 test が API 経路を mock したい時は同 path を再 setattr で上書き可能
    (test 内 monkeypatch.setattr は autouse の patch を override する).
    """
    monkeypatch.setattr(
        "monitor.news_relevance.score_relevance",
        lambda title, summary="", source="": {
            "relevance_score": 0, "axis": "none", "reason_ja": "conftest noop",
        },
        raising=False,
    )
    monkeypatch.setattr(
        "monitor.news_deep_dive.deep_dive_article",
        lambda item, *, budget_remaining_usd: None,
        raising=False,
    )
    # 2026-06-02 fix: X (Grok) も実 API を叩かせない。x_news_sources.json を
    # enabled=true に切り替えると、run_news_check 系 test が fetch_x_entries 経由で
    # 実 xAI を叩き (課金 + flake)、かつ「全 source 失敗」前提の test が崩れる。
    # 最下層 search_x_posts を no-op 化し config.enabled に依らず hermetic に保つ。
    # X 固有挙動を見たい個別 test は search_x_posts を再 setattr で上書き可能。
    monkeypatch.setattr(
        "monitor.xai_wrapper.search_x_posts",
        lambda *args, **kwargs: [],
        raising=False,
    )
    # W223 step1 (2026-06-05): eBay GetItem (Trading API) を block。
    # ebay_listing_image.get_ebay_image_url が cache miss 時に _api_image_urls を
    # 叩く → 実 eBay API HTTP を発火し flake + 課金。最下層を [] 固定で hermetic 化
    # (search_x_posts と同流儀)。画像 fetch 挙動を見たい個別 test は再 setattr で上書き可能。
    monkeypatch.setattr(
        "monitor.ebay_image_fetcher._api_image_urls",
        lambda *args, **kwargs: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _block_real_discord_post(monkeypatch):
    """依頼ボード#39 S2 (2026-07-03): notification_center choke point 経由で全 Discord
    通知が一元化されたため、テストで実 webhook (.env DISCORD_WEBHOOK_URL は本番の実 URL)
    へ実送信されないよう既定で requests.post を no-op 化する (204 相当の偽レスポンス)。
    実送信の中身 (payload 等) を検証したい個別 test は
    `notifiers.notification_center.requests.post` を再 monkeypatch すれば上書き可能。
    """
    class _FakeDiscordResponse:
        status_code = 204

    monkeypatch.setattr(
        "notifiers.notification_center.requests.post",
        lambda *args, **kwargs: _FakeDiscordResponse(),
        raising=False,
    )
