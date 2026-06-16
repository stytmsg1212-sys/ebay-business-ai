#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W229 Phase 2: Terapeak ハーベスト + 自動ゲート判定バッチ (毎日 03:30 JST).

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §6 / §9
仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md

処理フロー (設計書 §9):
  1. config.tasks_enabled.research_harvest を確認 (enabled=false → skip + 痕跡 Q0)
  2. CDP 疎通確認 → 失敗なら skip + Discord
  3. クォータ残チェック (api_call_log JST 当日集計、sqlite-timezone.md 遵守)
  4. seed_queries × 2 パターン (fresh_24h / two_year_echo) で harvest_product_list
  5. dedup: 同一 run 内タイトル重複 + DB 既存 gate_decision 済候補との重複
     (skip_too_new のみ再判定: 開始日更新後に再評価)
  6. max_items_per_run 上限まで各商品:
     scrape_product_detail → evaluate_sourcing_gate → insert_research_candidate + save_gate_decision
  7. navigate 1 回ごとに api_call_log へ記録 (provider='terapeak', model='cdp', operation='terapeak_read')
  8. Discord 通知 (パターン別収穫件数 / gate 通過・却下件数 / クォータ消費 / エラー)
  9. 失敗時も必ず success=False + Discord (Q0 偽装成功禁止)

Q0 silent skip 防止:
  - enabled=false → log + Discord 通知
  - クォータ不足 → log + Discord 通知 (縮退処理)
  - anti-bot 連続失敗 → 即停止 + Discord 通知
  - 各 skip / 失敗 / 縮退に必ず痕跡を残す

SQLite TIMESTAMP は UTC. JST 当日集計は `DATE(called_at, '+9 hours') = DATE('now', '+9 hours')` で行う.
SKU 規約: research_candidates は ebay_item_id を持たない独立 entity。重複排除キーは harvest_keyword のみ。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# CDP Check & env load
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://localhost:9222"
_PROVIDER = "terapeak"
_MODEL = "cdp"
_OPERATION = "terapeak_read"
# anti-bot 連続失敗でバッチ停止する閾値 (market_analysis_refresh と同じ)
_STOP_ON_CONSECUTIVE_FAILURES = 5
# Terapeak クォータ (1 日上限 250 navigate, market_analysis と共有)
_DAILY_QUOTA = 250

# モジュールレベルインポート (テストでパッチ可能にするため)
from monitor.terapeak_scraper import harvest_product_list, HarvestedProduct, scrape_product_detail  # noqa: E402
from monitor.research_gate import (  # noqa: E402
    evaluate_sourcing_gate,
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
)
from monitor.research_candidates_db import (  # noqa: E402
    insert_research_candidate,
    save_gate_decision,
    update_status,
    STATUS_HARVESTED,
    STATUS_GATE_PASSED,
    STATUS_GATE_REJECTED,
    STATUS_NEEDS_REVIEW,
)


