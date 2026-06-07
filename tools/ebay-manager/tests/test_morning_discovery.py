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
    _build_discovery_query,
    _fetch_recent_sold,
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


def test_update_candidate_feedback_returns_true_on_real_update(monkeypatch):
    """実 row 存在 + 有効 decision で True 返却."""
    init_db()
    # W187: update_candidate_feedback は tasks.task_morning_discovery 独自の
    # module 定数 DB_PATH (実 data/monitor.db) を raw sqlite3.connect する。
    # conftest 隔離は monitor.database.DB_PATH のみ差し替えるため、seed (get_conn
    # = tmp) と update (実 DB) が別 DB になり rowcount=0 で fail していた。
    # 同一 tmp DB に揃える (実 DB 汚染防止 + 決定的化)。
    import monitor.database as _db
    import tasks.task_morning_discovery as _tmd
    monkeypatch.setattr(_tmd, "DB_PATH", _db.DB_PATH)
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


# ──────────────────────────────────────────────
# W129 (2026-05-15): profit=0 = 見積不能シグナル表示
# ──────────────────────────────────────────────


def test_discord_profit_zero_shows_unestimable_not_dollar_zero():
    """W129: profit=0 (prompt が見積不能シグナルとして返す値) を Discord で『$0』表示しない.

    prompt L181-184 で「estimated_profit_usd は null 不可、見積不能なら 0 + 理由」
    と指示している. UI で『想定粗利 $0』と表示すると赤字判定と誤読され、
    user の skip 履歴に流れて Few-shot 学習を歪めるため、明示的に
    「見積不能 (理由は根拠欄)」と表示すること.
    """
    import inspect
    from tasks.task_morning_discovery import _send_discord
    src = inspect.getsource(_send_discord)
    # profit == 0 分岐の存在を確認
    assert "profit == 0" in src or "profit==0" in src, (
        "_send_discord に profit==0 の特別 handling がない"
    )
    assert "見積不能" in src, (
        "_send_discord に『見積不能』表示文言がない"
    )


def test_streamlit_profit_zero_shows_unestimable_not_dollar_zero():
    """W129 (Streamlit 側): tab_morning_discovery.py が profit=0 を『$0』表示しない."""
    import inspect
    # tab module を import
    sys.path.insert(0, str(PROJECT_ROOT))
    from tabs import tab_morning_discovery
    # render 系 function を全部 source 取得して文字列検索
    members = inspect.getmembers(tab_morning_discovery, inspect.isfunction)
    all_src = "\n".join(inspect.getsource(fn) for _, fn in members)
    assert "profit_usd == 0" in all_src, (
        "tab_morning_discovery に profit_usd==0 の特別 handling がない"
    )
    assert "見積不能" in all_src, (
        "tab_morning_discovery に『見積不能』表示文言がない"
    )


# ──────────────────────────────────────────────
# W122 階層1 sold 実績注入 (#1 自社sold縦深掘り)
# ──────────────────────────────────────────────

_DUMMY_TOP_SELLERS = [
    {
        "title": "Sony WH-1000XM5",
        "current_price": 280,
        "watch_count": 12,
        "sales_count_30d": 5,
        "rank": "A",
        "sku": "stock:01",
    }
]

_DUMMY_USER_DECISIONS: list[dict] = []
_DUMMY_JP_SELLERS = ["seller_a", "seller_b"]


def test_build_discovery_query_contains_sold_section_with_data():
    """recent_sold データあり: query に実 sold 実績セクションが含まれる."""
    recent_sold = [
        {
            "title": "Sony WH-1000XM4 Noise Canceling Headphones",
            "sold_count": 3,
            "avg_price_usd": 265.0,
            "last_sold_at": "2026-05-10T14:00:00",
        },
        {
            "title": "Sony LinkBuds S Wireless",
            "sold_count": 1,
            "avg_price_usd": 120.0,
            "last_sold_at": "2026-04-20T09:00:00",
        },
    ]
    query = _build_discovery_query(
        _DUMMY_TOP_SELLERS,
        _DUMMY_USER_DECISIONS,
        _DUMMY_JP_SELLERS,
        is_monday=False,
        recent_sold=recent_sold,
    )
    # セクションヘッダが存在する
    assert "自社実 sold 実績" in query
    assert "sales_history DB 由来" in query
    # 実 sold 商品タイトルが含まれる
    assert "Sony WH-1000XM4" in query
    assert "Sony LinkBuds S" in query
    # 件数・価格も含まれる
    assert "3件" in query
    assert "$265" in query
    # 階層1 への水平展開指示が含まれる
    assert "horizontal_pattern" in query
    assert "推定ではなく" in query


