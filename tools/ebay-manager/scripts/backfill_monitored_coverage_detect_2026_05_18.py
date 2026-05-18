#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139 緊急: 監視台帳 未登録の active 無在庫 listing を「検出のみ」バックフィル.

本番事故 (item 358487417178 が monitored_items 未登録で仕入先OOS検知不能 →
履行不能 US 注文 07-14655-19832) の暫定対応。

方針 (user 確定 2026-05-18):
  - policy B: OOS/判定不能でも **自動販売停止しない** (確認キュー方式、本スクリプトは
    検出と報告のみ。eBay qty 変更・停止は一切しない)。
  - 恒久対策 (ensure_monitor_coverage 定時タスク + 確認キュー UI) は W139 /feature-dev。

実施 (db-migration-rules 6-step 準拠):
  1. 対象を SELECT dump (snapshot, rollback 用) → data/backups/
  2. source_url 生成不能 (prefix 未登録) は登録せず flag
  3. upsert_item で monitored_items に登録 (ebay_item_id 主導 identify +
     source_url 単位集約 = Codex 落とし穴#4 回避。upsert_item 既存実装が担保)
  4. 登録した listing のみ対象に check_items_batch で実スクレイプ (full
     run_inventory_check は呼ばない = supplier 探索/Discord/22分 を誘発しない)
  5. qty>=1 ∧ (在庫無 | ページなし | 判定不能) = 履行不能リスクを report
  6. 実行後 24h 以内に retrospective code-reviewer (本スクリプト + 本番DB書込)

Q0: scraper が曖昧な応答なら「要手動確認」と報告 (在庫有 と誤断定しない)。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import (  # noqa: E402
    get_conn, build_source_url, find_site_config_by_sku,
    upsert_item, get_active_items, get_site_configs,
)
from monitor.scrapers import prepare_batch_items, check_items_batch  # noqa: E402

DRY = "--apply" not in sys.argv  # default dry-run; --apply で実書込


def _unmonitored_rows() -> list[dict]:
    with get_conn() as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute(
            """SELECT l.ebay_item_id, l.sku, l.title, l.quantity_ebay,
                      l.source_status, l.created_at
               FROM ebay_listings l
               WHERE l.is_ended=0 AND l.sku LIKE 'ebay%'
                 AND NOT EXISTS (SELECT 1 FROM monitored_items m
                                 WHERE m.sku=l.sku)
               ORDER BY l.created_at""").fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = _unmonitored_rows()
    print(f"[mode] {'DRY-RUN (書込なし、--apply で実行)' if DRY else 'APPLY (本番DB書込)'}")
    print(f"[target] 監視台帳 未登録 active 無在庫 listing: {len(rows)} 件\n")

    # ── step 1: snapshot ──
    bdir = BASE / "data" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    snap = bdir / f"w139_unmonitored_snapshot_{ts}.json"
    with get_conn() as c:
        mon_count = c.execute("SELECT COUNT(*) FROM monitored_items").fetchone()[0]
    snap.write_text(json.dumps(
        {"taken_at": datetime.now().isoformat(),
         "monitored_items_count_before": mon_count,
         "unmonitored_listings": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[snapshot] {snap} (monitored_items before={mon_count})\n")

    # ── step 2-3: 登録 (URL生成不能は flag) ──
    registered, url_fail = [], []
    for r in rows:
        sku, eid, title = r["sku"], r["ebay_item_id"], r["title"]
        url = None
        try:
            url = build_source_url(sku)
        except Exception as e:  # noqa: BLE001
            url = None
            print(f"  [url-err] {eid} sku={sku}: {e}")
        cfg = find_site_config_by_sku(sku)
        if not url or not cfg:
            url_fail.append(r)
            print(f"  [URL生成不可/DLQ] {eid} sku={sku} "
                  f"(prefix 未登録 site_config) — 手動対応要")
            continue
        if DRY:
            print(f"  [would-register] {eid} sku={sku} -> {url}")
        else:
            mid = upsert_item(sku=sku, ebay_item_id=eid, title=title)
            print(f"  [registered] monitored_items.id={mid} {eid} sku={sku}")
        registered.append(r)

    if DRY:
        print("\n[DRY-RUN 終了] --apply 付与で登録+スクレイプを実行します。")
        return

    # ── step 4: 登録分のみ実スクレイプ (full pipeline 不使用) ──
    print(f"\n[scrape] 登録 {len(registered)} 件を check_items_batch で実検証中...")
    configs_by_prefix = {c["convert_url"]: c for c in get_site_configs()}
    reg_skus = {r["sku"] for r in registered}
    items = [it for it in get_active_items() if it.get("sku") in reg_skus]
    batch = prepare_batch_items(items, configs_by_prefix)
    raw = check_items_batch(batch) if batch else {}

    # ── step 5: 履行不能リスク report ──
    OOS = {"在庫無", "ページなし", "売り切れ", "終了"}
    qty_by_eid = {r["ebay_item_id"]: r["quantity_ebay"] for r in rows}
    title_by_eid = {r["ebay_item_id"]: r["title"] for r in rows}
    danger, ok, ambiguous = [], [], []
    for it in items:
        eid = it.get("ebay_item_id")
        res = raw.get(it.get("id")) or raw.get(eid) or {}
        st = (res.get("status") or res.get("last_status") or "判定不能")
        qty = qty_by_eid.get(eid, "?")
        rec = {"ebay_item_id": eid, "sku": it.get("sku"), "qty": qty,
               "status": st, "title": (title_by_eid.get(eid) or "")[:55]}
        if st in OOS and isinstance(qty, int) and qty >= 1:
            danger.append(rec)
        elif st in ("在庫有",):
            ok.append(rec)
        else:
            ambiguous.append(rec)

    print("\n" + "=" * 64)
    print("【最優先・履行不能リスク】仕入先OOS/ページ消失 かつ eBay qty>=1 "
          "(= 売れたら仕入れ不能、policy B により自動停止せず手動対応要)")
    print("=" * 64)
    for d in danger:
        print(f"  ★ {d['ebay_item_id']} qty={d['qty']} {d['status']} | "
              f"{d['sku']} | {d['title']}")
    if not danger:
        print("  (なし)")
    print(f"\n【要手動確認 (判定不能/scraper曖昧)】{len(ambiguous)} 件 "
          "— 在庫有と誤断定しない (Q0)")
    for a in ambiguous:
        print(f"  ? {a['ebay_item_id']} qty={a['qty']} {a['status']} | "
              f"{a['sku']} | {a['title']}")
    print(f"\n【在庫有 (当面安全)】{len(ok)} 件")
    print(f"\n【URL生成不能/DLQ (site_config prefix 未登録、手動要)】"
          f"{len(url_fail)} 件")
    for u in url_fail:
        print(f"  ! {u['ebay_item_id']} qty={u['quantity_ebay']} | "
              f"{u['sku']} | {u['title'][:55]}")

    with get_conn() as c:
        mon_after = c.execute(
            "SELECT COUNT(*) FROM monitored_items").fetchone()[0]
        still = c.execute(
            """SELECT COUNT(*) FROM ebay_listings l
               WHERE l.is_ended=0 AND l.sku LIKE 'ebay%'
                 AND NOT EXISTS (SELECT 1 FROM monitored_items m
                                 WHERE m.sku=l.sku)""").fetchone()[0]
    print("\n" + "=" * 64)
    print(f"[verify] monitored_items: {mon_count} -> {mon_after} "
          f"(+{mon_after - mon_count})")
    print(f"[verify] 未登録 残: {still} 件 "
          f"(URL生成不能 {len(url_fail)} 件は登録不可のため残存=想定内)")
    print(f"[snapshot] rollback 用: {snap}")
    print("[next] 24h 以内に retrospective code-reviewer (本番DB書込)")


if __name__ == "__main__":
    main()
