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
import logging
import traceback
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
# バッチ実行コンテキスト (2026-04-25 hour ドリフト事故対応).
# execute_daily_tasks 開始時に batch_hour/batch_id を捕獲し、
# should_task_run / run_task はこれを参照する. datetime.now() 都度参照を廃止.
# 単一プロセス・単一 batch 直列実行が前提.
# ──────────────────────────────────────────────────────────────────────
_batch_ctx: dict = {"id": None, "hour": None, "started_at": None}

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
      1. eBay同期     → ランク・メトリクスを最新化（他タスクが参照）
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
    # 朝バッチで本日の重点 3 項目を Opus 4.7 で生成 → DASHBOARD 表示
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
        results['email'] = run_task(
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
    if should_task_run('sales_tracking', config):
        from tasks.task_sales_tracking import run_sales_tracking
        results['sales_tracking'] = run_task(
            '売上トラッキング',
            lambda: run_sales_tracking(config),
            task_key='sales_tracking')

    # ──────────────────────────────────────
    # Step 11: ニュース確認（独立タスク）
    # ──────────────────────────────────────
    if should_task_run('news_check', config):
        from tasks.task_news_check import run_news_check
        results['news'] = run_task(
            'AI/Claudeニュース',
            lambda: run_news_check(config),
            task_key='news_check')

    # ──────────────────────────────────────
    # Step 12: 燃料サーチャージ自動取得（週次、月曜朝のみ）
    # ──────────────────────────────────────
    if should_task_run('fuel_surcharge_check', config):
        from tasks.task_fuel_surcharge_check import run_fuel_surcharge_check
        results['fuel_surcharge_check'] = run_task(
            '燃料サーチャージ取得',
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
    _send_discord_notifications(config, results)

    logger.info("=" * 60)
    logger.info(f"【定時実行完了】 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    return results


def _send_discord_notifications(config, results):
    """全タスク結果をDiscordに通知"""
    webhook_url = config.get('discord', {}).get('webhook_url')
    if not webhook_url:
        return

    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook_url)

        # 1. デイリーレポート（全タスクのステータスサマリー）
        notifier.send_daily_report(results)

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

        # 4. 新規ライバル検出（あれば通知）
        rival = results.get('rival_detection', {})
        if rival and rival.get('new_sellers_count', 0) > 0:
            sellers = rival.get('sellers', [])
            embed = {
                'title': '🔍 新規ライバルセラー検出',
                'color': 16753920,  # Orange
                'timestamp': datetime.now().isoformat(),
                'fields': [
                    {
                        'name': '新規検出',
                        'value': f"{rival['new_sellers_count']}件 / {rival.get('total_scanned', 0)}件スキャン",
                        'inline': True
                    },
                ] + [
                    {
                        'name': f"📌 {s.get('seller', '?')}",
                        'value': f"FB: {s.get('feedback_score', 0)} / 競合: {s.get('competing_count', 0)}商品",
                        'inline': True
                    }
                    for s in sellers[:5]
                ]
            }
            notifier.send_message("🔍 新規ライバル検出", embed)

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

    # ── W13 X ベース AI ニュース取得 (独立 CronJob) ──
    # code-reviewer H-4 対応: 既存 execution_times とは別に固有の時刻で発火.
    # schedule_config.json で時刻・有効フラグ調整可能.
    x_news_cfg = (config.get('tasks_enabled', {}).get('x_news_check') or {})
    if x_news_cfg.get('enabled', True):
        x_hour = int(x_news_cfg.get('cron_hour', 6))
        x_minute = int(x_news_cfg.get('cron_minute', 0))
        scheduler.add_job(
            _run_x_news_only,
            trigger=CronTrigger(hour=x_hour, minute=x_minute, second=0),
            args=[config, x_hour],
            id=f'x_news_check_{x_hour:02d}_{x_minute:02d}',
            name=f'W13 X AI ニュース ({x_hour:02d}:{x_minute:02d})',
            replace_existing=True,
        )
        logger.info(
            f"W13 X ニュース発火: 毎日 {x_hour:02d}:{x_minute:02d}"
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
    # 30 分ごとに GetOrders で新規注文を polling し、以下 2 種を検知:
    #   - DDP-B (US_only) 発送 invoice アラート
    #   - $1500+ DE/IT/FR/KZ 高額 EU 注文
    scheduler.add_job(
        _run_order_alert_check,
        trigger=CronTrigger(minute='*/30', second=0),
        args=[config],
        id='order_alert_check',
        name='W7-A 注文アラート (30分ごと)',
        replace_existing=True,
    )
    logger.info("W7-A 注文アラート: 30分ごと")

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
    # 毎朝 07:00 JST に Opus 4.7 が 5 階層構造で 3 件発掘.
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

    return scheduler


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
    """W122 朝の新商品発掘 — 1 日 1 回 Opus 4.7 で 3 件発掘 (07:00 JST)."""
    try:
        from tasks.task_morning_discovery import run_morning_discovery
    except ImportError as e:
        logger.error(f"task_morning_discovery import 失敗: {e}")
        return
    _run_isolated_task('morning_discovery', 'W122 朝の新商品発掘',
                       lambda: run_morning_discovery(config),
                       scheduled_hour=scheduled_hour)


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
        本実装は entry で _batch_ctx を save、exit (try/finally) で restore する.
        これにより並行 daily batch の context が isolated task の存在に影響されない.
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


def _run_x_news_only(config: dict, scheduled_hour: int = 6):
    """W13 専用の独立実行 (他タスクと併走しない)."""
    logger.info("============================================================")
    logger.info("【W13 X ニュース 単独実行】")
    from tasks.task_x_news_check import run_x_news_check
    _run_isolated_task('x_news_check', 'W13 X AI ニュース',
                       lambda: run_x_news_check(config), scheduled_hour=scheduled_hour)


def _run_customs_check_only(config: dict, scheduled_hour: int = 6):
    """W14 専用の独立実行 (他タスクと併走しない、06:10 オフセット)."""
    logger.info("============================================================")
    logger.info("【W14 通関対応 単独実行】")
    from tasks.task_customs_check import run_customs_check
    _run_isolated_task('customs_check', 'W14 通関対応',
                       lambda: run_customs_check(config), scheduled_hour=scheduled_hour)


def _run_health_check(config: dict, scheduled_hour: int = 4):
    """健康チェック cron — 本日 expected vs executed を照合し、欠落があれば Discord 即時通知.

    各 batch 終了後 (例: 02:30 batch は 03:25 頃完了 → 04:00 起動) に走らせる.
    """
    try:
        from tasks.task_scheduler_health_check import run_scheduler_health_check
    except Exception as e:  # noqa: BLE001
        logger.error(f"health_check import 失敗: {e}")
        return
    _run_isolated_task('scheduler_health_check', '定時実行ヘルスチェック',
                       lambda: run_scheduler_health_check(config),
                       scheduled_hour=scheduled_hour)


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
