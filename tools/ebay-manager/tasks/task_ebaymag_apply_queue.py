# -*- coding: utf-8 -*-
"""W284 Phase 2: eBaymag 各国版 反映キュー自動消化タスク.

ebaymag_apply_queue の active job を CDP+eBaymagログイン生存中に消化し、
希望出品国(ebay_listings.ebaymag_desired_sites_json)を eBaymag に反映する。

CDP/eBaymagログイン不在時: 未処理件数を Discord 通知して skip (Q0: silent にしない)。
全失敗経路: status/attempts/last_error/updated_at を必ず更新 (Q0: 偽装成功禁止)。
識別キー: ebay_item_id (SKU 禁止、sku-rules.md)。
timezone: next_attempt_at は UTC (datetime('now') 系、sqlite-timezone.md)。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# awaiting_import backoff テーブル (attempts → 次回待ち時間)
_BACKOFF_HOURS: list[int] = [1, 6, 24, 24, 24]  # attempts 0,1,2,3,4 → 5回目で needs_manual
_AWAITING_IMPORT_MAX_ATTEMPTS = len(_BACKOFF_HOURS)
# failed の上限 (H1 code-reviewer 2026-06-20): failed も backoff 付き再試行し、上限到達で
# needs_manual。これが無いと恒久失敗 job が毎 run 重い CDP 操作を永久消費する。
_FAILED_MAX_ATTEMPTS = 5

# CDP 不在時の Discord 通知 throttle (H2 code-reviewer 2026-06-20): UTC 日付 1 日 1 回 +
# 未処理件数が前回通知時より増えたら同日でも再通知 (件数感応=滞留悪化の通知欠落を防ぐ)。
# モジュールレベル変数 (app_kv 非依存、再起動でリセット=過剰通知方向で安全)。
_cdp_absent_notify_state: dict = {"date": None, "n_pending": -1}

# キュー消化 1 回の最大処理件数 (長時間 CDP 占有を避ける)
_MAX_JOBS_PER_RUN = 10

# done purge 間隔 (days)
_DONE_PURGE_DAYS = 30


def _should_send_cdp_absent_notify(config: dict, n_pending: int) -> bool:  # noqa: ARG001
    """CDP 不在通知をこの run で送るべきか (UTC 日付 1 日 1 回 + 滞留増加で再通知)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = _cdp_absent_notify_state
    return st["date"] != today or n_pending > st["n_pending"]


def _record_cdp_absent_notified(config: dict, n_pending: int) -> None:  # noqa: ARG001
    """CDP 不在通知済みを記録 (UTC 日付 + 通知時点の未処理件数)."""
    _cdp_absent_notify_state["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _cdp_absent_notify_state["n_pending"] = n_pending


def _probe_cdp_ebaymag() -> tuple[bool, str]:
    """CDP + eBaymagログイン生存を probe する。

    ebaymag_driver の fetch_site_states は product_id が必要なため、
    最軽量な方法として _get_ebaymag_page を直接呼ぶ subprocess を避け、
    driver 内部の CDP 接続チェックを再利用する。

    Returns:
        (alive: bool, error_msg: str)  alive=True ならキュー消化を続行。
    """
    try:
        from playwright.sync_api import sync_playwright
        from monitor.ebaymag_driver import _get_ebaymag_page, EbaymagResult, _should_isolate
    except ImportError as e:
        return False, f"playwright/driver import 失敗: {e}"

    if _should_isolate():
        # Streamlit (Windows) 配下 — subprocess で probe
        import subprocess, sys, os
        from pathlib import Path
        script = (
            "import json, sys; "
            "from monitor.ebaymag_driver import _get_ebaymag_page, EbaymagResult; "
            "from playwright.sync_api import sync_playwright; "
            "res = EbaymagResult(); "
            "with sync_playwright() as p: pg = _get_ebaymag_page(p, res); "
            "print(json.dumps({'ok': pg is not None, 'error': res.error}))"
        )
        proj = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["EBAYMAG_DRIVER_SUBPROCESS"] = "0"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, encoding="utf-8",
                timeout=30, env=env, cwd=proj,
            )
            out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
            alive = bool(out.get("ok"))
            err = out.get("error") or (proc.stderr or "").strip()[-200:]
            return alive, err or ""
        except Exception as e:  # noqa: BLE001
            return False, f"CDP probe subprocess 失敗: {e}"
    else:
        res = EbaymagResult()
        try:
            with sync_playwright() as p:
                pg = _get_ebaymag_page(p, res)
                alive = pg is not None
        except Exception as e:  # noqa: BLE001
            alive = False
            res.error = str(e)
        return alive, res.error or ""


