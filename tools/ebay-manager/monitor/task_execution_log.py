"""task_execution_log helpers — 定時実行タスクの実行/skip/失敗を DB に記録.

2026-04-25 W?? (definition_of_done 系): daily_relist が 5 日間 hour ドリフトで
サイレントスキップされていた事故を受けて新設. 全タスクの状態を 1 テーブルに集約し、
- MonoDeck の「定時実行」タブで可視化
- 健康チェック cron で「期待されたが実行されていない」タスクを検知 → Discord 即時アラート

スキーマ: monitor/database.py の v20 マイグレーション参照.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 全定時実行タスクのレジストリ.
# (task_key, display_name, expected_hours, expected_weekdays, batch_owner)
#   expected_hours: そのタスクが本来実行されるべき時刻 (None = 全 batch hour で実行).
#   expected_weekdays: None = 毎日, [0] = 月曜のみ など.
#   batch_owner: 'main' (execute_daily_tasks) / 'news' / 'customs'
# ──────────────────────────────────────────────────────────────────────
TASK_SCHEDULE: list[dict[str, Any]] = [
    # main batch (02,11,15,18,22 の execution_schedule.times)
    # company_secretary は is_morning (= min(times) = 02) 限定で execute_daily_tasks 先頭に走る.
    {"key": "company_secretary", "display": "秘書ルーティン (朝: email+TODO繰越+research)", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "ebay_sync", "display": "eBay連携同期", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "ensure_monitor_coverage", "display": "監視台帳カバレッジ自動補完 (W139)", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "inventory_check", "display": "在庫チェック", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "inventory_alert", "display": "在庫切れ通知", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "supplier_select", "display": "仕入先候補選出", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "supplier_sweep", "display": "仕入先候補スイープ", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "enrich_listings_physical", "display": "listings物理データenrichment", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "estimate_weights_claude", "display": "Claude weight推定", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "daily_relist", "display": "End→Relist SEOブースト", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "cleanup_old_relisted", "display": "退役listing 90日経過cleanup", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "research_morning_brief", "display": "Research 脳 morning brief (W24)", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "video_learning_queue", "display": "動画学習キュー", "hours": [2, 18], "weekdays": None, "owner": "main"},
    {"key": "email_pickup", "display": "メール取得", "hours": [11, 15, 18, 22], "weekdays": None, "owner": "main"},
    # W21 (2026-04-26) で 'research' (新商品リサーチ) は廃止. 2026-05-25 に
    # TASK_SCHEDULE からも削除 (毎 main slot で health_alert false-positive を出し
    # ていた、silent-skip 検知能力を低下させていた回復). daily_scheduler dispatcher
    # からも既に削除済 (grep 'research' で hit なし).
    {"key": "rival_detection", "display": "W153 商品別ライバル検出", "hours": [2], "weekdays": None, "owner": "main"},
    {"key": "rival_pricing_refresh", "display": "W183 ライバル価格 refresh & 値下げ", "hours": [0, 6, 12, 18], "weekdays": None, "owner": "rival_pricing"},
    {"key": "morning_discovery", "display": "W122 朝の新商品発掘 (Opus)", "hours": [7], "weekdays": None, "owner": "morning_discovery"},
    {"key": "data_sync", "display": "データストア統合", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "price_optimization", "display": "価格最適化", "hours": None, "weekdays": None, "owner": "main"},
    {"key": "fuel_surcharge_check", "display": "燃料サーチャージ更新リマインダー", "hours": [2], "weekdays": [0], "owner": "main"},
    # 独立 cron
    # W322 (2026-07-05): 前倒し 06:00→05:45 (news) / 06:10→05:50 (customs)。
    # 朝の部 06:00 開始前に用意する目的 (設計書 §6 変更提案#2/#3)。cron 時刻は
    # config/schedule_config.json の cron_hour/cron_minute が真の source of truth
    # だが、TASK_SCHEDULE.hours は cron 側改定に手動追随する必要がある
    # (cascade-update.md, tests/test_w322_evening_refresh.py に乖離検知テストあり)。
    {"key": "news_check", "display": "W154 AI ニュース取得", "hours": [5], "weekdays": None, "owner": "news"},
    {"key": "customs_check", "display": "W14 通関対応", "hours": [5], "weekdays": None, "owner": "customs"},
    {"key": "budget_alert", "display": "予算アラート", "hours": [6, 12, 19], "weekdays": None, "owner": "budget"},
    {"key": "video_learning_resume", "display": "動画学習 quota reset 後再開 (16:30)", "hours": [16], "weekdays": None, "owner": "video_resume"},
    {"key": "scheduler_health_check", "display": "定時実行ヘルスチェック", "hours": [4, 12, 16, 19, 23], "weekdays": None, "owner": "health"},
    {"key": "market_analysis_refresh", "display": "W7-A 市場戦略 refresh", "hours": [2], "weekdays": [6], "owner": "market_analysis"},
    {"key": "daily_codex_lint", "display": "W125 Codex 文書 lint (毎日 03:00)", "hours": [3], "weekdays": None, "owner": "codex_lint"},
    {"key": "research_harvest", "display": "W229 商品リサーチ発掘 (毎日 03:30)", "hours": [3], "weekdays": None, "owner": "research"},
    {"key": "research_sourcing", "display": "W228 リサーチ探索 (毎日 04:30)", "hours": [4], "weekdays": None, "owner": "research"},
    {"key": "rival_seller_sweep", "display": "W#3 ライバルセラー新規出品モニター", "hours": [2], "weekdays": None, "owner": "main"},
    # Codex Round 1 fix MEDIUM-4 (2026-05-16): kind=interval で main batch slot 期待から除外.
    # hours=None は本来「全 batch slot で実行」を意味するが、本 task は 30 分毎 cron なので
    # expected slot 模型と齟齬. kind=interval マーカーで get_today_expected_tasks 側で skip.
    {"key": "claude_loop_healthcheck", "display": "W131 P5 claude-loop watcher (30分ごと)", "hours": None, "weekdays": None, "owner": "claude_loop_healthcheck", "kind": "interval", "interval_minutes": 30},
    # W244 (2026-06-10): order_alert_check は 2026-04-27 から 30 分毎 cron で稼働し
    # task_execution_log に記録されていたのに本レジストリに未登録だった
    # (MonoDeck 定時実行タブ・日次レポートの表示名解決から漏れる)。
    {"key": "order_alert_check", "display": "注文アラート+売却同期 (5分ごと)", "hours": None, "weekdays": None, "owner": "order_alert", "kind": "interval", "interval_minutes": 5},
    # W148 (2026-05-21): キーワード新着監視. 2h ごと :20 分 subprocess crawl.
    {"key": "keyword_watch_crawl", "display": "W148 キーワード新着監視 (2h ごと :20)", "hours": None, "weekdays": None, "owner": "keyword_watch", "kind": "interval", "interval_minutes": 120},
    # W283 Phase 9 (2026-06-19): 送料 rate table 月次自動更新. 毎月1日 03:00 のみ.
    # kind=monthly で月初以外は missed 判定から除外 (毎日 false-positive 回避)。
    {"key": "rate_table_monthly_update", "display": "月次送料 rate table 自動更新 (DDP差額式)", "hours": [3], "weekdays": None, "owner": "rate_table_batch", "kind": "monthly", "month_day": 1},
    # W284 Phase 2 (2026-06-20): eBaymag 反映キュー消化 (1日3回: 11:30/15:30/22:30 JST)
    # CDP+eBaymagログイン不在時は skip+通知。独立 cron (interval に近い性質のため
    # hours に列挙しているが missed 判定で false-positive を出さないよう owner を専用に)。
    {"key": "ebaymag_apply_queue", "display": "W284 eBaymag 反映キュー消化 (1日3回)", "hours": [11, 15, 22], "weekdays": None, "owner": "ebaymag_apply"},
    # W284 Phase 2 (2026-06-20): eBaymag 更新同期 監査 (日次 02:45 JST)
    {"key": "ebaymag_sync_audit", "display": "W284 eBaymag 更新同期 監査 (日次)", "hours": [2], "weekdays": None, "owner": "ebaymag_apply"},
    # W284 Phase 3 (2026-06-20): eBaymag-aware relist 窓ゼロ (feature flag OFF 既定)
    # 登録はされるが feature flag OFF = run 内即 skip なので missed 判定は CDP 在席依存と同じ owner に
    {"key": "ebaymag_relist", "display": "W284 eBaymag relist 窓ゼロ (flag OFF 既定)", "hours": [11, 15, 22], "weekdays": None, "owner": "ebaymag_apply"},
    # W286 (2026-06-28): リサーチ対戦アリーナ. 毎日 05:00 JST. CDP 必須 (不在 skip も痕跡あり).
    {"key": "research_duel", "display": "W286 リサーチ対戦アリーナ (毎日 05:00)", "hours": [5], "weekdays": None, "owner": "research"},
    # W293 (2026-06-29): eBaymag セッション維持 heartbeat. 15 分ごと interval.
    # kind=interval で get_today_expected_tasks の missed 判定から除外 (毎時 false-positive 回避)。
    {"key": "ebaymag_session_heartbeat", "display": "W293 eBaymag セッション維持", "hours": None, "weekdays": None, "owner": "ebaymag_heartbeat", "kind": "interval", "interval_minutes": 15},
    # W301 AI 店長 Phase1 S4 (2026-07-02): 競合分類 (Shadow 固定, pricing_eligible 不変).
    {"key": "rival_classify", "display": "W301 AI店長 競合分類 (毎日 03:00, Shadow)", "hours": [3], "weekdays": None, "owner": "rival_classify"},
    # W301 AI 店長 Phase1 S4 (2026-07-02): 競合 GetItem 定点観測 (蓄積のみ、消費は Phase2).
    {"key": "competitor_snapshot", "display": "W301 AI店長 競合定点観測 (毎日 05:30)", "hours": [5], "weekdays": None, "owner": "competitor_snapshot"},
    # #44 Wave2 (2026-07-04): US 本体 DB↔eBay 整合性 日次突合 (title/condition/
    # ItemSpecifics禁止Name/ConditionDescription/画像). 毎日 02:15 JST.
    {"key": "listing_content_audit", "display": "#44 出品内容監査 (DB↔eBay突合, 毎日 02:15)", "hours": [2], "weekdays": None, "owner": "listing_content_audit"},
    # #45 (2026-07-04): 仕入先候補 (pending/accepted) の availability 定期再チェック.
    # 毎日 02:50 JST (主 batch 02:30 後、daily_codex_lint/rival_classify 03:00 前).
    {"key": "supplier_availability_recheck", "display": "#45 仕入先候補 availability 再チェック (毎日 02:50)", "hours": [2], "weekdays": None, "owner": "supplier_availability_recheck"},
    # W322 (2026-07-05): 夕方 refresh. competitor_snapshot/rival_classify 2 回目実行
    # + AI店長「今夜の価格対応候補」digest. 毎日 19:30 JST.
    {"key": "evening_refresh", "display": "W322 夕方 refresh (競合再観測+分類+digest, 毎日 19:30)", "hours": [19], "weekdays": None, "owner": "evening_refresh"},
]

TASK_SCHEDULE_BY_KEY: dict[str, dict[str, Any]] = {t["key"]: t for t in TASK_SCHEDULE}


def make_batch_id(start_dt: Optional[datetime] = None) -> str:
    """batch ID を生成. 同一 batch 内の全タスクは同じ batch_id を共有."""
    dt = start_dt or datetime.now()
    return dt.strftime("%Y%m%d_%H%M")


def log_task_start(
    task_key: str,
    display_name: str,
    batch_id: str,
    batch_hour: int,
) -> int:
    """タスク開始を記録し、log_id を返す."""
    started_at = datetime.now()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO task_execution_log
              (task_key, display_name, batch_id, batch_hour, status,
               started_at, expected_today)
            VALUES (?, ?, ?, ?, 'started', ?, 1)
            """,
            (task_key, display_name, batch_id, batch_hour, started_at),
        )
        return cur.lastrowid


