#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W223 step1 (2026-06-05): eBay 商品画像 cache (ebay_listing_image) の単体テスト.

検証対象:
  - migration v63 で ebay_listings に ebay_image_url / ebay_image_fetched_at 列が増える
  - get_ebay_image_url: cache hit (GetItem を叩かない) / miss → GetItem → DB 保存
  - 鮮度窓 (30 日) 超過 cache は再取得
  - GetItem 空 → None (fail-open)
  - init_db 2 回でデータ保持 (Q2 冪等性)

DB は conftest の autouse fixture で tmp_path に隔離済。各 test 冒頭で init_db()。
conftest が `_api_image_urls` を [] 固定 (network block) するため、cache miss 時に
取得させたい test は monkeypatch で都度上書きする。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _insert_listing(eid: str, *, image_url=None, fetched_at_sql: str | None = None):
    """ebay_listings に最小行を 1 件 INSERT。

    fetched_at_sql: None なら ebay_image_fetched_at は NULL。
      'now' なら CURRENT_TIMESTAMP (鮮度内)。
      それ以外は文字列をそのまま INSERT (古い日付で stale を作る)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, "
            "ebay_image_url, ebay_image_fetched_at) VALUES (?, ?, ?, ?, "
            + ("CURRENT_TIMESTAMP" if fetched_at_sql == "now" else "?")
            + ")",
            (eid, f"ebayme_{eid}", "Test Item", image_url)
            if fetched_at_sql == "now"
            else (eid, f"ebayme_{eid}", "Test Item", image_url, fetched_at_sql),
        )


def test_migration_v63_adds_image_columns():
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "ebay_image_url" in cols
    assert "ebay_image_fetched_at" in cols
    assert ver >= 63, f"user_version は v63 以上であるべき (got {ver})"


def test_cache_hit_does_not_call_getitem(monkeypatch):
    from monitor.database import init_db
    from monitor import ebay_image_fetcher
    init_db()
    _insert_listing("100", image_url="https://i.ebayimg.com/cached/s-l1600.jpg",
                    fetched_at_sql="now")

    def _boom(*_a, **_k):
        raise AssertionError("cache hit なのに GetItem を叩いた")
    monkeypatch.setattr(ebay_image_fetcher, "_api_image_urls", _boom)

    from monitor.ebay_listing_image import get_ebay_image_url
    assert get_ebay_image_url("100") == "https://i.ebayimg.com/cached/s-l1600.jpg"


def test_cache_miss_fetches_and_persists(monkeypatch):
    from monitor.database import init_db, get_conn
    from monitor import ebay_image_fetcher
    init_db()
    _insert_listing("200")  # ebay_image_url=NULL, fetched_at=NULL

    monkeypatch.setattr(
        ebay_image_fetcher, "_api_image_urls",
        lambda eid: ["https://i.ebayimg.com/fresh1.jpg",
                     "https://i.ebayimg.com/fresh2.jpg"],
    )

    from monitor.ebay_listing_image import get_ebay_image_url
    got = get_ebay_image_url("200")
    assert got == "https://i.ebayimg.com/fresh1.jpg", "1 枚目を採用"

    # DB に cache されたこと (次回 hit 用)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ebay_image_url, ebay_image_fetched_at FROM ebay_listings "
            "WHERE ebay_item_id=?", ("200",),
        ).fetchone()
    assert row[0] == "https://i.ebayimg.com/fresh1.jpg"
    assert row[1] is not None, "fetched_at が記録される"


def test_stale_cache_refetches(monkeypatch):
    from monitor.database import init_db
    from monitor import ebay_image_fetcher
    init_db()
    # 30 日窓より古い fetched_at + 旧 URL
    _insert_listing("300", image_url="https://i.ebayimg.com/OLD.jpg",
                    fetched_at_sql="2020-01-01 00:00:00")

    monkeypatch.setattr(
        ebay_image_fetcher, "_api_image_urls",
        lambda eid: ["https://i.ebayimg.com/NEW.jpg"],
    )

    from monitor.ebay_listing_image import get_ebay_image_url
    assert get_ebay_image_url("300") == "https://i.ebayimg.com/NEW.jpg", "stale は再取得"


def test_getitem_empty_returns_none_fail_open(monkeypatch):
    from monitor.database import init_db
    from monitor import ebay_image_fetcher
    init_db()
    _insert_listing("400")
    monkeypatch.setattr(ebay_image_fetcher, "_api_image_urls", lambda eid: [])

    from monitor.ebay_listing_image import get_ebay_image_url
    assert get_ebay_image_url("400") is None, "取得不能は None (fail-open)"


def test_empty_ebay_item_id_returns_none():
    from monitor.ebay_listing_image import get_ebay_image_url
    assert get_ebay_image_url("") is None
    assert get_ebay_image_url(None) is None


def test_init_db_idempotent_preserves_image_cache(monkeypatch):
    """Q2: init_db 2 回で ebay_image_url が消えない (冪等性)."""
    from monitor.database import init_db, get_conn
    init_db()
    _insert_listing("500", image_url="https://i.ebayimg.com/keep.jpg",
                    fetched_at_sql="now")
    init_db()  # 再実行
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ebay_image_url FROM ebay_listings WHERE ebay_item_id=?",
            ("500",),
        ).fetchone()
    assert row is not None and row[0] == "https://i.ebayimg.com/keep.jpg", (
        "init_db 再実行で cache が消失 = 冪等性違反"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
