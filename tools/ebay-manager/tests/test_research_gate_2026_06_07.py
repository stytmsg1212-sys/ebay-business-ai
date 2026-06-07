#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 売れ行きゲート 5 分岐 unit test.

仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md §2-A
テスト対象: monitor.research_gate.evaluate_sourcing_gate

方針:
  - 5 decision 各代表ケース + 境界 90 日 + 開始日不明フォールバック
  - API 不要 (pure 関数)、conftest の autouse DB patch は無害で共存可。
  - today= を固定してテスト再現性を保証。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from monitor.research_gate import (
    DECISION_REJECT_DEADSTOCK,
    DECISION_REJECT_NO_DEMAND,
    DECISION_SKIP_TOO_NEW,
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
    evaluate_sourcing_gate,
)

# テスト基準日 (固定で再現性担保)
_TODAY = date(2026, 6, 7)

# ── ヘルパー: 基準日から N 日前の listing_start_date 文字列を返す ──────────
def _days_ago(n: int) -> str:
    """N 日前を YYYY-MM-DD 形式で返す."""
    return (_TODAY - timedelta(days=n)).isoformat()


# ============================================================================
# 分岐 1: target_instock (sold_90d >= 2)
# ============================================================================

class TestTargetInstock:
    """sold_90d >= 2 は最優先、他の条件を問わず target_instock."""

    def test_basic(self):
        decision, reason = evaluate_sourcing_gate(
            sold_90d=2,
            has_active_listing=True,
            listing_start_date=_days_ago(200),
            sold_1_2yr=1,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_INSTOCK
        assert "90 日" in reason

    def test_high_sold(self):
        decision, _ = evaluate_sourcing_gate(
            sold_90d=10,
            has_active_listing=False,
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_INSTOCK

    def test_sold_90d_exactly_2(self):
        """境界: sold_90d=2 はギリギリ target_instock."""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=2,
            has_active_listing=False,
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_INSTOCK

    def test_sold_90d_1_does_not_trigger(self):
        """sold_90d=1 は target_instock にならない (他分岐へ)."""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=1,
            has_active_listing=False,
            sold_1_2yr=5,
            today=_TODAY,
        )
        assert decision != DECISION_TARGET_INSTOCK


# ============================================================================
# 分岐 2: reject_deadstock (has_active_listing + sold_90d < 2 + 開始 90 日以上)
# ============================================================================

class TestRejectDeadstock:
    """競合が 90 日以上出して売れない = 死に筋."""

    def test_basic_exact_90_days(self):
        """境界: 開始から ちょうど 90 日経過 → reject_deadstock."""
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(90),
            sold_1_2yr=10,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_DEADSTOCK
        assert "死に筋" in reason

    def test_91_days(self):
        """91 日以上も同様."""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=1,
            has_active_listing=True,
            listing_start_date=_days_ago(91),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_DEADSTOCK

    def test_180_days(self):
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(180),
            sold_1_2yr=3,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_DEADSTOCK

    def test_sold_1yr_does_not_save_deadstock(self):
        """sold_1_2yr が多くても出品 90 日以上 + sold_90d < 2 = reject_deadstock (仕様 §2-A)."""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(120),
            sold_1_2yr=100,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_DEADSTOCK


# ============================================================================
# 分岐 3: skip_too_new (has_active_listing + sold_90d < 2 + 開始 90 日未満)
# ============================================================================

class TestSkipTooNew:
    """出品開始 89 日以下は保留 (保留しない = 再出現時に再判定)."""

    def test_basic_89_days(self):
        """境界: 89 日は skip_too_new."""
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(89),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW
        assert "再判定" in reason

    def test_1_day(self):
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(1),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW

    def test_yyyymm_format(self):
        """YYYY-MM 形式 (日なし) でパースできること."""
        # 89 日前の YYYY-MM (月初) を構築
        start = _TODAY - timedelta(days=30)
        start_ym = start.strftime("%Y-%m")
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=start_ym,
            sold_1_2yr=0,
            today=_TODAY,
        )
        # 30 日前の月初 → 89 日未満の可能性が高い (スキップ or デッドストック)
        assert decision in {DECISION_SKIP_TOO_NEW, DECISION_REJECT_DEADSTOCK}

    def test_start_date_none_fallback(self):
        """listing_start_date=None かつ has_active_listing=True → skip_too_new (保守的)."""
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=None,
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW
        assert "不明" in reason

    def test_start_date_empty_string_fallback(self):
        """空文字も None 同様に skip_too_new (保守的)。Q0: 捏造しない。"""
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date="",
            sold_1_2yr=5,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW
        assert "不明" in reason

    def test_start_date_invalid_string_fallback(self):
        """パース不能な文字列も skip_too_new (保守的)。"""
        decision, reason = evaluate_sourcing_gate(
            sold_90d=1,
            has_active_listing=True,
            listing_start_date="invalid-date",
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW
        assert "不明" in reason


# ============================================================================
# 分岐 4: target_oos_watch (出品ゼロ + sold_1_2yr >= 2)
# ============================================================================

class TestTargetOosWatch:
    """出品ゼロ + 過去 1〜2 年で 2 件以上 = 在庫 0 + 監視候補."""

    def test_basic(self):
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            sold_1_2yr=2,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_OOS_WATCH
        assert "監視" in reason

    def test_sold_1_2yr_exactly_2(self):
        """境界: sold_1_2yr=2 はギリギリ target_oos_watch。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            sold_1_2yr=2,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_OOS_WATCH

    def test_sold_1_2yr_1_does_not_trigger(self):
        """sold_1_2yr=1 は target_oos_watch にならない。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            sold_1_2yr=1,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_NO_DEMAND

    def test_listing_start_date_ignored_when_no_listing(self):
        """has_active_listing=False なら listing_start_date は無視。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            listing_start_date=_days_ago(10),  # 渡しても無視される
            sold_1_2yr=3,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_OOS_WATCH


# ============================================================================
# 分岐 5: reject_no_demand (それ以外)
# ============================================================================

class TestRejectNoDemand:
    """過去も需要なし → 除外。"""

    def test_basic(self):
        decision, reason = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_NO_DEMAND
        assert "需要なし" in reason

    def test_sold_1_2yr_1(self):
        """sold_1_2yr=1 は reject_no_demand。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=1,
            has_active_listing=False,
            sold_1_2yr=1,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_NO_DEMAND


# ============================================================================
# 境界値テスト (90 日ジャスト)
# ============================================================================

class TestBoundary:
    """90 日の境界を細かく確認。"""

    def test_89_days_is_skip_too_new(self):
        """89 日 → skip_too_new。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(89),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_SKIP_TOO_NEW

    def test_90_days_is_reject_deadstock(self):
        """90 日 → reject_deadstock (閾値に到達)。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=True,
            listing_start_date=_days_ago(90),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_REJECT_DEADSTOCK

    def test_sold_90d_2_overrides_deadstock(self):
        """sold_90d=2 なら 90 日以上でも target_instock (分岐 1 最優先)。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=2,
            has_active_listing=True,
            listing_start_date=_days_ago(200),
            sold_1_2yr=0,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_INSTOCK

    def test_sold_1_2yr_2_boundary(self):
        """sold_1_2yr=2 (最低ライン) → target_oos_watch。"""
        decision, _ = evaluate_sourcing_gate(
            sold_90d=0,
            has_active_listing=False,
            sold_1_2yr=2,
            today=_TODAY,
        )
        assert decision == DECISION_TARGET_OOS_WATCH


# ============================================================================
# 返値の型チェック
# ============================================================================

class TestReturnTypes:
    """evaluate_sourcing_gate の返値は常に (str, str) のタプル。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(sold_90d=5, has_active_listing=True, sold_1_2yr=5),
            dict(sold_90d=0, has_active_listing=True,
                 listing_start_date=_days_ago(100), sold_1_2yr=0),
            dict(sold_90d=0, has_active_listing=True,
                 listing_start_date=_days_ago(50), sold_1_2yr=0),
            dict(sold_90d=0, has_active_listing=False, sold_1_2yr=3),
            dict(sold_90d=0, has_active_listing=False, sold_1_2yr=0),
        ],
    )
    def test_return_is_tuple_of_two_strings(self, kwargs):
        kwargs.setdefault("today", _TODAY)
        result = evaluate_sourcing_gate(**kwargs)
        assert isinstance(result, tuple)
        assert len(result) == 2
        decision, reason = result
        assert isinstance(decision, str)
        assert isinstance(reason, str)
        assert len(reason) > 0
