#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""リサーチ対戦アリーナ (W286) 夜間タスク — AI が当日セルで 5 品リサーチ + 凍結 snapshot.

設計書: .company/engineering/docs/2026-06-27-research-duel-arena-system-design-v2.md §1 Phase1。

フロー (前夜自動):
  1. 当日セル(6日ローテ)判定 = determine_cell (純関数、決定的)
  2. harvest_product_list で候補取得 → 凍結 evidence bundle を snapshot_json へ
     (Terapeak は絶対日付 pin 不可のため、AI が取得した item 集合自体を凍結 / Codex H1)
  3. research_duel_db.create_round (冪等)
  4. 上位 N 品を research_poc.evaluate_product で評価し research_candidates に着地(rc_id)
     (W288/W291/W290 修正済の sourcing エンジンをそのまま使う)
  5. research_duel_db.save_ai_picks (rc_id 参照の薄い台帳 → status ai_done)

既存資産再利用(K2): terapeak_scraper.harvest_product_list / research_poc.evaluate_product /
  research_duel_db / task_research_harvest._check_cdp_available。CDP(9222)必須。
Q0: enabled=false / CDP不在 / harvest失敗 は痕跡を残して skip (偽装成功にしない)。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 6 日サイクルの基準日 (この日を cycle index 0 とする決定的アンカー)。
_CYCLE_EPOCH = date(2026, 6, 29)
_AI_PICKS = 5  # AI が提示し、オーナーが採点する品数
# duel pattern → harvest pattern (terapeak_scraper の表現)
_HARVEST_PATTERN = {"new": "fresh_24h", "echo": "two_year_echo"}

# fresh (new) の sold 窓幅。terapeak_scraper.build_harvest_url の fresh_24h は
# dayRange=7 (= 直近 7 日) で harvest するため、表示窓も 7 日 (両端含む 6 日差) で揃える。
# これを変えるなら build_harvest_url 側の day_range=7 も同時に直すこと (単一真実源)。
_FRESH_WINDOW_DAYS = 7


def _two_year_target(today: date) -> date:
    """today から 2 年前のカレンダー日付 (2/29 → 2/28 丸め)。

    terapeak_scraper._two_year_target と同一規約 (echo harvest の起点日)。
    duel UI の期間表示と harvest 起点を一致させるためここでも定義する
    (scraper への循環 import を避ける薄い複製、丸め規約は同一)。
    """
    try:
        return today.replace(year=today.year - 2)
    except ValueError:
        return today.replace(year=today.year - 2, day=28)


def compute_duel_window(jst_date_iso: str, pattern: str) -> tuple[str, str]:
    """対戦の「凍結対象期間」の絶対日付 (start, end) を ISO 文字列で返す (純関数)。

    AI が実際に harvest した sold 窓と一致させる = UI の「固定期間（絶対・凍結）」表示の
    単一真実源 (user が朝のブラインドで同じ期間を再現できる必要がある)。

      - new  (fresh_24h):   直近 7 日窓 = [jst_date - 6日, jst_date]
                             (build_harvest_url: dayRange=7, endDate=now)
      - echo (two_year_echo): 2 年前の同じ週 = [target, target + 6日]
                             (build_harvest_url: startDate=target 00:00 から 730 日窓だが、
                              user が再現する対象は「2 年前の同じ頃」= target 起点の 7 日窓)

    Returns: (start_iso "YYYY-MM-DD", end_iso "YYYY-MM-DD")。jst_date_iso が不正なら ValueError。
    """
    base = date.fromisoformat(jst_date_iso[:10])
    span = timedelta(days=_FRESH_WINDOW_DAYS - 1)
    pat = (pattern or "").lower()
    if pat == "echo":
        start = _two_year_target(base)
        end = start + span
    else:  # new / fresh (既定)
        end = base
        start = base - span
    return start.isoformat(), end.isoformat()


def _default_cycle_categories() -> list[dict]:
    """duel の 3 カテゴリ (config 未指定時のフォールバック)。"""
    return [
        {"query": "measuring instrument", "category_id": 12576,
         "label": "Business & Industrial（計測器）"},
        {"query": "vintage computer", "category_id": 58058,
         "label": "Computers/Tablets & Networking"},
        {"query": "headphones", "category_id": 293,
         "label": "Consumer Electronics"},
    ]


