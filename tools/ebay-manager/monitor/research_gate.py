#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 売れ行きゲート — 5 分岐判定ロジック (pure 関数、UI 非依存).

仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md §2-A
ロック済み決定 (§7 / §8 P1-2):
  - ゲート入力は当面すべて手入力 (Terapeak scraper は W229 以降)。
  - 5 分岐の優先順:
    1. sold_90d >= 2 → target_instock
    2. has_active_listing かつ sold_90d < 2 かつ 出品開始から 90 日以上 → reject_deadstock
    3. has_active_listing かつ sold_90d < 2 かつ 出品開始 90 日未満 → skip_too_new
    4. has_active_listing なし かつ sold_1_2yr >= 2 → target_oos_watch
    5. それ以外 → reject_no_demand

Q0 / K0 (サイレントスキップ禁止 / 仮定を捏造しない):
  - listing_start_date が None かつ has_active_listing=True の場合、
    「出品期間が不明」のため保守的に skip_too_new 扱い。
    reason に「開始日不明のため skip_too_new」と明記する。捏造しない。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

# ---- 決定値 (decision の取りうる 5 値) ----------------------------------------
DECISION_TARGET_INSTOCK = "target_instock"
DECISION_TARGET_OOS_WATCH = "target_oos_watch"
DECISION_REJECT_DEADSTOCK = "reject_deadstock"
DECISION_SKIP_TOO_NEW = "skip_too_new"
DECISION_REJECT_NO_DEMAND = "reject_no_demand"

_VALID_DECISIONS: frozenset[str] = frozenset({
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
    DECISION_REJECT_DEADSTOCK,
    DECISION_SKIP_TOO_NEW,
    DECISION_REJECT_NO_DEMAND,
})

# 出品期間の判定閾値 (仕様書 §2-A: 「出品開始 90 日以上で売れてない = 死に筋」)
_LISTING_AGE_THRESHOLD_DAYS = 90


def _parse_listing_start_date(raw: Optional[str]) -> Optional[date]:
    """listing_start_date を date に変換. パース失敗時は None.

    対応フォーマット:
      - "YYYY-MM"   → その月の 1 日
      - "YYYY-MM-DD" / "YYYY/MM/DD"
      - 空文字 / None → None (呼び出し元で保守的に扱う)

    K0: 不明な形式は None を返して caller で捏造なし処理を行う。
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    # YYYY-MM のみ (日なし)
    m = re.fullmatch(r"(\d{4})[/\-](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return None
    # YYYY-MM-DD or YYYY/MM/DD
    m2 = re.fullmatch(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            return None
    return None


def evaluate_sourcing_gate(
    *,
    sold_90d: int,
    has_active_listing: bool,
    listing_start_date: Optional[str] = None,
    sold_1_2yr: int,
    today: Optional[date] = None,
) -> tuple[str, str]:
    """売れ行きゲート 5 分岐判定.

    Args:
        sold_90d: 直近 90 日の sold 数 (Terapeak 手入力)。
        has_active_listing: 現在 active な競合出品が存在するか (Terapeak 手入力)。
        listing_start_date: 最古の競合出品の開始年月 (任意)。例: "2025-03" / "2025-03-15"。
            has_active_listing=False の場合は無視される。
            has_active_listing=True かつ None の場合 → 保守的に skip_too_new 扱い。
        sold_1_2yr: 1〜2 年の sold 数 (Terapeak 手入力)。
        today: テスト用に日付を固定する場合に渡す。省略時は date.today()。

    Returns:
        (decision, reason):
            decision: DECISION_* 定数の 1 つ。
            reason: 人間可読の根拠文字列 (日本語)。
    """
    if today is None:
        today = date.today()

    # ── 分岐 1: sold_90d >= 2 → 在庫あり寄り ────────────────────────────
    if sold_90d >= 2:
        return (
            DECISION_TARGET_INSTOCK,
            f"直近 90 日で {sold_90d} 件 sold → 需要あり・在庫あり寄りで仕入候補。",
        )

    # ── 以下は sold_90d < 2 ──────────────────────────────────────────────

    if has_active_listing:
        # 開始日のパース
        start_date = _parse_listing_start_date(listing_start_date)

        if start_date is None:
            # Q0: 捏造しない。開始日不明は保守的に skip_too_new。
            return (
                DECISION_SKIP_TOO_NEW,
                "出品あり・直近 90 日 sold < 2 だが出品開始日が不明 (未入力 or パース不能)。"
                "保守的に skip_too_new 扱い。再出現時に開始日を入力して再判定してください。",
            )

        listing_age_days = (today - start_date).days

        # ── 分岐 2: 出品あり + 90 日以上経過 + 売れてない → 死に筋 ─────
        if listing_age_days >= _LISTING_AGE_THRESHOLD_DAYS:
            return (
                DECISION_REJECT_DEADSTOCK,
                f"出品あり・開始から {listing_age_days} 日経過・直近 90 日 sold < 2 → 死に筋。"
                "競合が 90 日以上出して売れていないため除外。",
            )

        # ── 分岐 3: 出品あり + 90 日未満 + 売れてない → 今回スキップ ──
        remaining = _LISTING_AGE_THRESHOLD_DAYS - listing_age_days
        return (
            DECISION_SKIP_TOO_NEW,
            f"出品あり・開始から {listing_age_days} 日 (90 日未満)・直近 90 日 sold < 2 → 今回スキップ。"
            f"あと {remaining} 日後以降に再判定してください。",
        )

    # ── has_active_listing = False (出品ゼロ) ────────────────────────────

    # ── 分岐 4: 出品ゼロ + 1〜2 年で 2 件以上 → 在庫0+監視 ─────────────
    if sold_1_2yr >= 2:
        return (
            DECISION_TARGET_OOS_WATCH,
            f"現在出品ゼロ・1〜2 年 sold {sold_1_2yr} 件 → 過去需要あり。"
            "在庫 0 + キーワード監視で仕入チャンスを待つ候補。",
        )

    # ── 分岐 5: それ以外 → 需要なし ──────────────────────────────────────
    return (
        DECISION_REJECT_NO_DEMAND,
        f"現在出品ゼロ・1〜2 年 sold {sold_1_2yr} 件 (< 2) → 過去も需要なし。除外。",
    )
