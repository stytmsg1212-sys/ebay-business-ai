"""H2 回帰テスト (2026-05-29 Opus 4.8 総チェック): ebay_listings の
FOREIGN KEY(sku) -> monitored_items(sku) 撤廃が安全であることを保証する.

背景:
- sku は listing 識別子ではない (stock**/ebay** を多数 listing が共有). FK(sku) は
  意味論的に誤り. v28 で参照先 monitored_items.UNIQUE(sku) も撤廃済 = 無効 FK だった.
- listing 識別は ebay_item_id (NOT NULL UNIQUE). sku-rules.md 準拠.

不変条件 (本テストが守る):
1. fresh DB の ebay_listings スキーマに sku を参照する FOREIGN KEY が存在しない.
2. ebay_item_id NOT NULL UNIQUE は維持 (listing 識別子).
3. init_db 2 回 → ebay_listings のデータ保持 (冪等性, Q2).
4. sku を複数 listing が共有できる (FK 制約に阻まれない = SKU 規約の正常動作).
"""
from __future__ import annotations

import sqlite3

import pytest


def _ebay_listings_schema_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ebay_listings'"
    ).fetchone()
    return row[0] if row and row[0] else ""


def test_fresh_db_ebay_listings_has_no_sku_fk(tmp_path, monkeypatch):
    """fresh DB の ebay_listings に sku FK が無く、ebay_item_id UNIQUE は維持."""
    db_path = tmp_path / "fresh.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    with db_mod.get_conn() as c:
        # 実際の FK 定義を PRAGMA で検査 (CREATE 文の SQL はコメントを含むため文字列マッチ不可).
        fks = c.execute("PRAGMA foreign_key_list(ebay_listings)").fetchall()
        sku_fks = [f for f in fks if "sku" in (str(f[3]).lower(), str(f[4]).lower())]
        assert not sku_fks, f"sku を参照する FOREIGN KEY が残存: {sku_fks}"
        # ebay_item_id NOT NULL UNIQUE (listing 識別子) は維持されていること.
        schema = _ebay_listings_schema_sql(c)
        assert "ebay_item_id" in schema and "UNIQUE" in schema.upper(), \
            "ebay_item_id UNIQUE (listing 識別子) が失われた"


def test_ebay_listings_idempotent_and_shared_sku(tmp_path, monkeypatch):
    """init_db 2 回でデータ保持 + 同一 sku を複数 listing が共有できる."""
    db_path = tmp_path / "idem.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    with db_mod.get_conn() as c:
        # FK が残っていれば monitored_items に無い sku の INSERT は (enforced 時) 失敗するが、
        # 未強制でも「同一 sku を複数 listing が持つ」のが SKU 規約上の正常動作.
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title) VALUES (?,?,?)",
            ("111111111111", "stock:01", "Item A"),
        )
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title) VALUES (?,?,?)",
            ("222222222222", "stock:01", "Item B"),
        )

    db_mod.init_db()  # 2 回目: データ保持
    with db_mod.get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM ebay_listings WHERE sku='stock:01'"
        ).fetchone()[0]
        assert n == 2, "同一 sku を複数 listing が共有できない or データ消失"
        # ebay_item_id で 1 件特定できる (listing 識別子)
        row = c.execute(
            "SELECT title FROM ebay_listings WHERE ebay_item_id='222222222222'"
        ).fetchone()
        assert row[0] == "Item B"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
