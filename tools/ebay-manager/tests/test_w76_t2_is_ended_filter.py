#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W76 T2: is_ended filter 残り 6 query regression test.

W67 T1 (rival_detection / market_analysis_refresh / tab_market_strategy) 完了後の
T2 (UI consumer 4 関数 + price_optimization 1 query):
- tasks/task_price_optimization.py:47
- monitor/database.py: get_ebay_listings_by_rank (3 query) / get_rank_stats / get_rank_distribution_details

旧: ended (is_ended=1) 行が UI dashboard / rank 統計 / 価格最適化に leak 中 (101 件)
新: 各 query に COALESCE(is_ended, 0) = 0 filter で除外
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """ebay_listings に active + ended 混在 fixtures を入れた temp DB."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE ebay_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT UNIQUE NOT NULL,
            sku TEXT,
            title TEXT,
            current_price REAL DEFAULT 0,
            rank TEXT,
            watch_count INTEGER DEFAULT 0,
            view_count INTEGER DEFAULT 0,
            sales_count_30d INTEGER DEFAULT 0,
            metrics_score REAL DEFAULT 0,
            source_status TEXT,
            competitor_min_price REAL,
            competitor_count INTEGER,
            total_sold_count INTEGER DEFAULT 0,
            last_sold_at TEXT,
            last_synced_at TEXT,
            watch_growth_rate REAL DEFAULT 0,
            view_growth_rate REAL DEFAULT 0,
            sales_growth_rate REAL DEFAULT 0,
            is_ended INTEGER DEFAULT 0,
            quantity_ebay INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = [
        ("E_ACT_1", "stk1", "Active E1", 50.0, "E", 0),
        ("E_ACT_2", "stk2", "Active E2", 60.0, "E", 0),
        ("E_END_1", "stk3", "Ended E1",  70.0, "E", 1),  # ended → 除外対象
        ("E_END_2", "stk4", "Ended E2",  80.0, "E", 1),  # ended → 除外対象
        ("A_ACT_1", "stk5", "Active A1", 100.0, "A", 0),
        ("A_END_1", "stk6", "Ended A1", 200.0, "A", 1),  # ended → 除外対象
        ("S_ACT_1", "stk7", "Active S1", 500.0, "S", 0),
        ("D_ACT_1", "stk8", "Active D1",   0.0, "D", 0),  # price=0 → price_optim 除外
    ]
    for eid, sku, title, price, rank, ie in rows:
        conn.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, rank, is_ended) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, sku, title, price, rank, ie),
        )
    conn.commit()
    conn.close()

    # monitor.database の get_conn を temp DB に向ける
    from monitor import database as db_mod

    def _temp_get_conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db_mod, "get_conn", _temp_get_conn)
    return db_path


def test_get_rank_stats_excludes_ended(tmp_db):
    """get_rank_stats: ended 3 件 (E×2, A×1) が除外される."""
    from monitor.database import get_rank_stats
    stats = get_rank_stats()
    # active のみカウント: E=2, A=1, S=1, D=1, B=0, C=0
    assert stats["E"] == 2, f"E expected 2 active, got {stats['E']}"
    assert stats["A"] == 1, f"A expected 1 active, got {stats['A']}"
    assert stats["S"] == 1
    assert stats["D"] == 1
    assert stats["B"] == 0
    assert stats["C"] == 0


def test_get_rank_distribution_details_excludes_ended(tmp_db):
    """get_rank_distribution_details: ended 行が COUNT/AVG から除外される."""
    from monitor.database import get_rank_distribution_details
    d = get_rank_distribution_details()
    assert d["E"]["count"] == 2, f"E count expected 2, got {d['E']['count']}"
    assert d["A"]["count"] == 1


def test_get_ebay_listings_by_rank_with_rank_excludes_ended(tmp_db):
    """get_ebay_listings_by_rank(rank='E'): active のみ 2 件返却."""
    from monitor.database import get_ebay_listings_by_rank
    listings = get_ebay_listings_by_rank(rank="E")
    eids = {L["ebay_item_id"] for L in listings}
    assert eids == {"E_ACT_1", "E_ACT_2"}, (
        f"expected only active E listings, got {eids}"
    )


def test_get_ebay_listings_by_rank_all_order_by_rank_excludes_ended(tmp_db):
    """get_ebay_listings_by_rank(): order_by_rank=True path で全 active 返却."""
    from monitor.database import get_ebay_listings_by_rank
    listings = get_ebay_listings_by_rank(order_by_rank=True)
    eids = {L["ebay_item_id"] for L in listings}
    expected = {"E_ACT_1", "E_ACT_2", "A_ACT_1", "S_ACT_1", "D_ACT_1"}
    assert eids == expected, f"diff: missing={expected - eids}, extra={eids - expected}"
    assert len(listings) == 5


def test_get_ebay_listings_by_rank_order_by_created_excludes_ended(tmp_db):
    """get_ebay_listings_by_rank(order_by_rank=False): fallback path も除外."""
    from monitor.database import get_ebay_listings_by_rank
    listings = get_ebay_listings_by_rank(order_by_rank=False)
    eids = {L["ebay_item_id"] for L in listings}
    assert "E_END_1" not in eids
    assert "A_END_1" not in eids
    assert len(listings) == 5


def test_price_optimization_via_static_check():
    """task_price_optimization.py 内の SELECT に COALESCE(is_ended, 0) = 0 が含まれる."""
    src = (_PROJECT_ROOT / "tasks" / "task_price_optimization.py").read_text(encoding="utf-8")
    assert "COALESCE(is_ended" in src, (
        "is_ended filter が task_price_optimization.py に含まれていない"
    )


def test_database_module_static_check():
    """database.py の rank 系 4 関数全てに is_ended filter が含まれる."""
    src = (_PROJECT_ROOT / "monitor" / "database.py").read_text(encoding="utf-8")
    # get_ebay_listings_by_rank / get_rank_stats / get_rank_distribution_details の filter
    # (3 関数 = 3 つ以上の COALESCE 出現を期待 — get_ebay_listings_by_rank だけで 3 query)
    coalesce_count = src.count("COALESCE(is_ended, 0) = 0")
    assert coalesce_count >= 5, (
        f"COALESCE(is_ended, 0) = 0 出現数が少ない (期待 5+ get_ebay_listings_by_rank 3 + "
        f"get_rank_stats 1 + get_rank_distribution_details 1), 実際 {coalesce_count}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
