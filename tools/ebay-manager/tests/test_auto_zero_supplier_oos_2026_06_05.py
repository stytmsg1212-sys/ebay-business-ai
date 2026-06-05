#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先OOS → eBay在庫 自動0化 の候補判定テスト (2026-06-05)。

背景: 仕入先(Yahoo等)が売切=「ページなし」になっても eBay在庫が自動0化されず、
履行不能な注文が発生した (item 358343669478 が売れて仕入不可)。
恒久対策 = sync_data_stores で仕入先OOS検知時に eBay在庫を0化。本テストは
その候補判定 `_should_auto_zero` (誤検知で正常listingを0化しないこと) を守る番人。
"""
import pytest

from tasks.task_sync_data_stores import _should_auto_zero


@pytest.mark.parametrize("status,prev_status,expected", [
    # ページなし = ページ消滅 = 確定終了 → 即時0化 (prev 不問)
    ("ページなし", "在庫有", True),
    ("ページなし", None, True),
    ("ページなし", "在庫無", True),
    # 在庫無 = 2回連続 (prev も在庫無) のみ0化 (一時的 scrape 誤検知を除外)
    ("在庫無", "在庫無", True),
    ("在庫無", "在庫有", False),   # 初回在庫無 → まだ0化しない
    ("在庫無", None, False),
    # 正常・不確実は0化しない
    ("在庫有", "在庫無", False),
    ("unknown", "unknown", False),
    ("エラー", "エラー", False),    # fetch失敗 (ページ消滅ではない) → 0化しない
])
def test_should_auto_zero(status, prev_status, expected):
    assert _should_auto_zero(status, prev_status) is expected
