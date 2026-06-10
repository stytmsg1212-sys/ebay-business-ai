#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 FIX-1: ゲート判定永続化 unit test.

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §3-2 / §4
対象: monitor.research_candidates_db.save_gate_decision
      monitor.database.init_db (v69 migration idempotency)

方針:
  - conftest._isolate_monitor_db が autouse で tmp DB に差し替える。
  - init_db() を明示的に呼び v69 schema を作成してからテスト。
  - FIX-1 の主要ケース: target_instock / target_oos_watch → gate_passed、
    reject / skip → gate_rejected、move_status=False、rc_id 不在。
  - v69 idempotency: init_db() 2 回連続でデータ保持を確認。
"""
from __future__ import annotations

import json
import pytest

from monitor.database import init_db, get_conn
from monitor.research_candidates_db import (
    STATUS_GATE_PASSED,
    STATUS_GATE_REJECTED,
    STATUS_NEW,
    get_research_candidate,
    insert_research_candidate,
    save_gate_decision,
)
from monitor.research_gate import (
    DECISION_REJECT_DEADSTOCK,
    DECISION_REJECT_NO_DEMAND,
    DECISION_SKIP_TOO_NEW,
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
)


# ---- 共通セットアップ -------------------------------------------------------

@pytest.fixture()
def db():
    """v69 schema 付き tmp DB を初期化して返す."""
    init_db()
    return get_conn


# ============================================================================
# v69 migration idempotency (Q2)
# ============================================================================

class TestV69Idempotency:
    """init_db() を 2 回呼んでもデータが消えないこと (Q2 冪等性)."""

    def test_double_init_retains_data(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="冪等テスト商品")
        assert rc_id is not None

        # 2 回目の init_db — v69 の ALTER TABLE は全て except OperationalError で握る
        init_db()

        row = get_research_candidate(rc_id)
        assert row is not None, "2 回目 init_db() 後にデータが消えた"
        assert row["title_ja"] == "冪等テスト商品"

    def test_v69_columns_exist_after_init(self):
        """v69 で追加される代表列が存在すること."""
        init_db()
        with get_conn() as conn:
            cols = {
                r[1] for r in
                conn.execute("PRAGMA table_info(research_candidates)").fetchall()
            }
        required = {
            "gate_decision", "gate_reason", "gate_inputs_json", "gated_at",
            "source", "profit_jpy_true", "profit_usd_true",
            "keisuke_pass", "keisuke_detail_json",
            "weight_source", "weight_confidence",
            "listing_draft_id", "watch_ids_json", "result_ebay_item_id",
        }
        missing = required - cols
        assert not missing, f"v69 列が見つからない: {missing}"


# ============================================================================
# save_gate_decision — target_* → gate_passed
# ============================================================================

class TestSaveGateDecisionTargetPassed:
    """target_instock / target_oos_watch は STATUS_GATE_PASSED へ遷移する."""

    def test_target_instock_moves_to_gate_passed(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="在庫あり商品")
        inputs = {"sold_90d": 3, "has_active_listing": True}
        result = save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="sold_90d=3 が閾値を超えた",
            inputs_dict=inputs,
            move_status=True,
        )
        assert result is True

        row = get_research_candidate(rc_id)
        assert row["status"] == STATUS_GATE_PASSED
        assert row["gate_decision"] == DECISION_TARGET_INSTOCK
        assert row["gate_reason"] == "sold_90d=3 が閾値を超えた"
        # inputs_dict が JSON として復元できること
        saved = json.loads(row["gate_inputs_json"])
        assert saved["sold_90d"] == 3

    def test_target_oos_watch_moves_to_gate_passed(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="在庫なし監視商品")
        result = save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_OOS_WATCH,
            reason="has_active_listing=False + sold_1_2yr>=1",
            inputs_dict={"sold_90d": 0, "has_active_listing": False},
            move_status=True,
        )
        assert result is True
        row = get_research_candidate(rc_id)
        assert row["status"] == STATUS_GATE_PASSED


# ============================================================================
# save_gate_decision — reject / skip → gate_rejected
# ============================================================================

class TestSaveGateDecisionRejected:
    """reject_* / skip_* は STATUS_GATE_REJECTED へ遷移する."""

    @pytest.mark.parametrize("decision", [
        DECISION_REJECT_DEADSTOCK,
        DECISION_REJECT_NO_DEMAND,
        DECISION_SKIP_TOO_NEW,
    ])
    def test_reject_moves_to_gate_rejected(self, decision: str):
        init_db()
        rc_id = insert_research_candidate(title_ja=f"却下商品_{decision}")
        result = save_gate_decision(
            rc_id=rc_id,
            decision=decision,
            reason=f"{decision} のため却下",
            inputs_dict={"sold_90d": 0},
            move_status=True,
        )
        assert result is True
        row = get_research_candidate(rc_id)
        assert row["status"] == STATUS_GATE_REJECTED
        assert row["gate_decision"] == decision


# ============================================================================
# save_gate_decision — move_status=False
# ============================================================================

class TestSaveGateDecisionMoveStatusFalse:
    """move_status=False は gate_* 列だけ書き status は変わらない."""

    def test_no_status_change_when_move_status_false(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="手動Wizardテスト商品")
        result = save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="手動 Wizard から後付け保存",
            inputs_dict={"sold_90d": 5},
            move_status=False,
        )
        assert result is True

        row = get_research_candidate(rc_id)
        # status は new のまま (insert 直後の既定)
        assert row["status"] == STATUS_NEW
        # gate 列は書かれている
        assert row["gate_decision"] == DECISION_TARGET_INSTOCK


# ============================================================================
# save_gate_decision — rc_id 不在
# ============================================================================

class TestSaveGateDecisionMissingId:
    """rc_id が存在しない場合は False を返す (ValueError は raise しない)."""

    def test_returns_false_for_nonexistent_rc_id(self):
        init_db()
        result = save_gate_decision(
            rc_id=999999,
            decision=DECISION_TARGET_INSTOCK,
            reason="存在しない rc_id",
            inputs_dict={},
        )
        assert result is False


# ============================================================================
# save_gate_decision — バリデーション (Q0)
# ============================================================================

class TestSaveGateDecisionValidation:
    """decision / reason が空の場合は ValueError (Q0 silent skip 防止)."""

    def test_empty_decision_raises(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="バリデーションテスト")
        with pytest.raises(ValueError, match="decision is required"):
            save_gate_decision(
                rc_id=rc_id,
                decision="",
                reason="正常な reason",
                inputs_dict={},
            )

    def test_empty_reason_raises(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="バリデーションテスト2")
        with pytest.raises(ValueError, match="reason is required"):
            save_gate_decision(
                rc_id=rc_id,
                decision=DECISION_TARGET_INSTOCK,
                reason="   ",  # whitespace のみ
                inputs_dict={},
            )


# ============================================================================
# gated_at は UTC で保存される (sqlite-timezone.md ルール)
# ============================================================================

class TestGatedAtUtc:
    """gated_at が NULL でなく設定されていること (UTC CURRENT_TIMESTAMP)."""

    def test_gated_at_is_set(self):
        init_db()
        rc_id = insert_research_candidate(title_ja="gated_at テスト")
        save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="gated_at 検証用",
            inputs_dict={},
        )
        row = get_research_candidate(rc_id)
        assert row["gated_at"] is not None, "gated_at が NULL のまま"


# ============================================================================
# FIX-A: evaluate_product rc_id 引き継ぎテスト
# ============================================================================

class TestEvaluateProductRcIdReuse:
    """FIX-A: rc_id 指定で INSERT をスキップし、既存 gate 行を再利用する."""

    def test_rc_id_reuse_single_row(self, monkeypatch):
        """gate 保存 → 同 title で evaluate_product(rc_id=...) → 単一行に両方の列が載る."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc
        from monitor import claude_evaluator as ce

        _init_db()

        # gate 行を作る (new → gate_passed)
        rc_id = insert_research_candidate(title_ja="FIX-A テスト商品")
        save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="FIX-A テスト: sold_90d=5",
            inputs_dict={"sold_90d": 5},
            move_status=True,
        )

        # gate_* 列が書かれていること
        row = get_research_candidate(rc_id)
        assert row["status"] == STATUS_GATE_PASSED
        assert row["gate_decision"] == DECISION_TARGET_INSTOCK

        # フリマ探索 mock
        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url=f"https://example.com/{platform}/FIX_A",
                    title=f"{kw} 美品",
                    price_jpy=15_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=85, reasoning="FIX-A mock"),
        )

        # 既存 rc_id を渡して evaluate_product を呼ぶ
        result = research_poc.evaluate_product(
            "FIX-A テスト商品",
            rc_id=rc_id,
            manual_weight_g=500.0,
            terapeak_avg_price_usd=250.0,
        )

        # 同一 rc_id であること (新規行を作っていないこと)
        assert result["rc_id"] == rc_id, (
            f"新規行が作られた (expected rc_id={rc_id}, got={result['rc_id']})"
        )

        # 単一行に gate_decision と found_url / match_score の両方が載ること
        row_after = get_research_candidate(rc_id)
        assert row_after["gate_decision"] == DECISION_TARGET_INSTOCK, (
            "gate_decision が消えた"
        )
        assert row_after["found_url"] is not None, "found_url が書かれていない"
        assert row_after["match_score"] == 85, "match_score が書かれていない"
        assert row_after["status"] in {"sourced", "needs_review"}, (
            f"status が想定外: {row_after['status']!r}"
        )

    def test_input_snapshot_persisted_on_reuse(self, monkeypatch):
        """FIX-A 追補: 再利用パスでも terapeak / 重量が行に書き戻される.

        2026-06-10 Q1 実機 (rc_id=10) で発覚: 再利用パスは INSERT を通らないため
        terapeak_avg_price_usd が NULL のまま利益だけ書かれていた。承認キューは
        「Terapeak と利益額を見て承認」が前提なので、入力スナップショットの
        書き戻しを保証する。
        """
        from monitor.database import init_db as _init_db
        from monitor import research_poc
        from monitor import claude_evaluator as ce

        _init_db()

        rc_id = insert_research_candidate(title_ja="FIX-A 追補テスト商品")
        save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="FIX-A 追補テスト",
            inputs_dict={"sold_90d": 5},
            move_status=True,
        )
        # gate 時点では入力スナップショットは空 (UI セクション A は数量のみ)
        assert get_research_candidate(rc_id)["terapeak_avg_price_usd"] is None

        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url=f"https://example.com/{platform}/FIX_A2",
                    title=f"{kw} 美品",
                    price_jpy=12_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=85, reasoning="mock"),
        )

        result = research_poc.evaluate_product(
            "FIX-A 追補テスト商品",
            rc_id=rc_id,
            manual_weight_g=300.0,
            terapeak_avg_price_usd=120.0,
        )
        assert result["rc_id"] == rc_id

        row = get_research_candidate(rc_id)
        assert row["terapeak_avg_price_usd"] == 120.0, (
            f"terapeak が書き戻されていない: {row['terapeak_avg_price_usd']!r}"
        )
        assert row["manual_weight_g"] == 300.0, (
            f"weight が書き戻されていない: {row['manual_weight_g']!r}"
        )
        # gate 列は維持されること (UPDATE が gate 列を触らない)
        assert row["gate_decision"] == DECISION_TARGET_INSTOCK

    def test_update_input_snapshot_nonexistent_raises(self):
        """update_input_snapshot: 存在しない rc_id は ValueError (Q0 silent no-op 禁止)."""
        from monitor.database import init_db as _init_db
        from monitor.research_candidates_db import update_input_snapshot

        _init_db()

        import pytest as _pytest
        with _pytest.raises(ValueError, match="rc_id=777666"):
            update_input_snapshot(777_666, terapeak_avg_price_usd=99.0)

    def test_nonexistent_rc_id_raises_value_error(self):
        """rc_id が存在しない場合は ValueError (Q0 silent 新規作成禁止)."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc

        _init_db()

        import pytest as _pytest
        with _pytest.raises(ValueError, match="rc_id=999888"):
            research_poc.evaluate_product(
                "存在しない rc_id テスト",
                rc_id=999_888,
            )

    def test_title_changed_uses_new_row(self, monkeypatch):
        """title を書き換えた場合は新規行 (rc_id 引き継がれない)."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc
        from monitor import claude_evaluator as ce

        _init_db()

        # gate 行を作る
        rc_id = insert_research_candidate(title_ja="元の商品名")
        save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="title 変更テスト",
            inputs_dict={},
            move_status=True,
        )

        # フリマ探索 mock
        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url=f"https://example.com/new",
                    title=f"{kw}",
                    price_jpy=10_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=70, reasoning="title 変更テスト"),
        )

        # 商品名を変えて呼ぶ (rc_id を渡さない = UI でタイトル書き換え時の動作)
        result = research_poc.evaluate_product(
            "別の商品名",  # title 変更
            # rc_id=None (渡さない)
            manual_weight_g=300.0,
            terapeak_avg_price_usd=200.0,
        )

        # 新規行が作られること (rc_id が gate 行と異なる)
        assert result["rc_id"] != rc_id, (
            "title を変えたのに gate 行の rc_id が引き継がれた"
        )


