"""W7-A Phase 3 listing 単位化テスト.

検証対象 (2026-04-29 SKU 主キー設計崩壊事故 再発防止):
- migration v26 冪等性 (init_db を複数回呼んでも user_version=26 不変)
- pending_market_changes の PK = ebay_item_id
- market_strategy_decisions の ebay_item_id NOT NULL
- 主キー違反 (同 ebay_item_id 重複 insert で IntegrityError)
- 同 SKU 複数 listing で独立 proposed_market を持てる (cascade 事故防止の核心)
"""
from __future__ import annotations

import sqlite3

import pytest

from monitor import database as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Fresh DB に全 migration (v0-v26) + swap script 適用済の状態を作る.

    Production と同じ post-swap state (canonical 名 = listing 粒度) で test する.
    """
    tmp_db_path = tmp_path / 'monitor.db'
    monkeypatch.setattr(db, 'DB_PATH', tmp_db_path)
    db.init_db()
    # swap: 旧 sku 集約 を DROP し _new を canonical に RENAME
    from scripts.migrate_pending_to_listing_v26 import main as swap_main
    rc = swap_main()
    assert rc == 0, f"swap script failed with rc={rc}"
    yield tmp_db_path


class TestMigrationV26:
    # クラス名は v26 (W7-A listing 単位化) 検証由来。W50 で v27 が追加されたが,
    # クラス名 rename は K2 surgical 違反になるため残置。assert 値のみ追従。
    def test_user_version_is_26(self, fresh_db):
        # 2026-04-30 W50 で v27 (Yahoo Auctions ebayyh_ seed) 追加 →
        # 2026-05-01 W72 で v28 (monitored_items.UNIQUE(sku) 撤廃) 追加.
        # init_db を呼んだ後の user_version は 28 が正しい.
        with sqlite3.connect(str(fresh_db)) as c:
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            assert ver == 28

    def test_idempotent_double_run(self, fresh_db):
        """init_db を再度呼んでもエラーなし、 user_version 不変."""
        db.init_db()
        db.init_db()
        with sqlite3.connect(str(fresh_db)) as c:
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            assert ver == 28

    def test_canonical_tables_only(self, fresh_db):
        """post-swap state: _new テーブルが消え canonical のみ残る."""
        with sqlite3.connect(str(fresh_db)) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            assert "pending_market_changes" in tables
            assert "market_strategy_decisions" in tables
            assert "pending_market_changes_new" not in tables
            assert "market_strategy_decisions_new" not in tables

    def test_pending_market_changes_pk_is_ebay_item_id(self, fresh_db):
        """post-swap: canonical pending_market_changes の PK = ebay_item_id."""
        with sqlite3.connect(str(fresh_db)) as c:
            cols = list(c.execute("PRAGMA table_info(pending_market_changes)"))
            pk_cols = [r[1] for r in cols if r[5] == 1]
            assert pk_cols == ["ebay_item_id"]

    def test_market_strategy_decisions_ebay_item_id_not_null(self, fresh_db):
        """post-swap: canonical market_strategy_decisions の ebay_item_id NOT NULL."""
        with sqlite3.connect(str(fresh_db)) as c:
            cols = {r[1]: r for r in c.execute(
                "PRAGMA table_info(market_strategy_decisions)"
            )}
            # PRAGMA: (cid, name, type, notnull, dflt_value, pk)
            assert "ebay_item_id" in cols
            assert cols["ebay_item_id"][3] == 1, \
                "ebay_item_id should be NOT NULL"


class TestPKEnforcement:
    def _seed_market_analysis(self, conn, sku: str = "S1") -> int:
        conn.execute(
            "INSERT INTO market_analysis (sku, scraped_at) VALUES (?, ?)",
            (sku, "2026-04-29"),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_duplicate_ebay_item_id_blocked(self, fresh_db):
        """同 ebay_item_id 重複 insert で IntegrityError (cascade 事故核心防止)."""
        with sqlite3.connect(str(fresh_db)) as c:
            mid = self._seed_market_analysis(c)
            c.execute(
                """INSERT INTO pending_market_changes
                   (ebay_item_id, sku, proposed_market, proposed_at,
                    market_analysis_id)
                   VALUES ('E1', 'S1', 'US_only', '2026-04-29', ?)""",
                (mid,),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    """INSERT INTO pending_market_changes
                       (ebay_item_id, sku, proposed_market, proposed_at,
                        market_analysis_id)
                       VALUES ('E1', 'S1', 'mixed_global', '2026-04-29', ?)""",
                    (mid,),
                )

    def test_same_sku_multiple_listings_independent(self, fresh_db):
        """同 SKU 2 listing は独立 proposed_market を持てる (40 cascade 事故防止)."""
        with sqlite3.connect(str(fresh_db)) as c:
            mid = self._seed_market_analysis(c, sku="SHARED")
            c.execute(
                """INSERT INTO pending_market_changes
                   (ebay_item_id, sku, proposed_market, proposed_at,
                    market_analysis_id)
                   VALUES ('E1', 'SHARED', 'US_only', '2026-04-29', ?)""",
                (mid,),
            )
            c.execute(
                """INSERT INTO pending_market_changes
                   (ebay_item_id, sku, proposed_market, proposed_at,
                    market_analysis_id)
                   VALUES ('E2', 'SHARED', 'mixed_global', '2026-04-29', ?)""",
                (mid,),
            )
            rows = c.execute(
                """SELECT ebay_item_id, proposed_market
                   FROM pending_market_changes
                   WHERE sku='SHARED' ORDER BY ebay_item_id"""
            ).fetchall()
            assert rows == [("E1", "US_only"), ("E2", "mixed_global")]

    def test_market_analysis_id_required(self, fresh_db):
        """market_analysis_id は NOT NULL."""
        with sqlite3.connect(str(fresh_db)) as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    """INSERT INTO pending_market_changes
                       (ebay_item_id, sku, proposed_market, proposed_at,
                        market_analysis_id)
                       VALUES ('E1', 'S1', 'US_only', '2026-04-29', NULL)""",
                )

    def test_decisions_ebay_item_id_required(self, fresh_db):
        """market_strategy_decisions.ebay_item_id は NOT NULL."""
        with sqlite3.connect(str(fresh_db)) as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    """INSERT INTO market_strategy_decisions
                       (sku, ebay_item_id, action, decided_at)
                       VALUES ('S1', NULL, 'approved', '2026-04-29')""",
                )

    def test_decisions_action_check_constraint(self, fresh_db):
        """action は CHECK で 3 値に制限."""
        with sqlite3.connect(str(fresh_db)) as c:
            # 正常値は OK
            c.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, action, decided_at)
                   VALUES ('S1', 'E1', 'approved', '2026-04-29')""",
            )
            # 不正値で IntegrityError
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    """INSERT INTO market_strategy_decisions
                       (sku, ebay_item_id, action, decided_at)
                       VALUES ('S2', 'E2', 'invalid_action', '2026-04-29')""",
                )