def test_build_discovery_query_sold_empty_uses_fallback_message():
    """recent_sold が空リスト: フォールバックメッセージが含まれ、query が壊れない."""
    query = _build_discovery_query(
        _DUMMY_TOP_SELLERS,
        _DUMMY_USER_DECISIONS,
        _DUMMY_JP_SELLERS,
        is_monday=False,
        recent_sold=[],
    )
    assert "自社実 sold 実績" in query
    # フォールバック文言
    assert "実 sold 実績なし" in query
    # query 全体が壊れていない (必須セクションが残る)
    assert "自社売れ筋 TOP" in query
    assert "出力フォーマット" in query


def test_build_discovery_query_sold_none_uses_fallback_message():
    """recent_sold=None (デフォルト): フォールバックメッセージが含まれ、query が壊れない."""
    query = _build_discovery_query(
        _DUMMY_TOP_SELLERS,
        _DUMMY_USER_DECISIONS,
        _DUMMY_JP_SELLERS,
        is_monday=False,
        recent_sold=None,
    )
    assert "自社実 sold 実績" in query
    assert "実 sold 実績なし" in query
    assert "出力フォーマット" in query


def test_fetch_recent_sold_returns_list_on_empty_db(monkeypatch):
    """sales_history が空 (tmp DB) でも例外を投げず空リストを返す (Q0 silent skip 防止)."""
    import monitor.database as _db
    import tasks.task_morning_discovery as _tmd
    monkeypatch.setattr(_tmd, "DB_PATH", _db.DB_PATH)
    init_db()
    result = _fetch_recent_sold(days=90, limit=30)
    assert isinstance(result, list)
    # テスト用 tmp DB は sales_history が空なので 0 件
    assert len(result) == 0


def test_fetch_recent_sold_returns_sold_rows(monkeypatch):
    """sales_history にデータがある場合、sold 実績が返る."""
    import monitor.database as _db
    import tasks.task_morning_discovery as _tmd
    monkeypatch.setattr(_tmd, "DB_PATH", _db.DB_PATH)
    init_db()
    # テストデータを挿入
    with get_conn() as c:
        c.execute(
            """INSERT INTO sales_history
               (ebay_item_id, title, sold_price_usd, sold_at)
               VALUES
               ('item001', 'Sony WH-1000XM5 Headphones', 285.0, datetime('now', '-10 days')),
               ('item002', 'Sony WH-1000XM5 Headphones', 290.0, datetime('now', '-5 days')),
               ('item003', 'Canon PowerShot G7X Mark III', 620.0, datetime('now', '-20 days'))"""
        )
    result = _fetch_recent_sold(days=90, limit=30)
    assert isinstance(result, list)
    assert len(result) == 2  # title で GROUP BY するので 2 行
    titles = [r["title"] for r in result]
    assert "Sony WH-1000XM5 Headphones" in titles
    assert "Canon PowerShot G7X Mark III" in titles
    # sold_count が多い順に並んでいる (Sony=2件 > Canon=1件)
    assert result[0]["title"] == "Sony WH-1000XM5 Headphones"
    assert result[0]["sold_count"] == 2


def test_fetch_recent_sold_excludes_old_records(monkeypatch):
    """days パラメータより古い sold は除外される."""
    import monitor.database as _db
    import tasks.task_morning_discovery as _tmd
    monkeypatch.setattr(_tmd, "DB_PATH", _db.DB_PATH)
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO sales_history
               (ebay_item_id, title, sold_price_usd, sold_at)
               VALUES
               ('item_recent', 'Recent Item', 100.0, datetime('now', '-10 days')),
               ('item_old', 'Old Item', 100.0, datetime('now', '-200 days'))"""
        )
    result = _fetch_recent_sold(days=90, limit=30)
    titles = [r["title"] for r in result]
    assert "Recent Item" in titles
    assert "Old Item" not in titles


def test_fetch_recent_sold_db_error_returns_empty_list(monkeypatch):
    """sales_history 取得が例外になっても空リストが返り、例外が上位に伝播しない (Q0)."""
    import tasks.task_morning_discovery as _tmd

    def _raise(*args: object, **kwargs: object) -> list:
        raise RuntimeError("DB 接続エラー (テスト用)")

    monkeypatch.setattr(
        "monitor.database.get_recent_sold_for_discovery",
        _raise,
        raising=False,
    )
    result = _fetch_recent_sold(days=90, limit=30)
    assert result == []


def test_get_recent_sold_for_discovery_read_only_no_schema_change():
    """get_recent_sold_for_discovery は SELECT のみで既存スキーマを変更しない."""
    init_db()
    from monitor.database import get_recent_sold_for_discovery, get_conn
    with get_conn() as c:
        ver_before = c.execute("PRAGMA user_version").fetchone()[0]
    get_recent_sold_for_discovery(days=90, limit=30)
    with get_conn() as c:
        ver_after = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver_before == ver_after, (
        f"get_recent_sold_for_discovery が user_version を変更した: "
        f"{ver_before} -> {ver_after}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
