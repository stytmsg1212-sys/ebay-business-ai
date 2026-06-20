#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W284 Phase1: #5(SKU条件)撤廃 の回帰テスト (2026-06-20)

`_select_relist_targets` の WHERE 句から `AND sku IS NOT NULL AND sku != ''` を
削除したことで、SKU 空の listing が relist 候補に選出されるようになったことを検証する。

金銭直結:
- SKU 空 listing (産業機器等 17件) がSEOブーストの機会を得る
- #6 (ebaymag_segment='出さない') は維持されており、eBaymag 各国版との衝突なし
- 識別キー = ebay_item_id (sku-rules.md: SKU は listing 識別に使わない)
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


def _seed_listing(
    item_id: str,
    sku: str = "stock01",
    segment: str = "出さない",
    rank: str = "E",
    watch: int = 0,
    quantity: int = 1,
    is_ended: int = 0,
):
    """relist 候補として挿入。識別キーは ebay_item_id。"""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ebay_listings
               (ebay_item_id, sku, title, rank, watch_count, quantity_ebay,
                is_ended, time_left_seconds, start_time, ebaymag_segment)
               VALUES (?, ?, 'T', ?, ?, ?, ?, 100, '2026-01-01', ?)""",
            (item_id, sku, rank, watch, quantity, is_ended, segment),
        )


def _selected_ids(limit: int = 7, cooldown_days: int = 30) -> set[str]:
    from tasks.task_daily_relist import _select_relist_targets
    return {t["ebay_item_id"] for t in _select_relist_targets(limit=limit, cooldown_days=cooldown_days)}


# ---------------------------------------------------------------------------
# #5 撤廃: SKU 空 listing が選出される
# ---------------------------------------------------------------------------

def test_empty_sku_string_is_selected(tmp_db):
    """sku='' の listing が segment='出さない' で選出される (#5 撤廃確認)。"""
    _seed_listing("ITEM_EMPTY_SKU", sku="")
    assert "ITEM_EMPTY_SKU" in _selected_ids()


def test_empty_sku_whitespace_is_selected(tmp_db):
    """sku に空白のみの値を持つ listing が segment='出さない' で選出される (#5 撤廃確認)。
    DB は sku TEXT NOT NULL のため NULL sku は実在しない。
    旧 WHERE 条件 `sku != ''` はトリムせず比較するため ' ' (スペース) は通過していたが、
    #5 撤廃後は条件自体がなくなるため、あらゆる非 NULL sku (空含む) が対象になる。
    """
    _seed_listing("ITEM_SPACE_SKU", sku=" ")
    assert "ITEM_SPACE_SKU" in _selected_ids()


def test_sku_present_still_selected(tmp_db):
    """SKU 有り listing も引き続き選出される (既存動作の非退行)。"""
    _seed_listing("ITEM_WITH_SKU", sku="stock01")
    assert "ITEM_WITH_SKU" in _selected_ids()


# ---------------------------------------------------------------------------
# #6 維持: ebaymag_segment が '出さない' 以外は選出されない
# ---------------------------------------------------------------------------

def test_segment_zenkok_excluded(tmp_db):
    """segment='全国' は選出されない (#6 維持)。"""
    _seed_listing("ITEM_ZENKOK", sku="", segment="全国")
    assert "ITEM_ZENKOK" not in _selected_ids()


def test_segment_yusen_excluded(tmp_db):
    """segment='優先国' は選出されない (#6 維持)。"""
    _seed_listing("ITEM_YUSEN", sku="stock01", segment="優先国")
    assert "ITEM_YUSEN" not in _selected_ids()


def test_segment_null_excluded(tmp_db):
    """segment=NULL (未分類) は選出されない (安全側除外 = 既存 W242 動作)。"""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ebay_listings
               (ebay_item_id, sku, title, rank, watch_count, quantity_ebay,
                is_ended, time_left_seconds, start_time, ebaymag_segment)
               VALUES (?, '', 'T', 'E', 0, 1, 0, 100, '2026-01-01', NULL)""",
            ("ITEM_NULL_SEG",),
        )
    assert "ITEM_NULL_SEG" not in _selected_ids()


def test_only_dasanai_is_selected_among_all_segments(tmp_db):
    """4 種の segment が混在する場合、'出さない' のみ選出される。"""
    _seed_listing("IT_DASANAI", sku="", segment="出さない")
    _seed_listing("IT_ZENKOK", sku="", segment="全国")
    _seed_listing("IT_YUSEN", sku="stock01", segment="優先国")
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ebay_listings
               (ebay_item_id, sku, title, rank, watch_count, quantity_ebay,
                is_ended, time_left_seconds, start_time, ebaymag_segment)
               VALUES (?, '', 'T', 'E', 0, 1, 0, 100, '2026-01-01', NULL)""",
            ("IT_NULL_SEG",),
        )
    selected = _selected_ids()
    assert "IT_DASANAI" in selected
    assert "IT_ZENKOK" not in selected
    assert "IT_YUSEN" not in selected
    assert "IT_NULL_SEG" not in selected


# ---------------------------------------------------------------------------
# 他の選定条件は不変 (非退行確認)
# ---------------------------------------------------------------------------

def test_watch_nonzero_excluded(tmp_db):
    """watch_count > 0 は SKU 空でも除外 (既存条件不変)。"""
    _seed_listing("ITEM_WATCHED", sku="", watch=1)
    assert "ITEM_WATCHED" not in _selected_ids()


def test_rank_not_e_excluded(tmp_db):
    """rank != 'E' は SKU 空でも除外 (既存条件不変)。"""
    _seed_listing("ITEM_RANK_A", sku="", rank="A")
    assert "ITEM_RANK_A" not in _selected_ids()


def test_is_ended_excluded(tmp_db):
    """is_ended=1 は SKU 空でも除外 (既存条件不変)。"""
    _seed_listing("ITEM_ENDED", sku="", is_ended=1)
    assert "ITEM_ENDED" not in _selected_ids()


def test_quantity_zero_excluded(tmp_db):
    """quantity_ebay=0 は SKU 空でも除外 (既存条件不変)。"""
    _seed_listing("ITEM_NO_QTY", sku="", quantity=0)
    assert "ITEM_NO_QTY" not in _selected_ids()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
