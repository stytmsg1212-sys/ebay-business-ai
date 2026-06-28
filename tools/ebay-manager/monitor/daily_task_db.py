#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本日の作業タブ (W292) データ層 — daily_task_set / daily_task_streak CRUD.

設計書: tools/ebay-manager/.company/engineering/docs/2026-06-27-today-tasks-tab-design.md
スコープ (段1 データ層):
  - JST 当日の未登録 listing 上位 10 件を売れ筋 DESC で選定・凍結 (スナップショット固定)。
  - 連続達成日数 (streak) の読取・更新。
  - 欠落バッジ算出 (_missing_badges)。

ルール準拠:
  - listing 識別は ebay_item_id (sku-rules.md: SKU を一意キーにしない)。
  - JST 日付は DATE('now','+9 hours') で SQL 由来 (sqlite-timezone.md)。
  - Q0: 凍結 0 件は logger.warning で可視化 (silent skip 禁止)。
  - K1: 最小実装。コネクション管理は get_conn に委譲。
"""
from __future__ import annotations

import logging
from typing import Optional

from .database import get_conn

logger = logging.getLogger(__name__)

# ---- 内部定数 ----------------------------------------------------------------

_METRIC_INITIAL_REGISTER = "initial_register"

# ---- JST 日付 ----------------------------------------------------------------


def _today_jst(conn) -> str:
    """SQL 由来の JST 当日 'YYYY-MM-DD' (sqlite-timezone.md)."""
    row = conn.execute("SELECT DATE('now','+9 hours')").fetchone()
    return row[0]


# ---- 選定・凍結 --------------------------------------------------------------


def get_or_create_today_task_set(
    today_jst: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """JST 当日の作業 10 件を取得 (無ければ選定して凍結、あれば再利用)。

    today_jst: 'YYYY-MM-DD' (JST)。None なら内部で DATE('now','+9 hours') を使う。
    返り値: rank 昇順の list[dict]。各 dict は daily_task_set + ebay_listings 結合の
            live 値 (title / sold / competitor_count / 物理属性 / initial_registered)。
    冪等: 同日に複数回呼んでも同じ件数 (UNIQUE(jst_date, rank) で二重 INSERT を回避)。
    Q0: 凍結 0 件 (全 IGNORE) は logger.warning で可視化。
    """
    with get_conn() as conn:
        if today_jst is None:
            today_jst = _today_jst(conn)

        # Step 1: 当日セット存在チェック
        cnt = conn.execute(
            "SELECT COUNT(*) FROM daily_task_set WHERE jst_date = ?",
            (today_jst,),
        ).fetchone()[0]

        if cnt == 0:
            # Step 2: 未済プール抽出 (売れ筋 DESC 主キー + タイブレーク)
            rows = conn.execute(
                """
                SELECT
                    el.ebay_item_id,
                    el.title,
                    COALESCE(el.total_sold_count, 0) AS sold,
                    (SELECT COUNT(*) FROM competitor_products cp
                       WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1
                    ) AS competitor_count
                FROM ebay_listings el
                WHERE (el.is_ended IS NULL OR el.is_ended = 0)
                  AND el.title IS NOT NULL AND el.title != ''
                  AND COALESCE(el.initial_registered, 0) = 0
                ORDER BY
                    COALESCE(el.total_sold_count, 0) DESC,
                    CASE WHEN (SELECT COUNT(*) FROM competitor_products cp
                                 WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1
                              ) = 0
                         THEN 0 ELSE 1 END ASC,
                    (
                        (CASE WHEN el.purchase_yen IS NULL OR el.purchase_yen = 0
                              THEN 1 ELSE 0 END) +
                        (CASE WHEN el.weight_g IS NULL OR el.weight_g = 0
                              THEN 1 ELSE 0 END) +
                        (CASE WHEN el.length_cm IS NULL OR el.width_cm IS NULL
                                  OR el.height_cm IS NULL
                                  OR el.length_cm = 0 OR el.width_cm = 0 OR el.height_cm = 0
                              THEN 1 ELSE 0 END) +
                        (CASE WHEN el.lp_breakeven_usd IS NULL OR el.lp_breakeven_usd = 0
                              THEN 1 ELSE 0 END)
                    ) DESC,
                    el.ebay_item_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            # Step 3: 選定結果を daily_task_set に凍結
            for rank, row in enumerate(rows, start=1):
                conn.execute(
                    "INSERT OR IGNORE INTO daily_task_set "
                    "(jst_date, rank, ebay_item_id, title_snap, sold_snap) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (today_jst, rank, row[0], row[1], row[2]),
                )

            # Q0: 凍結 0 件の silent skip 防止
            frozen_cnt = conn.execute(
                "SELECT COUNT(*) FROM daily_task_set WHERE jst_date = ?",
                (today_jst,),
            ).fetchone()[0]
            if frozen_cnt == 0:
                logger.warning(
                    "[daily_task_db] %s: 凍結 0 件 — 未登録 active listing が存在しないか "
                    "INSERT OR IGNORE で全件衝突 (silent skip 防止ログ)",
                    today_jst,
                )

        # Step 4: 凍結済みセットを live 値で結合して返す
        live_rows = conn.execute(
            """
            SELECT
                dts.rank,
                dts.ebay_item_id,
                COALESCE(el.title, dts.title_snap)                      AS title,
                COALESCE(el.total_sold_count, dts.sold_snap, 0)         AS sold,
                el.sku,
                el.primary_market,
                el.purchase_yen,
                el.weight_g,
                el.length_cm,
                el.width_cm,
                el.height_cm,
                el.lp_breakeven_usd,
                COALESCE(el.initial_registered, 0)                      AS initial_registered,
                el.initial_registered_at,
                (SELECT COUNT(*) FROM competitor_products cp
                   WHERE cp.our_item_id = dts.ebay_item_id AND cp.is_active = 1
                ) AS competitor_count,
                (el.ebay_item_id IS NULL)                               AS listing_gone
            FROM daily_task_set dts
            LEFT JOIN ebay_listings el ON el.ebay_item_id = dts.ebay_item_id
            WHERE dts.jst_date = ?
            ORDER BY dts.rank ASC
            """,
            (today_jst,),
        ).fetchall()

    return [dict(r) for r in live_rows]


