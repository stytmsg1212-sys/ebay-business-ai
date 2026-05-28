"""W68 Step 1 regression test: init_db() の DB drift 解消 (PMC + MSD canonical 新スキーマ化).

検証対象 (`monitor/database.py:1498-1508` PMC, `:1512-1522` MSD, v26 block gate):
- canonical pending_market_changes は ebay_item_id PRIMARY KEY (新スキーマ) で作成
- canonical market_strategy_decisions は ebay_item_id TEXT NOT NULL を持つ (新スキーマ)
- 既存 v27 DB で init_db 2 回連続実行してデータ + スキーマ完全保持 (Q2 冪等性)
- 新規環境 (user_version=0) で init_db 完走で新スキーマ作成 + 孤児 _new テーブル 0 個
- 旧 v25 DB から起動するレガシーパスでは v26 block gate が False → _new 作成 + INSERT JOIN 実走
  (本 init_db 内では _new までで停止、RENAME は one-shot script 責務).
  one-shot script `migrate_pending_to_listing_v26.py` 後の完了状態も verify (HIGH-A 対応).

過去事故: 2026-04-29 W7-A SKU 主キー崩壊 / 2026-04-30 SKU 一意性誤推論 (連続違反 = 品質事故).
詳細: `.claude/rules/sku-rules.md` / `feedback_sku_misuse_repeat_offense.md` /
      `.claude/rules/db-migration-rules.md` (Q2 冪等性ルール).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を作って monitor.database.DB_PATH を差し替え + init_db で schema 作成"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r[5] == 1]


def _column_notnull(conn: sqlite3.Connection, table: str, col: str) -> int:
    """対象列の notnull constraint (1 if NOT NULL, 0 otherwise). 列無しなら -1."""
    for r in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if r[1] == col:
            return r[3]
    return -1


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def test_v29_supplier_eval_pending_idempotent_fresh(tmp_path, monkeypatch):
    """W94 RT-1 (code-reviewer H-2): fresh DB → init_db 2 回連続 → user_version=44
    維持 + supplier_eval_pending データ保持 + UNIQUE(custom_id, batch_id) 効く.

    canonical HEAD は W140 (2026-05-19) で v44 (メモ/売却警告、
    v41 W138-A → v42 W7/W183 H4 race → v43 W142 +each の後)。本テストの不変条件は
    「init_db が HEAD へ収束し再実行で drift しない」であり、版数は
    HEAD 追従 (migration 追加時は HEAD pin を cascade 更新)."""
    db_path = tmp_path / "v29.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()
    with db_mod.get_conn() as c:
        c.execute(
            "INSERT INTO supplier_eval_pending (custom_id, batch_id, reason) "
            "VALUES (?, ?, ?)",
            ("eid1|mercari|0", "msgbatch_test", "hard_timeout"),
        )

    db_mod.init_db()  # 2 回目
    with db_mod.get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 55  # canonical HEAD (...→v52 W153 v2→v53 W139-revisit→v54 W182→v55 W183)
        n = c.execute("SELECT COUNT(*) FROM supplier_eval_pending").fetchone()[0]
        assert n == 1, "v29 migration 再実行でデータ消失 (Q2 冪等性違反)"

    # UNIQUE(custom_id, batch_id) 制約 verify (別 connection で例外確認)
    with sqlite3.connect(db_path) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO supplier_eval_pending (custom_id, batch_id, reason) "
                "VALUES (?, ?, ?)",
                ("eid1|mercari|0", "msgbatch_test", "duplicate"),
            )


def test_init_db_idempotent_v27_existing_db(tmp_db):
    """既存 v27 DB で init_db 2 回連続実行: スキーマ + データ完全保持 (Q2 冪等性)"""
    from monitor.database import get_conn, init_db

    # 1 回目 init_db は fixture 内で完走済 → user_version=27
    with get_conn() as c:
        ver_before = c.execute("PRAGMA user_version").fetchone()[0]
        # PMC + MSD にデータ INSERT (foreign key 充足のため market_analysis 親 row 挿入)
        c.execute(
            "INSERT INTO market_analysis (sku, keyword, day_range, total_sold, "
            "us_count, non_us_count, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("stock:01", "test", 30, 5, 3, 2, datetime.now()),
        )
        ma_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO pending_market_changes "
            "(ebay_item_id, sku, current_market, proposed_market, proposed_at, "
            "market_analysis_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ITEM_TEST", "stock:01", "mixed_global", "US_only",
             datetime.now(), ma_id, "test"),
        )
        c.execute(
            "INSERT INTO market_strategy_decisions "
            "(sku, ebay_item_id, previous_market, proposed_market, final_market, "
            "action, decided_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("stock:01", "ITEM_TEST", "mixed_global", "US_only", "US_only",
             "approved", datetime.now(), "test"),
        )

    # 2 回目 init_db
    init_db()

    with get_conn() as c:
        ver_after = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver_before == ver_after == 55, (
            f"user_version drift: before={ver_before} after={ver_after} "
            "(期待 55 維持: v55 = W183 EC直URL canonical HEAD)"
        )
        assert _pk_columns(c, "pending_market_changes") == ["ebay_item_id"]
        assert _column_notnull(c, "market_strategy_decisions", "ebay_item_id") == 1, (
            "market_strategy_decisions.ebay_item_id NOT NULL constraint 喪失"
        )
        # データ保持
        pmc_count = c.execute("SELECT COUNT(*) FROM pending_market_changes").fetchone()[0]
        msd_count = c.execute("SELECT COUNT(*) FROM market_strategy_decisions").fetchone()[0]
        assert pmc_count == 1, f"PMC データ消失: count={pmc_count}"
        assert msd_count == 1, f"MSD データ消失: count={msd_count}"


def test_init_db_fresh_env_creates_new_schema(tmp_path, monkeypatch):
    """user_version=0 の fresh DB から init_db で新スキーマ作成 + _new 孤児 0 個"""
    db_path = tmp_path / "fresh.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    db_mod.init_db()

    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 55, f"user_version != 55: {ver} (期待 55 = HEAD: ...→ v52 W153 v2 → v55 W183 EC直URL)"

        # canonical 新スキーマ
        assert _pk_columns(c, "pending_market_changes") == ["ebay_item_id"]
        assert _column_notnull(c, "pending_market_changes", "sku") == 1
        assert _column_notnull(
            c, "pending_market_changes", "market_analysis_id"
        ) == 1
        assert _column_notnull(c, "market_strategy_decisions", "ebay_item_id") == 1

        # 孤児 _new 残存 0
        assert not _table_exists(c, "pending_market_changes_new"), (
            "fresh env で _new 孤児テーブルが残存 (gate が機能していない)"
        )
        assert not _table_exists(c, "market_strategy_decisions_new"), (
            "fresh env で msd_new 孤児テーブルが残存 (gate が機能していない)"
        )


def test_init_db_v25_to_v26_legacy_migration_path(tmp_path, monkeypatch):
    """旧 v25 DB (PMC sku PK / MSD ebay_item_id 列なし) から起動 →
    v26 block gate False → _new 作成 + 旧→新 INSERT JOIN 実走 (HIGH-A 強化版)

    本 init_db 内では _new までで停止 (canonical 旧スキーマ残存) →
    one-shot script `migrate_pending_to_listing_v26.py` 実行で canonical 新スキーマ昇格 →
    完了状態 verify
    """
    db_path = tmp_path / "v25.db"

    # v25 旧スキーマ DB を手動構築 (init_db を介さず直接 CREATE)
    with sqlite3.connect(db_path) as c:
        c.execute("""
            CREATE TABLE pending_market_changes (
                sku TEXT PRIMARY KEY,
                current_market TEXT,
                proposed_market TEXT NOT NULL,
                proposed_at TIMESTAMP NOT NULL,
                market_analysis_id INTEGER,
                reason TEXT
            )
        """)
        c.execute("""
            CREATE TABLE market_strategy_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                previous_market TEXT,
                proposed_market TEXT,
                final_market TEXT,
                action TEXT,
                decided_at TIMESTAMP NOT NULL,
                reason TEXT,
                reviewer TEXT DEFAULT 'user'
            )
        """)
        c.execute("PRAGMA user_version = 25")

    # init_db 実行 (新版): v26 block 突入で gate=False → _new 作成パス実走
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()

    # Stage 1: v26 block 実走済 (canonical 旧 + _new 作成 + user_version=27)
    with sqlite3.connect(db_path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 55, f"user_version != 55: {ver} (期待 55 = HEAD: ...→ v52 W153 v2 → v55 W183 EC直URL)"
        # canonical はまだ旧スキーマ (RENAME は one-shot script 責務)
        assert _pk_columns(c, "pending_market_changes") == ["sku"], (
            "v26 gate が False で canonical 旧スキーマ維持 (期待動作)"
        )
        # _new が作成されている
        assert _table_exists(c, "pending_market_changes_new"), (
            "v26 block gate=False で _new 作成パス未実走 (gate logic bug)"
        )
        assert _table_exists(c, "market_strategy_decisions_new"), (
            "v26 block gate=False で msd_new 未作成"
        )
        # _new schema verify
        assert _pk_columns(c, "pending_market_changes_new") == ["ebay_item_id"]
        assert _column_notnull(
            c, "market_strategy_decisions_new", "ebay_item_id"
        ) == 1

    # Stage 2: one-shot script 実行 → canonical 新スキーマ昇格.
    # sys.path + sys.modules 両方を try/finally で cleanup (test 並列性 / 順序非依存性確保).
    import importlib
    import sys
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    try:
        swap_mod = importlib.import_module("scripts.migrate_pending_to_listing_v26")
        rc = swap_mod.main()
    finally:
        sys.modules.pop("scripts.migrate_pending_to_listing_v26", None)
        sys.modules.pop("scripts", None)
        if str(project_root) in sys.path:
            sys.path.remove(str(project_root))
    assert rc == 0, f"one-shot script failed: rc={rc}"

    # Stage 2 verify: swap 完了
    with sqlite3.connect(db_path) as c:
        assert _pk_columns(c, "pending_market_changes") == ["ebay_item_id"]
        assert _column_notnull(c, "market_strategy_decisions", "ebay_item_id") == 1
        assert not _table_exists(c, "pending_market_changes_new")
        assert not _table_exists(c, "market_strategy_decisions_new")


def test_init_db_v26_block_skips_when_canonical_already_new(tmp_path, monkeypatch):
    """user_version<26 だが canonical PMC が既に新スキーマで存在する場合 (人為的不整合)、
    v26 block gate が True → _new 作成を skip して user_version=26 にだけ上げる
    """
    db_path = tmp_path / "manual.db"

    # canonical を手動で新スキーマ化、user_version=25 にセット (人為的 inconsistency)
    with sqlite3.connect(db_path) as c:
        c.execute("""
            CREATE TABLE pending_market_changes (
                ebay_item_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                current_market TEXT,
                proposed_market TEXT NOT NULL,
                proposed_at TIMESTAMP NOT NULL,
                market_analysis_id INTEGER NOT NULL,
                reason TEXT
            )
        """)
        c.execute("""
            CREATE TABLE market_strategy_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                ebay_item_id TEXT NOT NULL,
                previous_market TEXT,
                proposed_market TEXT,
                final_market TEXT,
                action TEXT,
                decided_at TIMESTAMP NOT NULL,
                reason TEXT,
                reviewer TEXT DEFAULT 'user'
            )
        """)
        c.execute("PRAGMA user_version = 25")

    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()

    with sqlite3.connect(db_path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 55, f"user_version != 55: {ver} (期待 55 = HEAD: ...→ v52 W153 v2 → v55 W183 EC直URL)"
        # gate True で _new 作成 skip
        assert not _table_exists(c, "pending_market_changes_new"), (
            "gate=True で _new 作成 skip 動作せず (孤児発生)"
        )
        assert not _table_exists(c, "market_strategy_decisions_new"), (
            "gate=True で msd_new 作成 skip 動作せず"
        )
        # canonical 新スキーマ維持
        assert _pk_columns(c, "pending_market_changes") == ["ebay_item_id"]
        assert _column_notnull(c, "market_strategy_decisions", "ebay_item_id") == 1
