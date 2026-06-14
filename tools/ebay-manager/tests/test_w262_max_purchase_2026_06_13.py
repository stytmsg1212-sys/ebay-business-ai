#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W262 (board #1): 損益分岐仕入価格 逆算 + watch 上限価格採用 テスト.

対象:
  monitor.research_poc.compute_max_purchase_jpy
  tabs.tab_w228_research._compute_target_buy_jpy
  tabs.tab_w228_research._run_watch_only_approval (price_max_jpy 差し替え)

カバレッジ:
  (a) 境界検証: 返値 X で けいすけ基準 PASS、X+1 で FAIL (二分探索の正しさ)
  (b) 入力欠落: terapeak / weight 欠落 → (None, reason)、0 clip 禁止
  (c) 低 terapeak: ¥1 仕入でも不達 → (None, reason)
  (d) 単調性: 重い商品ほど上限仕入価格が下がる (送料増)
  (e) watch-only 承認: price_max_jpy に逆算値が使われる + memo に明記
  (f) fallback 互換: 逆算不能 rc は found_price_jpy に fallback (既存挙動維持)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.research_poc import (
    compute_max_purchase_jpy,
    compute_profit_true_for_research,
    keisuke_check,
)


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------

def _keisuke_pass_at(
    purchase_yen: int,
    *,
    terapeak_usd: float,
    weight_g: float,
    settings: dict,
) -> bool:
    """指定仕入価格でのけいすけ基準合否 (compute_max_purchase_jpy と同じ probe)."""
    profit_jpy, _usd, reason, revenue_jpy = compute_profit_true_for_research(
        terapeak_avg_price_usd=terapeak_usd,
        purchase_yen=purchase_yen,
        manual_weight_g=weight_g,
        settings=settings,
    )
    assert profit_jpy is not None, f"probe 計算不能 (purchase={purchase_yen}): {reason}"
    return bool(keisuke_check(float(profit_jpy), float(revenue_jpy or 0))["pass"])


# ---------------------------------------------------------------------------
# (a) 境界検証
# ---------------------------------------------------------------------------

class TestComputeMaxPurchaseBoundary:
    """二分探索の境界が正しいこと (X=PASS / X+1=FAIL)."""

    def test_boundary_pass_at_x_fail_at_x_plus_1(self):
        from calculator import load_settings
        s = load_settings()

        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=300.0,
            manual_weight_g=500.0,
            settings=s,
        )
        assert reason is None, f"逆算失敗: {reason}"
        assert isinstance(max_jpy, int) and max_jpy >= 1

        # 返値 X で PASS
        assert _keisuke_pass_at(
            max_jpy, terapeak_usd=300.0, weight_g=500.0, settings=s
        ), f"max_jpy={max_jpy} で PASS しない (境界誤り)"
        # X+1 で FAIL
        assert not _keisuke_pass_at(
            max_jpy + 1, terapeak_usd=300.0, weight_g=500.0, settings=s
        ), f"max_jpy+1={max_jpy + 1} でも PASS する (境界誤り)"

    def test_max_purchase_is_below_revenue(self):
        """上限仕入価格は売上 (terapeak×fx) を超えない (粗いサニティ)."""
        from calculator import load_settings
        s = load_settings()
        fx = s.get("exchange_rate", 150)

        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=200.0,
            manual_weight_g=400.0,
            settings=s,
        )
        assert reason is None, f"逆算失敗: {reason}"
        # 仕入が商品売値全額を超えて利益が出ることはない
        assert max_jpy < 200.0 * fx * 2, (
            f"max_jpy={max_jpy} が売上の 2 倍超 (入力異常検知が機能していない)"
        )


# ---------------------------------------------------------------------------
# (b) 入力欠落
# ---------------------------------------------------------------------------

class TestComputeMaxPurchaseMissingInputs:
    """terapeak / weight 欠落時は (None, reason)。0 clip 禁止 (P1-1 と同根)."""

    def test_terapeak_none_returns_none(self):
        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=None,
            manual_weight_g=500.0,
        )
        assert max_jpy is None
        assert reason is not None and "terapeak" in reason

    def test_terapeak_zero_returns_none(self):
        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=0.0,
            manual_weight_g=500.0,
        )
        assert max_jpy is None
        assert reason is not None

    def test_weight_none_returns_none(self):
        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=300.0,
            manual_weight_g=None,
        )
        assert max_jpy is None
        assert reason is not None and "weight" in reason.lower()

    def test_weight_zero_returns_none(self):
        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=300.0,
            manual_weight_g=0.0,
        )
        assert max_jpy is None
        assert reason is not None


# ---------------------------------------------------------------------------
# (c) 低 terapeak: ¥1 仕入でも不達
# ---------------------------------------------------------------------------

