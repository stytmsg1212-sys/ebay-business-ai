#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W75 4c: sync_inventory_status_to_db SKU rule violation regression.

旧: `WHERE sku=?` で listing 1 件特定 → SKU 多 listing 共有時に非決定論動作
新: (1) JSON ebay_id 直接利用 → (2) source_url 逆引き → 両方なければ silent skip 防止 warning

inventory_check_results.json の実データは ebay* SKU のみ + URL 充足 99.7% で
影響範囲は限定的だが、SKU rule (.claude/rules/sku-rules.md) の根本是正.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_temp_db(tmp_path) -> Path:
    """最小構造の ebay_listings テーブルを持つ temp DB を作る."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE ebay_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT UNIQUE NOT NULL,
            sku TEXT,
            source_url TEXT,
            source_status TEXT,
            source_last_checked TEXT,
            source_out_of_stock_since TEXT,
            is_ended INTEGER DEFAULT 0,
            quantity_ebay INTEGER DEFAULT 0
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _seed_listings(db_path: Path, listings: list[dict]):
    conn = sqlite3.connect(db_path)
    for L in listings:
        conn.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, source_url, source_status) "
            "VALUES (?, ?, ?, ?)",
            (L["ebay_item_id"], L.get("sku"), L.get("source_url"),
             L.get("source_status")),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def patched_module(tmp_path, monkeypatch):
    """task_sync_data_stores を temp DB + 任意 JSON で動かす fixture."""
    from tasks import task_sync_data_stores as t
    db_path = _make_temp_db(tmp_path)

    def _temp_get_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(t, "get_conn", _temp_get_conn)
    monkeypatch.setattr(t, "BASE_DIR", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    return t, tmp_path, db_path


def _write_inv_json(tmp_path: Path, results: list[dict]):
    import json
    (tmp_path / "data" / "inventory_check_results.json").write_text(
        json.dumps({"results": results}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_url_based_lookup_updates_listing(patched_module):
    """url ベース lookup で正しく ebay_listings を更新する (主経路)."""
    t, tmp_path, db_path = patched_module
    _seed_listings(db_path, [{
        "ebay_item_id": "EID_001",
        "sku": "ebayyh_q1234",
        "source_url": "https://page.auctions.yahoo.co.jp/jp/auction/q1234",
        "source_status": "在庫有",
    }])
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_q1234",
        "url": "https://page.auctions.yahoo.co.jp/jp/auction/q1234",
        "ebay_id": None,
        "status": "在庫無",
        "checked_at": "2026-05-01T12:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["updated"] == 1, f"expected 1 updated, got {r}"

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT source_status, source_out_of_stock_since "
                       "FROM ebay_listings WHERE ebay_item_id=?",
                       ("EID_001",)).fetchone()
    assert row[0] == "在庫無"
    # 在庫有 → 在庫無 遷移で source_out_of_stock_since が set される
    # 2026-06-11 BUG-2a 修正: checked_at (JST naive, 'T' 区切り) ではなく
    # UTC 現在時刻 "%Y-%m-%d %H:%M:%S" 形式で書き込まれる。
    assert row[1] is not None, "source_out_of_stock_since が NULL のまま"
    assert "T" not in row[1], f"UTC 形式でない (T 区切り含む): {row[1]!r}"
    from datetime import datetime
    datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")  # parse 可能であること


def test_ebay_id_priority_over_url(patched_module):
    """JSON ebay_id 充足時は URL より優先 (直接 lookup で 1 query)."""
    t, tmp_path, db_path = patched_module
    _seed_listings(db_path, [{
        "ebay_item_id": "EID_PRIORITY",
        "sku": "ebayyh_q9999",
        "source_url": "https://example.com/somewhere",  # JSON URL とは別
        "source_status": "在庫有",
    }])
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_q9999",
        "url": "https://different-url.com/will-not-match",  # URL は不一致
        "ebay_id": "EID_PRIORITY",  # ebay_id で直接 lookup されるべき
        "status": "在庫無",
        "checked_at": "2026-05-01T13:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["updated"] == 1, "ebay_id 経由で更新されること (URL 不一致でも)"


def test_no_identifier_logs_warning_and_counts_not_found(patched_module, caplog):
    """url + ebay_id 両方欠落で silent skip ではなく warning + not_found 計上."""
    t, tmp_path, _ = patched_module
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_orphan",
        "url": "",
        "ebay_id": None,
        "status": "在庫無",
        "checked_at": "2026-05-01T14:00:00",
    }])
    import logging
    with caplog.at_level(logging.WARNING):
        r = t.sync_inventory_status_to_db()
    assert r["updated"] == 0
    assert r["not_found"] == 1, "識別 key 不在は not_found に計上"
    # warning log で痕跡が残ること (Q0 silent skip 防止)
    assert any("identifier 不在" in rec.message for rec in caplog.records), (
        f"warning log が出ていない: {[r.message for r in caplog.records]}"
    )


def test_url_not_found_returns_not_found(patched_module):
    """JSON 内 URL が DB に存在しない場合は not_found 計上、例外なし."""
    t, tmp_path, _ = patched_module
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_ghost",
        "url": "https://nowhere.example.com/not-listed",
        "ebay_id": None,
        "status": "在庫無",
        "checked_at": "2026-05-01T15:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["updated"] == 0
    assert r["not_found"] == 1


def test_no_where_sku_in_source():
    """static check: task_sync_data_stores.py から `WHERE sku=?` SQL が消えている."""
    src = (_PROJECT_ROOT / "tasks" / "task_sync_data_stores.py").read_text(encoding="utf-8")
    # 旧 violation pattern
    assert "WHERE sku=?" not in src, (
        "`WHERE sku=?` SKU rule 違反 SQL が残存. "
        "WHERE source_url=? または WHERE ebay_item_id=? に書き換え必要."
    )
    # 新 lookup pattern が使われていること
    assert "WHERE source_url=?" in src or "source_url=?" in src, (
        "URL ベース lookup が見当たらない"
    )


def test_in_stock_recovery_clears_oos_since(patched_module):
    """在庫復活時に source_out_of_stock_since がクリアされる."""
    t, tmp_path, db_path = patched_module
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, source_url, source_status, "
        "source_out_of_stock_since) VALUES (?, ?, ?, ?, ?)",
        ("EID_REC", "ebayyh_rec", "https://example.com/rec", "在庫無", "2026-04-30T00:00:00"),
    )
    conn.commit()
    conn.close()
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_rec",
        "url": "https://example.com/rec",
        "ebay_id": None,
        "status": "在庫有",
        "checked_at": "2026-05-01T16:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["updated"] == 1

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT source_status, source_out_of_stock_since FROM ebay_listings "
        "WHERE ebay_item_id=?", ("EID_REC",),
    ).fetchone()
    assert row[0] == "在庫有"
    assert row[1] is None, "在庫復活で source_out_of_stock_since がクリアされること"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