def log_task_finish(
    log_id: int,
    success: bool,
    message: str = "",
    duration_sec: Optional[float] = None,
) -> None:
    """タスク完了/失敗で log_task_start のレコードを更新."""
    finished_at = datetime.now()
    status = "completed" if success else "failed"
    msg = (message or "")[:1000]
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE task_execution_log
               SET status = ?, success = ?, message = ?,
                   finished_at = ?, duration_sec = ?
             WHERE id = ?
            """,
            (status, 1 if success else 0, msg, finished_at, duration_sec, log_id),
        )


def log_task_skip(
    task_key: str,
    display_name: str,
    batch_id: str,
    batch_hour: int,
    reason: str,
    skip_kind: str = "skip_other",
    expected_today: bool = False,
) -> None:
    """should_task_run が False を返した時の skip 記録.

    skip_kind: 'skip_disabled' | 'skip_time' | 'skip_weekday' | 'skip_other'
    """
    if skip_kind not in ("skip_disabled", "skip_time", "skip_weekday", "skip_other"):
        skip_kind = "skip_other"
    started_at = datetime.now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_execution_log
              (task_key, display_name, batch_id, batch_hour, status,
               started_at, message, expected_today)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_key, display_name, batch_id, batch_hour, skip_kind,
                started_at, (reason or "")[:500],
                1 if expected_today else 0,
            ),
        )


def get_today_executions() -> list[dict]:
    """本日 (00:00 JST 〜 now) の全タスク実行ログを新しい順で返す."""
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, task_key, display_name, batch_id, batch_hour,
                   status, started_at, finished_at, duration_sec,
                   success, message, expected_today
              FROM task_execution_log
             WHERE started_at >= ?
             ORDER BY started_at DESC
            """,
            (today_start,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_executions(days: int = 7, limit: int = 500) -> list[dict]:
    """直近 N 日の実行ログ."""
    since = datetime.now() - timedelta(days=days)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, task_key, display_name, batch_id, batch_hour,
                   status, started_at, finished_at, duration_sec,
                   success, message, expected_today
              FROM task_execution_log
             WHERE started_at >= ?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_task_last_success(task_key: str) -> Optional[datetime]:
    """そのタスクが最後に成功した時刻 (DBに記録されたもの)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT started_at FROM task_execution_log
             WHERE task_key = ? AND status = 'completed' AND success = 1
             ORDER BY started_at DESC LIMIT 1
            """,
            (task_key,),
        ).fetchone()
    if row and row[0]:
        try:
            return datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
    return None


