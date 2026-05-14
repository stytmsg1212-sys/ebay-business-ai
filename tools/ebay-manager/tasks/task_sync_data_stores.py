#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: データストア統合
sku_conversion_results.json のデータを monitor.db の ebay_listings に統合する。
inventory_check_results.json の在庫ステータスも ebay_listings に反映する。

これにより、2つの分離したデータストアを1つのDBに統一する。
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

from monitor.database import get_conn  # noqa: E402  WAL+busy_timeout適用で安全

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / 'data' / 'monitor.db'  # 後方互換（他モジュール参照用）


def sync_sku_conversion_to_db() -> Dict:
    """
    sku_conversion_results.json → ebay_listings テーブルに統合

    source, source_url, classification カラムを更新する。
    """
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
    if not sku_file.exists():
        logger.warning("sku_conversion_results.json が見つかりません")
        return {'updated': 0, 'not_found': 0}

    with open(sku_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = get_conn()
    updated = 0
    not_found = 0

    # sourced items
    for item in data.get('sourced', []):
        ebay_id = item.get('ebay_id', '')
        if not ebay_id:
            continue

        result = conn.execute(
            "SELECT id FROM ebay_listings WHERE ebay_item_id=?", (ebay_id,)
        ).fetchone()

        if result:
            conn.execute(
                """UPDATE ebay_listings SET source=?, source_url=?, classification='sourced'
                   WHERE ebay_item_id=?""",
                (item.get('source', ''), item.get('source_url', ''), ebay_id),
            )
            updated += 1
        else:
            not_found += 1

    # self_stock items
    for item in data.get('self_stock', []):
        ebay_id = item.get('ebay_id', '')
        if not ebay_id:
            continue

        result = conn.execute(
            "SELECT id FROM ebay_listings WHERE ebay_item_id=?", (ebay_id,)
        ).fetchone()

        if result:
            conn.execute(
                """UPDATE ebay_listings SET classification='self_stock'
                   WHERE ebay_item_id=?""",
                (ebay_id,),
            )
            updated += 1

    conn.commit()
    conn.close()

    logger.info(f"SKU変換データ統合: {updated}件更新, {not_found}件未一致")
    return {'updated': updated, 'not_found': not_found}


def sync_inventory_status_to_db() -> Dict:
    """
    inventory_check_results.json → ebay_listings テーブルに反映

    source_status, source_last_checked, source_out_of_stock_since を更新する。
    """
    inv_file = BASE_DIR / 'data' / 'inventory_check_results.json'
    if not inv_file.exists():
        logger.warning("inventory_check_results.json が見つかりません")
        return {'updated': 0, 'not_found': 0}

    with open(inv_file, 'r', encoding='utf-8') as f:
        inv_data = json.load(f)

    results = inv_data.get('results', [])
    if not results:
        return {'updated': 0, 'not_found': 0}

    conn = get_conn()
    updated = 0
    not_found = 0

    for item in results:
        sku = item.get('sku', '')  # log 用 (識別 key としては使わない、SKU rule 準拠)
        url = (item.get('url') or '').strip()
        json_eid = (item.get('ebay_id') or '').strip()

        if not url and not json_eid:
            # 識別 key 不在 → silent skip 防止のため警告 log + 計上
            not_found += 1
            logger.warning(
                f"sync_inventory_status_to_db: identifier 不在 "
                f"(url + ebay_id 両方欠落) sku={sku!r}"
            )
            continue

        status = item.get('status', '不明')
        checked_at = item.get('checked_at', '')

        # 2026-05-01 W75 4c: 旧 SKU 経由 lookup (SKU rule 違反) を解消.
        # 優先順位: (1) JSON ebay_id 直接利用 (2) source_url 逆引き
        # 旧 SQL は SKU で listing 1 件特定 → 同 SKU 多 listing (有在庫 stock*)
        # ケースで非決定論動作だった. 本データは ebay* SKU のみ (stock* は inventory_check
        # スコープ外) で実質的影響は無いが、ルール上の根本是正.
        if json_eid:
            row = conn.execute(
                "SELECT ebay_item_id, source_status, source_out_of_stock_since "
                "FROM ebay_listings WHERE ebay_item_id=?",
                (json_eid,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ebay_item_id, source_status, source_out_of_stock_since "
                "FROM ebay_listings "
                "WHERE source_url=? AND (is_ended IS NULL OR is_ended=0) "
                "LIMIT 1",
                (url,),
            ).fetchone()

        if not row:
            not_found += 1
            continue

        ebay_item_id = row[0]
        prev_status = row[1]

        # BUG-2: source_out_of_stock_since の管理は ebay_sync.match_source_status_to_ebay に一元化
        # 以前はここで checked_at を使っていたが、ebay_sync 側が CURRENT_TIMESTAMP で
        # セットする値と競合し上書きされる問題があった。この関数は status と last_checked のみ更新。
        conn.execute(
            """UPDATE ebay_listings SET
               source_status=?, source_last_checked=?
               WHERE ebay_item_id=?""",
            (status, checked_at, ebay_item_id),
        )

        # 在庫切れ開始日は、ebay_sync が未処理 (source_out_of_stock_since IS NULL) かつ
        # ここで新規に在庫無になる場合のみ、フォールバックで checked_at をセット
        if status == '在庫無' and prev_status != '在庫無':
            conn.execute(
                """UPDATE ebay_listings SET source_out_of_stock_since=?
                   WHERE ebay_item_id=? AND source_out_of_stock_since IS NULL""",
                (checked_at, ebay_item_id),
            )
        elif status == '在庫有':
            # 在庫復活 → 開始日クリア
            conn.execute(
                "UPDATE ebay_listings SET source_out_of_stock_since=NULL "
                "WHERE ebay_item_id=? AND source_out_of_stock_since IS NOT NULL",
                (ebay_item_id,),
            )
        updated += 1

    conn.commit()
    conn.close()

    logger.info(f"在庫ステータス統合: {updated}件更新, {not_found}件未一致")
    return {'updated': updated, 'not_found': not_found}


def sync_enrichment_to_db() -> Dict:
    """
    sku_conversion_results.json の物理データ（weight, dimensions）→ ebay_listings に統合
    """
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
    if not sku_file.exists():
        return {'updated': 0}

    with open(sku_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = get_conn()
    updated = 0

    for item in data.get('sourced', []):
        ebay_id = item.get('ebay_id', '')
        weight = item.get('weight_g', 0)

        if not ebay_id or not weight:
            continue

        conn.execute(
            """UPDATE ebay_listings SET
               weight_g=?, length_cm=?, width_cm=?, height_cm=?,
               includes=?, warranty=?
               WHERE ebay_item_id=?""",
            (
                weight,
                item.get('length_cm', 0),
                item.get('width_cm', 0),
                item.get('height_cm', 0),
                item.get('includes', ''),
                item.get('warranty', ''),
                ebay_id,
            ),
        )
        updated += 1

    conn.commit()
    conn.close()

    logger.info(f"物理データ統合: {updated}件更新")
    return {'updated': updated}


def run_sync_data_stores(config) -> Dict:
    """
    データストア統合タスク

    1. sku_conversion_results.json → ebay_listings（source, classification）
    2. inventory_check_results.json → ebay_listings（source_status）
    3. enrichment data → ebay_listings（weight, dimensions）

    Returns:
        {'success': bool, 'sku_sync': dict, 'inventory_sync': dict, 'enrichment_sync': dict}
    """
    logger.info("【開始】データストア統合タスク")

    try:
        # DB初期化（マイグレーション実行）
        sys.path.insert(0, str(BASE_DIR / 'monitor'))
        from monitor.database import init_db
        init_db()

        # Step 1: SKU変換データ統合
        sku_result = sync_sku_conversion_to_db()

        # Step 2: 在庫ステータス統合
        inv_result = sync_inventory_status_to_db()

        # Step 3: 物理データ統合
        enrich_result = sync_enrichment_to_db()

        total = sku_result['updated'] + inv_result['updated'] + enrich_result['updated']
        logger.info(f"データストア統合完了: 合計{total}件更新")

        return {
            'success': True,
            'sku_sync': sku_result,
            'inventory_sync': inv_result,
            'enrichment_sync': enrich_result,
            'total_updated': total,
            'message': f'データストア統合完了: {total}件更新'
        }

    except Exception as e:
        logger.error(f"データストア統合エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e)
        }
