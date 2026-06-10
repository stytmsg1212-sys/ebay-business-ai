#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
既存の source_out_of_stock_since JST naive 値を UTC に一発正規化する one-shot スクリプト。

Q2 6-step:
  1. 対象: source_out_of_stock_since LIKE '%T%' ('T'区切り = JST naive の署名。
     CURRENT_TIMESTAMP 由来は 'YYYY-MM-DD HH:MM:SS' で 'T' を含まない)
  2. snapshot: 変換前の全対象行を JSON で data/backup_oos_since_YYYYMMDD_HHMMSS.json に dump
  3. 変換: datetime.fromisoformat(v) - timedelta(hours=9) → strftime("%Y-%m-%d %H:%M:%S")
  4. --dry-run が既定 (表示のみ)。--apply で実書込。
     apply 時はまず 1 件だけ UPDATE → 検証 print → 残り全件
  5. 実行後 SELECT で 'T' 含有 0 件を確認して print
  6. init_db は一切触らない (one-shot script、DB migration ではない)

使い方:
  python scripts/normalize_oos_since_utc_2026_06_11.py             # dry-run (既定)
  python scripts/normalize_oos_since_utc_2026_06_11.py --apply     # 実書込
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.database import get_conn  # noqa: E402


def _convert(value: str) -> str:
    """JST naive (ISO 'T' 形式) → UTC "%Y-%m-%d %H:%M:%S" 形式。"""
    dt_jst = datetime.fromisoformat(value)
    dt_utc = dt_jst - timedelta(hours=9)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="source_out_of_stock_since JST→UTC 正規化")
    parser.add_argument("--apply", action="store_true", help="実際に UPDATE を実行する (既定は dry-run)")
    args = parser.parse_args()

    dry_run = not args.apply

    # Step 1: 対象行を取得
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ebay_item_id, source_out_of_stock_since "
            "FROM ebay_listings "
            "WHERE source_out_of_stock_since LIKE '%T%'"
        ).fetchall()

    if not rows:
        print("[normalize] 対象行なし ('T' 含有 0 件)。処理スキップ。")
        return

    print(f"[normalize] 対象: {len(rows)} 件 (JST naive 'T' 区切り)")

    # Step 2: snapshot dump
    backup_dir = PROJECT_ROOT / "data"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"backup_oos_since_{ts}.json"
    snapshot = [{"ebay_item_id": r[0], "source_out_of_stock_since": r[1]} for r in rows]
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[normalize] snapshot 保存: {backup_path}")

    # Step 3 & 4: 変換プレビュー
    conversions = []
    for row in rows:
        eid, val = row[0], row[1]
        try:
            new_val = _convert(val)
        except (ValueError, TypeError) as e:
            print(f"[normalize] 変換スキップ (parse error) eid={eid!r} val={val!r}: {e}")
            continue
        conversions.append((eid, val, new_val))

    print(f"[normalize] 変換可能: {len(conversions)} 件")
    for eid, old, new in conversions[:5]:
        print(f"  eid={eid}  {old!r} -> {new!r}")
    if len(conversions) > 5:
        print(f"  ... (他 {len(conversions) - 5} 件)")

    if dry_run:
        print("[normalize] dry-run モード: 書込なし。--apply で実書込。")
        return

    if not conversions:
        print("[normalize] 変換対象 0 件。終了。")
        return

    # apply: まず 1 件だけ UPDATE → 検証 → 残り全件
    first_eid, first_old, first_new = conversions[0]
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET source_out_of_stock_since=? WHERE ebay_item_id=?",
            (first_new, first_eid),
        )
    print(f"[normalize] 1件試行完了: eid={first_eid}  {first_old!r} -> {first_new!r}")

    # 1件確認
    with get_conn() as conn:
        check = conn.execute(
            "SELECT source_out_of_stock_since FROM ebay_listings WHERE ebay_item_id=?",
            (first_eid,),
        ).fetchone()
    actual = check[0] if check else None
    print(f"[normalize] 1件確認: eid={first_eid}  実際の値={actual!r}")
    if actual != first_new:
        print(f"[normalize] ERROR: 期待値 {first_new!r} と不一致。中断。")
        sys.exit(1)

    # 残り全件
    remaining = conversions[1:]
    if remaining:
        with get_conn() as conn:
            for eid, _old, new_val in remaining:
                conn.execute(
                    "UPDATE ebay_listings SET source_out_of_stock_since=? WHERE ebay_item_id=?",
                    (new_val, eid),
                )
        print(f"[normalize] 残り {len(remaining)} 件 UPDATE 完了")

    # Step 5: 'T' 含有 0 件確認
    with get_conn() as conn:
        remaining_t = conn.execute(
            "SELECT COUNT(*) FROM ebay_listings WHERE source_out_of_stock_since LIKE '%T%'"
        ).fetchone()[0]
    print(f"[normalize] 完了: 'T' 含有残存 = {remaining_t} 件 (0 なら正常)")
    if remaining_t != 0:
        print("[normalize] WARNING: 'T' 含有が残存。確認が必要。")


if __name__ == "__main__":
    main()
