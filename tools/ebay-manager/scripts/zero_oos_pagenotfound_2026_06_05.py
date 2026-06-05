#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-06-05 緊急: 仕入先ページ消滅(ページなし)+ qty>=1 の無在庫出品を eBay在庫0化。

背景: 仕入先(Yahoo等)が売切=「ページなし」になっても eBay 在庫が自動0化されず、
履行不能な注文が発生した (item 358343669478 が今朝売れて仕入不可)。
本 one-shot は現在の危険在庫 (qty>=1 + source_status='ページなし' + ebay* SKU +
未退役 + 未確認) の eBay 在庫を 0 にして販売窓を即閉鎖する (user 承認済 2026-06-05)。

仕入先ページ消滅 = 仕入不可なので 0化に損失なし (完全に安全側)。
手順: 1件試行→成功確認→残り (リスク書込の段階実行)。各件ログ + 最終サマリ。
"""
from __future__ import annotations

import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from monitor.credentials import get_ebay_credentials
from monitor.database import get_conn, update_ebay_listing_quantity
from monitor.ebay_client import revise_inventory_quantity
from ui_cache import bump_db_version


def _targets() -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT ebay_item_id, sku, title, quantity_ebay, rank, source_status
               FROM ebay_listings
               WHERE quantity_ebay >= 1 AND COALESCE(is_ended,0)=0
                 AND sku GLOB 'ebay*' AND COALESCE(risk_confirmed,0)=0
                 AND source_status = 'ページなし'
               ORDER BY CASE rank WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2
                        WHEN 'C' THEN 3 WHEN 'D' THEN 4 WHEN 'E' THEN 5 ELSE 6 END"""
        ).fetchall()]


def main() -> int:
    creds = get_ebay_credentials({})
    if not all(creds.get(k) for k in ("app_id", "dev_id", "cert_id", "user_token")):
        print("[ABORT] eBay 認証情報が不足")
        return 2
    rows = _targets()
    print(f"[snapshot] 対象 {len(rows)} 件 (qty>=1 + ページなし):")
    for r in rows:
        print(f"  {r['ebay_item_id']} qty={r['quantity_ebay']} rank={r['rank']} | {(r['title'] or '')[:50]}")
    if not rows:
        print("対象なし")
        return 0

    ok, ng = 0, 0
    for i, r in enumerate(rows):
        eid = r["ebay_item_id"]
        res = revise_inventory_quantity(
            eid, 0, creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
        )
        if res.get("success"):
            update_ebay_listing_quantity(eid, 0)
            bump_db_version()
            ok += 1
            print(f"  [OK {i+1}/{len(rows)}] {eid} → qty 0")
        else:
            ng += 1
            print(f"  [NG {i+1}/{len(rows)}] {eid}: {res.get('message')}")
        # 1 件目が失敗したら中断 (creds/API 不調を全件で繰り返さない)
        if i == 0 and not res.get("success"):
            print("[ABORT] 1 件目が失敗。creds/API を確認。残りは中断。")
            return 3
    print(f"\n[summary] 成功 {ok} 件 / 失敗 {ng} 件")
    return 0 if ng == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