class TestCascadeRejection:
    """事故核心の E2E SQL レベル: 1 listing approve → 同 SKU の他 listing は無傷.

    2026-04-29 SKU 主キー設計崩壊事故 (`stock:01` が 40 listing で共有 →
    1 件承認で 40 listing 巻添え cascade) からの再発防止の証明.

    Note: tab_market_strategy.py の _bulk_decision は streamlit import を伴うため
    test 環境での import が遅い/hang. 代わりに同等 SQL を直接実行して
    cascade 排除を検証する SQL レベル regression test.
    """

    @staticmethod
    def _run_bulk_decision_sql(conn, ebay_item_id: str, action: str) -> None:
        """tab_market_strategy._bulk_decision と同等 SQL.

        cascade 防止核心: WHERE ebay_item_id = ? に統一 (sku 集約 cascade を排除).
        """
        from datetime import datetime
        row = conn.execute(
            "SELECT * FROM pending_market_changes WHERE ebay_item_id = ?",
            (ebay_item_id,),
        ).fetchone()
        if not row:
            return
        keys = [d[0] for d in conn.execute(
            "SELECT * FROM pending_market_changes LIMIT 0"
        ).description]
        row = dict(zip(keys, row))

        final_market = (
            row["proposed_market"] if action == "approved"
            else row.get("current_market")
        )
        conn.execute(
            """INSERT INTO market_strategy_decisions
               (sku, ebay_item_id, previous_market, proposed_market,
                final_market, action, decided_at, reason, reviewer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["sku"], ebay_item_id, row.get("current_market"),
             row["proposed_market"], final_market, action,
             datetime.now().isoformat(), row.get("reason"), "user"),
        )
        if action == "approved":
            conn.execute(
                "UPDATE ebay_listings SET primary_market = ? "
                "WHERE ebay_item_id = ?",
                (row["proposed_market"], ebay_item_id),
            )
        conn.execute(
            "DELETE FROM pending_market_changes WHERE ebay_item_id = ?",
            (ebay_item_id,),
        )

    def test_approve_one_listing_does_not_cascade(self, fresh_db):
        """SHARED SKU で E1/E2 の 2 listing + 2 pending → E1 のみ approve →
        E2 の pending と primary_market が両方無傷."""
        # seed: 2 listings sharing SKU + 2 pending
        with sqlite3.connect(str(fresh_db)) as c:
            c.execute(
                """INSERT INTO ebay_listings
                   (ebay_item_id, sku, title, quantity_ebay)
                   VALUES (?, ?, ?, ?)""",
                ("E1", "SHARED", "Audio-Technica X", 1),
            )
            c.execute(
                """INSERT INTO ebay_listings
                   (ebay_item_id, sku, title, quantity_ebay)
                   VALUES (?, ?, ?, ?)""",
                ("E2", "SHARED", "Le Creuset Y", 1),
            )
            c.execute(
                "INSERT INTO market_analysis (sku, scraped_at) VALUES (?, ?)",
                ("SHARED", "2026-04-29"),
            )
            mid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                """INSERT INTO pending_market_changes
                   (ebay_item_id, sku, proposed_market, proposed_at,
                    market_analysis_id)
                   VALUES (?, ?, ?, ?, ?)""",
                ("E1", "SHARED", "US_only", "2026-04-29", mid),
            )
            c.execute(
                """INSERT INTO pending_market_changes
                   (ebay_item_id, sku, proposed_market, proposed_at,
                    market_analysis_id)
                   VALUES (?, ?, ?, ?, ?)""",
                ("E2", "SHARED", "mixed_global", "2026-04-29", mid),
            )
            c.commit()

        # E1 のみ approve (_bulk_decision 同等の SQL)
        with sqlite3.connect(str(fresh_db)) as c:
            self._run_bulk_decision_sql(c, "E1", "approved")

        # 検証: cascade 排除核心
        with sqlite3.connect(str(fresh_db)) as c:
            # E1 pending は消える
            e1_pending = c.execute(
                "SELECT * FROM pending_market_changes WHERE ebay_item_id = ?",
                ("E1",),
            ).fetchone()
            assert e1_pending is None, \
                "E1 pending must be deleted after approve"

            # E2 pending は残存 (cascade されない)
            e2_pending = c.execute(
                "SELECT * FROM pending_market_changes WHERE ebay_item_id = ?",
                ("E2",),
            ).fetchone()
            assert e2_pending is not None, \
                "E2 pending must remain (cascade rejection)"

            # E1 primary_market は US_only に更新
            e1_pm = c.execute(
                "SELECT primary_market FROM ebay_listings "
                "WHERE ebay_item_id = ?", ("E1",),
            ).fetchone()
            assert e1_pm[0] == "US_only", f"E1 primary_market = {e1_pm[0]}"

            # E2 primary_market は無傷 (NULL のまま) ← 事故核心の防止
            e2_pm = c.execute(
                "SELECT primary_market FROM ebay_listings "
                "WHERE ebay_item_id = ?", ("E2",),
            ).fetchone()
            assert e2_pm[0] is None, \
                f"E2 primary_market must remain NULL (was {e2_pm[0]}). " \
                "If this fails, cascade事故が再発しています."

            # decisions は E1 だけ記録
            decisions = c.execute(
                "SELECT ebay_item_id, action FROM market_strategy_decisions "
                "ORDER BY ebay_item_id"
            ).fetchall()
            assert decisions == [("E1", "approved")], \
                f"decisions should only contain E1, got {decisions}"
