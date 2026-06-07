#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 各国版 (非USD) 除外フィルタの回帰テスト (2026-06-07)。

背景: eBaymag 全国 ON で各国サイト複製 listing が currency=CAD/GBP/EUR/AUD で
GetMyeBaySelling に混入 → ebay_listings に約500件取込 → 定時処理が各国版に走り
二重管理で破壊するリスク。currency!=USD を取り込まない根治の固定テスト。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor import ebay_sync  # noqa: E402


def _listing(item_id, currency, sku="ebayyh_p1"):
    return {
        "item_id": item_id, "sku": sku, "title": "t", "quantity": 1,
        "current_price": 100.0, "currency": currency, "shipping_cost": 0.0,
        "watch_count": 0, "view_count": 0, "sales_count_30d": 0,
        "time_left_seconds": 1000, "start_time": "",
    }


def _patches():
    """sync_listings_from_ebay の外部副作用を全 mock 化する patch 群。"""
    return [
        patch("monitor.ebay_sync.init_db"),
        patch("monitor.ebay_sync.enrich_listings_with_metrics",
              side_effect=lambda l, *a, **k: l),
        patch("monitor.ebay_sync.update_ebay_listing_metrics"),
        patch("monitor.ebay_sync.update_ebay_listing_timing"),
        patch("monitor.ebay_sync.unmark_ebay_listing_ended", return_value=False),
        patch("monitor.ebay_sync.match_source_status_to_ebay", return_value=0),
        patch("monitor.ebay_sync.cleanup_stale_supplier_candidates",
              return_value={"rejected_ended": 0, "rejected_orphan": 0}),
    ]


def _run_with(listings):
    """get_active_listings を listings に差し替えて sync を実行し、upsert された
    ebay_item_id 集合と stats を返す。"""
    patchers = _patches()
    for p in patchers:
        p.start()
    try:
        with patch("monitor.ebay_sync.get_active_listings", return_value=listings), \
             patch("monitor.ebay_sync.upsert_ebay_listing") as up:
            stats = ebay_sync.sync_listings_from_ebay("a", "b", "c", "d")
        upserted = {c.kwargs.get("ebay_item_id") for c in up.call_args_list}
        return upserted, stats
    finally:
        for p in patchers:
            p.stop()


class TestIntlCurrencyFilter:
    def test_non_usd_excluded_usd_kept(self):
        listings = [
            _listing("US1", "USD"),
            _listing("CA1", "CAD"),
            _listing("UK1", "GBP"),
            _listing("DE1", "EUR"),
            _listing("AU1", "AUD"),
        ]
        upserted, stats = _run_with(listings)
        assert "US1" in upserted
        assert {"CA1", "UK1", "DE1", "AU1"}.isdisjoint(upserted), \
            f"各国版が取り込まれた: {upserted}"
        assert stats["intl_skipped"] == 4
        assert stats["synced"] == 1

    def test_empty_and_none_currency_treated_as_usd(self):
        """currency が空/None の listing は US 本体扱いで取り込む (誤除外しない安全側)。"""
        listings = [
            _listing("EMPTY", ""),
            _listing("NONE", None),
            _listing("USD1", "USD"),
        ]
        upserted, stats = _run_with(listings)
        assert upserted == {"EMPTY", "NONE", "USD1"}
        assert stats["intl_skipped"] == 0

    def test_all_usd_nothing_skipped(self):
        listings = [_listing("A", "USD"), _listing("B", "USD")]
        upserted, stats = _run_with(listings)
        assert upserted == {"A", "B"}
        assert stats["intl_skipped"] == 0


class TestSingleListingCurrencyGuard:
    """sync_single_listing (UI 1件同期) も非USD を再混入させないこと (HIGH-3)。"""

    def test_single_non_usd_skipped(self):
        with patch("monitor.ebay_sync.init_db"), \
             patch("monitor.ebay_sync.get_single_listing",
                   return_value=_listing("CA9", "CAD", sku="stock:01")), \
             patch("monitor.ebay_sync.upsert_ebay_listing") as up:
            out = ebay_sync.sync_single_listing("CA9", "a", "b", "c", "d")
        assert out["success"] is False
        assert "CAD" in out["message"]
        up.assert_not_called()

    def test_single_usd_upserted(self):
        with patch("monitor.ebay_sync.init_db"), \
             patch("monitor.ebay_sync.get_single_listing",
                   return_value=_listing("US9", "USD", sku="stock:01")), \
             patch("monitor.ebay_sync.upsert_ebay_listing") as up, \
             patch("monitor.ebay_sync.update_ebay_listing_metrics"):
            out = ebay_sync.sync_single_listing("US9", "a", "b", "c", "d")
        assert out["success"] is True
        up.assert_called_once()
