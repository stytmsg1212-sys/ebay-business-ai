#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 在庫切れ商品と「同じ型番」の商品を仕入先で見つける

重要な理解：
- このタスクは「同じ型番の商品」を見つけることが目標
- 「それがeBay既存出品と同等か」の判断は Claude (AI) が行う
- スクリプトは候補を見つけて、比較しやすいデータ構造で提示

例：
  元の出品：ADVANTEST R8340A (傷あり, ボディのみ, ¥30,000)
  探すもの：ADVANTEST R8340A の別の出品
  Claude が判定：「傷あり, ボディのみ, ¥18,000」は同等か？
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

# 利益計算用のインポート
try:
    from .task_calculate_max_cost import calculate_max_cost_price
except ImportError:
    try:
        from task_calculate_max_cost import calculate_max_cost_price
    except ImportError:
        logger.warning("task_calculate_max_cost module not found")
        calculate_max_cost_price = None


def load_sourced_items() -> Dict[str, Dict]:
    """
    eBayで出品されている仕入先商品の情報を読み込み

    Returns:
        {
            'ebayme_32400850054': {
                'sku': 'ebayme_32400850054',
                'ebay_item_id': '356420645893',
                'model_number': 'R8340A',
                'manufacturer': 'ADVANTEST',
                'source': 'Yahoo Auctions',
                'source_url': 'https://auctions.yahoo.co.jp/jp/auction/...',
                'title': '☆ADVANTEST R8340A Ultra High Resistance Meter ☆',
                'condition': '傷や汚れあり',
                'includes': 'ボディのみ',
                'warranty': 'ノークレーム',
                'price_jpy': 30000
            },
            ...
        }
    """
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'

    if not sku_file.exists():
        logger.warning(f"SKU変換ファイルが見つかりません: {sku_file}")
        return {}

    try:
        with open(sku_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"仕入先商品情報を読み込み: {len(data)}件")
        return data
    except Exception as e:
        logger.warning(f"SKU変換ファイル読み込みエラー: {e}")
        return {}


def extract_model_number(ebay_item: Dict) -> str:
    """
    eBay出品から型番を抽出

    Args:
        ebay_item: sku_conversion_results.json から取得したeBay出品情報

    Returns:
        型番（例: "R8340A", "KP-717G"）
    """

    # 優先順位：
    # 1. model_number フィールド（既に抽出済み）
    if ebay_item.get('model_number'):
        return ebay_item['model_number']

    # 2. sku から推測（ebayme_32400850054 の場合は数値部分）
    sku = ebay_item.get('sku', '')
    if '_' in sku:
        parts = sku.split('_')
        if len(parts) >= 3:
            return parts[-1]  # 最後の数値部分

    # 3. title から推測
    title = ebay_item.get('title', '')
    if title:
        # タイトルから「R8340A」「KP-717G」のようなパターンを抽出
        import re
        match = re.search(r'([A-Z0-9]+-?[A-Z0-9]+)', title)
        if match:
            return match.group(1)

    return sku  # 最終手段として SKU を返す


def build_search_query(model_number: str, source: str) -> str:
    """
    同じ型番の商品を探すための検索クエリを構築

    Args:
        model_number: 型番（例: "R8340A"）
        source: 仕入先（例: "Yahoo Auctions", "メルカリ"）

    Returns:
        検索クエリ（例: "R8340A Yahoo Auctions 販売中"）
    """

    # 仕入先別の検索方法
    source_search_patterns = {
        'Yahoo Auctions': f'{model_number} site:auctions.yahoo.co.jp',
        'ヤフオク': f'{model_number} site:auctions.yahoo.co.jp',
        'メルカリ': f'{model_number} site:mercari.com',
        'Mercari': f'{model_number} site:mercari.com',
        'ラクマ': f'{model_number} site:rakuma.rakuten.co.jp',
        'Rakuma': f'{model_number} site:rakuma.rakuten.co.jp',
        'PayPayフリマ': f'{model_number} site:paypayfleamarket.yahoo.co.jp',
    }

    # 仕入先に応じたクエリを選択
    query = source_search_patterns.get(source, f'{model_number} {source} 販売中')

    logger.info(f"検索クエリ: {query}")
    return query