class TestComputeMaxPurchaseLowTerapeak:
    """売値が低すぎて送料負けする場合は (None, reason) で正直に返す."""

    def test_one_dollar_item_cannot_pass(self):
        from calculator import load_settings
        s = load_settings()
        max_jpy, reason = compute_max_purchase_jpy(
            terapeak_avg_price_usd=1.0,
            manual_weight_g=500.0,
            settings=s,
        )
        assert max_jpy is None, (
            f"$1 商品 (送料負け確実) で上限 ¥{max_jpy} が返った (偽の目標価格)"
        )
        assert reason is not None and "不達" in reason


# ---------------------------------------------------------------------------
# (d) 単調性: 重量増 → 上限仕入価格 減
# ---------------------------------------------------------------------------

class TestComputeMaxPurchaseMonotonicity:
    """送料が高くなる (重量増) ほど上限仕入価格は下がる."""

    def test_heavier_item_has_lower_max_purchase(self):
        from calculator import load_settings
        s = load_settings()

        light, r1 = compute_max_purchase_jpy(
            terapeak_avg_price_usd=300.0, manual_weight_g=300.0, settings=s
        )
        heavy, r2 = compute_max_purchase_jpy(
            terapeak_avg_price_usd=300.0, manual_weight_g=3000.0, settings=s
        )
        assert r1 is None and r2 is None, f"逆算失敗: light={r1}, heavy={r2}"
        assert heavy < light, (
            f"重量 3000g の上限 ¥{heavy:,} が 300g の上限 ¥{light:,} 以上 "
            "(送料が利益計算に反映されていない疑い)"
        )


# ---------------------------------------------------------------------------
# (e)(f) watch-only 承認: price_max_jpy 差し替え + fallback 互換
#     (既存 test_w228_phase4_approval_2026_06_12.py の watch-only パターン踏襲)
# ---------------------------------------------------------------------------

def _make_watch_candidate_in_db(title_ja: str) -> int:
    """not_found 再キュー経路で awaiting_approval に置いた候補を作る."""
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
    )
    init_db()
    rc_id = insert_research_candidate(title_ja=title_ja)
    update_status(rc_id, 'sourcing')            # new → sourcing
    update_status(rc_id, 'not_found')           # sourcing → not_found
    update_status(rc_id, 'awaiting_approval')   # not_found → awaiting_approval (監視候補)
    return rc_id


def _make_watch_rc_dict(
    rc_id: int,
    title_ja: str,
    *,
    terapeak_avg_price_usd: Optional[float] = None,
    manual_weight_g: Optional[float] = None,
    found_price_jpy: Optional[int] = None,
) -> dict:
    """watch-only 承認用 rc 辞書 (found_url 無し)."""
    return {
        'rc_id': rc_id,
        'title_ja': title_ja,
        'found_url': None,
        'found_price_jpy': found_price_jpy,
        'found_condition_ja': None,
        'manual_weight_g': manual_weight_g,
        'terapeak_avg_price_usd': terapeak_avg_price_usd,
        'length_cm': None,
        'width_cm': None,
        'height_cm': None,
        'profit_jpy_true': None,
        'profit_usd_true': None,
        'keisuke_pass': None,
        'keisuke_detail_json': None,
        'section232_flag': 0,
    }


def test_watch_only_approval_uses_computed_max_purchase():
    """(e) terapeak + weight ありの監視候補 → price_max_jpy = 損益分岐仕入価格."""
    from calculator import load_settings
    from monitor.research_candidates_db import get_research_candidate
    from tabs.tab_w228_research import _run_approval_logic

    terapeak_usd = 300.0
    weight_g = 500.0

    # 期待値: 同入力での逆算結果
    s = load_settings()
    expected_max, expected_reason = compute_max_purchase_jpy(
        terapeak_avg_price_usd=terapeak_usd,
        manual_weight_g=weight_g,
        settings=s,
    )
    assert expected_reason is None, f"テスト前提: 逆算成功すること ({expected_reason})"

    rc_id = _make_watch_candidate_in_db('W262-Test-Computed SONY WH-1000XM5')
    rc = _make_watch_rc_dict(
        rc_id, 'W262-Test-Computed SONY WH-1000XM5',
        terapeak_avg_price_usd=terapeak_usd,
        manual_weight_g=weight_g,
        found_price_jpy=None,
    )

    calls = []

    def mock_add_watch(**kwargs):
        calls.append(kwargs)
        return (800 + len(calls), True)

    with patch('tabs._supplier_description_pipeline.generate_supplier_description') as _p_gen, \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch', side_effect=mock_add_watch), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is True, f'失敗: {result["message"]}'
    assert _p_gen.call_count == 0, 'found_url 無しで description 生成が呼ばれた'
    assert len(calls) == 2  # mercari + yahoo_auctions

    # W262 核心: price_max_jpy が逆算値 (found_price_jpy=None ではなく)
    assert all(c['price_max_jpy'] == expected_max for c in calls), (
        f"price_max_jpy が逆算値 ¥{expected_max:,} でない: "
        f"{[c['price_max_jpy'] for c in calls]}"
    )
    # memo に損益分岐の痕跡 (後から watch 一覧で由来が分かること)
    assert all('損益分岐' in (c.get('memo') or '') for c in calls), (
        f"memo に損益分岐の明記なし: {[c.get('memo') for c in calls]}"
    )
    # result にも記録
    assert result['price_max_jpy'] == expected_max
    # success message に上限価格を明示
    assert f'¥{expected_max:,}' in result['message']

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc['status'] == 'watch_registered'


