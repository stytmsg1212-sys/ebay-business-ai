#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード#3 / W265: リサーチ状態整合 (ランク係数で中古売値を減額) の単体テスト.

検証対象 (純ロジック):
  - condition_sale_coeff: 8 段階ランク → 売値係数
  - _condition_is_used: 中古/新品/不明 の 3 値分類
  - compute_profit_true_for_research(effective_sale_price_usd=...): 売値 override で
    利益が下がること / 未指定で従来挙動を保つこと
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.research_poc import (  # noqa: E402
    condition_sale_coeff,
    _condition_is_used,
    _RANK_SALE_COEFF,
    compute_profit_true_for_research,
)


# ---- condition_sale_coeff ----

@pytest.mark.parametrize("rank,expected", [
    ("N", 1.00), ("S", 1.00),
    ("A", 0.85), ("B", 0.75), ("C", 0.65),
    ("D", 0.50), ("PO", 0.45), ("As-Is", 0.35),
])
def test_coeff_known_ranks(rank, expected):
    assert condition_sale_coeff(rank) == expected


def test_coeff_unknown_and_none_default_1():
    # 未知ランク / None / 空文字は無補正 (1.0)
    assert condition_sale_coeff(None) == 1.0
    assert condition_sale_coeff("") == 1.0
    assert condition_sale_coeff("ZZZ") == 1.0


def test_coeff_new_ranks_no_discount():
    # 新品 (N/S) は二重減額しない = 回帰なし
    assert condition_sale_coeff("N") == 1.0
    assert condition_sale_coeff("S") == 1.0


def test_coeff_used_ranks_monotonic_decreasing():
    # 状態が悪化するほど係数は単調減少 (A > B > C > D > PO > As-Is)
    order = ["A", "B", "C", "D", "PO", "As-Is"]
    vals = [_RANK_SALE_COEFF[r] for r in order]
    assert vals == sorted(vals, reverse=True)
    assert all(v < 1.0 for v in vals)


# ---- _condition_is_used ----

def test_condition_is_used_tristate():
    assert _condition_is_used("美品") is True
    assert _condition_is_used("ジャンク/動作未確認") is True
    assert _condition_is_used("新品") is False
    assert _condition_is_used("新品同様/未使用") is False
    assert _condition_is_used(None) is None
    assert _condition_is_used("") is None


# ---- compute_profit_true_for_research with override ----

_BASE_KW = dict(
    terapeak_avg_price_usd=100.0,
    purchase_yen=3000,
    manual_weight_g=500,
)


def test_override_lowers_profit():
    """売値 override (中古減額) を与えると利益は無補正より低くなる."""
    profit_new, _, reason_new, rev_new = compute_profit_true_for_research(**_BASE_KW)
    # B ランク係数 0.75 で売値を $75 に
    profit_used, _, reason_used, rev_used = compute_profit_true_for_research(
        effective_sale_price_usd=75.0, **_BASE_KW
    )
    assert reason_new is None and reason_used is None, (reason_new, reason_used)
    assert profit_new is not None and profit_used is not None
    # 売値が下がれば利益も売上も下がる (過大評価の是正)
    assert profit_used < profit_new
    assert rev_used < rev_new


def test_override_none_equals_baseline():
    """override 未指定 / None は terapeak_avg をそのまま使う (従来挙動)."""
    a = compute_profit_true_for_research(**_BASE_KW)
    b = compute_profit_true_for_research(effective_sale_price_usd=None, **_BASE_KW)
    assert a == b


def test_override_zero_or_negative_ignored():
    """0 / 負の override は無視して terapeak_avg を使う (偽の売値で計算しない)."""
    baseline = compute_profit_true_for_research(**_BASE_KW)
    z = compute_profit_true_for_research(effective_sale_price_usd=0.0, **_BASE_KW)
    neg = compute_profit_true_for_research(effective_sale_price_usd=-5.0, **_BASE_KW)
    assert z == baseline
    assert neg == baseline


