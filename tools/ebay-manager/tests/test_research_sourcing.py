#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 Phase 3: task_research_sourcing unit test.

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §10 Phase 3 DoD

テストケース:
  (1) sourcing バッチ正常系 (evaluate_product mock → gate_passed → awaiting_approval 着地)
  (2) コスト cap 到達で中断 + 残候補 gate_passed 温存
  (3) fail-CLOSED (コスト集計例外で AI 呼び出しゼロ)
  (4) borderline → needs_review
  (5) 技術エラー → needs_review / 検索 0 件 → not_found の P2 分離
  (6) 重量推定失敗 → needs_review (P1-1: 0 clip 禁止)

conftest._isolate_monitor_db が autouse で tmp DB を使用 (本番 DB 汚染防止)。
"""
from __future__ import annotations

import json
import pytest

from monitor.database import init_db, get_conn
from monitor.research_candidates_db import (
    insert_research_candidate,
    get_research_candidate,
    update_status,
    save_gate_decision,
    STATUS_GATE_PASSED,
    STATUS_AWAITING_APPROVAL,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_FOUND,
    STATUS_SOURCED,
    STATUS_NEW,
)
from monitor.research_gate import DECISION_TARGET_INSTOCK, DECISION_TARGET_OOS_WATCH


# ---- ヘルパ ----------------------------------------------------------------

def _make_gate_passed_candidate(
    title_ja: str = "Sony WH-1000XM5",
    terapeak_avg_price_usd: float = 200.0,
    manual_weight_g: float = None,
    weight_source: str = None,
    gate_inputs: dict = None,
) -> int:
    """gate_passed 状態の research_candidate を作成して rc_id を返す."""
    init_db()
    rc_id = insert_research_candidate(
        title_ja=title_ja,
        terapeak_avg_price_usd=terapeak_avg_price_usd,
        manual_weight_g=manual_weight_g,
    )
    # gate_passed に遷移
    inputs_dict = gate_inputs or {"sold_90d": 5, "has_active_listing": True,
                                   "listing_start_date": "2024-01", "sold_1_2yr": 10}
    save_gate_decision(
        rc_id=rc_id,
        decision=DECISION_TARGET_INSTOCK,
        reason="sold >= 2 / has_active_listing=True",
        inputs_dict=inputs_dict,
        move_status=True,
    )
    # weight_source を直接書く (save_gate_decision は weight_source 非対応)
    if weight_source is not None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE research_candidates SET weight_source=? WHERE rc_id=?",
                (weight_source, rc_id),
            )
    return rc_id


def _make_config(
    enabled: bool = True,
    daily_cost_cap_usd: float = 3.0,
    max_items_per_run: int = 20,
) -> dict:
    return {
        "tasks_enabled": {
            "research_sourcing": {
                "enabled": enabled,
                "daily_cost_cap_usd": daily_cost_cap_usd,
                "max_items_per_run": max_items_per_run,
            }
        },
        "discord": {"webhook_url": ""},
    }


# ============================================================================
# (1) 正常系: gate_passed → evaluate_product mock → awaiting_approval
# ============================================================================

class TestNormalFlow:
    """evaluate_product を mock し gate_passed → awaiting_approval 着地を確認."""

    def test_gate_passed_to_awaiting_approval(self, monkeypatch):
        """keisuke PASS + borderline=False で sourced → awaiting_approval に遷移."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            title_ja="Sony WH-1000XM5",
            terapeak_avg_price_usd=200.0,
            manual_weight_g=300.0,  # 重量設定済 → AI 推定 skip
        )

        # evaluate_product を mock (sourced 終端、keisuke PASS、borderline=False)
        def _mock_evaluate(title_ja, *, rc_id=None, manual_weight_g=None,
                           terapeak_avg_price_usd=None, **kw):
            # status を DB 上で sourced に遷移させる (状態機械を通す)
            from monitor.research_candidates_db import update_status as _upd
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_SOURCED)
            from monitor.research_candidates_db import save_profit_true
            save_profit_true(
                rc_id=rc_id,
                profit_jpy_true=5000,
                profit_usd_true=33.0,
                keisuke_pass=True,
                keisuke_detail_json=json.dumps({
                    "pass": True, "profit_rate": 0.10, "pass_600": True,
                    "pass_rate": True, "threshold_jpy": 600.0, "borderline": False,
                }),
            )
            return {
                "rc_id": rc_id,
                "status": STATUS_SOURCED,
                "match_score": 85,
                "match_reason": "型番一致",
                "estimated_profit_usd": 33.0,
                "profit_jpy_true": 5000,
                "profit_usd_true": 33.0,
                "keisuke_pass": True,
                "keisuke_detail": {
                    "pass": True, "profit_rate": 0.10, "pass_600": True,
                    "pass_rate": True, "threshold_jpy": 600.0, "borderline": False,
                },
                "needs_review_reason": None,
                "found_url": "https://mercari.com/xxx",
                "found_price_jpy": 18000,
                "found_condition_ja": "美品",
                "source_platform": "mercari",
                "search_errors": [],
                "hits_count_total": 3,
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)

        # Section 232 mock (純関数なので副作用なし)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda title: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )

        # コスト mock (残量十分)
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap",
            lambda cap: (True, cap - 0.01, None),
        )

        # Discord mock
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["processed"] == 1
        assert result["sourced"] == 1
        assert result["awaiting_approval"] == 1
        assert result["needs_review"] == 0

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_AWAITING_APPROVAL


