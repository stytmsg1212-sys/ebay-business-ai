#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W301 AI 店長 Phase1 S4: 競合定点観測 (competitor_snapshots 蓄積、毎日 05:30 JST).

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md
  §5 (competitor_snapshot.py コンポーネント設計) / §6 (データフロー、Phase1 は蓄積のみ)。

責務:
  1. 対象 = 「値下げ適格の active 競合 (pricing_eligible=1)」 と
     「rival_classifications で real/review と判定された競合」 の和集合
     (`monitor.database.get_snapshot_targets`)。
  2. GetItem で quantity_sold / quantity_available / seller feedback / country /
     price / shipping を取得し `competitor_snapshots` に INSERT (蓄積のみ、
     Phase2 で消費予定。本タスクは既存 competitor_products / rival_classifications
     への書き込みは一切行わない = 読み取り専用の観測タスク)。
  3. 1 run の API コール上限 (config `max_calls_per_run`、既定
     DEFAULT_MAX_CALLS_PER_RUN) を超えた分は処理せず、翌日以降の run に自然に
     持ち越す (`get_snapshot_targets` は「最終取得が古い順」に並ぶため、
     今回処理しきれなかった対象は次回優先的に処理される = starvation なし)。
  4. 失敗した item は skip せず件数を summary に集計する (Q0: silent skip 禁止)。
  5. kill switch: config['tasks_enabled']['competitor_snapshot']['enabled']
     (default True)。

⚠️ ebay_client.get_competitor_snapshot_batch の docstring 参照: 設計書は
  Shopping API GetMultipleItems (20 件/コール) を前提にしていたが、本 codebase
  には Shopping API クライアントが存在しないため Trading API GetItem
  (1 ItemID/コール) で実装している。「1 コール」の意味が設計書の想定と異なる
  (20 件/コールではなく 1 件/コール) 点は報告書に明記する。

SKU 規約: 本タスクは SKU を一切参照しない (listing 識別は competitor_item_id /
  our_item_id のみ、sku-rules.md 準拠)。
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

from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
from monitor.database import get_snapshot_targets, insert_competitor_snapshot

logger = logging.getLogger(__name__)

# 1 run で消費してよい API コール上限 (= item 数、Trading API GetItem は 1 件/コール).
# 設計書の「30 コール = 600 商品 (20件/コール想定)」から、本実装の実際の粒度
# (1件/コール) に合わせて調整した既定値 (config で上書き可能)。
DEFAULT_MAX_CALLS_PER_RUN = 100


def _resolve_snapshot_webhook(config: dict) -> str:
    disc = config.get('discord') or {}
    return (disc.get('rival_webhook_url') or disc.get('webhook_url') or "").strip()