def test_coeff_applied_to_terapeak_matches_explicit_override():
    """係数 × terapeak と明示 override が一致する (結線の数式整合)."""
    coeff = condition_sale_coeff("B")  # 0.75
    via_coeff = compute_profit_true_for_research(
        effective_sale_price_usd=100.0 * coeff, **_BASE_KW
    )
    via_explicit = compute_profit_true_for_research(
        effective_sale_price_usd=75.0, **_BASE_KW
    )
    assert via_coeff == via_explicit


# ---- _decide_condition_pricing (純関数、確信度ゲート) ----

from monitor.research_poc import _decide_condition_pricing  # noqa: E402


def test_decide_used_high_confidence_discounts():
    out = _decide_condition_pricing(
        rank_code="B", rank_confidence=0.9,
        terapeak_avg_price_usd=200.0,
        scraped_condition_ja="やや傷や汚れあり", fallback_condition_ja=None,
    )
    assert out["condition_is_used"] == 1
    assert out["effective_sale_price_usd"] == 150.0  # 200 * 0.75
    assert "中古(B)" in out["condition_match_note"]
    assert out["found_condition_ja"] == "やや傷や汚れあり"


def test_decide_new_high_confidence_no_discount():
    out = _decide_condition_pricing(
        rank_code="N", rank_confidence=0.95,
        terapeak_avg_price_usd=200.0,
        scraped_condition_ja="新品未使用", fallback_condition_ja=None,
    )
    assert out["condition_is_used"] == 0
    assert out["effective_sale_price_usd"] is None
    assert "新品(N)" in out["condition_match_note"]


def test_decide_low_confidence_no_discount():
    """API 失敗 fallback (confidence 0.3) は減額しない (機会損失方向の誤りを防ぐ)."""
    out = _decide_condition_pricing(
        rank_code="As-Is", rank_confidence=0.3,
        terapeak_avg_price_usd=200.0,
        scraped_condition_ja=None, fallback_condition_ja="美品",
    )
    assert out["condition_is_used"] is None
    assert out["effective_sale_price_usd"] is None
    assert "無補正" in out["condition_match_note"]
    # fallback の状態文字列は保持される
    assert out["found_condition_ja"] == "美品"


# ---- evaluate_product end-to-end (stage ①②③ 結線 + DB 列書込) ----