def prepare_evaluation_prompt(candidates: List[Dict], product_info: dict) -> str:
    """
    Claude による候補評価用のプロンプトを準備

    Args:
        candidates: WebFetch で詳細情報を取得した候補リスト
        product_info: 元の商品情報

    Returns:
        Claude への評価指示プロンプト
    """

    sku = product_info.get('sku', '')
    source = product_info.get('source', '')
    product_name = product_info.get('product_name', '')

    prompt = f"""
以下の在庫切れ商品と同等の商品候補を評価してください。

【元の商品情報】
- SKU: {sku}
- 仕入先: {source}
- 商品名: {product_name}

【候補商品の詳細情報】
"""

    for idx, candidate in enumerate(candidates, 1):
        prompt += f"""
{idx}. {candidate['title']}
   URL: {candidate['url']}
   詳細情報:
   {candidate.get('details', 'N/A')}
"""

    prompt += """
【評価基準】
各候補について以下を確認し、同等性スコア (0.0～1.0) を計算してください：
- 販売中/出品中か？（販売中:0.3点）
- 商品の状態は？（新品/未使用:0.3点、美品:0.2点、中古:0.1点）
- 付属品の有無は？（セット/付属:0.2点）
- 価格は適正か？

【結果形式】
JSON形式で以下を返してください：
```json
{
  "candidates": [
    {
      "rank": 1,
      "url": "...",
      "title": "...",
      "score": 0.85,
      "reason": "販売中で、状態は良好。価格も適正。"
    }
  ]
}
```
"""

    return prompt


def _is_valid_source_url(url: str, source: str) -> bool:
    """
    URL が指定された仕入先のものかを確認

    Args:
        url: 候補商品の URL
        source: 仕入先名（メルカリ、Yahoo Auctions等）

    Returns:
        True なら仕入先の URL
    """

    source_domains = {
        'メルカリ': ['jp.mercari.com', 'mercari.com'],
        'Yahoo Auctions': ['page.auctions.yahoo.co.jp', 'auctions.yahoo.co.jp'],
        'ヤフオク': ['page.auctions.yahoo.co.jp', 'auctions.yahoo.co.jp'],
        'Rakuma': ['rakuma.rakuten.co.jp', 'fril.jp'],
        'PayPayフリマ': ['paypayfleamarket.yahoo.co.jp', 'fril.jp'],
        'PayPay フリマ': ['paypayfleamarket.yahoo.co.jp', 'fril.jp'],
        '楽天市場': ['item.rakuten.co.jp', 'rakuten.co.jp'],
        'Amazon': ['amazon.co.jp', 'amazon.com'],
    }

    domains = source_domains.get(source, [])
    url_lower = url.lower()

    return any(domain in url_lower for domain in domains)


def rank_candidates(candidates: List[Dict]) -> List[Dict]:
    """
    候補商品をスコアでランク付け

    Args:
        candidates: WebFetch で評価済みの候補リスト

    Returns:
        スコア順（降順）にソートされたリスト
    """

    # スコアでソート
    ranked = sorted(candidates, key=lambda x: x['score'], reverse=True)

    # 上位3件を返す
    return ranked[:3]


def prepare_equivalence_check_tasks(alerts: List[Dict], ebay_items: Dict, settings: Dict = None) -> Dict:
    """
    同等性判定用のタスクを準備

    スクリプトが「同じ型番の候補」を見つけた後、
    Claude が「これはeBay既存出品と同等か？」を判定するための
    比較データ構造を準備する

    Args:
        alerts: 在庫切れアラート（status_change が検知された商品）
        ebay_items: eBayで出品中の商品（model_number と condition が既知）
        settings: 利益計算の設定（exchange_rate, tax_rate等）

    Returns:
        {
            'tasks': [
                {
                    'sku': str,
                    'ebay_original': {
                        'title': str,
                        'condition': str,
                        'includes': str,
                        'warranty': str,
                        'price_jpy': int,
                        'url': str
                    },
                    'model_number': str,
                    'source': str,
                    'search_query': str,
                    'max_cost_price_jpy': float,
                    'status': 'awaiting_candidates'
                }
            ]
        }
    """

    tasks = []

    for alert in alerts:
        sku = alert.get('sku', '')
        source = alert.get('source', '')
        original_url = alert.get('url', '')

        logger.info(f"同等性判定タスク準備: {sku} ({source})")

        # eBay出品情報を取得
        ebay_item = ebay_items.get(sku, {})
        model_number = extract_model_number(ebay_item)

        # 検索クエリを生成
        search_query = build_search_query(model_number, source)

        # 仕入先最大価格を計算（設定がある場合）
        max_cost_price_jpy = 0
        if settings and calculate_max_cost_price:
            calc_result = calculate_max_cost_price(ebay_item, settings)
            if calc_result.get('success'):
                max_cost_price_jpy = calc_result.get('max_cost_price_jpy', 0)
                logger.info(f"  最大仕入価格: ¥{max_cost_price_jpy:,.0f}")
            else:
                logger.warning(f"  最大仕入価格の計算失敗: {calc_result.get('error', 'Unknown error')}")

        task = {
            'sku': sku,
            'ebay_original': {
                'title': ebay_item.get('title', ''),
                'condition': ebay_item.get('condition', ''),
                'includes': ebay_item.get('includes', ''),
                'warranty': ebay_item.get('warranty', ''),
                'price_jpy': ebay_item.get('price_jpy', 0),
                'url': original_url,
                'source': source,
            },
            'model_number': model_number,
            'source': source,
            'search_query': search_query,
            'max_cost_price_jpy': max_cost_price_jpy,
            'status': 'awaiting_candidates'  # Claude が候補を見つけるのを待機
        }

        tasks.append(task)

    logger.info(f"同等性判定タスク準備完了: {len(tasks)}件")

    return {'tasks': tasks}