def _send_discord_summary(config: dict, summary: dict) -> None:
    """失敗が一定件数を超えた run のみ通知 (毎日の正常蓄積は通知不要、alert fatigue 抑制)."""
    webhook = _resolve_snapshot_webhook(config)
    if not webhook:
        logger.warning(
            "[W301 S4] Discord webhook 未設定 — competitor_snapshot 失敗通知をスキップ (痕跡)"
        )
        return
    from notifiers.discord_notifier import DiscordNotifier
    content = (
        f"⚠️ **W301 competitor_snapshot 失敗多発**\n"
        f"対象={summary['targets']} 件中 成功={summary['captured']} / "
        f"失敗={summary['failed']} (API コール={summary['api_calls_used']}, "
        f"残={summary['remaining']} 件は翌回へ持ち越し)"
    )
    try:
        DiscordNotifier(webhook, bypass_env=True).send_message(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[W301 S4] discord summary alert failed: {e}")


def run_competitor_snapshot(config: Optional[dict] = None) -> dict:
    """cron 経路. daily_scheduler.py から呼ばれる (毎日 05:30 JST)."""
    cfg = config or {}
    task_cfg = (cfg.get('tasks_enabled') or {}).get('competitor_snapshot') or {}
    result: dict = {
        "success": False, "targets": 0, "captured": 0, "failed": 0,
        "api_calls_used": 0, "remaining": 0, "message": "",
    }

    # ── kill switch (Q0: skip も痕跡) ──
    if not task_cfg.get('enabled', True):
        result["success"] = True
        result["message"] = "competitor_snapshot: enabled=false → skip"
        logger.info(result["message"])
        return result

    try:
        max_calls = int(task_cfg.get('max_calls_per_run', DEFAULT_MAX_CALLS_PER_RUN))

        targets = get_snapshot_targets()
        total_targets = len(targets)
        if total_targets == 0:
            result["success"] = True
            result["message"] = (
                "0 targets (pricing_eligible=1 の active 競合、および "
                "rival_classifications real/review が 0 件) — skip"
            )
            logger.info(f"[W301 S4] {result['message']}")
            return result

        batch = targets[:max_calls]
        remaining = max(0, total_targets - len(batch))
        result["targets"] = total_targets
        result["remaining"] = remaining

        creds = get_ebay_credentials(cfg)
        if not ebay_credentials_ok(creds):
            result["message"] = "eBay 認証情報未設定"
            logger.error(f"[W301 S4] {result['message']}")
            return result

        from monitor.ebay_client import get_competitor_snapshot_batch

        item_ids = [t["competitor_item_id"] for t in batch]
        item_to_our = {t["competitor_item_id"]: t.get("our_item_id") for t in batch}

        snapshots, calls_used = get_competitor_snapshot_batch(
            item_ids,
            creds.get("app_id", ""),
            creds.get("dev_id", ""),
            creds.get("cert_id", ""),
            creds.get("user_token", ""),
            max_calls=max_calls,
        )
        result["api_calls_used"] = calls_used

        captured = 0
        failed = 0
        for item_id in item_ids:
            data = snapshots.get(item_id)
            if data is None:
                failed += 1
                continue
            try:
                insert_competitor_snapshot(
                    competitor_item_id=item_id,
                    our_item_id=item_to_our.get(item_id),
                    **data,
                )
                captured += 1
            except Exception as e:  # noqa: BLE001 — Q0: 個別失敗で run 全体を止めない
                failed += 1
                logger.warning(
                    f"[W301 S4] insert_competitor_snapshot failed "
                    f"(competitor_item_id={item_id}): {type(e).__name__}: {e}"
                )

        result["captured"] = captured
        result["failed"] = failed
        # W301 MED#4 fix (2026-07-02): batch>0 かつ captured==0 (全滅) は
        # success=False として扱う (W245 パターンと整合)。API 障害や credential
        # 期限切れ等の網羅的失敗を「success=True で failed=N」と偽装成功しない
        # (Q0 silent-skip-prevention)。部分成功 (captured>0) は従来どおり success=True。
        if len(batch) > 0 and captured == 0:
            result["success"] = False
        else:
            result["success"] = True
        result["message"] = (
            f"targets={total_targets} batch={len(batch)} captured={captured} "
            f"failed={failed} api_calls={calls_used} remaining={remaining}"
        )
        logger.info(f"[W301 S4] {result['message']}")

        # Q0: 失敗が半数以上なら Discord (API 障害の早期検知、正常時は通知しない)
        if len(batch) > 0 and failed >= max(3, len(batch) // 2):
            _send_discord_summary(cfg, result)
    except Exception as e:
        logger.exception("[W301 S4] run_competitor_snapshot failed")
        result["message"] = f"top-level: {type(e).__name__}: {e}"
        result["success"] = False
    return result


if __name__ == "__main__":
    # 手動 CLI 実行用 (cron 同等)。
    from daily_scheduler import load_config  # type: ignore
    _cfg = load_config()
    _r = run_competitor_snapshot(_cfg)
    print(json.dumps(_r, indent=2, ensure_ascii=False))
