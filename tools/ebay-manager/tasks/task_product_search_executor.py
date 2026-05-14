#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: Claude による同等商品の web 検索実行
product_search_tasks.json から検索タスクを読み込んで、Claude が web 検索を実行
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


def load_search_tasks(tasks_file: Path = None) -> List[Dict]:
    """
    検索タスクを読み込み

    Args:
        tasks_file: タスクファイルパス（デフォルト: data/product_search_tasks.json）

    Returns:
        検索タスクのリスト
    """

    if tasks_file is None:
        tasks_file = BASE_DIR / 'data' / 'product_search_tasks.json'

    if not tasks_file.exists():
        logger.warning(f"タスクファイルが見つかりません: {tasks_file}")
        return []

    try:
        with open(tasks_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tasks = data.get('tasks', [])
        logger.info(f"検索タスクを読み込み: {len(tasks)}件")
        return tasks
    except Exception as e:
        logger.warning(f"タスク読み込みエラー: {e}")
        return []


def execute_search_task(task: Dict) -> Dict:
    """
    単一の検索タスクを実行

    **重要**: この関数は Claude が直接呼び出すことを想定しています。
    Claude は WebSearch ツールを使用して検索を実行します。

    Args:
        task: 検索タスク
        {
            'sku': str,
            'source': str,
            'original_url': str,
            'queries': [str],
            'product_name': str
        }

    Returns:
        {
            'sku': str,
            'source': str,
            'original_url': str,
            'candidates': [
                {
                    'rank': 1,
                    'url': str,
                    'title': str,
                    'source': str,
                    'score': float,
                    'reason': str
                }
            ],
            'message': str
        }
    """

    sku = task.get('sku', '')
    source = task.get('source', '')
    queries = task.get('queries', [])

    logger.info(f"\n【検索実行】{sku} ({source})")
    logger.info(f"検索クエリ: {queries}")

    result = {
        'sku': sku,
        'source': source,
        'original_url': task.get('original_url', ''),
        'candidates': [],
        'message': '検索を実行してください（Claude がこの関数を呼び出しています）'
    }

    return result


def save_search_results(results: List[Dict], output_file: Path = None) -> Path:
    """
    検索結果を JSON ファイルに保存

    Args:
        results: 検索結果のリスト
        output_file: 保存先（デフォルト: data/product_search_results.json）

    Returns:
        保存されたファイルパス
    """

    if output_file is None:
        output_file = BASE_DIR / 'data' / 'product_search_results.json'

    try:
        data = {
            'executed_at': datetime.now().isoformat(),
            'results': results
        }

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"検索結果を保存: {output_file}")
        return output_file

    except Exception as e:
        logger.error(f"結果保存エラー: {e}")
        raise


def generate_execution_prompt(tasks: List[Dict]) -> str:
    """
    Claude が web 検索を実行するためのプロンプトを生成

    Args:
        tasks: 検索タスクのリスト

    Returns:
        Claude への指示プロンプト
    """

    if not tasks:
        return "検索タスクがありません。"

    prompt = f"""
以下の {len(tasks)} 件の商品について、仕入先プラットフォームで同等商品を検索してください。

【検索方法】
1. 各タスクの「クエリ」を使用して WebSearch で検索
2. 結果の中から指定された仕入先（source）のものを抽出
3. 有望な候補について WebFetch で詳細確認
4. 同等性スコア（0.0～1.0）を計算
5. Top 3 件の候補を JSON 形式で返す

【検索タスク】
"""

    for idx, task in enumerate(tasks, 1):
        prompt += f"""
{idx}. SKU: {task['sku']}
   商品: {task.get('product_name', 'N/A')}
   仕入先: {task['source']}
   元の URL: {task['original_url']}
   検索クエリ: {', '.join(task['queries'])}

"""

    prompt += """
【評価基準】
各候補について以下をチェックし、スコアを計算してください：

- 販売中 / 出品中か？
  ✓ Yes: +0.3点
  ✗ No: -0.2点

- 商品の状態は？
  • 新品 / 未使用: +0.3点
  • 美品（使用感少）: +0.2点
  • 通常（中古）: +0.1点
  • 訳あり / 要確認: -0.1点

- 付属品は？
  ✓ フル付属 / セット: +0.2点
  ~ 部分的: +0.1点
  ✗ なし: 0点

- 仕入先は指定のものか？
  ✓ 完全一致: +0.1点
  ✗ 異なる: -0.2点

最終スコア = 上記合計（0.0～1.0 に正規化）

【返却形式】

各商品ごと、以下の JSON を生成してください：

```json
{
  "sku": "ebayme_32400850054",
  "source": "メルカリ",
  "original_url": "https://jp.mercari.com/item/m32400850054",
  "candidates": [
    {
      "rank": 1,
      "url": "https://jp.mercari.com/item/m87654321",
      "title": "レアアイテム A",
      "source": "メルカリ",
      "score": 0.85,
      "reason": "販売中で状態も良好。付属品も完備。価格も適正。"
    },
    {
      "rank": 2,
      "url": "https://jp.mercari.com/item/m87654322",
      "title": "レアアイテム A（同等品）",
      "source": "メルカリ",
      "score": 0.72,
      "reason": "販売中。若干の使用感あるが価格が安い。"
    },
    {
      "rank": 3,
      "url": "https://page.auctions.yahoo.co.jp/jp/auction/q123456",
      "title": "レアアイテム A（オークション）",
      "source": "Yahoo Auctions",
      "score": 0.68,
      "reason": "即決あり。状態は中程度。配送時間が長い。"
    }
  ]
}
```

【重要な注意点】
- 実際に存在する商品 URL を返してください（作り話の URL は避けてください）
- スコアは現実的な範囲（0.4～0.95）を目安に
- 同等商品が見つからない場合は candidates を空配列で返す
- 各結果は改行で区切って複数返す
"""

    return prompt


def main():
    """CLI エントリーポイント"""

    import sys

    print("=" * 70)
    print("Product Search Executor - Claude による web 検索実行")
    print("=" * 70)

    # タスクを読み込み
    tasks = load_search_tasks()

    if not tasks:
        print("❌ 検索タスクが見つかりません")
        print("   まず task_product_search.py を実行してください")
        sys.exit(1)

    print(f"\n✅ {len(tasks)}件の検索タスクを読み込み")

    # Claude への指示プロンプトを生成
    prompt = generate_execution_prompt(tasks)

    print("\n" + "=" * 70)
    print("Claude への指示プロンプト")
    print("=" * 70)
    print(prompt)

    print("\n" + "=" * 70)
    print("⏳ Claude が上記のプロンプトに従って web 検索を実行します")
    print("=" * 70)


if __name__ == '__main__':
    main()