def determine_cell(today: date, cycle_categories: list[dict]) -> dict:
    """6 日ローテで当日のセル (pattern, category) を決める (純関数・決定的).

    cycle index = (today - epoch).days % 6:
      0,1,2 → new (fresh)   × category[0,1,2]
      3,4,5 → echo (2年前)  × category[0,1,2]

    Returns: {"pattern": "new"|"echo", "category": dict, "cycle_index": int, "day_of_cycle": int}
    """
    if not cycle_categories:
        raise ValueError("cycle_categories is empty")
    n = len(cycle_categories)
    idx = (today - _CYCLE_EPOCH).days % 6
    pattern = "new" if idx < 3 else "echo"
    cat = cycle_categories[(idx % 3) % n]
    return {
        "pattern": pattern,
        "category": cat,
        "cycle_index": idx,
        "day_of_cycle": idx + 1,  # 1-6 (人間表示用)
    }


def _freeze_snapshot(
    products: list, cell: dict, jst_date_iso: str, max_items: int = 50
) -> str:
    """harvest 結果を凍結 evidence bundle (JSON) 化する.

    翌朝オーナーが「同じ盤面」を再現/参照するための item 集合を固定 (Codex H1/Fugu M1)。
    HarvestedProduct の属性差異に耐えるため getattr で防御的に拾う。
    window_start/window_end = harvest した sold 窓の絶対日付 (UI の期間表示の単一真実源)。
    """
    items = []
    for p in products[:max_items]:
        items.append({
            "title": getattr(p, "title", None),
            "price": getattr(p, "price", None) or getattr(p, "avg_price_usd", None),
            "url": getattr(p, "url", None) or getattr(p, "item_url", None),
        })
    window_start, window_end = compute_duel_window(jst_date_iso, cell["pattern"])
    bundle = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "pattern": cell["pattern"],
        "category": cell["category"],
        "cycle_index": cell["cycle_index"],
        "window_start": window_start,
        "window_end": window_end,
        "item_count": len(items),
        "items": items,
    }
    return json.dumps(bundle, ensure_ascii=False)


def _invalidate_stale_rounds(today: date) -> list[int]:
    """過去日付のまま ai_pending/ai_done で放置された round を invalidated へ遷移させる.

    round_id=4 (6/28, ai_done のまま放置) 型の「死んだラウンド」対策。新ラウンド開始の
    たびに呼び、状態機械の invalidated 終端へ寄せる (DB 直接 UPDATE はしない、状態機械の
    遷移検証 = research_duel_db.invalidate_round 経由 / Q2 スコープ外遵守)。
    1 件の遷移失敗で batch を止めない (Q0: 痕跡は logger.warning に残す)。
    """
    from monitor import research_duel_db as duel
    stale_statuses = {duel.STATUS_AI_PENDING, duel.STATUS_AI_DONE}
    today_iso = today.isoformat()
    invalidated: list[int] = []
    for rnd in duel.list_rounds(limit=60):
        if rnd.get("status") not in stale_statuses:
            continue
        if str(rnd.get("jst_date") or "")[:10] >= today_iso:
            continue
        rid = int(rnd["round_id"])
        try:
            if duel.invalidate_round(
                rid,
                reason=(
                    f"stale round (jst_date={rnd.get('jst_date')}, "
                    f"status={rnd.get('status')}) — 新ラウンド開始時に自動無効化"
                ),
            ):
                invalidated.append(rid)
        except ValueError as e:  # noqa: BLE001 — 1 件の失敗で batch を止めない (Q0 痕跡)
            logger.warning(
                "[research_duel] stale round invalidate 失敗 round_id=%s: %s", rid, e
            )
    if invalidated:
        logger.info("[research_duel] stale round 自動 invalidated: %s", invalidated)
    return invalidated


