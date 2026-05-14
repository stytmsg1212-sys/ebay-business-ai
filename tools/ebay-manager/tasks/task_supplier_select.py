#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 既に在庫切れになっているもの（30-50個）の対応
在庫無が「3日以上続いている」商品を検出し、
同一ソースの別商品URL、または代替ソースを候補として提示する。

データソース:
  - data/inventory_check_results.json → 在庫チェック結果（348件）
  - data/sku_conversion_results.json → SKU変換結果（ソースURL付き）
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent


def load_inventory_results() -> Dict:
    """在庫チェック結果を読み込み"""
    inv_file = BASE_DIR / 'data' / 'inventory_check_results.json'

    if not inv_file.exists():
        logger.warning(f"在庫チェック結果が見つかりません: {inv_file}")
        return {}

    try:
        with open(inv_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"在庫チェック結果読み込みエラー: {e}")
        return {}


def load_sku_conversion_results() -> Dict:
    """SKU変換結果を読み込み（sourced リストをSKUで引けるdictに変換）"""
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'

    if not sku_file.exists():
        logger.warning(f"SKU変換ファイルが見つかりません: {sku_file}")
        return {}

    try:
        with open(sku_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # sourced リストを SKU → item の dict に変換
        sku_map = {}
        for item in data.get('sourced', []):
            sku = item.get('sku', '')
            if sku:
                sku_map[sku] = item

        logger.info(f"SKU変換結果を読み込み: {len(sku_map)}件")
        return sku_map
    except Exception as e:
        logger.warning(f"SKU変換ファイル読み込みエラー: {e}")
        return {}


def detect_long_term_out_of_stock(
    inventory_results: Dict,
    threshold_days: int = 3
) -> List[Dict]:
    """
    在庫無が threshold_days 日以上続いている商品を検出（修正版）。

    BUG-5 修正: inventory_check_results.json の `checked_at` は「最後にチェックした日時」
    であり「在庫切れが始まった日時」ではないため誤用していた。DBの
    `ebay_listings.source_out_of_stock_since` を正として判定する。
    """
    # DB から在庫切れ継続期間を取得
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from monitor.database import get_conn

    now = datetime.now()
    long_term_out = []

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sku, ebay_item_id, source, source_url,
                      source_out_of_stock_since, source_last_checked
               FROM ebay_listings
               WHERE source_status='在庫無'
                 AND (is_ended IS NULL OR is_ended=0)
                 AND source_out_of_stock_since IS NOT NULL
                 AND source_out_of_stock_since <= datetime('now', ?)""",
            (f"-{threshold_days} days",),
        ).fetchall()

    for r in rows:
        try:
            oos_since = datetime.fromisoformat(r["source_out_of_stock_since"])
        except (ValueError, TypeError):
            continue
        days_out = (now - oos_since).days
        long_term_out.append({
            'sku': r['sku'] or '',
            'ebay_id': r['ebay_item_id'] or '',
            'source': r['source'] or '',
            'url': r['source_url'] or '',
            'checked_at': r['source_last_checked'] or '',
            'days_out_of_stock': days_out,
        })

    logger.info(f"長期在庫切れ（{threshold_days}日以上）: {len(long_term_out)}件")
    return long_term_out[:50]


def find_same_source_alternatives(
    sku: str,
    source: str,
    sku_map: Dict,
    inventory_results: Dict,
) -> List[Dict]:
    """
    同一ソース（例: メルカリ）で在庫有の別商品を候補として返す。
    「同じ仕入先で別の商品は買えるか？」を確認する。

    ※ 同一商品の代替品を見つけるわけではない。
       同一ソースでまだ生きている取引実績のある仕入先情報を返す。
    """
    # 在庫チェック結果から同一ソースで在庫有のアイテムを取得
    results = inventory_results.get('results', [])
    in_stock_same_source = []

    for item in results:
        if (item.get('source') == source
                and item.get('status') == '在庫有'
                and item.get('sku') != sku):
            in_stock_same_source.append({
                'sku': item.get('sku', ''),
                'url': item.get('url', ''),
                'source': source,
                'status': '在庫有',
            })

    return in_stock_same_source[:5]


def run_supplier_select(config):
    """
    在庫無が「3日以上続いている」商品を検出し、情報を整理して返す

    Returns:
        {
            'success': bool,
            'product_count': int,
            'out_of_stock_summary': {source: count},
            'suppliers': [{sku, ebay_id, source, url, days_out, title, alternatives}],
            'message': str
        }
    """

    logger.info("【開始】仕入先候補選出タスク")

    try:
        # Step 1: 在庫チェック結果を読み込み
        inventory_results = load_inventory_results()
        if not inventory_results:
            return {
                'success': False,
                'product_count': 0,
                'suppliers': [],
                'message': 'Inventory check results not found'
            }

        # Step 2: 長期在庫切れ商品を検出
        long_term_out = detect_long_term_out_of_stock(inventory_results, threshold_days=3)

        if not long_term_out:
            logger.info("長期在庫切れ商品がありません")
            return {
                'success': True,
                'product_count': 0,
                'suppliers': [],
                'message': 'No long-term out-of-stock items'
            }

        # Step 3: SKU変換結果でeBayタイトル等を補完
        sku_map = load_sku_conversion_results()

        # Step 4: ソース別の集計 + 候補情報の構築
        source_summary = {}
        suppliers = []

        for item in long_term_out:
            sku = item['sku']
            source = item['source']

            # ソース別カウント
            source_summary[source] = source_summary.get(source, 0) + 1

            # SKU変換結果からタイトル取得
            sku_info = sku_map.get(sku, {})
            title = sku_info.get('title', '')

            # 同一ソースの在庫有アイテム（仕入先の生存確認）
            alternatives = find_same_source_alternatives(
                sku, source, sku_map, inventory_results)

            suppliers.append({
                'sku': sku,
                'ebay_id': item.get('ebay_id', ''),
                'source': source,
                'url': item['url'],
                'title': title[:60] if title else '',
                'days_out_of_stock': item['days_out_of_stock'],
                'source_still_active': len(alternatives) > 0,
                'same_source_in_stock': len(alternatives),
                'supplier_candidates': [
                    {
                        'name': f"{source}（同一ソース在庫有）",
                        'url': alt['url'],
                        'score': 0.5,
                        'details': {
                            'stock': 1,
                            'price': 0,
                            'shipping_days': 3,
                        }
                    }
                    for alt in alternatives[:3]
                ],
            })

        # 日数が長い順にソート
        suppliers.sort(key=lambda x: x['days_out_of_stock'], reverse=True)

        logger.info(f"仕入先候補選出完了: {len(long_term_out)}件")
        logger.info(f"  ソース別: {source_summary}")

        return {
            'success': True,
            'product_count': len(long_term_out),
            'out_of_stock_summary': source_summary,
            'suppliers': suppliers,
            'message': f'仕入先候補選出完了: {len(long_term_out)}件'
        }

    except Exception as e:
        logger.error(f"仕入先選出エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'product_count': 0,
            'suppliers': [],
            'error': str(e)
        }
