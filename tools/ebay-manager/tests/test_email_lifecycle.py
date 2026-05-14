"""Email life-cycle (reset_confirmed_emails deprecation + prune_old_confirmed_emails) の試験.

2026-04-22: 重複出力バグ修正。`reset_confirmed_emails()` は no-op 化され、
`prune_old_confirmed_emails()` で age ベース削除する仕様になったため、その挙動を検証する。
"""
from __future__ import annotations

import sqlite3
import time
from unittest import mock

import pytest

from monitor import database as db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """クリーンな SQLite を一時作成して本体 DB 代わりに使用する。"""
    tmp_db = tmp_path / 'monitor.db'
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_db))
    # 最小スキーマ (emails テーブルのみ)
    with sqlite3.connect(str(tmp_db)) as con:
        con.execute(
            """CREATE TABLE emails (
                gmail_id TEXT PRIMARY KEY,
                subject TEXT, sender TEXT, date TEXT,
                body_text TEXT, body_ja TEXT, category TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed INTEGER DEFAULT 0,
                summary_ja TEXT, action_ja TEXT, buyer_message_ja TEXT,
                priority_ai TEXT, category_ai TEXT
            )"""
        )
    return tmp_db


class TestResetConfirmedEmailsDeprecated:
    def test_reset_is_now_noop(self, temp_db):
        """重大バグ: DELETE 方式は INSERT OR IGNORE と衝突し重複表示を誘発するため
        reset_confirmed_emails() は no-op 化されている。"""
        with db.get_conn() as c:
            c.execute("INSERT INTO emails (gmail_id, confirmed) VALUES ('g1', 1)")
            c.execute("INSERT INTO emails (gmail_id, confirmed) VALUES ('g2', 0)")

        db.reset_confirmed_emails()

        with db.get_conn() as c:
            rows = c.execute("SELECT gmail_id, confirmed FROM emails ORDER BY gmail_id").fetchall()
        # 何も消えない
        assert len(rows) == 2
        assert rows[0]['gmail_id'] == 'g1'
        assert rows[0]['confirmed'] == 1  # 確認済みのままで残る (重要)
        assert rows[1]['gmail_id'] == 'g2'


class TestPruneOldConfirmedEmails:
    def _insert(self, gmail_id: str, confirmed: int, fetched_at: str):
        """特定の fetched_at で行を投入 (datetime 文字列指定)。"""
        with db.get_conn() as c:
            c.execute(
                "INSERT INTO emails (gmail_id, confirmed, fetched_at) VALUES (?, ?, ?)",
                (gmail_id, confirmed, fetched_at),
            )

    def test_prune_removes_old_confirmed(self, temp_db):
        # 60日前の確認済み
        self._insert('old_conf', 1, "datetime('now', '-60 days')")
        # 今日の確認済み
        self._insert('new_conf', 1, "datetime('now', '-1 days')")
        # 60日前の未確認 (保持対象: prune されない)
        self._insert('old_unconf', 0, "datetime('now', '-60 days')")
        # NOTE: 上の挿入は datetime リテラルが文字列として入るので SQL 側で変換
        with db.get_conn() as c:
            c.execute("UPDATE emails SET fetched_at = datetime('now', '-60 days') WHERE gmail_id IN ('old_conf', 'old_unconf')")
            c.execute("UPDATE emails SET fetched_at = datetime('now', '-1 days') WHERE gmail_id = 'new_conf'")

        deleted = db.prune_old_confirmed_emails(days=30)

        assert deleted == 1  # 60日前の確認済み1件だけ
        with db.get_conn() as c:
            remaining = [r['gmail_id'] for r in c.execute(
                "SELECT gmail_id FROM emails ORDER BY gmail_id"
            ).fetchall()]
        assert 'old_conf' not in remaining
        assert 'new_conf' in remaining        # 新しい確認済みは残る
        assert 'old_unconf' in remaining      # 未確認は age 関係なく残る

    def test_prune_zero_days_is_noop(self, temp_db):
        """days<=0 は no-op (安全ガード)。"""
        self._insert('c1', 1, "now")
        with db.get_conn() as c:
            c.execute("UPDATE emails SET fetched_at = datetime('now', '-90 days') WHERE gmail_id='c1'")
        assert db.prune_old_confirmed_emails(days=0) == 0
        assert db.prune_old_confirmed_emails(days=-5) == 0
        with db.get_conn() as c:
            n = c.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        assert n == 1  # 削除されていない

    def test_prune_empty_db(self, temp_db):
        """空 DB でも例外なく 0 を返す。"""
        assert db.prune_old_confirmed_emails(days=30) == 0

    def test_insert_or_ignore_preserves_confirmed_after_prune_skip(self, temp_db):
        """prune されなかった confirmed=1 レコードは、後続の INSERT OR IGNORE で上書きされない
        (PK conflict で skip される)。これが今回の重複バグ修正の核心挙動。"""
        self._insert('g_keep', 1, "now")
        # INSERT OR IGNORE で同じ gmail_id を未確認として入れようとする (Gmail fetch 相当)
        with db.get_conn() as c:
            c.execute("INSERT OR IGNORE INTO emails (gmail_id, confirmed) VALUES ('g_keep', 0)")
        with db.get_conn() as c:
            row = c.execute(
                "SELECT confirmed FROM emails WHERE gmail_id='g_keep'"
            ).fetchone()
        # PK conflict で skip され、元の confirmed=1 が残っているはず
        assert row['confirmed'] == 1, (
            "INSERT OR IGNORE should preserve existing confirmed=1 row; "
            "this is why reset_confirmed_emails (DELETE) was the duplicate-mail root cause"
        )
