#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139-fix one-shot: monitored_items.ebay_item_id を backfill.

背景: find_coverage_gaps を `m.sku=l.sku` から `m.ebay_item_id=l.ebay_item_id`
キー化する (sku-rules.md 準拠) が、本番 monitored_items の約半数 (~200/390) が
ebay_item_id IS NULL/''。query 変更を先に入れると旧行が全て phantom gap 化し
非dedupe Discord 緊急通知爆発 + monitored 汚染。よって **query 変更デプロイ前に**
本 backfill で ebay_item_id を充填する (ビルドシーケンス: backfill→query)。

解決ロジック (sku-rules.md: SKU結合は使わない。sku→build_source_url の URL
派生のみ許容):
  1. monitored_items.source_url を ebay_listings.source_url と文字列一致
     (COALESCE(is_ended,0)=0 = active) → ちょうど 1 件なら採用 (主経路)
  2. active 0 件: ended 含め再一致 → ちょうど 1 件なら採用 (過去 listing 紐付け)
  3. 複数件一致: 一意決定不能 = unresolved (推測で誤 ID 充填しない = Q0)
  4. m.source_url NULL/空: m.sku から build_source_url 再計算 → 1 を再試行
  5. 全不能: unresolved (件数 + id + sku + source_url を報告 = Q0 silent skip 禁止)

安全 (db-migration-rules 6-step):
  - --dry-run (既定) / --apply
  - --apply 時: SELECT snapshot を JSON 保存 → 1 件試行 → 残り → 再 SELECT
  - 冪等: ebay_item_id 充填済は WHERE で対象外。2 回目 updated=0
  - 推測充填しない (unresolved は NULL のまま、後続 cleanup の判定対象)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
# DB_PATH は __file__ 基準の絶対パスなので os.chdir 不要。import 時副作用を
# 作らない (pytest から _resolve_ebay_item_id を直接 test 可能にする = K3)。
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

from monitor.database import (  # noqa: E402
    get_conn, build_source_url, _build_source_url_from_sku,
)

SNAP_DIR = os.path.join(PROJ, "data", "w139fix_backfill")


def _candidate_urls(m_source_url, m_sku):
    """保存値 + 両生成器 (build_source_url / _build_source_url_from_sku) の
    URL を候補集合に。HIGH-1 (2026-05-18 実証): mercari 等で 2 生成器が
    食い違い、本番 ebay_listings.source_url は _build_source_url_from_sku 形、
    monitored.source_url は行ごとに両形が混在。どの形で保存されていても
    listing と突合できるよう全候補で IN 照合する。"""
    urls = set()
    if m_source_url:
        urls.add(m_source_url)
    if m_sku:
        for fn in (build_source_url, _build_source_url_from_sku):
            try:
                u = fn(m_sku)
            except Exception:  # noqa: BLE001 — 片方失敗は他候補で吸収
                u = None
            if u:
                urls.add(u)
    return urls