class TestEvaluateProductConditionMatch:
    """resolve/classify を注入し、状態整合が利益計算と DB に反映されるか."""

    def _setup_mocks(self, monkeypatch, *, rank_code, confidence, condition_ja):
        from types import SimpleNamespace
        from monitor import research_poc
        from monitor import claude_evaluator as ce
        from monitor.rank_classifier import RankClassification

        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url=f"https://mercari.com/{platform}/W265",
                    title=f"{kw}",
                    price_jpy=4_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=85, reasoning="W265 mock"),
        )
        monkeypatch.setattr(
            "monitor.product_resolver.resolve_product_from_url",
            lambda url, **kw: SimpleNamespace(
                scrape_error=None, condition_ja=condition_ja,
                description_ja="テスト説明", title_ja="テスト商品",
            ),
            raising=False,
        )
        monkeypatch.setattr(
            "monitor.rank_classifier.classify_rank",
            lambda **kw: RankClassification(
                rank_code=rank_code, rank_label="x", rank_jp="x",
                ebay_condition_id="3000", confidence=confidence, reasoning="mock",
            ),
            raising=False,
        )

    def test_used_candidate_discounts_and_persists(self, monkeypatch):
        from monitor import research_poc
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate, get_research_candidate,
        )
        init_db()
        self._setup_mocks(monkeypatch, rank_code="B", confidence=0.9,
                          condition_ja="やや傷や汚れあり")
        rc_id = insert_research_candidate(
            title_ja="W265 中古テスト", terapeak_avg_price_usd=200.0,
            manual_weight_g=500.0,
        )
        result = research_poc.evaluate_product(
            "W265 中古テスト", rc_id=rc_id,
            manual_weight_g=500.0, terapeak_avg_price_usd=200.0,
        )
        assert result["condition_is_used"] == 1
        assert result["condition_matched_price_usd"] == pytest.approx(150.0)
        assert "中古(B)" in result["condition_match_note"]
        # DB に永続化されている
        row = get_research_candidate(rc_id)
        assert row["condition_is_used"] == 1
        assert row["condition_matched_price_usd"] == pytest.approx(150.0)
        assert row["condition_match_note"]

    def test_used_profit_lower_than_new_baseline(self, monkeypatch):
        """同一候補で中古(減額)の利益 < 新品(無補正)の利益 (過大評価の是正)."""
        from monitor import research_poc
        from monitor.database import init_db
        from monitor.research_candidates_db import insert_research_candidate

        init_db()
        # 新品ケース (N, 無補正)
        self._setup_mocks(monkeypatch, rank_code="N", confidence=0.95,
                          condition_ja="新品未使用")
        rc_new = insert_research_candidate(
            title_ja="W265 baseline", terapeak_avg_price_usd=300.0,
            manual_weight_g=500.0,
        )
        res_new = research_poc.evaluate_product(
            "W265 baseline", rc_id=rc_new,
            manual_weight_g=500.0, terapeak_avg_price_usd=300.0,
        )
        # 中古ケース (D, 0.50 補正)
        self._setup_mocks(monkeypatch, rank_code="D", confidence=0.9,
                          condition_ja="ジャンク")
        rc_used = insert_research_candidate(
            title_ja="W265 used", terapeak_avg_price_usd=300.0,
            manual_weight_g=500.0,
        )
        res_used = research_poc.evaluate_product(
            "W265 used", rc_id=rc_used,
            manual_weight_g=500.0, terapeak_avg_price_usd=300.0,
        )
        assert res_new["profit_jpy_true"] is not None
        assert res_used["profit_jpy_true"] is not None
        assert res_used["profit_jpy_true"] < res_new["profit_jpy_true"]
        assert res_used["condition_is_used"] == 1
        assert res_new["condition_is_used"] == 0

    def test_reevaluate_to_needs_review_clears_stale_condition(self, monkeypatch):
        """HIGH-1 回帰: 中古判定済み行が再評価で探索エラー→needs_review に落ちる時、
        前回の condition 3列 (中古フラグ/減額売値/note) が stale 残存しないこと
        (UI/Discord の偽装バッジ防止 / Q0)."""
        from monitor import research_poc
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate, get_research_candidate,
            update_research_candidate_result, STATUS_NEEDS_REVIEW,
        )
        init_db()
        # 1回目: 中古判定で condition 3列が書かれる (sourced)
        self._setup_mocks(monkeypatch, rank_code="B", confidence=0.9,
                          condition_ja="やや傷や汚れあり")
        rc_id = insert_research_candidate(
            title_ja="W265 stale テスト", terapeak_avg_price_usd=200.0,
            manual_weight_g=500.0,
        )
        research_poc.evaluate_product(
            "W265 stale テスト", rc_id=rc_id,
            manual_weight_g=500.0, terapeak_avg_price_usd=200.0,
        )
        assert get_research_candidate(rc_id)["condition_is_used"] == 1

        # sourced → needs_review に遷移 (retry 前の状態。condition 列は保持 = stale 源)。
        # 実運用の retry_sourcing は needs_review → sourcing で再探索を起こす。
        update_research_candidate_result(
            rc_id, new_status=STATUS_NEEDS_REVIEW,
            needs_review_reason="retry 前リセット (テスト)",
        )
        assert get_research_candidate(rc_id)["condition_is_used"] == 1  # まだ残存

        # 2回目: needs_review→sourcing→(全 platform エラー)→needs_review 早期 return
        def _raise(*a, **k):
            raise RuntimeError("anti-bot")
        monkeypatch.setattr(research_poc, "_search_freemarket", _raise)
        research_poc.evaluate_product(
            "W265 stale テスト", rc_id=rc_id,
            manual_weight_g=500.0, terapeak_avg_price_usd=200.0,
        )
        row = get_research_candidate(rc_id)
        # 前回の中古減額ラベルが消えていること (偽装バッジ防止)
        assert row["condition_is_used"] is None
        assert row["condition_matched_price_usd"] is None
        assert "前回値クリア" in (row["condition_match_note"] or "")

    def test_reevaluate_to_not_found_clears_stale_condition(self, monkeypatch):
        """再評価でヒット0件→not_found に落ちる時も condition 3列を消す
        (在庫なし行に stale な中古バッジが残らない / Q0)."""
        from monitor import research_poc
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate, get_research_candidate,
            update_research_candidate_result, STATUS_NEEDS_REVIEW,
        )
        init_db()
        # 1回目: 中古判定で condition 3列が書かれる (sourced)
        self._setup_mocks(monkeypatch, rank_code="C", confidence=0.9,
                          condition_ja="使用感あり")
        rc_id = insert_research_candidate(
            title_ja="W265 not_found テスト", terapeak_avg_price_usd=180.0,
            manual_weight_g=400.0,
        )
        research_poc.evaluate_product(
            "W265 not_found テスト", rc_id=rc_id,
            manual_weight_g=400.0, terapeak_avg_price_usd=180.0,
        )
        assert get_research_candidate(rc_id)["condition_is_used"] == 1
        update_research_candidate_result(
            rc_id, new_status=STATUS_NEEDS_REVIEW,
            needs_review_reason="retry 前リセット (テスト)",
        )
        # 2回目: フリマ探索 0 件 → not_found 早期 return
        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [],
        )
        research_poc.evaluate_product(
            "W265 not_found テスト", rc_id=rc_id,
            manual_weight_g=400.0, terapeak_avg_price_usd=180.0,
        )
        row = get_research_candidate(rc_id)
        assert row["status"] == "not_found"
        assert row["condition_is_used"] is None
        assert row["condition_matched_price_usd"] is None
        assert "前回値クリア" in (row["condition_match_note"] or "")


