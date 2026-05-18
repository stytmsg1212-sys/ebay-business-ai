"""定時実行ヘルスチェック — 期待されたタスクが実行されていない場合に Discord 即時通知.

2026-04-25 hour ドリフト事故対応で新設.
daily_relist が 5 日間サイレントスキップされていたが、誰も気づかなかった.
これを再発させないため、各 batch 終了後に「期待された slot に成功完了ログがあるか」
を照合し、欠落していれば Discord webhook で即時アラートを送る.

実行タイミング (daily_scheduler.setup_scheduler 参照):
    04:00 / 12:00 / 16:00 / 19:00 / 23:00 — 各 batch 終了後
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _send_discord_alert(webhook_url: str, fresh_missed: list[dict]) -> bool:
    """欠落タスクのうち本日初通知のものを Discord に送る."""
    if not webhook_url or not fresh_missed:
        return False
    try:
        import httpx
    except ImportError as e:
        logger.error(f"httpx import 失敗: {e}")
        return False

    fields = []
    for m in fresh_missed[:20]:  # Discord embed field 25 件制限
        h = m.get("expected_hour")
        slot_str = f"{int(h):02d}:00 batch" if h is not None else "毎batch"
        fields.append({
            "name": f"[警告] {m.get('display_name') or m.get('task_key')}",
            "value": f"key=`{m.get('task_key')}` / slot={slot_str}",
            "inline": False,
        })
    embed = {
        "title": "[緊急] 定時実行 欠落検知",
        "description": (
            f"本日 expected されたタスク {len(fresh_missed)} 件が未完了です. "
            "scheduler.log と MonoDeck「定時実行」タブを確認してください. "
            "(同 task は本日中は再通知しません)"
        ),
        "color": 0xD84C38,  # alert red
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
    }
    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:
        logger.error(f"Discord 通知送信エラー: {e}")
        return False


def _send_coverage_alert(webhook_url: str, coverable: list, dlq: list) -> bool:
    """W139: 監視台帳カバレッジ欠落を Discord に送る.

    coverable>0 = ensure_monitor_coverage が登録できていない盲点
                  (= bug、active 無在庫が在庫監視外で履行不能リスク)。
    dlq>0       = source_url 生成不能 (site_config prefix 未登録)。
                  自動解消不能、site_config 追加 = 人手対応要。
    """
    if not webhook_url or (not coverable and not dlq):
        return False
    try:
        import httpx
    except ImportError as e:  # noqa: BLE001
        logger.error(f"httpx import 失敗: {e}")
        return False
    fields = []
    if coverable:
        fields.append({
            "name": f"[緊急] 監視台帳 未登録 {len(coverable)} 件 (bug)",
            "value": "active 無在庫出品が在庫監視外 = 仕入先OOS検知不能 = "
                     "履行不能リスク。ensure_monitor_coverage を要確認。 "
                     + ", ".join(c["sku"] for c in coverable[:10]),
            "inline": False,
        })
    if dlq:
        fields.append({
            "name": f"[要対応] URL生成不能 (DLQ) {len(dlq)} 件",
            "value": "site_config prefix 未登録で監視台帳に載せられない盲点。"
                     "site_config 追加が必要 (W139)。 "
                     + ", ".join(d["sku"] for d in dlq[:10]),
            "inline": False,
        })
    embed = {
        "title": "[緊急] 監視カバレッジ欠落検知 (W139)",
        "description": ("無在庫出品で仕入先在庫監視の対象外になっているものが "
                        "あります。MonoDeck と scheduler.log を確認してください。"),
        "color": 0xD84C38,
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
    }
    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:  # noqa: BLE001
        logger.error(f"Discord coverage 通知送信エラー: {e}")
        return False


def _send_coverage_error_alert(webhook_url: str, err: str) -> bool:
    """W139 (Codex HIGH 2026-05-18): カバレッジ算出『自体』の失敗を Discord 通知.

    find_coverage_gaps() が壊れると盲点の有無すら判定不能 = 監視の監視が
    沈黙 = 原始事故 (daily_relist 5日 silent skip) と同型。log だけでは
    気付けないため必ず Discord で緊急可視化する (R-11)。
    """
    if not webhook_url:
        return False
    try:
        import httpx
    except ImportError as e:  # noqa: BLE001
        logger.error(f"httpx import 失敗: {e}")
        return False
    embed = {
        "title": "[最緊急] 監視カバレッジ算出が失敗 (W139)",
        "description": (
            "find_coverage_gaps() が例外で失敗しました。無在庫出品の在庫監視"
            "漏れを検知できない状態です (盲点の有無すら不明)。scheduler.log を"
            "確認し ensure_monitor_coverage / 監視台帳を至急点検してください。"
        ),
        "color": 0xD84C38,
        "timestamp": datetime.now().isoformat(),
        "fields": [{"name": "error", "value": str(err)[:900],
                    "inline": False}],
    }
    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:  # noqa: BLE001
        logger.error(f"Discord coverage-error 通知送信エラー: {e}")
        return False


def _check_coverage(config: dict) -> dict:
    """W139: 監視台帳カバレッジ欠落を算出し、必要なら Discord 通知.

    coverable>0 は active な money blind-spot なので **dedupe せず毎回通知**
    (解消されるまで loud であるべき)。dlq>0 は人手対応待ちの残存盲点なので
    日次 dedupe (5x/日 spam 回避、でも毎日 1 回は必ず可視化)。
    例外は握り潰さず fail-safe で通知側に倒す (Q0)。
    """
    try:
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        gaps = find_coverage_gaps()
    except Exception as e:  # noqa: BLE001 — 算出失敗自体を silent にしない
        # Codex HIGH (2026-05-18): 監視カバレッジ算出『自体』が壊れた状態は
        # 最も危険 (盲点の有無すら不明 = active 無在庫の未監視再発を検知不能)。
        # log だけでは原始事故同様に気付けない (R-11) ため **必ず Discord 緊急
        # 通知**する。算出失敗は dedupe しない (解消まで loud であるべき)。
        logger.error(f"coverage gap 算出失敗: {e}", exc_info=True)
        err_sent = False
        try:
            webhook_url = (config.get("discord") or {}).get("webhook_url") or ""
            err_sent = _send_coverage_error_alert(webhook_url, str(e))
        except Exception as _ae:  # noqa: BLE001 — 通知失敗も silent にしない
            logger.error(f"coverage error alert 送信失敗: {_ae}", exc_info=True)
        return {"coverable": -1, "dlq": -1, "dlq_skus": [],
                "coverage_alert_sent": err_sent,
                "coverage_error_alert_sent": err_sent,
                "coverage_error": str(e)}
    coverable, dlq = gaps["coverable"], gaps["dlq"]
    if coverable:
        logger.error(
            f"[W139] 監視台帳 未登録 {len(coverable)} 件 (ensure_monitor_"
            f"coverage 不全 = bug): {[c['sku'] for c in coverable[:10]]}")
    if dlq:
        logger.warning(
            f"[W139] DLQ (URL生成不能/site_config 未登録) {len(dlq)} 件: "
            f"{[d['sku'] for d in dlq]}")
    alert_coverable = list(coverable)  # 非 dedupe (urgent)
    alert_dlq: list = []
    if dlq:
        try:
            from monitor.task_execution_log import claim_alert_dedupe
            fresh = claim_alert_dedupe(task_key="__w139_coverage_dlq__",
                                       expected_hour=0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"coverage dlq dedupe DB error: {e}")
            fresh = True  # fail-safe で通知側
        if fresh:
            alert_dlq = list(dlq)
    sent = False
    if alert_coverable or alert_dlq:
        webhook_url = (config.get("discord") or {}).get("webhook_url") or ""
        sent = _send_coverage_alert(webhook_url, alert_coverable, alert_dlq)
    return {"coverable": len(coverable), "dlq": len(dlq),
            "dlq_skus": [d["sku"] for d in dlq],
            "coverage_alert_sent": sent}


def run_scheduler_health_check(config: dict) -> dict:
    """本日の expected vs executed を照合し、欠落あれば通知 (本日初通知のみ).

    W139: 加えて監視台帳カバレッジ欠落 (active 無在庫の在庫監視漏れ /
    URL生成不能 DLQ) も同時に検知し Discord 通知する (監視の監視)。
    """
    cov = _check_coverage(config)
    try:
        from monitor.task_execution_log import find_missed_tasks, claim_alert_dedupe
    except ImportError as e:
        logger.error(f"task_execution_log import 失敗: {e}")
        return {"success": False, "message": f"import error: {e}",
                "coverage": cov}

    missed = find_missed_tasks(datetime.now(), config=config)
    if not missed:
        logger.info("定時実行ヘルスチェック: 欠落なし")
        return {
            "success": True,
            "missed_count": 0,
            "coverage": cov,
            "message": (
                "all expected tasks completed | "
                f"coverage: unregistered={cov['coverable']} "
                f"dlq={cov['dlq']}"
            ),
        }

    logger.warning(
        f"定時実行ヘルスチェック: 欠落 {len(missed)} 件: "
        f"{[(m['task_key'], m.get('expected_hour')) for m in missed]}"
    )

    # 本日初通知のものだけ抽出 (H-5 対応 dedupe).
    fresh_missed: list[dict] = []
    suppressed = 0
    for m in missed:
        try:
            fresh = claim_alert_dedupe(
                task_key=m["task_key"],
                expected_hour=int(m["expected_hour"]),
            )
        except Exception as e:  # noqa: BLE001 — DB 失敗で全 alert を止めるのは本末転倒
            logger.warning(f"alert dedupe DB error ({m['task_key']}): {e}")
            fresh = True  # フェールセーフで通知側に倒す
        if fresh:
            fresh_missed.append(m)
        else:
            suppressed += 1

    sent = False
    if fresh_missed:
        webhook_url = (config.get("discord") or {}).get("webhook_url") or ""
        sent = _send_discord_alert(webhook_url, fresh_missed)
    return {
        "success": True,
        "missed_count": len(missed),
        "fresh_count": len(fresh_missed),
        "suppressed_count": suppressed,
        "missed": missed,
        "discord_sent": sent,
        "coverage": cov,
        "message": (
            f"missed {len(missed)} tasks "
            f"(fresh={len(fresh_missed)}, suppressed={suppressed}), "
            f"discord_sent={sent} | "
            f"coverage: unregistered={cov['coverable']} dlq={cov['dlq']} "
            f"cov_alert={cov['coverage_alert_sent']}"
        ),
    }
