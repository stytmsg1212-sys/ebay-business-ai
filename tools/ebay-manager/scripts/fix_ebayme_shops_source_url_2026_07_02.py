#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot: ebayme_2JNdrTRFyn3rAX2enYZwkZ の source_url を修正 (2026-07-02).

対象: ebay_listings.sku = 'ebayme_2JNdrTRFyn3rAX2enYZwkZ' (1 件のみ確認済み)
問題: 修正コード適用前の壊れた URL が両テーブルに残っており、
  - ebay_listings.source_url : 前回 raw UPDATE 実行で shops URL に修正済 (2026-07-02)
  - monitored_items.source_url: `https://jp.mercari.com/item/m2JN...` (404 通常メルカリURL)
    → 在庫チェックは monitored_items 側 (get_active_items) を読むので誤 URL のまま。
修正後:
  - 両テーブルの source_url = 'https://jp.mercari.com/shops/product/2JN...'
  - monitored_items.site_config_id = ebayMS_ (メルカリショップ設定)
  - ebay_listings.source_status='unknown' 等リセット (次回 inventory_check で再評価)

方針:
  raw UPDATE は使わず、既存の高水準関数 `update_ebay_listing_sku(eid, sku)` を再利用。
  同関数は同一 SKU を渡しても以下を 1 トランザクションで原子的に実行する:
    (a) ebay_listings.source_url = COALESCE(_build_source_url_from_sku(sku), source_url)
        (T2c 修正済みで shops URL を返す)
    (b) source_status='unknown', source_last_checked=NULL,
        source_out_of_stock_since=NULL, risk_confirmed=0
    (c) _sync_monitored_items_sku(): ebay_listings.source_url を monitored_items に
        mirror (source_url_manual=0 のみ、今回対象は 0 で対象)。
        site_config_id も find_site_config_by_sku(sku) 経由で ebayMS_ に更新される。

db-migration-rules 6-step 厳守:
  Step 1: SELECT で両テーブル before dump (rollback 用 snapshot)
  Step 2: update_ebay_listing_sku を 1 件に絞って実行
  Step 3: (Step 2 で 1 件のみ対応)
  Step 4: 両テーブル SELECT で after 確認
  Step 5: 実行ログ (before/after) を stdout に出力
  Step 6: rollback SQL を stdout に出力

実行方法:
  cd C:\\Users\\gucch\\projects\\claude\\tools\\ebay-manager
  python scripts/fix_ebayme_shops_source_url_2026_07_02.py

  --dry-run オプションで update_ebay_listing_sku を呼ばず SELECT 確認のみ。
