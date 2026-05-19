#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139-fix one-shot: backfill 後も ebay_item_id 解決不能な孤立 monitored 行を
is_active=0 に降格 (DELETE しない = 誤削除防止)。

孤立行の定義 (全 AND):
  - ebay_item_id IS NULL OR ''  (backfill で埋まらなかった unresolved)
  - is_active=1                  (既に停止済は二重処理しない = 冪等)
  - その行の source_url (無ければ sku から build_source_url 再計算) で
    ebay_listings に COALESCE(is_ended,0)=0 な active listing が **1 件も無い**
    (= もう生きている出品が無い死んだ監視エントリ)

誤削除防止ガード (多層、W139 原事故 = 監視対象を誤減 → 履行不能 の再現防止):
  G1. DELETE 禁止。UPDATE is_active=0 のみ (誤判定でも is_active=1 復元可)。
      check_log は保持 (Q0 痕跡)。
  G2. active listing が source_url で紐づく行は **絶対対象外** (核心ガード)。
  G3. 件数上限: 対象 > monitored 総数の 5% (user 承認、dry-run 実数で再提示) は
      自動 apply せず report のみで停止 → user 判断 (大量降格 = backfill 不全
      の signal)。
  G4. backfill 順序ガード: ebay_item_id NULL 件数が総数の 15% 超 = backfill
      未適用の疑い → 既定停止 (--force-prebackfill で明示上書き可)。
  G5. --apply 時 snapshot JSON 保存 (db-migration-rules 6-step、24h
      retrospective 用)。冪等 (is_active=0 は WHERE 除外、2 回目 0 件)。
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
# 作らない (pytest から _has_active_listing を直接 test 可能にする = K3)。
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

from monitor.database import (  # noqa: E402
    get_conn, build_source_url, _build_source_url_from_sku,
)

SNAP_DIR = os.path.join(PROJ, "data", "w139fix_backfill")
THRESH_PCT = 0.05      # G3: 5% 上限 (user 承認、dry-run 実数で再提示)
PREBACKFILL_PCT = 0.15  # G4: NULL 率がこれ超なら backfill 未適用疑い


def _has_active_listing(conn, source_url, sku):
    """source_url に紐づく active listing が有るか (G2 核心ガード)。

    HIGH-1 (2026-05-18 実証): build_source_url と _build_source_url_from_sku
    が mercari 等で食い違い本番 ebay_listings.source_url は後者形。単一生成器
    照合だと mercari active listing を誤って『紐付きなし』と判定 → 孤立誤判定
    → is_active=0 → 監視外 → 履行不能 (= W139 原事故再現)。保存値 + 両生成器
    形を IN で網羅し取りこぼさない。"""
    urls = set()
    if source_url:
        urls.add(source_url)
    if sku:
        for fn in (build_source_url, _build_source_url_from_sku):
            try:
                u = fn(sku)
            except Exception:  # noqa: BLE001
                u = None
            if u:
                urls.add(u)
    if not urls:
        return False
    qm = ",".join("?" * len(urls))
    n = conn.execute(
        f"SELECT COUNT(*) FROM ebay_listings "
        f"WHERE source_url IN ({qm}) AND COALESCE(is_ended,0)=0",
        tuple(urls),
    ).fetchone()[0]
    return n > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-prebackfill", action="store_true",
                    help="G4 を無視 (backfill 未適用でも続行、非推奨)")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"=== W139-fix cleanup orphan monitored [{mode}] {ts} ===")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) FROM monitored_items").fetchone()[0]
        null_cnt = conn.execute(
            "SELECT COUNT(*) FROM monitored_items "
            "WHERE ebay_item_id IS NULL OR ebay_item_id=''").fetchone()[0]
        print(f"monitored 総数={total} / ebay_item_id NULL/''={null_cnt}")

        # G4: backfill 順序ガード
        if total and null_cnt / total > PREBACKFILL_PCT \
                and not args.force_prebackfill:
            print(f"\n[STOP G4] NULL 率 {null_cnt/total:.0%} > "
                  f"{PREBACKFILL_PCT:.0%} = backfill 未適用の疑い。"
                  "先に backfill --apply を実行。"
                  "意図的に続行するなら --force-prebackfill。")
            return

        rows = conn.execute(
            "SELECT id, sku, source_url, ebay_item_id, title, is_active "
            "FROM monitored_items "
            "WHERE (ebay_item_id IS NULL OR ebay_item_id='') "
            "  AND is_active=1"
        ).fetchall()

        orphans, protected = [], []
        for r in rows:
            if _has_active_listing(conn, r["source_url"], r["sku"]):
                protected.append(dict(r))  # G2: active 紐付き = 絶対保護
            else:
                orphans.append(dict(r))

    print(f"\nNULL かつ is_active=1 = {len(rows)} 件")
    print(f"  G2 保護 (active listing 紐付き、対象外) = {len(protected)} 件")
    print(f"  孤立 (cleanup 対象候補) = {len(orphans)} 件")
    for o in orphans:
        print(f"  [孤立] id={o['id']} sku={o['sku']} "
              f"url={o['source_url']} title={(o['title'] or '')[:40]}")

    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"cleanup_snapshot_{ts}.json")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "mode": mode, "total": total,
                   "orphans": orphans, "protected_count": len(protected)},
                  f, ensure_ascii=False, indent=2)
    print(f"\nsnapshot 保存: {snap}")

    # G3: 件数上限
    over = total and len(orphans) > total * THRESH_PCT
    if over:
        print(f"\n[STOP G3] 孤立 {len(orphans)} 件 > 総数の "
              f"{THRESH_PCT:.0%} ({int(total*THRESH_PCT)} 件)。"
              "自動 apply せず停止。dry-run 実数を user に提示し判断を仰ぐこと。")
        if args.apply:
            print("  (--apply 指定だが G3 で中断、UPDATE 未実行)")
        return

    if not args.apply:
        print(f"\n[DRY-RUN] is_active=0 降格 未実行。対象 {len(orphans)} 件。"
              "--apply で適用 (G3 上限内のため apply 可)。")
        return
    if not orphans:
        print("\n[APPLY] 孤立 0 件。処理不要 (冪等)。")
        return

    with get_conn() as conn:
        first = orphans[0]
        cur = conn.execute(
            "UPDATE monitored_items SET is_active=0 "
            "WHERE id=? AND is_active=1", (first["id"],))
        if cur.rowcount != 1:
            print(f"[ABORT] 1 件試行 rowcount={cur.rowcount} (期待 1)。中断。")
            return
        print(f"[APPLY] 1 件試行 OK (id={first['id']})。残り適用...")
        for o in orphans[1:]:
            conn.execute(
                "UPDATE monitored_items SET is_active=0 "
                "WHERE id=? AND is_active=1", (o["id"],))

    with get_conn() as conn:
        still = conn.execute(
            "SELECT COUNT(*) FROM monitored_items "
            "WHERE (ebay_item_id IS NULL OR ebay_item_id='') "
            "  AND is_active=1").fetchone()[0]
    print(f"[APPLY] 完了。降格 {len(orphans)} 件。NULL かつ is_active=1 残="
          f"{still} (G2 保護分のみ残るのが期待値)")
    print(f"rollback: snapshot {snap} の orphans[].id を is_active=1 に戻す")


if __name__ == "__main__":
    main()