def run_research_duel(
    config: Optional[dict] = None, today: Optional[date] = None
) -> dict:
    """W286 夜間 duel バッチ本体. AI が当日セルで 5 品リサーチして duel_round に保存."""
    cfg = config or {}
    duel_cfg = (cfg.get("tasks_enabled") or {}).get("research_duel") or {}
    result: dict = {
        "success": False, "round_id": None, "ai_picks": 0,
        "cell": None, "errors": [], "message": "",
    }

    # ── enabled ガード (Q0: skip も痕跡) ──
    if not duel_cfg.get("enabled", False):
        result["success"] = True
        result["message"] = "research_duel: enabled=false → skip"
        logger.info(result["message"])
        return result

    today = today or date.today()
    cats = duel_cfg.get("cycle_categories") or _default_cycle_categories()
    cell = determine_cell(today, cats)
    pattern, cat = cell["pattern"], cell["category"]
    result["cell"] = f"Day{cell['day_of_cycle']}/6 {pattern}×{cat.get('label')}"
    logger.info("[research_duel] cell=%s", result["cell"])

    # ── 死んだラウンドの自動無効化 (過去日付のまま ai_pending/ai_done で放置) ──
    _invalidate_stale_rounds(today)

    # ── CDP 疎通 (harvest と同じ前提、再利用) ──
    from tasks.task_research_harvest import _check_cdp_available
    if not _check_cdp_available():
        result["message"] = (
            "research_duel: CDP Chrome 未起動 (port 9222) → skip。"
            "scripts/start_chrome_cdp.bat + eBay ログイン後に再試行。"
        )
        logger.error(result["message"])
        return result

    # ── harvest (1 セル分) ──
    from monitor.terapeak_scraper import harvest_product_list
    hv = harvest_product_list(
        cat.get("query", ""),
        _HARVEST_PATTERN[pattern],
        category_id=int(cat.get("category_id", 0) or 0),
        min_price=int(cat.get("min_price", 100)),
        max_pages=int(duel_cfg.get("max_pages", 1)),
    )
    if not getattr(hv, "success", False):
        result["errors"].append(f"harvest失敗: {getattr(hv, 'error', None)}")
        result["message"] = f"research_duel: harvest失敗 → skip ({getattr(hv, 'error', None)})"
        logger.warning(result["message"])
        return result

    products = list(getattr(hv, "products", None) or getattr(hv, "items", None) or [])
    if not products:
        result["success"] = True
        result["message"] = "research_duel: harvest 0 件 → round は作るが picks なし"
        logger.warning(result["message"])

    # ── round 作成 (冪等) + 凍結 snapshot ──
    from monitor import research_duel_db as duel
    round_id = duel.create_round(
        jst_date=today.isoformat(),
        pattern=pattern,
        category_id=int(cat.get("category_id", 0) or 0),
        category_label=cat.get("label"),
        snapshot_json=_freeze_snapshot(products, cell, today.isoformat()),
        prompt_version="duel-v1",
    )
    result["round_id"] = round_id

    # ── 上位 N 品を sourcing (research_poc.evaluate_product) → research_candidates ──
    # 重量推定 (g) は sourcing バッチの Haiku 4.5 ヘルパーをそのまま流用 (K2: 重複実装しない)。
    # 推定失敗時は None のまま渡す (Q0: 0 埋め禁止 → profit は evaluate_product 側で needs_review)。
    from monitor.research_poc import evaluate_product
    from tasks.task_research_sourcing import _estimate_weight
    picks: list[dict] = []
    for i, p in enumerate(products[:_AI_PICKS], start=1):
        title = getattr(p, "title", None)
        if not title or not title.strip():
            continue
        title = title.strip()
        tera_price = getattr(p, "avg_price_usd", None) or getattr(p, "price", None)
        # title → 発送重量(g) 推定。est は {"weight_g": float(g), ...} or None。
        est = _estimate_weight(title)
        manual_weight_g = est["weight_g"] if est else None
        rc_id = None
        try:
            ev = evaluate_product(
                title,
                terapeak_avg_price_usd=tera_price,
                manual_weight_g=manual_weight_g,  # g 単位 (evaluate_product の期待と一致)
            )
            rc_id = ev.get("rc_id")
        except Exception as e:  # noqa: BLE001 — 1 品の失敗で batch を止めない (Q0 痕跡)
            logger.warning("[research_duel] evaluate_product 失敗 rank=%s title=%r: %s",
                           i, title[:40], e)
        picks.append({"rc_id": rc_id, "rank": i, "title_ja": title})

    saved = True
    if picks:
        saved = duel.save_ai_picks(round_id, picks)  # → status ai_done (ai_pending 時のみ前進)
        if not saved:
            skip_msg = (
                f"round_id={round_id} は採点確定済/完了/無効化済のため "
                "AI picks 上書きをスキップしました。"
            )
            result["errors"].append(skip_msg)
            logger.warning("[research_duel] %s", skip_msg)
    result["ai_picks"] = len(picks) if saved else 0
    result["success"] = True
    result["message"] = (
        f"research_duel: round_id={round_id} {result['cell']} "
        f"picks={len(picks) if saved else 0} (harvest {len(products)} 件中)"
        + ("" if saved else " [picks保存スキップ: 採点確定済]")
    )
    logger.info(result["message"])
    return result
