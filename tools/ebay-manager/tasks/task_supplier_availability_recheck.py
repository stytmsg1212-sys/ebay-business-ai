#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード#45 (2026-07-04): 仕入先候補に売り切れ商品が混入する問題の恒久対策.

root cause (トリアージ確定済):
    supplier_candidates.availability_status は発見時 (W182 gate,
    task_supplier_sweep.py / task_supplier_candidate_search.py) に 1 回セットされる
    だけで、その後は再チェックされない。時間経過で仕入先が売り切れても DB の値は
    古いままなので UI (tab_supplier_candidates.py) の除外フィルタ
    (availability_status IN ('unavailable','not_found') 除外) が効かず、
    売り切れ済の候補が actionable tab に混入し続ける。

    過去の手動対策 = scripts/check_supplier_candidates_oos_2026_05_20.py (W148-Y)。
    one-shot script で scheduler 未組込だったため、本 task で定時実行化する。

処理フロー:
    1. status IN ('pending','accepted') かつ
       (availability_checked_at IS NULL OR stale_days 日超) の候補を古い順に取得
    2. 各候補の candidate_url を再チェック
       - Yahoo オークション: fetch_yahoo_end_status で「落札者なし終了」を判別し、
         終了から 24h は unavailable 確定にしない (再出品慣行の猶予、
         feedback_yahoo_auction_end_two_patterns.md 準拠)。
       - それ以外: 既存 W182 gate 関数 monitor.scrapers.check_candidate_availability
         (mercari / paypay / yahoo shopping 等の既存判定ロジック) を流用。
    3. 結論が出た (available / unavailable / not_found) 候補のみ DB 更新。
       判定不能 (unknown / grace 中 / fetch エラー) は **据え置き** (Q0: 不確実な
       状態を確定扱いにしない。次回実行で再チェックされる)。
    4. unavailable / not_found 確定は status='rejected' + auto_rejected=1 で
       却下 (W148-Y 踏襲、user 判断枠の auto_rejected=0 汚さない)。

config (config.tasks_enabled.supplier_availability_recheck):
    stale_days: 再チェック対象にする経過日数 (default 3)
    max_candidates_per_run: 1 回の上限件数 (default 100, 外部サイト負荷抑制)
    sleep_between_checks_sec: 候補間の sleep 秒 (default 1.0)
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import (  # noqa: E402
    get_supplier_candidates_for_availability_recheck,
    update_supplier_candidate_availability,
    reject_supplier_candidate_availability,
    record_supplier_candidate_availability_attempt,
    mark_supplier_candidate_pending_reject,
)
from monitor.scrapers import check_candidate_availability  # noqa: E402
from monitor.yahoo_auction_status import fetch_yahoo_end_status  # noqa: E402

logger = logging.getLogger(__name__)

# 結論が出たとみなす status (この 3 つ以外は判定保留)
_CONCLUSIVE_STATUSES = ("available", "unavailable", "not_found")

# MED-1 (2026-07-04 code-reviewer): サイト構造変化を検知するため、判定不能比率が
# 高い時 (処理数十分 + 8 割超が保留) は warning + summary flag を残す。通知不要。
_UNKNOWN_RATIO_MIN_PROCESSED = 20
_UNKNOWN_RATIO_THRESHOLD = 0.8