class TestLowMatchDowngradeClearsCondition:
    """Codex HIGH-1 回帰: match_score < floor の降格経路 (clear_profit_fields /
    clear_found_fields) が condition 3列も消すこと (誤マッチ品の中古バッジ残存防止)."""

    def _seed_with_condition(self):
        from monitor.database import init_db
        from monitor.research_candidates_db import (
            insert_research_candidate, update_research_candidate_result,
            get_research_candidate,
        )
        init_db()
        rc_id = insert_research_candidate(
            title_ja="W265 降格テスト", terapeak_avg_price_usd=200.0,
            manual_weight_g=500.0,
        )
        update_research_candidate_result(
            rc_id,
            found_url="https://mercari.com/item/x",
            found_price_jpy=5000,
            condition_is_used=1,
            condition_matched_price_usd=150.0,
            condition_match_note="中古(B) 売値75%補正: $200→$150",
        )
        assert get_research_candidate(rc_id)["condition_is_used"] == 1
        return rc_id

    def test_clear_profit_fields_clears_condition(self):
        from monitor.research_candidates_db import (
            clear_profit_fields, get_research_candidate,
        )
        rc_id = self._seed_with_condition()
        clear_profit_fields(rc_id)
        row = get_research_candidate(rc_id)
        assert row["condition_is_used"] is None
        assert row["condition_matched_price_usd"] is None
        assert row["condition_match_note"] is None

    def test_clear_found_fields_clears_condition(self):
        from monitor.research_candidates_db import (
            clear_found_fields, get_research_candidate,
        )
        rc_id = self._seed_with_condition()
        clear_found_fields(rc_id)
        row = get_research_candidate(rc_id)
        assert row["condition_is_used"] is None
        assert row["condition_matched_price_usd"] is None
        assert row["condition_match_note"] is None
