#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Daily Scheduler - eBay Manager 自動実行スケジューラー
毎日 5:00, 11:00, 17:00, 22:00 に定時実行タスク群を管理

v2: 実行順序最適化 + 組織ルーティング + Discord通知拡充 + リトライ機能
"""

import os
import sys
import json
import time
import threading
import logging
import traceback
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# W95 (2026-05-03): 多重起動防止 file lock (Windows msvcrt).
# 5/3 早朝の 3 並列稼働事故 (PID 65408 / 1308 / 82880) 構造的予防.
# OS-level lock = process 死で auto-release、stale PID 判定不要.
import msvcrt

# ──────────────────────────────────────────────────────────────────────
# バッチ実行コンテキスト (2026-04-25 hour ドリフト事故対応 /
#                          2026-05-18 thread-local 化で silent skip 根治).
# execute_daily_tasks 開始時に batch_hour/batch_id を捕獲し、
# should_task_run / run_task はこれを参照する. datetime.now() 都度参照を廃止.
#
# 【thread-local 必須の理由 / 2026-05-18 事故根治】
# APScheduler は各 job を別 worker thread で並行実行する。_batch_ctx を素の
# module global dict にすると、長時間 02:30 batch 実行中 (ebay_sync +
# inventory_check + supplier_sweep で ~03:21 まで) に別 thread の isolated
# task (03:00 daily_codex_lint / order_alert 等) が hour を 3 に上書きし、
# まだ走行中の 02:30 batch が daily_relist 評価時 (03:21) に clobbered
# hour=3 を読んで `batch_hour=3 not in execution_times=[2]` で silent skip。
# 2026-04-25/05-05 事故の再発 (daily_codex_lint cron 追加 2026-05-15 →
# 5/16 から daily_relist/enrich/estimate_weights/cleanup/research_morning
# が毎日 silent skip)。_run_isolated_task の save/restore は thread 跨ぎで
# 非 composable のため無効。thread-local 化で各 job の batch context を
# 完全分離する。should_task_run は execute_daily_tasks 内からのみ呼ばれ、
# 同 thread の冒頭で hour を set 済 = 並行 isolated task の影響を受けない。
# ──────────────────────────────────────────────────────────────────────
class _ThreadLocalBatchCtx:
    """thread ごとに独立した batch context を提供する dict 互換 proxy.

    APScheduler worker thread は再利用されるが、execute_daily_tasks は自身の
    冒頭で必ず hour/id を set し直すため stale 読みは起きない (gating consumer
    は execute_daily_tasks のみ)。
    """

    def __init__(self):
        self._local = threading.local()

    def _d(self) -> dict:
        d = getattr(self._local, "d", None)
        if d is None:
            d = {"id": None, "hour": None, "started_at": None}
            self._local.d = d
        return d

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def get(self, k, default=None):
        return self._d().get(k, default)

    def clear(self):
        self._d().clear()

    def update(self, other):
        self._d().update(other)

    def keys(self):
        return self._d().keys()

    def __iter__(self):
        return iter(self._d())

    def __contains__(self, k):
        return k in self._d()


_batch_ctx = _ThreadLocalBatchCtx()

# Windows cp932 対応: stdout/stderrをUTF-8化し、子プロセス環境にも伝播
sys.path.insert(0, str(Path(__file__).parent))
import utf8_console  # noqa: F401 — import副作用でconfigure実行

# パス設定
BASE_DIR = Path(__file__).parent
TASKS_DIR = BASE_DIR / 'tasks'
CONFIG_DIR = BASE_DIR / 'config'
CONFIG_FILE = CONFIG_DIR / 'schedule_config.json'
LOGS_DIR = BASE_DIR / 'logs'

# ディレクトリ作成（ロギング設定の前に実行）
LOGS_DIR.mkdir(exist_ok=True)

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(str(LOGS_DIR / 'scheduler.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def load_config():
    """設定ファイルを読み込む"""
    if not CONFIG_FILE.exists():
        logger.warning(f"設定ファイルが見つかりません: {CONFIG_FILE}")
        return {
            'tasks_enabled': {
                'email_pickup': True,
                'news_check': True,
                'ebay_sync': True,
                'inventory_check': True,
                'inventory_alert': True,
                'supplier_select': True,
                'rival_detection': True,
            }
        }

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info("設定ファイルを読み込みました")
        # 2026-05-25 .env 移行 (commit 8473103) で空になった webhook を in-memory 復元.
        # 同 config dict は全 cron job に参照渡しされるため通知ガードを 1 箇所で一括復活.
        from notifiers.discord_notifier import inject_webhook_into_config
        inject_webhook_into_config(config)
        return config
    except Exception as e:
        logger.error(f"設定ファイル読み込みエラー: {e}")
        return {}


def safe_import_and_run(task_name, module_path, func_name, config, max_retries=1, retry_delay=30, task_key=None):
    """Step の import + 実行をまとめて安全化。
    import エラーや関数実行エラーで **他 Step に波及させない**。
    2026-04-20 の 02:30 クラッシュ事例（sys.stdout.reconfigure None エラー）の再発防止。
    task_key: 内部キー (task_execution_log 記録用).
    """
    try:
        module = __import__(module_path, fromlist=[func_name])
        func = getattr(module, func_name)
    except Exception as e:
        logger.error(f"【インポート失敗】{task_name} ({module_path}.{func_name}): {e}")
        logger.error(traceback.format_exc())
        return {"success": False, "error": f"import failed: {e}"}
    return run_task(task_name, lambda: func(config), max_retries=max_retries, retry_delay=retry_delay, task_key=task_key)


def run_task(task_name, task_func, max_retries=1, retry_delay=30, task_key=None):
    """
    タスク実行ラッパー（リトライ機能付き）

    Args:
        task_name: 表示用タスク名
        task_func: 実行する関数
        max_retries: 最大リトライ回数（デフォルト1 = リトライなし）
        retry_delay: リトライ間隔（秒）
        task_key: 内部キー (task_execution_log 記録用). 未指定時は記録しない.
    """
    log_id: Optional[int] = None
    if task_key and _batch_ctx.get("id") is not None:
        try:
            from monitor.task_execution_log import log_task_start, log_task_finish
            log_id = log_task_start(
                task_key=task_key,
                display_name=task_name,
                batch_id=_batch_ctx["id"],
                batch_hour=int(_batch_ctx["hour"]),
            )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"task_execution_log start 失敗 ({task_key}): {_le}")

    started_at = time.time()
    last_error_msg = ""
    for attempt in range(1, max_retries + 1):
        logger.info(f"【開始】{task_name}" + (f" (試行{attempt}/{max_retries})" if max_retries > 1 else ""))
        try:
            result = task_func()
            logger.info(f"【完了】{task_name}")
            if log_id is not None:
                try:
                    from monitor.task_execution_log import log_task_finish
                    success = bool(result.get("success", True)) if isinstance(result, dict) else True
                    msg = json.dumps(result, ensure_ascii=False, default=str)[:1000] if isinstance(result, dict) else ""
                    log_task_finish(
                        log_id=log_id,
                        success=success,
                        message=msg,
                        duration_sec=time.time() - started_at,
                    )
                except Exception as _le:  # noqa: BLE001
                    logger.warning(f"task_execution_log finish 失敗 ({task_key}): {_le}")
            return result
        except Exception as e:
            last_error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"【エラー】{task_name}: {str(e)}")
            logger.error(traceback.format_exc())
            if attempt < max_retries:
                logger.info(f"  → {retry_delay}秒後にリトライします...")
                time.sleep(retry_delay)

    if log_id is not None:
        try:
            from monitor.task_execution_log import log_task_finish
            log_task_finish(
                log_id=log_id,
                success=False,
                message=last_error_msg or f"failed after {max_retries} attempts",
                duration_sec=time.time() - started_at,
            )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"task_execution_log finish (failed) 失敗 ({task_key}): {_le}")

    return {'success': False, 'error': f'{task_name} failed after {max_retries} attempts'}


def is_morning_execution(config=None):
    """朝バッチ（設定 execution_schedule.times の最小値）に一致するか判定。
    秘書ルーティン(email + TODO繰越 + research)をまとめ実行するスロット。
    旧実装は hour==5 固定だったが、スケジュール改定で 02:30 に移ったため
    config から動的に決定する。
    2026-04-25: batch 開始時の hour を _batch_ctx から参照 (hour ドリフト対策).
    """
    times = []
    if config:
        times = config.get('execution_schedule', {}).get('times', [])
    morning_hour = min(times) if times else 2
    batch_hour = _batch_ctx.get("hour")
    current_hour = int(batch_hour) if batch_hour is not None else datetime.now().hour
    return current_hour == morning_hour


def should_task_run(task_name, config):
    """タスクが現在の batch で実行されるべきか判定.

    2026-04-25 hour ドリフト事故対策:
        execution_times の判定は datetime.now() ではなく、batch 開始時に
        セットされた _batch_ctx["hour"] (= cron 予定時刻 scheduled_hour) を
        参照する. これにより inventory_check が長引いて 02:30 → 03:22 に
        ずれても、execution_times=[2] のタスクが skip されなくなる.
    曜日 (execution_weekday) は datetime.now() を継続使用 (実用上 batch が
    日付を跨ぐことは想定していない).
    skip 時は task_execution_log に "skip_*" ステータスで記録する.
    例外発生時は logger.error してから False を返す (サイレント例外撲滅).
    """
    try:
        return _should_task_run_impl(task_name, config)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"should_task_run({task_name}) で予期せぬ例外: {type(e).__name__}: {e}",
            exc_info=True,
        )
        # 安全側に倒して False (skip 扱い). サイレントスキップ防止のため log は残す.
        try:
            batch_id = _batch_ctx.get("id")
            batch_hour = _batch_ctx.get("hour")
            if batch_id is not None and batch_hour is not None:
                from monitor.task_execution_log import log_task_skip
                log_task_skip(
                    task_key=task_name,
                    display_name=task_name,
                    batch_id=batch_id,
                    batch_hour=int(batch_hour),
                    reason=f"should_task_run exception: {type(e).__name__}: {e}",
                    skip_kind="skip_other",
                    expected_today=True,
                )
        except Exception:
            pass
        return False


def _should_task_run_impl(task_name, config):
    """should_task_run の実装本体 (try/except 外側でラップされる)."""
    now = datetime.now()
    # batch_ctx が無い場合 (テスト等) は now() にフォールバックするが警告.
    # 本番では setup_scheduler 経由で必ずセットされる.
    batch_hour = _batch_ctx.get("hour")
    if batch_hour is None:
        logger.warning(
            f"should_task_run({task_name}) called without batch_ctx['hour']. "
            f"datetime.now().hour にフォールバック (hour ドリフト リスク有り)."
        )
        current_hour = now.hour
    else:
        current_hour = int(batch_hour)
    current_weekday = now.weekday()
    batch_id = _batch_ctx.get("id")

    task_config = config.get('tasks_enabled', {}).get(task_name)

    # 設定がない場合はデフォルト有効
    if task_config is None:
        return True

    display_name = task_name
    if isinstance(task_config, dict):
        display_name = task_config.get('description') or task_name

    def _record_skip(kind: str, reason: str) -> None:
        if not batch_id:
            return  # batch context が無いケースは記録しない
        try:
            from monitor.task_execution_log import log_task_skip, TASK_SCHEDULE_BY_KEY
            sched = TASK_SCHEDULE_BY_KEY.get(task_name)
            expected_today = False
            if sched is not None:
                # weekdays 制約も考慮 (M-4 対応)
                weekdays = sched.get("weekdays")
                if weekdays is not None and current_weekday not in weekdays:
                    expected_today = False
                else:
                    hours = sched.get("hours")
                    if hours is None:
                        expected_today = True
                    else:
                        expected_today = current_hour in hours
            log_task_skip(
                task_key=task_name,
                display_name=display_name,
                batch_id=batch_id,
                batch_hour=current_hour,
                reason=reason,
                skip_kind=kind,
                expected_today=expected_today,
            )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"task_execution_log skip 失敗 ({task_name}): {_le}")

    # bool の場合はそのまま返す
    if isinstance(task_config, bool):
        if not task_config:
            _record_skip("skip_disabled", "config: enabled=False (bool)")
        return task_config

    # dict の場合
    if isinstance(task_config, dict):
        if not task_config.get('enabled', True):
            _record_skip("skip_disabled", "config: enabled=False")
            return False
        # 時刻フィルタ
        if 'execution_times' in task_config:
            if current_hour not in task_config['execution_times']:
                _record_skip(
                    "skip_time",
                    f"batch_hour={current_hour} not in execution_times="
                    f"{task_config['execution_times']}",
                )
                return False
        # 曜日フィルタ（指定がない場合は毎日）
        if 'execution_weekday' in task_config:
            if current_weekday not in task_config['execution_weekday']:
                _record_skip(
                    "skip_weekday",
                    f"weekday={current_weekday} not in execution_weekday="
                    f"{task_config['execution_weekday']}",
                )
                return False
        return True

    return bool(task_config)


def execute_daily_tasks(config, scheduled_hour=None):
    """
    毎回実行する全タスク

    Args:
        config: schedule_config.json の dict.
        scheduled_hour: cron がトリガーした **予定時刻の hour** (0-23).
            これを batch_ctx["hour"] にセットすることで、inventory_check 等で
            実時刻が hour を跨いでも `should_task_run(execution_times=[H])` が
            正しく True を返す (hour ドリフト対策の核心).
            None の場合は datetime.now().hour にフォールバック (テスト用途のみ).

    実行順序（ビジネスロジック最適化済み）:
      1.  eBay同期      → ランク・メトリクスを最新化（他タスクが参照）
      1.5 監視台帳補完  → 新規無在庫出品を monitored_items 自動登録
                          (W139, ebay_sync 後・在庫チェック前で当batch反映)
      2. 在庫チェック  → 仕入先の在庫状態を取得
      3. 在庫アラート  → 在庫変動を検出（↑の結果に依存）
      4. 仕入先候補    → 長期在庫切れの代替候補（↑と在庫データに依存）
      5. メール取得    → eBay売上通知等
      6. リサーチ      → 市場調査（eBay同期後の最新ランクデータを使用）
      7. ライバル検出  → 競合セラー（eBay同期後の最新キーワードを使用）
      8. ニュース確認  → 独立タスク（最後でOK）
    """

    # ── batch context 設定 (hour ドリフト対策の中核) ──
    # batch_hour は scheduled_hour (cron 予定時刻) を優先. 実時刻ではない.
    # 実時刻を使うと inventory_check の遅延で hour=3 になり execution_times=[2] と乖離する.
    _batch_start_dt = datetime.now()
    if scheduled_hour is None:
        logger.warning(
            "execute_daily_tasks が scheduled_hour 無しで呼ばれました. "
            "datetime.now().hour にフォールバックしますが、setup_scheduler 経由なら "
            "本番では発生しない経路です."
        )
        _bh = _batch_start_dt.hour
    else:
        _bh = int(scheduled_hour)
    # batch_id にも scheduled_hour を含めて、ドリフト時も同じ batch を識別可能に.
    _batch_ctx["id"] = f"{_batch_start_dt.strftime('%Y%m%d')}_{_bh:02d}sched"
    _batch_ctx["hour"] = _bh
    _batch_ctx["started_at"] = _batch_start_dt

    logger.info("=" * 60)
    logger.info(
        f"【定時実行開始】 {_batch_start_dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(batch_id={_batch_ctx['id']}, scheduled_hour={_bh})"
    )
    logger.info("=" * 60)

    # プリフライトチェック: パイプラインの整合性を検証
    try:
        from harness_test import run_preflight
        if not run_preflight(config):
            logger.error("【中止】プリフライトチェックで致命的エラーを検出")
            return {'success': False, 'error': 'preflight check failed'}
        logger.info("プリフライトチェック: OK")
    except Exception as e:
        logger.warning(f"プリフライトチェックをスキップ: {e}")

    results = {}
    is_morning = is_morning_execution(config)
    retry_config = config.get('retry_policy', {})
    max_retries = retry_config.get('max_retries', 2)
    retry_delay = retry_config.get('retry_delay', 30)

    # ──────────────────────────────────────
    # 【朝5:00限定】秘書ルーティン（email + TODO繰越 + research を内包）
    # import 失敗でも他 Step に波及させない safe_import_and_run を使用
    # ──────────────────────────────────────
    if is_morning:
        results['company_secretary'] = safe_import_and_run(
            '秘書ルーティン', 'tasks.task_company_secretary', 'run_company_secretary', config,
            task_key='company_secretary',
        )

    # ──────────────────────────────────────
    # Step 1: eBay同期（最初に実行 → ランクデータを最新化）
    # ──────────────────────────────────────
    if should_task_run('ebay_sync', config):
        from tasks.task_ebay_sync import run_ebay_sync
        results['ebay_sync'] = run_task(
            'eBay連携同期',
            lambda: run_ebay_sync(config),
            max_retries=max_retries, retry_delay=retry_delay,
            task_key='ebay_sync')

    # ──────────────────────────────────────
    # Step 1.5: 監視台帳カバレッジ自動補完 (W139 / 2026-05-18)
    # ebay_sync 後・inventory_check 前に実行: 新規同期された無在庫出品で
    # monitored_items 未登録のものを自動登録し、当 batch の inventory_check
    # から監視対象に入れる。本番事故 (358487417178 が未登録で仕入先OOS
    # 検知不能 → 履行不能注文) の恒久対策。Codex 独立診断収束。
    # ──────────────────────────────────────
    if should_task_run('ensure_monitor_coverage', config):
        from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
        results['ensure_monitor_coverage'] = run_task(
            '監視台帳カバレッジ自動補完',
            lambda: run_ensure_monitor_coverage(config),
            max_retries=max_retries, retry_delay=retry_delay,
            task_key='ensure_monitor_coverage')

    # ──────────────────────────────────────
    # Step 2: 在庫チェック
    # ──────────────────────────────────────
    if should_task_run('inventory_check', config):
        from tasks.task_inventory_check import run_inventory_check
        inv_result = run_task(
            '在庫チェック',
            lambda: run_inventory_check(config),
            max_retries=max_retries, retry_delay=retry_delay,
            task_key='inventory_check')
        results['inventory_check'] = inv_result
        # 2026-05-07: reset_all_risk_confirmed() 呼出を撤廃.
        # 旧仕様: inventory_check 成功で全 listing の risk_confirmed=0 リセット
        #   → user が確認チェック入れても次の inventory_check で消えて
        #     「ずーっと残ってる」と誤認させる UX bug を引き起こしていた.
        # 新仕様: user の確認は永続化. risk_confirmed=1 は手動で 0 に戻すまで維持.
        # 新規 OOS は risk_confirmed=0 で挿入されるため要対応リストに自動表示される.

    # ──────────────────────────────────────
    # Step 3: 在庫切れアラート（在庫チェック結果に依存）
    # ──────────────────────────────────────
    inventory_alert_result = None
    if should_task_run('inventory_alert', config):
        from tasks.task_inventory_alert import run_inventory_alert
        inventory_alert_result = run_task(
            '在庫切れ通知',
            lambda: run_inventory_alert(config),
            task_key='inventory_alert')
        results['inventory_alert'] = inventory_alert_result

    # ──────────────────────────────────────
    # Step 4: 仕入先候補選出（長期在庫切れ商品の代替探し）
    # ──────────────────────────────────────
    if should_task_run('supplier_select', config):
        from tasks.task_supplier_select import run_supplier_select
        results['supplier_select'] = run_task(
            '仕入先候補選出',
            lambda: run_supplier_select(config),
            task_key='supplier_select')

    # ──────────────────────────────────────
    # Step 4b: 仕入先候補スイープ（Pattern 2 = 朝バッチで長期OOS一括探索）
    # ──────────────────────────────────────
    if should_task_run('supplier_sweep', config):
        from tasks.task_supplier_sweep import run_supplier_sweep
        results['supplier_sweep'] = run_task(
            '仕入先候補スイープ',
            lambda: run_supplier_sweep(config),
            task_key='supplier_sweep')

    # ──────────────────────────────────────
    # Step 4c: ebay_listings 物理データ enrichment（profit計算用 weight/dimensions）
    # ──────────────────────────────────────
    if should_task_run('enrich_listings_physical', config):
        from tasks.task_enrich_listings_physical import run_enrich_listings_physical
        results['enrich_listings_physical'] = run_task(
            'listings物理データenrichment',
            lambda: run_enrich_listings_physical(config),
            task_key='enrich_listings_physical')

    # ──────────────────────────────────────
    # Step 4d: Claude weight 推定（eBay側で weight 未入力の listing を補完）
    # ──────────────────────────────────────
    if should_task_run('estimate_weights_claude', config):
        from tasks.task_estimate_weights_claude import run_estimate_weights_claude
        results['estimate_weights_claude'] = run_task(
            'Claude weight推定',
            lambda: run_estimate_weights_claude(config),
            task_key='estimate_weights_claude')

    # ──────────────────────────────────────
    # Step 4e: 日次 End→Relist SEO ブースト (watch=0 / rank=E を最大7件)
    # ──────────────────────────────────────
    if should_task_run('daily_relist', config):
        from tasks.task_daily_relist import run_daily_relist
        results['daily_relist'] = run_task(
            'End→Relist SEOブースト',
            lambda: run_daily_relist(config),
            task_key='daily_relist')

    # ──────────────────────────────────────
    # Step 4e-cleanup: daily_relist 由来 is_ended=1 を 90 日経過後に物理 DELETE
    # 2026-04-30 D 案 + 90 日 user 公認. relist_history に系譜保持済のため履歴消失なし.
    # 設計詳細: tasks/task_cleanup_old_relisted.py
    # ──────────────────────────────────────
    if should_task_run('cleanup_old_relisted', config):
        from tasks.task_cleanup_old_relisted import run_cleanup_old_relisted
        results['cleanup_old_relisted'] = run_task(
            '退役listing 90日経過cleanup',
            lambda: run_cleanup_old_relisted(config),
            task_key='cleanup_old_relisted')

    # ──────────────────────────────────────
    # Step 4f: 動画学習キュー処理 (pending YouTube を Gemini で構造化)
    # ──────────────────────────────────────
    if should_task_run('video_learning_queue', config):
        from tasks.task_video_learning import run_video_learning_queue
        results['video_learning_queue'] = run_task(
            '動画学習キュー',
            lambda: run_video_learning_queue(config),
            task_key='video_learning_queue')

    # ──────────────────────────────────────
    # Step 4g: W24 Research 脳 morning brief (朝 02:30 のみ)
    # 朝バッチで本日の重点 3 項目を Opus 4.8 で生成 → DASHBOARD 表示
    # ──────────────────────────────────────
    if should_task_run('research_morning_brief', config):
        from tasks.task_research_morning_brief import run_research_morning_brief
        results['research_morning_brief'] = run_task(
            'リサーチ脳 morning brief',
            lambda: run_research_morning_brief(config),
            task_key='research_morning_brief')

    # ──────────────────────────────────────
    # Step 5: メール取得（5:00は秘書ルーティンで実行済み）
    # ──────────────────────────────────────
    if should_task_run('email_pickup', config):
        # 2026-04-22 FIX: DELETE WHERE confirmed=1 → INSERT OR IGNORE で同じメールが
        # 再挿入される重大バグがあった。age ベース (30日超) の prune に切替。
        from monitor.database import prune_old_confirmed_emails
        _pruned = prune_old_confirmed_emails(days=30)
        if _pruned:
            logger.info(f"古い確認済みメールを {_pruned} 件 prune")
        from tasks.task_email_pickup import run_email_pickup
        # W244: results キーを task_key と統一 ('email' → 'email_pickup')。
        # 日次レポート data-driven 化で results キー = TASK_SCHEDULE キーが前提に。
        results['email_pickup'] = run_task(
            'メール取得',
            lambda: run_email_pickup(config),
            task_key='email_pickup')
    elif is_morning:
        logger.info("【スキップ】メール取得: 秘書ルーティンで実行済み")

    # ──────────────────────────────────────
    # Step 6: リサーチ — W21 (2026-04-26) で削除
    # 出力 .company/research/notes/*.md が DASHBOARD から削除 (4/23) され
    # 死蔵化していたため task_research.py ごと削除. 将来は W23 Research 脳が代替.
    # ──────────────────────────────────────

    # ──────────────────────────────────────
    # Step 7: ライバルセラー検出（eBay同期後の最新キーワードを使用）
    # ──────────────────────────────────────
    if should_task_run('rival_detection', config):
        from tasks.task_rival_detection import run_rival_detection
        results['rival_detection'] = run_task(
            'ライバルセラー検出',
            lambda: run_rival_detection(config),
            task_key='rival_detection')

    # ──────────────────────────────────────
    # Step 7.5: W#3 ライバルセラー新規出品モニター (W244 / 2026-06-10 結線)
    # TASK_SCHEDULE には登録済 (hours=[2]) なのに dispatch が存在せず、
    # 稼働実績 0 件のままヘルスチェック false-positive を出し続けていた
    # 幽霊タスクを設計意図 (task docstring 経路 A) 通り朝 batch に結線。
    # 注意: run_rival_seller_sweep_task は内部で log_task_start/finish を
    # 自己記録するため task_key を渡さない (渡すと task_execution_log 二重記録)。
    # monitored_sellers 0 件時は即 return (痕跡は log に残る、Q0 準拠)。
    # ──────────────────────────────────────
    if should_task_run('rival_seller_sweep', config):
        from tasks.task_rival_seller_sweep import run_rival_seller_sweep_task
        _rss_hour = _batch_ctx.get("hour")
        results['rival_seller_sweep'] = run_task(
            'ライバルセラー新規出品モニター (W#3)',
            lambda: run_rival_seller_sweep_task(
                config,
                scheduled_hour=int(_rss_hour) if _rss_hour is not None else 2))

    # ──────────────────────────────────────
    # Step 8: データストア統合（eBay同期 + 在庫チェック後に実行）
    # ──────────────────────────────────────
    if should_task_run('data_sync', config):
        from tasks.task_sync_data_stores import run_sync_data_stores
        results['data_sync'] = run_task(
            'データストア統合',
            lambda: run_sync_data_stores(config),
            task_key='data_sync')

    # ──────────────────────────────────────
    # Step 9: 価格最適化（ランク + 競合データがある状態で実行）
    # ──────────────────────────────────────
    if should_task_run('price_optimization', config):
        from tasks.task_price_optimization import run_price_optimization
        results['price_optimization'] = run_task(
            '価格最適化',
            lambda: run_price_optimization(config),
            task_key='price_optimization')

    # ──────────────────────────────────────
    # Step 10: 売上トラッキング
    # ──────────────────────────────────────
    # W160 (2026-05-24): sales_tracking 物理削除. W149 (2026-05-22) で
    # enabled:false 化済 (task_order_alert.GetOrders に置換)。本 W で
    # tasks/task_sales_tracking.py + 関連 mapping を完全撤去.

    # W154 (2026-05-22 PM): main batch の news_check dispatch 削除.
    # W154 で旧 W55 (Anthropic HTML) + 旧 W13 (X+Reddit+HN) を統合し、
    # 独立 cron (06:00) で 1 本化. main batch 経由の二重起動を防ぐため
    # ここの分岐を物理削除. config の "news_check" entry も併せて削除済.

    # ──────────────────────────────────────
    # Step 12: 燃料サーチャージ週次更新リマインダー（通知専用、月曜朝のみ）
    # ──────────────────────────────────────
    if should_task_run('fuel_surcharge_check', config):
        from tasks.task_fuel_surcharge_check import run_fuel_surcharge_check
        results['fuel_surcharge_check'] = run_task(
            '燃料サーチャージ更新リマインダー',
            lambda: run_fuel_surcharge_check(config),
            task_key='fuel_surcharge_check')

    # ──────────────────────────────────────
    # 組織ルーティング: 結果を .company 各部署に配信
    # ──────────────────────────────────────
    try:
        from company_router import route_all_results
        route_all_results(results)
    except Exception as e:
        logger.error(f"組織ルーティングエラー: {e}")

    # ──────────────────────────────────────
    # Discord 通知: 全タスク結果のレポート送信
    # ──────────────────────────────────────
    _send_discord_notifications(config, results, is_morning=is_morning)

    logger.info("=" * 60)
    logger.info(f"【定時実行完了】 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    return results


def _get_recent_tariff_emails(hours: int = 26) -> list:
    """W216: 直近 hours 時間に取得した tariff_policy メールを返す (毎朝 Discord 掲出用)。

    emails.fetched_at は Python datetime.now() bind = JST naive (sqlite-timezone rule の
    例外カラム)。よって cutoff も Python の JST で算出して bind し、TZ ずれを避ける
    (SQL datetime('now') = UTC との混在禁止)。判定は rule ベースの `category` のみで
    行う (claude_summarizer の category enum に tariff_policy は無く category_ai は
    供給され得ないため、OR 条件にしても空振り = dead 条件になる. code-review HIGH-1)。
    """
    from datetime import timedelta
    from monitor.database import get_conn
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT subject, sender, COALESCE(NULLIF(summary_ja,''), '') AS summary_ja,
                      COALESCE(NULLIF(priority_ai,''), '') AS priority_ai, fetched_at
               FROM emails
               WHERE category = 'tariff_policy'
                 AND fetched_at >= ?
               ORDER BY fetched_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def _send_tariff_digest(notifier) -> None:
    """W216: 関税ポリシー/率変更/追加請求/還付メールの毎朝ダイジェストを Discord 掲出。

    関税は利益計算 (settings.duty_rate, W215) に直結するため、最新情報を漏らさず
    user に毎朝届ける (user 要望 2026-06-03)。該当メール 0 件なら何も送らない
    (no-news = no-noise)。Q0: 取りこぼし防止が目的なので送信失敗は warning ログに残す。
    """
    try:
        emails = _get_recent_tariff_emails(hours=26)
        if not emails:
            return
        lines = []
        for em in emails[:10]:
            subj = (em.get('subject') or '').strip()[:90]
            summ = (em.get('summary_ja') or '').strip()[:120]
            pri = (em.get('priority_ai') or '').strip()
            tag = f"[{pri}] " if pri else ''
            lines.append(f"• {tag}{subj}" + (f"\n　└ {summ}" if summ else ''))
        more = f"\n…他 {len(emails) - 10} 件" if len(emails) > 10 else ''
        embed = {
            "title": f"🛃 関税アップデート {len(emails)}件 (率変更/追加請求/還付)",
            "description": "\n".join(lines) + more,
            "color": 0xE67E22,
            "footer": {"text": "利益計算 duty_rate に直結 — 率変更なら settings.us_duty を見直し (W215/W216)"},
        }
        notifier.send_message("🛃 **関税アップデート** (毎朝チェック)", embed=embed)
        logger.info(f"W216 関税ダイジェスト送信: {len(emails)}件")
    except Exception as e:
        logger.warning(f"W216 関税ダイジェスト送信エラー: {e}")


def _send_discord_notifications(config, results, is_morning: bool = False):
    """全タスク結果をDiscordに通知"""
    webhook_url = config.get('discord', {}).get('webhook_url')
    if not webhook_url:
        return

    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook_url)

        # 1. デイリーレポート（全タスクのステータスサマリー）
        notifier.send_daily_report(results)

        # 1b. W216 関税アップデート ダイジェスト (毎朝のみ、利益直結ニュースの取りこぼし防止)
        if is_morning:
            _send_tariff_digest(notifier)

        # 2. 在庫切れアラート（あれば個別通知）
        inv_alert = results.get('inventory_alert', {})
        if inv_alert and inv_alert.get('alert_count', 0) > 0:
            # 仕入先候補と合わせて通知
            supplier = results.get('supplier_select', {})
            supplier_candidates = supplier.get('suppliers', []) if supplier else []
            out_of_stock = inv_alert.get('alerts', [])
            notifier.send_inventory_alert(out_of_stock, supplier_candidates)

        # 3. 高影響ニュース（あれば個別通知）
        news = results.get('news', {})
        if news and news.get('high_impact_count', 0) > 0:
            notifier.send_news_summary(news.get('news', []))

        # 新規ライバル検出の通知は task_rival_detection._send_discord_aggregate
        # ("🎯 W153 新規ライバル検出", listing 別明細) が担う. ここで旧 embed を
        # 送ると同一トリガーで二重通知になる (sellers=[] で退化していた) ため削除.

        logger.info("Discord 通知を送信しました")

    except Exception as e:
        logger.warning(f"Discord 通知エラー: {e}")


def setup_scheduler():
    """スケジューラーセットアップ（2026-04-19改定スケジュール）"""

    config = load_config()

    # H-3 対応: max_instances=1 を強制して batch 並行起動を防ぐ.
    # coalesce=True: 過去の missed トリガーは 1 回にまとめる (連続発火回避).
    # misfire_grace_time=600: 10 分以内なら遅延起動を許容 (それ以上は skip ログ).
    scheduler = BackgroundScheduler(
        job_defaults={
            'max_instances': 1,
            'coalesce': True,
            'misfire_grace_time': 600,
        }
    )
    scheduler.configure(timezone='Asia/Tokyo')

    # ユーザー作業時間(06:00-08:00, 21:00-24:00)に合わせた実行時刻
    # 02:30 朝バッチ / 11:00 軽 / 15:00 クレーム検知 / 18:00 夜バッチ / 22:00 日次サマリー
    execution_times = config.get('execution_schedule', {}).get('times', [2, 11, 15, 18, 22])
    minute_map = config.get('execution_schedule', {}).get('minutes', {'2': 30})

    for hour in execution_times:
        minute = int(minute_map.get(str(hour), 0))
        trigger = CronTrigger(hour=hour, minute=minute, second=0)
        scheduler.add_job(
            execute_daily_tasks,
            trigger=trigger,
            # H-1 対応: scheduled_hour を渡して hour ドリフトに耐える.
            args=[config, hour],
            id=f'daily_task_{hour:02d}_{minute:02d}',
            name=f'日々の全体実行 ({hour:02d}:{minute:02d})',
            replace_existing=True
        )
        logger.info(f"スケジュール設定: 毎日 {hour:02d}:{minute:02d} 実行 (scheduled_hour={hour})")

    # ── W154 AI ニュース取得 (独立 CronJob) ──
    # 旧 W13 (x_news_check) を W154 で news_check に統合. config の
    # "news_check" entry (cron_hour / cron_minute / enabled) を参照.
    news_cfg = (config.get('tasks_enabled', {}).get('news_check') or {})
    if news_cfg.get('enabled', True):
        n_hour = int(news_cfg.get('cron_hour', 6))
        n_minute = int(news_cfg.get('cron_minute', 0))
        scheduler.add_job(
            _run_news_only,
            trigger=CronTrigger(hour=n_hour, minute=n_minute, second=0),
            args=[config, n_hour],
            id=f'news_check_{n_hour:02d}_{n_minute:02d}',
            name=f'W154 AI ニュース取得 ({n_hour:02d}:{n_minute:02d})',
            replace_existing=True,
        )
        logger.info(
            f"W154 ニュース発火: 毎日 {n_hour:02d}:{n_minute:02d}"
        )

    # ── W14 通関対応自動化 (独立 CronJob) ──
    # code-reviewer H-6 対応: W13 (06:00) と API 競合を避けるため 06:10 にオフセット
    customs_cfg = (config.get('tasks_enabled', {}).get('customs_check') or {})
    if customs_cfg.get('enabled', True):
        c_hour = int(customs_cfg.get('cron_hour', 6))
        c_minute = int(customs_cfg.get('cron_minute', 10))
        scheduler.add_job(
            _run_customs_check_only,
            trigger=CronTrigger(hour=c_hour, minute=c_minute, second=0),
            args=[config, c_hour],
            id=f'customs_check_{c_hour:02d}_{c_minute:02d}',
            name=f'W14 通関対応 ({c_hour:02d}:{c_minute:02d})',
            replace_existing=True,
        )
        logger.info(
            f"W14 通関対応発火: 毎日 {c_hour:02d}:{c_minute:02d}"
        )

    # ── 定時実行ヘルスチェック (2026-04-25 サイレントスキップ事故対応) ──
    # 各 batch 終了後タイミングで「本日 expected vs executed」を照合し、
    # 欠落があれば Discord で即通知. 02:30 batch は inventory_check が長引くと
    # 03:30 を超える可能性があるため 04:00 に設定. 他 batch は終了直後タイミング.
    health_check_times = [(4, 0), (12, 0), (16, 0), (19, 0), (23, 0)]
    for h, m in health_check_times:
        scheduler.add_job(
            _run_health_check,
            trigger=CronTrigger(hour=h, minute=m, second=0),
            args=[config, h],
            id=f'health_check_{h:02d}_{m:02d}',
            name=f'定時実行ヘルスチェック ({h:02d}:{m:02d})',
            replace_existing=True,
        )
    logger.info(
        f"定時実行ヘルスチェック発火: {', '.join(f'{h:02d}:{m:02d}' for h, m in health_check_times)}"
    )

    # ── 予算アラート (2026-04-26 追加) ──
    # 1 日 3 回 (06:00 / 12:00 / 19:00) Anthropic API 予算状況を Discord 通知.
    # Tier 1 上限 $35 等の節約モードでの月末予測 + 主要消費内訳.
    budget_times = [(6, 0), (12, 0), (19, 0)]
    for h, m in budget_times:
        scheduler.add_job(
            _run_budget_alert,
            trigger=CronTrigger(hour=h, minute=m, second=0),
            args=[config, h],
            id=f'budget_alert_{h:02d}_{m:02d}',
            name=f'予算アラート ({h:02d}:{m:02d})',
            replace_existing=True,
        )
    logger.info(
        f"予算アラート発火: {', '.join(f'{h:02d}:{m:02d}' for h, m in budget_times)}"
    )

    # ── 動画学習 quota reset 直後の自動再開 (2026-04-26 追加) ──
    # Gemini Free Tier 20 RPD は PT midnight = JST 16:00 にリセット.
    # 16:30 に bulk script を起動して、failed を新しい順から再処理する.
    # quota 余裕 5 件分 (= 15 件 max) を残し、他用途とぶつからないようにする.
    scheduler.add_job(
        _run_video_learning_after_reset,
        trigger=CronTrigger(hour=16, minute=30, second=0),
        args=[config, 16],
        id='video_learning_after_reset_16_30',
        name='動画学習 quota reset 後再開 (16:30)',
        replace_existing=True,
    )
    logger.info("動画学習 quota reset 後再開: 毎日 16:30")

    # ── W7-A 市場戦略 refresh (2026-04-27 追加) ──
    # Terapeak (Research Products) を週次で全 SKU 解析 → primary_market 判定.
    # 動画 [60JJUZaMdpo] の 70% 閾値ベース.
    # CDP Chrome が起動してない場合は自動 skip + Discord 通知.
    scheduler.add_job(
        _run_market_analysis_refresh,
        trigger=CronTrigger(day_of_week='sun', hour=2, minute=0, second=0),
        args=[config],
        id='market_analysis_refresh_sun_02',
        name='W7-A 市場戦略 refresh (日 02:00)',
        replace_existing=True,
    )
    logger.info("W7-A 市場戦略 refresh: 毎週日曜 02:00")

    # ── W7-A 注文アラート (2026-04-27 追加) ──
    # v81 (2026-06-25): 30分→5分。GetOrders 1 call/run と軽量・全処理冪等
    # (dedup 済) なので 5分実行で重複副作用なし。売れてから MonoDeck 在庫反映を
    # 最大8.5h→5分に短縮 + 売却専用ch 通知 + 無在庫→仕入先 queue を高頻度化。
    # second=20 は主 batch (00/30 分 second=0) との発火衝突回避 (L1133 と同流儀)。
    # 検知 2 種 (DDP-B 発送 invoice / $1500+ EU) + 売却アクション (v81)。
    scheduler.add_job(
        _run_order_alert_check,
        trigger=CronTrigger(minute='*/5', second=20),
        args=[config],
        id='order_alert_check',
        name='注文アラート+売却同期 (5分ごと)',
        replace_existing=True,
    )
    logger.info("注文アラート+売却同期: 5分ごと (v81)")

    # ── W183 ライバル価格 refresh + 値下げ (2026-05-10 追加) ──
    # L1 Phase 3 Clarify: 6 時間間隔.
    # 主 batch (02:30/11/15/18/22) との衝突を避けて minute=45 で発火.
    # → 00:45 / 06:45 / 12:45 / 18:45 JST.
    rival_pricing_hours = [0, 6, 12, 18]
    for h in rival_pricing_hours:
        scheduler.add_job(
            _run_rival_pricing_refresh,
            trigger=CronTrigger(hour=h, minute=45, second=0),
            args=[config, h],
            id=f'rival_pricing_refresh_{h:02d}_45',
            name=f'W183 ライバル価格 refresh ({h:02d}:45)',
            replace_existing=True,
        )
    logger.info(
        f"W183 ライバル価格 refresh 発火: 毎日 "
        f"{', '.join(f'{h:02d}:45' for h in rival_pricing_hours)}"
    )

    # ── W122 朝の新商品発掘 (2026-05-13 追加) ──
    # 毎朝 07:00 JST に Opus 4.8 が 5 階層構造で 3 件発掘.
    # 主 batch (02:30/11/15/18/22) との衝突を避けるため独立 cron.
    # 既存 morning_brief (W24, 02:30) とは別役割で並設.
    scheduler.add_job(
        _run_morning_discovery,
        trigger=CronTrigger(hour=7, minute=0, second=0),
        args=[config, 7],
        id='morning_discovery_07_00',
        name='W122 朝の新商品発掘 (07:00)',
        replace_existing=True,
        max_instances=1,
    )
    logger.info("W122 朝の新商品発掘 発火: 毎日 07:00 JST")

    # ── W125 daily_codex_lint (2026-05-15 追加) ──
    # 毎日 03:00 JST に直近 7 日編集された memory / KB / 設計書を Codex lint.
    # 主 batch (02:30) 直後の独立 cron, Plus 5h 枠の余裕で動かす.
    # HIGH 3+ で Discord 通知, 上限 30 files/run.
    scheduler.add_job(
        _run_daily_codex_lint,
        trigger=CronTrigger(hour=3, minute=0, second=0),
        args=[config, 3],
        id='daily_codex_lint_03_00',
        name='W125 daily_codex_lint (03:00)',
        replace_existing=True,
        max_instances=1,
    )
    logger.info("W125 daily_codex_lint 発火: 毎日 03:00 JST")

    # ── W229 商品リサーチ発掘 (2026-06-10 追加) ──
    # 毎日 03:30 JST に Terapeak 2 パターン収穫 + 自動ゲート判定.
    # 02:30 batch 終了後. CDP Chrome 専有. market_analysis (日 02:00) とはクォータ DB で調停.
    # daily_codex_lint (03:00) と 30 分ずらして CDP 占有競合を回避.
    # enabled は config (tasks_enabled.research_harvest.enabled) で制御.
    # 2026-06-10 Q1 実機 PASS 後に user 承認で enabled=true 化.
    scheduler.add_job(
        _run_research_harvest,
        trigger=CronTrigger(hour=3, minute=30, second=0),
        args=[config, 3],
        id='research_harvest_03_30',
        name='W229 商品リサーチ発掘 (03:30)',
        replace_existing=True,
        max_instances=1,
    )
    _rh_enabled = config.get('tasks_enabled', {}).get('research_harvest', {}).get('enabled', False)
    logger.info(f"W229 商品リサーチ発掘 発火: 毎日 03:30 JST (enabled={_rh_enabled})")

    # ── W228 Phase 3 リサーチ探索 (2026-06-11 追加) ──
    # 毎日 04:30 JST に gate_passed 候補のフリマ探索→AI重量推定→利益計算→承認キュー積み.
    # harvest (03:30) 完了 1h 後. Claude Haiku 4.5 + evaluate_product (subprocess Playwright).
    # コスト上限 $3/日 (fail-CLOSED). enabled は config (tasks_enabled.research_sourcing.enabled) で制御.
    scheduler.add_job(
        _run_research_sourcing,
        trigger=CronTrigger(hour=4, minute=30, second=0),
        args=[config, 4],
        id='research_sourcing_04_30',
        name='W228 リサーチ探索 (04:30)',
        replace_existing=True,
        max_instances=1,
    )
    _rs_enabled = config.get('tasks_enabled', {}).get('research_sourcing', {}).get('enabled', False)
    logger.info(f"W228 リサーチ探索 発火: 毎日 04:30 JST (enabled={_rs_enabled})")

    # ── W286 リサーチ対戦アリーナ (2026-06-28 追加) ──
    # 毎日 05:00 JST に 6 日ローテ × 3 カテゴリで AI が 5 品リサーチ → duel_round 保存.
    # research_sourcing (04:30) 完了後・user 起動前 (06:00) の空き枠.
    # CDP Chrome (port 9222) 必須 (research_harvest と同じ前提). 不在時は痕跡 skip (Q0).
    # 既存 CDP タスク: research_harvest=03:30, research_sourcing=04:30 (subprocess のみ).
    # 05:00 は CDP を占有するタスクなし = 競合ゼロ。
    # enabled は config (tasks_enabled.research_duel.enabled) で制御.
    scheduler.add_job(
        _run_research_duel,
        trigger=CronTrigger(hour=5, minute=0, second=0),
        args=[config, 5],
        id='research_duel_05_00',
        name='W286 リサーチ対戦アリーナ (05:00)',
        replace_existing=True,
        max_instances=1,
    )
    _rd_enabled = config.get('tasks_enabled', {}).get('research_duel', {}).get('enabled', False)
    logger.info(f"W286 リサーチ対戦アリーナ 発火: 毎日 05:00 JST (enabled={_rd_enabled})")

    # ── W301 AI 店長 Phase1 S4 競合分類 (2026-07-02 追加) ──
    # 毎日 03:00 JST に listing_rival_discoveries (status='new') を rival_classifier
    # (ハード除外→スコア→グレーのみ Claude Haiku) で分類. daily_codex_lint (03:00) と
    # 同時刻だが別 job id + thread-local batch ctx (既存対策済) で共存可.
    # Shadow 固定 (pricing_eligible は本タスクから一切変更しない、設計書 §8).
    # enabled は config (tasks_enabled.rival_classify.enabled) で制御.
    scheduler.add_job(
        _run_rival_classify,
        trigger=CronTrigger(hour=3, minute=0, second=0),
        args=[config, 3],
        id='rival_classify_03_00',
        name='W301 AI店長 競合分類 (03:00)',
        replace_existing=True,
        max_instances=1,
    )
    _rc_enabled = config.get('tasks_enabled', {}).get('rival_classify', {}).get('enabled', True)
    logger.info(f"W301 AI店長 競合分類 発火: 毎日 03:00 JST (enabled={_rc_enabled})")

    # ── W301 AI 店長 Phase1 S4 競合 GetItem 定点観測 (2026-07-02 追加) ──
    # 毎日 05:30 JST に pricing_eligible=1 の active 競合 + rival_classifications
    # real/review 競合を対象に GetItem で定点観測データを competitor_snapshots へ蓄積
    # (Phase1 は蓄積のみ、消費は Phase2). research_duel (05:00) 完了後の空き枠.
    # enabled は config (tasks_enabled.competitor_snapshot.enabled) で制御.
    scheduler.add_job(
        _run_competitor_snapshot,
        trigger=CronTrigger(hour=5, minute=30, second=0),
        args=[config, 5],
        id='competitor_snapshot_05_30',
        name='W301 AI店長 競合定点観測 (05:30)',
        replace_existing=True,
        max_instances=1,
    )
    _cs_enabled = config.get('tasks_enabled', {}).get('competitor_snapshot', {}).get('enabled', True)
    logger.info(f"W301 AI店長 競合定点観測 発火: 毎日 05:30 JST (enabled={_cs_enabled})")

    # ── W283 Phase 9 月次送料 rate table 自動更新 (2026-06-19 追加) ──
    # 毎月1日 03:00 JST に FedEx/DHL 実費差額式で rate table 金額を自動追従.
    # 前月為替確定後・主 batch (02:30) 後. codex_lint(03:00) と同時刻だが別 owner・
    # 月1回のみ。mode=dry_run の間は updateShippingCost を呼ばず diff 通知のみ (初月検証)。
    # 検証後 user が config.rate_table_batch.mode='auto' へ昇格。
    scheduler.add_job(
        _run_rate_table_batch,
        trigger=CronTrigger(day=1, hour=3, minute=0, second=0),
        args=[config, 3],
        id='rate_table_monthly_update',
        name='W283 月次送料 rate table 自動更新 (毎月1日 03:00)',
        replace_existing=True,
        max_instances=1,
    )
    _rtb_enabled = config.get('tasks_enabled', {}).get('rate_table_monthly_update', {}).get('enabled', False)
    _rtb_mode = (config.get('rate_table_batch') or {}).get('mode', 'dry_run')
    logger.info(f"W283 月次送料 rate table 更新 発火: 毎月1日 03:00 JST (enabled={_rtb_enabled}, mode={_rtb_mode})")

    # ── W284 Phase 2 eBaymag 反映キュー自動消化 (2026-06-20 追加) ──
    # CDP+eBaymagログイン生存時に ebaymag_apply_queue を消化。
    # CDP 不在が常態なので頻度は控えめ = 1 日 3 回 (主 batch 後タイミング)。
    # 11:30 / 15:30 / 22:30 JST (主 batch :00/:30 の直後オフセット)。
    # enabled は config (tasks_enabled.ebaymag_apply_queue.enabled) で制御。
    _emq_cfg = (config.get('tasks_enabled', {}) or {}).get('ebaymag_apply_queue', {}) or {}
    # money-direct タスク (各国版 mutate)。config 欠落時は fail-safe OFF (HIGH-A 修正 2026-06-20)。
    # canary 後に schedule_config.json の tasks_enabled.ebaymag_apply_queue.enabled=true で昇格。
    if _emq_cfg.get('enabled', False):
        for _emq_h, _emq_m in [(11, 30), (15, 30), (22, 30)]:
            scheduler.add_job(
                _run_ebaymag_apply_queue,
                trigger=CronTrigger(hour=_emq_h, minute=_emq_m, second=0),
                args=[config, _emq_h],
                id=f'ebaymag_apply_queue_{_emq_h:02d}_{_emq_m:02d}',
                name=f'W284 eBaymag 反映キュー消化 ({_emq_h:02d}:{_emq_m:02d})',
                replace_existing=True,
                max_instances=1,
            )
        logger.info("W284 eBaymag 反映キュー消化 発火: 毎日 11:30 / 15:30 / 22:30 JST")
    else:
        logger.info("W284 eBaymag 反映キュー消化: disabled (tasks_enabled.ebaymag_apply_queue.enabled=false)")

    # ── #44 Wave2 出品内容監査 (2026-07-04 追加) ──
    # 日次 (02:15 JST、主 batch 02:30 と重ならない時刻)。GetItem で
    # title/condition/ItemSpecifics(禁止Name)/ConditionDescription/画像を突合。
    # enabled は config (tasks_enabled.listing_content_audit.enabled) で制御。
    _lca_cfg = (config.get('tasks_enabled', {}) or {}).get('listing_content_audit', {}) or {}
    if _lca_cfg.get('enabled', True):
        scheduler.add_job(
            _run_listing_content_audit,
            trigger=CronTrigger(hour=2, minute=15, second=0),
            args=[config, 2],
            id='listing_content_audit_02_15',
            name='#44 出品内容監査 (02:15)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info("#44 出品内容監査 発火: 毎日 02:15 JST")
    else:
        logger.info("#44 出品内容監査: disabled (tasks_enabled.listing_content_audit.enabled=false)")

    # ── #45 仕入先候補 availability 再チェック (2026-07-04 追加) ──
    # 日次 (02:50 JST、主 batch 02:30 後・daily_codex_lint/rival_classify 03:00 前)。
    # pending/accepted の候補を再チェックし sold_out/not_found を却下 (混入の恒久対策)。
    # enabled は config (tasks_enabled.supplier_availability_recheck.enabled) で制御。
    _sar_cfg = (config.get('tasks_enabled', {}) or {}).get('supplier_availability_recheck', {}) or {}
    if _sar_cfg.get('enabled', True):
        scheduler.add_job(
            _run_supplier_availability_recheck,
            trigger=CronTrigger(hour=2, minute=50, second=0),
            args=[config, 2],
            id='supplier_availability_recheck_02_50',
            name='#45 仕入先候補 availability 再チェック (02:50)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info("#45 仕入先候補 availability 再チェック 発火: 毎日 02:50 JST")
    else:
        logger.info(
            "#45 仕入先候補 availability 再チェック: disabled "
            "(tasks_enabled.supplier_availability_recheck.enabled=false)"
        )

    # ── W284 Phase 2 eBaymag 更新同期 監査 (2026-06-20 追加) ──
    # 日次 (02:45 JST、主 batch 02:30 直後)。GetItem で US本体 vs 各国版を突合。
    # enabled は config (tasks_enabled.ebaymag_sync_audit.enabled) で制御。
    _esa_cfg = (config.get('tasks_enabled', {}) or {}).get('ebaymag_sync_audit', {}) or {}
    if _esa_cfg.get('enabled', True):
        scheduler.add_job(
            _run_ebaymag_sync_audit,
            trigger=CronTrigger(hour=2, minute=45, second=0),
            args=[config, 2],
            id='ebaymag_sync_audit_02_45',
            name='W284 eBaymag 更新同期 監査 (02:45)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info("W284 eBaymag 更新同期 監査 発火: 毎日 02:45 JST")
    else:
        logger.info("W284 eBaymag 更新同期 監査: disabled (tasks_enabled.ebaymag_sync_audit.enabled=false)")

    # ── W284 Phase 3 eBaymag-aware relist (窓ゼロ) (2026-06-20 追加) ──
    # CDP+eBaymagログイン在席時に eBaymag 商品の relist を窓ゼロで完結。
    # feature flag (tasks_enabled.ebaymag_relist.enabled) は既定 False。
    # flag OFF でも add_job は行う (flag-gated = run 時に task 内で即 skip)。
    # タイミング: ebaymag_apply_queue (:30:00) の 30 秒後 (:30:30)。
    # HIGH-2 訂正 (code-reviewer 2026-06-20): Phase2 定時はこの relist より先に実行済の
    # ため、relist 後 discover ラグで委譲した relist_relink ジョブは同回の Phase2 では
    # 拾われない。窓 (各国版未公開) を縮めるため、run_ebaymag_relist 末尾で自前に
    # run_ebaymag_apply_queue を1回呼んで同 run で消化する (旧コメント「同一タイミングで
    # 問題なし」は誤りだった)。
    _emr_cfg = (config.get('tasks_enabled', {}) or {}).get('ebaymag_relist', {}) or {}
    for _emr_h, _emr_m in [(11, 30), (15, 30), (22, 30)]:
        scheduler.add_job(
            _run_ebaymag_relist,
            trigger=CronTrigger(hour=_emr_h, minute=_emr_m, second=30),
            args=[config, _emr_h],
            id=f'ebaymag_relist_{_emr_h:02d}_{_emr_m:02d}',
            name=f'W284 eBaymag relist 窓ゼロ ({_emr_h:02d}:{_emr_m:02d})',
            replace_existing=True,
            max_instances=1,
        )
    _emr_status = "enabled" if _emr_cfg.get('enabled', False) else "disabled (flag OFF)"
    logger.info(
        "W284 eBaymag relist (窓ゼロ) 発火: 毎日 11:30:30 / 15:30:30 / 22:30:30 JST "
        f"({_emr_status})"
    )

    # ── W131 P5 claude_loop_healthcheck (2026-05-16 追加) ──
    # 30 分ごとに claude auto-restart loop の heartbeat を確認、stale なら auto-recovery.
    # SessionStart hook (user セッション開始時) と並列で watcher-of-watcher を構成.
    # 主 batch (00/30 分) 衝突を避けて minute=*/30 + second=15 で発火.
    scheduler.add_job(
        _run_claude_loop_healthcheck,
        trigger=CronTrigger(minute='*/30', second=15),
        args=[config],
        id='claude_loop_healthcheck',
        name='W131 P5 claude-loop watcher (30分ごと)',
        replace_existing=True,
        max_instances=1,
    )
    logger.info("W131 P5 claude_loop_healthcheck 発火: 30 分ごと (*/30 :15)")

    # ── W148 キーワード新着監視 (2026-05-21 追加) ──
    # 2 時間ごと :20 分 (主 batch 02:30/11:30/15/18/22 と衝突しないオフセット).
    # subprocess (`python -m tasks.task_keyword_watch_crawl`) で別プロセス起動し
    # sync_playwright を APScheduler worker thread から物理分離 (mercari_search.py
    # の main thread sequential 前提を満たす).
    kw_cfg = (config.get('tasks_enabled', {}) or {}).get('keyword_watch_crawl', {}) or {}
    if kw_cfg.get('enabled', True):
        interval_hours = int(kw_cfg.get('interval_hours', 2))
        scheduler.add_job(
            _run_keyword_watch_crawl,
            trigger=CronTrigger(hour=f'*/{interval_hours}', minute=20, second=0),
            args=[config],
            id='keyword_watch_crawl',
            name=f'W148 キーワード新着監視 ({interval_hours}h ごと :20)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            f"W148 キーワード新着監視 発火: {interval_hours} 時間ごと :20 分 (subprocess)"
        )

    # ── W293 eBaymag セッション維持 heartbeat (2026-06-29 追加) ──
    # 15 分ごと :45 秒 (主 batch :00/:30 および order_alert :05/:20 との発火衝突回避)。
    # enabled は config (tasks_enabled.ebaymag_session_heartbeat.enabled) で制御。
    # kill switch 二重ガード: config gate (ここ) + task 内部 config 再確認。
    _hb_cfg = (config.get('tasks_enabled', {}) or {}).get('ebaymag_session_heartbeat', {}) or {}
    if _hb_cfg.get('enabled', True):
        _hb_iv = int(_hb_cfg.get('interval_minutes', 15))
        scheduler.add_job(
            _run_ebaymag_session_heartbeat,
            trigger=CronTrigger(minute=f'*/{_hb_iv}', second=45),
            args=[config],
            id='ebaymag_session_heartbeat',
            name=f'W293 eBaymag セッション維持 ({_hb_iv}分ごと)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info(f"W293 eBaymag セッション維持 発火: {_hb_iv} 分ごと :45 秒")
    else:
        logger.info("W293 eBaymag セッション維持: disabled (tasks_enabled.ebaymag_session_heartbeat.enabled=false)")

    return scheduler


def _run_keyword_watch_crawl(config: dict):
    """W148 キーワード新着監視 — subprocess で task_keyword_watch_crawl を別プロセス起動。

    sync_playwright を APScheduler worker thread から物理分離 (mercari_search.py
    の main thread sequential 前提を満たす根本対応、Codex 2 段 HIGH-1)。
    """
    def _launch_subprocess() -> dict:
        cwd = Path(__file__).resolve().parent  # tools/ebay-manager
        timeout_sec = int(
            (config.get('tasks_enabled', {}) or {})
            .get('keyword_watch_crawl', {})
            .get('subprocess_timeout_sec', 600)
        )
        try:
            res = subprocess.run(
                [sys.executable, "-m", "tasks.task_keyword_watch_crawl"],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,  # Windows pythonw deadlock 防止
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env={**os.environ},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"keyword_watch_crawl subprocess timeout ({timeout_sec}s)"
            ) from e
        if res.returncode != 0:
            raise RuntimeError(
                f"keyword_watch_crawl subprocess failed (exit={res.returncode}): "
                f"stderr={(res.stderr or '')[:500]} stdout={(res.stdout or '')[:300]}"
            )
        stdout_tail = (res.stdout or '').strip()[:300]
        logger.info(f"keyword_watch_crawl OK: {stdout_tail}")
        # Codex 2 段 HIGH-A: _run_isolated_task は dict + .get("success") 期待。
        # string 返却は AttributeError で成功が failed 記録になるため必ず dict 化。
        return {"success": True, "message": stdout_tail}

    _run_isolated_task('keyword_watch_crawl', 'W148 キーワード新着監視',
                       _launch_subprocess,
                       scheduled_hour=None)


def _run_ebaymag_session_heartbeat(config: dict):
    """W293 eBaymag セッション維持 heartbeat — 15 分ごと。"""
    try:
        from tasks.task_ebaymag_session_heartbeat import run_ebaymag_session_heartbeat
    except ImportError as e:
        logger.error(f"task_ebaymag_session_heartbeat import 失敗: {e}")
        return
    _run_isolated_task('ebaymag_session_heartbeat', 'W293 eBaymag セッション維持',
                       lambda: run_ebaymag_session_heartbeat(config),
                       scheduled_hour=None)


def _run_budget_alert(config: dict, scheduled_hour: int = 12):
    """予算アラート cron — 1 日 3 回 Anthropic API 予算状況を Discord 通知."""
    try:
        from tasks.task_budget_alert import run_budget_alert
    except ImportError as e:
        logger.error(f"task_budget_alert import 失敗: {e}")
        return
    _run_isolated_task('budget_alert', '予算アラート',
                       lambda: run_budget_alert(config),
                       scheduled_hour=scheduled_hour)


def _run_market_analysis_refresh(config: dict):
    """W7-A 市場戦略 refresh — 週次で Terapeak 全 SKU 解析."""
    try:
        from tasks.task_market_analysis_refresh import run_market_analysis_refresh
    except ImportError as e:
        logger.error(f"task_market_analysis_refresh import 失敗: {e}")
        return
    _run_isolated_task('market_analysis_refresh', 'W7-A 市場戦略 refresh',
                       lambda: run_market_analysis_refresh(config),
                       scheduled_hour=2)


def _run_order_alert_check(config: dict):
    """W7-A 注文アラート — 30分ごとに新規注文を polling."""
    try:
        from tasks.task_order_alert import run_order_alert_check
    except ImportError as e:
        logger.error(f"task_order_alert import 失敗: {e}")
        return
    _run_isolated_task('order_alert_check', 'W7-A 注文アラート',
                       lambda: run_order_alert_check(config),
                       scheduled_hour=None)


def _run_rival_pricing_refresh(config: dict, scheduled_hour: int):
    """W183 ライバル価格 refresh + 値下げ判定 — 6 時間ごと (00/06/12/18 時 :45)."""
    try:
        from tasks.task_rival_pricing import run_rival_pricing_refresh
    except ImportError as e:
        logger.error(f"task_rival_pricing import 失敗: {e}")
        return
    _run_isolated_task('rival_pricing_refresh', 'W183 ライバル価格 refresh',
                       lambda: run_rival_pricing_refresh(config),
                       scheduled_hour=scheduled_hour)


def _run_morning_discovery(config: dict, scheduled_hour: int = 7):
    """W122 朝の新商品発掘 — 1 日 1 回 Opus 4.8 で 3 件発掘 (07:00 JST)."""
    try:
        from tasks.task_morning_discovery import run_morning_discovery
    except ImportError as e:
        logger.error(f"task_morning_discovery import 失敗: {e}")
        return
    _run_isolated_task('morning_discovery', 'W122 朝の新商品発掘',
                       lambda: run_morning_discovery(config),
                       scheduled_hour=scheduled_hour)


def _run_daily_codex_lint(config: dict, scheduled_hour: int = 3):
    """W125 daily_codex_lint — 直近 7 日編集された memory / KB / 設計書を Codex lint (03:00 JST)."""
    try:
        from tasks.task_daily_codex_lint import run as run_daily_codex_lint
    except ImportError as e:
        logger.error(f"task_daily_codex_lint import 失敗: {e}")
        return
    _run_isolated_task('daily_codex_lint', 'W125 Codex 文書 lint',
                       lambda: run_daily_codex_lint(config),
                       scheduled_hour=scheduled_hour)


def _run_research_harvest(config: dict, scheduled_hour: int = 3):
    """W229 商品リサーチ発掘 — 毎日 03:30 JST に Terapeak ハーベスト + 自動ゲート判定."""
    try:
        from tasks.task_research_harvest import run_research_harvest
    except ImportError as e:
        logger.error(f"task_research_harvest import 失敗: {e}")
        return
    _run_isolated_task('research_harvest', 'W229 商品リサーチ発掘',
                       lambda: run_research_harvest(config),
                       scheduled_hour=scheduled_hour)


def _run_research_sourcing(config: dict, scheduled_hour: int = 4):
    """W228 リサーチ探索 — 毎日 04:30 JST に gate_passed 候補のフリマ探索→利益→キュー積み."""
    try:
        from tasks.task_research_sourcing import run_research_sourcing
    except ImportError as e:
        logger.error(f"task_research_sourcing import 失敗: {e}")
        return
    _run_isolated_task('research_sourcing', 'W228 リサーチ探索',
                       lambda: run_research_sourcing(config),
                       scheduled_hour=scheduled_hour)


def _run_research_duel(config: dict, scheduled_hour: int = 5):
    """W286 リサーチ対戦アリーナ — 毎日 05:00 JST に 6 日ローテ×3 カテゴリで 5 品 AI リサーチ."""
    try:
        from tasks.task_research_duel import run_research_duel
    except ImportError as e:
        logger.error(f"task_research_duel import 失敗: {e}")
        return
    _run_isolated_task('research_duel', 'W286 リサーチ対戦アリーナ',
                       lambda: run_research_duel(config),
                       scheduled_hour=scheduled_hour)


def _run_rival_classify(config: dict, scheduled_hour: int = 3):
    """W301 AI 店長 Phase1 S4 — 毎日 03:00 JST に競合分類 (Shadow 固定)."""
    try:
        from tasks.task_rival_classify import run_rival_classify
    except ImportError as e:
        logger.error(f"task_rival_classify import 失敗: {e}")
        return
    _run_isolated_task('rival_classify', 'W301 AI店長 競合分類',
                       lambda: run_rival_classify(config),
                       scheduled_hour=scheduled_hour)


def _run_competitor_snapshot(config: dict, scheduled_hour: int = 5):
    """W301 AI 店長 Phase1 S4 — 毎日 05:30 JST に競合 GetItem 定点観測 (蓄積のみ)."""
    try:
        from tasks.task_competitor_snapshot import run_competitor_snapshot
    except ImportError as e:
        logger.error(f"task_competitor_snapshot import 失敗: {e}")
        return
    _run_isolated_task('competitor_snapshot', 'W301 AI店長 競合定点観測',
                       lambda: run_competitor_snapshot(config),
                       scheduled_hour=scheduled_hour)


def _run_rate_table_batch(config: dict, scheduled_hour: int = 3):
    """W283 Phase 9 — 毎月1日 03:00 JST に送料 rate table を実費差額式へ自動更新.

    mode は config.rate_table_batch.mode (既定 dry_run)。dry_run は updateShippingCost を
    呼ばず diff を Discord/DB に記録するのみ。kill switch =
    tasks_enabled.rate_table_monthly_update.enabled (false で停止)。
    """
    if not config.get('tasks_enabled', {}).get('rate_table_monthly_update', {}).get('enabled', False):
        logger.info("rate_table_monthly_update: disabled (kill switch) — skip")
        return
    try:
        from scripts.shipping_rate_batch import run_batch as rtb
    except ImportError as e:
        logger.error(f"shipping_rate_batch import 失敗: {e}")
        # 月次 = 月1回発火。痕跡を残さないと次の発火まで誰も気付かない (Q0 silent skip)。
        _run_isolated_task('rate_table_monthly_update', '月次送料 rate table 自動更新',
                           lambda: {"success": False,
                                    "message": f"import 失敗: {type(e).__name__}: {e}"},
                           scheduled_hour=scheduled_hour)
        return

    def _runner() -> dict:
        res = rtb.run_batch(config)
        ok = res.get("outcome") in ("dry_run", "auto_applied")
        msg = f"outcome={res.get('outcome')} changes={res.get('n_changes')} run={res.get('run_id')}"
        return {"success": ok, "message": msg, **res}

    _run_isolated_task('rate_table_monthly_update', '月次送料 rate table 自動更新',
                       _runner, scheduled_hour=scheduled_hour)


def _run_claude_loop_healthcheck(config: dict):
    """W131 P5 claude_loop_healthcheck — 30 分ごとに claude auto-restart loop の生存を監視.

    scheduled_hour=None: 30 分毎で固定 hour なし.
    DOWN 検知時は start-claude-loop.ps1 を spawn + Discord 通知 (R-11).
    """
    try:
        from tasks.task_claude_loop_healthcheck import run as run_healthcheck
    except ImportError as e:
        logger.error(f"task_claude_loop_healthcheck import 失敗: {e}")
        return
    _run_isolated_task('claude_loop_healthcheck', 'W131 P5 claude-loop watcher',
                       lambda: run_healthcheck(config),
                       scheduled_hour=None)


def _run_video_learning_after_reset(config: dict, scheduled_hour: int = 16):
    """Gemini quota reset 直後 (16:30 JST) の自動再開.

    failed を pending にリセットし、新しい順 15 件まで処理する.
    """
    logger.info("=" * 60)
    logger.info("【動画学習 quota reset 後再開】")

    def _runner() -> dict:
        import subprocess
        import os
        env = os.environ.copy()
        env.setdefault("VIDEO_LEARNING_MAX_PER_RUN", "15")
        env.setdefault("VIDEO_LEARNING_SLEEP_SEC", "15")
        env.setdefault("VIDEO_LEARNING_DOWNLOAD", "1")
        cookies = Path(__file__).parent / "cookies" / "youtube.txt"
        if cookies.exists():
            env["YTDLP_COOKIES_FILE"] = str(cookies)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "scripts.run_all_video_learning_2026_04_25"],
                env=env, capture_output=True, text=True, timeout=2400,
                cwd=str(Path(__file__).parent),
            )
            return {
                "success": r.returncode == 0,
                "stdout_tail": (r.stdout or "")[-500:],
                "stderr_tail": (r.stderr or "")[-500:],
                "message": f"exit={r.returncode}",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "timeout (>40min)"}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "message": f"{type(e).__name__}: {e}"}

    _run_isolated_task('video_learning_resume', '動画学習 quota reset 後再開',
                       _runner, scheduled_hour=scheduled_hour)


def _run_isolated_task(task_key: str, display_name: str, runner,
                        scheduled_hour):
    """独立 cron 用の薄いラッパー. batch_ctx を save/restore して execution_log を記録する.

    scheduled_hour: cron 予定時刻の hour (int). 30 分ごと task など固定 hour 無しの
        場合は None を許容し、現在時刻から導出する (W7-A 注文アラート 等).

    重要 (2026-05-05 daily_relist 6 日 silent skip 事故対策):
        本関数は execute_daily_tasks の長時間 batch (例: 02:30 → 03:13) と並行に
        APScheduler スレッドで動く. 旧実装は `_batch_ctx` をグローバルに上書きし、
        並行実行中の daily batch の `should_task_run` 判定 (= `_batch_ctx["hour"]`
        参照) を破壊していた (例: 03:00 order_alert_check 発火で hour=3 に上書き
        → 03:13 daily_relist が `batch_hour=3 not in [2]` で skip).
        【2026-05-18 訂正】save/restore は thread 跨ぎで非 composable のため
        単独では不十分だった (03:00 codex_lint 等が長時間 run 中に 02:30 batch
        が clobbered hour=3 を読み daily_relist 等が毎日 silent skip)。根治は
        `_batch_ctx` の thread-local 化 (module 冒頭 _ThreadLocalBatchCtx)。
        本 save/restore は thread-local 下では同一 thread 内の後始末として
        無害に機能する (異 thread の daily batch には元々到達しない)。
    """
    started_dt = datetime.now()
    if scheduled_hour is None:
        # 30 分ごと task など固定 hour なし — 現在時刻を使用
        bh = started_dt.hour
    else:
        bh = int(scheduled_hour)
    batch_id = f"{started_dt.strftime('%Y%m%d')}_{bh:02d}sched_{task_key[:12]}"

    # 並行 daily batch の context を破壊しないよう save & restore.
    saved_ctx = dict(_batch_ctx)
    _batch_ctx["id"] = batch_id
    _batch_ctx["hour"] = bh
    _batch_ctx["started_at"] = started_dt
    log_id = None
    try:
        try:
            from monitor.task_execution_log import log_task_start
            log_id = log_task_start(
                task_key=task_key,
                display_name=display_name,
                batch_id=batch_id,
                batch_hour=bh,
            )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"task_execution_log start 失敗 ({task_key}): {_le}")
        started_ts = time.time()
        success = False
        msg = ""
        try:
            r = runner() or {}
            success = bool(r.get("success", False))
            msg = json.dumps(r, ensure_ascii=False, default=str)[:1000]
            if success:
                logger.info(f"【完了】{display_name}: {r.get('message')}")
            else:
                logger.warning(f"【失敗】{display_name}: {r.get('message')}")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            logger.error(f"{display_name} 例外: {e}", exc_info=True)
        finally:
            if log_id is not None:
                try:
                    from monitor.task_execution_log import log_task_finish
                    log_task_finish(
                        log_id=log_id,
                        success=success,
                        message=msg,
                        duration_sec=time.time() - started_ts,
                    )
                except Exception as _le:  # noqa: BLE001
                    logger.warning(f"task_execution_log finish 失敗 ({task_key}): {_le}")
    finally:
        # _batch_ctx を save 前の状態に restore. 並行 daily batch の値が保たれる.
        _batch_ctx.clear()
        _batch_ctx.update(saved_ctx)


def _run_news_only(config: dict, scheduled_hour: int = 6):
    """W154 AI ニュース取得 専用の独立実行 (他タスクと併走しない)."""
    logger.info("============================================================")
    logger.info("【W154 AI ニュース取得 単独実行】")
    from tasks.task_news_check import run_news_check
    _run_isolated_task('news_check', 'W154 AI ニュース取得',
                       lambda: run_news_check(config), scheduled_hour=scheduled_hour)


def _run_customs_check_only(config: dict, scheduled_hour: int = 6):
    """W14 専用の独立実行 (他タスクと併走しない、06:10 オフセット)."""
    logger.info("============================================================")
    logger.info("【W14 通関対応 単独実行】")
    from tasks.task_customs_check import run_customs_check
    _run_isolated_task('customs_check', 'W14 通関対応',
                       lambda: run_customs_check(config), scheduled_hour=scheduled_hour)


def _run_ebaymag_apply_queue(config: dict, scheduled_hour: int = 11):
    """W284 Phase 2 eBaymag 反映キュー消化 — CDP+eBaymagログイン生存時に active job を消化."""
    try:
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
    except ImportError as e:
        logger.error(f"task_ebaymag_apply_queue import 失敗: {e}")
        return
    _run_isolated_task('ebaymag_apply_queue', 'W284 eBaymag 反映キュー消化',
                       lambda: run_ebaymag_apply_queue(config),
                       scheduled_hour=scheduled_hour)


def _run_ebaymag_sync_audit(config: dict, scheduled_hour: int = 2):
    """W284 Phase 2 eBaymag 更新同期 監査 — US本体 vs 各国版の乖離検出."""
    try:
        from tasks.task_ebaymag_sync_audit import run_ebaymag_sync_audit
    except ImportError as e:
        logger.error(f"task_ebaymag_sync_audit import 失敗: {e}")
        return
    _run_isolated_task('ebaymag_sync_audit', 'W284 eBaymag 更新同期 監査',
                       lambda: run_ebaymag_sync_audit(config),
                       scheduled_hour=scheduled_hour)


def _run_listing_content_audit(config: dict, scheduled_hour: int = 2):
    """#44 Wave2 US本体 DB↔eBay 整合性 日次突合 — title/condition/ItemSpecifics/画像."""
    try:
        from tasks.task_listing_content_audit import run_listing_content_audit
    except ImportError as e:
        logger.error(f"task_listing_content_audit import 失敗: {e}")
        return
    _run_isolated_task('listing_content_audit', '#44 出品内容監査 (DB↔eBay 突合)',
                       lambda: run_listing_content_audit(config),
                       scheduled_hour=scheduled_hour)


