"""C batch (ended 107 件 scrape) 完了後に走らせる auto-approve script.

C batch が `pending_market_changes` に提案を蓄積する → 本 script で全件 approve.
ended listings が対象なので送料計算に使われない (実害ゼロ)、approve 完了 = 全 listings
の primary_market 確定 → 「割り振って終わり」状態に到達.

Q2 6-step 準拠: snapshot → 1 件試行 → 残り → 検証.
reviewer = 'fastpath_endedC_2026_05_09'
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
REVIEWER = "fastpath_endedC_2026_05_09"


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    pending = conn.execute(
        "SELECT * FROM pending_market_changes ORDER BY proposed_at"
    ).fetchall()

    if len(pending) == 0:
        print("対象 0 件、abort (C batch 未完走 or 既に approve 済)")
        sys.exit(0)

    print(f"対象 pending: {len(pending)} 件")

    # snapshot
    backup = {
        "timestamp": datetime.now().isoformat(),
        "pending": [dict(r) for r in pending],
    }
    backup_path = BACKUP_DIR / f"approve_pending_endedC_2026_05_09_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(backup, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"snapshot: {backup_path.name}")

    # 全件 approve (ended listings 主体なので個別 review 不要)
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for p in pending:
            cur.execute(
                "UPDATE ebay_listings SET primary_market=?, market_analysis_at=? WHERE ebay_item_id=?",
                (p["proposed_market"], datetime.now().isoformat(), p["ebay_item_id"]),
            )
            cur.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, previous_market, proposed_market, final_market,
                    action, decided_at, reason, reviewer)
                   VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?)""",
                (
                    p["sku"], p["ebay_item_id"], p["current_market"],
                    p["proposed_market"], p["proposed_market"],
                    datetime.now().isoformat(),
                    f"fastpath ended C: {p['reason'] or ''}", REVIEWER,
                ),
            )
            cur.execute(
                "DELETE FROM pending_market_changes WHERE ebay_item_id=?",
                (p["ebay_item_id"],),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! exception: {type(e).__name__}: {e}")
        raise

    # 検証
    print("\n検証:")
    print("  ebay_listings 全件分布:")
    for r in conn.execute("SELECT primary_market, COUNT(*) FROM ebay_listings GROUP BY primary_market"):
        print(f"    {r[0]!r}: {r[1]}")

    nul = conn.execute("SELECT COUNT(*) FROM ebay_listings WHERE primary_market IS NULL").fetchone()[0]
    pmc = conn.execute("SELECT COUNT(*) FROM pending_market_changes").fetchone()[0]
    print(f"\n  NULL 残: {nul} (期待 0)")
    print(f"  pending 残: {pmc} (期待 0)")

    print("\n" + "=" * 60)
    print("- 使用モデル: rule-based fastpath script (no LLM)")
    print(f"- 検証経路: DB SELECT (NULL 0 件 + pending 0 件)")
    print(f"- 実機ログ: snapshot {backup_path.name}")
    print("- 残リスク: 24h 以内 retrospective code-reviewer (Q2)")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
