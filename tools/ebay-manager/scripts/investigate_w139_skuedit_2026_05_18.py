#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""READ-ONLY: 根本原因の確証 — SKU編集で monitored_items が
ebay_item_id 同一・sku 不一致の重複/陳腐化を起こしているか実証."""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
os.chdir(PROJ)
sys.path.insert(0, PROJ)
from monitor.database import get_conn  # noqa: E402

with get_conn() as conn:
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print("(A) 私の remediation が触れた 3 listing の ebay_listings vs monitored")
    print("=" * 72)
    for eid in ("356593353626",):  # GLSSWRKS (23:00 health-check 検出分)
        lr = conn.execute(
            "SELECT ebay_item_id, sku, last_synced_at FROM ebay_listings "
            "WHERE ebay_item_id=?", (eid,)).fetchone()
        print(f"ebay_listings[{eid}] = {dict(lr) if lr else None}")
        mr = conn.execute(
            "SELECT id, sku, ebay_item_id, source_url, created_at "
            "FROM monitored_items WHERE ebay_item_id=?", (eid,)).fetchall()
        print(f"  monitored_items WHERE ebay_item_id={eid}: {len(mr)} 行")
        for r in mr:
            print("   ", dict(r))

    print()
    print("=" * 72)
    print("(B) monitored_items.ebay_item_id が NULL/空 の件数 "
          "(upsert_item の ebay_item_id 識別が効かない母数)")
    print("=" * 72)
    tot = conn.execute("SELECT COUNT(*) FROM monitored_items").fetchone()[0]
    nullc = conn.execute(
        "SELECT COUNT(*) FROM monitored_items "
        "WHERE ebay_item_id IS NULL OR ebay_item_id=''").fetchone()[0]
    print(f"  monitored_items 総数={tot} / ebay_item_id NULL or '' = {nullc}")

    print()
    print("=" * 72)
    print("(C) 同一 ebay_item_id に複数 monitored 行 (SKU編集由来の重複候補)")
    print("=" * 72)
    dups = conn.execute(
        """SELECT ebay_item_id, COUNT(*) n,
                  GROUP_CONCAT(id) ids, GROUP_CONCAT(sku) skus
           FROM monitored_items
           WHERE ebay_item_id IS NOT NULL AND ebay_item_id<>''
           GROUP BY ebay_item_id HAVING n>1
           ORDER BY n DESC LIMIT 20""").fetchall()
    print(f"  重複 ebay_item_id = {len(dups)} 種")
    for d in dups:
        print("   ", dict(d))

    print()
    print("=" * 72)
    print("(D) sku 不一致: ebay_listings と monitored を ebay_item_id で突合し "
          "sku がズレてる active 無在庫 (= 誤検知 phantom gap の母数)")
    print("=" * 72)
    mism = conn.execute(
        """SELECT l.ebay_item_id, l.sku AS listing_sku, m.sku AS monitored_sku,
                  l.last_synced_at
           FROM ebay_listings l
           JOIN monitored_items m ON m.ebay_item_id = l.ebay_item_id
           WHERE COALESCE(l.is_ended,0)=0
             AND (l.quantity_ebay IS NULL OR l.quantity_ebay>=1)
             AND l.sku LIKE 'ebay%'
             AND l.sku <> m.sku
           ORDER BY l.last_synced_at DESC LIMIT 30""").fetchall()
    print(f"  ebay_item_id 一致だが sku ズレ (active 無在庫) = {len(mism)} 件")
    for r in mism:
        print("   ", dict(r))