def _check_cdp_available() -> bool:
    """CDP endpoint (port 9222) が応答するか確認."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", 9222))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _get_today_terapeak_quota_used() -> int:
    """JST 当日の api_call_log.terapeak_read 消費件数を集計.

    api_call_log.called_at は SQL CURRENT_TIMESTAMP = UTC で保存.
    sqlite-timezone.md 準拠: `DATE(called_at, '+9 hours') = DATE('now', '+9 hours')` で JST 当日.
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM api_call_log
               WHERE provider = ?
                 AND operation = ?
                 AND DATE(called_at, '+9 hours') = DATE('now', '+9 hours')""",
            (_PROVIDER, _OPERATION),
        ).fetchone()
    return int(row[0]) if row else 0


def _record_navigate(success: bool, error_message: Optional[str] = None) -> None:
    """Terapeak navigate 1 回を api_call_log に記録 (クォータ実体管理)."""
    from monitor.api_logger import log_api_call
    log_api_call(
        provider=_PROVIDER,
        model=_MODEL,
        operation=_OPERATION,
        input_tokens=0,
        output_tokens=0,
        success=success,
        error_message=error_message,
    )


def _send_discord(config: dict, message: str, severity: str = "info") -> bool:
    """Discord 通知ヘルパ.

    H-1: config['discord']['webhook_url'] は本番 json で空文字 (2026-05-25 に .env 移行済)。
    DiscordNotifier (env DISCORD_WEBHOOK_URL 優先読み) 経由に差し替えて確実に届けるよう修正。
    webhook が存在しない場合は logger.warning で必ず痕跡を残す (silent skip 禁止 Q0)。
    """
    from notifiers.discord_notifier import DiscordNotifier
    # config の webhook を fallback として渡す (空でも DiscordNotifier が env から読む)
    config_webhook = (config or {}).get("discord", {}).get("webhook_url") or ""
    notifier = DiscordNotifier(config_webhook)
    if not notifier.webhook_url:
        logger.warning("research_harvest: Discord webhook 未設定 — 通知 skip")
        return False
    color = {"info": 0x3399FF, "warn": 0xC89B2A, "error": 0xD84C38}.get(severity, 0x3399FF)
    try:
        embed = {
            "title": "W229 商品リサーチ発掘 (03:30)",
            "description": message,
            "color": color,
            "timestamp": datetime.now().isoformat(),
        }
        return notifier.send_message("", embed=embed)
    except Exception as e:
        logger.warning(f"Discord 送信失敗: {e}")
        return False


def _normalize_keyword(title: str) -> str:
    """タイトルを重複排除キーに正規化 (小文字 + 連続空白を単一スペース)."""
    return re.sub(r"\s+", " ", title.lower().strip())


def _get_existing_gate_decisions(normalized_keywords: list[str]) -> dict[str, dict]:
    """DB 既存の research_candidates を keyword → row dict で返す.

    source='terapeak_harvest' かつ harvest_keyword が一致する行を対象.
    skip_too_new は再判定対象なので gate_decision='skip_too_new' も返す。
    gate_decision=NULL (needs_review/scrape 失敗残骸) も含む — 呼出側で
    再 scrape 対象に分岐し、重複 INSERT を防ぐ (2026-06-10 レビュー MEDIUM-8)。

    Returns:
        {normalized_keyword: row_dict}  (NULL gate_decision 行も含む)
    """
    if not normalized_keywords:
        return {}
    from monitor.database import get_conn
    # SQLite の IN 句に動的バインド
    placeholders = ",".join("?" * len(normalized_keywords))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT rc_id, harvest_keyword, gate_decision, gate_reason,
                       gate_inputs_json, status
                FROM research_candidates
                WHERE source = 'terapeak_harvest'
                  AND harvest_keyword IN ({placeholders})
                ORDER BY created_at DESC""",
            normalized_keywords,
        ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        kw = row["harvest_keyword"]
        if kw and kw not in result:  # 最新行のみ保持 (ORDER BY created_at DESC)
            result[kw] = dict(row)
    return result


