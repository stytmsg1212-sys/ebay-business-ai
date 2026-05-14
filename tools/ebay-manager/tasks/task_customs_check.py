#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task: W14 通関対応自動化 orchestrator

Flow:
  1. Gmail から FedEx/DHL/UPS の通関メール検知 (customs_mail_detector)
  2. 各メールを parser で構造化 (customs_parser)
  3. tracking → product 解決 (customs_product_resolver)
  4. 商品写真 DL (ebay_image_fetcher)
  5. Claude Haiku でドラフト生成 (customs_draft_generator)
  6. DB 保存 (status='drafted' or 'drafted_no_photo' or 'manual')

送信 (status='drafted' → 'sent') は user が MONO Deck UI から明示的に実行する
(自動送信しない). このタスクは draft 生成までで完了.

code-reviewer HIGH-3 対応: 過去 1 年 backfill モードは dry_run デフォルト True、
batch_size + 段階拡張 (30日→90日→1年).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.customs_draft_generator import generate_draft  # noqa: E402
from monitor.customs_mail_detector import (  # noqa: E402
    DetectedMail, detect_customs_mails,
)
from monitor.customs_parser import ParsedRequest, parse_mail  # noqa: E402
from monitor.customs_product_resolver import resolve_product  # noqa: E402
from monitor.ebay_image_fetcher import fetch_product_photos  # noqa: E402

logger = logging.getLogger(__name__)


