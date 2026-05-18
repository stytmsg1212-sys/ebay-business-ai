#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139: backfill 済 20 件の仕入先在庫を実スクレイプし履行不能リスクを報告.

backfill_monitored_coverage_detect_2026_05_18.py で monitored_items 登録は
完了済 (本番コミット済)。本スクリプトは **登録しない**。登録済 listing を
プロジェクト正準 helper (_build_results + _JP_TO_STATS_KEY) で実検証し、
qty>=1 ∧ (在庫無 | ページなし | 不明) = 履行不能リスクを報告するのみ。
policy B: 自動販売停止は一切しない (確認キュー方式、報告 → 人手対応)。
Q0: 不明は「在庫有」と誤断定せず「要手動確認」として明示。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import get_active_items, get_site_configs  # noqa: E402
from monitor.scrapers import prepare_batch_items, check_items_batch  # noqa: E402
from tasks.task_inventory_check import _build_results, _JP_TO_STATS_KEY  # noqa: E402


def main() -> None:
    # 最新 snapshot から対象 21 件 (ebay_item_id/qty/title) を読む
    snaps = sorted(glob.glob(str(BASE / "data" / "backups"
                                  / "w139_unmonitored_snapshot_*.json")))
    snap = json.loads(Path(snaps[-1]).read_text(encoding="utf-8"))
    rows = snap["unmonitored_listings"]
    by_eid = {r["ebay_item_id"]: r for r in rows}
    target_skus = {r["sku"] for r in rows}
    print(f"[snapshot] {snaps[-1]} ({len(rows)} 件)")

    configs_by_prefix = {c["convert_url"]: c for c in get_site_configs()}
    items = [it for it in get_active_items() if it.get("sku") in target_skus]
    batch = prepare_batch_items(items, configs_by_prefix)
    print(f"[scrape] 対象 {len(items)} 件中 batch化 {len(batch)} 件 "
          f"(差は URL生成不能/prefix未登録 = DLQ)")
    raw = check_items_batch(batch) if batch else {}
    results = _build_results(items, raw, configs_by_prefix)

    danger, ok, ambiguous = [], [], []
    for r in results:
        eid = r.get("ebay_id")
        src = by_eid.get(eid, {})
        qty = src.get("quantity_ebay", "?")
        cat = _JP_TO_STATS_KEY.get(r.get("status", "不明"), "error")
        rec = {"eid": eid, "sku": r.get("sku"), "qty": qty,
               "status": r.get("status"), "cat": cat,
               "title": (src.get("title") or "")[:55]}
        if cat in ("out_of_stock", "page_not_found") and \
                isinstance(qty, int) and qty >= 1:
            danger.append(rec)
        elif cat == "in_stock":
            ok.append(rec)
        else:
            ambiguous.append(rec)

    scraped_eids = {r.get("ebay_id") for r in results}
    dlq = [r for r in rows if r["ebay_item_id"] not in scraped_eids]

    print("\n" + "=" * 70)
    print("★【最優先=履行不能リスク】仕入先 在庫無/ページ消失 ∧ eBay qty>=1")
    print("  (policy B: 自動停止しない → 下記は user 手動対応が必要)")
    print("=" * 70)
    for d in sorted(danger, key=lambda x: -(x["qty"] if isinstance(x["qty"], int) else 0)):
        print(f"  ★ {d['eid']} qty={d['qty']} [{d['status']}] {d['sku']}")
        print(f"      {d['title']}")
    if not danger:
        print("  (なし)")

    print(f"\n?【要手動確認=判定不能/不明】{len(ambiguous)} 件 "
          "(Q0: 在庫有と誤断定しない)")
    for a in ambiguous:
        print(f"  ? {a['eid']} qty={a['qty']} [{a['status']}] {a['sku']} | "
              f"{a['title']}")

    print(f"\n○【在庫有=当面安全】{len(ok)} 件")
    for o in ok:
        print(f"  ○ {o['eid']} qty={o['qty']} {o['sku']} | {o['title']}")

    print(f"\n!【DLQ=URL生成不能 (site_config prefix 未登録)、登録不可】"
          f"{len(dlq)} 件")
    for u in dlq:
        print(f"  ! {u['ebay_item_id']} qty={u['quantity_ebay']} "
              f"{u['sku']} | {u['title'][:55]}")

    print("\n" + "=" * 70)
    print(f"集計: 履行不能リスク={len(danger)} / 要手動確認={len(ambiguous)} "
          f"/ 在庫有={len(ok)} / DLQ={len(dlq)}")


if __name__ == "__main__":
    main()
