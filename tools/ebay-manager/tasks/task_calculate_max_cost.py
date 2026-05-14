#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: eBay出品から「同等商品判定に必要な仕入先最大価格」を計算

概要：
  eBayで出品している商品の売却価格・重量・送料等から、
  「この商品と同等の商品を、いくら以下で仕入れれば利益が出るか」を計算する。

  その結果を同等商品判定に使用。

ロジック：
  1. eBay出品情報を取得（価格、重量、寸法）
  2. calculator.calculate() で現在の利益を計算
  3. 逆算：「目標利益を維持するには、仕入れ値をいくらまで下げられるか」
  4. その金額を同等商品判定の基準にする
"""

import sys
import json
import logging
from pathlib import Path
from typing import Dict, Optional

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# calculator をインポート
try:
    from calculator import CalcInput, calculate, load_settings
except ImportError:
    logger.warning("calculator module not found, will use mock")
    CalcInput = None
    calculate = None
    load_settings = None


def get_ebay_item_specs(ebay_item: Dict) -> Dict:
    """
    eBay出品情報から、利益計算に必要なスペックを抽出

    Args:
        ebay_item: sku_conversion_results.json の sourced item
        {
            'ebay_id': '356420645893',
            'sku': 'ebayme_32400850054',
            'title': '...',
            'weight_g': 500,
            'length_cm': 10,
            'width_cm': 10,
            'height_cm': 10,
            'item_price_usd': 49.99,
            'category_id': 0
        }

    Returns:
        {
            'ebay_id': str,
            'title': str,
            'item_price_usd': float,
            'weight_g': float,
            'dimensions': {'length': float, 'width': float, 'height': float}
        }
    """

    return {
        'ebay_id': ebay_item.get('ebay_id', ''),
        'title': ebay_item.get('title', ''),
        'item_price_usd': ebay_item.get('item_price_usd', 0.0),
        'weight_g': ebay_item.get('weight_g', 0.0),
        'dimensions': {
            'length': ebay_item.get('length_cm', 0.0),
            'width': ebay_item.get('width_cm', 0.0),
            'height': ebay_item.get('height_cm', 0.0)
        },
        'category_id': ebay_item.get('category_id', 0),
        'is_ddu': ebay_item.get('is_ddu', False)
    }


def calculate_max_cost_price(
    ebay_item: Dict,
    settings: Dict,
    target_profit_jpy: int = 3000,
    cost_adjustment_jpy: int = 0
) -> Dict:
    """
    eBay出品から「仕入先最大価格」を計算

    Args:
        ebay_item: eBay出品情報（price_usd, weight, dimensions等を含む）
        settings: 利益計算の設定（exchange_rate, tax_rate等）
        target_profit_jpy: 目標利益（デフォルト: ¥3,000）
        cost_adjustment_jpy: その他コスト調整（梱包材等、デフォルト: 0）

    Returns:
        {
            'success': bool,
            'ebay_id': str,
            'title': str,
            'item_price_usd': float,
            'current_profit_jpy': float,  # 現在の利益（仕入れ0円の場合）
            'max_cost_price_jpy': float,  # 仕入先最大価格
            'reasoning': str,  # 計算過程の説明
            'error': str  # エラー時のメッセージ
        }
    """

    if calculate is None:
        logger.error("calculator module not available")
        return {
            'success': False,
            'error': 'calculator module not found'
        }

    try:
        logger.info(f"【開始】最大仕入価格計算: {ebay_item.get('title', '')[:50]}")

        # ステップ1: eBay出品スペックを抽出
        specs = get_ebay_item_specs(ebay_item)

        ebay_id = specs['ebay_id']
        title = specs['title']
        item_price_usd = specs['item_price_usd']
        weight_g = specs['weight_g']
        length = specs['dimensions']['length']
        width = specs['dimensions']['width']
        height = specs['dimensions']['height']

        if not item_price_usd or not weight_g:
            return {
                'success': False,
                'ebay_id': ebay_id,
                'title': title,
                'error': 'Missing price or weight information'
            }

        # ステップ2: 「仕入価格 = ¥0」として現在の利益を計算
        logger.info(f"ステップ2: 現在の利益を計算（仕入価格 = 0円）")

        calc_input = CalcInput(
            purchase_yen=0.0,  # 仕入れ 0 円で試算
            item_price_usd=item_price_usd,
            weight_g=weight_g,
            length_cm=length,
            width_cm=width,
            height_cm=height,
            category_id=specs['category_id'],
            is_ddu=specs['is_ddu']
        )

        calc_result = calculate(calc_input, settings)

        # 各送料サービスの中から最安を選択（同等性判定用）
        if calc_result.service_results:
            best_service = min(
                calc_result.service_results,
                key=lambda x: x.total_shipping
            )
            current_profit_jpy = best_service.profit
            shipping_cost_jpy = best_service.total_shipping

            logger.info(f"  現在の利益（仕入0円）: ¥{current_profit_jpy:,.0f}")
            logger.info(f"  最安送料: {best_service.service_name} (¥{shipping_cost_jpy:,.0f})")
        else:
            return {
                'success': False,
                'ebay_id': ebay_id,
                'title': title,
                'error': 'No shipping services available'
            }

        # ステップ3: 逆算「目標利益を達成するには仕入れ値をいくらまで下げられるか」
        logger.info(f"ステップ3: 仕入先最大価格を逆算")

        # 現在の利益から、許容できる仕入価格の上限を計算
        # current_profit = revenue - ebay_costs - purchase_price
        # => purchase_price = current_profit - target_profit_jpy

        max_cost_price = current_profit_jpy - target_profit_jpy - cost_adjustment_jpy

        if max_cost_price < 0:
            logger.warning(f"  警告: 目標利益達成不可能（最大仕入価格がマイナス）")
            max_cost_price = 0

        logger.info(f"  目標利益: ¥{target_profit_jpy:,}")
        logger.info(f"  最大仕入価格: ¥{max_cost_price:,.0f}")

        reasoning = (
            f"eBay出品価格 ${item_price_usd} から、\n"
            f"送料・eBay手数料等のコスト (¥{calc_result.ebay_cost_subtotal:,.0f}) を差し引くと、\n"
            f"目標利益 ¥{target_profit_jpy:,} を確保するには、\n"
            f"仕入先価格は ¥{max_cost_price:,.0f} 以下である必要があります。"
        )

        return {
            'success': True,
            'ebay_id': ebay_id,
            'title': title,
            'item_price_usd': item_price_usd,
            'current_profit_jpy': current_profit_jpy,
            'target_profit_jpy': target_profit_jpy,
            'max_cost_price_jpy': max_cost_price,
            'reasoning': reasoning
        }

    except Exception as e:
        logger.error(f"最大仕入価格計算エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'ebay_id': ebay_item.get('ebay_id', ''),
            'title': ebay_item.get('title', ''),
            'error': str(e)
        }


def run_max_cost_calculation(config: Dict, ebay_items: Dict = None) -> Dict:
    """
    複数のeBay出品について最大仕入価格を計算

    Args:
        config: 設定辞書
        ebay_items: sku_conversion_results.json の sourced items（辞書形式）

    Returns:
        {
            'success': bool,
            'total_items': int,
            'calculations': [
                {
                    'sku': str,
                    'max_cost_price_jpy': float,
                    ...
                }
            ]
        }
    """

    logger.info("【開始】eBay出品の最大仕入価格一括計算")

    if ebay_items is None:
        # sku_conversion_results.json から sourced items を読み込み
        sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
        if not sku_file.exists():
            return {
                'success': False,
                'error': 'sku_conversion_results.json not found'
            }

        with open(sku_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        ebay_items = {item['sku']: item for item in data.get('sourced', [])}

    # settings を読み込み
    try:
        settings = load_settings()
    except Exception as e:
        logger.error(f"settings 読み込みエラー: {e}")
        return {
            'success': False,
            'error': f'Failed to load settings: {e}'
        }

    calculations = []

    for sku, ebay_item in ebay_items.items():
        result = calculate_max_cost_price(ebay_item, settings)

        if result.get('success'):
            # eBay item の情報と計算結果を合わせる
            calc_data = {
                'sku': sku,
                'ebay_id': ebay_item.get('ebay_id'),
                'title': ebay_item.get('title'),
                'source': ebay_item.get('source'),
                'item_price_usd': result.get('item_price_usd'),
                'max_cost_price_jpy': result.get('max_cost_price_jpy'),
                'current_profit_jpy': result.get('current_profit_jpy'),
                'target_profit_jpy': result.get('target_profit_jpy')
            }
            calculations.append(calc_data)

    logger.info(f"✅ 計算完了: {len(calculations)}件")

    return {
        'success': True,
        'total_items': len(ebay_items),
        'calculated_items': len(calculations),
        'calculations': calculations
    }
