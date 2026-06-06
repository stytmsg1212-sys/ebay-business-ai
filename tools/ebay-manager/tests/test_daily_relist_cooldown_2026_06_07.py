#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_relist cooldown 設定変更 (2026-06-07: 30日ハードコード→config化, 既定値10) の
回帰テスト。

money/account-adjacent: cooldown を短縮すると同一 listing の relist 頻度が上がり、
過剰 relist = Defect 率 / アカウント停止リスクに直結する。`_select_relist_targets` の
cooldown_days パラメータが old/new 両 ItemID に正しく effき、成功 relist のみ cooldown を
発火させ、0/負値が安全側に矯正されることを保証する。

出典: 2026-06-07 code-reviewer HIGH-1 (選定ロジック変更に回帰テスト欠落)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_listing(item_id: str, sku: str = "ebay_x", rank: str = "E", watch: int = 0):
    """relist 対象になり得る active listing を投入 (watch=0 & rank=E & qty>=1 & sku有効)。"""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ebay_listings
               (ebay_item_id, sku, title, rank, watch_count, quantity_ebay,
                is_ended, time_left_seconds, start_time)
               VALUES (?, ?, 'T', ?, ?, 1, 0, 100, '2026-01-01')""",
            (item_id, sku, rank, watch),
        )


def _seed_relist(old_item_id, new_item_id, success, days_ago):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO relist_history (old_item_id, new_item_id, success, created_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (old_item_id, new_item_id, success, f"-{days_ago}"),
        )


def _selected_ids(cooldown_days: int) -> set[str]:
    from tasks.task_daily_relist import _select_relist_targets
    return {t["ebay_item_id"] for t in _select_relist_targets(limit=7, cooldown_days=cooldown_days)}


def test_cooldown_excludes_recent_old_item(tmp_db):
    """5日前に成功relistした old ItemID は cooldown=10 で除外、=3 では選出される。"""
    _seed_listing("OLD1")
    _seed_relist("OLD1", None, 1, "5 days")
    assert "OLD1" not in _selected_ids(10)
    assert "OLD1" in _selected_ids(3)


def test_cooldown_applies_to_new_item_id_too(tmp_db):
    """relist 後の new ItemID も cooldown 対象 (new_item_id subquery)。"""
    _seed_listing("NEW1")
    _seed_relist("OLD0", "NEW1", 1, "2 days")
    assert "NEW1" not in _selected_ids(10)


def test_failed_relist_does_not_trigger_cooldown(tmp_db):
    """success=0 の relist は cooldown を発火させない (FINDING 4)。"""
    _seed_listing("OLD2")
    _seed_relist("OLD2", None, 0, "1 days")
    assert "OLD2" in _selected_ids(10)


def test_cooldown_zero_clamped_to_one_day(tmp_db):
    """cooldown_days=0/負値は max(1, ...) で 1日に矯正される (12h前の relist は除外)。"""
    _seed_listing("OLD3")
    _seed_relist("OLD3", None, 1, "12 hours")
    # 矯正後の最小1日 → 12時間前は cooldown 窓内 = 除外
    assert "OLD3" not in _selected_ids(0)
    assert "OLD3" not in _selected_ids(-5)


def test_longer_cooldown_excludes_more(tmp_db):
    """cooldown を伸ばすほど除外が増える (供給=プール/cooldown の関係を担保)。"""
    _seed_listing("A")
    _seed_listing("B")
    _seed_relist("A", None, 1, "8 days")   # 8日前
    _seed_relist("B", None, 1, "20 days")  # 20日前
    # cooldown=10: A除外(8<10) / B選出(20>10)
    sel10 = _selected_ids(10)
    assert "A" not in sel10 and "B" in sel10
    # cooldown=30: A,B とも除外
    sel30 = _selected_ids(30)
    assert "A" not in sel30 and "B" not in sel30


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
