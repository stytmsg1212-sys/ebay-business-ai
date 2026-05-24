#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ハーネステスト - パイプライン自己検証
daily_scheduler ↔ tasks ↔ config ↔ company_router ↔ DB の整合性を自動検証

使い方:
  python harness_test.py          # 全検証
  python harness_test.py --quick  # インポートとconfig整合性のみ（高速）

daily_scheduler の実行前に自動で呼ばれる（preflight モード）。
問題があれば WARNING/FAIL を出力し、致命的な場合は実行を止める。
"""

import sys
import json
import sqlite3
import importlib
from pathlib import Path
from datetime import datetime

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# ── 定義: scheduler が実行するタスクの正規マッピング ──
# key = config のタスク名, value = (モジュールパス, 関数名, results キー)
TASK_REGISTRY = {
    'ebay_sync':        ('tasks.task_ebay_sync',        'run_ebay_sync',        'ebay_sync'),
    'inventory_check':  ('tasks.task_inventory_check',  'run_inventory_check',  'inventory_check'),
    'inventory_alert':  ('tasks.task_inventory_alert',  'run_inventory_alert',  'inventory_alert'),
    'supplier_select':  ('tasks.task_supplier_select',  'run_supplier_select',  'supplier_select'),
    'email_pickup':     ('tasks.task_email_pickup',     'run_email_pickup',     'email'),
    # 'research' — W21 (2026-04-26) で削除。task_research.py 死蔵化により.
    'rival_detection':  ('tasks.task_rival_detection',  'run_rival_detection',  'rival_detection'),
    'data_sync':        ('tasks.task_sync_data_stores', 'run_sync_data_stores', 'data_sync'),
    'price_optimization': ('tasks.task_price_optimization', 'run_price_optimization', 'price_optimization'),
    # W160 (2026-05-24): 'sales_tracking' (task_sales_tracking) 削除. W149 で置換済.
    'news_check':       ('tasks.task_news_check',       'run_news_check',       'news'),
}

# scheduler が呼ぶが config 管理外のタスク（朝5:00限定）
SPECIAL_TASKS = {
    'company_secretary': ('tasks.task_company_secretary', 'run_company_secretary', 'company_secretary'),
}

# DB 必須カラム（ebay_listings）
REQUIRED_DB_COLUMNS = [
    'ebay_item_id', 'sku', 'title', 'current_price', 'rank',
    'source', 'source_url', 'classification',
    'source_status', 'source_last_checked', 'source_out_of_stock_since',
    'competitor_min_price', 'competitor_count',
    'price_suggestion', 'price_suggestion_reason',
    'total_sold_count', 'last_sold_at',
]

# 必須データファイル
REQUIRED_DATA_FILES = [
    'data/monitor.db',
    'data/sku_conversion_results.json',
    'data/sourced_items_for_playwright.csv',
]

# company_router のルーティング関数
ROUTER_FUNCTIONS = [
    'route_ebay_sync', 'route_inventory_check', 'route_inventory_alert',
    'route_rival_detection', 'route_news_check', 'route_supplier_select',
    'route_email', 'route_price_optimization',
    # 'route_sales_tracking' は W160 (2026-05-24) で削除
    'route_data_sync', 'route_all_results',
]


class HarnessTest:
    def __init__(self):
        self.results = []
        self.pass_count = 0
        self.warn_count = 0
        self.fail_count = 0

    def _record(self, level, category, message):
        self.results.append((level, category, message))
        if level == 'PASS':
            self.pass_count += 1
        elif level == 'WARN':
            self.warn_count += 1
        elif level == 'FAIL':
            self.fail_count += 1

    def ok(self, cat, msg):
        self._record('PASS', cat, msg)

    def warn(self, cat, msg):
        self._record('WARN', cat, msg)

    def fail(self, cat, msg):
        self._record('FAIL', cat, msg)

    # ──────────────────────────────────────
    # T1: タスクモジュールのインポート検証
    # ──────────────────────────────────────
    def test_task_imports(self):
        all_tasks = {**TASK_REGISTRY, **SPECIAL_TASKS}
        for task_name, (module_path, func_name, _) in all_tasks.items():
            try:
                mod = importlib.import_module(module_path)
                fn = getattr(mod, func_name)
                if callable(fn):
                    self.ok('import', f'{task_name} → {module_path}.{func_name}')
                else:
                    self.fail('import', f'{task_name}: {func_name} is not callable')
            except ImportError as e:
                self.fail('import', f'{task_name}: モジュール {module_path} が見つからない ({e})')
            except AttributeError:
                self.fail('import', f'{task_name}: {module_path} に {func_name} が存在しない')

    # ──────────────────────────────────────
    # T2: config ↔ scheduler 整合性
    # ──────────────────────────────────────
    def test_config_consistency(self):
        config_file = BASE_DIR / 'config' / 'schedule_config.json'
        if not config_file.exists():
            self.fail('config', 'schedule_config.json が見つかりません')
            return

        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        tasks_enabled = config.get('tasks_enabled', {})

        # config に定義されているがレジストリにないタスク
        for task_name in tasks_enabled:
            if task_name not in TASK_REGISTRY:
                self.warn('config', f'config に {task_name} があるがレジストリに未登録')

        # レジストリにあるが config に未定義のタスク
        for task_name in TASK_REGISTRY:
            if task_name not in tasks_enabled:
                self.warn('config', f'{task_name} がレジストリにあるが config に未定義（デフォルト有効で動作）')

        # config 内の各タスクの構造検証
        for task_name, task_conf in tasks_enabled.items():
            if isinstance(task_conf, dict):
                if 'enabled' not in task_conf:
                    self.warn('config', f'{task_name}: "enabled" フィールドがない（デフォルト有効）')
                if 'execution_times' in task_conf:
                    times = task_conf['execution_times']
                    valid_times = config.get('execution_schedule', {}).get('times', [5, 11, 17, 22])
                    for t in times:
                        if t not in valid_times:
                            self.warn('config', f'{task_name}: execution_time {t} がスケジュール外')
            self.ok('config', f'{task_name}: 構造OK')

        # retry_policy 検証
        retry = config.get('retry_policy', {})
        if not retry:
            self.warn('config', 'retry_policy が未定義')
        elif retry.get('max_retries', 0) < 1:
            self.warn('config', 'max_retries が0以下')
        else:
            self.ok('config', f"retry_policy: max={retry.get('max_retries')}, delay={retry.get('retry_delay')}s")

    # ──────────────────────────────────────
    # T3: DB スキーマ検証
    # ──────────────────────────────────────
    def test_db_schema(self):
        db_path = BASE_DIR / 'data' / 'monitor.db'
        if not db_path.exists():
            self.fail('db', 'monitor.db が見つかりません')
            return

        conn = sqlite3.connect(str(db_path))

        # ebay_listings のカラム検証
        cursor = conn.execute("PRAGMA table_info(ebay_listings)")
        cols = {row[1] for row in cursor.fetchall()}

        for col in REQUIRED_DB_COLUMNS:
            if col in cols:
                self.ok('db', f'ebay_listings.{col}')
            else:
                self.fail('db', f'ebay_listings.{col} が存在しない（マイグレーション未実行?）')

        # sales_history テーブルの存在確認
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sales_history'"
        )
        if cursor.fetchone():
            self.ok('db', 'sales_history テーブル存在')
        else:
            self.fail('db', 'sales_history テーブルが存在しない')

        # データ件数チェック
        try:
            count = conn.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
            if count > 0:
                self.ok('db', f'ebay_listings: {count}件')
            else:
                self.warn('db', 'ebay_listings: 0件（データが空）')
        except Exception as e:
            self.fail('db', f'ebay_listings クエリエラー: {e}')

        conn.close()

    # ──────────────────────────────────────
    # T4: データファイル検証
    # ──────────────────────────────────────
    def test_data_files(self):
        for rel_path in REQUIRED_DATA_FILES:
            full_path = BASE_DIR / rel_path
            if full_path.exists():
                size = full_path.stat().st_size
                if size > 0:
                    self.ok('data', f'{rel_path} ({size:,} bytes)')
                else:
                    self.warn('data', f'{rel_path} は空ファイル')
            else:
                self.fail('data', f'{rel_path} が見つかりません')

        # inventory_check_results.json の鮮度チェック
        inv_file = BASE_DIR / 'data' / 'inventory_check_results.json'
        if inv_file.exists():
            try:
                with open(inv_file, 'r', encoding='utf-8') as f:
                    inv_data = json.load(f)
                checked_at = inv_data.get('checked_at', '')
                if checked_at:
                    checked_dt = datetime.fromisoformat(checked_at)
                    age_hours = (datetime.now() - checked_dt).total_seconds() / 3600
                    if age_hours > 48:
                        self.warn('data', f'inventory_check_results.json は {age_hours:.0f}時間前のデータ')
                    else:
                        self.ok('data', f'inventory_check_results.json: {age_hours:.1f}時間前')
            except Exception:
                pass

    # ──────────────────────────────────────
    # T5: company_router 検証
    # ──────────────────────────────────────
    def test_company_router(self):
        try:
            import company_router
            for func_name in ROUTER_FUNCTIONS:
                if hasattr(company_router, func_name) and callable(getattr(company_router, func_name)):
                    self.ok('router', f'{func_name}')
                else:
                    self.fail('router', f'{func_name} が company_router に存在しない')
        except ImportError as e:
            self.fail('router', f'company_router のインポートに失敗: {e}')

        # .company ディレクトリの存在確認（company_router と同じ探索ロジック）
        try:
            company_dir = company_router.get_company_root()
        except Exception:
            company_dir = None
        if company_dir and company_dir.exists():
            departments = [d.name for d in company_dir.iterdir() if d.is_dir()]
            self.ok('router', f'.company/ 存在 (部署: {", ".join(departments)})')
        else:
            self.warn('router', '.company/ が見つからない（組織ルーティング先がない）')

    # ──────────────────────────────────────
    # T6: Discord notifier 検証
    # ──────────────────────────────────────
    def test_discord_notifier(self):
        try:
            from notifiers.discord_notifier import DiscordNotifier
            self.ok('discord', 'DiscordNotifier インポート成功')

            # webhook URL が config にあるか
            config_file = BASE_DIR / 'config' / 'schedule_config.json'
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                webhook = config.get('discord', {}).get('webhook_url', '')
                if webhook and webhook.startswith('https://discord.com/api/webhooks/'):
                    self.ok('discord', 'webhook URL 設定済み')
                else:
                    self.warn('discord', 'webhook URL が未設定または不正')
        except ImportError as e:
            self.warn('discord', f'DiscordNotifier のインポートに失敗: {e}')

    # ──────────────────────────────────────
    # T7: タスク間データフロー検証
    # ──────────────────────────────────────
    def test_data_flow(self):
        """タスク間の依存関係が満たされているか検証"""

        # inventory_alert は inventory_check の結果に依存
        inv_file = BASE_DIR / 'data' / 'inventory_check_results.json'
        if inv_file.exists():
            self.ok('flow', 'inventory_check → inventory_alert: データファイル存在')
        else:
            self.warn('flow', 'inventory_check → inventory_alert: inventory_check_results.json がない')

        # supplier_select は sku_conversion + inventory_check に依存
        sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
        if sku_file.exists() and inv_file.exists():
            self.ok('flow', 'sku_conversion + inventory_check → supplier_select: 両データ存在')
        else:
            missing = []
            if not sku_file.exists():
                missing.append('sku_conversion_results.json')
            if not inv_file.exists():
                missing.append('inventory_check_results.json')
            self.warn('flow', f'supplier_select 依存データ不足: {", ".join(missing)}')

        # data_sync は sku_conversion + inventory_check → DB
        db_path = BASE_DIR / 'data' / 'monitor.db'
        if sku_file.exists() and inv_file.exists() and db_path.exists():
            self.ok('flow', 'data_sync: ソースデータ + DB 存在')
        else:
            self.warn('flow', 'data_sync: 必要なファイルが不足')

        # price_optimization は ebay_listings のデータに依存
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM ebay_listings WHERE current_price > 0"
                ).fetchone()
                if row[0] > 0:
                    self.ok('flow', f'price_optimization: 価格データ {row[0]}件')
                else:
                    self.warn('flow', 'price_optimization: current_price > 0 のデータが0件')
            except Exception:
                self.warn('flow', 'price_optimization: ebay_listings クエリ失敗')
            conn.close()

    # ──────────────────────────────────────
    # 実行
    # ──────────────────────────────────────
    def run_all(self, quick=False):
        print("=" * 60)
        print(f"  ハーネステスト - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        self.test_task_imports()
        self.test_config_consistency()

        if not quick:
            self.test_db_schema()
            self.test_data_files()
            self.test_company_router()
            self.test_discord_notifier()
            self.test_data_flow()

        self._print_report()
        return self.fail_count == 0

    def _print_report(self):
        print("\n" + "-" * 60)

        # FAIL と WARN だけ表示（PASS は省略）
        issues = [(l, c, m) for l, c, m in self.results if l != 'PASS']
        if issues:
            print("\n問題:")
            for level, cat, msg in issues:
                icon = '!!!' if level == 'FAIL' else '...'
                print(f"  {icon} [{cat}] {msg}")

        print(f"\n結果: {self.pass_count} PASS / {self.warn_count} WARN / {self.fail_count} FAIL")

        if self.fail_count > 0:
            print(">>> 致命的な問題があります。修正してください。")
        elif self.warn_count > 0:
            print(">>> 警告がありますが実行可能です。")
        else:
            print(">>> 全て正常です。")
        print()


def run_preflight(config=None) -> bool:
    """
    daily_scheduler から呼ばれるプリフライトチェック。
    False を返した場合、致命的問題があるため実行を中止すべき。
    """
    ht = HarnessTest()
    return ht.run_all(quick=True)


def run_full_test() -> bool:
    """全検証を実行"""
    ht = HarnessTest()
    return ht.run_all(quick=False)


if __name__ == '__main__':
    quick = '--quick' in sys.argv
    ht = HarnessTest()
    success = ht.run_all(quick=quick)
    sys.exit(0 if success else 1)
