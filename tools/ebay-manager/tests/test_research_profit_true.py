#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 FIX-2: keisuke_check + compute_profit_true_for_research unit test.

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §14-Q1/Q2
対象:
  monitor.research_poc.keisuke_check
  monitor.research_poc.compute_profit_true_for_research
  monitor.research_candidates_db.save_profit_true

方針:
  - keisuke_check は純関数 (外部 I/O なし) → parametrize で境界値を網羅。
  - compute_profit_true_for_research は calculator.calculate を実際に呼ぶ。
    (conftest._isolate_monitor_db は DB なしでも harmless なので共存)
  - weight 欠落 → (None, None, reason) を確認 (P1-1: 0 clip 禁止)。
  - save_profit_true は DB に書いて get_research_candidate で検証。
"""
from __future__ import annotations

import json
import pytest
from types import SimpleNamespace

from monitor.research_poc import keisuke_check, compute_profit_true_for_research


@pytest.fixture(autouse=True)
def _stub_w265_resolve(monkeypatch):
    """W265: evaluate_product が仕入先ページを実 scrape しないよう stub (hermetic)。

    scrape_error 付き = _decide_condition_pricing が untrusted → 売値無補正 = 既存
    テストの利益期待値を変えない。実 scrape の検証は Q1 実機で別途行う。
    """
    monkeypatch.setattr(
        "monitor.product_resolver.resolve_product_from_url",
        lambda url, **kw: SimpleNamespace(
            scrape_error="test-stub", condition_ja=None,
            description_ja=None, title_ja=None,
        ),
        raising=False,
    )


# ============================================================================
# keisuke_check 純関数テスト (設計書 §14-Q1 / §14-Q2)
# ============================================================================

class TestKeisukeCheckPass:
    """合格ケース: ¥600 条件 OR 6% 条件の either-or."""

    def test_pass_by_600_only(self):
        """profit=600 は ¥600 条件だけで合格 (率は 6% 未満でも OK)."""
        r = keisuke_check(profit_jpy=600.0, revenue_jpy=100_000.0)
        assert r["pass"] is True
        assert r["pass_600"] is True
        # 600 / 100,000 = 0.006 = 0.6% < 6%
        assert r["pass_rate"] is False

    def test_pass_by_rate_only(self):
        """rate=6% 以上は ¥600 未満でも合格."""
        # revenue=10,000 → threshold_rate = 600 円。profit=599 < 600 → pass_600=False
        # profit/revenue = 599/10,000 = 5.99% < 6% → ギリギリ失敗
        # revenue=9,000 → 540 円  profit=540 < 600 → pass_600=False
        # profit/revenue = 540/9000 = 6% → pass_rate=True
        r = keisuke_check(profit_jpy=540.0, revenue_jpy=9_000.0)
        assert r["pass"] is True
        assert r["pass_rate"] is True
        assert r["pass_600"] is False

    def test_pass_both(self):
        """¥600 以上かつ 6% 以上 → 両条件合格."""
        r = keisuke_check(profit_jpy=2_000.0, revenue_jpy=20_000.0)
        assert r["pass"] is True
        assert r["pass_600"] is True
        assert r["pass_rate"] is True


class TestKeisukeCheckFail:
    """不合格ケース."""

    def test_fail_both_conditions(self):
        """¥600 未満かつ 6% 未満は不合格."""
        # profit=500 < 600, rate = 500/20,000 = 2.5% < 6%
        r = keisuke_check(profit_jpy=500.0, revenue_jpy=20_000.0)
        assert r["pass"] is False
        assert r["pass_600"] is False
        assert r["pass_rate"] is False

    def test_negative_profit_fails(self):
        """負の利益は当然不合格."""
        r = keisuke_check(profit_jpy=-100.0, revenue_jpy=50_000.0)
        assert r["pass"] is False
        assert r["profit_rate"] < 0


class TestKeisukeCheckBoundary:
    """境界値テスト."""

    def test_exactly_600_passes(self):
        """profit=600 はちょうど合格 (>= 600)."""
        r = keisuke_check(profit_jpy=600.0, revenue_jpy=100_000.0)
        assert r["pass"] is True
        assert r["pass_600"] is True

    def test_599_fails_if_rate_also_fails(self):
        """profit=599 かつ rate < 6% は不合格."""
        r = keisuke_check(profit_jpy=599.0, revenue_jpy=100_000.0)
        # rate = 599/100,000 = 0.599% < 6% → both fail
        assert r["pass"] is False

    def test_exactly_6_percent_passes(self):
        """rate=6.0% (ぴったり) は合格."""
        # revenue=10,000 → threshold_rate = 600 円。profit=600 → both pass
        r = keisuke_check(profit_jpy=600.0, revenue_jpy=10_000.0)
        assert r["pass_rate"] is True
        assert r["pass"] is True


class TestKeisukeCheckBorderline:
    """borderline ±20% 帯テスト (設計書 §14-Q2)."""

    def test_borderline_within_20pct(self):
        """threshold の ±20% 帯内なら borderline=True."""
        # threshold_jpy = min(600, revenue*0.06)
        # revenue=9,000 → min(600, 540) = 540
        # 80% = 432, 120% = 648。profit=500 は 432〜648 内 → borderline
        r = keisuke_check(profit_jpy=500.0, revenue_jpy=9_000.0)
        assert r["borderline"] is True
        assert r["threshold_jpy"] == pytest.approx(540.0, rel=1e-3)

    def test_borderline_exactly_80pct(self):
        """threshold * 0.80 はボーダーライン内 (>= の境界)."""
        # threshold = min(600, revenue*0.06)。revenue=100,000 → min(600, 6000) = 600
        # 80% = 480。profit=480 → 480 >= 480 → borderline
        r = keisuke_check(profit_jpy=480.0, revenue_jpy=100_000.0)
        assert r["borderline"] is True

    def test_outside_borderline_is_false(self):
        """threshold * 0.79 以下ならボーダーライン外."""
        # threshold=600, 80%=480。profit=479 → borderline=False
        r = keisuke_check(profit_jpy=479.0, revenue_jpy=100_000.0)
        assert r["borderline"] is False


class TestKeisukeCheckRevenueZero:
    """revenue=0 エッジケース (設計書 §14 注記)."""

    def test_revenue_zero_uses_only_600_criterion(self):
        """revenue ≤ 0 は率計算不能 → ¥600 条件のみ判定、pass_rate=False."""
        r = keisuke_check(profit_jpy=700.0, revenue_jpy=0.0)
        assert r["pass"] is True
        assert r["pass_600"] is True
        assert r["pass_rate"] is False
        assert r["profit_rate"] == 0.0
        assert r["threshold_jpy"] == 600.0

    def test_revenue_zero_profit_low_fails(self):
        """revenue=0 かつ profit < 600 → 不合格."""
        r = keisuke_check(profit_jpy=500.0, revenue_jpy=0.0)
        assert r["pass"] is False


# ============================================================================
# keisuke_check: profit_rate field
# ============================================================================

class TestKeisukeCheckProfitRate:
    """profit_rate が正しく計算・丸めされること."""

    def test_profit_rate_rounded_to_4_digits(self):
        """profit_rate は小数点 4 桁に丸められる."""
        r = keisuke_check(profit_jpy=1_234.0, revenue_jpy=56_789.0)
        # 1234 / 56789 = 0.021729... → 丸め後 4 桁
        assert isinstance(r["profit_rate"], float)
        assert len(str(r["profit_rate"]).split(".")[-1]) <= 4


# ============================================================================
# compute_profit_true_for_research (FIX-2)
# ============================================================================

class TestComputeProfitTrueForResearch:
    """calculator.calculate を使った真値利益計算テスト."""

    def test_weight_missing_returns_none(self):
        """weight=None → (None, None, reason, None) で 0 clip しない (P1-1)."""
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=300.0,
            purchase_yen=30_000,
            manual_weight_g=None,
        )
        assert profit_jpy is None, "weight 欠落時に profit_jpy を 0 に clip してはいけない"
        assert profit_usd is None
        assert revenue_jpy is None
        assert reason is not None and ("weight" in reason.lower() or "manual_weight" in reason)

    def test_weight_zero_returns_none(self):
        """weight=0 → (None, None, reason, None) で 0 clip しない (P1-1)."""
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=300.0,
            purchase_yen=30_000,
            manual_weight_g=0.0,
        )
        assert profit_jpy is None
        assert profit_usd is None
        assert revenue_jpy is None
        assert reason is not None

    def test_valid_inputs_return_int_profit(self):
        """正常な入力は (int, float, None, float) を返す."""
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=400.0,
            purchase_yen=20_000,
            manual_weight_g=500.0,
        )
        # 計算成功時
        assert reason is None
        assert isinstance(profit_jpy, int), f"profit_jpy は int 型であること (got {type(profit_jpy)})"
        assert isinstance(profit_usd, float)
        assert isinstance(revenue_jpy, float), f"revenue_jpy は float 型であること (got {type(revenue_jpy)})"
        assert revenue_jpy > 0

    def test_terapeak_price_missing_returns_none(self):
        """terapeak 価格 None → 計算不能."""
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=0.0,
            purchase_yen=30_000,
            manual_weight_g=500.0,
        )
        assert profit_jpy is None
        assert revenue_jpy is None
        assert reason is not None

    def test_purchase_yen_zero_returns_none(self):
        """purchase_yen=0 → 計算不能."""
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=300.0,
            purchase_yen=0,
            manual_weight_g=500.0,
        )
        assert profit_jpy is None
        assert revenue_jpy is None
        assert reason is not None

    def test_profit_uses_max_service_result(self):
        """最良サービス (profit 最大) を採用する."""
        # 軽量品では複数のサービスで利益が異なる。最大値が返ること。
        from calculator import CalcInput, calculate, load_settings
        s = load_settings()
        inp = CalcInput(
            purchase_yen=15_000.0,
            item_price_usd=250.0,
            weight_g=300.0,
        )
        calc_result = calculate(inp, s)
        expected_best = max(calc_result.service_results, key=lambda sv: sv.profit).profit

        profit_jpy, _, _, _ = compute_profit_true_for_research(
            terapeak_avg_price_usd=250.0,
            purchase_yen=15_000,
            manual_weight_g=300.0,
            settings=s,
        )
        assert profit_jpy == int(expected_best), (
            f"max service profit={expected_best}, got={profit_jpy}"
        )

    def test_settings_injected_directly(self):
        """settings を直接渡すと load_settings() を呼ばない."""
        from calculator import load_settings
        s = load_settings()
        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=350.0,
            purchase_yen=18_000,
            manual_weight_g=400.0,
            settings=s,
        )
        # settings が有効なら計算成功するはず (profit の正負は問わない)
        assert reason is None or "失敗" not in reason, f"settings 渡しなのに失敗: {reason}"
        assert revenue_jpy is not None and revenue_jpy > 0

    def test_revenue_jpy_includes_shipping(self):
        """revenue_jpy は calculator.calculate の revenue と一致し、商品価格のみより大きい."""
        from calculator import CalcInput, calculate, load_settings
        s = load_settings()
        inp = CalcInput(
            purchase_yen=20_000.0,
            item_price_usd=300.0,
            weight_g=500.0,
        )
        calc_result = calculate(inp, s)
        expected_revenue = float(calc_result.revenue)
        fx = s.get("exchange_rate", 150)
        item_price_jpy_only = 300.0 * fx

        profit_jpy, profit_usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=300.0,
            purchase_yen=20_000,
            manual_weight_g=500.0,
            settings=s,
        )
        assert reason is None
        assert revenue_jpy == pytest.approx(expected_revenue, rel=1e-4), (
            f"revenue_jpy={revenue_jpy} が calculator.revenue={expected_revenue} と不一致"
        )
        assert revenue_jpy > item_price_jpy_only, (
            f"revenue_jpy={revenue_jpy} は商品価格のみ({item_price_jpy_only})より大きいはず (送料込み)"
        )


# ============================================================================
# save_profit_true (DB 書込テスト)
# ============================================================================

class TestSaveProfitTrue:
    """DB への profit_true 書込テスト."""

    def test_save_profit_true_persists(self):
        """save_profit_true が DB に正しく書き込まれること."""
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate,
            save_profit_true,
            get_research_candidate,
        )

        init_db()
        rc_id = insert_research_candidate(title_ja="利益真値テスト商品")

        detail = {"pass": True, "profit_rate": 0.08, "pass_600": True}
        ok = save_profit_true(
            rc_id=rc_id,
            profit_jpy_true=1_200,
            profit_usd_true=7.5,
            keisuke_pass=True,
            keisuke_detail_json=json.dumps(detail, ensure_ascii=False),
        )
        assert ok is True

        row = get_research_candidate(rc_id)
        assert row["profit_jpy_true"] == 1_200
        assert row["profit_usd_true"] == pytest.approx(7.5, rel=1e-4)
        assert row["keisuke_pass"] == 1  # SQLite は bool を 0/1 で保存

    def test_save_profit_true_none_values_stored_as_null(self):
        """profit=None は NULL で保存される (0 clip 禁止 P1-1)."""
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate,
            save_profit_true,
            get_research_candidate,
        )

        init_db()
        rc_id = insert_research_candidate(title_ja="利益NULL テスト商品")
        ok = save_profit_true(
            rc_id=rc_id,
            profit_jpy_true=None,
            profit_usd_true=None,
            keisuke_pass=False,
            keisuke_detail_json='{"pass": false}',
        )
        assert ok is True

        row = get_research_candidate(rc_id)
        assert row["profit_jpy_true"] is None, "None を 0 に clip してはいけない"
        assert row["profit_usd_true"] is None

    def test_save_profit_true_nonexistent_rc_id_returns_false(self):
        """rc_id が存在しない場合は False を返す (Q0)."""
        from monitor.database import init_db
        from monitor.research_candidates_db import save_profit_true

        init_db()
        ok = save_profit_true(
            rc_id=999_999,
            profit_jpy_true=1_000,
            profit_usd_true=6.0,
            keisuke_pass=True,
            keisuke_detail_json='{}',
        )
        assert ok is False


# ============================================================================
# HIGH-1: _infer_condition_ja 単体テスト (再発防止: "書いたつもり no-op" 検出)
# ============================================================================

class TestInferConditionJa:
    """_infer_condition_ja の優先順位 / 境界値 / DB 書込テスト."""

    def test_junk_beats_good_description(self):
        """「美品 ジャンク」→ ジャンク側 (悪い状態優先)."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("SONY ヘッドフォン 美品 ジャンク 部品取り")
        assert result == "ジャンク/動作未確認"

    def test_power_on_only(self):
        """「通電確認のみ」→ 通電のみ."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("Panasonic ブルーレイ 通電確認のみ 動作未確認")
        # 「動作未確認」がより先のカテゴリなので「ジャンク/動作未確認」が返る
        assert result == "ジャンク/動作未確認"

    def test_power_on_only_without_junk(self):
        """「通電のみ」(ジャンク系なし) → 通電のみ."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("Victor テープデッキ 通電のみ 未確認")
        assert result == "通電のみ"

    def test_no_match_returns_none(self):
        """一致なし → None (推定を捏造しない)."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("Audio-Technica ATH-M50x")
        assert result is None

    def test_empty_string_returns_none(self):
        """空文字列 → None."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("")
        assert result is None

    def test_shinpin_detected(self):
        """「新品」「未開封」「シュリンク」→ 新品."""
        from monitor.research_poc import _infer_condition_ja
        result = _infer_condition_ja("Nikon レンズ 新品 未開封")
        assert result == "新品"

    def test_shinpin_yoyo_beats_shinpin(self):
        """「新品同様」は「新品」より悪い状態優先ルールで上書きされない (別カテゴリ)."""
        from monitor.research_poc import _infer_condition_ja
        # 「新品同様」単独 → "新品同様/未使用"
        result = _infer_condition_ja("Canon EF 50mm 新品同様 開封品")
        assert result == "新品同様/未使用"

    def test_found_condition_ja_written_to_db(self):
        """found_condition_ja が非 None の時 DB に書かれること (HIGH-1 再発防止).

        update_research_candidate_result を直接呼んで DB に書き、
        get_research_candidate で確認することで「書いたつもり no-op」を検出する。
        """
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            get_research_candidate,
            insert_research_candidate,
            update_research_candidate_result,
        )
        from monitor.research_poc import _infer_condition_ja

        init_db()
        rc_id = insert_research_candidate(title_ja="found_condition_ja 書込テスト商品")

        # キーワードマッチするタイトルで推定
        title_with_keyword = "SONY WH-1000XM4 美品 動作確認済み"
        cond = _infer_condition_ja(title_with_keyword)
        assert cond is not None, "_infer_condition_ja が None を返した (テスト前提失敗)"

        # new → sourcing → sourced の順で遷移する (直接 new→sourced は禁止)
        from monitor.research_candidates_db import update_status, STATUS_SOURCING
        update_status(rc_id, STATUS_SOURCING)

        update_research_candidate_result(
            rc_id,
            found_url="https://example.com/item/1",
            found_price_jpy=8_000,
            found_condition_ja=cond,
            match_score=85,
            match_reason="外観一致",
            estimated_profit_usd=None,
            new_status="sourced",
            needs_review_reason=None,
        )

        row = get_research_candidate(rc_id)
        assert row["found_condition_ja"] == cond, (
            f"DB に found_condition_ja が書かれていない (got: {row['found_condition_ja']!r})"
        )


