"""W72 one-shot migration v28: monitored_items.UNIQUE(sku) 撤廃.

前提:
  - PRAGMA user_version = 27 (W50 ebayyh_ seed 完了済)
  - SKU rule (.claude/rules/sku-rules.md) 違反 = UNIQUE(sku) を撤廃するため
    監視テーブルを RECREATE する.

冪等:
  - PRAGMA user_version >= 28 → no-op
  - sqlite_autoindex 不在 (UNIQUE 撤廃済) でも user_version setter のみで完結
  - tx 自動 rollback で部分実行残骸を防ぐ

実行:
  cd tools/ebay-manager
  python scripts/migrate_monitored_items_v28.py

事故対応:
  失敗時は data/monitor.db.backup_w72_*.db からリストア.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn


def _has_unique_on_sku(conn: sqlite3.Connection) -> bool:
    """monitored_items に sqlite_autoindex (UNIQUE 由来) が残っているかを判定.

    monitored_items の PRIMARY KEY (id) は AUTOINCREMENT で autoindex を作らないため
    autoindex 検出 = sku UNIQUE 制約由来と特定できる.
    """
    for r in conn.execute("PRAGMA index_list(monitored_items)").fetchall():
        if r[1].startswith("sqlite_autoindex"):
            return True
    return False


def _recreate_without_unique(conn: sqlite3.Connection) -> int:
    """monitored_items を UNIQUE 無しで RECREATE. 全 row 保持. Returns 移行 row 数."""
    conn.execute("""
        CREATE TABLE monitored_items_v28 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT,
            title TEXT,
            sku TEXT NOT NULL,
            source_url TEXT,
            site_config_id INTEGER,
            is_active INTEGER DEFAULT 1,
            last_status TEXT DEFAULT 'unknown',
            last_check TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (site_config_id) REFERENCES site_configs(id)
        )
    """)

    # id 保持で INSERT (check_log.item_id FK 互換性確保).
    moved = conn.execute("""
        INSERT INTO monitored_items_v28
            (id, ebay_item_id, title, sku, source_url, site_config_id,
             is_active, last_status, last_check, created_at)
        SELECT id, ebay_item_id, title, sku, source_url, site_config_id,
               is_active, last_status, last_check, created_at
        FROM monitored_items
    """).rowcount

    conn.execute("DROP TABLE monitored_items")
    conn.execute("ALTER TABLE monitored_items_v28 RENAME TO monitored_items")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_monitored_active "
        "ON monitored_items(is_active)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_monitored_ebay_id "
        "ON monitored_items(ebay_item_id) "
        "WHERE ebay_item_id IS NOT NULL AND ebay_item_id != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_monitored_source_url "
        "ON monitored_items(source_url) "
        "WHERE source_url IS NOT NULL AND source_url != ''"
    )
    return moved


def main() -> int:
    with get_conn() as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver >= 28:
            print(f"既に user_version={ver} (>= 28) → migration skip.")
            return 0
        if ver != 27:
            print(
                f"WARN: user_version={ver} (expected 27). 中断.",
                file=sys.stderr,
            )
            return 2

        before_n = conn.execute(
            "SELECT COUNT(*) FROM monitored_items"
        ).fetchone()[0]

        if not _has_unique_on_sku(conn):
            print("UNIQUE(sku) 既に撤廃済 → user_version=28 setter のみ.")
            conn.execute("PRAGMA user_version = 28")
            return 0

        print(f"RECREATE 開始: {before_n} 行を保持して UNIQUE(sku) 撤廃.")
        moved = _recreate_without_unique(conn)
        after_n = conn.execute(
            "SELECT COUNT(*) FROM monitored_items"
        ).fetchone()[0]

        if moved != before_n or after_n != before_n:
            raise RuntimeError(
                f"row 数不一致 (before={before_n} moved={moved} after={after_n})"
            )

        if _has_unique_on_sku(conn):
            raise RuntimeError("UNIQUE(sku) 撤廃失敗")

        conn.execute("PRAGMA user_version = 28")
        print(
            f"完了: monitored_items {before_n} 行保持, UNIQUE(sku) 撤廃, user_version=28."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
