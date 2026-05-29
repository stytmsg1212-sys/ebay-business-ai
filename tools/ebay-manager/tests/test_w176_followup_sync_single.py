"""W176-followup (2026-05-27): sync_single_listing の HIGH 経路回帰テスト。

code-reviewer HIGH-1 (SKU 空 skip) / credentials 欠落 / listing None の 3 経路を
最小コストで縫う。eBay API mock で副作用なし。
"""
from __future__ import annotations

import pytest

from monitor import ebay_sync


def test_sync_single_listing_empty_sku_skips_upsert(monkeypatch):
    """HIGH-1: SKU 空での upsert は monitored_items 整合性を崩すため skip。
    success=False で SKU 空メッセージを返し、upsert_ebay_listing は呼ばれない。"""

    def _fake_get_single(*_a, **_kw):
        return {
            "item_id": "358602711505",
            "title": "Test Title",
            "sku": "",  # 空 = skip 対象
            "quantity": 1,
            "current_price": 10.0,
            "shipping_cost": 0.0,
            "watch_count": 0,
            "view_count": 0,
            "sales_count_30d": 0,
        }

    called = {"upsert": False, "metrics": False}

    def _fake_upsert(*_a, **_kw):
        called["upsert"] = True

    def _fake_metrics(*_a, **_kw):
        called["metrics"] = True

    monkeypatch.setattr(ebay_sync, "get_single_listing", _fake_get_single)
    monkeypatch.setattr(ebay_sync, "upsert_ebay_listing", _fake_upsert)
    monkeypatch.setattr(ebay_sync, "update_ebay_listing_metrics", _fake_metrics)
    monkeypatch.setattr(ebay_sync, "init_db", lambda: None)

    r = ebay_sync.sync_single_listing(
        "358602711505", "app", "dev", "cert", "tok"
    )

    assert r["success"] is False
    assert "SKU が空" in r["message"]
    assert called["upsert"] is False, "SKU 空時に upsert が呼ばれてはいけない"
    assert called["metrics"] is False, "SKU 空時に metrics 更新が呼ばれてはいけない"


def test_sync_single_listing_listing_none_returns_friendly_message(monkeypatch):
    """get_single_listing が None (Not found / API error / parse fail) を返した時、
    success=False で診断可能 message を返す。"""

    monkeypatch.setattr(ebay_sync, "get_single_listing", lambda *_a, **_kw: None)
    monkeypatch.setattr(ebay_sync, "init_db", lambda: None)

    r = ebay_sync.sync_single_listing(
        "999999999999", "app", "dev", "cert", "tok"
    )

    assert r["success"] is False
    assert "GetItem returned no item" in r["message"]
    assert "999999999999" in r["message"]


def test_sync_single_listing_credentials_missing_returns_error(monkeypatch):
    """credentials 未設定なら eBay API を叩かず success=False を返す。"""

    api_called = {"flag": False}

    def _should_not_be_called(*_a, **_kw):
        api_called["flag"] = True
        return None

    monkeypatch.setattr(ebay_sync, "get_single_listing", _should_not_be_called)
    monkeypatch.setattr(ebay_sync, "init_db", lambda: None)

    r = ebay_sync.sync_single_listing(
        "358602711505", "", "", "", ""  # 全て空
    )

    assert r["success"] is False
    assert "credentials not configured" in r["message"]
    assert api_called["flag"] is False, "credentials 欠落時に eBay API が呼ばれてはいけない"


def test_sync_single_listing_empty_item_id_returns_error(monkeypatch):
    """item_id 空文字 / whitespace 入力時の guard 動作確認。"""

    monkeypatch.setattr(ebay_sync, "init_db", lambda: None)

    r = ebay_sync.sync_single_listing(
        "   ", "app", "dev", "cert", "tok"  # whitespace のみ
    )

    assert r["success"] is False
    assert "empty" in r["message"].lower()