"""
from __future__ import annotations

import sys
import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

TARGET_SKU = "ebayme_2JNdrTRFyn3rAX2enYZwkZ"
TARGET_ITEM_ID = "2JNdrTRFyn3rAX2enYZwkZ"
EXPECTED_URL = f"https://jp.mercari.com/shops/product/{TARGET_ITEM_ID}"


def _fetch_state(conn, target_eid: str) -> dict:
    """両テーブルの現状を dump (rollback 用 snapshot)"""
    el = conn.execute(
        """SELECT ebay_item_id, sku, source_url, source_status,
                  source_last_checked, source_out_of_stock_since, risk_confirmed,
                  COALESCE(source_url_manual, 0) AS source_url_manual
             FROM ebay_listings WHERE ebay_item_id=?""",
        (target_eid,),
    ).fetchone()
    mi = conn.execute(
        """SELECT id, ebay_item_id, sku, source_url, site_config_id,
                  COALESCE(source_url_manual, 0) AS source_url_manual, is_active
             FROM monitored_items WHERE ebay_item_id=?""",
        (target_eid,),
    ).fetchone()
    return {
        "ebay_listings": dict(el) if el else None,
        "monitored_items": dict(mi) if mi else None,
    }


def main(dry_run: bool = False) -> None:
    from monitor.database import (
        get_conn, init_db,
        update_ebay_listing_sku, find_site_config_by_sku,
    )
    from sku_mapping_manager import generate_url, is_mercari_shops_item_id

    # 事前検証: ヘルパー / site_config が正しくショップ設定を返すことを確認
    assert is_mercari_shops_item_id(TARGET_ITEM_ID), (
        f"{TARGET_ITEM_ID} が is_mercari_shops_item_id=True でない (コード修正漏れ)"
    )
    assert generate_url("ebayme_", TARGET_ITEM_ID) == EXPECTED_URL
    _cfg = find_site_config_by_sku(TARGET_SKU)
    assert _cfg is not None and _cfg.get("convert_url") == "ebayMS_", (
        f"find_site_config_by_sku が ebayMS_ 設定を返さない: {_cfg}"
    )
    logger.info("[check] helpers OK: URL=%s / site=%s (id=%s)",
                EXPECTED_URL, _cfg.get("site_name"), _cfg.get("id"))
    expected_site_cfg_id = _cfg["id"]

    init_db()

    with get_conn() as conn:
        # Step 1: 対象行 SELECT dump (両テーブル、rollback 用)
        # SKU で当たり数を確認 (SKU で複数 hit するのは異常 = sku-rules 違反疑い)
        eid_rows = conn.execute(
            "SELECT ebay_item_id FROM ebay_listings WHERE sku=?",
            (TARGET_SKU,),
        ).fetchall()
        if not eid_rows:
            logger.warning("[Step1] 対象 SKU が ebay_listings に存在せず: %s", TARGET_SKU)
            return
        if len(eid_rows) != 1:
            logger.error(
                "[Step1] 対象 SKU が %d 件ヒット。1 件を期待していたので中止。"
                "ebay_item_id 単位で個別対応が必要。",
                len(eid_rows),
            )
            for r in eid_rows:
                logger.error("  hit ebay_item_id=%s", r[0])
            return
        target_eid = eid_rows[0][0]

        before = _fetch_state(conn, target_eid)
        logger.info("[Step1] before ebay_listings: %s",
                    json.dumps(before["ebay_listings"], ensure_ascii=False))
        logger.info("[Step1] before monitored_items: %s",
                    json.dumps(before["monitored_items"], ensure_ascii=False))

        # 事前想定チェック (両テーブル source_url_manual=0 が期待)
        el_before = before["ebay_listings"]
        mi_before = before["monitored_items"]
        if el_before and int(el_before.get("source_url_manual", 0)) == 1:
            logger.warning(
                "[Step1] ebay_listings.source_url_manual=1 = update_ebay_listing_sku は "
                "source_url を維持する。手動 URL 設計上正しい挙動なので、想定と異なる場合は user 確認要"
            )
        if mi_before and int(mi_before.get("source_url_manual", 0)) == 1:
            logger.warning(
                "[Step1] monitored_items.source_url_manual=1 = _sync_monitored_items_sku は "
                "source_url を維持する。想定と異なる場合は user 確認要"
            )

        if dry_run:
            logger.info(
                "[DRY RUN] update_ebay_listing_sku(%r, %r) をスキップ",
                target_eid, TARGET_SKU,
            )
            return

    # Step 2 (= Step 3): 高水準関数を 1 件だけ呼び出し (1 トランザクション、内部で両テーブル反映)
    #                     with get_conn() を関数側で開くので、上の conn は Step 1 で閉じる。
    logger.info(
        "[Step2] update_ebay_listing_sku(%r, %r) 実行 (same SKU で URL 再構築)",
        target_eid, TARGET_SKU,
    )
    update_ebay_listing_sku(target_eid, TARGET_SKU)

    # Step 4: SELECT で after 確認
    with get_conn() as conn2:
        after = _fetch_state(conn2, target_eid)
    logger.info("[Step4] after  ebay_listings: %s",
                json.dumps(after["ebay_listings"], ensure_ascii=False))
    logger.info("[Step4] after  monitored_items: %s",
                json.dumps(after["monitored_items"], ensure_ascii=False))

    # 期待値 assert (どれか失敗したら raise)
    el_after = after["ebay_listings"]
    mi_after = after["monitored_items"]
    assert el_after["source_url"] == EXPECTED_URL, (
        f"[Step4] ebay_listings.source_url 期待値と不一致: got={el_after['source_url']!r}"
    )
    assert el_after["source_status"] == "unknown", (
        f"[Step4] ebay_listings.source_status リセットされていない: got={el_after['source_status']!r}"
    )
    assert el_after["source_last_checked"] is None, (
        f"[Step4] ebay_listings.source_last_checked リセット漏れ: got={el_after['source_last_checked']!r}"
    )
    assert el_after["source_out_of_stock_since"] is None, (
        f"[Step4] ebay_listings.source_out_of_stock_since リセット漏れ: got={el_after['source_out_of_stock_since']!r}"
    )
    assert int(el_after["risk_confirmed"]) == 0, (
        f"[Step4] ebay_listings.risk_confirmed リセット漏れ: got={el_after['risk_confirmed']!r}"
    )
    assert mi_after["source_url"] == EXPECTED_URL, (
        f"[Step4] monitored_items.source_url 期待値と不一致: got={mi_after['source_url']!r}"
    )
    assert mi_after["site_config_id"] == expected_site_cfg_id, (
        f"[Step4] monitored_items.site_config_id が ebayMS_ ({expected_site_cfg_id}) でない: "
        f"got={mi_after['site_config_id']!r}"
    )

    # Step 5/6: 実行ログ + rollback SQL
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "executed_at": now,
        "script": "fix_ebayme_shops_source_url_2026_07_02.py",
        "ebay_item_id": target_eid,
        "sku": TARGET_SKU,
        "before": before,
        "after": after,
        "dry_run": False,
    }
    print("[DONE]", json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("[Step5] 実行ログ出力完了")
    # rollback SQL (手動 restore 用、通常は不要)
    _el_b = before["ebay_listings"]
    _mi_b = before["monitored_items"]
    logger.info(
        "[Step6] rollback (ebay_listings): "
        "UPDATE ebay_listings SET source_url=?, source_status=?, source_last_checked=?, "
        "source_out_of_stock_since=?, risk_confirmed=? WHERE ebay_item_id=?",
    )
    logger.info(
        "         params=(%r, %r, %r, %r, %r, %r)",
        _el_b["source_url"], _el_b["source_status"], _el_b["source_last_checked"],
        _el_b["source_out_of_stock_since"], _el_b["risk_confirmed"], target_eid,
    )
    logger.info(
        "[Step6] rollback (monitored_items): "
        "UPDATE monitored_items SET source_url=?, site_config_id=? WHERE ebay_item_id=?",
    )
    logger.info(
        "         params=(%r, %r, %r)",
        _mi_b["source_url"], _mi_b["site_config_id"], target_eid,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix ebayme_ shops source_url via update_ebay_listing_sku")
    parser.add_argument("--dry-run", action="store_true", help="SELECT のみで UPDATE しない")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