def run_research_harvest(config: Optional[dict] = None) -> dict:
    """W229 ハーベストバッチ本体.

    Returns:
        {
            'success': bool,
            'harvested_fresh': int,
            'harvested_echo': int,
            'gate_passed': int,
            'gate_rejected': int,
            'quota_used_this_run': int,
            'skipped_dedup': int,
            'errors': list[str],
            'message': str,
        }
    """
    cfg = config or {}
    started_at = datetime.now()
    result: dict = {
        "success": False,
        "harvested_fresh": 0,
        "harvested_echo": 0,
        "gate_passed": 0,
        "gate_rejected": 0,
        "quota_used_this_run": 0,
        "skipped_dedup": 0,
        "errors": [],
        "message": "",
    }

    # ── 1. config 確認 ────────────────────────────────────────────────
    harvest_cfg = (cfg.get("tasks_enabled") or {}).get("research_harvest") or {}
    if not harvest_cfg.get("enabled", False):
        msg = "research_harvest: enabled=false → skip (設計書 §6-2, Q0)"
        logger.info(msg)
        try:
            from daily_scheduler import _batch_ctx
            from monitor.task_execution_log import log_task_skip
            _bid = _batch_ctx.get("id")
            _bhr = _batch_ctx.get("hour")
            if _bid is not None and _bhr is not None:
                log_task_skip(
                    task_key="research_harvest",
                    display_name="research_harvest",
                    batch_id=_bid,
                    batch_hour=int(_bhr),
                    reason="disabled_by_config",
                    skip_kind="skip_disabled",
                )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"research_harvest: log_task_skip 失敗: {_le}")
        result["message"] = msg
        result["success"] = True  # skip は正常終了 (偽装成功ではない)
        return result

    seed_queries: list[dict] = harvest_cfg.get("seed_queries") or []
    max_items_per_run: int = int(harvest_cfg.get("max_items_per_run", 50))
    max_pages: int = int(harvest_cfg.get("max_pages", 2))

    if not seed_queries:
        msg = "research_harvest: seed_queries が空 → skip"
        logger.warning(msg)
        _send_discord(cfg, msg, severity="warn")
        result["message"] = msg
        result["success"] = True
        return result

    # ── 2. CDP 疎通確認 ──────────────────────────────────────────────
    if not _check_cdp_available():
        msg = (
            "research_harvest: CDP Chrome が起動していません (port 9222).\n"
            "scripts/start_chrome_cdp.bat を実行 → eBay ログイン後に再試行してください."
        )
        logger.error(msg)
        _send_discord(cfg, msg, severity="error")
        result["message"] = msg
        return result

    # ── 3. クォータチェック ────────────────────────────────────────────
    quota_used_before = _get_today_terapeak_quota_used()
    quota_remaining = _DAILY_QUOTA - quota_used_before
    logger.info(
        f"research_harvest: クォータ確認 used={quota_used_before} "
        f"remaining={quota_remaining} limit={_DAILY_QUOTA}"
    )

    if quota_remaining <= 0:
        msg = (
            f"research_harvest: クォータ上限到達 "
            f"(今日消費={quota_used_before}/{_DAILY_QUOTA}) → skip\n"
            "market_analysis と共有クォータです。翌日 03:30 に再試行します。"
        )
        logger.warning(msg)
        _send_discord(cfg, msg, severity="warn")
        result["message"] = msg
        result["success"] = True  # skip は正常終了
        return result

    # クォータが少ない場合は max_items を縮退
    # 1 商品あたり最大 3 navigate (90d + ACTIVE + 730d), seed × 2 パターンで 1 navigate/page
    # 保守的見積もり: 1 商品 = 3 navigate + 1 harvest navigate = 4 (上限側)
    # 縮退しきい値: remaining < 20 なら最小 5 件
    effective_max_items = max_items_per_run
    if quota_remaining < 20:
        effective_max_items = min(5, max_items_per_run)
        msg = (
            f"research_harvest: クォータ残量が少ない ({quota_remaining} remaining) "
            f"→ 縮退: max_items={effective_max_items}"
        )
        logger.warning(msg)
        _send_discord(cfg, msg, severity="warn")

    # ── 4. ハーベスト ──────────────────────────────────────────────
    patterns = ["fresh_24h", "two_year_echo"]
    # fresh_24h 優先で合算 max_items_per_run まで詰める (設計書 §5-0 Q10)
    fresh_products: list[HarvestedProduct] = []
    echo_products: list[HarvestedProduct] = []

    for seed in seed_queries:
        keyword: str = seed.get("query", "")
        category_id: int = int(seed.get("category_id", 0))
        min_price: int = int(seed.get("min_price", 100))

        if not keyword.strip():
            logger.warning(f"research_harvest: seed_query の query が空 → skip: {seed}")
            continue

        for pattern in patterns:
            logger.info(
                f"research_harvest: harvest keyword={keyword!r} "
                f"pattern={pattern} category={category_id} min_price={min_price}"
            )
            harvest_result = harvest_product_list(
                keyword,
                pattern,
                category_id=category_id,
                min_price=min_price,
                max_pages=max_pages,
            )
            # harvest 1 呼出 = 1〜max_pages navigate → pages_loaded 分だけ記録
            _pages = max(1, getattr(harvest_result, "pages_loaded", 1) or 1)
            for _p in range(_pages):
                _record_navigate(
                    success=harvest_result.success if _p == _pages - 1 else True,
                    error_message=harvest_result.error if _p == _pages - 1 else None,
                )
            result["quota_used_this_run"] += _pages

            if not harvest_result.success:
                err = f"harvest失敗 ({pattern}): {harvest_result.error}"
                logger.warning(err)
                result["errors"].append(err)
                # anti-bot 検知の場合は後続も失敗するため即停止 (eBay error redirect)
                if harvest_result.error and "error redirect" in (harvest_result.error or ""):
                    msg = f"research_harvest: eBay anti-bot 検知 → 即停止\n{err}"
                    logger.error(msg)
                    _send_discord(cfg, msg, severity="error")
                    result["message"] = msg
                    return result
            else:
                if pattern == "fresh_24h":
                    fresh_products.extend(harvest_result.products)
                    result["harvested_fresh"] += len(harvest_result.products)
                else:
                    echo_products.extend(harvest_result.products)
                    result["harvested_echo"] += len(harvest_result.products)

    # fresh_24h 優先で合算: 残枠に two_year_echo を追加
    combined: list[HarvestedProduct] = list(fresh_products)
    remaining_slots = max_items_per_run - len(combined)
    if remaining_slots > 0:
        combined.extend(echo_products[:remaining_slots])

    logger.info(
        f"research_harvest: 収穫合計 fresh={len(fresh_products)} "
        f"echo={len(echo_products)} combined={len(combined)}"
    )

    if not combined:
        msg = (
            f"research_harvest: 収穫 0 件 (fresh_24h={len(fresh_products)}, "
            f"two_year_echo={len(echo_products)}). "
            f"クォータ消費={result['quota_used_this_run']}"
        )
        logger.info(msg)
        _send_discord(cfg, msg, severity="info")
        result["message"] = msg
        result["success"] = True
        return result

    # ── 5. dedup: 同一 run 内タイトル重複 ─────────────────────────────
    seen_in_run: set[str] = set()
    deduped: list[HarvestedProduct] = []
    for prod in combined:
        nk = _normalize_keyword(prod.title)
        if nk in seen_in_run:
            result["skipped_dedup"] += 1
            continue
        seen_in_run.add(nk)
        deduped.append(prod)

    # dedup: DB 既存 gate_decision 済との重複チェック
    all_nkws = [_normalize_keyword(p.title) for p in deduped]
    existing_map = _get_existing_gate_decisions(all_nkws)

    # skip_too_new / NULL (needs_review) 以外は skip (gate_decision 確定済)
    to_process: list[tuple[HarvestedProduct, str]] = []  # (product, normalized_keyword)
    for prod in deduped:
        nk = _normalize_keyword(prod.title)
        existing = existing_map.get(nk)
        if existing is None:
            # 新規
            to_process.append((prod, nk))
        elif existing.get("gate_decision") is None:
            # gate_decision=NULL (needs_review / scrape 失敗残骸) → 再 scrape 対象
            logger.info(
                f"research_harvest: needs_review 再試行 title={prod.title[:60]!r} "
                f"rc_id={existing['rc_id']}"
            )
            to_process.append((prod, nk))
        elif existing.get("gate_decision") == "skip_too_new":
            # 再判定対象 (仕様書 §3-4: skip_too_new は再出現時に再判定)
            logger.info(
                f"research_harvest: skip_too_new 再判定 title={prod.title[:60]!r} "
                f"rc_id={existing['rc_id']}"
            )
            to_process.append((prod, nk))
        else:
            # gate_decision 確定済 → skip
            result["skipped_dedup"] += 1
            logger.debug(
                f"research_harvest: dedup skip (gate確定済={existing['gate_decision']}) "
                f"title={prod.title[:60]!r}"
            )

    logger.info(
        f"research_harvest: dedup後 処理対象={len(to_process)} "
        f"skip_dedup={result['skipped_dedup']}"
    )

    # ── 6. 各商品: scrape_product_detail → evaluate_sourcing_gate → DB着地 ──

    consecutive_failures = 0
    # C-2: anti-bot break 検知フラグ。break 後も success=True 上書きしないように制御する。
    aborted = False

    for _item_idx, (prod, nk) in enumerate(to_process[:effective_max_items]):
        # H-2(c): 10 件毎にクォータを再確認して超過なら中断 (Q0 痕跡あり)。
        if _item_idx > 0 and _item_idx % 10 == 0:
            _quota_now = _get_today_terapeak_quota_used()
            if _quota_now >= _DAILY_QUOTA:
                _qmsg = (
                    f"research_harvest: run 中クォータ到達 "
                    f"(used={_quota_now}/{_DAILY_QUOTA}) → 残 {len(to_process[:effective_max_items]) - _item_idx} 件を中断"
                )
                logger.warning(_qmsg)
                _send_discord(cfg, _qmsg, severity="warn")
                result["errors"].append(_qmsg)
                break

        logger.info(f"research_harvest: 処理中 title={prod.title[:60]!r}")

        # scrape_product_detail
        try:
            gate_data = scrape_product_detail(prod.title)
        except Exception as e:
            err = f"scrape_product_detail 例外: {e}"
            logger.error(err)
            result["errors"].append(f"{prod.title[:40]}: {err}")
            consecutive_failures += 1
            # navigate 記録 (失敗)
            _record_navigate(success=False, error_message=str(e))
            result["quota_used_this_run"] += 1
            if consecutive_failures >= _STOP_ON_CONSECUTIVE_FAILURES:
                msg = (
                    f"research_harvest: 連続 {consecutive_failures} 件失敗 → anti-bot 停止\n"
                    f"eBay scrape 連続失敗. 残件は翌日 03:30 に再試行."
                )
                logger.error(msg)
                _send_discord(cfg, msg, severity="error")
                # C-2: aborted=True でフラグを立てる。result["message"] は上書きしない。
                aborted = True
                result["success"] = False
                result["message"] = msg
                break
            continue

        # H-2: navigate 記録は実消費回数分 (navigates_used) を計上する。
        # Q6 skip 時 = 1, フル経路 = 3, 途中失敗 = その時点まで。
        # 旧実装は一律 1 カウントで最大 1/3 の過小評価があった。
        nav_count = getattr(gate_data, "navigates_used", 1) or 1
        for _i in range(nav_count):
            _record_navigate(
                success=gate_data.success if _i == nav_count - 1 else True,
                error_message=gate_data.error if _i == nav_count - 1 else None,
            )
        result["quota_used_this_run"] += nav_count

        if not gate_data.success:
            err = f"scrape失敗 ({prod.title[:40]}): {gate_data.error}"
            logger.warning(err)
            result["errors"].append(err)
            consecutive_failures += 1

            if consecutive_failures >= _STOP_ON_CONSECUTIVE_FAILURES:
                msg = (
                    f"research_harvest: 連続 {consecutive_failures} 件失敗 → anti-bot 停止\n"
                    f"eBay scrape 連続失敗. 残件は翌日 03:30 に再試行."
                )
                logger.error(msg)
                _send_discord(cfg, msg, severity="error")
                # C-2: aborted=True でフラグを立てる。result["message"] は上書きしない。
                aborted = True
                result["success"] = False
                result["message"] = msg
                break

            # 技術失敗 = needs_review で DB に残す (設計書 §4-3 P2)
            # MEDIUM: 既存行 (同 harvest_keyword) があれば再利用して重複 INSERT を防ぐ。
            try:
                existing_nr = existing_map.get(nk)
                if existing_nr is not None:
                    # 既存行: needs_review への遷移可否を確認してから遷移
                    _old_status = existing_nr.get("status", "")
                    from monitor.research_candidates_db import can_transition
                    if can_transition(_old_status, STATUS_NEEDS_REVIEW):
                        rc_id_nr = int(existing_nr["rc_id"])
                        _update_harvest_meta(rc_id_nr, nk, prod)
                        update_status(
                            rc_id_nr,
                            STATUS_NEEDS_REVIEW,
                            needs_review_reason=f"scrape失敗: {gate_data.error}",
                        )
                    else:
                        logger.warning(
                            f"research_harvest: needs_review 遷移不可 "
                            f"(status={_old_status}) title={prod.title[:40]!r} — log のみ"
                        )
                else:
                    rc_id_nr = insert_research_candidate(
                        prod.title,
                        terapeak_avg_price_usd=prod.avg_sold_price_usd,
                        harvest_pattern=_get_harvest_pattern(prod, fresh_products),
                    )
                    _update_harvest_meta(rc_id_nr, nk, prod)
                    update_status(
                        rc_id_nr,
                        STATUS_NEEDS_REVIEW,
                        needs_review_reason=f"scrape失敗: {gate_data.error}",
                    )
            except Exception as db_err:
                logger.error(f"research_harvest: needs_review 保存失敗: {db_err}")
            continue

        consecutive_failures = 0  # 成功でリセット

        # evaluate_sourcing_gate
        # 依頼ボード#23 (2026-06-15): 全世界グラット除外シグナルも渡す
        # (target_oos_watch 予定の候補のみ scrape 済、それ以外は -1=未取得)。
        decision, reason = evaluate_sourcing_gate(
            sold_90d=gate_data.sold_90d,
            has_active_listing=gate_data.has_active_listing,
            listing_start_date=gate_data.listing_start_date,
            sold_1_2yr=gate_data.sold_1_2yr,
            worldwide_active_count=gate_data.worldwide_active_count,
            worldwide_sold_90d=gate_data.worldwide_sold_90d,
        )
        inputs_dict = {
            "sold_90d": gate_data.sold_90d,
            "has_active_listing": gate_data.has_active_listing,
            "listing_start_date": gate_data.listing_start_date,
            "sold_1_2yr": gate_data.sold_1_2yr,
            "avg_sold_price_usd": gate_data.avg_sold_price_usd,
            "worldwide_active_count": gate_data.worldwide_active_count,
            "worldwide_sold_90d": gate_data.worldwide_sold_90d,
        }

        logger.info(
            f"research_harvest: ゲート判定 decision={decision} "
            f"title={prod.title[:50]!r}"
        )

        # DB 着地: 既存 skip_too_new の rc_id を再利用、それ以外は新規 INSERT
        existing = existing_map.get(nk)
        harvest_pattern_val = _get_harvest_pattern(prod, fresh_products)

        try:
            if existing and (
                existing.get("gate_decision") == "skip_too_new"
                or existing.get("gate_decision") is None  # needs_review (NULL) 行も再利用
            ):
                # 再判定: 既存行の gate_* を更新し STATUS_HARVESTED から再遷移
                rc_id = int(existing["rc_id"])
                _update_harvest_meta(rc_id, nk, prod)
                # gate_rejected (skip_too_new) / needs_review → harvested へ戻してから再遷移
                # _ALLOWED_TRANSITIONS で合法な遷移のみ実行
                from monitor.research_candidates_db import can_transition
                _cur_status = existing.get("status", "")
                if can_transition(_cur_status, STATUS_HARVESTED):
                    update_status(rc_id, STATUS_HARVESTED)
                else:
                    logger.debug(
                        f"research_harvest: {_cur_status!r}→harvested 遷移不可 "
                        f"(rc_id={rc_id}) — gate 判定のみ実行"
                    )
            else:
                # C-1: 新規 INSERT 経路では update_status(rc_id, STATUS_HARVESTED) を呼ばない。
                # _ALLOWED_TRANSITIONS[STATUS_NEW] に STATUS_HARVESTED が含まれないため
                # ValueError → except 吸収 → save_gate_decision 未到達 → 行が 'new' のまま
                # + 翌晩重複 INSERT 蓄積という問題を防ぐ。
                # save_gate_decision(move_status=True) が new → gate_passed/gate_rejected を
                # 直接実行する (許可済遷移: _ALLOWED_TRANSITIONS[STATUS_NEW] に両方含まれる)。
                rc_id = insert_research_candidate(
                    prod.title,
                    terapeak_avg_price_usd=prod.avg_sold_price_usd,
                    harvest_pattern=harvest_pattern_val,
                )
                _update_harvest_meta(rc_id, nk, prod)

            # gate 判定を保存し status も遷移
            save_gate_decision(
                rc_id=rc_id,
                decision=decision,
                reason=reason,
                inputs_dict=inputs_dict,
                move_status=True,  # target_* → gate_passed / それ以外 → gate_rejected
            )

            if decision in {DECISION_TARGET_INSTOCK, DECISION_TARGET_OOS_WATCH}:
                result["gate_passed"] += 1
            else:
                result["gate_rejected"] += 1

        except Exception as db_err:
            err = f"DB着地失敗 ({prod.title[:40]}): {db_err}"
            logger.error(err)
            result["errors"].append(err)

    # ── 7. Discord 通知 ────────────────────────────────────────────
    duration_sec = (datetime.now() - started_at).total_seconds()
    total_harvested = result["harvested_fresh"] + result["harvested_echo"]
    error_count = len(result["errors"])

    summary_lines = [
        f"収穫: {total_harvested} 件 "
        f"(fresh_24h={result['harvested_fresh']}, two_year_echo={result['harvested_echo']})",
        f"ゲート: 通過={result['gate_passed']} / 却下={result['gate_rejected']}",
        f"dedup skip: {result['skipped_dedup']} 件",
        f"クォータ消費 (今回): {result['quota_used_this_run']} navigate",
        f"所要時間: {duration_sec:.0f}秒",
    ]
    if error_count > 0:
        summary_lines.append(f"エラー: {error_count} 件")
        for e in result["errors"][:5]:
            summary_lines.append(f"  ・{e[:80]}")

    msg = "\n".join(summary_lines)
    severity = "error" if error_count > 0 and result["gate_passed"] == 0 else "info"
    _send_discord(cfg, msg, severity=severity)

    # C-2: aborted=True (anti-bot break) なら success=False を維持、message も上書きしない。
    if not aborted:
        result["success"] = True
        result["message"] = msg
    logger.info(f"research_harvest 完了: {msg}")
    return result


def _get_harvest_pattern(prod, fresh_products: list) -> str:
    """prod が fresh_products リストに含まれているか判定してパターン文字列を返す."""
    return "fresh_24h" if prod in fresh_products else "two_year_echo"


def _update_harvest_meta(rc_id: int, normalized_keyword: str, prod) -> None:
    """harvest 由来メタ列を research_candidates に書き込む.

    source / harvest_keyword / ebay_avg_sold_price_usd / ebay_total_sold / harvested_at
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            """UPDATE research_candidates
               SET source='terapeak_harvest',
                   harvest_keyword=?,
                   ebay_avg_sold_price_usd=?,
                   ebay_total_sold=?,
                   harvested_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
               WHERE rc_id=?""",
            (
                normalized_keyword,
                prod.avg_sold_price_usd,
                prod.total_sold_count,
                rc_id,
            ),
        )