def test_watch_only_approval_fallback_to_found_price_when_uncomputable():
    """(f) terapeak 無し (逆算不能) → found_price_jpy に fallback (既存挙動維持)."""
    from tabs.tab_w228_research import _run_approval_logic

    rc_id = _make_watch_candidate_in_db('W262-Test-Fallback KEYENCE FS-N41N')
    rc = _make_watch_rc_dict(
        rc_id, 'W262-Test-Fallback KEYENCE FS-N41N',
        terapeak_avg_price_usd=None,   # 逆算不能
        manual_weight_g=280.0,
        found_price_jpy=5_000,         # fallback 先
    )

    calls = []

    def mock_add_watch(**kwargs):
        calls.append(kwargs)
        return (850 + len(calls), True)

    with patch('tabs._supplier_description_pipeline.generate_supplier_description'), \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch', side_effect=mock_add_watch), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is True, f'失敗: {result["message"]}'
    assert all(c['price_max_jpy'] == 5_000 for c in calls), (
        f"fallback (found_price_jpy=5000) が使われていない: "
        f"{[c['price_max_jpy'] for c in calls]}"
    )
    # 逆算値でないので memo に損益分岐の明記はしない (虚偽ラベル禁止)
    assert all('損益分岐' not in (c.get('memo') or '') for c in calls)
    # M-2: 由来の正のラベル (候補品価格である旨) は明記する (監査性)
    assert all('候補品価格' in (c.get('memo') or '') for c in calls), (
        f"memo に由来ラベルなし: {[c.get('memo') for c in calls]}"
    )
    assert result['price_max_jpy'] == 5_000


def test_watch_only_approval_s232_annotation_in_memo():
    """H-1: section232_flag=1 の rc → memo / message に Section232 注記伝播.

    逆算は実関税未反映 (calculator legacy washing) のため、S232 該当品では
    上限価格が過大になり得る。買いシグナル (in_price_range 通知) が赤字仕入を
    招かないよう、watch memo と承認メッセージに注記を必ず残す。
    """
    from calculator import load_settings
    from tabs.tab_w228_research import _run_approval_logic

    terapeak_usd = 300.0
    weight_g = 500.0
    s = load_settings()
    expected_max, expected_reason = compute_max_purchase_jpy(
        terapeak_avg_price_usd=terapeak_usd,
        manual_weight_g=weight_g,
        settings=s,
    )
    assert expected_reason is None, f"テスト前提: 逆算成功すること ({expected_reason})"

    rc_id = _make_watch_candidate_in_db('W262-Test-S232 Zojirushi 炊飯器')
    rc = _make_watch_rc_dict(
        rc_id, 'W262-Test-S232 Zojirushi 炊飯器',
        terapeak_avg_price_usd=terapeak_usd,
        manual_weight_g=weight_g,
        found_price_jpy=None,
    )
    rc['section232_flag'] = 1  # HS 8516.60.40 想定 (Annex I-B 25%)

    calls = []

    def mock_add_watch(**kwargs):
        calls.append(kwargs)
        return (880 + len(calls), True)

    with patch('tabs._supplier_description_pipeline.generate_supplier_description'), \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch', side_effect=mock_add_watch), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is True, f'失敗: {result["message"]}'
    # 逆算値自体は採用される (自動 BLOCK しない = 規約準拠)
    assert all(c['price_max_jpy'] == expected_max for c in calls)
    # memo に Section232 注記が伝播していること
    assert all('Section232' in (c.get('memo') or '') for c in calls), (
        f"memo に Section232 注記なし: {[c.get('memo') for c in calls]}"
    )
    # 承認メッセージにも注記
    assert 'Section232' in result['message'], (
        f"message に Section232 注記なし: {result['message']}"
    )


def test_compute_target_buy_jpy_handles_missing_rc():
    """_compute_target_buy_jpy(None) は (None, reason) で例外を出さない."""
    from tabs.tab_w228_research import _compute_target_buy_jpy
    max_jpy, reason = _compute_target_buy_jpy(None)
    assert max_jpy is None
    assert reason is not None
