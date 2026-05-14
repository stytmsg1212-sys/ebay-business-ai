"""未 scrape listings の誤 unknown 確定を rollback.

経緯:
  - 5/9 22:59 fastpath_classify_all_null_2026_05_09.py が 110 件の「market_analysis に
    row が一切ない (= Terapeak で一度も調べていない)」listings を unknown 確定にした.
  - これは Q0 silent skip 違反 (「scrape 必要」を「unknown 確定」で逃避修正).
  - 観測なし = 判定不能、データなし signal は本来「scrape 待ち」状態であるべき.

対象:
  - market_analysis に row なし
  - market_strategy_decisions の reviewer='fastpath_assistant_2026_05_09'
  - 現在 ebay_listings.primary_market='unknown'
  → 110 件

action:
  - ebay_listings: primary_market = NULL, market_analysis_at = NULL
  - market_strategy_decisions: action='rejected' 追加 (audit trail) + reviewer='rollback_unscraped_2026_05_09'
  - 残り (sample 0/1-2/3-4 = 232 件) の unknown 確定は維持 (観測した上で sample 不足 = 確定可)

reviewer = 'rollback_unscraped_2026_05_09'
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
REVIEWER = "rollback_unscraped_2026_05_09"
REASON = (
    "rollback: 'fastpath_assistant_2026_05_09' wrongly classified 110 listings as 'unknown' "
    "without ever scraping Terapeak. 'unscraped' != 'sample insufficient'. "
    "Restore to NULL so D batch can scrape them and provide actual data for classification. "
    "Q0 silent-skip-prevention.md violation correction."
)


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Step 1: snapshot — 対象は「fastpath で確定 + market_analysis に row なし」
    targets = conn.execute(
        """SELECT el.ebay_item_id, el.sku, el.title, el.primary_market,
                  el.market_analysis_at, msd.id AS decision_id, msd.reason AS decision_reason
           FROM ebay_listings el
           LEFT JOIN market_analysis ma ON el.ebay_item_id = ma.ebay_item_id
           JOIN market_strategy_decisions msd
             ON el.ebay_item_id = msd.ebay_item_id
            AND msd.reviewer = 'fastpath_assistant_2026_05_09'
           WHERE ma.ebay_item_id IS NULL
             AND el.primary_market = 'unknown'
           ORDER BY el.ebay_item_id"""
    ).fetchall()

    if len(targets) == 0:
        print("対象 0 件、abort")
        sys.exit(0)

    print(f"対象 (未 scrape + 今夜 unknown 確定): {len(targets)} 件")

    backup = {
        "timestamp": datetime.now().isoformat(),
        "targets": [dict(r) for r in targets],
    }
    backup_path = BACKUP_DIR / f"rollback_unscraped_2026_05_09_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(backup, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"snapshot: {backup_path.name}")

    # Step 2: 1 件試行
    first = targets[0]
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "UPDATE ebay_listings SET primary_market=NULL, market_analysis_at=NULL WHERE ebay_item_id=?",
            (first["ebay_item_id"],),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print(f"!!! rowcount={cur.rowcount} !!!")
            sys.exit(2)
        cur.execute(
            """INSERT INTO market_strategy_decisions
               (sku, ebay_item_id, previous_market, proposed_market, final_market,
                action, decided_at, reason, reviewer)
               VALUES (?, ?, 'unknown', NULL, NULL, 'rejected', ?, ?, ?)""",
            (first["sku"], first["ebay_item_id"], datetime.now().isoformat(), REASON, REVIEWER),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise

    pm_after = conn.execute(
        "SELECT primary_market FROM ebay_listings WHERE ebay_item_id=?",
        (first["ebay_item_id"],),
    ).fetchone()[0]
    if pm_after is not None:
        print(f"!!! 1 件試行: 期待 None, 実 {pm_after!r} !!!")
        sys.exit(3)
    print(f"[step2] OK ({first['ebay_item_id']} → NULL)")

    # Step 3: 残り
    print(f"\n[step3] 残り {len(targets) - 1} 件")
    cur.execute("BEGIN")
    try:
        for r in targets[1:]:
            cur.execute(
                "UPDATE ebay_listings SET primary_market=NULL, market_analysis_at=NULL WHERE ebay_item_id=?",
                (r["ebay_item_id"],),
            )
            cur.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, previous_market, proposed_market, final_market,
                    action, decided_at, reason, reviewer)
                   VALUES (?, ?, 'unknown', NULL, NULL, 'rejected', ?, ?, ?)""",
                (r["sku"], r["ebay_item_id"], datetime.now().isoformat(), REASON, REVIEWER),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise

    # Step 4: 検証
    print("\n[step4] 検証")
    print("ebay_listings.primary_market 分布:")
    for r in conn.execute("SELECT primary_market, COUNT(*) FROM ebay_listings GROUP BY primary_market"):
        print(f"  {r[0]!r}: {r[1]}")

    rejected_count = conn.execute(
        "SELECT COUNT(*) FROM market_strategy_decisions WHERE reviewer=?", (REVIEWER,)
    ).fetchone()[0]
    print(f"\nrollback rejected rows: {rejected_count} (期待 {len(targets)})")

    print("\n" + "=" * 60)
    print("- 使用モデル: rollback script (no LLM)")
    print(f"- 検証経路: DB SELECT (110 件 NULL 復帰 + 110 件 rejected audit)")
    print(f"- 実機ログ: snapshot {backup_path.name}")
    print("- 残リスク: D batch で 110 件 scrape → 実データで再分類")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
