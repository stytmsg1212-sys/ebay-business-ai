#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W227 根治 foundation: 商品状態 Condition を rank(人気度) 列から物理分離した
migration v66 + setter の回帰テスト。

- migration v66 冪等性 (init_db x2 でデータ保持、ebay_condition_id/condition_rank 列)
- update_ebay_listing_condition: 部分更新 / バリデーション / rank(人気度) 非干渉
- rank(人気度) と ebay_condition_id(状態) が独立して共存できること
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_v66_migration_idempotent():
    """init_db 2 回でデータ保持 + ebay_condition_id/condition_rank 列存在 + ver>=66。"""
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended) "
            "VALUES ('W227T','stock','Idem',0)"
        )
        # rank(人気度S) と condition(N/1000) を独立に書く
        c.execute(
            "UPDATE ebay_listings SET ebay_condition_id='1000', "
            "condition_rank='N', rank='S' WHERE ebay_item_id='W227T'"
        )
    init_db()  # 2 回目 (冪等性)
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        row = c.execute(
            "SELECT ebay_condition_id, condition_rank, rank "
            "FROM ebay_listings WHERE ebay_item_id='W227T'"
        ).fetchone()
    assert {"ebay_condition_id", "condition_rank"} <= cols
    assert ver >= 66, ver
    # 人気度rank='S' と 状態(cond_id=1000/rank=N) が独立保持 = データ消失なし
    assert tuple(row) == ("1000", "N", "S"), tuple(row)


def test_update_condition_setter_partial_and_independent():
    """update_ebay_listing_condition: 部分更新可 / 人気度 rank を触らない。"""
    from monitor.database import (
        init_db, get_conn, update_ebay_listing_condition,
    )
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, rank, is_ended) "
            "VALUES ('W227S','stock','T','C',0)"  # 人気度 rank=C
        )
    update_ebay_listing_condition("W227S", ebay_condition_id="3000")
    update_ebay_listing_condition("W227S", condition_rank="B")  # 部分更新 (別呼出)
    with get_conn() as c:
        row = c.execute(
            "SELECT ebay_condition_id, condition_rank, rank "
            "FROM ebay_listings WHERE ebay_item_id='W227S'"
        ).fetchone()
    # 状態(3000/B) を書いても人気度 rank=C は不変 = 完全分離
    assert tuple(row) == ("3000", "B", "C"), tuple(row)


def test_update_condition_setter_validates_rank():
    """condition_rank は 8 段階以外を拒否 (Q0: 不正値を黙って保存しない)。"""
    from monitor.database import init_db, update_ebay_listing_condition
    init_db()
    with pytest.raises(ValueError):
        update_ebay_listing_condition("W227X", condition_rank="X")
    # ebay_condition_id は GetItem 由来の任意値を許容 (書籍 condition 等)
    # → ValueError を投げない (存在しない eid でも UPDATE は 0 行で無害)
    update_ebay_listing_condition("W227X", ebay_condition_id="4000")