# ============================================================================
# (2) コスト cap 到達で中断 + 残候補 gate_passed 温存
# ============================================================================

class TestCostCapAbort:
    """コスト残量不足で AI 処理を中断し、残候補を gate_passed のまま温存する."""

    def test_cap_reached_midway(self, monkeypatch):
        """max_items_per_run=1 で 1 件処理後、2 件目処理前に cap 到達 → 残候補は gate_passed 温存."""
        init_db()
        # rc1 は先に作成 (created_at が古い = list_research_candidates の DESC で後ろになる)
        # rc2 は後で作成 (DESC で先頭 = 最初に処理される)
        # 実際の list 順は created_at DESC なので後から作ったものが先
        # max_items_per_run=2 にして 2 件目処理前に cap 到達させる
        rc1 = _make_gate_passed_candidate("商品A", manual_weight_g=300.0)
        rc2 = _make_gate_passed_candidate("商品B", manual_weight_g=200.0)

        processed_ids: list[int] = []

        def _mock_check_cost_cap(cap):
            # 1 回目 (初期チェック): OK
            # 2 回目 (最初の候補処理前): OK
            # 3 回目 (2 番目の候補処理前): NG (cap 到達 = agg_error なし)
            # 4 回目以降 (Discord 送信後の残量確認): NG
            if len(processed_ids) < 1:
                return (True, 2.99, None)
            return (False, 0.0, None)

        monkeypatch.setattr("tasks.task_research_sourcing._check_cost_cap", _mock_check_cost_cap)

        def _mock_evaluate(title_ja, *, rc_id=None, **kw):
            processed_ids.append(rc_id)
            from monitor.research_candidates_db import update_status as _upd, save_profit_true
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_SOURCED)
            save_profit_true(
                rc_id=rc_id,
                profit_jpy_true=5000,
                profit_usd_true=33.0,
                keisuke_pass=True,
                keisuke_detail_json=json.dumps({
                    "pass": True, "profit_rate": 0.10, "pass_600": True,
                    "pass_rate": True, "threshold_jpy": 600.0, "borderline": False,
                }),
            )
            return {
                "rc_id": rc_id, "status": STATUS_SOURCED, "match_score": 80,
                "keisuke_detail": {"borderline": False, "pass": True},
                "needs_review_reason": None, "search_errors": [],
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config(max_items_per_run=2))

        assert result["success"] is True
        # 1 件は処理、残 1 件は gate_passed のまま温存
        assert len(processed_ids) == 1, "1 件だけ処理されている"
        assert result["cost_aborted"] == 1, "残 1 件が cost_aborted"
        # 処理されなかった候補は gate_passed のまま
        unprocessed_id = rc1 if processed_ids[0] == rc2 else rc2
        cand = get_research_candidate(unprocessed_id)
        assert cand["status"] == STATUS_GATE_PASSED, "残候補は gate_passed のまま温存"


# ============================================================================
# (3) fail-CLOSED: コスト集計例外で AI 呼び出しゼロ
# ============================================================================

