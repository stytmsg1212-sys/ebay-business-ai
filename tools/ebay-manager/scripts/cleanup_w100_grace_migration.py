"""W100 Phase 1 cleanup: 旧仕様 yahoo_grace_until を全 NULL 化.

旧仕様 (`task_supplier_apply.py::accept_supplier_candidate` の Yahoo 分岐) で
`accepted` 状態の listing にセットされていた yahoo_grace_until を全クリア.

理由:
  - 旧仕様: 採用→反映の遅延 (now + 24h)
  - 新仕様 (W100 Phase 3): ヤフオク終了→24h 後にリサーチ実行可能 (auction_end_time + 24h)
  - 意味が逆転しているため、旧値を新仕様で使うと誤動作する
  - inventory_check (Phase 3) が改めて立て直す

冪等: 何度実行しても結果同じ (NULL → NULL).

H-3 fix (2026-05-06): rollback snapshot を JSON ファイル保存
  (Q2 db-migration-rules.md 規定: 複数行 UPDATE は 6-step 手順、4 番 rollback snapshot 必須).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 削除前 snapshot (rollback 可能性確保 / Q2 規定)
    rows = conn.execute(
        "SELECT ebay_item_id, sku, yahoo_grace_until, primary_market, market_analysis_at "
        "FROM ebay_listings WHERE yahoo_grace_until IS NOT NULL"
    ).fetchall()
    print(f"=== W100 cleanup: 旧 yahoo_grace_until クリア ===")
    print(f"対象: {len(rows)} 件")

    if len(rows) > 0:
        # H-NEW-3 fix (2026-05-06): parents=True で data/ 不在環境への移植時 K0 hidden assumption 解消
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = BACKUP_DIR / f"w100_grace_snapshot_{ts}.json"
        snapshot = {
            "created_at": datetime.now().isoformat(),
            "purpose": "W100 Phase 1 cleanup rollback snapshot (Q2 規定準拠)",
            "rows": [dict(r) for r in rows],
        }
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"rollback snapshot: {snapshot_path} ({len(rows)} 件)")

    for r in rows[:20]:
        print(f"  {r['ebay_item_id']} sku={r['sku']} grace_until={r['yahoo_grace_until']}")
    if len(rows) > 20:
        print(f"  ... 他 {len(rows) - 20} 件")

    # 全 NULL 化 (touch 範囲: yahoo_grace_until 列のみ、他列は変更しない)
    n = conn.execute(
        "UPDATE ebay_listings SET yahoo_grace_until = NULL WHERE yahoo_grace_until IS NOT NULL"
    ).rowcount
    conn.commit()
    print()
    print(f"UPDATE 完了: {n} 行を NULL 化")

    # 検証 (Q2 6-step 4: 結果を再 SELECT)
    remain = conn.execute(
        "SELECT COUNT(*) FROM ebay_listings WHERE yahoo_grace_until IS NOT NULL"
    ).fetchone()[0]
    assert remain == 0, f"残存 {remain} 行"
    print(f"VERIFIED: yahoo_grace_until IS NOT NULL = 0 件")
    conn.close()


if __name__ == "__main__":
    main()
