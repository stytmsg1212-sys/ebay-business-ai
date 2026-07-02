"""W301 AI 店長 Phase1 S1 (2026-07-02): migration v86 冪等性.

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §4/§7(S1)

Q2 db-migration-rules:
  - fresh DB → init_db で competitor_products.pricing_eligible 列 +
    rival_classifications / competitor_snapshots / ddu_sellers /
    warning_brand_watchlist の 4 テーブルが存在 + user_version=86
  - init_db を 2 回連続実行してもデータ保持・version drift なし (冪等)
  - schema_ver を 85 に強制で戻して再突入させても ALTER 重複 /
    INSERT OR IGNORE 重複で crash しない (Holbein seed が重複しない)
  - v84 以前の DB からの upgrade パスで v85→v86 が単一 init_db 呼出で完走する
  - SKU 規約: rival_classifications / competitor_snapshots とも
    ebay_item_id / competitor_item_id / our_item_id で識別、sku 列を持たない
    (sku-rules.md 準拠)
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
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def test_v86_column_and_tables_exist_and_version(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        cols = _cols(c, "competitor_products")
        assert "pricing_eligible" in cols
        for tbl in (
            "rival_classifications",
            "competitor_snapshots",
            "ddu_sellers",
            "warning_brand_watchlist",
        ):
            assert _table_exists(c, tbl), f"{tbl} が作成されていない"
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 86, f"user_version={ver} (期待 >=86)"


def test_v86_pricing_eligible_default_zero(tmp_db):
    """新規採用は default 0 (Shadow 対象、最重要安全策)."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id) "
            "VALUES (?, ?)",
            ("OUR_NEW", "COMP_NEW"),
        )
        row = c.execute(
            "SELECT pricing_eligible FROM competitor_products "
            "WHERE competitor_item_id='COMP_NEW'"
        ).fetchone()
        assert row[0] == 0, "新規行の pricing_eligible が 0 (Shadow) でない"


def test_v86_holbein_seeded_exactly_once(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM warning_brand_watchlist WHERE brand='Holbein'"
        ).fetchone()[0]
        assert count == 1, "Holbein seed が 1 件でない"


def test_v86_idempotent_data_preserved(tmp_db):
    """init_db 2 回実行でデータ保持 (Q2 冪等性必須テスト)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible) VALUES (?,?,?)",
            ("OUR1", "COMP1", 1),
        )
        c.execute(
            "INSERT INTO rival_classifications "
            "(ebay_item_id, competitor_item_id, classification) VALUES (?,?,?)",
            ("OUR1", "COMP1", "real"),
        )
        c.execute(
            "INSERT INTO competitor_snapshots "
            "(competitor_item_id, our_item_id, quantity_sold) VALUES (?,?,?)",
            ("COMP1", "OUR1", 5),
        )
        c.execute(
            "INSERT INTO ddu_sellers (seller_id, reason) VALUES (?,?)",
            ("seller_x", "test"),
        )
    init_db()  # 2 回目
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 86, f"version drift: {ver}"
        row = c.execute(
            "SELECT pricing_eligible FROM competitor_products "
            "WHERE competitor_item_id='COMP1'"
        ).fetchone()
        assert row[0] == 1, "competitor_products データ消失 (Q2 冪等性違反)"
        assert c.execute(
            "SELECT COUNT(*) FROM rival_classifications"
        ).fetchone()[0] == 1
        assert c.execute(
            "SELECT COUNT(*) FROM competitor_snapshots"
        ).fetchone()[0] == 1
        assert c.execute(
            "SELECT COUNT(*) FROM ddu_sellers"
        ).fetchone()[0] == 1
        holbein_count = c.execute(
            "SELECT COUNT(*) FROM warning_brand_watchlist WHERE brand='Holbein'"
        ).fetchone()[0]
        assert holbein_count == 1, "Holbein seed が 2 回目実行で重複した"


def test_v86_forced_reentry_no_crash_and_no_duplicate(tmp_db):
    """user_version を 85 に戻して再 init → ALTER 重複 / seed 重複でも
    OperationalError で落ちず、データも重複しない (try/except 冪等)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO ddu_sellers (seller_id, reason) VALUES ('seller_keep','keep-me')"
        )
        c.execute("PRAGMA user_version = 85")
    init_db()  # v86 block 再突入 (ALTER 重複・INSERT OR IGNORE 重複) → 落ちないこと
    with sqlite3.connect(tmp_db) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 86
        row = c.execute(
            "SELECT reason FROM ddu_sellers WHERE seller_id='seller_keep'"
        ).fetchone()
        assert row is not None and row[0] == "keep-me", "ddu_sellers データ消失"
        holbein_count = c.execute(
            "SELECT COUNT(*) FROM warning_brand_watchlist WHERE brand='Holbein'"
        ).fetchone()[0]
        assert holbein_count == 1, "Holbein seed が再突入で重複した"
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(competitor_products)").fetchall()}
        assert "pricing_eligible" in cols


def test_v86_upgrade_path_from_v84(tmp_path, monkeypatch):
    """v84 以前の DB からの upgrade パス: v85→v86 が単一 init_db 呼出で完走する."""
    db_path = tmp_path / "v84.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()  # まず最新まで作成
    with db_mod.get_conn() as c:
        c.execute("PRAGMA user_version = 84")  # v84 相当まで巻き戻す
    db_mod.init_db()  # v85 → v86 と連続適用されること
    with sqlite3.connect(db_path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 86
        # v85 (relist_history.source) も途中適用されている
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(relist_history)").fetchall()}
        assert "source" in cols
        for tbl in (
            "rival_classifications",
            "competitor_snapshots",
            "ddu_sellers",
            "warning_brand_watchlist",
        ):
            assert c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone() is not None


def test_v86_no_sku_column_in_new_tables(tmp_db):
    """sku-rules.md: listing 識別は ebay_item_id / competitor_item_id、
    rival_classifications / competitor_snapshots は sku 列を持たない."""
    from monitor.database import get_conn
    with get_conn() as c:
        for tbl in ("rival_classifications", "competitor_snapshots"):
            cols = _cols(c, tbl)
            assert "sku" not in cols, f"{tbl} に sku 列があってはいけない"