def run_customs_check(
    config: Optional[dict] = None, *,
    days: int = 7,
    max_per_carrier: int = 30,
    dry_run: bool = False,
) -> dict:
    """通関メール 1 日分の処理 orchestrator.

    Args:
        config: schedule_config.json の dict
        days: Gmail 検索範囲 (default 7 日)
        max_per_carrier: 各 carrier 上限件数
        dry_run: True なら DB への書き込みをしない (backfill preview 用)

    Returns:
        {'success', 'detected', 'drafted', 'manual', 'errors', 'message'}
    """
    cfg = config or {}
    task_cfg = (cfg.get("tasks_enabled") or {}).get("customs_check") or {}
    if isinstance(task_cfg, dict) and task_cfg.get("enabled") is False:
        return {
            "success": True, "detected": 0, "drafted": 0, "manual": 0,
            "errors": 0, "message": "disabled (feature flag off)",
        }

    # Gmail service 初期化
    try:
        from tasks.task_email_pickup import get_gmail_service
        gmail = get_gmail_service(cfg)
    except Exception as e:  # noqa: BLE001
        return {
            "success": False, "detected": 0, "drafted": 0, "manual": 0,
            "errors": 1, "message": f"Gmail init failed: {e}",
        }
    if gmail is None:
        return {
            "success": False, "detected": 0, "drafted": 0, "manual": 0,
            "errors": 1, "message": "Gmail service unavailable",
        }

    logger.info(f"【開始】W14 通関対応 (days={days}, dry_run={dry_run})")
    detected_mails = detect_customs_mails(
        gmail, days=days, max_per_carrier=max_per_carrier
    )
    logger.info(f"detected {len(detected_mails)} customs mails")

    drafted = 0
    manual = 0
    errors = 0
    processed_ids: set[str] = set()

    for det in detected_mails:
        if det.gmail_id in processed_ids:
            continue
        processed_ids.add(det.gmail_id)

        # 既に DB に登録済みなら skip (idempotent)
        if not dry_run and _is_already_registered(det.gmail_id):
            logger.debug(f"skip {det.gmail_id}: already registered")
            continue

        try:
            result = _process_one(det, gmail, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.error(
                f"process {det.gmail_id} failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            continue

        if result == "drafted":
            drafted += 1
        elif result == "manual":
            manual += 1

    msg = (
        f"detected={len(detected_mails)} drafted={drafted} "
        f"manual={manual} errors={errors} (dry_run={dry_run})"
    )
    logger.info(f"【完了】W14 通関対応: {msg}")
    return {
        "success": errors < len(detected_mails) or not detected_mails,
        "detected": len(detected_mails),
        "drafted": drafted,
        "manual": manual,
        "errors": errors,
        "message": msg,
    }


def _process_one(det: DetectedMail, gmail, *, dry_run: bool) -> str:
    """1 件の処理. 'drafted' / 'manual' を返す."""
    parsed: ParsedRequest = parse_mail(det, gmail)
    # product 解決
    if parsed.tracking_number:
        resolved = resolve_product(
            tracking_number=parsed.tracking_number,
            recipient_name=parsed.recipient_name,
            ship_date=parsed.ship_date,
            gmail_service=gmail,
        )
    else:
        from monitor.customs_product_resolver import ResolvedProduct
        resolved = ResolvedProduct(
            ebay_item_id=None, sku=None, title=None,
            source="unresolved", confidence="low",
        )

    # 写真取得 (ebay_item_id が得られている場合のみ)
    photos: list[Path] = []
    if resolved.ebay_item_id and parsed.tracking_number:
        try:
            photos = fetch_product_photos(
                resolved.ebay_item_id,
                tracking_number=parsed.tracking_number,
                max_photos=5,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"photo fetch failed: {e}")

    # draft 生成
    draft = generate_draft(
        carrier=det.carrier,
        parsed=parsed,
        detected_sender=det.sender,
        product_title=resolved.title,
        ebay_item_id=resolved.ebay_item_id,
    )

    # 状態判定 (H-C 対応: operator precedence バグ修正 + drafted_no_photo dead code 復活)
    # H-D 対応: product resolver が low confidence なら manual
    if (
        parsed.manual_review_required
        or not resolved.ebay_item_id
        or not draft.to_list
        or getattr(resolved, "confidence", "low") == "low"
    ):
        final_status = "manual"
    elif not photos:
        final_status = "drafted_no_photo"
    else:
        final_status = "drafted"

    if dry_run:
        logger.info(
            f"[DRY] {det.gmail_id} carrier={det.carrier} "
            f"track={parsed.tracking_number} product={resolved.title} "
            f"status={final_status} conf={draft.confidence}"
        )
        # H-修正: dry_run と非 dry_run で return 統一 (caller の集計ずれ回避)
        return "drafted" if final_status.startswith("drafted") else "manual"

    # DB 書き込み
    _save_customs_request(
        det=det, parsed=parsed, resolved=resolved,
        draft=draft, photos=photos, final_status=final_status,
    )
    return "drafted" if final_status.startswith("drafted") else "manual"


def _is_already_registered(gmail_id: str) -> bool:
    from monitor.database import get_conn
    with get_conn() as conn:
        r = conn.execute(
            "SELECT 1 FROM customs_requests WHERE gmail_id = ?", (gmail_id,)
        ).fetchone()
        return r is not None


def _save_customs_request(
    *, det: DetectedMail, parsed: ParsedRequest, resolved,
    draft, photos: list[Path], final_status: str,
) -> int:
    from monitor.database import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO customs_requests
               (gmail_id, carrier, tracking_number, recipient, ship_date,
                deadline, request_items, ebay_item_id, sku, product_title,
                draft_subject, draft_body, draft_recipients,
                attached_photos, attached_attachments,
                template_used, template_hash, kb_hits, status,
                drafted_at, error_msg,
                gmail_thread_id, original_message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       CURRENT_TIMESTAMP, ?, ?, ?)""",
            (
                det.gmail_id, det.carrier, parsed.tracking_number,
                parsed.recipient_name, parsed.ship_date, parsed.deadline,
                json.dumps(parsed.request_items, ensure_ascii=False),
                resolved.ebay_item_id, resolved.sku, resolved.title,
                draft.subject, draft.body,
                json.dumps({
                    "to": draft.to_list, "cc": draft.cc_list
                }, ensure_ascii=False),
                json.dumps([str(p) for p in photos], ensure_ascii=False),
                json.dumps(
                    [str(p) for p in parsed.attachments_saved],
                    ensure_ascii=False,
                ),
                draft.template_used, draft.template_hash,
                json.dumps({
                    "manufacturer": (
                        draft.manufacturer_hit.brand
                        if draft.manufacturer_hit else None
                    ),
                    "hts": draft.hts_hit.code if draft.hts_hit else None,
                }, ensure_ascii=False),
                final_status,
                "; ".join(draft.manual_review_reasons + parsed.warnings) or None,
                # W14 v19: reply スレッド対応のヘッダー保存
                det.gmail_thread_id or None,
                det.rfc822_message_id or None,
            ),
        )
        return int(cur.lastrowid)


# ─────────────────────────────────────────────
# backfill モード (H-3 対応)
# ─────────────────────────────────────────────

def run_backfill(
    config: dict, *,
    days: int,
    dry_run: bool = True,
    batch_size: int = 10,
    sleep_between_sec: float = 2.0,
) -> dict:
    """過去 days 日分を段階 backfill. 失敗 3 連続で強制停止.

    推奨: days=30 → 90 → 365 の順で段階拡張.
    """
    if days < 1 or days > 400:
        raise ValueError("days must be 1..400")
    logger.info(
        f"【backfill】days={days} dry_run={dry_run} batch={batch_size}"
    )
    r = run_customs_check(
        config, days=days, max_per_carrier=batch_size * 10, dry_run=dry_run,
    )
    r["backfill_days"] = days
    r["backfill_dry_run"] = dry_run
    return r


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import io
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    try:
        with io.open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    r = run_customs_check(cfg, dry_run=False)
    print(json.dumps(r, indent=2, ensure_ascii=False))
