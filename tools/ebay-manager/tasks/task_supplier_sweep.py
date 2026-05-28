#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先候補 Pattern 2 バッチ（長期在庫切れ全件スイープ）

役割:
  - Pattern 1 async (inventory_check 成功時の daemon thread) が取りこぼした、
    あるいは新規追加ではない「既に長期在庫切れ」SKU を朝バッチで一括処理する
  - 毎朝 02:30 に発火（schedule_config.json の execution_times=[2] で制御）

処理フロー:
  1. ebay_listings から「source_out_of_stock_since が N 日以上前」の SKU を取得
  2. 直近 M 日以内に supplier_candidates が登録済みの SKU は除外（重複探索抑制）
  3. 先頭 K 件に制限（API コスト・実行時間の上限）
  4. 各 SKU で run_supplier_candidate_search() を順次実行
  5. SKU 間は sleep で rate limit

設定可能パラメータ（config.tasks_enabled.supplier_sweep）:
  oos_days_threshold: 在庫切れ継続日数の下限（デフォルト 3 日）
  skip_if_searched_within_days: 直近探索済みならスキップする期間（デフォルト 7 日）
  max_skus_per_run: 1 回で処理する最大 SKU 数（デフォルト 30）
  sleep_between_skus_sec: SKU 間のスリープ秒（デフォルト 3）
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import (  # noqa: E402
    get_conn, get_ebay_listing_by_item_id, add_supplier_candidate,
)
from tasks.task_supplier_candidate_search import (  # noqa: E402
    run_supplier_candidate_search,
    search_candidates_on_platform,
    _normalize_url,
    _estimate_profit_for_candidate,
    _get_threshold,
    MATCH_SCORE_THRESHOLD,
    ALT_LISTING_SCORE_THRESHOLD,
)
from calculator import (  # noqa: E402
    check_supplier_candidate_profitable, load_settings,
)
# W182 HIGH-2 fix (2026-05-28): module-level import で test monkeypatch 互換
from monitor.scrapers import check_candidate_availability  # noqa: E402

logger = logging.getLogger(__name__)


# Anthropic Batch API custom_id 制約: ^[a-zA-Z0-9_-]{1,64}$
# regex compliance pinned in tests/test_w94_custom_id_regex.py
def _build_batch_custom_id(ebay_item_id: str, platform: str, idx: int) -> str:
    """W94 batch API custom_id 構築 (Anthropic regex compliant)."""
    return f"{ebay_item_id}-{platform}-{idx}"


def _fetch_sweep_targets(
    oos_days_threshold: int,
    skip_if_searched_within_days: int,
    limit: int,
) -> list[tuple[str, str]]:
    """
    スイープ対象 (ebay_item_id, sku) ペアを返す (W75 4b: SKU 単独 → tuple 化).

    条件:
      - ebay_listings.source_out_of_stock_since が threshold 日以上前
      - 直近 skip_if_searched_within_days 日以内に supplier_candidates が無い
      - qty=0 (復活候補) を優先、次に qty>=1 (置換候補)
      - 同一 qty 内では古い在庫切れから優先

    2026-04-24 修正: 旧 SQL は `l.quantity_ebay >= 1` で qty=0 を除外していたため、
    販売停止済商品 (qty=0 + 仕入先OOS) の復活候補が 0 件になっていた (164件 stuck)。
    qty=0 は user にとって最優先の復活対象であり、除外すべきではない。
    source_status='在庫無' の絞り込みは維持 (SKU書換直後 'unknown' や
    復活 '在庫有' listing の誤掃引を防ぐ HIGH-2 対策)。

    2026-05-01 W75 4b: ebay_item_id を併せて取得 (run_supplier_candidate_search が
    listing 識別 canonical key として ebay_item_id を要求するため). sc.sku=l.sku の
    重複検出 JOIN は本 sweep 範囲では集合 filter (LIKE 'ebay%') 同等の挙動 = 残置.
    """
    # FINDING 1 (2026-05-05): sku GLOB 'ebay*' で stock prefix 除外 (case-sensitive).
    # NOT EXISTS 重複検出も sc.ebay_item_id 単位に変更 (= SKU rule 違反予防、
    # 同 SKU 兄弟 listing が探索済時に残り全部 skip される silent 抜けの解消).
    # W100 (2026-05-06): yahoo_grace_until > now の listing は探索 skip
    # (ヤフオク終了 + 24h 後の再出品慣行を待つ).
    sql = """
        SELECT l.ebay_item_id, l.sku
        FROM ebay_listings l
        WHERE l.source_out_of_stock_since IS NOT NULL
          AND l.source_out_of_stock_since <= datetime('now', ? )
          AND l.source_status = '在庫無'
          AND (l.is_ended IS NULL OR l.is_ended=0)
          AND l.sku GLOB 'ebay*'
          AND (l.yahoo_grace_until IS NULL OR l.yahoo_grace_until <= datetime('now'))
          AND NOT EXISTS (
              SELECT 1 FROM supplier_candidates sc
              WHERE sc.ebay_item_id = l.ebay_item_id
                AND sc.created_at >= datetime('now', ? )
          )
        ORDER BY
          CASE WHEN l.quantity_ebay = 0 THEN 0 ELSE 1 END,
          l.source_out_of_stock_since ASC
        LIMIT ?
    """
    oos_clause = f"-{oos_days_threshold} days"
    skip_clause = f"-{skip_if_searched_within_days} days"

    with get_conn() as conn:
        rows = conn.execute(sql, (oos_clause, skip_clause, limit)).fetchall()
    return [(r["ebay_item_id"], r["sku"]) for r in rows
            if r["ebay_item_id"] and r["sku"]]


