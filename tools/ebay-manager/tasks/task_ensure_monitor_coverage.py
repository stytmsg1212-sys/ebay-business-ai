#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W139: 監視台帳 (monitored_items) カバレッジ自動補完.

本番事故 (2026-05-18, item 358487417178 ebayyh_m1227590760 が monitored_items
未登録で仕入先OOS検知不能 → 履行不能 US 注文 07-14655-19832) の恒久対策。

根本原因: inventory_check の真実源は monitored_items のみ。eBay 出品を
ebay_listings に取り込む scheduled `ebay_sync` は monitored_items を登録せず、
登録経路は MonoDeck 手動UI 2 経路 + supplier_apply のみ。そこを通らない
無在庫出品は永久に在庫監視されない構造欠陥 (Codex 独立診断も同一結論)。

対策: 本タスクを execute_daily_tasks の **ebay_sync の後、inventory_check の
前** に毎 main batch 実行し、active な無在庫出品で monitored_items 未登録の
ものを自動登録する (= eBay 真実 → 監視台帳 の自動リコンシリエーション)。

設計上の不変条件 (Codex review 反映):
  - 対象 = is_ended=0 ∧ quantity_ebay>=1 ∧ 無在庫SKU (sku LIKE 'ebay%')。
    qty=0/ended は「既に販売停止 = RISK でない」(2026-04-20 業務確認) ので対象外。
  - listing 識別は ebay_item_id 主導。SKU は無在庫判定 + URL 変換のみ
    (.claude/rules/sku-rules.md)。登録は upsert_item に委譲 (ebay_item_id →
    source_url の順で identify、source_url 単位集約を維持)。
  - 監視カバレッジ有無の判定キーは **ebay_item_id** (sku-rules.md 準拠、
    listing 一意 ID。`m.sku = l.sku` 結合は禁止パターン)。
    2026-05-18 事故: 旧実装は `NOT EXISTS WHERE m.sku=l.sku` だったため、
    user の MonoDeck SKU 編集で ebay_listings.sku だけ更新され
    monitored_items が旧 sku のまま残り → 監視中の listing を phantom gap と
    誤検知 → 非dedupe Discord 緊急通知爆発 + monitored 汚染。対策 =
    ebay_item_id キー化 + update_ebay_listing_sku/upsert_ebay_listing の
    monitored 追従 (_sync_monitored_items_sku) + ebay_item_id backfill。
  - source_url 生成不能 / site_config prefix 未登録 (例 ebayYF) は **登録不可**。
    silent skip せず DLQ として件数 + SKU を返す (Q0)。健全性チェック側
    (task_scheduler_health_check) がこれを Discord 可視化する。
  - policy B (user 確定 2026-05-18): OOS 検知時の自動販売停止はしない。
    本タスクは「監視対象に載せる」だけ。OOS の検知 → アラート → 代替仕入先
    探索 → MonoDeck 表示 は既存 inventory_check 経路がそのまま担う
    (既存システムに auto stop-sell は無く policy B は既存挙動で充足)。
  - 冪等: 登録済は対象に入らない (NOT EXISTS by ebay_item_id)。2 回連続
    実行で registered=0 (upsert_item が ebay_item_id 一致行を UPDATE)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def find_coverage_gaps() -> dict:
    """現在の監視カバレッジ欠落を算出 (ステートレス、副作用なし).

    Returns:
        {
          'coverable':  [ {ebay_item_id, sku, title, quantity_ebay}, ... ],
              # 未登録だが source_url 生成可能 = 本タスクが登録すべき対象
          'dlq':        [ {ebay_item_id, sku, title, quantity_ebay}, ... ],
              # 未登録 かつ source_url 生成不能 (prefix 未登録) = 手動/site_config 要
        }
    health_check (Component B) と本タスク (Component A) の両方が使う共有判定。
    """
    import sqlite3
    from monitor.database import get_conn, build_source_url, find_site_config_by_sku
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        # code-reviewer MEDIUM (2026-05-18): NULL 防御。anti-silent-skip
        # 機能自身が NULL で盲点を作らないよう、コードベース既存の
        # COALESCE 防御パターンに統一する:
        #   - is_ended NULL = 「終了でない」= active 扱い → 監視対象に入れる
        #     (NULL を取りこぼすと「未登録なのに coverable に出ない」silent gap)
        #   - quantity_ebay: 明示 0 のみ除外 (= 確実に販売停止済 = RISK でない、
        #     2026-04-20 業務確認)。NULL は「数量不明 = 売れている可能性」=
        #     安全側で監視対象に入れる (qty 同期失敗で再盲点化を防ぐ)。
        # W139-fix (2026-05-18): カバレッジ判定キーは ebay_item_id
        # (sku-rules.md: listing 識別は ebay_item_id、m.sku=l.sku 結合は禁止)。
        # 旧実装 (m.sku=l.sku) は SKU 編集で monitored が旧 sku に取り残され
        # 監視中 listing を phantom gap 誤検知した (本事故の根本原因)。
        # m.ebay_item_id 空行は「カバレッジなし」扱い = backfill で充填、
        # 残存は cleanup (孤立行 is_active=0)。上記 NULL 防御は維持。
        # W139-fix HIGH-2 (Codex 2026-05-19): COALESCE(m.is_active,1)=1 で
        # 「カバー済」= active な monitored 行が在ること、と定義する。
        # inventory_check は is_active=1 のみ処理 (get_active_items) するため、
        # is_active=0 行 (cleanup 降格等) を「監視済」と数えると active 無在庫
        # listing が実際は未監視なのに gap に出ない silent unmonitored になる。
        rows = conn.execute(
            """SELECT l.ebay_item_id, l.sku, l.title, l.quantity_ebay
               FROM ebay_listings l
               WHERE COALESCE(l.is_ended, 0) = 0
                 AND (l.quantity_ebay IS NULL OR l.quantity_ebay >= 1)
                 AND l.sku LIKE 'ebay%'
                 AND NOT EXISTS (SELECT 1 FROM monitored_items m
                                 WHERE m.ebay_item_id = l.ebay_item_id
                                   AND m.ebay_item_id IS NOT NULL
                                   AND m.ebay_item_id <> ''
                                   AND COALESCE(m.is_active, 1) = 1)
               ORDER BY l.created_at""").fetchall()
    coverable, dlq = [], []
    for r in rows:
        sku = r["sku"]
        try:
            url = build_source_url(sku)
        except Exception as e:  # noqa: BLE001 — 生成失敗は DLQ 行きで Q0 可視
            logger.warning(f"build_source_url 失敗 sku={sku}: {e}")
            url = None
        cfg = find_site_config_by_sku(sku)
        rec = {
            "ebay_item_id": r["ebay_item_id"],
            "sku": sku,
            "title": r["title"],
            "quantity_ebay": r["quantity_ebay"],
        }
        if url and cfg:
            coverable.append(rec)
        else:
            dlq.append(rec)
    return {"coverable": coverable, "dlq": dlq}