def _run_supplier_availability_recheck(config: dict, scheduled_hour: int = 2):
    """#45 仕入先候補 (pending/accepted) の availability 定期再チェック."""
    try:
        from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    except ImportError as e:
        logger.error(f"task_supplier_availability_recheck import 失敗: {e}")
        return
    _run_isolated_task('supplier_availability_recheck', '#45 仕入先候補 availability 再チェック',
                       lambda: run_supplier_availability_recheck(config),
                       scheduled_hour=scheduled_hour)


def _run_ebaymag_relist(config: dict, scheduled_hour: int = 11):
    """W284 Phase 3 eBaymag-aware relist (窓ゼロ) — CDP+eBaymagログイン在席時のみ実行。

    feature flag (tasks_enabled.ebaymag_relist.enabled) が False の場合は
    task 内で即 skip されるため、scheduler 登録自体は常に行う (flag-gated)。
    """
    try:
        from tasks.task_ebaymag_relist import run_ebaymag_relist
    except ImportError as e:
        logger.error(f"task_ebaymag_relist import 失敗: {e}")
        return
    _run_isolated_task('ebaymag_relist', 'W284 eBaymag relist (窓ゼロ)',
                       lambda: run_ebaymag_relist(config),
                       scheduled_hour=scheduled_hour)


