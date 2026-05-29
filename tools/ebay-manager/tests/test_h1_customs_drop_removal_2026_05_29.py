"""H1 回帰テスト (2026-05-29 Opus 4.8 総チェック): init_db 内 customs_requests の
条件付き DROP TABLE 撤廃が安全であることを保証する.

背景:
- 旧 `database.py` init_db は customs_requests のスキーマ文字列に "deadline LIKE" を
  含むかの脆弱判定で、含まなければ customs_send_audit / customs_kb_pending /
  customs_requests を **DROP** していた (v18 95 件消失クラスの再発リスク).
- Q2 規定 (init_db 内 DROP TABLE 禁止) 違反のため撤廃.

不変条件 (本テストが守る):
1. fresh DB → init_db 2 回 → customs 3 テーブルが LIKE 制約で存在 + データ保持 (冪等性).
2. 旧 GLOB 制約 + v19 前 status set (no 'drafted_in_gmail') + データ行を持つレガシー DB
   → init_db → DROP されず、後続 v19 のデータ保持 RENAME+INSERT SELECT が
   LIKE 制約へ自動再構築し、データ行が保全される (DROP 撤廃の安全性の核心).
"""
from __future__ import annotations

import sqlite3

import pytest


def _customs_schema_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='customs_requests'"
    ).fetchone()
    return row[0] if row and row[0] else ""


def test_fresh_db_idempotent_no_drop(tmp_path, monkeypatch):
    """fresh DB → init_db 2 回連続 → customs 3 テーブル健在 + データ保持 + LIKE 制約."""
    db_path = tmp_path / "fresh.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    with db_mod.get_conn() as c:
        c.execute(
            "INSERT INTO customs_requests (gmail_id, carrier, status) "
            "VALUES (?, ?, ?)",
            ("gmail_fresh_1", "fedex", "detected"),
        )
        req_id = c.execute(
            "SELECT id FROM customs_requests WHERE gmail_id='gmail_fresh_1'"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO customs_send_audit "
            "(customs_request_id, recipients_hash, body_hash, result) "
            "VALUES (?, ?, ?, ?)",
            (req_id, "rh", "bh", "success"),
        )
        c.execute(
            "INSERT INTO customs_kb_pending (kind, brand_or_category, proposed_json) "
            "VALUES (?, ?, ?)",
            ("manufacturer", "TestBrand", "{}"),
        )

    db_mod.init_db()  # 2 回目: DROP されないはず
    with db_mod.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM customs_requests"
        ).fetchone()[0] == 1, "customs_requests データ消失 (DROP 再発)"
        assert c.execute(
            "SELECT COUNT(*) FROM customs_send_audit"
        ).fetchone()[0] == 1, "customs_send_audit データ消失 (DROP 再発)"
        assert c.execute(
            "SELECT COUNT(*) FROM customs_kb_pending"
        ).fetchone()[0] == 1, "customs_kb_pending データ消失 (DROP 再発)"
        schema = _customs_schema_sql(c)
        assert "deadline LIKE" in schema, "LIKE 制約が失われた"
        assert "GLOB" not in schema, "GLOB バグが残存"


def test_legacy_glob_db_migrates_without_drop(tmp_path, monkeypatch):
    """旧 GLOB 制約 + v19 前 status の DB に init_db → DROP せず v19 でデータ保持再構築."""
    db_path = tmp_path / "legacy_glob.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    # v18 当時のバグ版スキーマを手で再現 (deadline GLOB, status に drafted_in_gmail なし)
    with sqlite3.connect(db_path) as c:
        c.execute("""
            CREATE TABLE customs_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id TEXT NOT NULL UNIQUE,
                gmail_sent_id TEXT,
                carrier TEXT NOT NULL CHECK(carrier IN ('fedex','dhl','ups')),
                tracking_number TEXT,
                recipient TEXT,
                ship_date TEXT,
                deadline TEXT CHECK(
                    deadline IS NULL OR deadline GLOB '____-__-__'
                ),
                request_items TEXT,
                ebay_item_id TEXT,
                sku TEXT,
                product_title TEXT,
                draft_subject TEXT,
                draft_body TEXT,
                draft_recipients TEXT,
                attached_photos TEXT,
                attached_attachments TEXT,
                template_used TEXT,
                template_hash TEXT,
                kb_hits TEXT,
                status TEXT NOT NULL
                    CHECK(status IN (
                        'detected','drafted','drafted_no_photo',
                        'sending','sent','failed','manual'
                    ))
                    DEFAULT 'detected',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                drafted_at TIMESTAMP,
                sent_at TIMESTAMP,
                error_msg TEXT
            )
        """)
        # deadline は NULL (GLOB '_' リテラル扱いで実日付は挿入不可 = まさに v18 バグ)
        c.execute(
            "INSERT INTO customs_requests "
            "(gmail_id, carrier, product_title, status, deadline) "
            "VALUES (?, ?, ?, ?, NULL)",
            ("gmail_legacy_1", "dhl", "Legacy Item", "detected"),
        )
        c.execute("PRAGMA user_version = 18")

    # 旧 GLOB スキーマである前提を確認
    with sqlite3.connect(db_path) as c:
        before = _customs_schema_sql(c)
        assert "GLOB" in before
        assert "'drafted_in_gmail'" not in before

    db_mod.init_db()

    with db_mod.get_conn() as c:
        # データ行が DROP されず保全されている (核心)
        rows = c.execute(
            "SELECT gmail_id, product_title FROM customs_requests"
        ).fetchall()
        assert len(rows) == 1, "レガシー DB のデータが消失 (DROP 撤廃の安全性違反)"
        assert rows[0][0] == "gmail_legacy_1"
        assert rows[0][1] == "Legacy Item"
        # v19 のデータ保持再構築で GLOB→LIKE 修正 + drafted_in_gmail 追加
        after = _customs_schema_sql(c)
        assert "deadline LIKE" in after, "GLOB→LIKE 修正が行われていない"
        assert "GLOB" not in after, "GLOB バグが残存"
        assert "'drafted_in_gmail'" in after, "v19 status migration が走っていない"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