DEFAULT_MAIN_SLOTS = [2, 11, 15, 18, 22]


def _resolve_main_slots(config: Optional[dict] = None) -> list[int]:
    """schedule_config.json から main batch slot を取得. M-3 対応 (ハードコード回避)."""
    if config:
        times = config.get("execution_schedule", {}).get("times")
        if isinstance(times, list) and times:
            try:
                return [int(t) for t in times]
            except (TypeError, ValueError):
                pass
    return list(DEFAULT_MAIN_SLOTS)


def get_today_expected_tasks(
    now: Optional[datetime] = None,
    config: Optional[dict] = None,
) -> list[dict]:
    """本日この時刻までに実行されているはずのタスク一覧を返す.

    判定ルール:
      - hours=None なら毎 batch slot で実行 (常に「今日の slot をすべて」を期待).
      - hours=[H,...] なら H 時の batch slot にぴったり実行されているはず.
      - weekdays が指定されていれば、当該曜日のみ期待.

    結果には「現時刻までに実行が始まっているべき」の slot のみを含む.
    判定は「slot_hour < current_hour」(現時刻が slot 時間を超えている)
    で行う. H-6 対応: minute 比較は health_check が hour 跨ぎで起動する
    前提なので不要 (health_check は 04:00/12:00/16:00/19:00/23:00 起動).
    """
    now = now or datetime.now()
    weekday = now.weekday()
    current_hour = now.hour
    main_slots = _resolve_main_slots(config)
    out: list[dict] = []
    for t in TASK_SCHEDULE:
        if t.get("weekdays") is not None and weekday not in t["weekdays"]:
            continue
        # Codex Round 1 fix MEDIUM-4 (2026-05-16): kind=interval task は cron が
        # batch slot ではなく */30 等で発火するため expected slot 模型から除外.
        # MonoDeck の missed 判定で false positive を出さない.
        if t.get("kind") == "interval":
            continue
        # 月次 task (rate_table_monthly_update): 月初 (month_day) のみ期待。
        # それ以外の日は毎日 missed false-positive を出さないよう除外。
        if t.get("kind") == "monthly" and now.day != t.get("month_day", 1):
            continue
        hours = t.get("hours")
        slots: list[int]
        if hours is None:
            slots = list(main_slots)
        else:
            slots = list(hours)
        for h in slots:
            if h < current_hour:
                out.append({
                    "task_key": t["key"],
                    "display_name": t["display"],
                    "expected_hour": h,
                })
    return out