def _run_health_check(config: dict, scheduled_hour: int = 4):
    """健康チェック cron — 本日 expected vs executed を照合し、欠落があれば Discord 即時通知.

    各 batch 終了後 (例: 02:30 batch は 03:25 頃完了 → 04:00 起動) に走らせる.
    検知後はそのまま autofix orchestrator へ **同じ結果 object を渡し** (再度
    ヘルスチェックを呼ばない = 非 dedupe alert の二重発火を防ぐ) Tier 別自動対処する。
    """
    try:
        from tasks.task_scheduler_health_check import run_scheduler_health_check
    except Exception as e:  # noqa: BLE001
        logger.error(f"health_check import 失敗: {e}")
        return

    def _runner():
        health = run_scheduler_health_check(config)
        try:
            from tasks.task_health_autofix import run_health_autofix
            health["autofix"] = run_health_autofix(config, health)
        except Exception as e:  # noqa: BLE001 — autofix 失敗で検知結果を失わない
            logger.error(f"health autofix 失敗: {e}", exc_info=True)
            health["autofix"] = {"error": str(e)}
        return health

    _run_isolated_task('scheduler_health_check', '定時実行ヘルスチェック',
                       _runner, scheduled_hour=scheduled_hour)


SCHEDULER_LOCK_FILE = Path(__file__).parent / 'data' / 'scheduler.lock'
SCHEDULER_PID_FILE = Path(__file__).parent / 'data' / 'scheduler.pid'