class TestFailClosed:
    """コスト集計で例外が発生した場合、AI を一切呼び出さずに終了する."""

    def test_cost_check_exception_blocks_all_ai_and_fails(self, monkeypatch):
        """集計失敗 (技術失敗) → AI 呼び出しゼロ + success=False (Q0: 偽装成功禁止)."""
        init_db()
        _make_gate_passed_candidate("テスト商品", manual_weight_g=300.0)

        # 1 回目 (初期 cap チェック) で集計失敗 → fail-CLOSED
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap",
            lambda cap: (False, 0.0, "OperationalError: no such table: api_call_log"),
        )

        evaluate_called = [False]

        def _mock_evaluate(*a, **kw):
            evaluate_called[0] = True
            raise AssertionError("evaluate_product should NOT be called on fail-CLOSED")

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is False, "集計失敗は技術失敗 = failed (Q0 偽装成功禁止)"
        assert evaluate_called[0] is False, "AI は呼ばれていない"
        assert any("集計失敗" in e for e in result["errors"])

    def test_cap_reached_is_normal_skip(self, monkeypatch):
        """cap 到達 (agg_error なし) → AI 呼び出しゼロ + success=True (正常 skip)."""
        init_db()
        _make_gate_passed_candidate("テスト商品2", manual_weight_g=300.0)

        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap",
            lambda cap: (False, 0.01, None),  # 残量 < margin、集計は正常
        )

        evaluate_called = [False]

        def _mock_evaluate(*a, **kw):
            evaluate_called[0] = True
            raise AssertionError("evaluate_product should NOT be called on cap reached")

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True, "cap 到達は正常 skip"
        assert evaluate_called[0] is False, "AI は呼ばれていない"


# ============================================================================
# (4) borderline → needs_review
# ============================================================================