def find_missed_tasks(
    now: Optional[datetime] = None,
    config: Optional[dict] = None,
) -> list[dict]:
    """本日の expected slots のうち、完了 (success) ログが無いものを返す.

    H-1 対応 (batch_hour ドリフト耐性):
        2026-04-25 から daily_scheduler は batch_ctx['hour'] = scheduled_hour
        で記録するため、execute_daily_tasks(scheduled_hour=2) で起動した
        batch のタスク群はすべて batch_hour=2 で DB に記録される. そのため
        expected_hour と batch_hour の単純一致比較が正しく機能する.
        ただし旧バージョンや独立 cron でドリフトする可能性に備えて、
        「expected_hour 以降 ~ 次 slot 直前」の範囲でも一致を許容する.
    """
    now = now or datetime.now()
    expected = get_today_expected_tasks(now, config=config)
    if not expected:
        return []
    main_slots = sorted(set(_resolve_main_slots(config)))
    # W170 (2026-05-25): started_at は `log_task_start` で `datetime.now()` を bind するため
    # **JST naive 保存** (UTC ではない). `sqlite-timezone.md` の「全 timestamp UTC 保存」
    # 記述は SQL `CURRENT_TIMESTAMP` 系のみで、Python bind 系は例外 (md-files-can-be-wrong R-1).
    # 旧コード `DATE(started_at, '+9 hours')` は JST 値に **追加で +9h shift** = 過剰補正で
    # JST 15:00 以降の completion を翌日扱いにし missed と誤判定. 5/05 修正 (FINDING 5) は
    # データ format の誤認に基づいた逆効果修正だった (実 DB hour 分布 02/11/15/18/22 で確認).
    jst_today = now.strftime('%Y-%m-%d')
    # in-flight stale 抑制: started_at は JST naive 保存 (log_task_start が datetime.now() を
    # bind) なので now から直接差引き。UTC shift 不要 (sqlite-timezone.md 例外カラム参照)。
    # 3h を超える started 行は orphan として in-flight 判定から除外し orphan_task 別経路に委ねる。
    three_hours_ago = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT task_key, batch_hour
              FROM task_execution_log
             WHERE DATE(started_at) = ? AND status = 'completed' AND success = 1
            """,
            (jst_today,),
        ).fetchall()
        # W39 S3 (2026-07-03): 正当スキップ (CDP 不在等で success=False のまま
        # log_task_skip のみ呼ばれるケース) の痕跡行を「充足」扱いにするための取得。
        # log_task_skip は status='skip_disabled'/'skip_time'/'skip_weekday'/'skip_other'
        # のいずれかで INSERT する (本モジュール内 skip_kind 参照)。真に何の痕跡も無い
        # slot (成功も skip も無い) は従来通り missed のまま.
        # HIGH-2 (統合レビュー 2026-07-03): 'skip_time' は「時刻ドリフトで誤スキップ
        # された」signal (2026-04-25 daily_relist 5 日 silent skip / 2026-05-16 thread
        # 跨ぎ clobber 事故の検知経路) につき、充足扱いにすると再発時に欠落検知が
        # 沈黙する → 除外必須。skip_other / skip_disabled / skip_weekday は正当スキップ
        # として充足扱いする。
        skip_rows = conn.execute(
            """
            SELECT task_key, batch_hour
              FROM task_execution_log
             WHERE DATE(started_at) = ?
               AND status LIKE 'skip%' AND status <> 'skip_time'
            """,
            (jst_today,),
        ).fetchall()
        # in-flight バッチ (status='started', finished_at IS NULL, 直近 3h 以内) の
        # batch_hour 集合を取得。supplier_sweep 等の長時間バッチが走行中に health_check が
        # 後続タスク (daily_relist 等) を missed と誤判定 → autofix 二重実行するのを防ぐ。
        # (6/30 シナリオ: 03:10 supplier_sweep 開始 → 04:00 health_check が daily_relist を
        #  missed 誤判定 → 04:01 autofix が daily_relist を先行実行 → 04:16 通常バッチも実行
        #  = 二重実行 14 件の根本原因)
        inflight_rows = conn.execute(
            """
            SELECT DISTINCT batch_hour
              FROM task_execution_log
             WHERE status = 'started' AND finished_at IS NULL
               AND started_at >= ?
            """,
            (three_hours_ago,),
        ).fetchall()
    completed_by_task: dict[str, set[int]] = {}
    for r in rows:
        completed_by_task.setdefault(str(r["task_key"]), set()).add(int(r["batch_hour"]))
    skipped_by_task: dict[str, set[int]] = {}
    for r in skip_rows:
        skipped_by_task.setdefault(str(r["task_key"]), set()).add(int(r["batch_hour"]))
    # in-flight バッチの batch_hour 集合 (slot 窓の突合に使用)
    inflight_batch_hours: set[int] = {int(r["batch_hour"]) for r in inflight_rows}

    def _slot_window(slot: int) -> tuple[int, int]:
        """expected slot の許容窓 [slot, next_slot - 1] を返す.

        例: main_slots=[2,11,15,18,22], slot=2 → window=(2, 10).
        next slot が無い場合 (slot=22) → window=(22, 23).
        独立 cron (slot=6 など) で main_slots に無い場合 → (slot, slot+3) を仮定.
        """
        if slot in main_slots:
            idx = main_slots.index(slot)
            if idx + 1 < len(main_slots):
                return slot, main_slots[idx + 1] - 1
            return slot, 23
        # main_slots 外 (W13 X ニュース 06 / W14 通関 06 など独立 cron). 3 時間窓を許容.
        return slot, min(23, slot + 3)

    missed: list[dict] = []
    for e in expected:
        task_key = e["task_key"]
        eh = int(e["expected_hour"])
        actual = completed_by_task.get(task_key, set())
        lo, hi = _slot_window(eh)
        if any(lo <= bh <= hi for bh in actual):
            continue
        # W39 S3: 正当スキップ痕跡 (log_task_skip 由来の 'skip%' 行) が同一スロット窓
        # 内にあれば充足扱い。CDP 不在等で success=False のまま skip 痕だけを残す
        # タスク (research_harvest/research_duel の CDP 不在経路など) が毎日「欠落」
        # 誤検知されるのを防ぐ。真に痕跡が無い slot は従来通り missed のまま.
        skipped = skipped_by_task.get(task_key, set())
        if any(lo <= bh <= hi for bh in skipped):
            continue
        # in-flight バッチが同一スロット窓内で走行中なら missed にしない。
        # 例: supplier_sweep (batch_hour=2) が 03:10〜04:16 走行中の場合、
        # daily_relist (窓=[2,10]) が未到達でも missed と誤判定しない。
        if any(lo <= bh <= hi for bh in inflight_batch_hours):
            logger.debug(
                "[find_missed_tasks] in-flight batch_hour=%s が窓[%d,%d]内のため "
                "task=%s を missed 抑制",
                sorted(bh for bh in inflight_batch_hours if lo <= bh <= hi),
                lo, hi, task_key,
            )
            continue
        missed.append(e)
    return missed


def is_completed_or_running_today(task_key: str) -> bool:
    """当日 (JST) に task_key が completed(success=1) または in-flight か判定.

    Layer1 二重実行ガード専用: _handle_tier1_rerun が再実行直前に呼ぶ。
    - completed AND success=1: 当日すでに正常完了した
    - started AND finished_at IS NULL: 現在実行中 (in-flight)

    failed / 行なし / 別日 completed は False を返す (正当な再実行は維持)。

    started_at は log_task_start が Python datetime.now() を bind するため
    JST naive 保存。DATE(started_at)=? で JST today を直接比較 (+9h shift 禁止)。
    sqlite-timezone.md 参照。
    """
    today = datetime.now().strftime('%Y-%m-%d')
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM task_execution_log
             WHERE task_key=? AND DATE(started_at)=?
               AND ( (status='completed' AND success=1)
                  OR (status='started' AND finished_at IS NULL) )
             LIMIT 1
            """,
            (task_key, today),
        ).fetchone()
    return row is not None


