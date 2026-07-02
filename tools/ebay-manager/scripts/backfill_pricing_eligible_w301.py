#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W301 AI 店長 Phase1 S1 backfill: 既存 active 採用ライバルを pricing_eligible=1 へ.

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §11 Q2(a)。
migration v86 で `competitor_products.pricing_eligible` を追加すると、既存行は
全て default 0 (Shadow 安全側) になる。しかし過去に人間が手動採用した active
ライバル (約178件、is_active=1) は既に値下げ判断の実運用対象として温存されて
いたため、pricing_eligible=0 のままだと運用が後退する (user 承認済み条件 1)。
本 one-shot script はその既存 active 採用分のみを pricing_eligible=1 に
backfill する。新規採用・非 active 行は default 0 のまま (Shadow 対象)。

db-migration-rules.md 6-step 準拠:
  1. 対象件数 SELECT dump (JSON snapshot、rollback 用)
  2. dry-run 表示 (--apply 無指定なら書込しない)
  3. --apply 指定時のみ UPDATE 実行 (--limit で件数を絞った試行も可能)
  4. 実行後 SELECT 再確認 (before/after 件数を出力)
  5. 24h 以内に retrospective code-reviewer (実行者側の運用、本 script の範囲外)
  6. 異常時は snapshot から rollback

⚠️ 本 script は作成のみ (generator scope)。実行判断は main / user に委ねる。
本番 data/monitor.db への --apply 実行はこのタスクでは行わない。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "data/monitor.db"


def _fetch_targets(conn: sqlite3.Connection) -> list[dict]:
    """backfill 対象 = active 採用 (is_active=1) かつ未だ pricing_eligible=0 の行.

    listing 識別は競合側 competitor_item_id (UNIQUE、sku-rules.md 準拠、sku 不使用)。
    """
    rows = conn.execute(
        "SELECT id, our_item_id, competitor_item_id, competitor_seller, "
        "is_active, pricing_eligible "
        "FROM competitor_products "
        "WHERE is_active = 1 AND COALESCE(pricing_eligible, 0) = 0 "
        "ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB, help="対象 DB path (テスト時は tmp DB を指定)")
    ap.add_argument("--limit", type=int, default=0, help="0=全件、N=先頭N件のみ(試行用)")
    ap.add_argument("--apply", action="store_true", help="未指定なら dry-run(書込なし)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    targets = _fetch_targets(conn)
    if args.limit:
        targets = targets[: args.limit]
    print(f"[backfill_w301] 対象 {len(targets)} 件 (apply={args.apply}, db={args.db})\n")

    # Step 1: snapshot backup (rollback 用)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path(args.db).parent / f"backup_pricing_eligible_w301_{ts}.json"
    backup_path.write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[backfill_w301] snapshot -> {backup_path}\n")

    for t in targets:
        label = t["competitor_item_id"]
        print(
            f"--- id={t['id']} our_item_id={t['our_item_id']} "
            f"competitor_item_id={label} is_active={t['is_active']} "
            f"pricing_eligible: 0 -> 1"
        )

    applied = 0
    if args.apply:
        ids = [t["id"] for t in targets]
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE competitor_products SET pricing_eligible = 1 "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            applied = cur.rowcount
            conn.commit()

    # Step 4: 実行後 SELECT 再確認
    after_eligible = conn.execute(
        "SELECT COUNT(*) FROM competitor_products WHERE pricing_eligible = 1"
    ).fetchone()[0]
    after_active_not_eligible = conn.execute(
        "SELECT COUNT(*) FROM competitor_products "
        "WHERE is_active = 1 AND COALESCE(pricing_eligible, 0) = 0"
    ).fetchone()[0]
    print(
        f"\n[backfill_w301] {'適用' if args.apply else 'dry-run'} 完了: "
        f"対象 {len(targets)} / 書込 {applied} "
        f"(適用後 pricing_eligible=1 総数={after_eligible}, "
        f"active かつ未backfillの残={after_active_not_eligible})"
    )
    conn.close()


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    finally:
        print(f"[backfill_w301] elapsed {time.time()-t0:.1f}s")
