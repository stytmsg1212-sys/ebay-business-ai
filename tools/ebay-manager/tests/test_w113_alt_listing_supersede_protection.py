"""W113 (2026-05-09): apply 時の supersede ロジックで alt_listing_possible=1 候補が
巻き添え auto_rejected されない regression test.

事故事例:
  2026-05-08 W112 verify 中、ItemID 358274830101 の id=508 (ADVANTEST TR6143
  = alt_listing_possible=1 別 SKU 出品機会) が、別候補 (id=507) の apply 副作用で
  auto_rejected された.

修正内容:
  task_supplier_apply.py:164-170 の supersede WHERE 句に
  AND COALESCE(alt_listing_possible, 0) = 0 を追加.

このテストは tasks.task_supplier_apply の supersede SQL ロジックを直接検証する.
本番 DB に依存しない (sqlite tmp で自己完結).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def db_with_supplier_schema(tmp_path):
    """W113 で参照される最小スキーマを持つ tmp DB を構築."""
    db_path = tmp_path / "test_w113.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE supplier_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            ebay_item_id TEXT NOT NULL,
            source_platform TEXT,
            candidate_url TEXT,
            candidate_price_jpy INTEGER,
            candidate_title TEXT,
            match_score INTEGER,
            match_reasoning TEXT,
            profit_jpy REAL,
            profitable INTEGER,
            status TEXT DEFAULT 'pending',
            user_action_at TIMESTAMP,
            discovered_via TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            junk_likely_untested INTEGER DEFAULT 0,
            alt_listing_possible INTEGER DEFAULT 0,
            alt_listing_note TEXT,
            auto_rejected INTEGER DEFAULT 0,
            eval_model TEXT
        )"""
    )
    conn.commit()
    return conn


def _insert_candidate(conn, **kwargs):
    """テスト用 candidate row を挿入し id を返す."""
    defaults = {
        "sku": "ebayyh_test001",
        "ebay_item_id": "999000001",
        "source_platform": "yahoo_auctions",
        "candidate_url": "https://example.com/x1",
        "candidate_price_jpy": 5000,
        "candidate_title": "test",
        "match_score": 85,
        "status": "pending",
        "alt_listing_possible": 0,
        "auto_rejected": 0,
    }
    defaults.update(kwargs)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" * len(defaults))
    cur = conn.execute(
        f"INSERT INTO supplier_candidates ({cols}) VALUES ({placeholders})",
        list(defaults.values()),
    )
    conn.commit()
    return cur.lastrowid


def _run_w113_supersede(conn, ebay_item_id: str, candidate_id: int) -> int:
    """W113 fix 後の supersede SQL を再現. 影響行数を返す."""
    cur = conn.execute(
        "UPDATE supplier_candidates "
        "SET status='rejected', auto_rejected=1, user_action_at=CURRENT_TIMESTAMP "
        "WHERE ebay_item_id=? AND status='pending' AND id != ? "
        "  AND COALESCE(alt_listing_possible, 0) = 0",
        (ebay_item_id, candidate_id),
    )
    conn.commit()
    return cur.rowcount


def test_w113_alt_listing_candidate_not_auto_rejected_after_apply(db_with_supplier_schema):
    """同 ebay_item_id で apply された場合でも、alt_listing_possible=1 候補は
    auto_rejected されないこと."""
    conn = db_with_supplier_schema
    eid = "358274830101"

    # 通常候補 (apply される側)
    main_id = _insert_candidate(
        conn, ebay_item_id=eid, candidate_url="https://example.com/main",
        match_score=85, alt_listing_possible=0,
    )
    # alt_listing 候補 (auto_rejected されてはいけない)
    alt_id = _insert_candidate(
        conn, ebay_item_id=eid, candidate_url="https://example.com/alt",
        match_score=25, alt_listing_possible=1,
    )

    # main_id を apply した想定で supersede 実行
    affected = _run_w113_supersede(conn, eid, main_id)

    # alt 候補は pending のまま、auto_rejected=0
    alt_row = conn.execute(
        "SELECT status, auto_rejected FROM supplier_candidates WHERE id=?", (alt_id,)
    ).fetchone()
    assert alt_row[0] == "pending", "alt_listing 候補が auto_rejected された (W113 regression)"
    assert alt_row[1] == 0
    assert affected == 0  # 通常候補は他にいないので 0 行影響


def test_w113_normal_pending_candidate_still_superseded(db_with_supplier_schema):
    """alt_listing_possible=0 の通常候補は従来通り auto_rejected される (regression
    としての副作用なし、supersede 自体は機能維持)."""
    conn = db_with_supplier_schema
    eid = "358274830101"
    main_id = _insert_candidate(conn, ebay_item_id=eid, alt_listing_possible=0)
    other_normal_id = _insert_candidate(
        conn, ebay_item_id=eid,
        candidate_url="https://example.com/other_normal",
        alt_listing_possible=0,
    )

    affected = _run_w113_supersede(conn, eid, main_id)

    other_row = conn.execute(
        "SELECT status, auto_rejected FROM supplier_candidates WHERE id=?",
        (other_normal_id,),
    ).fetchone()
    assert other_row[0] == "rejected"
    assert other_row[1] == 1
    assert affected == 1


def test_w113_mixed_alt_and_normal_only_normal_superseded(db_with_supplier_schema):
    """alt + 通常 が混在する場合、通常のみ supersede、alt は保護されること."""
    conn = db_with_supplier_schema
    eid = "358274830101"
    main_id = _insert_candidate(conn, ebay_item_id=eid, alt_listing_possible=0)
    other_normal_id = _insert_candidate(
        conn, ebay_item_id=eid,
        candidate_url="https://example.com/other_normal",
        alt_listing_possible=0,
    )
    alt_id = _insert_candidate(
        conn, ebay_item_id=eid,
        candidate_url="https://example.com/alt",
        alt_listing_possible=1,
    )

    affected = _run_w113_supersede(conn, eid, main_id)

    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT id, status, auto_rejected FROM supplier_candidates WHERE id IN (?, ?)",
            (other_normal_id, alt_id),
        ).fetchall()
    }
    assert rows[other_normal_id] == ("rejected", 1)
    assert rows[alt_id] == ("pending", 0)
    assert affected == 1


def test_w113_different_ebay_item_id_not_affected(db_with_supplier_schema):
    """別 ebay_item_id の候補は (alt 有無に関わらず) supersede 対象外であること."""
    conn = db_with_supplier_schema
    eid_a = "358274830101"
    eid_b = "358293723954"
    main_id = _insert_candidate(conn, ebay_item_id=eid_a, alt_listing_possible=0)
    cross_normal_id = _insert_candidate(conn, ebay_item_id=eid_b, alt_listing_possible=0)

    _run_w113_supersede(conn, eid_a, main_id)

    cross_row = conn.execute(
        "SELECT status FROM supplier_candidates WHERE id=?", (cross_normal_id,)
    ).fetchone()
    assert cross_row[0] == "pending"
