#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W301 AI 店長 Phase1 S4: rival_classify 定時オーケストレーション (毎日 03:00 JST).

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md
  §5 (task_rival_classify.py コンポーネント設計) / §6 (データフロー) / §8 (money-direct安全)。

責務:
  1. `listing_rival_discoveries` の未分類 (status='new') を読み込み、自社
     `ebay_listings` (ebay_item_id で JOIN, sku 不使用) から signals を構築する。
  2. `monitor/rival_classifier.classify_batch` で分類 (ハード除外→スコア→
     グレーのみ Claude Haiku)。同関数が rival_classifications への保存も行う。
  3. 3 分岐の DB 反映 (設計書 §6):
       - noise  → listing_rival_discoveries.status = 'dismissed'
       - real   → status = 'monitoring_added' + competitor_products upsert
                  (add_or_reactivate_competitor は 新規 INSERT=default 0、
                  再活性化=pricing_eligible を 0 に強制リセット (W301 HIGH-1 修正、
                  残留 eligible=1 の復活防止)。いずれも Shadow 安全)
       - review → status は 'new' のまま (既存 triage UI へ、次回 run でも
                  再分類対象になる仕様、設計書 §6 逐語)
     ※ この 3 分岐反映は S4 の指示文には明記されていないが、実施しないと
       status='new' の discovery が毎日再分類され続け、AI コストが無限に
       積み上がる (Q0 相当の運用リスク)。設計書 §5/§6 (必読 #1) が明記する
       挙動のため、本タスクの一部として実装した (実装判断として報告書に明記)。
  4. Shadow 固定 (shadow_mode=True、Phase1 は常に True)。pricing_eligible
     列は本ファイルからは一切書き込まない (ライフサイクル方針 = 停止時クリア /
     inactive→active 遷移時のみ 0 リセット / 立てるのは user UI トグル専用、
     は add_or_reactivate_competitor 側で一元管理。W301 HIGH#1/MED#2 修正)。
  5. kill switch: config['tasks_enabled']['rival_classify']['enabled']
     (default True)。無効時は success=True + skip 痕跡を返す (Q0)。
  6. max_ai_calls_per_run は config で上書き可能 (未指定は rival_classifier の
     デフォルト 50)。cap 超過 (route='ai_cap_exceeded') / AI 例外
     (ai_error/ai_parse_error/ai_key_missing) は review へ倒れつつ、
     件数を集計して Discord へ通知する (既存 W153 _resolve_rival_webhook /
     _send_discord_errors_alert の流儀を踏襲)。

SKU 規約: 本タスクは SKU を listing 識別に使わない (ebay_item_id /
  competitor_item_id のみ)。our_sku は add_or_reactivate_competitor への
  補助情報としてのみ受け渡す (sku-rules.md 許可用途外の使用はしない)。
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional

# pythonw.exe gotcha guard
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from monitor.database import (
    add_or_reactivate_competitor,
    get_conn,
    get_ddu_seller_ids,
    get_self_ebay_item_ids,
    get_warning_brand_names,
    update_rival_discovery_status,
)
from monitor.rival_classifier import DEFAULT_THRESHOLDS, classify_batch

logger = logging.getLogger(__name__)

# 分類が AI 判定 (成功/失敗問わず) を経由したことを示す route 群
_AI_ATTEMPTED_ROUTES = ("ai", "ai_error", "ai_parse_error", "ai_key_missing")
# Q0: review+log+Discord 対象 (cap 超過 / AI 例外系。ハード除外や純スコア noise は対象外)
_ISSUE_ROUTES = ("ai_cap_exceeded", "ai_error", "ai_parse_error", "ai_key_missing")


def _derive_our_rank(ebay_condition_id: Optional[str], condition_rank: Optional[str]) -> Optional[str]:
    """CLAUDE.md コンディションランク 8 段階 (N/S/A/B/C/D/PO/As-Is) を
    ebay_listings.ebay_condition_id (実 eBay ConditionID) + condition_rank
    (3000=Used のサブランク) から導出する。

    ⚠️ ebay_listings.rank 列 (人気度グレード、v66 migration comment) とは
    別物であり本関数では参照しない (CLAUDE.md / md-files-can-be-wrong 準拠)。
    """
    if not ebay_condition_id:
        return None
    cid = str(ebay_condition_id).strip()
    if cid == "1000":
        return "N"
    if cid == "1500":
        return "S"
    if cid == "7000":
        return "As-Is"
    if cid == "3000":
        return condition_rank or None
    return None


def _fetch_new_discoveries() -> list[dict]:
    """status='new' の discoveries + 自社 ebay_listings (ebay_item_id JOIN) から signals 構築."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                lrd.id AS discovery_id,
                lrd.ebay_item_id,
                lrd.competitor_item_id,
                lrd.competitor_seller,
                lrd.competitor_title,
                lrd.competitor_price_usd,
                el.title AS our_title,
                el.current_price AS our_price_usd,
                el.sku AS our_sku,
                el.ebay_condition_id AS our_ebay_condition_id,
                el.condition_rank AS our_condition_rank
            FROM listing_rival_discoveries lrd
            LEFT JOIN ebay_listings el ON el.ebay_item_id = lrd.ebay_item_id
            WHERE lrd.status = 'new'
            ORDER BY lrd.first_seen_at ASC
            """
        ).fetchall()
    signals_list = []
    for r in rows:
        d = dict(r)
        d["our_rank"] = _derive_our_rank(
            d.pop("our_ebay_condition_id"), d.pop("our_condition_rank")
        )
        signals_list.append(d)
    return signals_list


def _resolve_classify_webhook(config: dict) -> str:
    """ライバル専用 webhook 優先, 未設定なら既定へ fallback (task_rival_detection.py と同方針)."""
    disc = config.get('discord') or {}
    return (disc.get('rival_webhook_url') or disc.get('webhook_url') or "").strip()


def _fetch_review_backlog() -> int:
    """review 判定で status='new' のまま滞留している discovery の累計件数.

    real/noise は status が dismissed/monitoring_added に遷移するため、run 後に
    status='new' で残るのは review 判定 (+ 未処理分) のみ (本ファイル冒頭 docstring
    「責務 3.」の 3 分岐反映を参照)。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listing_rival_discoveries WHERE status='new'"
        ).fetchone()
    return int(row[0]) if row else 0


def _send_daily_summary(config: dict, result: dict) -> None:
    """依頼ボード#46: rival_classify 完了時に AI 店長の稼働状況を日次 1 通発行.

    「動きが見えない」への対応 (Q0 可視化)。分類 0 件の日でも「実行済」を明示して
    送る (silent skip との誤認防止)。既存 `_send_discord_issue_alert` (cap 超過 /
    AI 例外系、warning) とは役割分離 — 本関数は稼働報告 (severity='info')。
    通知記録 (notification_log) は notifiers.notification_center.record_and_maybe_send
    choke point 経由で行い、category='rival' の既存 discord_category_gate
    (config/schedule_config.json) にそのまま従う (新カテゴリを増やさない, K1)。
    通知処理自体の失敗でタスク結果 (result) を壊さない (Q0: 既に result は確定済)。
    """
    try:
        review_backlog = _fetch_review_backlog()
        title = "🤖 AI店長 日次"
        body = (
            f"本日 real {result.get('real', 0)} 件 / noise {result.get('noise', 0)} 件 "
            f"(実行済) / review 滞留 {review_backlog} 件 (累計)\n"
            f"Shadow 運用中 — 自動値付けへの反映はまだありません (監視・分類のみ)"
        )
        from datetime import date as _date
        dedupe_key = f"rival_classify_daily_summary_{_date.today().isoformat()}"

        from notifiers.notification_center import record_and_maybe_send
        record_and_maybe_send(
            "rival", "info", title, body,
            link_target="rival", dedupe_key=dedupe_key,
        )
    except Exception as e:  # noqa: BLE001 — 通知失敗を run_rival_classify の失敗にしない
        logger.warning(f"[W301 S4] daily summary 通知失敗 (痕跡のみ): {e}")


def _send_discord_issue_alert(config: dict, summary: dict, issue_items: list[dict]) -> None:
    """cap 超過 / AI 例外系の review 転落を Discord 通知 (Q0: review+log+Discord)."""
    webhook = _resolve_classify_webhook(config)
    if not webhook:
        logger.warning(
            "[W301 S4] Discord webhook 未設定 — rival_classify issue 通知をスキップ (痕跡)"
        )
        return
    from notifiers.discord_notifier import DiscordNotifier
    excerpt = []
    for it in issue_items[:5]:
        eid = str(it.get("ebay_item_id") or "")
        title = (it.get("our_title") or eid or "?")[:40]
        excerpt.append(f"- {title} ({eid[-4:]}): {(it.get('reason') or '')[:80]}")
    extra = len(issue_items) - 5
    content = (
        f"🤖 **AI店長 要確認 {len(issue_items)} 件** "
        f"(本日処理 {summary.get('processed', 0)} 件中)\n"
        + "\n".join(excerpt)
        + (f"\n... 他 {extra} 件" if extra > 0 else "")
        + "\n→ AI が自動判定できず保留した商品です。現状は Shadow 運用中で自動値付けへの"
          "影響はありません（対応不要。内容確認は商品管理タブの「新規発見ライバル」）。"
    )
    try:
        DiscordNotifier(webhook, bypass_env=True).send_message(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[W301 S4] discord issue alert failed: {e}")


def run_rival_classify(config: Optional[dict] = None) -> dict:
    """cron 経路. daily_scheduler.py から呼ばれる (毎日 03:00 JST)."""
    cfg = config or {}
    task_cfg = (cfg.get('tasks_enabled') or {}).get('rival_classify') or {}
    result: dict = {
        "success": False, "processed": 0, "real": 0, "noise": 0, "review": 0,
        "ai_calls_used": 0, "issues": 0, "self_excluded": 0, "message": "",
    }

    # ── kill switch (Q0: skip も痕跡) ──
    if not task_cfg.get('enabled', True):
        result["success"] = True
        result["message"] = "rival_classify: enabled=false → skip"
        logger.info(result["message"])
        return result

    try:
        discoveries = _fetch_new_discoveries()
        if not discoveries:
            result["success"] = True
            result["message"] = "0 discoveries (status='new') — skip"
            logger.info(f"[W301 S4] {result['message']}")
            _send_daily_summary(cfg, result)
            return result

        dou_blacklist = get_ddu_seller_ids()
        warning_brands = get_warning_brand_names()
        # W308: 自社出品との自己マッチ遮断 (competitor_item_id が自社 ebay_item_id
        # と一致する discovery を AI を呼ばず noise 直行させる)。
        self_item_ids = get_self_ebay_item_ids()

        thresholds = dict(DEFAULT_THRESHOLDS)
        max_ai_calls = task_cfg.get('max_ai_calls_per_run')
        if max_ai_calls is not None:
            thresholds['max_ai_calls_per_run'] = int(max_ai_calls)

        # Phase1 は Shadow 固定 (design doc §11 Q1 が未解決のため常に True。
        # 将来 user 合意で解除する場合も本タスクの外側 (config) で切替える設計にすべきで、
        # 現時点では config 値を信用せずコード側で固定 = 誤って解除される事故を防ぐ).
        shadow_mode = True

        results = classify_batch(
            discoveries,
            dou_blacklist=dou_blacklist,
            warning_brands=warning_brands,
            thresholds=thresholds,
            shadow_mode=shadow_mode,
            persist=True,
            self_item_ids=self_item_ids,
        )

        issue_items: list[dict] = []
        for signals, res in zip(discoveries, results):
            discovery_id = signals["discovery_id"]
            if res.route in _AI_ATTEMPTED_ROUTES:
                result["ai_calls_used"] += 1
            if res.exclude_reason == "self_listing":
                result["self_excluded"] += 1

            if res.classification == "noise":
                update_rival_discovery_status(discovery_id, "dismissed")
                result["noise"] += 1
            elif res.classification == "real":
                # W301 MED#3 fix (2026-07-02): add_or_reactivate_competitor が成功して
                # から status='monitoring_added' へ更新する順序に変更。失敗時は
                # status='new' のまま維持 = 翌 run の rival_classify で再試行可能
                # (Q0: 個別失敗が silent skip で永久に取り残されない)。
                # 旧順序 (status 先→add 後) では add 失敗時に status='monitoring_added'
                # に確定してしまい、competitor_products への登録が抜けたまま
                # discovery が「対処済」状態になり以降永久に拾われない silent gap
                # (money-direct: W183 追従対象登録漏れ) が発生していた。
                try:
                    add_or_reactivate_competitor(
                        our_item_id=signals["ebay_item_id"],
                        our_sku=signals.get("our_sku") or "",
                        competitor_seller=signals.get("competitor_seller") or "",
                        competitor_item_id=signals["competitor_item_id"],
                    )
                    update_rival_discovery_status(discovery_id, "monitoring_added")
                except Exception as e:  # noqa: BLE001 — Q0: 個別失敗で run 全体を止めない
                    logger.warning(
                        f"[W301 S4] add_or_reactivate_competitor failed "
                        f"(ebay_item_id={signals['ebay_item_id']}, "
                        f"competitor_item_id={signals['competitor_item_id']}): "
                        f"{type(e).__name__}: {e} — "
                        f"status='new' を維持 (翌 run 再試行、MED#3)"
                    )
                result["real"] += 1
            else:
                # 'review' — status='new' のまま維持 (既存 triage UI, 設計書 §6)
                result["review"] += 1

            if res.route in _ISSUE_ROUTES:
                issue_items.append({
                    "ebay_item_id": signals.get("ebay_item_id"),
                    "our_title": signals.get("our_title"),
                    "competitor_item_id": signals.get("competitor_item_id"),
                    "route": res.route,
                    "reason": res.reason,
                })

        result["processed"] = len(discoveries)
        result["issues"] = len(issue_items)
        result["success"] = True
        result["message"] = (
            f"processed={result['processed']} real={result['real']} "
            f"noise={result['noise']} review={result['review']} "
            f"ai_calls={result['ai_calls_used']} issues={result['issues']} "
            f"self_excluded={result['self_excluded']}"
        )
        logger.info(f"[W301 S4] {result['message']}")

        if issue_items:
            _send_discord_issue_alert(cfg, result, issue_items)
        _send_daily_summary(cfg, result)
    except Exception as e:
        logger.exception("[W301 S4] run_rival_classify failed")
        result["message"] = f"top-level: {type(e).__name__}: {e}"
        result["success"] = False
    return result


if __name__ == "__main__":
    # 手動 CLI 実行用 (cron 同等)。
    # 注: task_rival_detection.py 等の既存 __main__ は `monitor.config_loader`
    # を import しているが、本 codebase に当該モジュールは存在しない (dead code、
    # md-files-can-be-wrong: 既存コードも誤りを含み得る)。実体は daily_scheduler.load_config。
    from daily_scheduler import load_config  # type: ignore
    _cfg = load_config()
    _r = run_rival_classify(_cfg)
    print(json.dumps(_r, indent=2, ensure_ascii=False))