class TestBorderlineToNeedsReview:
    """keisuke borderline=True の候補は needs_review に落とす."""

    def test_borderline_goes_to_needs_review(self, monkeypatch):
        init_db()
        rc_id = _make_gate_passed_candidate("borderline商品", manual_weight_g=300.0)

        def _mock_evaluate(title_ja, *, rc_id=None, **kw):
            from monitor.research_candidates_db import update_status as _upd, save_profit_true
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_SOURCED)
            save_profit_true(
                rc_id=rc_id,
                profit_jpy_true=620,
                profit_usd_true=4.1,
                keisuke_pass=True,
                keisuke_detail_json=json.dumps({
                    "pass": True, "profit_rate": 0.062, "pass_600": True,
                    "pass_rate": True, "threshold_jpy": 600.0, "borderline": True,
                }),
            )
            return {
                "rc_id": rc_id, "status": STATUS_SOURCED, "match_score": 75,
                "keisuke_detail": {"borderline": True, "pass": True},
                "needs_review_reason": None, "search_errors": [],
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["needs_review"] == 1

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NEEDS_REVIEW
        assert "境界" in (cand.get("needs_review_reason") or "")


# ============================================================================
# (5) P2: 技術エラー → needs_review / 0 件 → not_found の分離
# ============================================================================

class TestP2StatusSeparation:
    """技術失敗と業務判断 (0 件) の状態が混同されないことを確認."""

    def test_search_error_goes_to_needs_review(self, monkeypatch):
        """フリマ探索で取得エラー → needs_review (技術失敗)."""
        init_db()
        rc_id = _make_gate_passed_candidate("エラー商品", manual_weight_g=300.0)

        def _mock_evaluate(title_ja, *, rc_id=None, **kw):
            # 技術失敗: evaluate_product が needs_review を返す
            from monitor.research_candidates_db import update_status as _upd
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_NEEDS_REVIEW,
                 needs_review_reason="フリマ探索で取得エラー: mercari: Timeout")
            return {
                "rc_id": rc_id, "status": STATUS_NEEDS_REVIEW,
                "match_score": None, "keisuke_detail": None,
                "needs_review_reason": "フリマ探索で取得エラー: mercari: Timeout",
                "search_errors": ["mercari: Timeout"],
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["needs_review"] == 1

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NEEDS_REVIEW

    def test_zero_hits_goes_to_not_found(self, monkeypatch):
        """フリマ検索 0 件 → not_found (業務判断)."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            "在庫なし商品",
            manual_weight_g=300.0,
            # sold_1_2yr=0 → awaiting_approval に上げない
            gate_inputs={"sold_90d": 3, "has_active_listing": True,
                         "listing_start_date": "2024-01", "sold_1_2yr": 0},
        )

        def _mock_evaluate(title_ja, *, rc_id=None, **kw):
            from monitor.research_candidates_db import update_status as _upd
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_NOT_FOUND)
            return {
                "rc_id": rc_id, "status": STATUS_NOT_FOUND,
                "match_score": None, "keisuke_detail": None,
                "needs_review_reason": None, "search_errors": [],
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        # sold_1_2yr=0 なので not_found のまま (承認キューに上げない)
        assert result["not_found"] == 1
        assert result["awaiting_approval"] == 0

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NOT_FOUND


# ============================================================================
# (6) 重量推定失敗 → needs_review (P1-1)
# ============================================================================

class TestWeightEstimateFailure:
    """AI 重量推定失敗時は 0 clip せず needs_review に落とす (P1-1)."""

    def test_weight_estimate_failure_goes_to_needs_review(self, monkeypatch):
        """_estimate_weight が None を返した場合 → needs_review (evaluate_product 呼ばない)."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            "重量不明商品",
            manual_weight_g=None,  # 重量なし → AI 推定が試みられる
        )

        # AI 推定失敗 (None を返す)
        monkeypatch.setattr("tasks.task_research_sourcing._estimate_weight", lambda t: None)

        evaluate_called = [False]
        def _mock_evaluate(*a, **kw):
            evaluate_called[0] = True
            raise AssertionError("evaluate_product should not be called after weight failure")

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["needs_review"] == 1
        assert evaluate_called[0] is False, "重量不明で evaluate_product を呼ばない"

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NEEDS_REVIEW
        assert "重量推定失敗" in (cand.get("needs_review_reason") or "")

    def test_weight_already_set_skips_estimate(self, monkeypatch):
        """manual_weight_g 設定済み + weight_source='ai_estimate' → 再推定 skip."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            "重量設定済商品",
            manual_weight_g=500.0,
            weight_source="ai_estimate",  # 既に AI 推定済み
        )

        estimate_called = [False]
        def _mock_estimate(title):
            estimate_called[0] = True
            return None  # 呼ばれてはいけない

        monkeypatch.setattr("tasks.task_research_sourcing._estimate_weight", _mock_estimate)

        def _mock_evaluate(title_ja, *, rc_id=None, **kw):
            from monitor.research_candidates_db import update_status as _upd, save_profit_true
            _upd(rc_id, "sourcing")
            _upd(rc_id, STATUS_SOURCED)
            save_profit_true(
                rc_id=rc_id, profit_jpy_true=3000, profit_usd_true=20.0,
                keisuke_pass=True,
                keisuke_detail_json=json.dumps({
                    "pass": True, "profit_rate": 0.08, "pass_600": True,
                    "pass_rate": True, "threshold_jpy": 600.0, "borderline": False,
                }),
            )
            return {
                "rc_id": rc_id, "status": STATUS_SOURCED, "match_score": 80,
                "keisuke_detail": {"borderline": False, "pass": True},
                "needs_review_reason": None, "search_errors": [],
            }

        monkeypatch.setattr("monitor.research_poc.evaluate_product", _mock_evaluate)
        monkeypatch.setattr(
            "monitor.research_section232.estimate_section232",
            lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
        )
        monkeypatch.setattr(
            "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
        )
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert estimate_called[0] is False, "AI 推定済み商品は再推定しない"


# ============================================================================
# (7) match_score < 60 → not_found 降格 + 利益値クリア (supplier-matching-rules)
# ============================================================================

def _mock_evaluate_sourced_factory(match_score: int):
    """sourced 終端 + 指定 match_score + 利益保存済の evaluate_product mock を作る."""
    def _mock_evaluate(title_ja, *, rc_id=None, **kw):
        from monitor.research_candidates_db import (
            update_status as _upd,
            save_profit_true,
            update_research_candidate_result,
        )
        _upd(rc_id, "sourcing")
        _upd(rc_id, STATUS_SOURCED)
        # 実フロー同様、探索結果 (found_url / match_score) を DB に永続化
        update_research_candidate_result(
            rc_id,
            found_url="https://mercari.com/items/m_wrong",
            found_price_jpy=550,
            match_score=match_score,
            match_reason="別カテゴリ商品" if match_score < 60 else "型番一致",
        )
        save_profit_true(
            rc_id=rc_id,
            profit_jpy_true=13786,  # 誤マッチの安値由来の虚偽利益 (実機 Fujitsu N7100 事例)
            profit_usd_true=91.0,
            keisuke_pass=True,
            keisuke_detail_json=json.dumps({
                "pass": True, "profit_rate": 0.30, "pass_600": True,
                "pass_rate": True, "threshold_jpy": 600.0, "borderline": False,
            }),
        )
        return {
            "rc_id": rc_id, "status": STATUS_SOURCED,
            "match_score": match_score,
            "match_reason": "別カテゴリ商品" if match_score < 60 else "型番一致",
            "found_url": "https://mercari.com/items/m_wrong",
            "found_price_jpy": 550,
            "keisuke_detail": {"borderline": False, "pass": True},
            "needs_review_reason": None, "search_errors": [],
        }
    return _mock_evaluate


def _patch_common(monkeypatch):
    """Section232 / cost cap / Discord の共通 mock."""
    monkeypatch.setattr(
        "monitor.research_section232.estimate_section232",
        lambda t: {"flag": False, "annex": None, "rate": None, "matched_keyword": None},
    )
    monkeypatch.setattr(
        "tasks.task_research_sourcing._check_cost_cap", lambda cap: (True, 2.99, None)
    )
    monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)


class TestLowMatchScoreDemotion:
    """2026-06-11 Q1 実機事故の再発防止: sourced + match_score < 60 は
    not_found に降格し、誤マッチ価格由来の利益真値をクリアする."""

    def test_low_score_demoted_with_profit_cleared_and_queued_as_watch(self, monkeypatch):
        """score=30 + sold_1_2yr>0 → not_found 降格 → 利益クリア → 監視候補として承認待ち."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            "Fujitsu fi-7160 スキャナ", manual_weight_g=3000.0,
            gate_inputs={"sold_90d": 5, "has_active_listing": True,
                         "listing_start_date": "2024-01", "sold_1_2yr": 10},
        )
        monkeypatch.setattr(
            "monitor.research_poc.evaluate_product", _mock_evaluate_sourced_factory(30)
        )
        _patch_common(monkeypatch)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["low_match"] == 1
        assert result["sourced"] == 0, "降格後は sourced にカウントしない"
        assert result["not_found"] == 1
        # sold_1_2yr>0 → 監視候補として承認待ちに上がる (利益は未計算表示)
        assert result["not_found_approval"] == 1
        assert result["awaiting_approval"] == 1  # total = sourced系0 + not_found系1

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_AWAITING_APPROVAL
        # 虚偽利益のクリア確認 (verify_numbers: 誤マッチ価格の利益を残さない)
        assert cand["profit_jpy_true"] is None
        assert cand["profit_usd_true"] is None
        assert cand["estimated_profit_usd"] is None
        assert not cand["keisuke_pass"]
        # 承認キュー再投入行は誤マッチ仕入先フィールドも除去
        # (残すと承認 UI が found_url/found_price_jpy を下書きに消費 =
        #  誤商品 URL・虚偽原価の draft 汚染。retrospective H1 / rc 36 実発生)
        assert cand["found_url"] is None
        assert cand["found_price_jpy"] is None
        assert cand["found_condition_ja"] is None
        # 棄却監査痕跡は match_score / match_reason で残す
        assert cand["match_score"] == 30

    def test_low_score_without_sold_history_stays_not_found(self, monkeypatch):
        """score=0 + sold_1_2yr=0 → not_found 降格のまま (承認キューに積まない)."""
        init_db()
        rc_id = _make_gate_passed_candidate(
            "マクドナルド誤マッチ商品", manual_weight_g=500.0,
            gate_inputs={"sold_90d": 3, "has_active_listing": True,
                         "listing_start_date": "2024-01", "sold_1_2yr": 0},
        )
        monkeypatch.setattr(
            "monitor.research_poc.evaluate_product", _mock_evaluate_sourced_factory(0)
        )
        _patch_common(monkeypatch)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["low_match"] == 1
        assert result["not_found"] == 1
        assert result["awaiting_approval"] == 0
        assert result["not_found_approval"] == 0

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NOT_FOUND
        assert cand["profit_jpy_true"] is None
        # 終端 not_found (承認キューに戻さない行) は found_url を監査痕跡として残す
        assert cand["found_url"] == "https://mercari.com/items/m_wrong"

    def test_requeue_aborted_when_clear_found_fields_fails(self, monkeypatch):
        """clear_found_fields が False → 汚染防止のため再キュー中止 (not_found のまま).

        2026-06-13 retrospective H1: found_* を温存したまま awaiting_approval に
        戻すと承認 UI が誤商品 URL・虚偽原価を draft に消費する (rc 36 / draft 26
        実発生)。除去に失敗したら監視候補としても戻さない fail-closed を検証。
        """
        init_db()
        rc_id = _make_gate_passed_candidate(
            "clear失敗fail-closed検証商品", manual_weight_g=800.0,
            gate_inputs={"sold_90d": 5, "has_active_listing": True,
                         "listing_start_date": "2024-01", "sold_1_2yr": 4},
        )
        monkeypatch.setattr(
            "monitor.research_poc.evaluate_product", _mock_evaluate_sourced_factory(30)
        )
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "monitor.research_candidates_db.clear_found_fields", lambda _rc: False
        )

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["not_found"] == 1
        # 再キューは中止される (sold_1_2yr>0 でも承認キューに積まない)
        assert result["not_found_approval"] == 0
        assert result["awaiting_approval"] == 0
        # Q0: 中止の痕跡が errors に残る
        assert any("clear_found_fields 失敗" in e for e in result["errors"])

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_NOT_FOUND, "fail-closed: not_found のまま"

    def test_score_at_floor_passes_through(self, monkeypatch):
        """score=60 (floor ちょうど) は降格しない → 従来どおり awaiting_approval + 利益保持."""
        init_db()
        rc_id = _make_gate_passed_candidate("境界スコア商品", manual_weight_g=300.0)
        monkeypatch.setattr(
            "monitor.research_poc.evaluate_product", _mock_evaluate_sourced_factory(60)
        )
        _patch_common(monkeypatch)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config())

        assert result["success"] is True
        assert result["low_match"] == 0
        assert result["sourced"] == 1
        assert result["awaiting_approval"] == 1

        cand = get_research_candidate(rc_id)
        assert cand["status"] == STATUS_AWAITING_APPROVAL
        assert cand["profit_jpy_true"] == 13786, "floor 以上は利益を保持"