def run_ensure_monitor_coverage(config: dict) -> dict:
    """active 無在庫出品で monitored_items 未登録のものを自動登録する.

    Returns (run_task 契約):
        {
          'success': bool,
          'scanned': int,        # 未登録だった総数 (coverable + dlq)
          'registered': int,     # 今回 monitored_items に載せた数
          'failed': int,         # 登録試行が例外で失敗した数
          'dlq': int,            # source_url 生成不能で登録できなかった数
          'dlq_skus': list[str], # DLQ の SKU (Q0: 明示報告、健全性チェック連携)
          'message': str,
        }
    """
    from monitor.database import upsert_item

    logger.info("【開始】監視台帳カバレッジ自動補完 (W139)")
    gaps = find_coverage_gaps()
    coverable, dlq = gaps["coverable"], gaps["dlq"]
    scanned = len(coverable) + len(dlq)

    registered = 0
    failed = 0
    for rec in coverable:
        try:
            mid = upsert_item(
                sku=rec["sku"],
                ebay_item_id=rec["ebay_item_id"],
                title=rec["title"] or "",
            )
            registered += 1
            logger.info(
                f"  [coverage] 登録 monitored_items.id={mid} "
                f"{rec['ebay_item_id']} sku={rec['sku']}"
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.error(
                f"  [coverage] 登録失敗 {rec['ebay_item_id']} "
                f"sku={rec['sku']}: {e}",
                exc_info=True,
            )

    dlq_skus = [d["sku"] for d in dlq]
    if dlq:
        # Q0: silent skip 禁止。登録できない盲点を必ず痕跡化
        # (Discord 可視化は task_scheduler_health_check 側 = Component B)。
        logger.warning(
            f"  [coverage][DLQ] source_url 生成不能で登録不可 {len(dlq)} 件 "
            f"(site_config prefix 未登録、手動/site_config 対応要): "
            f"{', '.join(dlq_skus)}"
        )

    # success 判定: 登録試行の例外 (failed) があれば False
    # (silent-skip-prevention: 失敗を success に偽装しない)。
    # scanned=0 (補完対象なし) は正常 = success True。
    # DLQ は「登録不可だが検出・報告済」= 本タスクの失敗ではない
    # (恒久解消は site_config 追加 = W139 別 sub / 健全性チェックが警告継続)。
    success = (failed == 0)
    msg = (
        f"scanned={scanned} registered={registered} failed={failed} "
        f"dlq={len(dlq)}"
        + (f" dlq_skus=[{', '.join(dlq_skus)}]" if dlq_skus else "")
    )
    logger.info(f"監視台帳カバレッジ自動補完 完了: {msg}")
    return {
        "success": success,
        "scanned": scanned,
        "registered": registered,
        "failed": failed,
        "dlq": len(dlq),
        "dlq_skus": dlq_skus,
        "message": msg,
    }
