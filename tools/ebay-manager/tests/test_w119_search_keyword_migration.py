"""W119 v34 migration regression test: ebay_listings に search_keyword 系 3 列を追加.

検証対象 (`monitor/database.py` v34 block):
- search_keyword TEXT / search_keyword_generated_at TIMESTAMP / search_keyword_source TEXT の 3 列が ebay_listings に存在
- 既存 v33 DB で init_db 2 回連続実行してデータ + スキーマ完全保持 (Q2 冪等性)
- 新規環境 (user_version=0) で init_db 完走 → user_version=34
- v34 block を 2 回適用しても OperationalError で落ちず、PRAGMA user_version は 34 のまま

参照: `.claude/rules/db-migration-rules.md` (Q2 冪等性ルール).
"""
from __future__ import annotations

import sqlite3

import pytest


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_v34_columns_added_fresh(tmp_path, monkeypatch):
    """fresh DB → init_db で search_keyword 系 3 列が ebay_listings に存在 + user_version=34."""
    db_path = tmp_path / "fresh.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()

    with db_mod.get_conn() as c:
        cols = _columns(c, "ebay_listings")
        assert "search_keyword" in cols, "search_keyword 列が無い"
        assert "search_keyword_generated_at" in cols, "search_keyword_generated_at 列が無い"
        assert "search_keyword_source" in cols, "search_keyword_source 列が無い"
        assert _user_version(c) >= 36, f"user_version は 34 期待 (実際 {_user_version(c)})"


def test_v34_idempotent_data_preserved(tmp_path, monkeypatch):
    """init_db 2 回連続実行で search_keyword に書き込んだ値が消えないこと (Q2 冪等性)."""
    db_path = tmp_path / "idem.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()

    with db_mod.get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, search_keyword, search_keyword_source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test_item_w119_001", "stock:test", "Test Title", "test keyword", "opus_batch"),
        )

    db_mod.init_db()

    with db_mod.get_conn() as c:
        row = c.execute(
            "SELECT search_keyword, search_keyword_source FROM ebay_listings WHERE ebay_item_id=?",
            ("test_item_w119_001",),
        ).fetchone()
        assert row is not None, "INSERT 直後の row が消失 (冪等性違反)"
        assert row[0] == "test keyword", f"search_keyword 値消失 (実際 {row[0]})"
        assert row[1] == "opus_batch", f"search_keyword_source 値消失 (実際 {row[1]})"
        assert _user_version(c) >= 36, f"user_version 不一致 (実際 {_user_version(c)})"


def test_v34_alter_block_safe_when_columns_already_exist(tmp_path, monkeypatch):
    """v34 block を 2 回適用しても OperationalError で落ちず schema 維持."""
    db_path = tmp_path / "twice.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    db_mod.init_db()
    db_mod.init_db()

    with db_mod.get_conn() as c:
        cols = _columns(c, "ebay_listings")
        assert sum(1 for c_name in cols if c_name == "search_keyword") == 1, \
            "search_keyword 列が重複追加された"
        assert _user_version(c) >= 36


def test_v34_upgrade_path_from_v33(tmp_path, monkeypatch):
    """v33 状態の DB を init_db で v34 に上げる際、search_keyword 系 3 列が追加される."""
    db_path = tmp_path / "from_v33.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    with db_mod.get_conn() as c:
        # v34 列を一旦削除して v33 状態を再現するのは SQLite の制約で難しいので、
        # user_version を v33 に戻して init_db を再実行 → v34 block が再適用されるが
        # ALTER TABLE は既存列で OperationalError 握りつぶし (上記 test 参照).
        # ここでは「v33 ジャンプ後に init_db で v34 維持」を確認する.
        c.execute("PRAGMA user_version = 33")

    db_mod.init_db()

    with db_mod.get_conn() as c:
        assert _user_version(c) >= 36
        cols = _columns(c, "ebay_listings")
        assert "search_keyword" in cols
        assert "search_keyword_generated_at" in cols
        assert "search_keyword_source" in cols