# ---- UI 集計用ラッパ ---------------------------------------------------------


def get_today_tasks_with_status(today_jst: Optional[str] = None) -> dict:
    """UI 集計用薄いラッパ。get_or_create_today_task_set を呼び、
    {"tasks": list[dict], "done": int, "total": int, "all_done": bool} を返す。

    集計母数から listing_gone (ebay_listings から物理削除済) を除外する。
    - total = listing_gone でない tasks の数。
    - done  = listing_gone でなく initial_registered が truthy な数。
    - all_done = (total > 0 and done == total)。
    tasks list 自体は全件返す (UI 側で gone を「削除済」と表示できるように)。
    """
    tasks = get_or_create_today_task_set(today_jst=today_jst)
    live_tasks = [t for t in tasks if not t.get("listing_gone")]
    total = len(live_tasks)
    done = sum(1 for t in live_tasks if t.get("initial_registered"))
    return {
        "tasks": tasks,
        "done": done,
        "total": total,
        "all_done": total > 0 and done == total,
    }


# ---- streak ------------------------------------------------------------------


def get_streak(metric: str = _METRIC_INITIAL_REGISTER) -> dict:
    """{"current_streak": int, "best_streak": int, "last_done_date": str|None}。
    行が無ければ全 0 / None を返す (INSERT しない = 読取専用)。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT current_streak, best_streak, last_done_date "
            "FROM daily_task_streak WHERE metric = ?",
            (metric,),
        ).fetchone()
    if row is None:
        return {"current_streak": 0, "best_streak": 0, "last_done_date": None}
    return {
        "current_streak": row[0],
        "best_streak": row[1],
        "last_done_date": row[2],
    }


def bump_streak_on_completion(
    today_jst: Optional[str] = None,
    metric: str = _METRIC_INITIAL_REGISTER,
) -> dict:
    """当日 10 件が all_done になった瞬間に呼ぶ (冪等)。

    - last_done_date == today_jst なら何もしない (同日二度押し吸収)。
    - last_done_date == 昨日 (JST) なら current_streak += 1。
    - それ以外 (飛び) なら current_streak = 1 にリセット。
    - best_streak = max(best_streak, current_streak)。
    - last_done_date = today_jst, updated_at = CURRENT_TIMESTAMP。
    UPSERT (INSERT ... ON CONFLICT(metric) DO UPDATE)。
    返り値 = 更新後 streak dict。
    「昨日」は SQL DATE(today_jst, '-1 day') で算出 (today_jst 基準)。
    """
    with get_conn() as conn:
        if today_jst is None:
            today_jst = _today_jst(conn)

        row = conn.execute(
            "SELECT current_streak, best_streak, last_done_date "
            "FROM daily_task_streak WHERE metric = ?",
            (metric,),
        ).fetchone()

        if row is not None and row[2] == today_jst:
            # 同日二度押し → 何もしない
            return {
                "current_streak": row[0],
                "best_streak": row[1],
                "last_done_date": row[2],
            }

        # 「昨日」判定
        yesterday = conn.execute(
            "SELECT DATE(?, '-1 day')", (today_jst,)
        ).fetchone()[0]

        if row is None:
            new_streak = 1
            new_best = 1
        elif row[2] == yesterday:
            # 連続達成
            new_streak = row[0] + 1
            new_best = max(row[1], new_streak)
        else:
            # 飛び → リセット
            new_streak = 1
            new_best = max(row[1] if row else 0, 1)

        conn.execute(
            """
            INSERT INTO daily_task_streak (metric, current_streak, best_streak, last_done_date, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(metric) DO UPDATE SET
                current_streak = excluded.current_streak,
                best_streak    = excluded.best_streak,
                last_done_date = excluded.last_done_date,
                updated_at     = excluded.updated_at
            """,
            (metric, new_streak, new_best, today_jst),
        )

    return {
        "current_streak": new_streak,
        "best_streak": new_best,
        "last_done_date": today_jst,
    }


# ---- 欠落バッジ (表示のみ・強制なし / 承認方針 A) ----------------------------


def _missing_badges(t: dict) -> list[str]:
    """残作業バッジのラベル list。空 list = 全項目埋まり (完備)。

    判定基準は §3.1 tie-break #3 / 商品管理 only_missing (L439-445) と同一。
    purchase_yen / weight_g / lp_breakeven_usd は not x (None/0 を欠落扱い)。
    寸法は 3 軸のうち 1 つでも欠落 or 0 で「寸法未」。
    """
    out: list[str] = []
    if not (t.get("competitor_count") or 0):
        out.append("ライバル未登録")
    if not t.get("purchase_yen"):
        out.append("仕入¥未")
    if not t.get("weight_g"):
        out.append("重量未")
    if not (t.get("length_cm") and t.get("width_cm") and t.get("height_cm")):
        out.append("寸法未")
    if not t.get("lp_breakeven_usd"):
        out.append("損益分岐未")
    return out