def acquire_singleton_lock():
    """W95: Windows file lock で多重起動を防ぐ。

    Returns: locked file handle (caller が起動中ずっと open 状態を保持).
    Exits: 別 instance が lock 保持中 / lock file open 不能 = sys.exit(1).

    実装メモ:
    - msvcrt.locking() でロックしたバイトはそのプロセス含め read 不可になるため、
      診断用 PID は別ファイル `scheduler.pid` に書き出す。
    - lock file は **必ず 1 byte 以上**を持つこと (msvcrt の Windows version 依存
      挙動で、0 byte ファイルだと LK_NBLCK が誤って成功する報告があるため H-1).
    - binary mode `'rb+'` で newline translation を回避 (H-3).
    - mkdir / touch / open 失敗時は logger.error + exit(1) で必ず user 通知 (H-2 Q0).

    OS-level lock のため process 死 (PC sleep / hard kill) で自動解放.
    stale PID 判定が不要で race condition フリー.
    """
    # H-2: file system 操作失敗を Q0 silent skip させない
    try:
        SCHEDULER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        # H-1: lock byte を確実に reserve するため 1 byte 書込み
        if not SCHEDULER_LOCK_FILE.exists() or SCHEDULER_LOCK_FILE.stat().st_size == 0:
            SCHEDULER_LOCK_FILE.write_bytes(b'\x00')
        # H-3: binary mode で newline translation 回避
        f = open(SCHEDULER_LOCK_FILE, 'rb+')
    except OSError as e:
        logger.error(
            f"singleton lock file open 失敗 ({SCHEDULER_LOCK_FILE}): "
            f"{type(e).__name__}: {e}. **多重起動防止が機能しません**。"
            f"data/ ディレクトリの permission を確認してください。"
        )
        sys.exit(1)

    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        # 既存 instance の PID は別ファイルから読み取り (lock しない)
        try:
            existing_pid = SCHEDULER_PID_FILE.read_text(encoding='ascii').strip() or '<unreadable>'
        except (OSError, UnicodeDecodeError):
            existing_pid = '<unreadable>'
        logger.error(
            f"別のスケジューラーが稼働中 (PID={existing_pid})。"
            f"多重起動防止のため終了します。"
        )
        sys.exit(1)
    # 診断用 PID 書込み (lock しない別ファイル)
    SCHEDULER_PID_FILE.write_text(str(os.getpid()), encoding='ascii')
    return f


def main():
    """メインエントリーポイント"""

    logger.info("eBay Manager Daily Scheduler v2 起動")
    logger.info(f"Python: {sys.version}")

    # W95: 多重起動防止 (file lock 取得失敗 = sys.exit(1))
    lock_handle = acquire_singleton_lock()
    logger.info(f"singleton lock acquired (PID={os.getpid()}, path={SCHEDULER_LOCK_FILE})")

    try:
        scheduler = setup_scheduler()
        scheduler.start()

        logger.info("スケジューラーが起動しました")

        for job in scheduler.get_jobs():
            logger.info(f"  - {job.name}: {job.trigger}")

        logger.info("スケジューラーを実行中... (Ctrl+C で停止)")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Ctrl+C が押されました。停止します...")

    except Exception as e:
        logger.error(f"スケジューラー起動エラー: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

    finally:
        if 'scheduler' in locals() and scheduler.running:
            scheduler.shutdown()
            logger.info("スケジューラーを停止しました")
        # W95: lock handle 明示 close で release を確実化 (handle leak 予防)
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
