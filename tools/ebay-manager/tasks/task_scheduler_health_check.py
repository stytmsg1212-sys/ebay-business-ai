"""定時実行ヘルスチェック — 期待されたタスクが実行されていない場合に Discord 即時通知.

2026-04-25 hour ドリフト事故対応で新設.
daily_relist が 5 日間サイレントスキップされていたが、誰も気づかなかった.
これを再発させないため、各 batch 終了後に「期待された slot に成功完了ログがあるか」
を照合し、欠落していれば Discord webhook で即時アラートを送る.

2026-05-25 Phase C 拡張: 朝の総点検で発覚した 4 盲点を追加検出
  (1) intermittent failure: 24h で同 task が 3+ 回 failed (silent flakiness)
  (2) orphan started: started のまま finished_at NULL かつ 2h+ 経過
  (3) DB lock spike: scheduler.log で直近 1h に "database is locked" 3+ 回
  (4) subprocess error: success=0 かつ message に returncode 痕跡 (codex_lint 型再発)

実行タイミング (daily_scheduler.setup_scheduler 参照):
    04:00 / 12:00 / 16:00 / 19:00 / 23:00 — 各 batch 終了後
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_webhook_url(config: dict) -> str:
    """Discord webhook URL を取得 (config 優先、空なら .env DISCORD_WEBHOOK_URL fallback).

    2026-05-25 真因 (commit 8473103): schedule_config.json から bare URL を撤去し
    .env DISCORD_WEBHOOK_URL に移行したが、本ファイル内 3 系統の Discord 通知関数は
    config 経由のみ参照していたため、URL が空文字列のまま silent skip していた.
    `notifiers/discord_notifier.py` は dotenv 経由で読むが、本 module は独自に
    httpx.post を呼ぶため別途 fallback が必要.
    """
    url = (config.get("discord") or {}).get("webhook_url") or ""
    if url:
        return url
    # config 空 → .env 再読込で os.environ に反映 (idempotent)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass  # dotenv 未インストール時は parent process の env を期待
    return os.environ.get("DISCORD_WEBHOOK_URL", "")


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
            webhook_url = _resolve_webhook_url(config)
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
        webhook_url = _resolve_webhook_url(config)
        sent = _send_coverage_alert(webhook_url, alert_coverable, alert_dlq)
    return {"coverable": len(coverable), "dlq": len(dlq),
            "dlq_skus": [d["sku"] for d in dlq],
            "coverage_alert_sent": sent}


def _check_phase_c_health(config: dict) -> dict:
    """W164 Phase C: 朝の 8 盲点中 4 検査を統合実行.

    各検査は失敗しても他に影響しない (try/except 個別)。閾値は固定 (24h/3件/2h/1h/3件).
    timezone: task_execution_log.started_at は `log_task_start` の `datetime.now()` で
    JST naive 保存 (`.claude/rules/sqlite-timezone.md` の「全 UTC 保存」記述は本 table
    に当てはまらない、md-files-can-be-wrong R-1)。SQL の `datetime('now')` は UTC のため
    9h ずれる。Python 側で cutoff 計算して bind する.
    """
    findings: dict = {"intermittent": [], "orphans": [], "db_locks": 0, "subprocess_errors": []}
    now_jst = datetime.now()
    cutoff_24h = (now_jst - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_2h = (now_jst - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from monitor.database import get_conn
        with get_conn() as conn:
            # (1) 24h で同 task_key が 3+ 回 failed (silent flakiness)
            rows = conn.execute(
                "SELECT task_key, COUNT(*) AS n, MAX(started_at) AS last_at "
                "FROM task_execution_log "
                "WHERE status='failed' AND started_at >= ? "
                "GROUP BY task_key HAVING n >= 3 ORDER BY n DESC",
                (cutoff_24h,),
            ).fetchall()
            findings["intermittent"] = [
                {"task_key": r[0], "count": r[1], "last_at": r[2]} for r in rows
            ]
            # (2) started のまま finished_at NULL かつ 2h+ 経過 (orphan)
            rows = conn.execute(
                "SELECT task_key, batch_id, started_at FROM task_execution_log "
                "WHERE status='started' AND finished_at IS NULL "
                "AND started_at < ? "
                "ORDER BY started_at DESC LIMIT 20",
                (cutoff_2h,),
            ).fetchall()
            findings["orphans"] = [
                {"task_key": r[0], "batch_id": r[1], "started_at": r[2]} for r in rows
            ]
            # (4) subprocess returncode 非 0 痕跡 (codex_lint 型再発、過去 24h)
            rows = conn.execute(
                "SELECT task_key, started_at, message FROM task_execution_log "
                "WHERE success=0 AND message LIKE '%returncode%' "
                "AND started_at >= ? "
                "ORDER BY started_at DESC LIMIT 10",
                (cutoff_24h,),
            ).fetchall()
            findings["subprocess_errors"] = [
                {"task_key": r[0], "started_at": r[1], "message": (r[2] or "")[:200]}
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001 — 検査自体の失敗を silent にしない
        logger.error(f"phase_c DB checks 失敗: {e}", exc_info=True)
        findings["db_query_error"] = str(e)

    # (3) scheduler.log で直近 1h の "database is locked" 出現回数
    try:
        log_path = Path(__file__).resolve().parent.parent / "logs" / "scheduler.log"
        if log_path.exists():
            cutoff = datetime.now() - timedelta(hours=1)
            count = 0
            # 末尾 256KB だけ読む (1h 分は十分収まる、低 I/O)
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                tail = f.read().decode("utf-8", errors="replace")
            ts_pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
            for line in tail.splitlines():
                if "database is locked" not in line:
                    continue
                m = ts_pat.match(line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if ts >= cutoff:
                    count += 1
            findings["db_locks"] = count
    except Exception as e:  # noqa: BLE001 — log 読取失敗も silent にしない
        logger.warning(f"phase_c scheduler.log scan 失敗: {e}")
        findings["log_scan_error"] = str(e)

    return findings


def _send_phase_c_alert(webhook_url: str, findings: dict) -> bool:
    """Phase C 4 検査の異常を 1 embed で送る (検出 0 件なら送らない).

    検査『自体』の失敗 (db_query_error / log_scan_error) も Discord で必ず可視化
    する (Codex HIGH 2026-05-18 同型: 監視の監視が沈黙したら最緊急に格上げ、R-11).
    """
    has_self_error = bool(findings.get("db_query_error") or findings.get("log_scan_error"))
    has_alert = bool(
        findings.get("intermittent") or findings.get("orphans")
        or findings.get("subprocess_errors") or (findings.get("db_locks") or 0) >= 3
    ) or has_self_error
    if not webhook_url or not has_alert:
        return False
    try:
        import httpx
    except ImportError as e:  # noqa: BLE001
        logger.error(f"httpx import 失敗: {e}")
        return False
    fields = []
    if findings.get("intermittent"):
        v = "\n".join(f"`{x['task_key']}` failed {x['count']}回 (last {x['last_at']})"
                      for x in findings["intermittent"][:5])
        fields.append({"name": "[要対応] 慢性失敗 (24h で 3+回 failed)", "value": v, "inline": False})
    if findings.get("orphans"):
        v = "\n".join(f"`{x['task_key']}` batch={x['batch_id']} started={x['started_at']}"
                      for x in findings["orphans"][:5])
        fields.append({"name": "[要対応] started 残骸 (2h+ 未完了)", "value": v, "inline": False})
    if (findings.get("db_locks") or 0) >= 3:
        fields.append({
            "name": f"[警告] DB lock spike 1h で {findings['db_locks']}回",
            "value": "scheduler.log で 'database is locked' 多発。並行 write 競合を疑う",
            "inline": False,
        })
    if findings.get("subprocess_errors"):
        v = "\n".join(f"`{x['task_key']}` {x['started_at']}\n  {x['message'][:80]}"
                      for x in findings["subprocess_errors"][:5])
        fields.append({"name": "[警告] subprocess 失敗 (codex_lint 型再発候補)",
                       "value": v, "inline": False})
    if findings.get("db_query_error"):
        fields.append({
            "name": "[最緊急] Phase C DB query 自体が失敗 (3 検査が沈黙)",
            "value": f"intermittent / orphans / subprocess 検知が一時停止中。"
                     f"error={str(findings['db_query_error'])[:800]}",
            "inline": False,
        })
    if findings.get("log_scan_error"):
        fields.append({
            "name": "[警告] Phase C scheduler.log scan 失敗 (DB lock 検知沈黙)",
            "value": f"error={str(findings['log_scan_error'])[:800]}",
            "inline": False,
        })
    embed = {
        "title": "[Phase C] scheduler 健康診断 異常検出",
        "description": ("既存の missed-task 検知では捕まらない 4 盲点 (慢性失敗 / orphan / "
                        "DB lock / subprocess) を検出しました。scheduler.log と MonoDeck で精査."),
        # 自己エラー時は赤色 (監視の監視沈黙 = 最緊急)、他は橙色 (注意要)
        "color": 0xD84C38 if has_self_error else 0xE69138,
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
    }
    try:
        r = httpx.post(webhook_url, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, OSError) as e:  # noqa: BLE001
        logger.error(f"Phase C Discord 通知エラー: {e}")
        return False


def run_scheduler_health_check(config: dict) -> dict:
    """本日の expected vs executed を照合し、欠落あれば通知 (本日初通知のみ).

    W139: 加えて監視台帳カバレッジ欠落 (active 無在庫の在庫監視漏れ /
    URL生成不能 DLQ) も同時に検知し Discord 通知する (監視の監視)。
    W164 Phase C (2026-05-25): 朝の 8 盲点中 4 検査を追加 (慢性失敗 / orphan started /
    DB lock spike / subprocess returncode 非 0)。検出時は橙色 embed で別送。
    """
    cov = _check_coverage(config)
    phase_c = _check_phase_c_health(config)
    webhook_url = _resolve_webhook_url(config)
    phase_c_sent = _send_phase_c_alert(webhook_url, phase_c)
    phase_c["alert_sent"] = phase_c_sent
    try:
        from monitor.task_execution_log import find_missed_tasks, claim_alert_dedupe
    except ImportError as e:
        logger.error(f"task_execution_log import 失敗: {e}")
        return {"success": False, "message": f"import error: {e}",
                "coverage": cov, "phase_c": phase_c}

    missed = find_missed_tasks(datetime.now(), config=config)
    if not missed:
        logger.info("定時実行ヘルスチェック: 欠落なし")
        return {
            "success": True,
            "missed_count": 0,
            "coverage": cov,
            "phase_c": phase_c,
            "message": (
                "all expected tasks completed | "
                f"coverage: unregistered={cov['coverable']} "
                f"dlq={cov['dlq']} | "
                f"phase_c: intermittent={len(phase_c['intermittent'])} "
                f"orphans={len(phase_c['orphans'])} "
                f"locks_1h={phase_c['db_locks']} "
                f"subproc_err={len(phase_c['subprocess_errors'])}"
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
        sent = _send_discord_alert(webhook_url, fresh_missed)
    return {
        "success": True,
        "missed_count": len(missed),
        "fresh_count": len(fresh_missed),
        "suppressed_count": suppressed,
        "missed": missed,
        "discord_sent": sent,
        "coverage": cov,
        "phase_c": phase_c,
        "message": (
            f"missed {len(missed)} tasks "
            f"(fresh={len(fresh_missed)}, suppressed={suppressed}), "
            f"discord_sent={sent} | "
            f"coverage: unregistered={cov['coverable']} dlq={cov['dlq']} "
            f"cov_alert={cov['coverage_alert_sent']} | "
            f"phase_c: intermittent={len(phase_c['intermittent'])} "
            f"orphans={len(phase_c['orphans'])} "
            f"locks_1h={phase_c['db_locks']} "
            f"subproc_err={len(phase_c['subprocess_errors'])}"
        ),
    }
