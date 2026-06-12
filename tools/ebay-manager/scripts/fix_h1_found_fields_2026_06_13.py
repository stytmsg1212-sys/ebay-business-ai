# -*- coding: utf-8 -*-
"""H1 補正 one-shot (2026-06-13 retrospective review NEEDS-FIX 対応).

対象: fix_low_match_awaiting_2026_06_11.py が再キュー時に found_* を温存した 3 件
  - rc 36 (draft_generated): found_url=g1232887923 (シルバー文字盤の別商品 ¥3,960)
                             → draft 26 の supplier_url / supplier_price_jpy にも伝播済
  - rc 58 (gate_rejected):   found_url=b1185469368 (¥640 カタログ)
  - rc 65 (gate_rejected):   found_url=o1232752482 (¥8,800 別モデル Oakley)

処置:
  1. research_candidates 3 行 → clear_found_fields() (正規 API、found_url/価格/状態クリア)
  2. listing_drafts id=26 → supplier_url / supplier_price_jpy を NULL 化
     (draft 本体は user 返答待ちのため残す。仕入先情報だけが別商品で虚偽)

Q2 6-step: snapshot → 1 件試行 → 残り → SELECT 検証。既定 dry-run、--apply で実行。
rollback: 本 script の snapshot json + backup_low_match_awaiting_20260611_232034.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import get_conn  # noqa: E402
from monitor.research_candidates_db import clear_found_fields  # noqa: E402

RC_IDS = [58, 65, 36]  # 58 = 1 件試行枠
DRAFT_ID = 26
SNAP = BASE / "data" / f"backup_h1_found_fields_{datetime.now():%Y%m%d_%H%M%S}.json"


def snapshot() -> None:
    with get_conn() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rcs = [dict(r) for r in conn.execute(
            "SELECT * FROM research_candidates WHERE rc_id IN (36, 58, 65)")]
        drafts = [dict(r) for r in conn.execute(
            "SELECT * FROM listing_drafts WHERE id=?", (DRAFT_ID,))]
    SNAP.write_text(
        json.dumps({"research_candidates": rcs, "listing_drafts": drafts},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"snapshot: {SNAP.name} (rc={len(rcs)} draft={len(drafts)})")


def verify() -> bool:
    ok = True
    with get_conn() as conn:
        for rc_id, url, price in conn.execute(
                "SELECT rc_id, found_url, found_price_jpy FROM research_candidates "
                "WHERE rc_id IN (36, 58, 65)"):
            clean = url is None and price is None
            print(f"  rc {rc_id}: found_url={url} price={price} -> "
                  f"{'CLEAN' if clean else 'CONTAMINATED'}")
            ok = ok and clean
        row = conn.execute(
            "SELECT supplier_url, supplier_price_jpy FROM listing_drafts WHERE id=?",
            (DRAFT_ID,)).fetchone()
        clean = row is not None and row[0] is None and row[1] is None
        print(f"  draft {DRAFT_ID}: supplier_url={row[0]} price={row[1]} -> "
              f"{'CLEAN' if clean else 'CONTAMINATED'}")
        ok = ok and clean
    return ok


def main(apply: bool) -> None:
    snapshot()
    if not apply:
        print("dry-run: 変更なし。--apply で実行")
        verify()
        return

    # Step 2: 1 件試行 (rc 58)
    ok = clear_found_fields(58)
    print(f"trial rc 58 clear_found_fields: {ok}")
    if not ok:
        print("ABORT: 試行失敗")
        sys.exit(1)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT found_url FROM research_candidates WHERE rc_id=58").fetchone()
    if row[0] is not None:
        print("ABORT: rc 58 の found_url が残存")
        sys.exit(1)

    # Step 3: 残り
    for rc_id in (65, 36):
        ok = clear_found_fields(rc_id)
        print(f"rc {rc_id} clear_found_fields: {ok}")
        if not ok:
            print(f"ABORT: rc {rc_id} 失敗 — snapshot から rollback 要")
            sys.exit(1)

    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listing_drafts SET supplier_url=NULL, supplier_price_jpy=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND supplier_url IS NOT NULL",
            (DRAFT_ID,))
        print(f"draft {DRAFT_ID} supplier クリア: rowcount={cur.rowcount}")
        if cur.rowcount != 1:
            print("WARN: rowcount != 1 (既にクリア済 or 不在) — verify で確認")

    # Step 4: 検証
    print("verify:")
    print("RESULT:", "OK" if verify() else "NG")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