def _resolve_ebay_item_id(conn, m_source_url, m_sku):
    """(ebay_item_id, reason) を返す。解決不能なら (None, reason).

    sku-rules.md 準拠: sku は build_source_url の URL 派生のみ (識別キーに
    しない)。突合キーは source_url (両生成器形を IN で網羅)。"""
    urls = _candidate_urls(m_source_url, m_sku)
    if not urls:
        return None, "source_url 無し かつ sku から再計算不能"
    qm = ",".join("?" * len(urls))
    params = tuple(urls)

    # 1. active 一致 (DISTINCT: 同 listing が複数候補形で重複ヒットしても 1)
    rows = conn.execute(
        f"SELECT DISTINCT ebay_item_id FROM ebay_listings "
        f"WHERE source_url IN ({qm}) AND COALESCE(is_ended,0)=0 "
        f"  AND ebay_item_id IS NOT NULL AND ebay_item_id<>''",
        params,
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0], "active 一致 1 件"
    if len(rows) > 1:
        return None, f"active 複数一致 {len(rows)} 件 (一意決定不能)"

    # 2. ended 含め再一致
    rows = conn.execute(
        f"SELECT DISTINCT ebay_item_id FROM ebay_listings "
        f"WHERE source_url IN ({qm}) AND ebay_item_id IS NOT NULL "
        f"  AND ebay_item_id<>''",
        params,
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0], "ended 含め一致 1 件"
    if len(rows) > 1:
        return None, f"ended 含め複数一致 {len(rows)} 件 (一意決定不能)"
    return None, "ebay_listings に一致 source_url 無し"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="実際に UPDATE する (既定は dry-run)")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"=== W139-fix backfill monitored_items.ebay_item_id [{mode}] {ts} ===")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        targets = conn.execute(
            "SELECT id, sku, source_url, ebay_item_id, title "
            "FROM monitored_items "
            "WHERE ebay_item_id IS NULL OR ebay_item_id=''"
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM monitored_items").fetchone()[0]
    print(f"monitored_items 総数={total} / 対象(ebay_item_id NULL/'')={len(targets)}")

    resolved, unresolved = [], []
    with get_conn() as conn:
        for r in targets:
            eid, reason = _resolve_ebay_item_id(
                conn, r["source_url"], r["sku"])
            rec = {"id": r["id"], "sku": r["sku"],
                   "source_url": r["source_url"], "resolved_ebay_item_id": eid,
                   "reason": reason}
            (resolved if eid else unresolved).append(rec)

    print(f"\n解決可能 = {len(resolved)} 件 / unresolved = {len(unresolved)} 件")
    print("\n-- unresolved (Q0: 推測充填せず NULL 維持、後続 cleanup 判定対象) --")
    for u in unresolved:
        print(f"  id={u['id']} sku={u['sku']} url={u['source_url']} "
              f"理由={u['reason']}")
    print("\n-- resolved sample (先頭 15) --")
    for x in resolved[:15]:
        print(f"  id={x['id']} sku={x['sku']} -> {x['resolved_ebay_item_id']} "
              f"({x['reason']})")

    os.makedirs(SNAP_DIR, exist_ok=True)
    snap_path = os.path.join(SNAP_DIR, f"backfill_snapshot_{ts}.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(
            {"ts": ts, "mode": mode, "total": total,
             "targets": [dict(r) for r in targets],
             "resolved": resolved, "unresolved": unresolved},
            f, ensure_ascii=False, indent=2)
    print(f"\nsnapshot 保存: {snap_path}")

    if not args.apply:
        print("\n[DRY-RUN] UPDATE 未実行。--apply で適用。")
        print(f"想定: ebay_item_id NULL {len(targets)} -> {len(unresolved)} に減少")
        return

    if not resolved:
        print("\n[APPLY] 解決可能 0 件。UPDATE 不要。")
        return

    # 6-step: 1 件試行 → 残り
    with get_conn() as conn:
        first = resolved[0]
        cur = conn.execute(
            "UPDATE monitored_items SET ebay_item_id=? "
            "WHERE id=? AND (ebay_item_id IS NULL OR ebay_item_id='')",
            (first["resolved_ebay_item_id"], first["id"]))
        if cur.rowcount != 1:
            print(f"[ABORT] 1 件試行で rowcount={cur.rowcount} (期待 1)。"
                  "冪等条件で既に埋まっている可能性。中断。")
            return
        print(f"[APPLY] 1 件試行 OK (id={first['id']})。残り適用...")
        for x in resolved[1:]:
            conn.execute(
                "UPDATE monitored_items SET ebay_item_id=? "
                "WHERE id=? AND (ebay_item_id IS NULL OR ebay_item_id='')",
                (x["resolved_ebay_item_id"], x["id"]))

    with get_conn() as conn:
        remain = conn.execute(
            "SELECT COUNT(*) FROM monitored_items "
            "WHERE ebay_item_id IS NULL OR ebay_item_id=''").fetchone()[0]
    print(f"[APPLY] 完了。ebay_item_id NULL/'' 残 = {remain} 件 "
          f"(unresolved {len(unresolved)} と一致が期待値)")
    print(f"rollback: snapshot {snap_path} の resolved[].id を "
          "ebay_item_id=NULL に戻す one-shot で復元可")


if __name__ == "__main__":
    main()