def _calc_next_attempt_at(attempts: int) -> str:
    """awaiting_import の backoff 時刻を UTC ISO 文字列で返す。"""
    hours = _BACKOFF_HOURS[min(attempts, len(_BACKOFF_HOURS) - 1)]
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _discord_notify(config: dict, message: str) -> None:
    """Discord 既定 ch に通知 (送信失敗は warn のみ、Q0 silent skip 禁止)。"""
    try:
        from notifiers.discord_notifier import notifier_for
        notifier = notifier_for("default")
        notifier.send_message(message)
    except Exception as e:  # noqa: BLE001
        logger.warning("Discord 通知失敗 (本処理に影響なし): %s", e)


def _process_job(job: dict, config: dict) -> dict:
    """1 件の apply_queue job を消化する。

    Returns:
        {"result": "applied" | "awaiting_import" | "failed" | "no_change",
         "error": str | None}
    """
    from monitor.database import (
        get_ebaymag_desired,
        get_ebaymag_product,
        upsert_ebaymag_product,
        mark_ebaymag_apply_status,
    )
    from monitor.ebaymag_driver import (
        discover_product_id,
        fetch_site_states,
        apply_site_changes,
        SITE_MAP,
    )

    job_id: int = job["id"]
    eid: str = job["ebay_item_id"]
    attempts: int = job.get("attempts", 0)

    def _fail(error_msg: str, *, needs_manual: bool = False) -> dict:
        # H1 (code-reviewer 2026-06-20): failed も backoff + 上限を設け、毎 run 無限
        # リトライ (重い CDP 操作の永久消費 + 正常 job の消化枠 food) を防ぐ。
        if not needs_manual and attempts + 1 >= _FAILED_MAX_ATTEMPTS:
            needs_manual = True
            error_msg = f"{error_msg} (failed {attempts + 1}回到達 → 手動対応へ)"
        status = "needs_manual" if needs_manual else "failed"
        next_at = None if needs_manual else _calc_next_attempt_at(attempts)
        mark_ebaymag_apply_status(
            job_id, status,
            last_error=error_msg,
            increment_attempt=True,
            next_attempt_at=next_at,
        )
        if needs_manual:
            _discord_notify(config, f"[eBaymag] 手動対応が必要です: {eid}\n{error_msg}")
        logger.warning("[ebaymag_apply_queue] job=%d eid=%s %s: %s", job_id, eid, status, error_msg)
        return {"result": "failed", "error": error_msg}

    # Step 1: listing が存在するか + 最新 desired を再読込 (スナップショット複製しない)
    desired_info = get_ebaymag_desired(eid)
    if desired_info is None:
        return _fail(f"listing 消滅または desired 未設定 (ebay_item_id={eid})", needs_manual=True)

    desired_sites: list[str] = desired_info.get("desired_sites") or []

    # Step 2: ebaymag_products から product_id を取得。なければ discover
    mapping = get_ebaymag_product(eid)
    product_id: str | None = mapping.get("product_id") if mapping else None

    if not product_id:
        # listing タイトル / search_keyword を query として discover
        from monitor.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT title, search_keyword FROM ebay_listings WHERE ebay_item_id=?",
                (eid,),
            ).fetchone()
        if row is None:
            return _fail(f"discover 用 listing が見つかりません (ebay_item_id={eid})", needs_manual=True)
        query = (row["search_keyword"] or row["title"] or "").strip()
        if not query:
            return _fail(f"discover 用クエリが空 (title も search_keyword も無し, eid={eid})", needs_manual=True)

        expected_itm = eid  # itm 照合の安全弁
        logger.info("[ebaymag_apply_queue] discover: eid=%s query=%r", eid, query[:60])
        disc_result = discover_product_id(query, expected_itm)

        if disc_result.ok and disc_result.product_id:
            product_id = disc_result.product_id
            # H3 (code-reviewer 2026-06-20): 空 states で実態キャッシュを上書きしない
            # (site_states 省略=NULL/既存維持)。Step3 fetch 成功後に states を確定する。
            upsert_ebaymag_product(eid, product_id=product_id)
            logger.info("[ebaymag_apply_queue] discover OK: eid=%s product_id=%s", eid, product_id)
        else:
            # eBaymag 取込ラグ / 未発見
            error_detail = disc_result.error or "discover: 候補なし"
            if attempts >= _AWAITING_IMPORT_MAX_ATTEMPTS:
                return _fail(
                    f"discover {_AWAITING_IMPORT_MAX_ATTEMPTS}回試行後も未発見。"
                    f"eBaymag 取込を確認してください。query={query!r} error={error_detail}",
                    needs_manual=True,
                )
            next_at = _calc_next_attempt_at(attempts)
            mark_ebaymag_apply_status(
                job_id, "awaiting_import",
                last_error=f"discover 未発見 query={query!r} error={error_detail}",
                increment_attempt=True,
                next_attempt_at=next_at,
            )
            logger.info(
                "[ebaymag_apply_queue] awaiting_import: eid=%s attempts=%d next_at=%s",
                eid, attempts + 1, next_at,
            )
            return {"result": "awaiting_import", "error": error_detail}

    # Step 3: 実態再取得 (誤 OFF 防止 / 設計 M1)
    actual_result = fetch_site_states(product_id, expected_itm=eid)
    if not actual_result.ok:
        return _fail(
            f"実態取得失敗 (product_id={product_id}): {actual_result.error}"
        )
    # states={} は成功扱い禁止 (Q0 / 設計書 §3.2)
    if not actual_result.site_states:
        return _fail(
            f"実態取得: site_states 空 (UI構造変化の可能性, product_id={product_id})"
        )

    actual: dict[str, bool] = actual_result.site_states
    all_sites = set(SITE_MAP.keys())
    desired_set = set(s for s in desired_sites if s in all_sites)
    actual_on = {s for s, v in actual.items() if v}

    turn_on = sorted(desired_set - actual_on)
    turn_off = sorted((actual_on - desired_set) & all_sites)

    if not turn_on and not turn_off:
        # 既に目標状態 — done に更新
        mark_ebaymag_apply_status(job_id, "done")
        logger.info("[ebaymag_apply_queue] no_change (already target): eid=%s", eid)
        return {"result": "no_change", "error": None}

    # Step 4: 差分適用
    logger.info(
        "[ebaymag_apply_queue] apply: eid=%s turn_on=%s turn_off=%s",
        eid, turn_on, turn_off,
    )
    apply_result = apply_site_changes(product_id, eid, turn_on, turn_off)

    if not apply_result.ok:
        return _fail(
            f"apply_site_changes 失敗 (product_id={product_id}): {apply_result.error}"
        )

    # Step 5: 新実態を ebaymag_products に更新 + job を done に
    new_states = apply_result.site_states
    if not new_states:
        return _fail(
            f"apply 後の site_states 空 (定着検証不能, product_id={product_id})"
        )
    upsert_ebaymag_product(eid, product_id=product_id, site_states=new_states)
    mark_ebaymag_apply_status(job_id, "done")
    logger.info("[ebaymag_apply_queue] applied OK: eid=%s states=%s", eid, new_states)
    return {"result": "applied", "error": None}


