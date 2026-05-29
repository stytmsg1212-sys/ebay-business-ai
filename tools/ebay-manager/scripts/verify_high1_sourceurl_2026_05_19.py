#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""READ-ONLY: HIGH-1 実証 — build_source_url vs _build_source_url_from_sku の
生成 URL 差、および本番 ebay_listings.source_url / monitored_items.source_url
の実値を実 SKU で突合."""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)
from monitor.database import (  # noqa: E402
    get_conn, build_source_url, _build_source_url_from_sku,
)

print("=== 生成器2系統の実出力比較 (実 SKU) ===")
with get_conn() as c:
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT ebay_item_id, sku, source_url FROM ebay_listings "
        "WHERE sku LIKE 'ebay%' AND COALESCE(is_ended,0)=0 "
        "ORDER BY RANDOM() LIMIT 12"
    ).fetchall()
    mism = 0
    for r in rows:
        sku = r["sku"]
        try:
            bu = build_source_url(sku)
        except Exception as e:  # noqa: BLE001
            bu = f"<EXC {e}>"
        try:
            fs = _build_source_url_from_sku(sku)
        except Exception as e:  # noqa: BLE001
            fs = f"<EXC {e}>"
        ls = r["source_url"]
        match_bu = (bu == ls)
        match_fs = (fs == ls)
        if not match_bu or bu != fs:
            mism += 1
        print(f"\nsku={sku}")
        print(f"  build_source_url        = {bu}")
        print(f"  _build_source_url_from_sku = {fs}")
        print(f"  ebay_listings.source_url   = {ls}")
        print(f"  listing==build? {match_bu} / listing==from_sku? {match_fs} "
              f"/ build==from_sku? {bu == fs}")

print(f"\n=== {len(rows)} 件中 不一致 {mism} 件 ===")

print("\n=== monitored_items.source_url の生成元実態 (sample) ===")
with get_conn() as c:
    c.row_factory = sqlite3.Row
    for r in c.execute(
        "SELECT id, ebay_item_id, sku, source_url FROM monitored_items "
        "WHERE sku LIKE 'ebay%' ORDER BY RANDOM() LIMIT 8"
    ).fetchall():
        sku = r["sku"]
        bu = build_source_url(sku) if sku else None
        fs = _build_source_url_from_sku(sku) if sku else None
        print(f"  id={r['id']} sku={sku} mon.url={r['source_url']}")
        print(f"     build={bu} from_sku={fs} "
              f"mon==build? {r['source_url']==bu} "
              f"mon==from_sku? {r['source_url']==fs}")

print("\n=== GLSSWRKS (356593353626) 実値 ===")
with get_conn() as c:
    c.row_factory = sqlite3.Row
    l = c.execute("SELECT sku, source_url FROM ebay_listings "
                  "WHERE ebay_item_id='356593353626'").fetchone()
    m = c.execute("SELECT id, sku, source_url FROM monitored_items "
                  "WHERE ebay_item_id='356593353626'").fetchone()
    print(f"  ebay_listings: {dict(l) if l else None}")
    print(f"  monitored_items: {dict(m) if m else None}")
    if l:
        print(f"  build_source_url({l['sku']}) = {build_source_url(l['sku'])}")
        print(f"  _build_source_url_from_sku({l['sku']}) = "
              f"{_build_source_url_from_sku(l['sku'])}")
