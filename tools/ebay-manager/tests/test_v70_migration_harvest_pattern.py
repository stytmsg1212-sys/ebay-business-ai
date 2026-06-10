#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migration v70: research_candidates.harvest_pattern 列追加の冪等性テスト.

Q2 db-migration-rules 準拠:
  - fresh DB → init_db で harvest_pattern 列が research_candidates に存在 + user_version=70
  - init_db を 2 回連続実行してもデータ保持 (冪等性必須テスト)
  - ALTER TABLE 重複でも OperationalError で落ちない (try/except 冪等)
  - harvest_pattern は NULL 許容 (手動入力は None、W229 ハーベストのみ非 None)

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §3-2 / Q10
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# fixture: 毎テストで独立した tmp DB を使う (本番 DB を汚染しない)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """tests/ 専用の tmp DB に monkeypatch して init_db."""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# (1) harvest_pattern 列の実在確認 + user_version >= 70
# ---------------------------------------------------------------------------

def test_v70_harvest_pattern_column_exists(tmp_db, monkeypatch):
    """init_db 後に harvest_pattern 列が research_candidates に存在し
    user_version が 70 以上になること."""
    import monitor.database as db_mod
    from monitor.database import get_conn

    with get_conn() as c:
        cols = _cols(c, "research_candidates")
        ver = c.execute("PRAGMA user_version").fetchone()[0]

    assert "harvest_pattern" in cols, (
        f"harvest_pattern 列が research_candidates に存在しない。cols={cols}"
    )
    assert ver >= 70, f"user_version={ver} (期待 >= 70)"


# ---------------------------------------------------------------------------
# (2) 冪等性: init_db x2 でデータ保持
# ---------------------------------------------------------------------------

def test_v70_idempotent_data_preserved(tmp_db, monkeypatch):
    """init_db を 2 回連続実行しても research_candidates のデータが消えない.

    Q2 db-migration-rules: init_db() 2 回連続でデータ保持を verify する自動テスト必須。
    """
    import monitor.database as db_mod
    from monitor.database import get_conn, init_db

    # init_db 済 (fixture) の状態でデータを挿入
    with get_conn() as c:
        c.execute(
            "INSERT INTO research_candidates "
            "(title_ja, manual_weight_g, terapeak_avg_price_usd, "
            " harvest_pattern, status) "
            "VALUES ('v70-冪等性テスト品', 300.0, 120.0, 'fresh_24h', 'new')"
        )

    init_db()  # 2 回目 — DROP/DELETE が走ったら 0 件になる = 冪等性違反

    with get_conn() as c:
        rows = c.execute(
            "SELECT title_ja, manual_weight_g, terapeak_avg_price_usd, "
            "harvest_pattern, status "
            "FROM research_candidates WHERE title_ja='v70-冪等性テスト品'"
        ).fetchall()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = _cols(c, "research_candidates")

    assert len(rows) == 1, f"冪等性違反: init_db 2 回でデータ消失 (rows={rows})"
    row = tuple(rows[0])
    assert row == ("v70-冪等性テスト品", 300.0, 120.0, "fresh_24h", "new"), (
        f"データ値が変化: {row}"
    )
    assert "harvest_pattern" in cols, "2 回目 init_db 後に harvest_pattern 列が消えた"
    assert ver >= 70, f"2 回目 init_db 後に user_version が drift: {ver}"


# ---------------------------------------------------------------------------
# (3) ALTER 重複でも落ちない (冪等 try/except 確認)
# ---------------------------------------------------------------------------

