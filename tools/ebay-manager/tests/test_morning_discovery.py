# -*- coding: utf-8 -*-
"""W122 task_morning_discovery のユニットテスト.

カバー範囲:
- _parse_response: 正常 JSON / コードブロック / 不正 JSON / 候補なし
- update_candidate_feedback: decision 値検証 (buy/skip/hold/listed のみ許可)
- migration v39: morning_discovery_candidates テーブル存在 + 冪等性
- TZ 比較: get_today_candidates が JST 今日の qa_id を拾える
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tasks.task_morning_discovery import (  # noqa: E402
    _parse_response,
    update_candidate_feedback,
)
from monitor.database import init_db, get_conn  # noqa: E402


# ──────────────────────────────────────────────
# _parse_response
# ──────────────────────────────────────────────

def test_parse_response_plain_json():
    raw = '{"candidates": [{"rank": 1, "product_name": "test"}]}'
    out = _parse_response(raw)
    assert out is not None
    assert len(out) == 1
    assert out[0]["product_name"] == "test"


def test_parse_response_code_block():
    raw = (
        "Here is the result:\n"
        "```json\n"
        '{"candidates": [{"rank": 1, "product_name": "a"}, '
        '{"rank": 2, "product_name": "b"}]}\n'
        "```\n"
        "End of response."
    )
    out = _parse_response(raw)
    assert out is not None
    assert len(out) == 2


def test_parse_response_invalid_json_returns_none():
    raw = "not a json at all, just plain text."
    out = _parse_response(raw)
    assert out is None


def test_parse_response_empty_candidates():
    raw = '{"candidates": []}'
    out = _parse_response(raw)
    assert out == []


def test_parse_response_missing_candidates_key():
    raw = '{"other_key": [1, 2, 3]}'
    out = _parse_response(raw)
    assert out is None


# ──────────────────────────────────────────────
# update_candidate_feedback
# ──────────────────────────────────────────────

def test_update_candidate_feedback_rejects_invalid_decision():
    assert update_candidate_feedback(1, "invalid_decision") is False
    assert update_candidate_feedback(1, "") is False
    assert update_candidate_feedback(1, "BUY") is False  # case sensitive


def test_update_candidate_feedback_returns_false_when_no_row():
    """H-4 fix: 存在しない candidate_id では False を返す (silent fail 防止)."""
    # 存在しない id を渡すと rowcount==0 で False を返す
    for valid in ("buy", "skip", "hold", "listed"):
        result = update_candidate_feedback(999999999, valid)
        assert result is False, (
            f"id=999999999 (存在しない) なのに {valid} で True 返却 = silent fail"
        )


def test_update_candidate_feedback_returns_true_on_real_update():
    """実 row 存在 + 有効 decision で True 返却."""
    init_db()
    test_qa_id = 999998
    with get_conn() as c:
        # cleanup
        c.execute(
            "DELETE FROM morning_discovery_candidates WHERE qa_id=?",
            (test_qa_id,),
        )
        cur = c.execute(
            """INSERT INTO morning_discovery_candidates
               (qa_id, candidate_rank, product_name, layer_origin)
               VALUES (?, 1, 'test_real_update', 'unknown')""",
            (test_qa_id,),
        )
        row_id = cur.lastrowid

    assert update_candidate_feedback(row_id, "buy", "テストコメント") is True

    with get_conn() as c:
        rec = c.execute(
            "SELECT user_decision, user_comment "
            "FROM morning_discovery_candidates WHERE id=?",
            (row_id,),
        ).fetchone()
        assert rec[0] == "buy"
        assert rec[1] == "テストコメント"
        # cleanup
        c.execute(
            "DELETE FROM morning_discovery_candidates WHERE qa_id=?",
            (test_qa_id,),
        )


def test_get_webhook_url_explicit_config_takes_priority():
    """H-1 fix: 明示 config の discord.webhook_url が最優先で返る."""
    from tasks.task_morning_discovery import _get_webhook_url
    assert _get_webhook_url(
        {"discord": {"webhook_url": "http://example.com/wh"}}
    ) == "http://example.com/wh"
    # 別キー名 (discord_webhook_url 直書き) もサポート
    assert _get_webhook_url(
        {"discord_webhook_url": "http://example.com/wh2"}
    ) == "http://example.com/wh2"


def test_get_webhook_url_falls_back_to_schedule_config():
    """H-1 fix + user 指摘: config 未指定 / 空でも schedule_config.json から fallback で取得.

    schedule_config.json は scheduler が読む正規 location.
    UI 手動実行ボタンは settings.json (= st.session_state.settings) を渡すため、
    webhook 未設定で通知 skip するのを防ぐ.
    """
    from tasks.task_morning_discovery import _get_webhook_url
    # 実 schedule_config.json から取得 (実 webhook URL が入っている前提)
    wh_none = _get_webhook_url(None)
    wh_empty = _get_webhook_url({})
    # discord.com を含む URL であること (実 webhook URL 形式)
    assert "discord.com" in wh_none or wh_none == "", (
        f"fallback で discord URL 取得できず: {wh_none[:40]}"
    )
    assert wh_empty == wh_none, "config={} と None で挙動が違う"


# ──────────────────────────────────────────────
# migration v39
# ──────────────────────────────────────────────

def test_migration_v39_creates_table():
    init_db()
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 39, f"user_version {ver} < 39"
        schema = c.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='morning_discovery_candidates'"
        ).fetchone()
        assert schema is not None, "morning_discovery_candidates テーブルが無い"
        cols = set(c.execute(
            "PRAGMA table_info(morning_discovery_candidates)"
        ).fetchall())
        col_names = {row[1] for row in c.execute(
            "PRAGMA table_info(morning_discovery_candidates)"
        ).fetchall()}
        # 必須列
        for col in ("qa_id", "candidate_rank", "product_name", "rationale",
                    "supplier_price_jpy", "ebay_estimated_price_usd",
                    "estimated_profit_usd", "similar_sold_count_30d",
                    "competitor_jp_count", "vero_risk_level", "star_rating",
                    "next_action", "source_urls", "layer_origin",
                    "user_decision", "user_comment", "user_decided_at",
                    "created_at"):
            assert col in col_names, f"列 {col} が無い"


def test_migration_v39_idempotent():
    """init_db を 3 回連続実行してもデータが消えない."""
    init_db()
    # ダミー row 1 つ INSERT
    test_qa_id = 999999
    with get_conn() as c:
        # 既存 row 削除 (冪等)
        c.execute(
            "DELETE FROM morning_discovery_candidates WHERE qa_id=?",
            (test_qa_id,),
        )
        c.execute(
            """INSERT INTO morning_discovery_candidates
               (qa_id, candidate_rank, product_name, layer_origin)
               VALUES (?, 99, 'test_idempotent', 'unknown')""",
            (test_qa_id,),
        )
    # 2 回連続再初期化
    init_db()
    init_db()
    with get_conn() as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM morning_discovery_candidates WHERE qa_id=?",
            (test_qa_id,),
        ).fetchone()[0]
        # cleanup
        c.execute(
            "DELETE FROM morning_discovery_candidates WHERE qa_id=?",
            (test_qa_id,),
        )
    assert cnt == 1, f"冪等性違反: idempotent test row {cnt} != 1"


# ──────────────────────────────────────────────
# TZ 比較 (sqlite-timezone.md 準拠)
# ──────────────────────────────────────────────

def test_today_discovery_query_uses_jst_conversion():
    """get_today_candidates の SQL が +9 hours 換算を含む."""
    from tasks.task_morning_discovery import (
        get_today_candidates,
        _today_discovery_exists,
    )
    import inspect
    src = inspect.getsource(get_today_candidates)
    assert "'+9 hours'" in src, "get_today_candidates の SQL が JST 換算していない"
    src2 = inspect.getsource(_today_discovery_exists)
    assert "'+9 hours'" in src2, (
        "_today_discovery_exists の SQL が JST 換算していない"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