# ============================================================================
# enabled=false で skip
# ============================================================================

class TestDisabledSkip:
    """enabled=false の場合 success=True + 処理 0 件で終了."""

    def test_disabled_returns_success(self, monkeypatch):
        init_db()
        monkeypatch.setattr("tasks.task_research_sourcing._send_discord", lambda *a, **kw: True)

        from tasks.task_research_sourcing import run_research_sourcing
        result = run_research_sourcing(_make_config(enabled=False))

        assert result["success"] is True
        assert result["processed"] == 0


# ============================================================================
# Section 232 推定ユニットテスト
# ============================================================================

class TestSection232:
    """estimate_section232 純関数の基本動作確認."""

    def test_no_match_returns_false(self):
        from monitor.research_section232 import estimate_section232
        r = estimate_section232("Sony WH-1000XM5 ワイヤレスイヤホン")
        assert r["flag"] is False
        assert r["annex"] is None

    def test_annex_ia_match(self):
        from monitor.research_section232 import estimate_section232
        r = estimate_section232("南部鉄器 鉄瓶 急須 日本製")
        assert r["flag"] is True
        assert r["annex"] == "I-A"
        assert r["rate"] == 0.50

    def test_annex_ib_match_rice_cooker(self):
        from monitor.research_section232 import estimate_section232
        r = estimate_section232("象印 炊飯器 5.5合 NW-SA10")
        assert r["flag"] is True
        assert r["annex"] == "I-B"
        assert r["rate"] == 0.25

    def test_annex_ib_match_refrigerator(self):
        from monitor.research_section232 import estimate_section232
        r = estimate_section232("シャープ 冷蔵庫 450L SJ-MF45J")
        assert r["flag"] is True
        assert r["annex"] == "I-B"

    def test_ia_takes_priority_over_ib(self):
        """I-A キーワードが I-B より優先される."""
        from monitor.research_section232 import estimate_section232
        # フライパン (I-A) と炊飯器 (I-B) が同一タイトルに存在する場合
        r = estimate_section232("鋳鉄 炊飯器 鍋")
        assert r["annex"] == "I-A"  # I-A が優先
