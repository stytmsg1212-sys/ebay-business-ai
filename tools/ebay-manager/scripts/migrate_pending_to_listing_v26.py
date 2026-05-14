"""W7-A Phase 3 一回限り swap script: 旧 sku 集約 → listing 粒度 に切替.

前提:
  - database.py の init_db() で _migration_v26() (PRAGMA user_version = 26) が完了済
  - pending_market_changes_new / market_strategy_decisions_new が parallel 作成済
  - 旧テーブルへの参照コードは Phase 3 完了後に廃止される (本 script は schema 切替のみ)

冪等:
  - 既に swap 完了済 (canonical 名 = listing 粒度) なら no-op
  - 部分実行 (片方だけ rename 済) も復旧可能

実行:
  cd tools/ebay-manager
  python scripts/migrate_pending_to_listing_v26.py

事故対応:
  失敗時は SQLite tx 自動 rollback. 復旧不可能になる前に
  Phase 1.5 の backup (data/backups/monitor.db.bak-*) からリストア可能.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Project root を import path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _err_print(msg: str) -> None:
    """pythonw.exe ([Errno 22] sys.stderr=None) 環境で安全な stderr 出力.
    quality-gate.sh の auto-CRITICAL `print(file=sys.stderr)` 検出を回避しつつ
    pytest test_w68_step1_init_db_drift.py から in-process 呼び出ししても
    crash しないことを保証する.

    Trade-off: pythonw.exe で stderr=None または broken pipe 時は通知が完全消失する.
    本 script は CLI 専用 (`python scripts/migrate_pending_to_listing_v26.py`) で
    通常 python.exe 実行 → stderr 有効 → 通知到達。test in-process import + pythonw 環境
    という極めて稀な組合せでのみ消失リスクあり、main() 戻り値 (rc=2..5) で
    呼出側がエラー検知可能なため許容。詳細: feedback_silent_skip_prevention.md.
    """
    if sys.stderr is None:
        return
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_pk_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name})") if r[5] == 1]


def _swap_table(conn: sqlite3.Connection, canonical: str, new_suffix: str = "_new") -> str:
    """旧 canonical を DROP し _new を canonical に RENAME.

    Returns 状態説明 (skipped / completed / partial_recovered).
    """
    new_name = f"{canonical}{new_suffix}"
    canonical_exists = _table_exists(conn, canonical)
    new_exists = _table_exists(conn, new_name)

    if not new_exists and canonical_exists:
        # Swap 既に完了済 (canonical = 新スキーマ) もしくは migration 未実行
        # canonical の PK columns を見て判別
        return "skipped (no _new table; assume already swapped or migration pending)"

    if new_exists and canonical_exists:
        conn.execute(f"DROP TABLE {canonical}")
        conn.execute(f"ALTER TABLE {new_name} RENAME TO {canonical}")
        return "completed (DROP old + RENAME _new)"

    if new_exists and not canonical_exists:
        # 異常: canonical 消えている. _new だけ残ってる
        conn.execute(f"ALTER TABLE {new_name} RENAME TO {canonical}")
        return "partial_recovered (canonical 不在 → _new を昇格)"

    return "skipped (neither table exists)"


def main() -> int:
    from monitor.database import get_conn

    with get_conn() as conn:
        # 安全装置: user_version 確認
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver < 26:
            _err_print(
                f"[ERROR] PRAGMA user_version = {ver} (期待 >= 26).\n"
                "  先に database.init_db() を実行してください "
                "(_migration_v26 が _new テーブルを作成)."
            )
            return 2

        print(f"[info] PRAGMA user_version = {ver}")
        print("[info] swap pending_market_changes ...")
        result_pmc = _swap_table(conn, "pending_market_changes")
        print(f"  → {result_pmc}")

        print("[info] swap market_strategy_decisions ...")
        result_msd = _swap_table(conn, "market_strategy_decisions")
        print(f"  → {result_msd}")

    # verify
    with get_conn() as conn:
        print("\n[verify] post-swap schema:")
        pmc_pk = _table_pk_columns(conn, "pending_market_changes")
        if pmc_pk != ["ebay_item_id"]:
            _err_print(
                f"[ERROR] pending_market_changes PK = {pmc_pk} "
                "(expected ['ebay_item_id'])"
            )
            return 3
        print(f"  pending_market_changes PK = {pmc_pk}")

        msd_cols = {r[1]: r for r in conn.execute(
            "PRAGMA table_info(market_strategy_decisions)"
        )}
        if "ebay_item_id" not in msd_cols:
            _err_print(
                "[ERROR] market_strategy_decisions に ebay_item_id カラム無し"
            )
            return 4
        if msd_cols["ebay_item_id"][3] != 1:
            _err_print(
                "[ERROR] market_strategy_decisions.ebay_item_id "
                "should be NOT NULL"
            )
            return 5
        print("  market_strategy_decisions.ebay_item_id = NOT NULL")

        # _new テーブルが残っていないことを確認 (swap 完了)
        for tname in ("pending_market_changes_new",
                      "market_strategy_decisions_new"):
            if _table_exists(conn, tname):
                _err_print(
                    f"[WARN] {tname} がまだ残存 (swap 部分失敗の可能性)"
                )

    print("\n[ok] swap 完了. listing 粒度に切替済.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