def run_supplier_sweep(config: dict) -> dict:
    """
    朝バッチ: 長期在庫切れ SKU の仕入先候補を一括探索。

    W94 (2026-05-02): config.tasks_enabled.supplier_sweep.use_batch_api=True で
    Anthropic Batch API + 1h Cache stack 経路に dispatch (50% off + ~10% cache 削減).
    default False (= 既存 realtime API 経路維持). batch 経路の詳細は
    `run_supplier_sweep_batch` docstring 参照.

    Returns:
      {'success', 'processed', 'candidates_found', 'errors', 'message'}
    """
    task_cfg = (config or {}).get("tasks_enabled", {}).get("supplier_sweep") or {}

    # W94: batch 経路への分岐 (default OFF で safe rollout)
    if task_cfg.get("use_batch_api", False):
        return run_supplier_sweep_batch(config)

    oos_days = int(task_cfg.get("oos_days_threshold", 3))
    skip_days = int(task_cfg.get("skip_if_searched_within_days", 7))
    max_skus = int(task_cfg.get("max_skus_per_run", 30))
    sleep_sec = float(task_cfg.get("sleep_between_skus_sec", 3))

    targets = _fetch_sweep_targets(oos_days, skip_days, max_skus)
    logger.info(
        f"仕入先候補スイープ対象: {len(targets)}件 "
        f"(oos>={oos_days}日 / skip_if_searched<={skip_days}日 / max={max_skus})"
    )

    if not targets:
        return {
            "success": True,
            "processed": 0,
            "candidates_found": 0,
            "errors": 0,
            "message": "スイープ対象なし",
        }

    processed = 0
    total_found = 0
    errors = 0

    for idx, (eid, sku) in enumerate(targets, start=1):
        logger.info(f"  [{idx}/{len(targets)}] item={eid} sku={sku} を探索中...")
        try:
            r = run_supplier_candidate_search(
                ebay_item_id=eid,
                sku=sku,
                config=config,
                discovered_via="pattern_2_batch",
            )
            processed += 1
            total_found += int(r.get("found") or 0)
            if not r.get("success"):
                errors += 1
                logger.warning(f"    item={eid} sku={sku}: {r.get('message')}")
        except Exception as e:
            errors += 1
            logger.error(f"    item={eid} sku={sku}: 例外発生 {e}", exc_info=True)

        if idx < len(targets) and sleep_sec > 0:
            time.sleep(sleep_sec)

    msg = (
        f"{processed}件処理 / 候補{total_found}件検出 / エラー{errors}件"
    )
    logger.info(f"仕入先候補スイープ完了: {msg}")
    return {
        "success": errors < processed,  # 全件エラーでなければ成功扱い
        "processed": processed,
        "candidates_found": total_found,
        "errors": errors,
        "message": msg,
    }