def save_equivalence_tasks(tasks_data: Dict, output_file: Path = None) -> Path:
    """
    同等性判定タスクを JSON ファイルに保存

    Args:
        tasks_data: prepare_equivalence_check_tasks の出力
        output_file: 保存先（デフォルト: data/equivalence_check_tasks.json）

    Returns:
        保存されたファイルパス
    """

    if output_file is None:
        output_file = BASE_DIR / 'data' / 'equivalence_check_tasks.json'

    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(tasks_data, f, ensure_ascii=False, indent=2)

        logger.info(f"同等性判定タスクを保存: {output_file}")
        return output_file

    except Exception as e:
        logger.error(f"タスク保存エラー: {e}")
        raise


def run_product_search(config: dict, alerts: List[Dict] = None) -> Dict:
    """
    在庫切れアラートから同等性判定タスクを準備

    **処理フロー:**
    1. 在庫切れアラートを取得
    2. 各アラートについて「eBay既存出品の情報」を取得
    3. 「同じ型番の商品を探す」タスクを準備
    4. Claude が同等性判定を行うための比較データを作成

    Args:
        config: 設定辞書
        alerts: task_inventory_alert.py の出力（alerts リスト）

    Returns:
        {
            'success': bool,
            'total_alerts': int,
            'tasks_prepared': int,
            'tasks_file': str,
            'message': str
        }
    """

    logger.info("【開始】同等性判定タスク準備")

    try:
        # ステップ1: アラート情報を取得
        if alerts is None:
            logger.info("ステップ1: 在庫切れアラート情報を読み込み中...")
            from tasks.task_inventory_alert import run_inventory_alert
            alert_result = run_inventory_alert(config)

            if not alert_result.get('success'):
                logger.warning("在庫アラートが検出されません")
                return {
                    'success': False,
                    'total_alerts': 0,
                    'tasks_prepared': 0,
                    'tasks_file': None,
                    'message': 'No alerts found'
                }

            alerts = alert_result.get('alerts', [])

        total_alerts = len(alerts)
        logger.info(f"ステップ1完了: {total_alerts}件のアラートを取得")

        if not alerts:
            logger.info("処理対象のアラートがありません")
            return {
                'success': True,
                'total_alerts': 0,
                'tasks_prepared': 0,
                'tasks_file': None,
                'message': 'No alerts to process'
            }

        # ステップ2: eBay出品情報（model_number, condition など）を取得
        logger.info("ステップ2: eBay出品情報を取得中...")
        ebay_items = load_sourced_items()

        if not ebay_items:
            logger.warning("eBay出品情報が見つかりません")
            return {
                'success': False,
                'total_alerts': total_alerts,
                'tasks_prepared': 0,
                'tasks_file': None,
                'message': 'eBay items data not found'
            }

        # ステップ2.5: 利益計算の設定を読み込み
        logger.info("ステップ2.5: 利益計算の設定を読み込み中...")
        settings = None
        try:
            from calculator import load_settings
            settings = load_settings()
        except Exception as e:
            logger.warning(f"設定読み込みエラー（最大仕入価格の計算はスキップ）: {e}")

        # ステップ3: 同等性判定タスクを準備
        logger.info(f"ステップ3: {total_alerts}件の同等性判定タスクを準備中...")

        tasks_data = prepare_equivalence_check_tasks(alerts, ebay_items, settings)
        tasks_prepared = len(tasks_data['tasks'])

        # ステップ4: タスクをファイルに保存
        logger.info("ステップ4: 同等性判定タスクをファイルに保存中...")

        tasks_file = save_equivalence_tasks(tasks_data)

        logger.info(f"✅ 同等性判定タスク準備完了: {tasks_prepared}件")
        logger.info(f"📋 タスクファイル: {tasks_file}")
        logger.info("⏳ 次のステップ: Claude が同じ型番の商品を検索し、同等性を判定します...")

        return {
            'success': True,
            'total_alerts': total_alerts,
            'tasks_prepared': tasks_prepared,
            'tasks_file': str(tasks_file),
            'message': f'Prepared {tasks_prepared} equivalence check tasks.'
        }

    except Exception as e:
        logger.error(f"同等性判定タスク準備エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'total_alerts': 0,
            'tasks_prepared': 0,
            'tasks_file': None,
            'error': str(e)
        }