def run_ebaymag_apply_queue(config: dict) -> dict:
    """W284 Phase 2: eBaymag 反映キュー自動消化。

    CDP+eBaymagログイン不在時: Discord 通知 (throttle 付き) して skip。
    生存時: active job を最大 _MAX_JOBS_PER_RUN 件消化。
    古い done を purge。

    Returns:
        {"success": bool, "processed": int, "applied": int,
         "awaiting_import": int, "failed": int, "message": str}
    """
    from monitor.database import (
        get_active_ebaymag_apply_jobs,
        purge_done_ebaymag_apply,
    )

    logger.info("[ebaymag_apply_queue] run 開始")

    # Step 1: CDP + eBaymagログイン生存 probe
    alive, probe_err = _probe_cdp_ebaymag()

    if not alive:
        # 未処理件数を把握してから Discord 通知 (throttle 付き)
        try:
            pending_jobs = get_active_ebaymag_apply_jobs()
            n_pending = len(pending_jobs)
        except Exception as e:  # noqa: BLE001
            n_pending = -1
            logger.warning("[ebaymag_apply_queue] pending 件数取得失敗: %s", e)

        msg = (
            f"[eBaymag] 反映キュー消化 skip — CDP Chrome または eBaymag ログインが不在。"
            f"未処理 {n_pending} 件。CDP Chrome (localhost:9222) + eBaymag ログインを"
            f"開いてください。(error: {probe_err})"
        )
        logger.info("[ebaymag_apply_queue] %s", msg)

        if n_pending > 0 and _should_send_cdp_absent_notify(config, n_pending):
            _discord_notify(
                config,
                f"[eBaymag] 反映待ち {n_pending}件。CDP Chrome + eBaymag ログインを開いてください。",
            )
            _record_cdp_absent_notified(config, n_pending)

        return {
            "success": True,  # CDP不在は正常 skip (failure ではない)
            "processed": 0,
            "applied": 0,
            "awaiting_import": 0,
            "failed": 0,
            "message": msg,
        }

    # Step 2: active job 消化
    jobs = get_active_ebaymag_apply_jobs(limit=_MAX_JOBS_PER_RUN)
    if not jobs:
        logger.info("[ebaymag_apply_queue] active job なし (skip)")
        # done purge のみ実行
        purge_done_ebaymag_apply(older_than_days=_DONE_PURGE_DAYS)
        return {
            "success": True,
            "processed": 0,
            "applied": 0,
            "awaiting_import": 0,
            "failed": 0,
            "message": "active job なし",
        }

    stats = {"processed": 0, "applied": 0, "awaiting_import": 0, "failed": 0}
    errors: list[str] = []

    for job in jobs:
        stats["processed"] += 1
        eid = job["ebay_item_id"]
        try:
            res = _process_job(job, config)
        except Exception as e:  # noqa: BLE001
            # _process_job 外の予期せぬ例外 — job を failed にして継続 (Q0 silent skip 禁止)
            err_msg = f"{type(e).__name__}: {e}"
            logger.error("[ebaymag_apply_queue] 予期せぬ例外 eid=%s: %s", eid, err_msg, exc_info=True)
            try:
                from monitor.database import mark_ebaymag_apply_status
                mark_ebaymag_apply_status(
                    job["id"], "failed",
                    last_error=f"予期せぬ例外: {err_msg}",
                    increment_attempt=True,
                )
            except Exception as _e:  # noqa: BLE001
                logger.error("[ebaymag_apply_queue] mark_failed 失敗: %s", _e)
            res = {"result": "failed", "error": err_msg}

        result_key = res.get("result", "failed")
        if result_key == "applied":
            stats["applied"] += 1
        elif result_key == "awaiting_import":
            stats["awaiting_import"] += 1
        elif result_key == "failed":
            stats["failed"] += 1
            if res.get("error"):
                errors.append(f"{eid}: {res['error'][:80]}")

    # Step 3: done purge
    try:
        purge_done_ebaymag_apply(older_than_days=_DONE_PURGE_DAYS)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ebaymag_apply_queue] done purge 失敗 (本処理に影響なし): %s", e)

    success = stats["failed"] == 0
    msg = (
        f"processed={stats['processed']} applied={stats['applied']} "
        f"awaiting_import={stats['awaiting_import']} failed={stats['failed']}"
    )
    if errors:
        _discord_notify(
            config,
            f"[eBaymag] 反映失敗 {stats['failed']}件:\n" + "\n".join(errors[:5]),
        )

    logger.info("[ebaymag_apply_queue] 完了: %s", msg)
    return {
        "success": success,
        "processed": stats["processed"],
        "applied": stats["applied"],
        "awaiting_import": stats["awaiting_import"],
        "failed": stats["failed"],
        "message": msg,
    }
