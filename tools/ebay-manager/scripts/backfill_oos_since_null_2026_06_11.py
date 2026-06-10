#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""source_status が在庫無/ページなしなのに source_out_of_stock_since=NULL の行を backfill する one-shot。

背景 (2026-06-11 code-reviewer MEDIUM-1):
  task_sync_data_stores.py の新ガード `prev_status not in ('在庫無','ページなし')` により、
  既に OOS 状態で since=NULL の行は在庫有バウンスまで永久に since が付かず、
  supplier_sweep / supplier_select の対象外で stuck する。

backfill 値 = 実行時点の UTC now。実際の OOS 開始時刻は復元不能のため、
「観測開始を今からにする」のが正直な選択 (待ち時間は今から起算)。

Q2 6-step:
  1. 対象: source_status IN ('在庫無','ページなし') AND (is_ended IS NULL OR is_ended=0)
     AND source_out_of_stock_since IS NULL
  2. snapshot: data/backup_oos_null_backfill_YYYYMMDD_HHMMSS.json
  3. --dry-run 既定 / --apply で実書込 (1 件試行 → 検証 → 残り全件)
  4. 実行後 SELECT で stuck 残存 0 件を確認
  5. init_db 非接触 (one-shot)

使い方:
  python scripts/backfill_oos_since_null_2026_06_11.py             # dry-run
  python scripts/backfill_oos_since_null_2026_06_11.py --apply     # 実書込
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.database import get_conn  # noqa: E402

_STUCK_WHERE = (
    "source_status IN ('在庫無','ページなし') "
    "AND (is_ended IS NULL OR is_ended=0) "
    "AND source_out_of_stock_since IS NULL"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="source_out_of_stock_since NULL backfill")
    parser.add_argument("--apply", action="store_true", help="実際に UPDATE を実行する (既定は dry-run)")
    args = parser.parse_args()
    dry_run = not args.apply

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Step 1: 対象行
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ebay_item_id, source_status, source_last_checked "
            f"FROM ebay_listings WHERE {_STUCK_WHERE}"
        ).fetchall()

    if not rows:
        print("[backfill] 対象行なし。処理スキップ。")
        return

    print(f"[backfill] 対象: {len(rows)} 件 (OOS だが since=NULL) → backfill 値 = {now_utc} (UTC now)")

    # Step 2: snapshot
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = PROJECT_ROOT / "data" / f"backup_oos_null_backfill_{ts}.json"
    snapshot = [
        {"ebay_item_id": r[0], "source_status": r[1], "source_last_checked": r[2]}
        for r in rows
    ]
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[backfill] snapshot 保存: {backup_path}")

    for r in rows[:5]:
        print(f"  eid={r[0]}  status={r[1]}  last_checked={r[2]!r}")
    if len(rows) > 5:
        print(f"  ... (他 {len(rows) - 5} 件)")

    if dry_run:
        print("[backfill] dry-run モード: 書込なし。--apply で実書込。")
        return

    # Step 3: 1 件試行 → 検証 → 残り全件
    first_eid = rows[0][0]
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET source_out_of_stock_since=? WHERE ebay_item_id=?",
            (now_utc, first_eid),
        )
    with get_conn() as conn:
        check = conn.execute(
            "SELECT source_out_of_stock_since FROM ebay_listings WHERE ebay_item_id=?",
            (first_eid,),
        ).fetchone()
    actual = check[0] if check else None
    print(f"[backfill] 1件試行: eid={first_eid}  実際の値={actual!r}")
    if actual != now_utc:
        print(f"[backfill] ERROR: 期待値 {now_utc!r} と不一致。中断。")
        sys.exit(1)

    remaining = [r[0] for r in rows[1:]]
    if remaining:
        with get_conn() as conn:
            for eid in remaining:
                conn.execute(
                    "UPDATE ebay_listings SET source_out_of_stock_since=? "
                    "WHERE ebay_item_id=? AND source_out_of_stock_since IS NULL",
                    (now_utc, eid),
                )
        print(f"[backfill] 残り {len(remaining)} 件 UPDATE 完了")

    # Step 4: stuck 残存 0 確認
    with get_conn() as conn:
        left = conn.execute(f"SELECT COUNT(*) FROM ebay_listings WHERE {_STUCK_WHERE}").fetchone()[0]
    print(f"[backfill] 完了: stuck 残存 = {left} 件 (0 なら正常)")
    if left != 0:
        print("[backfill] WARNING: stuck が残存。確認が必要。")


if __name__ == "__main__":
    main()