def _notify_accepted_candidate_sold_out(
    cand: dict, result: dict,
) -> None:
    """MED-2 (2026-07-04 code-reviewer): user 採用済みの仕入先が売り切れた時のみ通知。

    pending→rejected はノイズ抑止 (依頼ボード#39) のため通知しない。呼出元で
    prior status == 'accepted' を確認してから本関数を呼ぶこと。
    Discord は action_required カテゴリ (config gate 既定 ON、money-direct/user対応系)。
    通知失敗は logger.error で痕跡を残し、task 本体は継続 (Q0)。
    """
    try:
        from notifiers.notification_center import record_and_maybe_send
    except Exception as e:  # noqa: BLE001
        logger.error(f"[#45] record_and_maybe_send import 失敗 (通知 skip): {e}")
        return

    listing_title = (cand.get("listing_title") or cand.get("candidate_title")
                     or "(タイトル不明)")
    url = cand.get("candidate_url") or ""
    signal = result.get("signal") or ""
    status_label = result.get("status") or "unavailable"

    title = "採用済み仕入先が売り切れ"
    body = (
        f"商品: {listing_title}\n"
        f"候補URL: {url}\n"
        f"検知: {status_label} ({signal})\n"
        "再仕入れ先の再探索が必要です。"
    )
    dedupe_key = f"supplier_avail_recheck_accepted_{cand.get('id')}"

    try:
        record_and_maybe_send(
            category="action_required",
            severity="warning",
            title=title,
            body=body,
            link_target="tab_supplier_candidates",
            link_ref=str(cand.get("id")),
            dedupe_key=dedupe_key,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[#45] 通知送信例外 (id={cand.get('id')}): {e}")


def _classify_yahoo_with_grace(url: str, checked_at: str) -> dict:
    """ヤフオク候補の availability 判定 (24h 猶予つき、既存 fetch_yahoo_end_status 流用).

    feedback_yahoo_auction_end_two_patterns.md 準拠:
      - 落札済 (has_winner=True) 終了 → 即 unavailable
      - 落札者なし終了 (has_winner=False) → 終了時刻 + 24h までは判定保留 (grace)
        (24h 以内の再出品慣行を待つ。24h 経過後は同一 URL はもう買えないので unavailable 確定)
    """
    try:
        est = fetch_yahoo_end_status(url)
    except Exception as e:  # noqa: BLE001 — Q0: 予期せぬ例外も判定保留として痕跡を残す
        return {
            "conclusive": False, "status": "unknown",
            "signal": f"fetch_yahoo_end_status 例外: {e}", "checked_at": checked_at,
        }

    if est.raw_error:
        return {
            "conclusive": False, "status": "unknown",
            "signal": est.raw_error, "checked_at": checked_at,
        }

    if not est.is_ended:
        return {
            "conclusive": True, "status": "available",
            "signal": "__NEXT_DATA__ status=open", "checked_at": checked_at,
        }

    if est.has_winner:
        return {
            "conclusive": True, "status": "unavailable",
            "signal": "auction ended (has_winner=True, 落札済)", "checked_at": checked_at,
        }

    # 落札者なし終了: 24h 猶予
    if est.end_time_utc is not None:
        grace_until = est.end_time_utc + timedelta(hours=24)
        if datetime.now(timezone.utc) < grace_until:
            return {
                "conclusive": False, "status": "grace",
                "signal": f"auction ended no winner, grace until {grace_until.isoformat()}",
                "checked_at": checked_at,
            }
    # grace 経過済 (or end_time 不明) → 同一 URL は再出品されても買えない = unavailable 確定
    return {
        "conclusive": True, "status": "unavailable",
        "signal": "auction ended (no winner, grace elapsed)", "checked_at": checked_at,
    }


def _classify_candidate(url: str, source_platform: Optional[str]) -> dict:
    """候補 URL の availability を判定する.

    Returns: {'conclusive': bool, 'status': str, 'signal': str, 'checked_at': str}
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    if not url:
        return {
            "conclusive": False, "status": "unknown",
            "signal": "empty url", "checked_at": checked_at,
        }

    if "auctions.yahoo.co.jp" in url:
        return _classify_yahoo_with_grace(url, checked_at)

    # それ以外は既存 W182 gate 関数を流用 (mercari / paypay / yahoo shopping 等)
    avail = check_candidate_availability(url)
    status = avail.get("status") or "unknown"
    return {
        "conclusive": status in _CONCLUSIVE_STATUSES,
        "status": status,
        "signal": avail.get("signal"),
        "checked_at": avail.get("checked_at") or checked_at,
    }


def run_supplier_availability_recheck(config: dict) -> dict:
    """朝バッチ: 仕入先候補 (pending/accepted) の availability を定期再チェックする.

    Returns:
        {'success', 'processed', 'checked', 'rejected', 'skipped', 'message'}
    """
    task_cfg = (config or {}).get("tasks_enabled", {}).get(
        "supplier_availability_recheck") or {}
    stale_days = int(task_cfg.get("stale_days", 3))
    # LOW-4 (2026-07-04 Codex): 負値 (LIMIT -1 = 無制限) や過大値の暴走を防ぐため clamp。
    # 上限 500 件は外部サイトへの負荷 + APScheduler thread 占有時間の実務上限。
    _raw_max = int(task_cfg.get("max_candidates_per_run", 100))
    max_candidates = max(0, min(_raw_max, 500))
    sleep_sec = float(task_cfg.get("sleep_between_checks_sec", 1.0))

    targets = get_supplier_candidates_for_availability_recheck(stale_days, max_candidates)
    logger.info(
        f"仕入先候補 availability 再チェック対象: {len(targets)}件 "
        f"(stale>={stale_days}日 / max={max_candidates})"
    )
    if not targets:
        return {
            "success": True, "processed": 0, "checked": 0,
            "rejected": 0, "skipped": 0, "first_strike": 0,
            "notified_accepted": 0, "high_unknown_ratio": False,
            "message": "再チェック対象なし",
        }

    checked = 0
    rejected = 0
    skipped = 0
    first_strike = 0  # MED-2: 1 回目の conclusive-unavailable で保留にした件数
    notified_accepted = 0  # MED-2: 採用済み→却下反転で通知した件数

    for idx, cand in enumerate(targets, start=1):
        cid = cand["id"]
        url = cand.get("candidate_url") or ""
        platform = cand.get("source_platform")

        try:
            result = _classify_candidate(url, platform)
        except Exception as e:  # noqa: BLE001 — Q0: 判定失敗は skip として必ず記録
            skipped += 1
            logger.warning(f"  id={cid} 判定中に例外発生 (据え置き): {e}", exc_info=True)
            # MED-1: 判定不能でも attempted_at を進める (starvation 防止)
            try:
                record_supplier_candidate_availability_attempt(cid)
            except Exception as _e2:  # noqa: BLE001
                logger.warning(f"  id={cid} attempt 記録失敗 (無視): {_e2}")
            if idx < len(targets) and sleep_sec > 0:
                time.sleep(sleep_sec)
            continue

        if not result["conclusive"]:
            skipped += 1
            logger.info(
                f"  id={cid} 判定保留 status={result['status']} "
                f"signal={result['signal']!r} (据え置き、次回再チェック)"
            )
            # MED-1: 判定不能でも attempted_at を進める (starvation 防止、checked_at は据え置き)
            try:
                record_supplier_candidate_availability_attempt(cid)
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"  id={cid} attempt 記録失敗 (無視): {_e}")
            if idx < len(targets) and sleep_sec > 0:
                time.sleep(sleep_sec)
            continue

        checked += 1
        prior_pending_reject = int(cand.get("availability_pending_reject") or 0)

        if result["status"] in ("unavailable", "not_found"):
            if prior_pending_reject == 0:
                # MED-2: 1 回目の conclusive-unavailable は保留 (実 reject しない)。
                # 単発誤判定 (404 誤返し等) 1 回で恒久 auto_rejected=1 化される事故を防ぐ。
                marked = mark_supplier_candidate_pending_reject(
                    cid, result["status"], result.get("signal"), result.get("checked_at"),
                )
                if marked:
                    first_strike += 1
                    logger.info(
                        f"  id={cid} 1st strike status={result['status']} "
                        f"signal={result['signal']!r} (次回同判定なら却下)"
                    )
                else:
                    logger.warning(
                        f"  id={cid} 1st strike 記録 rowcount=0 "
                        "(status が pending/accepted から変わった?)"
                    )
            else:
                # MED-2: 2 回連続 conclusive-unavailable → 実 reject
                prior_status = (cand.get("status") or "").lower()
                updated = reject_supplier_candidate_availability(
                    cid, result["status"], result.get("signal"), result.get("checked_at"),
                )
                if updated:
                    rejected += 1
                    logger.info(
                        f"  id={cid} 却下 (2nd strike) status={result['status']} "
                        f"signal={result['signal']!r}"
                    )
                    # MED-2: 採用済み (accepted) の反転のみ通知
                    if prior_status == "accepted":
                        notified_accepted += 1
                        _notify_accepted_candidate_sold_out(cand, result)
                else:
                    logger.warning(
                        f"  id={cid} UPDATE rowcount=0 (status が pending/accepted から"
                        f"変わった? 却下スキップ)"
                    )
        else:
            # conclusive-available: 1st strike が立っていたなら「間に available が
            # 挟まった」= シグナルクリア (MED-2)。update_supplier_candidate_availability
            # 内で availability_pending_reject=0 に戻す。
            updated = update_supplier_candidate_availability(
                cid, result["status"], result.get("signal"), result.get("checked_at"),
            )
            if not updated:
                logger.warning(
                    f"  id={cid} availability 更新 rowcount=0 (status変化? skip)"
                )

        if idx < len(targets) and sleep_sec > 0:
            time.sleep(sleep_sec)

    # MED-1: 処理数十分あって 8 割超が保留 = サイト構造変化のサインの可能性。
    # 通知までは不要 (依頼ボード reviewer 指示)、log + summary flag で足りる。
    processed = len(targets)
    high_unknown_ratio = False
    if processed >= _UNKNOWN_RATIO_MIN_PROCESSED:
        ratio = skipped / processed if processed else 0.0
        if ratio > _UNKNOWN_RATIO_THRESHOLD:
            high_unknown_ratio = True
            logger.warning(
                f"[#45] 判定保留比率が高い (skipped={skipped}/{processed}, "
                f"ratio={ratio:.0%}): サイト構造変化の可能性あり要調査"
            )

    msg = (
        f"{processed}件走査 / 確定{checked}件 (1st strike{first_strike}件, 却下{rejected}件, "
        f"採用済み反転{notified_accepted}件) / 保留{skipped}件"
    )
    if high_unknown_ratio:
        msg += " [WARN: 高保留比率]"
    logger.info(f"仕入先候補 availability 再チェック完了: {msg}")
    return {
        "success": True,
        "processed": processed,
        "checked": checked,
        "rejected": rejected,
        "skipped": skipped,
        "first_strike": first_strike,
        "notified_accepted": notified_accepted,
        "high_unknown_ratio": high_unknown_ratio,
        "message": msg,
    }


if __name__ == "__main__":
    # 手動テスト: python -m tasks.task_supplier_availability_recheck
    import json
    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    r = run_supplier_availability_recheck(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
