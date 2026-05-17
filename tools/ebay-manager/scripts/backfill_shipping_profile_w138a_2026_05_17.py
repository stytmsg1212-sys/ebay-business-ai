"""W138-A (2026-05-17): shipping_profile_id 一括バックフィル one-shot.

目的: 全 active listing (~580、有/無在庫問わず) へ eBay GetItem の
SellerProfiles/SellerShippingProfile から取得した shipping_profile_id と
取得時刻 (shipping_profile_fetched_at) を投入し、商品管理 hero の
「BP 最初から自動表示」(W138-A) を初期化する。以降は 📤eBay反映時
_sync_db_to_actual / 単発「↻ 再取得」/ で維持される。

HIGH-1 (鮮度非対称): GetMyeBaySelling は BP を返さないため定期
task_ebay_sync に相乗りできず、本 backfill が初期母集団を作る。

HIGH-2 (NULL 多義性): GetItem 成功 & SellerProfiles 不在 = 確定 Inline
→ shipping_profile_id は明示 NULL、fetched_at は now (= 状態 b)。
GetItem 失敗 = 取得できていない → **fetched_at を書かない** (= 状態 a
「未取得」、Inline と誤断定しない)。skip 明細を JSON 保存。

Q2 / db-migration-rules 6-step 準拠:
  1. init_db を一切変更しない (本 one-shot のみ)。DROP/DELETE/ALTER なし
     (列は migration v41 で作成済)。
  2. 実行前に対象を SELECT で snapshot (rollback 用、JSON 保存)。
  3. dry-run (--apply 無し): 全 active を **GetItem read-only** で叩き
     件数・GetItem 成功率・(BP/確定Inline/err) 分布を計測する (DoD が
     dry-run での成功率確認を要求するため意図的)。**DB 書込のみ skip**
     (eBay write は元々ゼロ)。所要 ~580×~2s sleep ≈ 20 分超。
  4. UPDATE は `WHERE ebay_item_id=? AND shipping_profile_fetched_at
     IS NULL` で **write-time guard** (Codex#2): SELECT→GetItem→UPDATE
     の間に user が ↻/📤反映 で先に更新した行を上書きしない (TOCTOU 封鎖)。
     再実行で取得済 skip = 冪等 resume。
  5. apply 後 DB SELECT で (a)/(b)/(c) 分布・投入率を再確認。
  6. 実行後 24h 以内に retrospective code-reviewer (db-migration-rules)。

Codex#5 (scheduler 協調): apply 中は定時 task_ebay_sync を
`tasks_enabled.ebay_sync=false` で一時停止すること (kill switch、
USER_MANUAL 手順)。task_ebay_sync は本機能の 2 列を touch しない
(値破壊リスク無) が、SQLite WAL writer ロック競合回避のため。本 script
側も `database is locked` を短 retry で吸収する。

eBay は GetItem (読取専用) のみ。eBay への write は一切しない。
listing 識別は ebay_item_id (sku-rules 準拠、SKU はキー化しない)。

使い方:
  python scripts/backfill_shipping_profile_w138a_2026_05_17.py          # dry-run
  python scripts/backfill_shipping_profile_w138a_2026_05_17.py --apply  # 実書込
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")

from monitor.database import get_conn
from monitor.ebay_listing_snapshot import fetch_listing_snapshot
from monitor.inventory_sync import _get_credentials

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("w138a_backfill")

_SLEEP_BASE = 2.0          # GetItem 間隔 (anti-bot / rate、W7-A 規約準拠)
_BATCH = 50                # 進捗ログ単位 (中断耐性、再実行で残のみ)
_SNAPSHOT_DIR = Path(
    r"C:/Users/gucch/projects/claude/tools/ebay-manager/data/w138a_backfill"
)


def _update_with_retry(eid: str, bp_id, retries: int = 4) -> int:
    """write-time guard 付き UPDATE。`database is locked` を短 retry で吸収.

    戻り rowcount: 1=書込、0=既に取得済 (guard で skip、冪等 resume)。
    """
    last_err = None
    for attempt in range(retries):
        try:
            with get_conn() as c:
                cur = c.execute(
                    "UPDATE ebay_listings SET shipping_profile_id=?, "
                    "shipping_profile_fetched_at=datetime('now') "
                    "WHERE ebay_item_id=? "
                    "AND shipping_profile_fetched_at IS NULL",
                    (bp_id, eid),
                )
                return cur.rowcount
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err  # retry 尽きた = 異常 (silent skip しない)


def main(argv) -> int:
    apply = "--apply" in argv
    mode = "APPLY (実書込)" if apply else "DRY-RUN (書込まない)"
    logger.info(f"W138-A shipping_profile backfill 開始 — {mode}")
    if apply:
        logger.info(
            "※ apply 前に tasks_enabled.ebay_sync=false で定時同期を"
            "一時停止していること (Codex#5 / USER_MANUAL)。"
        )

    creds = _get_credentials()
    if not creds:
        logger.error("eBay 認証取得失敗 (中止)")
        return 1
    if creds[0] == "app_id":
        logger.error("creds がキー文字列 = dict→tuple バグ再発 (中止)")
        return 1
    app_id, dev_id, cert_id, user_token = creds

    # 対象 = 全 active listing で未取得 (fetched_at IS NULL)。有/無在庫
    # 問わず ~580 母数 (W135 の在庫 108 とは別)。再実行は残のみ = resume。
    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id, substr(title,1,40) t "
            "FROM ebay_listings "
            "WHERE (is_ended IS NULL OR is_ended=0) "
            "AND shipping_profile_fetched_at IS NULL "
            "AND ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "AND title IS NOT NULL AND title != '' "
            "ORDER BY ebay_item_id"
        ).fetchall()
    targets = [dict(r) for r in rows]
    logger.info(
        f"対象 (active / shipping_profile_fetched_at NULL): "
        f"{len(targets)} 件"
    )
    if not targets:
        logger.info("対象 0 件 — 全 active が取得済 (何もしない)")
        return 0

    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = _SNAPSHOT_DIR / (
        f"w138a-backfill-snapshot-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    snap_path.write_text(
        json.dumps(targets, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info(f"snapshot 保存: {snap_path}")

    bp_filled = inline_filled = skipped = errored = 0
    skip_log: list[dict] = []
    for i, t in enumerate(targets, 1):
        eid = t["ebay_item_id"]
        snap = fetch_listing_snapshot(
            eid, app_id, dev_id, cert_id, user_token
        )
        if not snap.ok:
            # GetItem 失敗 → fetched_at を書かない (状態 a 維持、
            # Inline 誤断定回避)。skip 明細に残す (Q0)。
            errored += 1
            skip_log.append({"ebay_item_id": eid, "reason": snap.error})
            logger.warning(
                f"[{i}/{len(targets)}] {eid} GetItem 失敗 (fetched_at "
                f"据置=未取得維持): {snap.error}"
            )
        else:
            bp_id = (str(snap.shipping_profile_id).strip()
                     if snap.shipping_profile_id else None)
            kind = "BP" if bp_id else "Inline(確定)"
            if apply:
                rc = _update_with_retry(eid, bp_id)
                if rc == 1:
                    if bp_id:
                        bp_filled += 1
                    else:
                        inline_filled += 1
                    logger.info(
                        f"[{i}/{len(targets)}] {eid} {kind} "
                        f"id={bp_id} | {t['t']}"
                    )
                else:
                    # guard で 0 = user が ↻/📤反映 で先に更新済
                    skipped += 1
                    skip_log.append(
                        {"ebay_item_id": eid,
                         "reason": f"rowcount={rc} (guard: 既に取得済)"}
                    )
                    logger.info(
                        f"[{i}/{len(targets)}] {eid} guard skip "
                        "(既に取得済 = 冪等)"
                    )
            else:
                if bp_id:
                    bp_filled += 1
                else:
                    inline_filled += 1
                logger.info(
                    f"[{i}/{len(targets)}] DRY {eid} {kind} "
                    f"id={bp_id} | {t['t']}"
                )
        if i % _BATCH == 0:
            logger.info(
                f"--- 進捗 {i}/{len(targets)} "
                f"(BP{bp_filled}/Inline{inline_filled}/"
                f"skip{skipped}/err{errored}) ---"
            )
        time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))

    logger.info(
        f"完了 [{mode}] 対象{len(targets)} / BP{bp_filled} / "
        f"Inline確定{inline_filled} / skip{skipped} / err(未取得据置){errored}"
    )
    if skip_log:
        sp = _SNAPSHOT_DIR / (
            f"w138a-backfill-skips-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        sp.write_text(
            json.dumps(skip_log, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        logger.info(f"skip/err 明細: {sp}")
    if not apply:
        logger.info("DRY-RUN でした。実書込は --apply を付けて再実行。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