# ============================================================================
# FIX-B: save_profit_true → update_research_candidate_result 順序テスト
# ============================================================================

class TestProfitSavedBeforeStatusCommit:
    """FIX-B: status 確定前に profit_jpy_true が保存されること."""

    def test_profit_saved_even_if_status_update_fails(self, monkeypatch):
        """update_research_candidate_result が raise しても profit_jpy_true は保存済み."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc, research_candidates_db as rc_db_mod
        from monitor import claude_evaluator as ce

        _init_db()

        # フリマ探索 mock
        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url="https://example.com/fixb",
                    title="テスト商品 美品",
                    price_jpy=20_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=80, reasoning="FIX-B mock"),
        )

        # update_research_candidate_result を raise するように mock
        original_update = rc_db_mod.update_research_candidate_result

        call_count = {"n": 0}

        def _mock_update(rc_id_arg, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("FIX-B テスト: status update 失敗を模倣")

        monkeypatch.setattr(rc_db_mod, "update_research_candidate_result", _mock_update)

        # evaluate_product を呼ぶ (例外が出ることを確認)
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="FIX-B テスト"):
            research_poc.evaluate_product(
                "FIX-B テスト商品",
                manual_weight_g=500.0,
                terapeak_avg_price_usd=300.0,
            )

        # status は sourcing のまま (sourced になっていない) でも
        # profit_jpy_true が DB に保存されていること
        from monitor.research_candidates_db import list_research_candidates
        rows = list_research_candidates(limit=10)
        # 最新行を探す
        target = next(
            (r for r in rows if r.get("title_ja") == "FIX-B テスト商品"), None
        )
        assert target is not None, "FIX-B テスト商品の行が見つからない"
        # status は sourced になっていない
        assert target["status"] != "sourced", (
            f"status が sourced になった (FIX-B 前: status update 先行パターン)"
        )
        # profit_jpy_true は保存済み (FIX-B 後: profit first)
        assert target["profit_jpy_true"] is not None, (
            "profit_jpy_true が None のまま (FIX-B 効果なし: save_profit_true が先に呼ばれていない)"
        )


# ============================================================================
# FIX-C: save_profit_true 戻り値 False の warning ログ確認
# ============================================================================

class TestSaveProfitTrueFalseWarning:
    """FIX-C: save_profit_true が False を返す時に warning ログが出ること."""

    def test_warning_logged_when_save_profit_true_returns_false(self, monkeypatch, caplog):
        """save_profit_true=False の時 [research_poc] warning が出る."""
        import logging
        from monitor.database import init_db as _init_db
        from monitor import research_poc, research_candidates_db as rc_db_mod
        from monitor import claude_evaluator as ce

        _init_db()

        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url="https://example.com/fixc",
                    title="テスト商品 新品",
                    price_jpy=25_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=75, reasoning="FIX-C mock"),
        )
        # save_profit_true を常に False を返すように mock
        monkeypatch.setattr(rc_db_mod, "save_profit_true", lambda **kw: False)

        with caplog.at_level(logging.WARNING, logger="monitor.research_poc"):
            research_poc.evaluate_product(
                "FIX-C テスト商品",
                manual_weight_g=400.0,
                terapeak_avg_price_usd=250.0,
            )

        # warning ログが出ていること
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("save_profit_true failed" in m for m in warning_msgs), (
            f"save_profit_true failed の warning が出ていない: {warning_msgs}"
        )


# ============================================================================
# HIGH-1 (4巡目): stale _GATE_RC_ID_KEY が別商品の評価に流用されない
# ============================================================================

class TestStaleGateRcIdNotReused:
    """HIGH-1 (4巡目): gate 永続化失敗後に旧 rc_id が別商品評価に流用されない."""

    def test_evaluate_product_rejects_title_mismatch_rc_id(self):
        """rc_id の DB 行 title_ja と入力 title 不一致 → ValueError (別商品行への書込防止)."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc
        from monitor.research_candidates_db import (
            get_research_candidate,
        )

        _init_db()

        # 商品 X の gate 行を作成 → gate_passed にする
        rc_id_x = insert_research_candidate(title_ja="商品X テスト HIGH-1")
        save_gate_decision(
            rc_id=rc_id_x,
            decision=DECISION_TARGET_INSTOCK,
            reason="HIGH-1 テスト: 商品X gate",
            inputs_dict={"sold_90d": 3},
            move_status=True,
        )

        # 商品 X の行が gate_passed であること
        row_x_before = get_research_candidate(rc_id_x)
        assert row_x_before["status"] == STATUS_GATE_PASSED

        # 商品 Y (別タイトル) で rc_id_x を渡して evaluate_product を呼ぶ
        # → title 不一致で ValueError が raise されること
        import pytest as _pytest
        with _pytest.raises(ValueError, match="不一致"):
            research_poc.evaluate_product(
                "商品Y 別の商品 HIGH-1",  # 商品 X とは異なるタイトル
                rc_id=rc_id_x,
            )

        # 商品 X の行の status が gate_passed のまま (汚染されていない) こと
        row_x_after = get_research_candidate(rc_id_x)
        assert row_x_after["status"] == STATUS_GATE_PASSED, (
            f"商品 Y の評価で商品 X 行が汚染された: status={row_x_after['status']!r}"
        )

    def test_evaluate_product_allows_same_title_rc_id(self, monkeypatch):
        """同一 title の rc_id は ValueError にならず正常に再利用できること (正常経路の回帰)."""
        from monitor.database import init_db as _init_db
        from monitor import research_poc
        from monitor import claude_evaluator as ce

        _init_db()

        rc_id = insert_research_candidate(title_ja="正常経路テスト HIGH-1")
        save_gate_decision(
            rc_id=rc_id,
            decision=DECISION_TARGET_INSTOCK,
            reason="HIGH-1 正常経路テスト gate",
            inputs_dict={"sold_90d": 5},
            move_status=True,
        )

        # フリマ探索 mock
        monkeypatch.setattr(
            research_poc, "_search_freemarket",
            lambda platform, kw, max_results=5: [
                research_poc.FreemarketHit(
                    source_platform=platform,
                    url=f"https://example.com/{platform}/HIGH1",
                    title=f"{kw} 美品",
                    price_jpy=18_000,
                    image_url=None,
                )
            ],
        )
        monkeypatch.setattr(
            ce, "evaluate_match",
            lambda **kw: ce.EvaluationResult(match_score=80, reasoning="HIGH-1 正常経路 mock"),
        )

        # 同一タイトルで呼ぶ → ValueError にならず成功すること
        result = research_poc.evaluate_product(
            "正常経路テスト HIGH-1",  # 同一タイトル
            rc_id=rc_id,
            manual_weight_g=400.0,
            terapeak_avg_price_usd=250.0,
        )

        assert result["rc_id"] == rc_id, (
            f"新規行が作られた (expected rc_id={rc_id}, got={result['rc_id']})"
        )
        assert result["status"] in {"sourced", "needs_review"}
