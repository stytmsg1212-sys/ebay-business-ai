#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: Claude による検索結果を処理して product_search_results.json に保存

Claude が WebSearch で見つけた同等商品の候補を、
構造化されたフォーマットで保存する。

処理フロー：
1. Claude からの検索結果を受け取る
2. 各候補をスコア順にソート
3. financially_viable フラグを設定
4. product_search_results.json に保存
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


def save_search_results(
    sku: str,
    source: str,
    model_number: str,
    candidates: List[Dict],
    max_cost_price_jpy: float = 0,
    output_file: Path = None
) -> Dict:
    """
    Claude による検索結果を product_search_results.json に保存

    Args:
        sku: 商品SKU
        source: 仕入先プラットフォーム（メルカリ、Yahoo Auctions等）
        model_number: 型番
        candidates: 候補商品のリスト
            {
                'url': str,
                'title': str,
                'condition': str,
                'includes': str,
                'warranty': str,
                'price_jpy': float,
                'score': float,
                'reason': str
            }
        max_cost_price_jpy: 最大仕入価格
        output_file: 保存先（デフォルト: data/product_search_results.json）

    Returns:
        保存結果の辞書
    """

    if output_file is None:
        output_file = BASE_DIR / 'data' / 'product_search_results.json'

    # 候補をスコア順（降順）にソート
    sorted_candidates = sorted(candidates, key=lambda x: x.get('score', 0), reverse=True)

    # financially_viable フラグを設定
    for idx, candidate in enumerate(sorted_candidates, 1):
        candidate['rank'] = idx
        candidate['financially_viable'] = (
            candidate.get('price_jpy', float('inf')) <= max_cost_price_jpy
        ) if max_cost_price_jpy > 0 else False

    # サマリーを作成
    viable_candidates = [c for c in sorted_candidates if c.get('financially_viable', False)]
    top_viable = viable_candidates[0] if viable_candidates else None

    recommendation = "採用推奨" if top_viable else "同等商品が見つかりませんでした"
    if top_viable:
        recommendation = f"採用推奨（Rank {top_viable['rank']} の候補を仕入れることで在庫補充可能）"

    task_result = {
        "sku": sku,
        "source": source,
        "model_number": model_number,
        "search_date": datetime.now().isoformat(),
        "candidates": sorted_candidates[:3],  # Top 3 のみ保存
        "summary": {
            "search_date": datetime.now().strftime('%Y-%m-%d'),
            "total_candidates_found": len(candidates),
            "candidates_evaluated": len(sorted_candidates),
            "top_viable_candidate": top_viable,
            "recommendation": recommendation
        }
    }

    # 既存の結果を読み込み
    results = []
    if output_file.exists():
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                results = data.get('tasks', [])
        except Exception as e:
            logger.warning(f"既存ファイル読み込みエラー: {e}")

    # 同じ SKU の結果があれば置き換え、なければ追加
    existing_idx = next((idx for idx, r in enumerate(results) if r.get('sku') == sku), None)
    if existing_idx is not None:
        results[existing_idx] = task_result
    else:
        results.append(task_result)

    # ファイルに保存
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({'tasks': results}, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ 検索結果を保存: {output_file}")
        logger.info(f"   SKU: {sku}")
        logger.info(f"   候補数: {len(sorted_candidates)}件")
        logger.info(f"   推奨: {recommendation}")

        return {
            'success': True,
            'sku': sku,
            'candidates_count': len(sorted_candidates),
            'viable_candidates_count': len(viable_candidates),
            'recommendation': recommendation
        }

    except Exception as e:
        logger.error(f"結果保存エラー: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def load_and_process_all_results(input_file: Path = None) -> Dict:
    """
    複数の検索結果を一括処理

    Args:
        input_file: 入力ファイル（デフォルト: data/equivalence_check_tasks.json）

    Returns:
        処理結果の辞書
    """

    if input_file is None:
        input_file = BASE_DIR / 'data' / 'equivalence_check_tasks.json'

    logger.info(f"検索タスク結果ファイルを読み込み: {input_file}")

    if not input_file.exists():
        logger.error(f"ファイルが見つかりません: {input_file}")
        return {
            'success': False,
            'error': 'Input file not found'
        }

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        tasks = data.get('tasks', [])
        logger.info(f"タスク数: {len(tasks)}件")

        return {
            'success': True,
            'total_tasks': len(tasks),
            'tasks': tasks
        }

    except Exception as e:
        logger.error(f"タスク読み込みエラー: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def batch_save_results(results_list: List[Dict]) -> Dict:
    """
    複数の検索結果を一括保存

    Args:
        results_list: 保存対象の結果リスト
            {
                'sku': str,
                'source': str,
                'model_number': str,
                'candidates': List[Dict],
                'max_cost_price_jpy': float
            }

    Returns:
        一括保存の結果
    """

    success_count = 0
    error_count = 0

    for result in results_list:
        save_result = save_search_results(
            sku=result.get('sku', ''),
            source=result.get('source', ''),
            model_number=result.get('model_number', ''),
            candidates=result.get('candidates', []),
            max_cost_price_jpy=result.get('max_cost_price_jpy', 0)
        )

        if save_result.get('success'):
            success_count += 1
        else:
            error_count += 1

    logger.info(f"✅ 一括保存完了: {success_count}件成功, {error_count}件失敗")

    return {
        'success': True,
        'total': len(results_list),
        'success_count': success_count,
        'error_count': error_count
    }


if __name__ == '__main__':
    # テスト：equivalence_check_tasks.json を読み込んで構造を確認
    result = load_and_process_all_results()
    if result.get('success'):
        print(f"タスク読み込み成功: {result['total_tasks']}件")
