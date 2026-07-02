#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W308: 自社セラーの自己マッチ遮断 — 既存 listing_rival_discoveries 一括 dismiss (one-shot).

背景 (main が SQL で確認済み、2026-07-02):
  `listing_rival_discoveries` に自社ストアの出品が競合として 77 件記録されていた
  (competitor_seller='mono_honpo_japan'、competitor_item_id 全件が自社
  `ebay_listings.ebay_item_id` と一致)。W301 の分類エンジン (`monitor/rival_classifier.py`)
  にハード除外 `self_listing` を追加した (本タスクの実装 1) ため、明日以降の
  rival_classify 実行では新規混入分は自動で noise/dismissed に落ちる。
  本 script は **過去に混入済みの残存分** を一括処理する one-shot。

対象抽出: `listing_rival_discoveries.competitor_item_id` が自社
  `ebay_listings.ebay_item_id` に実在する行 (item_id 一致 = 100% 自社出品、
  セラー名には依存しない decisive 判定。rival_classifier.py の hard-exclude と
  同じロジック)。うち `status != 'dismissed'` の行のみを対象にする
  (既に dismissed 済みの行は不変、二重処理しない)。

status 値: `listing_rival_discoveries.status` の既存許容値は
  `monitor.database.update_rival_discovery_status()` が定義する
  'new' / 'monitoring_added' / 'dismissed' の 3 値のみ (独自の 'dismissed_self'
  等は作らない、既存の流儀に合わせる)。本 script は 'dismissed' を使い、
  既存の `update_rival_discovery_status()` (行単位 UPDATE + status_changed_at
  更新、監査済み経路) をそのまま再利用する。

SKU 規約: 本 script は SKU を一切参照しない (listing 識別は ebay_item_id /
  competitor_item_id のみ、sku-rules.md 準拠)。

⚠️ 本 script は作成 (+ テスト) のみ。--apply による本番実行判断は main / user に
  委ねる (このタスクでは --apply を実行しない)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# scripts/ 直下実行時に repo(tools/ebay-manager) を import path へ (backfill_w301 と同パターン)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn, update_rival_discovery_status

_TARGET_QUERY = """
SELECT
    lrd.id AS discovery_id,
    lrd.ebay_item_id,
    lrd.competitor_item_id,
    lrd.competitor_seller,
    lrd.competitor_title,
    lrd.status,
    el.title AS our_title
FROM listing_rival_discoveries lrd
JOIN ebay_listings el ON el.ebay_item_id = lrd.competitor_item_id
WHERE lrd.status != 'dismissed'
ORDER BY lrd.id
"""


def _fetch_self_discoveries(limit: int = 0) -> list[dict]:
    """competitor_item_id が自社出品と一致し、まだ dismissed でない行を返す。"""
    with get_conn() as conn:
        rows = conn.execute(_TARGET_QUERY).fetchall()
    targets = [dict(r) for r in rows]
    if limit:
        targets = targets[:limit]
    return targets


def _run(*, apply: bool, limit: int) -> dict:
    targets = _fetch_self_discoveries(limit=limit)

    by_status: dict = {}
    for t in targets:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1

    summary: dict = {
        "apply": apply,
        "target_count": len(targets),
        "by_status_before": by_status,
    }

    if not apply:
        # dry-run: DB 書込ゼロ、対象の中身を確認できるよう先頭 10 件を例示。
        summary["sample"] = [
            {
                "discovery_id": t["discovery_id"],
                "ebay_item_id": t["ebay_item_id"],
                "competitor_item_id": t["competitor_item_id"],
                "competitor_seller": t["competitor_seller"],
                "our_title": t["our_title"],
                "status": t["status"],
            }
            for t in targets[:10]
        ]
        return summary

    dismissed = 0
    failed: list[int] = []
    for t in targets:
        ok = update_rival_discovery_status(t["discovery_id"], "dismissed")
        if ok:
            dismissed += 1
        else:
            failed.append(t["discovery_id"])

    summary["dismissed_count"] = dismissed
    summary["failed_ids"] = failed
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "W308: listing_rival_discoveries に混入した自社出品 (自己マッチ) を"
            "一括 dismiss (one-shot)"
        )
    )
    ap.add_argument("--db", default=None, help="対象 DB path (未指定は data/monitor.db)")
    ap.add_argument("--limit", type=int, default=0, help="0=全件、N=先頭N件のみ (段階実行用)")
    ap.add_argument(
        "--apply", action="store_true",
        help="未指定なら dry-run (DB 書込ゼロ、対象件数/サンプル表示のみ)",
    )
    args = ap.parse_args()

    if args.db:
        import monitor.database as db_mod
        db_mod.DB_PATH = Path(args.db)

    summary = _run(apply=args.apply, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"[dismiss_self_discoveries_w308] elapsed {time.time()-t0:.1f}s")