def is_completed_today(task_key: str) -> bool:
    """当日 (JST) に task_key が completed(success=1) か判定。in-flight は含まない。

    本番未使用 (2026-06-30 時点): run_daily_relist の Layer2 guard は
    relist_history ベースの _has_relisted_today に移行済 (v85, source 列追加で
    ebaymag_relist との source 混在を根治)。本関数は test から参照されるため
    削除せず保持する。

    in-flight (started AND finished_at IS NULL) を含めない理由:
    run_task が log_task_start で 'started' を記録した後に run_daily_relist が
    呼ばれるため、in-flight まで含めると 1 回目の実行が自己 skip してしまう
    (自己検出回避)。

    started_at は JST naive 保存。DATE(started_at)=? で直接比較。
    """
    today = datetime.now().strftime('%Y-%m-%d')
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM task_execution_log
             WHERE task_key=? AND DATE(started_at)=?
               AND status='completed' AND success=1
             LIMIT 1
            """,
            (task_key, today),
        ).fetchone()
    return row is not None


def claim_alert_dedupe(task_key: str, expected_hour: int, alert_date: Optional[str] = None) -> bool:
    """Discord 通知の重複防止 (H-5 対応).

    「(date, task_key, expected_hour) 組で本日初めての通知」の場合のみ True を返し、
    health_alert_log にレコード INSERT する. 既に通知済みの場合は alert_count を
    increment して False を返す.
    """
    date_str = alert_date or datetime.now().strftime("%Y-%m-%d")
    now_dt = datetime.now()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO health_alert_log
              (alert_date, task_key, expected_hour, first_alerted_at, last_alerted_at, alert_count)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (date_str, task_key, int(expected_hour), now_dt, now_dt),
        )
        inserted = cur.rowcount > 0
        if not inserted:
            conn.execute(
                """
                UPDATE health_alert_log
                   SET last_alerted_at = ?, alert_count = alert_count + 1
                 WHERE alert_date = ? AND task_key = ? AND expected_hour = ?
                """,
                (now_dt, date_str, task_key, int(expected_hour)),
            )
        return inserted