def test_v70_alter_idempotent_no_crash_on_repeat(tmp_path, monkeypatch):
    """user_version を 69 に戻して再 init → ALTER 重複でも OperationalError で落ちない."""
    import sqlite3
    import monitor.database as db_mod

    db_path = tmp_path / "v69.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()  # -> v70 まで到達

    # v70 block を強制再実行させるため version を 69 に戻す
    with db_mod.get_conn() as c:
        c.execute("PRAGMA user_version = 69")

    db_mod.init_db()  # v70 block 再突入 (ALTER 重複) → 落ちないこと

    with sqlite3.connect(db_path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = _cols(c, "research_candidates")

    assert ver >= 70, f"v70 block 再突入後に user_version が更新されない: {ver}"
    assert "harvest_pattern" in cols, "ALTER 重複後に harvest_pattern 列が消えた"


# ---------------------------------------------------------------------------
# (4) insert_research_candidate に harvest_pattern kwarg が通る
# ---------------------------------------------------------------------------

def test_insert_research_candidate_with_harvest_pattern(tmp_db, monkeypatch):
    """insert_research_candidate(harvest_pattern=...) が DB に書き込まれること.

    既存呼出 (harvest_pattern 省略) は None = NULL となり後方互換を保つ。
    """
    import monitor.database as db_mod
    from monitor.database import get_conn, init_db
    from monitor import research_candidates_db as rc_db

    # harvest_pattern 付き
    rc_id_fresh = rc_db.insert_research_candidate(
        "Sony WH-1000XM5",
        manual_weight_g=250.0,
        terapeak_avg_price_usd=200.0,
        harvest_pattern="fresh_24h",
    )
    # harvest_pattern なし (後方互換)
    rc_id_manual = rc_db.insert_research_candidate(
        "Audio-Technica ATH-M50x",
        manual_weight_g=285.0,
    )

    with get_conn() as c:
        row_fresh = c.execute(
            "SELECT harvest_pattern FROM research_candidates WHERE rc_id=?",
            (rc_id_fresh,),
        ).fetchone()
        row_manual = c.execute(
            "SELECT harvest_pattern FROM research_candidates WHERE rc_id=?",
            (rc_id_manual,),
        ).fetchone()

    assert row_fresh is not None, "fresh_24h 行が取得できない"
    assert row_fresh[0] == "fresh_24h", f"harvest_pattern 値が不一致: {row_fresh[0]}"

    assert row_manual is not None, "manual 行が取得できない"
    assert row_manual[0] is None, (
        f"省略時は NULL 期待だが {row_manual[0]!r} が書き込まれた (後方互換違反)"
    )


# ---------------------------------------------------------------------------
# (5) two_year_echo パターン + NULL どちらも DB に保存できる
# ---------------------------------------------------------------------------

def test_harvest_pattern_values(tmp_db, monkeypatch):
    """'fresh_24h' / 'two_year_echo' / None の 3 値が正しく round-trip する."""
    from monitor.database import get_conn
    from monitor import research_candidates_db as rc_db

    ids = {}
    for pattern in ("fresh_24h", "two_year_echo", None):
        label = f"テスト品_{pattern}"
        ids[pattern] = rc_db.insert_research_candidate(
            label,
            harvest_pattern=pattern,
        )

    with get_conn() as c:
        for pattern, rc_id in ids.items():
            row = c.execute(
                "SELECT harvest_pattern FROM research_candidates WHERE rc_id=?",
                (rc_id,),
            ).fetchone()
            assert row is not None, f"rc_id={rc_id} が見つからない"
            assert row[0] == pattern, (
                f"harvest_pattern round-trip 失敗: 書込={pattern!r}, 読出={row[0]!r}"
            )


# ---------------------------------------------------------------------------
# (6) _get_existing_gate_decisions の実 SQL が実スキーマで通る (回帰)
# ---------------------------------------------------------------------------

def test_get_existing_gate_decisions_real_schema(tmp_db, monkeypatch):
    """dedup lookup SQL が init_db 実スキーマに対して OperationalError なく走る.

    出典 2026-06-10 Q1 実機検証: 本体テスト 14 箇所全てが本関数を patch していたため
    SELECT 句の幻列 (listing_start_date — どの migration にも存在しない) が
    pytest 137 PASS を素通りし、実機で即クラッシュした。実スキーマ SQL smoke を固定化。
    """
    from monitor.database import get_conn
    from monitor import research_candidates_db as rc_db
    from tasks.task_research_harvest import _get_existing_gate_decisions

    rc_id = rc_db.insert_research_candidate(
        "Sony WH-1000XM5", harvest_pattern="fresh_24h"
    )
    with get_conn() as c:
        c.execute(
            "UPDATE research_candidates "
            "SET source='terapeak_harvest', harvest_keyword='sony wh-1000xm5' "
            "WHERE rc_id=?",
            (rc_id,),
        )

    result = _get_existing_gate_decisions(["sony wh-1000xm5", "missing keyword"])

    assert "sony wh-1000xm5" in result, f"既存行が lookup に出ない: {result}"
    row = result["sony wh-1000xm5"]
    assert row["rc_id"] == rc_id
    assert row["gate_decision"] is None  # needs_review 残骸 (NULL) も返す仕様
    assert "status" in row
    assert "missing keyword" not in result
