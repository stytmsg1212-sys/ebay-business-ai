#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W227 棚卸し (read-only): 全 active 出品を eBay GetItem し、現在の実 Condition を
集計。Open Box(1500) になっている listing = 過去の rank 二重使用 push で誤上書き
された疑いがあるため一覧化する。eBay へは一切書き込まない (GetItem のみ)。
出力: data/w227_condition_audit.json + 標準出力に進捗/サマリ。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.credentials import get_ebay_credentials  # noqa: E402
from monitor.ebay_listing_snapshot import fetch_listing_snapshot  # noqa: E402

_DB = _ROOT / "data" / "monitor.db"
_OUT = _ROOT / "data" / "w227_condition_audit.json"

# eBay ConditionID → 表示名
_CID_NAME = {
    "1000": "New", "1500": "Open Box (S)", "2000": "Manufacturer refurb",
    "2500": "Seller refurb", "3000": "Used", "7000": "For parts/As-Is",
}


def main() -> None:
    cr = get_ebay_credentials({})
    app, dev, cert, tok = (
        cr.get("app_id"), cr.get("dev_id"), cr.get("cert_id"), cr.get("user_token"),
    )
    if not all([app, dev, cert, tok]):
        print("ERROR: eBay credentials 不在")
        return

    conn = sqlite3.connect(str(_DB))
    rows = conn.execute(
        "SELECT ebay_item_id, COALESCE(rank,''), COALESCE(title,''), current_price "
        "FROM ebay_listings WHERE (is_ended IS NULL OR is_ended=0) "
        "AND title IS NOT NULL AND title!='' ORDER BY ebay_item_id"
    ).fetchall()
    conn.close()

    total = len(rows)
    print(f"START W227 audit: {total} active listings (GetItem only, no writes)")
    dist: Counter = Counter()
    openbox: list[dict] = []
    parts: list[dict] = []     # 7000 (As-Is) も併記 (誤push候補)
    errors = 0
    results: list[dict] = []

    for i, (eid, rank, title, price) in enumerate(rows, 1):
        cid = None
        try:
            snap = fetch_listing_snapshot(eid, app, dev, cert, tok)
            if snap.ok:
                cid = getattr(snap, "condition_id", None) or "(none)"
            else:
                cid = "ERR"
                errors += 1
        except Exception as e:  # noqa: BLE001
            cid = "EXC"
            errors += 1
        rec = {
            "eid": eid, "condition_id": cid,
            "condition_name": _CID_NAME.get(str(cid), str(cid)),
            "db_rank": rank, "title": title[:60], "price": price,
        }
        results.append(rec)
        dist[cid] += 1
        if cid == "1500":
            openbox.append(rec)
        if cid == "7000":
            parts.append(rec)
        if i % 25 == 0 or i == total:
            print(f"[{i}/{total}] dist={dict(dist)} openbox={len(openbox)} "
                  f"asis={len(parts)} err={errors}", flush=True)

    out = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "condition_distribution": dict(dist),
        "condition_distribution_named": {
            _CID_NAME.get(str(k), str(k)): v for k, v in dist.items()
        },
        "openbox_1500_count": len(openbox),
        "openbox_1500": openbox,
        "asis_7000_count": len(parts),
        "asis_7000": parts,
        "errors": errors,
        "all": results,
    }
    _OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("=" * 60)
    print(f"DONE. condition 分布: {dict(dist)}")
    print(f"Open Box(1500) = {len(openbox)} 件 / As-Is(7000) = {len(parts)} 件 / err={errors}")
    print(f"出力: {_OUT}")
    print("--- Open Box(1500) 一覧 (誤push疑い) ---")
    for r in openbox:
        print(f"  {r['eid']} | db_rank={r['db_rank']:3} | ${r['price']} | {r['title']}")


if __name__ == "__main__":
    main()
