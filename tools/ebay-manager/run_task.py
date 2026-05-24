#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
タスク即時実行 CLI

使い方:
  python run_task.py email          # メール取得
  python run_task.py news           # AIニュース
  python run_task.py research       # リサーチ
  python run_task.py sync           # eBay同期
  python run_task.py inventory      # 在庫チェック
  python run_task.py alert          # 在庫アラート
  python run_task.py supplier       # 仕入先候補
  python run_task.py rival          # ライバル検出
  python run_task.py data_sync      # データストア統合
  python run_task.py price          # 価格最適化
  python run_task.py sales          # 売上トラッキング
  python run_task.py all            # 全タスク（定時実行と同じ）
  python run_task.py list           # タスク一覧表示
"""

import sys
import json
import time
import logging
import traceback
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# Windows CP932対策: stdout/stderrをUTF-8化、子プロセスにも伝播
import utf8_console  # noqa: F401

# ロギング
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(str(BASE_DIR / 'logs' / 'run_task.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('run_task')

# タスク定義: (短縮名, 表示名, モジュール, 関数名)
TASKS = {
    'email':     ('📧 メール取得',       'tasks.task_email_pickup',       'run_email_pickup'),
    'news':      ('📰 AIニュース',       'tasks.task_news_check',         'run_news_check'),
    # 'research' — W21 (2026-04-26) で削除済 (死蔵化のため). 将来 W23 Research 脳が代替.
    'sync':      ('🔄 eBay同期',         'tasks.task_ebay_sync',          'run_ebay_sync'),
    'inventory': ('📦 在庫チェック',     'tasks.task_inventory_check',    'run_inventory_check'),
    'alert':     ('⚠️ 在庫アラート',     'tasks.task_inventory_alert',    'run_inventory_alert'),
    'supplier':  ('🏪 仕入先候補',       'tasks.task_supplier_select',    'run_supplier_select'),
    'rival':     ('👥 ライバル検出',     'tasks.task_rival_detection',    'run_rival_detection'),
    'data_sync': ('🗄️ データストア統合', 'tasks.task_sync_data_stores',   'run_sync_data_stores'),
    'price':     ('💰 価格最適化',       'tasks.task_price_optimization', 'run_price_optimization'),
    # W160 (2026-05-24): 'sales' (task_sales_tracking) 削除. W149 で置換済.
}


def load_config():
    config_file = BASE_DIR / 'config' / 'schedule_config.json'
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def run_single_task(task_key, config):
    """単一タスクを即時実行"""
    if task_key not in TASKS:
        print(f"不明なタスク: {task_key}")
        print(f"利用可能: {', '.join(TASKS.keys())}")
        return None

    display_name, module_path, func_name = TASKS[task_key]

    print(f"\n{'='*50}")
    print(f"  即時実行: {display_name}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")

    start = time.time()

    try:
        import importlib
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        result = func(config)
        elapsed = time.time() - start

        print(f"\n{'─'*50}")
        if isinstance(result, dict):
            success = result.get('success', True)
            msg = result.get('message', '')
            if success:
                print(f"✅ {display_name} 完了 ({elapsed:.1f}秒)")
            else:
                print(f"❌ {display_name} 失敗: {result.get('error', '不明')}")
            if msg:
                print(f"   {msg}")
            # 主要な数値を表示
            for key in ['checked_count', 'alert_count', 'total_suggestions',
                        'sales_count', 'total_updated', 'new_sellers_count']:
                if key in result:
                    print(f"   {key}: {result[key]}")
        else:
            print(f"✅ {display_name} 完了 ({elapsed:.1f}秒)")

        # 組織ルーティング
        try:
            from company_router import route_all_results
            from harness_test import TASK_REGISTRY
            # results_key を逆引き
            results_key = task_key
            for cfg_name, (_, _, rkey) in TASK_REGISTRY.items():
                if TASKS.get(task_key, (None, None, None))[2] == _ or rkey == task_key:
                    pass
            # 簡易ルーティング: 該当タスクの結果だけ渡す
            results_key_map = {
                'email': 'email', 'news': 'news', 'research': 'research',
                'sync': 'ebay_sync', 'inventory': 'inventory_check',
                'alert': 'inventory_alert', 'supplier': 'supplier_select',
                'rival': 'rival_detection', 'data_sync': 'data_sync',
                'price': 'price_optimization',
                # 'sales': 'sales_tracking' は W160 で削除
            }
            rkey = results_key_map.get(task_key, task_key)
            route_all_results({rkey: result})
        except Exception:
            pass  # ルーティング失敗は無視

        return result

    except Exception as e:
        elapsed = time.time() - start
        print(f"\n❌ {display_name} エラー ({elapsed:.1f}秒)")
        print(f"   {e}")
        logger.error(traceback.format_exc())
        return {'success': False, 'error': str(e)}


def run_all_tasks(config):
    """全タスクを定時実行と同じ順序で実行"""
    from daily_scheduler import execute_daily_tasks
    return execute_daily_tasks(config)


def show_task_list():
    """タスク一覧を表示"""
    print(f"\n{'='*50}")
    print("  利用可能なタスク")
    print(f"{'='*50}\n")

    for key, (display_name, _, _) in TASKS.items():
        print(f"  {key:<12} {display_name}")

    print(f"\n  {'all':<12} 全タスク実行（定時実行と同じ順序）")
    print(f"  {'list':<12} この一覧を表示")
    print(f"\n使い方: python run_task.py <タスク名>")
    print(f"複数実行: python run_task.py email news research\n")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        show_task_list()
        return

    config = load_config()
    task_args = sys.argv[1:]

    if task_args == ['list']:
        show_task_list()
        return

    if task_args == ['all']:
        run_all_tasks(config)
        return

    # 複数タスク指定対応
    results = {}
    for task_key in task_args:
        if task_key not in TASKS:
            print(f"不明なタスク: {task_key} (スキップ)")
            continue
        result = run_single_task(task_key, config)
        results[task_key] = result

    # 複数タスク実行時のサマリー
    if len(results) > 1:
        print(f"\n{'='*50}")
        print("  実行サマリー")
        print(f"{'='*50}")
        for key, result in results.items():
            display_name = TASKS[key][0]
            if isinstance(result, dict) and result.get('success') is False:
                print(f"  ❌ {display_name}")
            else:
                print(f"  ✅ {display_name}")
        print()


if __name__ == '__main__':
    main()
