#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 仕入先在庫切れの検知
前回比較で以下の状態変化を検知し、商品詳細情報を抽出
- 在庫有 → 在庫無
- 在庫有 → ページなし
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


def load_inventory_check_results() -> dict:
    """在庫チェック結果を読み込み"""
    inventory_file = BASE_DIR / 'data' / 'inventory_check_results.json'

    if not inventory_file.exists():
        logger.warning(f"在庫チェック結果が見つかりません: {inventory_file}")
        return {}

    try:
        with open(inventory_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info("在庫チェック結果を読み込み")
        return data
    except Exception as e:
        logger.warning(f"在庫チェック結果読み込みエラー: {e}")
        return {}


def detect_inventory_status_changes(inventory_data: dict) -> Dict[str, List]:
    """
    前回比較で以下の状態変化を検知：
    - 在庫有 → 在庫無
    - 在庫有 → ページなし

    Returns:
        {
            'became_out_of_stock': [...],
            'became_page_not_found': [...]
        }
    """

    changes = inventory_data.get('changes', {})
    changed_items = changes.get('changed_items', [])

    became_out_of_stock = []
    became_page_not_found = []

    for item in changed_items:
        prev_status = item.get('prev_status', '')
        current_status = item.get('current_status', '')

        # 在庫有 → 在庫無
        if prev_status == '在庫有' and current_status == '在庫無':
            became_out_of_stock.append(item)
            logger.info(f"検知: {item.get('sku')} が在庫有 → 在庫無に変化")

        # 在庫有 → ページなし
        if prev_status == '在庫有' and current_status == 'ページなし':
            became_page_not_found.append(item)
            logger.info(f"検知: {item.get('sku')} が在庫有 → ページなしに変化")

    return {
        'became_out_of_stock': became_out_of_stock,
        'became_page_not_found': became_page_not_found
    }


def extract_product_details(item: dict) -> dict:
    """
    商品の詳細情報を抽出・整形

    Args:
        item: 在庫チェック結果から取得した商品情報

    Returns:
        {
            'sku': str,
            'source': str（仕入先名）,
            'url': str,
            'status_change': str（「在庫有→在庫無」or「在庫有→ページなし」）,
            'changed_at': str（検知時刻）
        }
    """

    prev_status = item.get('prev_status', '')
    current_status = item.get('current_status', '')

    if prev_status == '在庫有' and current_status == '在庫無':
        status_change = '在庫有 → 在庫無'
    elif prev_status == '在庫有' and current_status == 'ページなし':
        status_change = '在庫有 → ページなし'
    else:
        status_change = f'{prev_status} → {current_status}'

    product_info = {
        'sku': item.get('sku', ''),
        'source': item.get('source', ''),
        'url': item.get('url', ''),
        'status_change': status_change,
        'changed_at': item.get('changed_at', datetime.now().isoformat()),
        'product_name': item.get('product_name', ''),  # あれば含める
        'product_description': item.get('product_description', '')  # あれば含める
    }

    return product_info


def run_inventory_alert(config):
    """
    仕入先在庫切れを検知

    以下の状態変化を検知し、商品詳細情報を抽出：
    - 在庫有 → 在庫無
    - 在庫有 → ページなし

    検知後、AI エージェントが同等商品をネットサーフィンで探す

    Args:
        config: 設定辞書

    Returns:
        {
            'success': bool,
            'alert_count': int,
            'alerts': [
                {
                    'sku': str,
                    'source': str,
                    'url': str,
                    'status_change': str,
                    'changed_at': str
                }
            ]
        }
    """

    logger.info("【開始】仕入先在庫切れ検知タスク")

    try:
        # ステップ1: 在庫チェック結果を読み込み
        inventory_data = load_inventory_check_results()

        if not inventory_data:
            logger.warning("在庫チェック結果が見つかりません")
            return {
                'success': False,
                'alert_count': 0,
                'alerts': [],
                'message': 'Inventory check results not found'
            }

        # ステップ2: 状態変化を検知
        logger.info("ステップ2: 状態変化を検知中...")
        status_changes = detect_inventory_status_changes(inventory_data)

        became_out_of_stock = status_changes.get('became_out_of_stock', [])
        became_page_not_found = status_changes.get('became_page_not_found', [])

        # 両方の変化を統合
        all_changes = became_out_of_stock + became_page_not_found

        logger.info(f"検知件数: {len(all_changes)}件")
        logger.info(f"  - 在庫有→在庫無: {len(became_out_of_stock)}件")
        logger.info(f"  - 在庫有→ページなし: {len(became_page_not_found)}件")

        if not all_changes:
            logger.info("状態変化がありません")
            return {
                'success': True,
                'alert_count': 0,
                'alerts': [],
                'message': 'No status changes detected'
            }

        # ステップ3: 商品詳細情報を抽出
        logger.info("ステップ3: 商品詳細情報を抽出中...")
        alerts = []

        for item in all_changes:
            product_info = extract_product_details(item)
            alerts.append(product_info)

            logger.info(
                f"検知: {product_info['sku']} "
                f"({product_info['source']}) "
                f"{product_info['status_change']}"
            )

        logger.info("仕入先在庫切れ検知完了")

        return {
            'success': True,
            'alert_count': len(alerts),
            'alerts': alerts,
            'message': f'在庫切れ検知完了: {len(alerts)}件'
        }

    except Exception as e:
        logger.error(f"在庫切れ検知エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'alert_count': 0,
            'alerts': [],
            'error': str(e)
        }
