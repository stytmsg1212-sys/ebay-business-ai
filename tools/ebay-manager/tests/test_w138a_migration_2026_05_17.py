"""W138-A (2026-05-17): migration v41 (shipping_profile_id +
shipping_profile_fetched_at) 冪等性.

Q2 db-migration-rules:
  - fresh DB → init_db で 2 列が ebay_listings に存在 + user_version=49
    (canonical HEAD: v41 W138-A → v42 W7/W183 H4 race → v43 W142 +each
     → v44 W140 メモ/売却警告 → v45 W133-FU fulfillment_kind
     → v46 W148 キーワード新着監視
     → v47 W149 eBay 売却注文取得 + fulfillment ひも付け
     → v48 W149 sales_history UNIQUE INDEX 複合キー化 silent gap 修正
     → v49 W151 初期登録 status (initial_registered + initial_registered_at))
  - init_db を 2 回連続実行してもデータ保持・version drift なし (冪等)
  - ADD COLUMN のみ (DROP/DELETE/RENAME 不在) = 既存データ非破壊
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _cols(conn, table):
    return {r[1] for r in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}


def test_v41_columns_exist_and_version(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        cols = _cols(c, "ebay_listings")
        assert "shipping_profile_id" in cols
        assert "shipping_profile_fetched_at" in cols
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 58, f"user_version={ver} (期待 58 = HEAD: ...→ v56 W185 supplier_candidates eid unique → v57 health autofix → v58 W192 Yahoo site_config)"


def test_v41_idempotent_data_preserved(tmp_db):
    """init_db 2 回 + 間に BP 値を書いてデータ保持 (Q2 冪等性必須テスト)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, shipping_profile_id, "
            " shipping_profile_fetched_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            ("ITEM_W138A", "stock:01", "T", "BP_KEEP"),
        )
    init_db()  # 2 回目
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 58, f"version drift: {ver}"
        row = c.execute(
            "SELECT shipping_profile_id, shipping_profile_fetched_at "
            "FROM ebay_listings WHERE ebay_item_id='ITEM_W138A'"
        ).fetchone()
        assert row[0] == "BP_KEEP", "BP データ消失 (Q2 冪等性違反)"
        assert row[1] is not None, "fetched_at 消失"


def test_v41_alter_idempotent_no_crash_on_repeat(tmp_path, monkeypatch):
    """user_version を 40 に戻して再 init → ALTER 重複でも
    OperationalError で落ちない (try/except 冪等)."""
    db_path = tmp_path / "v40.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()  # → v41
    with db_mod.get_conn() as c:
        # 列は既に存在する状態で v41 block を強制再実行させる
        c.execute("PRAGMA user_version = 40")
    db_mod.init_db()  # v41 block 再突入 (ALTER 重複) → 落ちないこと
    with sqlite3.connect(db_path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 58  # v41→...→v56(W185 eid unique)→v57(health autofix)→v58(W192 Yahoo site_config) まで到達 (canonical HEAD)
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(ebay_listings)").fetchall()}
        assert "shipping_profile_id" in cols
        assert "shipping_profile_fetched_at" in cols
