"""最速モード: 残 NULL listing 全件 + pending_market_changes 全件を assistant 判断で確定.

経緯:
  - 5/9 22:24:59 の UI 一括誤承認 50 件は別 script で rollback 済.
  - rollback 後、user 方針確定: 「assistant が割り振って終わり、新規出品分は出品時に
    user が UI で手動選択。scheduler 週次再評価不要」.
  - 残 NULL 337 件 + pending 189 件を assistant が一気に確定する.

判定ロジック:
  1. pending_market_changes 189 件 → final_market = proposed_market でそのまま approve
     (US_only→global_only 3 件 / US_only→unknown 5 件 / NULL→unknown 181 件)
  2. pending に未登録の NULL listings (~156 件) → market_analysis 最新 row の
     primary_market を採用. ma.primary_market = NULL/unknown または ma 自体が無い場合は
     'unknown' 確定 (sample 不足は確定状態として受容).
  3. 例外: pending US_only→global_only 3 件のみ「降格採用」(sample 充実、根拠明確).

Q2 6-step:
  1. snapshot JSON (全対象 listings + pending 行)
  2. 1 件試行 → ebay_listings 遷移 + decision INSERT
  3. 残り全件
  4. 再 SELECT で primary_market 分布検証 (NULL 0 件 が GOAL)
  5. 24h 以内 retrospective code-reviewer
  6. kill switch: pending_market_changes は DELETE で消去 (decision に audit 残る)

reviewer = 'fastpath_assistant_2026_05_09'
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
REVIEWER = "fastpath_assistant_2026_05_09"


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ───────────────────────────────────────────
    # Step 1: snapshot
    # ───────────────────────────────────────────
    null_listings = conn.execute(
        """SELECT el.ebay_item_id, el.sku, el.title, el.primary_market,
                  ma.primary_market AS ma_market,
                  ma.us_count, ma.non_us_count, ma.scraped_at
           FROM ebay_listings el
           LEFT JOIN (
             SELECT ebay_item_id, primary_market, us_count, non_us_count, scraped_at,
                    ROW_NUMBER() OVER (PARTITION BY ebay_item_id ORDER BY scraped_at DESC) AS rn
             FROM market_analysis
           ) ma ON el.ebay_item_id = ma.ebay_item_id AND ma.rn = 1
           WHERE el.primary_market IS NULL"""
    ).fetchall()

    pending = conn.execute(
        "SELECT * FROM pending_market_changes"
    ).fetchall()

    print(f"NULL listings: {len(null_listings)} 件")
    print(f"pending_market_changes: {len(pending)} 件")

    backup = {
        "timestamp": datetime.now().isoformat(),
        "null_listings_before": [dict(r) for r in null_listings],
        "pending_before": [dict(r) for r in pending],
    }
    backup_path = BACKUP_DIR / f"fastpath_classify_2026_05_09_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(backup, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"snapshot: {backup_path.name}")

    # 判定: ebay_item_id → (final_market, source, sku, previous_market)
    pending_map = {p["ebay_item_id"]: p for p in pending}
    decisions = []  # list of dict

    for el in null_listings:
        eid = el["ebay_item_id"]
        sku = el["sku"]
        prev = None  # current ebay_listings.primary_market = NULL

        if eid in pending_map:
            # Path A: pending 提案を採用
            p = pending_map[eid]
            final = p["proposed_market"]
            reason = f"fastpath: pending {p['current_market']!r}->{p['proposed_market']!r}, " + (p["reason"] or "")
            source = "pending"
        elif el["ma_market"] in ("US_only", "global_only", "mixed_global"):
            # Path B: ma 既判定確定 (rare、scheduler 判定済だが pending に入っていない)
            final = el["ma_market"]
            reason = f"fastpath: ma latest={el['ma_market']}, sample={el['us_count'] or 0}/{(el['us_count'] or 0)+(el['non_us_count'] or 0)}"
            source = "ma_direct"
        else:
            # Path C: sample 不足 / 未 scrape → unknown 確定
            sample_total = (el["us_count"] or 0) + (el["non_us_count"] or 0) if el["ma_market"] is not None else 0
            final = "unknown"
            reason = f"fastpath: sample {sample_total}, " + (
                "未 scrape" if el["scraped_at"] is None else f"ma_market={el['ma_market']!r}"
            )
            source = "fallback_unknown"

        decisions.append({
            "ebay_item_id": eid,
            "sku": sku,
            "previous_market": prev,
            "final_market": final,
            "reason": reason[:500],
            "source": source,
        })

    # 同じ pending に対して NULL listing が無い (= US_only→global_only 3 件 / US_only→unknown 5 件) も処理
    for p in pending:
        eid = p["ebay_item_id"]
        if any(d["ebay_item_id"] == eid for d in decisions):
            continue  # 既に NULL listing 経路で処理済
        # ebay_listings.primary_market が NULL でない (US_only 等から変更提案)
        el_row = conn.execute(
            "SELECT primary_market, sku FROM ebay_listings WHERE ebay_item_id=?", (eid,)
        ).fetchone()
        if el_row is None:
            continue  # listing 自体が無い (delete 済)
        decisions.append({
            "ebay_item_id": eid,
            "sku": el_row["sku"],
            "previous_market": el_row["primary_market"],
            "final_market": p["proposed_market"],
            "reason": f"fastpath: pending {p['current_market']!r}->{p['proposed_market']!r}, " + (p["reason"] or ""),
            "source": "pending_existing",
        })

    print(f"\n判定 sources 分布:")
    from collections import Counter
    src_counter = Counter(d["source"] for d in decisions)
    for k, v in src_counter.most_common():
        print(f"  {k}: {v}")
    final_counter = Counter(d["final_market"] for d in decisions)
    print(f"final_market 分布:")
    for k, v in final_counter.most_common():
        print(f"  {k}: {v}")

    # ───────────────────────────────────────────
    # Step 2: 1 件試行
    # ───────────────────────────────────────────
    first = decisions[0]
    print(f"\n[step2] 1 件試行: ebay_item_id={first['ebay_item_id']}, final={first['final_market']!r}")
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            """UPDATE ebay_listings
               SET primary_market = ?,
                   market_analysis_at = ?
               WHERE ebay_item_id = ?""",
            (first["final_market"], datetime.now().isoformat(), first["ebay_item_id"]),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print(f"!!! rowcount={cur.rowcount}, abort !!!")
            sys.exit(2)
        cur.execute(
            """INSERT INTO market_strategy_decisions
               (sku, ebay_item_id, previous_market, proposed_market, final_market,
                action, decided_at, reason, reviewer)
               VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?)""",
            (first["sku"], first["ebay_item_id"], first["previous_market"],
             first["final_market"], first["final_market"],
             datetime.now().isoformat(), first["reason"], REVIEWER),
        )
        cur.execute(
            "DELETE FROM pending_market_changes WHERE ebay_item_id = ?",
            (first["ebay_item_id"],),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! exception: {type(e).__name__}: {e} !!!")
        raise

    # 検証
    pm_after = conn.execute(
        "SELECT primary_market FROM ebay_listings WHERE ebay_item_id=?",
        (first["ebay_item_id"],),
    ).fetchone()[0]
    if pm_after != first["final_market"]:
        print(f"!!! 1 件試行: 期待 {first['final_market']!r}, 実 {pm_after!r}, abort !!!")
        sys.exit(3)
    print(f"  primary_market = {pm_after!r} (期待一致)")
    print("[step2] OK")

    # ───────────────────────────────────────────
    # Step 3: 残り
    # ───────────────────────────────────────────
    print(f"\n[step3] 残り {len(decisions) - 1} 件")
    cur.execute("BEGIN")
    try:
        for d in decisions[1:]:
            cur.execute(
                """UPDATE ebay_listings
                   SET primary_market = ?,
                       market_analysis_at = ?
                   WHERE ebay_item_id = ?""",
                (d["final_market"], datetime.now().isoformat(), d["ebay_item_id"]),
            )
            cur.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, previous_market, proposed_market, final_market,
                    action, decided_at, reason, reviewer)
                   VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?)""",
                (d["sku"], d["ebay_item_id"], d["previous_market"],
                 d["final_market"], d["final_market"],
                 datetime.now().isoformat(), d["reason"], REVIEWER),
            )
            cur.execute(
                "DELETE FROM pending_market_changes WHERE ebay_item_id = ?",
                (d["ebay_item_id"],),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! step3 exception: {type(e).__name__}: {e} !!!")
        raise

    # ───────────────────────────────────────────
    # Step 4: 検証
    # ───────────────────────────────────────────
    print("\n[step4] 検証")
    print("ebay_listings.primary_market 分布:")
    for r in conn.execute("SELECT primary_market, COUNT(*) FROM ebay_listings GROUP BY primary_market").fetchall():
        print(f"  {r[0]!r}: {r[1]}")

    nul_left = conn.execute("SELECT COUNT(*) FROM ebay_listings WHERE primary_market IS NULL").fetchone()[0]
    print(f"\nNULL 残: {nul_left} 件 (期待 0)")
    pmc_left = conn.execute("SELECT COUNT(*) FROM pending_market_changes").fetchone()[0]
    print(f"pending_market_changes 残: {pmc_left} 件 (期待 0)")

    decisions_added = conn.execute(
        "SELECT COUNT(*) FROM market_strategy_decisions WHERE reviewer=?",
        (REVIEWER,),
    ).fetchone()[0]
    print(f"decision rows added: {decisions_added} (期待 {len(decisions)})")

    print("\n" + "=" * 60)
    print("- 使用モデル: rule-based fastpath script (no LLM)")
    print(f"- 検証経路: DB SELECT (primary_market 全件確定 + pending list 0)")
    print(f"- 実機ログ: snapshot {backup_path.name}")
    print("- 残リスク: 24h 以内 retrospective code-reviewer (Q2)")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