# ============================================================================
# HIGH-A: evaluate_product (settings=None 経路) の keisuke revenue 回帰テスト
# ============================================================================

def test_evaluate_product_keisuke_revenue_includes_shipping_when_settings_none(monkeypatch):
    """HIGH-A 再発防止: settings=None 経路でも revenue_jpy が calculator.revenue と一致する.

    本番呼び出し元 tabs/tab_w228_research.py:315 は settings=None を渡す。
    修正前は settings=None ガード条件により fallback (商品価格×fx のみ) に落ち、
    keisuke_result["revenue_basis"]="calculator_revenue" ラベルが虚偽だった (HIGH-A)。

    検証項目:
      1. keisuke_result["revenue_jpy"] が calculator.calculate の revenue と一致
      2. revenue_jpy > terapeak_avg_price_usd * exchange_rate (送料込み > 商品価格のみ)
      3. keisuke_result["revenue_basis"] == "calculator_revenue"
    """
    from monitor.database import init_db
    from monitor import research_poc
    from calculator import CalcInput, calculate, load_settings

    init_db()

    monkeypatch.setattr(
        research_poc, "_search_freemarket",
        lambda platform, kw, max_results=5: [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/item/HIGH_A",
                title=f"{kw} ({platform})",
                price_jpy=20_000,
                image_url=None,
            )
        ],
    )

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: ce.EvaluationResult(
            match_score=88, reasoning="HIGH-A テスト mock"
        ),
    )

    terapeak_usd = 300.0
    weight_g = 500.0

    result = research_poc.evaluate_product(
        title_ja="HIGH-A Keisuke Revenue Test",
        manual_weight_g=weight_g,
        terapeak_avg_price_usd=terapeak_usd,
        settings=None,  # 本番経路: load_settings() に委ねる
    )

    # evaluate_product 自体が成功していること
    assert result["status"] == "sourced", (
        f"status={result['status']}, needs_review_reason={result.get('needs_review_reason')}"
    )

    # keisuke_result が生成されていること
    keisuke_detail = result.get("keisuke_detail")
    assert keisuke_detail is not None, "keisuke_detail が None (利益計算失敗経路に落ちた)"

    # revenue_basis が calculator_revenue であること (虚偽ラベル禁止)
    assert keisuke_detail.get("revenue_basis") == "calculator_revenue", (
        f"revenue_basis={keisuke_detail.get('revenue_basis')!r} (fallback に落ちている)"
    )

    # revenue_jpy が calculator.calculate の revenue と一致すること
    s = load_settings()
    inp = CalcInput(
        purchase_yen=20_000.0,
        item_price_usd=terapeak_usd,
        weight_g=weight_g,
    )
    calc_result = calculate(inp, s)
    expected_revenue = float(calc_result.revenue)
    fx = s.get("exchange_rate", 150)
    item_price_jpy_only = terapeak_usd * fx

    revenue_jpy = keisuke_detail.get("revenue_jpy")
    assert revenue_jpy is not None, "keisuke_detail に revenue_jpy が存在しない"
    assert revenue_jpy == pytest.approx(expected_revenue, rel=1e-3), (
        f"revenue_jpy={revenue_jpy} が calculator.revenue={expected_revenue} と不一致"
    )

    # 送料込みの revenue が商品価格のみより大きいこと
    assert revenue_jpy > item_price_jpy_only, (
        f"revenue_jpy={revenue_jpy} が商品価格のみ({item_price_jpy_only})以下 "
        "(送料が revenue に含まれていない)"
    )
