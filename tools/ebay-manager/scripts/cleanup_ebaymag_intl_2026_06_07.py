#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 各国版 (非USD) listing を MonoDeck DB から隔離削除する one-shot (2026-06-07).

背景: eBaymag を全国 ON にした結果、各国サイト (CA/UK/DE/AU 等) の複製 listing が
同一アカウントの GetMyeBaySelling に currency=CAD/GBP/EUR/AUD で混入し、約500件が
ebay_listings に取り込まれた (1 SKU 最大 8 item_id 複製)。これらは eBaymag が US 在庫
連動で自前管理するため、MonoDeck の定時処理 (relist/値下げ/在庫/仕入先) が触ると
二重管理で破壊する。currency!=USD の listing と紐付き行を削除し、US 本体のみに戻す。

恒久対策 (本 script とは別): monitor/ebay_sync.py で sync 時に currency!=USD を取り込まない。
本 script は「既に取り込まれてしまった既存行」のワンタイム掃除。

判別: GetMyeBaySelling の CurrentPrice currencyID != USD (実機確認 2026-06-07、<Site> は
GetMyeBaySelling では返らないため通貨で判別)。

Q2 6-step 準拠:
  1. 対象行を JSON backup (data/backups/) に snapshot (rollback 用)
  2. dry-run (既定) で件数提示
  3. --apply で transaction 内 DELETE
  4. DELETE 後に SELECT で残存0を確認
  5. 24h 以内 retrospective review (本 script + 関連 feedback を context に)
  6. 異常時は backup JSON から復元

使い方:
  python scripts/cleanup_ebaymag_intl_2026_06_07.py            # dry-run (件数のみ)
  python scripts/cleanup_ebaymag_intl_2026_06_07.py --apply    # 実削除 (backup 後)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = _ROOT / "data" / "monitor.db"
BACKUP_DIR = _ROOT / "data" / "backups"

# 削除対象テーブルと item_id カラム (2026-06-07 実測で参照のあった4テーブルのみ)。
TARGET_TABLES = {
    "ebay_listings": "ebay_item_id",
    "monitored_items": "ebay_item_id",
    "supplier_candidates": "ebay_item_id",
    "supplier_candidate_evaluations": "ebay_item_id",
}


def get_intl_item_ids() -> list[str]:
    """ライブ GetMyeBaySelling から currency!=USD の item_id を取得 (権威的判別)."""
    from monitor.credentials import get_ebay_credentials
    from monitor.ebay_client import get_active_listings

    cr = get_ebay_credentials()
    items = get_active_listings(cr["app_id"], cr["dev_id"], cr["cert_id"], cr["user_token"])
    intl = sorted({
        l["item_id"] for l in items
        if l.get("item_id") and (l.get("currency") or "USD") != "USD"
    })
    return intl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実削除 (未指定なら dry-run)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    print("ライブ GetMyeBaySelling から非USD(各国) item_id を取得中...")
    intl = get_intl_item_ids()
    print(f"非USD(eBaymag各国版) item_id: {len(intl)} 件")
    if not intl:
        print("対象0件。何もしません。")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(intl))

    # --- Step 1: 件数集計 + backup snapshot ---
    snapshot: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for tbl, col in TARGET_TABLES.items():
        rows = conn.execute(
            f"SELECT * FROM {tbl} WHERE {col} IN ({ph})", intl
        ).fetchall()
        snapshot[tbl] = [dict(r) for r in rows]
        counts[tbl] = len(rows)

    print("\n=== 削除対象 件数 ===")
    for tbl, n in counts.items():
        print(f"  {tbl}: {n}")
    total = sum(counts.values())
    print(f"  合計: {total} 行")

    if not args.apply:
        print("\n[DRY-RUN] --apply を付けると backup 後に実削除します。")
        conn.close()
        return 0

    # backup を必ず先に書く (rollback 用)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ebaymag_intl_cleanup_{ts}.json"
    backup_path.write_text(
        json.dumps(
            {"intl_item_ids": intl, "snapshot": snapshot, "counts": counts},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nbackup 書込: {backup_path} ({total} 行)")

    # --- Step 3: transaction 内 DELETE ---
    deleted: dict[str, int] = {}
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for tbl, col in TARGET_TABLES.items():
            r = cur.execute(f"DELETE FROM {tbl} WHERE {col} IN ({ph})", intl)
            deleted[tbl] = r.rowcount
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"DELETE 失敗、rollback しました: {e}")
        conn.close()
        return 1

    print("\n=== 削除実行結果 ===")
    for tbl, n in deleted.items():
        print(f"  {tbl}: {n} 行削除")

    # --- Step 4: SELECT で残存0を確認 ---
    print("\n=== 残存確認 (0 であるべき) ===")
    ok = True
    for tbl, col in TARGET_TABLES.items():
        n = conn.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE {col} IN ({ph})", intl
        ).fetchone()[0]
        print(f"  {tbl}: 残 {n}")
        if n:
            ok = False
    active_usd = conn.execute(
        "SELECT COUNT(*) FROM ebay_listings WHERE COALESCE(is_ended,0)=0"
    ).fetchone()[0]
    print(f"\nebay_listings active 残数 (US本体想定 ~525): {active_usd}")
    conn.close()
    print("\n完了。" + ("残存0 OK。" if ok else "⚠️ 残存あり、要確認。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
