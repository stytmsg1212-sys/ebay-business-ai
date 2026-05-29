#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139 live blind-spot 即時解消: 本番 run_ensure_monitor_coverage を 1 回実行.

これは hack ではなく W139 が 02:30 / main batch で回す**そのもの**の関数。
冪等・upsert_item 経由 (sanctioned)・単一 coverable のみ。23:00 health-check が
ERROR で検知した active 無在庫 blind-spot を翌 02:30 まで放置しない (Q0 + money
直結 履行不能注文 risk)。before/after snapshot で監査可能化 (Q2 spirit)。
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
os.chdir(PROJ)
sys.path.insert(0, PROJ)
from monitor.database import get_conn  # noqa: E402
from tasks.task_ensure_monitor_coverage import (  # noqa: E402
    find_coverage_gaps, run_ensure_monitor_coverage,
)


def snap(tag):
    with get_conn() as c:
        c.row_factory = sqlite3.Row
        tot = c.execute("SELECT COUNT(*) FROM monitored_items").fetchone()[0]
        hit = c.execute(
            "SELECT id, sku FROM monitored_items "
            "WHERE sku='ebayme_25388296384'").fetchall()
    g = find_coverage_gaps()
    print(f"[{tag}] monitored_items 総数={tot} / "
          f"ebayme_25388296384 行={[dict(r) for r in hit]} / "
          f"coverable={len(g['coverable'])} dlq={len(g['dlq'])}")
    return tot


print("=== BEFORE ===")
before = snap("before")

print("\n=== run_ensure_monitor_coverage({}) 実行 ===")
res = run_ensure_monitor_coverage({})
print("result:", res)

print("\n=== AFTER ===")
after = snap("after")

print(f"\nΔ monitored_items = {after - before:+d}")
print("冪等再実行 (registered=0 を確認):")
res2 = run_ensure_monitor_coverage({})
print("result(2):", {k: res2[k] for k in
                      ("success", "scanned", "registered", "failed", "dlq")})
