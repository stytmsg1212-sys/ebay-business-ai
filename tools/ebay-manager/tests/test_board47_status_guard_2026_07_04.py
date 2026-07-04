#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード#47: 在庫監視「状態不明」の user 手動回答が毎晩上書きされる問題の修正 (案A)。

root cause: sync_inventory_status_to_db が新 status を無条件 UPDATE していたため、
在庫監視タブ (#21) で user が「在庫有/在庫無/ページなし」と手動確定した明確な値が、
翌晩以降スクレイパーが判定不能 (不明/エラー/unknown) を返しただけで上書きされていた。

案A: 新status が不明系 かつ 既存が明確な値の場合のみ status 上書きを skip する。
既存も不明系なら (より新しい不明系の方が情報量が多いため) 上書きする。

3 象限 + 境界ケースを検証。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_temp_db(tmp_path) -> Path:
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


def _seed(db_path: Path, ebay_item_id: str, source_url: str, source_status: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, source_url, source_status) "
        "VALUES (?, ?, ?, ?)",
        (ebay_item_id, "ebayyh_test", source_url, source_status),
    )
    conn.commit()
    conn.close()


def _write_inv_json(tmp_path: Path, results: list[dict]):
    (tmp_path / "data" / "inventory_check_results.json").write_text(
        json.dumps({"results": results}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def patched_module(tmp_path, monkeypatch):
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


def _read_status_and_checked(db_path: Path, ebay_item_id: str):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT source_status, source_last_checked FROM ebay_listings "
        "WHERE ebay_item_id=?",
        (ebay_item_id,),
    ).fetchone()
    conn.close()
    return row


# ---- 象限 (a): 既存=明確 + 新=不明系 → 保護 (上書きしない) ----

def test_clear_existing_unclear_new_is_guarded(patched_module, caplog):
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_A", "https://example.com/a", "在庫有")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/a", "ebay_id": None,
        "status": "不明", "checked_at": "2026-07-04T03:00:00",
    }])
    import logging
    with caplog.at_level(logging.WARNING):
        r = t.sync_inventory_status_to_db()

    assert r["guarded"] == 1, f"guarded カウントが立っていない: {r}"
    status, checked = _read_status_and_checked(db_path, "EID_A")
    assert status == "在庫有", "明確な既存値が不明系で上書きされてしまった"
    assert checked == "2026-07-04T03:00:00", (
        "last_checked はチェック実施の記録として更新されるべき"
    )
    assert any("明確な状態を保護" in rec.message for rec in caplog.records), (
        "Q0: skip の痕跡が log に残っていない"
    )


def test_clear_existing_error_status_new_is_guarded(patched_module):
    """新status='エラー' でも同様に保護される (不明系は複数値ある)."""
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_B", "https://example.com/b", "在庫無")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/b", "ebay_id": None,
        "status": "エラー", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 1
    status, _ = _read_status_and_checked(db_path, "EID_B")
    assert status == "在庫無"


def test_clear_existing_page_not_found_new_is_guarded(patched_module):
    """既存=ページなし (明確値) も保護対象."""
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_C", "https://example.com/c", "ページなし")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/c", "ebay_id": None,
        "status": "不明", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 1
    status, _ = _read_status_and_checked(db_path, "EID_C")
    assert status == "ページなし"


# ---- 象限 (b): 既存=不明系 + 新=不明系 → 更新 (最新の不明系を採用) ----

def test_unclear_existing_unclear_new_is_updated(patched_module):
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_D", "https://example.com/d", "不明")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/d", "ebay_id": None,
        "status": "エラー", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 0, "既存も不明系なら guard 対象外のはず"
    status, _ = _read_status_and_checked(db_path, "EID_D")
    assert status == "エラー", "既存不明系→新不明系は更新されるべき (最新エラー種別を見せる)"


def test_unknown_default_existing_unclear_new_is_updated(patched_module):
    """DB デフォルト値 'unknown' (英語) からの遷移も不明系扱いで更新される."""
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_E", "https://example.com/e", "unknown")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/e", "ebay_id": None,
        "status": "不明", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 0
    status, _ = _read_status_and_checked(db_path, "EID_E")
    assert status == "不明"


# ---- 象限 (c): 既存=明確 + 新=明確 → 更新 (通常の状態遷移) ----

def test_clear_existing_clear_new_is_updated(patched_module):
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_F", "https://example.com/f", "在庫有")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/f", "ebay_id": None,
        "status": "在庫無", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 0, "既存/新とも明確値なら guard されないはず"
    status, _ = _read_status_and_checked(db_path, "EID_F")
    assert status == "在庫無", "通常の状態遷移 (在庫有→在庫無) は反映されるべき"


# ---- 境界: 既存=不明系 + 新=明確 → 更新 (不明が解消されるので当然反映) ----

def test_unclear_existing_clear_new_is_updated(patched_module):
    t, tmp_path, db_path = patched_module
    _seed(db_path, "EID_G", "https://example.com/g", "不明")
    _write_inv_json(tmp_path, [{
        "sku": "ebayyh_test", "url": "https://example.com/g", "ebay_id": None,
        "status": "在庫有", "checked_at": "2026-07-04T03:00:00",
    }])
    r = t.sync_inventory_status_to_db()
    assert r["guarded"] == 0
    status, _ = _read_status_and_checked(db_path, "EID_G")
    assert status == "在庫有"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
