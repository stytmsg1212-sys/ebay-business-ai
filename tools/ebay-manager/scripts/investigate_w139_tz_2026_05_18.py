#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""READ-ONLY: last_synced_at の TZ 確定 + GLSSWRKS 行の sync 鮮度を peers と比較."""
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
os.chdir(PROJ)
sys.path.insert(0, PROJ)
from monitor.database import get_conn  # noqa: E402

now_utc = datetime.now(timezone.utc)
now_jst = now_utc.astimezone(timezone(timedelta(hours=9)))
print(f"now UTC = {now_utc:%Y-%m-%d %H:%M:%S}")
print(f"now JST = {now_jst:%Y-%m-%d %H:%M:%S}")
print()

with get_conn() as conn:
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT MIN(last_synced_at) mn, MAX(last_synced_at) mx, "
        "COUNT(*) n FROM ebay_listings WHERE last_synced_at IS NOT NULL"
    ).fetchone()
    print(f"ebay_listings.last_synced_at  MIN={r['mn']}  MAX={r['mx']}  "
          f"n={r['n']}")
    print("  → MAX が now JST 付近なら TZ=JST、now UTC 付近なら TZ=UTC")
    print()

    # 22:00 batch (22:07 完了) 以降に synced された件数 (両 TZ 解釈)
    for label, thr in (
        ("JST解釈: >= '2026-05-18 22:07'", "2026-05-18 22:07:00"),
        ("UTC解釈: >= '2026-05-18 13:07' (=22:07 JST)", "2026-05-18 13:07:00"),
    ):
        c = conn.execute(
            "SELECT COUNT(*) FROM ebay_listings WHERE last_synced_at >= ?",
            (thr,),
        ).fetchone()[0]
        print(f"  {label}  → {c} 件 synced")
    print()

    # GLSSWRKS 行の last_synced を全体の鮮度分布で位置づけ
    g = conn.execute(
        "SELECT last_synced_at FROM ebay_listings "
        "WHERE ebay_item_id='356593353626'"
    ).fetchone()
    print(f"GLSSWRKS(356593353626) last_synced_at = {g['last_synced_at']}")
    newer = conn.execute(
        "SELECT COUNT(*) FROM ebay_listings WHERE last_synced_at > ?",
        (g["last_synced_at"],),
    ).fetchone()[0]
    older = conn.execute(
        "SELECT COUNT(*) FROM ebay_listings WHERE last_synced_at < ?",
        (g["last_synced_at"],),
    ).fetchone()[0]
    print(f"  この行より new な listing = {newer} / old = {older}")

    # 22:00 batch で sync された listing の last_synced サンプル (最大10)
    print()
    print("最近 synced の上位10 (last_synced_at desc):")
    for x in conn.execute(
        "SELECT ebay_item_id, sku, last_synced_at FROM ebay_listings "
        "ORDER BY last_synced_at DESC LIMIT 10"
    ).fetchall():
        print("  ", dict(x))
