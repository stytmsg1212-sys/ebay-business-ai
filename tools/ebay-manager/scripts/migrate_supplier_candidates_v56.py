"""W185 one-shot migration (2026-05-29 Opus 4.8 総チェック H3).

supplier_candidates の UNIQUE(sku, candidate_url) を UNIQUE(ebay_item_id, candidate_url)
へ張り替え、ebay_item_id を NOT NULL 化する。
背景: sku は listing 一意キーに使えない (sku-rules.md = listing 識別は ebay_item_id)。
旧 UNIQUE(sku, candidate_url) は同一 listing が別 sku を持つと dedup を取り違える。

Q2 準拠:
- init_db 内で RECREATE しない (DROP/DELETE 禁止) ため本 one-shot で実施。
- 旧 table は supplier_candidates_old_w185 として保持 (rollback 用 backup, DROP しない)。
- DDL+DML を 1 トランザクションで atomic に適用 (途中失敗で rollback)。

dedup ルール:
- (ebay_item_id, candidate_url) 重複は status 優先度 applied>accepted>pending>rejected、
  同順位は id ASC で 1 行に集約。重複行は同一 candidate_url = 同一仕入先判断のため、
  どれを残しても仕入先 URL は不変 (最も進んだ status の行を残し user_action_at を保全)。

冪等性:
- 既に UNIQUE(ebay_item_id, candidate_url) が適用済なら no-op で抜ける。

使い方:
    python scripts/migrate_supplier_candidates_v56.py            # dry-run (既定, 書込なし)
    python scripts/migrate_supplier_candidates_v56.py --apply    # 実行 (本番書込)

--apply 前に kill switch 推奨: supplier_sweep / supplier_candidate_search が同 table へ
書込中だと RENAME で SQLITE_BUSY (busy_timeout=5000ms 超過) → abort する。実行前に
scheduler 停止 or tasks_enabled.supplier_sweep.enabled=false / 同 candidate_search を
false にし、書込タスクが走っていない時間帯に実施すること。

本 script は Q2 6-step の一部。実行前に user 承認 + DB snapshot、実行後 24h
retrospective code-reviewer を必須とする。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402

OLD_TABLE = "supplier_candidates_old_w185"

# 移行後の正準スキーマ (database.py init_db の CREATE + v5/v13/v21/v54 migration 全列、
# 計 23 列)。ebay_item_id を NOT NULL 化し UNIQUE を ebay_item_id ベースへ変更した点のみ差分。
NEW_TABLE_SQL = """
CREATE TABLE supplier_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    ebay_item_id TEXT NOT NULL,
    source_platform TEXT,
    candidate_url TEXT NOT NULL,
    candidate_price_jpy INTEGER,
    candidate_title TEXT,
    match_score INTEGER,
    match_reasoning TEXT,
    profit_jpy REAL,
    profitable INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    user_action_at TIMESTAMP,
    discovered_via TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    junk_likely_untested INTEGER DEFAULT 0,
    alt_listing_possible INTEGER DEFAULT 0,
    alt_listing_note TEXT,
    auto_rejected INTEGER DEFAULT 0,
    eval_model TEXT,
    availability_status TEXT,
    availability_checked_at TIMESTAMP,
    availability_signal TEXT,
    UNIQUE(ebay_item_id, candidate_url)
)
"""

COLS = (
    "id, sku, ebay_item_id, source_platform, candidate_url, candidate_price_jpy, "
    "candidate_title, match_score, match_reasoning, profit_jpy, profitable, status, "
    "user_action_at, discovered_via, created_at, junk_likely_untested, "
    "alt_listing_possible, alt_listing_note, auto_rejected, eval_model, "
    "availability_status, availability_checked_at, availability_signal"
)

DEDUP_INSERT = f"""
INSERT INTO supplier_candidates ({COLS})
SELECT {COLS} FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY ebay_item_id, candidate_url
        ORDER BY CASE status
            WHEN 'applied' THEN 0
            WHEN 'accepted' THEN 1
            WHEN 'pending' THEN 2
            WHEN 'rejected' THEN 3
            ELSE 9 END,
            id ASC
    ) AS _rn
    FROM {OLD_TABLE}
)
WHERE _rn = 1
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _unique_col_sets(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """table の UNIQUE 制約由来 autoindex ごとの列名リストを返す。"""
    sets: list[list[str]] = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name, origin = idx[1], idx[3]  # (seq, name, unique, origin, partial)
        if str(name).startswith("sqlite_autoindex") and origin == "u":
            cols = [
                r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            if cols:
                sets.append(cols)
    return sets


def _has_new_unique(conn: sqlite3.Connection) -> bool:
    # 厳密な列セット一致で判定 (先頭列のみ一致の誤検知を避ける)。
    return ["ebay_item_id", "candidate_url"] in _unique_col_sets(
        conn, "supplier_candidates"
    )


def _preflight(conn: sqlite3.Connection) -> tuple[bool, str]:
    """abort 条件を検査。問題があれば (False, reason)。"""
    null_eid = conn.execute(
        "SELECT COUNT(*) FROM supplier_candidates "
        "WHERE ebay_item_id IS NULL OR TRIM(ebay_item_id)=''"
    ).fetchone()[0]
    if null_eid:
        return False, f"ebay_item_id が NULL/空白のみ の行 {null_eid} 件 (NOT NULL 化不可)"
    applied_conflict = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM supplier_candidates "
        "WHERE status='applied' AND ebay_item_id IS NOT NULL AND ebay_item_id!='' "
        "GROUP BY ebay_item_id, candidate_url HAVING COUNT(*)>1)"
    ).fetchone()[0]
    if applied_conflict:
        return False, (
            f"applied が同一 (ebay_item_id, candidate_url) で複数のグループ "
            f"{applied_conflict} 件 (人手判断衝突 = 自動 dedup 不可)"
        )
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="W185 supplier_candidates UNIQUE 張り替え")
    ap.add_argument(
        "--apply", action="store_true", help="実際に書き込む (既定は dry-run)"
    )
    args = ap.parse_args()

    conn = get_conn()
    conn.isolation_level = None  # 明示 BEGIN/COMMIT で DDL+DML を atomic に扱う
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]

        # 冪等: 既に新 UNIQUE 適用済なら no-op (user_version だけ補正)。
        if _has_new_unique(conn):
            print(
                f"[skip] 既に UNIQUE(ebay_item_id, candidate_url) 適用済 "
                f"(user_version={ver})。no-op。"
            )
            if ver < 56:
                if args.apply:
                    conn.execute("PRAGMA user_version = 56")
                    print("[fix] user_version を 56 に補正。")
                else:
                    print("[dry-run] user_version<56。--apply 時に 56 へ補正予定。")
            return 0

        # 不整合検知: backup table が既存なのに新 UNIQUE 未適用 = 過去 run の中途状態。
        if _table_exists(conn, OLD_TABLE):
            print(
                f"[ABORT] {OLD_TABLE} が既存だが新 UNIQUE 未適用。"
                "中途状態の可能性。手動確認してください。"
            )
            return 1

        # preflight + 件数算出 + 移行を 1 つの write transaction に収め、
        # 検査と migration の間に並行 writer が状態を変える race を排除する
        # (BEGIN IMMEDIATE で即 RESERVED lock を取得)。
        conn.execute("BEGIN IMMEDIATE")
        ok, reason = _preflight(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0]
        groups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM supplier_candidates "
            "GROUP BY ebay_item_id, candidate_url)"
        ).fetchone()[0]
        print(
            f"対象: {before} 行 → dedup 後 {groups} 行 (削減 {before - groups} 行)"
        )
        if not ok:
            conn.execute("ROLLBACK")
            print(f"[ABORT] preflight 失敗: {reason}")
            return 1

        if not args.apply:
            conn.execute("ROLLBACK")  # 読み取りのみ、lock を解放
            print(
                "[dry-run] --apply 未指定のため書込なし。"
                "上記 plan を確認後 --apply で実行してください。"
            )
            return 0

        # RENAME で named index は OLD_TABLE 側へ移動し名前を保持するため、
        # 新 table 用に名前を解放してから再作成する (autoindex は自動リネームされ衝突しない)。
        conn.execute(
            f"ALTER TABLE supplier_candidates RENAME TO {OLD_TABLE}"
        )
        conn.execute("DROP INDEX IF EXISTS idx_supplier_candidates_sku")
        conn.execute("DROP INDEX IF EXISTS idx_supplier_candidates_status")
        conn.execute(NEW_TABLE_SQL)
        conn.execute(
            "CREATE INDEX idx_supplier_candidates_sku "
            "ON supplier_candidates (sku)"
        )
        conn.execute(
            "CREATE INDEX idx_supplier_candidates_status "
            "ON supplier_candidates (status, match_score DESC)"
        )
        conn.execute(DEDUP_INSERT)
        after = conn.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0]
        if after != groups:
            conn.execute("ROLLBACK")
            print(f"[ABORT] 移行後行数 {after} != 期待 {groups}。rollback。")
            return 1
        conn.execute("PRAGMA user_version = 56")
        conn.execute("COMMIT")

        if not _has_new_unique(conn):
            print("[ERROR] COMMIT 後に新 UNIQUE 未検出。手動確認要。")
            return 1
        new_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        print(
            f"[OK] 移行完了: {after} 行。backup={OLD_TABLE} ({before} 行) 保持。"
            f" user_version={new_ver}。"
        )
        print(
            "[verify] UNIQUE(ebay_item_id, candidate_url) 適用確認。"
            "Q2: 24h 以内に retrospective code-reviewer を実施してください。"
        )
        return 0
    except sqlite3.Error as e:
        # OperationalError 以外 (IntegrityError 等、DEDUP_INSERT 由来含む) も
        # 必ず rollback してから再送出する (Q0: 握り潰し禁止)。
        if conn.in_transaction:
            conn.execute("ROLLBACK")
            print("[rollback] トランザクションを巻き戻しました。")
        print(f"[ERROR] {type(e).__name__}: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
