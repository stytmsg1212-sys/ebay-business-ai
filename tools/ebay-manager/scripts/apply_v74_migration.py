# -*- coding: utf-8 -*-
"""v74 migration 適用 + 冪等性 verify (依頼ボード#17 HIGH-1 / Q2).

init_db を 2 回連続実行し、列追加 + user_version=74 + データ保持を確認する。
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import init_db, get_conn  # noqa: E402

db = BASE / "data" / "monitor.db"
conn = sqlite3.connect(str(db))
before_ver = conn.execute("PRAGMA user_version").fetchone()[0]
before_rows = conn.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
conn.close()
print(f"before: user_version={before_ver}, ebay_listings={before_rows} rows")

init_db()
init_db()  # 冪等性: 2 回連続実行

conn = sqlite3.connect(str(db))
after_ver = conn.execute("PRAGMA user_version").fetchone()[0]
after_rows = conn.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
cols = {r[1] for r in conn.execute("PRAGMA table_info(ebay_listings)")}
conn.close()

print(f"after : user_version={after_ver}, ebay_listings={after_rows} rows")
print(f"last_supplier_search_at column: {'last_supplier_search_at' in cols}")
assert after_ver == 74, f"user_version expected 74, got {after_ver}"
assert "last_supplier_search_at" in cols, "column missing"
assert after_rows == before_rows, (
    f"row count changed {before_rows} -> {after_rows} (idempotency violation)"
)
print("OK: v74 applied, idempotent, data preserved")
