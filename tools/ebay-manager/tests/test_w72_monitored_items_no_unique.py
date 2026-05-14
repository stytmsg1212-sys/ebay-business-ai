"""W72 regression tests: monitored_items.UNIQUE(sku) 撤廃の挙動 verify.

SKU rule (.claude/rules/sku-rules.md) 準拠: UNIQUE(sku) は禁止される制約形式.
本 test は撤廃が **完了していること** + 撤廃の **必要性** (旧スキーマで重複 INSERT
が失敗する) を両側から assert する.
"""
from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _create_monitored_items_new(conn: sqlite3.Connection) -> None:
    """W72 新スキーマ (UNIQUE なし) で monitored_items 単独 CREATE."""
    conn.execute("""
        CREATE TABLE monitored_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT,
            title TEXT,
            sku TEXT NOT NULL,
            source_url TEXT,
            site_config_id INTEGER,
            is_active INTEGER DEFAULT 1,
            last_status TEXT DEFAULT 'unknown',
            last_check TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _create_monitored_items_legacy(conn: sqlite3.Connection) -> None:
    """W72 撤廃前の旧スキーマ (UNIQUE(sku) 残存)."""
    conn.execute("""
        CREATE TABLE monitored_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT,
            sku TEXT NOT NULL UNIQUE
        )
    """)


def test_w72_new_schema_has_no_unique_on_sku(tmp_path):
    """新スキーマで sqlite_autoindex (= UNIQUE 由来) が存在しないこと."""
    db = tmp_path / "w72_new.db"
    conn = sqlite3.connect(str(db))
    _create_monitored_items_new(conn)
    autoindex = [
        r[1] for r in conn.execute("PRAGMA index_list(monitored_items)").fetchall()
        if r[1].startswith("sqlite_autoindex")
    ]
    assert not autoindex, f"UNIQUE 制約残存: {autoindex}"
    conn.close()


def test_w72_duplicate_sku_insert_succeeds_in_new_schema(tmp_path):
    """新スキーマで 同 sku の複数行 INSERT が成功すること.

    有在庫 (stock:01) を将来 monitored_items に入れる場合の前提条件.
    """
    db = tmp_path / "w72_dup.db"
    conn = sqlite3.connect(str(db))
    _create_monitored_items_new(conn)
    conn.execute(
        "INSERT INTO monitored_items (ebay_item_id, sku, source_url) VALUES (?,?,?)",
        ("EID_A", "stock:01", None),
    )
    conn.execute(
        "INSERT INTO monitored_items (ebay_item_id, sku, source_url) VALUES (?,?,?)",
        ("EID_B", "stock:01", None),
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM monitored_items WHERE sku='stock:01'"
    ).fetchone()[0]
    assert n == 2, f"同 sku 複数行 INSERT 失敗: count={n}"
    conn.close()


def test_w72_legacy_schema_rejects_duplicate_sku(tmp_path):
    """逆 assert: 旧スキーマ (UNIQUE 残存) では重複 INSERT が失敗 = 撤廃の必要性根拠."""
    db = tmp_path / "w72_legacy.db"
    conn = sqlite3.connect(str(db))
    _create_monitored_items_legacy(conn)
    conn.execute("INSERT INTO monitored_items (sku) VALUES ('stock:01')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO monitored_items (sku) VALUES ('stock:01')")
    conn.close()


def test_w72_upsert_item_no_sku_lookup():
    """database.upsert_item の関数本体に SKU 経由 lookup が残っていないこと.

    SKU rule 違反 lookup (`WHERE sku=?` で 1 listing を特定) の物理消滅 verify.
    source_url ベース lookup は許容 (sku から派生計算 = SKU rule 許容用途).
    """
    from monitor import database as db_mod
    src = inspect.getsource(db_mod.upsert_item)
    # SKU rule 違反パターン (sku 単体での lookup) の不在 assert.
    # 単純文字列 match では `WHERE sku=?` を含む comment も誤検出するので、
    # SQL の SELECT/UPDATE 句に限定して match する.
    bad_patterns = [
        '"SELECT id FROM monitored_items WHERE sku=?"',
        "'SELECT id FROM monitored_items WHERE sku=?'",
    ]
    for pat in bad_patterns:
        assert pat not in src, (
            f"upsert_item に SKU 経由 lookup ({pat}) が残存。"
            "SKU rule 違反: listing 識別は ebay_item_id か source_url を使う。"
        )


def test_w72_upsert_item_uses_source_url_fallback():
    """upsert_item 関数本体に source_url fallback ロジックが含まれていること."""
    from monitor import database as db_mod
    src = inspect.getsource(db_mod.upsert_item)
    assert "WHERE source_url=?" in src, (
        "upsert_item に source_url fallback lookup が見つからない。"
        "ebay_item_id 不在時の identify 経路として必須。"
    )
