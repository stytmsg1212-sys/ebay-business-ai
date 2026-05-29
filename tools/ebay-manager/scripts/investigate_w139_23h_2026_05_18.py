#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""READ-ONLY 調査: 5/18 23:00 health-check の W139 未登録 1 件
(ebayme_25388296384) の根本原因 + thread-local 02:30 batch Q1 DoD 検証.

SELECT と side-effect-free な find_coverage_gaps() のみ。書込なし。
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)  # tools/ebay-manager
os.chdir(PROJ)
sys.path.insert(0, PROJ)

from monitor.database import (  # noqa: E402
    get_conn, build_source_url, find_site_config_by_sku,
)

TARGET = "25388296384"
print("=" * 72)
print("(1) ebay_listings: 25388296384 を含む行")
print("=" * 72)
with get_conn() as conn:
    conn.row_factory = sqlite3.Row
    lcols = [d[1] for d in conn.execute(
        "PRAGMA table_info(ebay_listings)").fetchall()]
    wanted = [c for c in (
        "ebay_item_id", "sku", "title", "is_ended", "quantity_ebay",
        "created_at", "updated_at", "last_synced_at", "ended_at",
        "status", "listing_status") if c in lcols]
    sel_l = ", ".join(wanted)
    order_l = "created_at" if "created_at" in lcols else "ebay_item_id"
    rows = conn.execute(
        f"""SELECT {sel_l}
           FROM ebay_listings
           WHERE ebay_item_id LIKE ? OR sku LIKE ?
           ORDER BY {order_l}""",
        (f"%{TARGET}%", f"%{TARGET}%"),
    ).fetchall()
    for r in rows:
        print(dict(r))
    if not rows:
        print("  (該当なし)")

    print()
    print("=" * 72)
    print("(2) monitored_items: 同 sku / ebay_item_id")
    print("=" * 72)
    cols = [d[1] for d in conn.execute(
        "PRAGMA table_info(monitored_items)").fetchall()]
    has_created = "created_at" in cols
    sel = "id, sku" + (", created_at" if has_created else "")
    # ebay_item_id 列があるか
    if "ebay_item_id" in cols:
        sel += ", ebay_item_id"
    mrows = conn.execute(
        f"SELECT {sel} FROM monitored_items WHERE sku LIKE ?",
        (f"%{TARGET}%",),
    ).fetchall()
    for r in mrows:
        print(dict(r))
    if not mrows:
        print("  (monitored_items に該当 sku なし = 未登録 確定)")

    print()
    tot = conn.execute("SELECT COUNT(*) FROM monitored_items").fetchone()[0]
    print(f"  monitored_items 総数 = {tot}")

print()
print("=" * 72)
print("(3) find_coverage_gaps() を今 実行 (Component A/B 共有判定)")
print("=" * 72)
from tasks.task_ensure_monitor_coverage import find_coverage_gaps  # noqa: E402

gaps = find_coverage_gaps()
print(f"  coverable = {len(gaps['coverable'])} 件")
for c in gaps["coverable"]:
    print("   [COVERABLE]", c)
print(f"  dlq = {len(gaps['dlq'])} 件")
for d in gaps["dlq"]:
    print("   [DLQ]", d)

print()
print("=" * 72)
print("(4) 対象 sku の URL/site_config 生成可否")
print("=" * 72)
for r in rows:
    sku = r["sku"]
    try:
        url = build_source_url(sku)
    except Exception as e:  # noqa: BLE001
        url = f"<EXC {e}>"
    cfg = find_site_config_by_sku(sku)
    print(f"  sku={sku!r} build_source_url={url!r} "
          f"site_config={'OK' if cfg else 'None'}")

print()
print("=" * 72)
print("(5) task_execution_log: 5/18 thread-local Q1 DoD (直近 30h, JST 安全)")
print("=" * 72)
WATCH = (
    "daily_relist", "enrich_listings_physical", "estimate_weights_claude",
    "cleanup_old_relisted", "research_morning_brief", "ensure_monitor_coverage",
)
with get_conn() as conn:
    conn.row_factory = sqlite3.Row
    tcols = [d[1] for d in conn.execute(
        "PRAGMA table_info(task_execution_log)").fetchall()]
    print(f"  task_execution_log columns = {tcols}")
    note_col = next(
        (c for c in ("detail", "note", "message", "extra", "info")
         if c in tcols), None)
    base = "task_key, status, started_at, finished_at"
    if "batch_id" in tcols:
        base += ", batch_id"
    if note_col:
        base += f", {note_col}"
    qmarks = ",".join("?" * len(WATCH))
    erows = conn.execute(
        f"""SELECT {base} FROM task_execution_log
            WHERE task_key IN ({qmarks})
              AND started_at >= datetime('now','-30 hours')
            ORDER BY started_at""",
        WATCH,
    ).fetchall()
    for r in erows:
        print("  ", dict(r))
    if not erows:
        print("  (直近 30h に該当タスク行なし)")
