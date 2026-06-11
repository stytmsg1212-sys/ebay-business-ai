#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W258/Phase-B: ebay_listings.ebay_image_url backfill (one-shot).

対象: ebay_listings WHERE COALESCE(is_ended,0)=0
       AND (ebay_image_url IS NULL OR ebay_image_url='')

取得方法:
  - monitor.ebay_listing_image.get_ebay_image_url を再利用 (GetItem→PictureURL)
  - 既存関数が DB cache + API 取得を担う。本スクリプトは呼ぶだけ。
  - 新規 API 実装は書かない (K1: 既存機能の再利用)

resume state: data/backfill_ebay_images_state.json に処理済 ebay_item_id を逐次追記。
再実行時は同ファイルを読んでスキップ。

100 件/batch で区切り、batch ごとに成功/失敗カウントを print。
--apply なしは dry-run (対象件数表示のみ)。

Q2 6-step:
  1. 対象件数 SELECT
  2. snapshot: data/backup_ebay_images_YYYYMMDD_HHMMSS.json (対象 ebay_item_id 一覧)
  3. --apply なしは dry-run 既定
  4. 実行はしない (main agent が Q2 6-step で実施)
  5. init_db 非接触 (one-shot)

使い方:
  python scripts/backfill_ebay_images_2026_06_11.py           # dry-run
  python scripts/backfill_ebay_images_2026_06_11.py --apply   # 実書込 (100件/batch)
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
from monitor.ebay_listing_image import get_ebay_image_url  # noqa: E402

_STATE_PATH = PROJECT_ROOT / "data" / "backfill_ebay_images_state.json"
_BATCH_SIZE = 100


def _load_done_eids() -> set[str]:
    """resume state: 処理済 ebay_item_id を set で返す。"""
    if not _STATE_PATH.exists():
        return set()
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("done", []))
    except (OSError, ValueError, KeyError):
        return set()


def _save_done_eids(done: set[str]) -> None:
    """resume state を保存 (逐次追記)。"""
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ebay_listings.ebay_image_url backfill"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="実際に GetItem 取得 + DB 書込を実行する (既定は dry-run)"
    )
    args = parser.parse_args()
    dry_run = not args.apply

    # Step 1: 対象行
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, title
               FROM ebay_listings
               WHERE COALESCE(is_ended,0)=0
                 AND (ebay_image_url IS NULL OR ebay_image_url='')
               ORDER BY ebay_item_id"""
        ).fetchall()

    if not rows:
        print("[backfill_ebay] 対象行なし。処理スキップ。")
        return

    print(f"[backfill_ebay] 対象: {len(rows)} 件 (active + ebay_image_url 未取得)")

    # Step 2: snapshot
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = PROJECT_ROOT / "data" / f"backup_ebay_images_{ts}.json"
    snapshot = [{"ebay_item_id": r[0], "title": r[1]} for r in rows]
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[backfill_ebay] snapshot 保存: {backup_path}")

    for r in rows[:5]:
        print(f"  eid={r[0]}  title={str(r[1] or '')[:60]!r}")
    if len(rows) > 5:
        print(f"  ... (他 {len(rows) - 5} 件)")

    if dry_run:
        print("[backfill_ebay] dry-run モード: 書込なし。--apply で実書込。")
        return

    # Step 3: resume state 読込
    done_eids = _load_done_eids()
    target = [r for r in rows if r[0] not in done_eids]
    print(f"[backfill_ebay] resume: 処理済={len(done_eids)} / 残={len(target)}")

    # Step 4: 100 件/batch で処理
    ok_total = 0
    fail_total = 0
    batch_num = 0

    for batch_start in range(0, len(target), _BATCH_SIZE):
        batch = target[batch_start: batch_start + _BATCH_SIZE]
        batch_num += 1
        ok_count = 0
        fail_count = 0

        for eid, title in batch:
            img_url = get_ebay_image_url(eid)
            if img_url:
                ok_count += 1
                # 成功のみ done に積む (失敗を done 化すると一時的な API 失敗が
                # 再実行で恒久スキップされる / code-reviewer MEDIUM-2)
                done_eids.add(eid)
            else:
                print(f"[backfill_ebay] WARN: eid={eid} 画像取得失敗 (title={str(title or '')[:40]!r})")
                fail_count += 1

        # batch 完了後に resume state を保存
        _save_done_eids(done_eids)
        ok_total += ok_count
        fail_total += fail_count
        print(
            f"[backfill_ebay] batch {batch_num}: "
            f"ok={ok_count} fail={fail_count} / {len(batch)} 件 "
            f"(累計 ok={ok_total} fail={fail_total})"
        )

    print(
        f"[backfill_ebay] 完了: 成功={ok_total} / 失敗={fail_total} / 合計={len(target)}"
    )


if __name__ == "__main__":
    main()