def run_supplier_sweep_batch(config: dict) -> dict:
    """W94: Anthropic Batch API + 1h Prompt Cache stack 経由の sweep 経路.

    既存 `run_supplier_sweep` (realtime API per-listing for-loop) と同じ output
    schema を返すが、内部で:
      1. 全 target を sweep_target SQL から抽出
      2. 全 target × 全 platform を Playwright スクレイプ (NOT batch 化対象)
      3. 全 (eid, hit) ペアを BatchItem に詰めて 1 batch で submit
      4. batch result を eid 単位に group → 既存 persist 経路と同じ filter + DB write
      5. min_batch_size 未満は realtime fallback (overhead で割引が逆ザヤするため)

    config.tasks_enabled.supplier_sweep:
      - use_batch_api: True で本関数を呼ぶ (default False)
      - min_batch_size: batch 化最小件数 (default 10)
      - oos_days_threshold / skip_if_searched_within_days / max_skus_per_run: 既存と同じ

    config.tasks_enabled.supplier_eval_batch (W94 専用):
      - poll_interval_sec: poll 間隔 (default 60)
      - hard_timeout_sec: SLA 超過 sentinel (default 14400 = 4h)

    Returns: {'success', 'processed', 'candidates_found', 'errors', 'message',
              'batch_id', 'batch_dlq': N, 'batch_fallback': N, 'cache_read_total': N}
    """
    from monitor.supplier_batch_evaluator import (
        BatchItem, evaluate_batch, DEFAULT_MIN_BATCH_SIZE,
    )
    from monitor.claude_evaluator import CLAUDE_MODEL as _eval_model

    task_cfg = (config or {}).get("tasks_enabled", {}).get("supplier_sweep") or {}
    eval_batch_cfg = (config or {}).get("tasks_enabled", {}).get("supplier_eval_batch") or {}

    oos_days = int(task_cfg.get("oos_days_threshold", 3))
    skip_days = int(task_cfg.get("skip_if_searched_within_days", 7))
    max_skus = int(task_cfg.get("max_skus_per_run", 30))
    min_batch = int(task_cfg.get("min_batch_size", DEFAULT_MIN_BATCH_SIZE))
    platforms = ["mercari", "yahoo_auctions", "paypay_furima"]

    targets = _fetch_sweep_targets(oos_days, skip_days, max_skus)
    logger.info(
        f"[W94 batch] sweep 対象: {len(targets)}件 "
        f"(oos>={oos_days}日 / skip<={skip_days}日 / max={max_skus} / min_batch={min_batch})"
    )

    if not targets:
        return {
            "success": True, "processed": 0, "candidates_found": 0, "errors": 0,
            "message": "スイープ対象なし", "batch_id": "", "batch_dlq": 0,
            "batch_fallback": 0, "cache_read_total": 0,
        }

    # H-6 (2026-05-02 code-reviewer): KB import を loop 外で 1 回に hoist.
    # 旧: 30 listing × 3 platform × N hit = 数百回 import 試行.
    try:
        from monitor.knowledge_lookup import (
            find_related_knowledge, format_knowledge_for_prompt,
        )
    except Exception:
        find_related_knowledge = None
        format_knowledge_for_prompt = None

    # Phase 1: 全 target × 全 platform スクレイプ + BatchItem build (Playwright batch 化対象外)
    listing_by_eid: dict[str, dict] = {}
    items_by_eid: dict[str, list] = {}  # eid -> list[(custom_id, hit)]
    batch_items: list[BatchItem] = []
    excluded_self = 0
    # W182 (2026-05-28): 在庫 gate (sold_out / not_found を AI 評価前に除外)
    # PayPay 検索 API が sold_out を返す bug の恒久対策 (Codex 2026-05-28 調査).
    # check_candidate_availability は module-level import (HIGH-2 fix、monkeypatch 互換).
    excluded_unavailable = 0
    url_avail_map: dict[str, dict] = {}
    scrape_errors = 0

    for eid, sku in targets:
        listing = get_ebay_listing_by_item_id(eid)
        if not listing:
            logger.warning(f"[W94 batch] listing not found for ebay_item_id={eid}")
            scrape_errors += 1
            continue
        listing_by_eid[eid] = listing
        ebay_title = listing.get("title") or ""
        ebay_image_url = listing.get("ebay_image_url")  # W9 後の image url、無ければ None
        listing_url_norm = _normalize_url(listing.get("source_url") or "")
        items_by_eid[eid] = []

        for plat in platforms:
            try:
                hits = search_candidates_on_platform(plat, ebay_title)
            except Exception as e:
                logger.warning(f"[W94 batch] scrape failed eid={eid} plat={plat}: {e}")
                scrape_errors += 1
                continue
            for idx, h in enumerate(hits):
                if listing_url_norm and _normalize_url(h.url) == listing_url_norm:
                    excluded_self += 1
                    continue
                # W182: 在庫 gate (sold_out / not_found は Claude Batch 評価前に reject、コスト削減)
                # 同 batch 内で同 URL を複数 hit する場合は cache (重複 fetch 削減).
                if h.url in url_avail_map:
                    _avail = url_avail_map[h.url]
                else:
                    _avail = check_candidate_availability(h.url)
                    url_avail_map[h.url] = _avail
                if _avail.get('status') in ('unavailable', 'not_found'):
                    excluded_unavailable += 1
                    logger.info(
                        f"[W94 batch] W182 skip {_avail.get('status')}: "
                        f"eid={eid} url={h.url} signal={_avail.get('signal')}"
                    )
                    continue
                # KB 注入 (動画学習で関連知識があれば prompt に追加)
                kb_text = ""
                if find_related_knowledge and format_knowledge_for_prompt:
                    try:
                        related = find_related_knowledge(
                            f"{ebay_title} {h.title or ''}", max_videos=2,
                        )
                        if related:
                            kb_text = "\n\n" + format_knowledge_for_prompt(
                                related, max_chars=1500,
                            )
                    except Exception as e:
                        logger.debug(f"[W94 batch] KB skip eid={eid}: {e}")

                custom_id = _build_batch_custom_id(eid, plat, idx)
                bi = BatchItem(
                    custom_id=custom_id,
                    ebay_title=ebay_title,
                    candidate_title=h.title or "",
                    platform=h.source_platform,
                    price_jpy=h.price_jpy,
                    url=h.url,
                    ebay_image_url=ebay_image_url,
                    candidate_image_url=h.image_url,
                    sku=sku,
                    ebay_item_id=eid,
                    knowledge_block=kb_text,
                )
                batch_items.append(bi)
                items_by_eid[eid].append((custom_id, h))

    if not batch_items:
        logger.info("[W94 batch] スクレイプ結果ゼロ件、batch submit せず終了")
        return {
            "success": True, "processed": len(targets), "candidates_found": 0,
            "errors": scrape_errors, "message": "候補ゼロ件", "batch_id": "",
            "batch_dlq": 0, "batch_fallback": 0, "cache_read_total": 0,
        }

    # Phase 2: min_batch_size 未満は realtime fallback (Q0: 失敗を silent skip しない)
    if len(batch_items) < min_batch:
        logger.info(
            f"[W94 batch] {len(batch_items)} 件 < min_batch_size {min_batch}、"
            f"realtime API fallback で各 listing 処理"
        )
        return _run_supplier_sweep_realtime_fallback(targets, config)

    # Phase 3: batch submit
    poll_interval = int(eval_batch_cfg.get("poll_interval_sec", 60))
    hard_timeout = int(eval_batch_cfg.get("hard_timeout_sec", 4 * 3600))
    batch_result = evaluate_batch(
        batch_items, model=_eval_model,
        poll_interval_sec=poll_interval, hard_timeout_sec=hard_timeout,
    )

    # Phase 4: result を eid 単位に persist
    settings = load_settings()
    fx = float(settings.get("exchange_rate", 155.0))
    alt0_threshold = _get_threshold(settings, "supplier_alt0_score_threshold", MATCH_SCORE_THRESHOLD)
    alt1_threshold = _get_threshold(settings, "supplier_alt1_score_threshold", ALT_LISTING_SCORE_THRESHOLD)

    total_persisted = 0
    total_found = 0
    eid_persisted_count: dict[str, int] = {}

    for eid, listing in listing_by_eid.items():
        sku = listing.get("sku") or ""
        for custom_id, hit in items_by_eid.get(eid, []):
            eval_r = batch_result.results.get(custom_id)
            if eval_r is None or eval_r.error:
                # batch 経路で評価失敗 (DLQ 行き or fallback で復旧不能)
                continue
            total_found += 1
            # 既存 persist filter (task_supplier_candidate_search.py L334-403 と同等)
            if eval_r.match_score < alt0_threshold and not eval_r.alt_listing_possible:
                continue
            if eval_r.alt_listing_possible and eval_r.match_score < alt1_threshold:
                continue

            profit_jpy: Optional[float] = None
            profitable = 0
            if hit.price_jpy is not None:
                profit_jpy = _estimate_profit_for_candidate(
                    listing=listing,
                    purchase_yen=hit.price_jpy,
                    settings=settings,
                )
                if profit_jpy is not None:
                    ok, _ = check_supplier_candidate_profitable(
                        profit_with_refund=profit_jpy,
                        purchase_yen=hit.price_jpy,
                    )
                    profitable = int(ok)
            if not eval_r.alt_listing_possible and not profitable:
                continue

            # W182: 在庫 gate で取得した availability を persist
            _w182_avail = url_avail_map.get(hit.url, {})
            row_id = add_supplier_candidate(
                sku=sku,
                candidate_url=hit.url,
                source_platform=hit.source_platform,
                candidate_price_jpy=hit.price_jpy,
                candidate_title=hit.title,
                match_score=eval_r.match_score,
                match_reasoning=eval_r.reasoning,
                profit_jpy=profit_jpy,
                profitable=profitable,
                ebay_item_id=eid,
                discovered_via="pattern_2_batch_w94",
                junk_likely_untested=int(eval_r.junk_likely_untested),
                alt_listing_possible=int(eval_r.alt_listing_possible),
                alt_listing_note=eval_r.alt_listing_note or None,
                eval_model=(
                    f"{_eval_model}-batch-fallback"
                    if custom_id in batch_result.fallback_custom_ids
                    else f"{_eval_model}-batch"
                ),
                availability_status=_w182_avail.get('status'),
                availability_checked_at=_w182_avail.get('checked_at'),
                availability_signal=_w182_avail.get('signal'),
            )
            if row_id:
                total_persisted += 1
                eid_persisted_count[eid] = eid_persisted_count.get(eid, 0) + 1

    msg = (
        f"W94 batch sweep: targets={len(targets)} batch_items={len(batch_items)} "
        f"persisted={total_persisted} fallback={batch_result.fallback_used} "
        f"dlq={batch_result.pending_dlq} cache_read={batch_result.cache_read_total}"
    )
    logger.info(msg)
    # H-3 (2026-05-02 code-reviewer): BatchResult.errored は既に fallback_used 控除済.
    # 二重控除しないよう scrape_errors + batch_result.errored のみ.
    # success = errored < submitted (= 何か残った件がある) AND timeout 無し AND DLQ ゼロ.
    success = (
        batch_result.errored < batch_result.submitted
        and not batch_result.timeout
        and batch_result.pending_dlq == 0
    )
    return {
        "success": success,
        "processed": len(targets),
        "candidates_found": total_found,
        "errors": scrape_errors + batch_result.errored,
        "message": msg,
        "batch_id": batch_result.batch_id,
        "batch_dlq": batch_result.pending_dlq,
        "batch_fallback": batch_result.fallback_used,
        "cache_read_total": batch_result.cache_read_total,
    }


def _run_supplier_sweep_realtime_fallback(
    targets: list[tuple[str, str]], config: dict,
) -> dict:
    """min_batch_size 未満時の realtime API fallback (Q0: silent skip 防止)."""
    processed = 0
    total_found = 0
    errors = 0
    for eid, sku in targets:
        try:
            r = run_supplier_candidate_search(
                ebay_item_id=eid, sku=sku, config=config,
                discovered_via="pattern_2_batch_w94_fallback",
            )
            processed += 1
            total_found += int(r.get("found") or 0)
            if not r.get("success"):
                errors += 1
        except Exception as e:
            errors += 1
            logger.error(f"[W94 fallback] eid={eid} sku={sku}: {e}")
    return {
        "success": errors < processed,
        "processed": processed,
        "candidates_found": total_found,
        "errors": errors,
        "message": f"W94 fallback realtime: {processed}件処理 / 候補{total_found}件 / err={errors}",
        "batch_id": "",
        "batch_dlq": 0,
        "batch_fallback": 0,
        "cache_read_total": 0,
    }


if __name__ == "__main__":
    # 手動テスト: python -m tasks.task_supplier_sweep
    import json
    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    r = run_supplier_sweep(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
