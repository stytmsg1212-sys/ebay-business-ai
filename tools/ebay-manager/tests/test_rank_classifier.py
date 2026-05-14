#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_classifier の単体テスト。

Claude API 呼出しは unittest.mock.patch で全モック化する。
実 API / 実 DB への接続は一切行わない。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# tools/ebay-manager/ を sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.rank_classifier import (  # noqa: E402
    RankClassification,
    VALID_RANKS,
    _build_result,
    _fallback_classify,
    _RANK_TABLE,
    _extract_json,
    classify_rank,
)


# =========================================================================
# テストユーティリティ
# =========================================================================

def _make_claude_response(payload: dict) -> MagicMock:
    """Claude messages.create の戻り値を模す。"""
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


# =========================================================================
# _build_result
# =========================================================================

class TestBuildResult:
    def test_valid_rank_n(self):
        r = _build_result("N", 0.9, "new sealed")
        assert r.rank_code == "N"
        assert r.rank_label == "New (Unopened)"
        assert r.ebay_condition_id == "1000"
        assert r.confidence == 0.9

    def test_valid_rank_as_is(self):
        r = _build_result("As-Is", 0.4, "不明")
        assert r.rank_code == "As-Is"
        assert r.ebay_condition_id == "7000"

    def test_valid_rank_po(self):
        r = _build_result("PO", 0.7, "通電のみ")
        assert r.rank_code == "PO"
        assert r.ebay_condition_id == "3000"

    def test_invalid_rank_falls_back_to_as_is(self):
        r = _build_result("X", 0.5, "不明")
        assert r.rank_code == "As-Is"

    def test_confidence_clamp_high(self):
        r = _build_result("A", 1.5, "")
        assert r.confidence == 1.0

    def test_confidence_clamp_low(self):
        r = _build_result("A", -0.3, "")
        assert r.confidence == 0.0

    def test_confidence_non_numeric(self):
        r = _build_result("A", "not a number", "")
        assert r.confidence == 0.0


# =========================================================================
# _fallback_classify (Claude なしでの regex 判定)
# =========================================================================

class TestFallbackClassify:
    def test_new_unopened(self):
        r = _fallback_classify("新品未開封", None, None)
        assert r.rank_code == "N"
        assert r.ebay_condition_id == "1000"

    def test_like_new_unused(self):
        r = _fallback_classify("未使用品", "開封済みだが使用していない", None)
        assert r.rank_code == "S"
        assert r.ebay_condition_id == "1500"

    def test_excellent(self):
        r = _fallback_classify("美品", "使用に伴う小キズあり", None)
        assert r.rank_code == "A"

    def test_good(self):
        r = _fallback_classify("良品", None, None)
        assert r.rank_code == "B"

    def test_fair_use_marks(self):
        r = _fallback_classify("使用感あり", None, None)
        assert r.rank_code == "C"

    def test_issues_wake_ari(self):
        r = _fallback_classify("訳あり 傷あり", None, None)
        assert r.rank_code == "D"

    def test_power_on_only(self):
        r = _fallback_classify("通電確認のみ", None, None)
        assert r.rank_code == "PO"

    def test_as_is_junk(self):
        r = _fallback_classify("ジャンク", "動作未確認", None)
        assert r.rank_code == "As-Is"
        assert r.ebay_condition_id == "7000"

    def test_as_is_keyword_priority(self):
        # 「美品だがジャンク」は安全側 As-Is を優先する
        r = _fallback_classify("美品", "ジャンク扱い", None)
        assert r.rank_code == "As-Is"

    def test_unknown_defaults_to_as_is_low_confidence(self):
        r = _fallback_classify("", None, None)
        assert r.rank_code == "As-Is"
        assert r.confidence <= 0.4

    def test_haystack_combines_all_inputs(self):
        # title のみに「美品」があっても拾える
        r = _fallback_classify("中古", None, "Sony WH-1000XM5 美品")
        assert r.rank_code == "A"

    # -----------------------------------------------------------------
    # Mercari/Yahoo 標準状態ラベル (2026-04-22 改訂, code-reviewer H1 対応)
    # "目立った傷や汚れなし" "やや傷や汚れあり" "傷や汚れあり" "全体的に状態が悪い"
    # -----------------------------------------------------------------
    def test_mercari_label_medatta_kizu_nashi_classified_as_B(self):
        """メルカリ/ヤフオクの上から3番目「目立った傷や汚れなし」は B (Good)。"""
        r = _fallback_classify("目立った傷や汚れなし", None, None)
        assert r.rank_code == "B"

    def test_mercari_label_yaya_kizu_yogare_ari_classified_as_B(self):
        """「やや傷や汚れあり」は B。C の substring match に奪われないこと (H1 バグ)。"""
        r = _fallback_classify("やや傷や汚れあり", None, None)
        assert r.rank_code == "B"

    def test_mercari_label_kizu_yogare_ari_classified_as_C(self):
        """「傷や汚れあり」(「やや」無し) は C。B よりも劣化が進んだ状態。"""
        r = _fallback_classify("傷や汚れあり", None, None)
        assert r.rank_code == "C"

    def test_mercari_label_zentaitekini_warui_classified_as_D(self):
        """「全体的に状態が悪い」は D (Issues)。"""
        r = _fallback_classify("全体的に状態が悪い", None, None)
        assert r.rank_code == "D"

    def test_chuuko_desuga_kirei_classified_as_B(self):
        """「中古ですが綺麗な方」→ B (ユーザー実例: NDF-1500-U 変圧器)。"""
        r = _fallback_classify(
            "中古ですが綺麗な方だと思います",
            "メキシコで2年程度使用してました",
            None,
        )
        assert r.rank_code == "B"

    def test_positive_signal_fallback_not_as_is(self):
        """キーワード合致なしでも「使用実績」などの動作ヒントがあれば B。
        2026-04-22 改訂: 従来 As-Is default だったのを、動作実績ある場合は B へ。"""
        r = _fallback_classify(
            "状態は普通です",
            "日本の家電で使用実績ありです。動作確認済。",
            None,
        )
        assert r.rank_code == "B"
        assert r.confidence >= 0.5

    def test_pure_unknown_still_defaults_as_is(self):
        """動作実績も 中古 表記も何もない完全不明なら As-Is のまま (安全側)。"""
        r = _fallback_classify("", "hello world", "random english title")
        assert r.rank_code == "As-Is"


# =========================================================================
# _extract_json
# =========================================================================

class TestExtractJson:
    def test_fenced(self):
        text = '```json\n{"a": 1}\n```'
        out = _extract_json(text)
        assert out is not None
        assert json.loads(out) == {"a": 1}

    def test_bare_json(self):
        text = 'prefix {"a": 1} suffix'
        out = _extract_json(text)
        assert out is not None

    def test_truncated_json_repair(self):
        # 閉じ } が欠けているケース
        text = '{"a": 1, "b": "x"'
        out = _extract_json(text)
        assert out is not None
        assert out.endswith("}")

    def test_empty(self):
        assert _extract_json("") is None
        assert _extract_json("no json at all") is None


# =========================================================================
# classify_rank (Claude mock)
# =========================================================================

class TestClassifyRankWithMock:
    def test_claude_returns_rank_A(self):
        resp = _make_claude_response({
            "rank_code": "A",
            "rank_label": "Excellent",
            "rank_jp": "Tested Minor Wear",
            "confidence": 0.92,
            "reasoning": "美品 + 動作確認済",
        })
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        with patch("monitor.rank_classifier._get_client", return_value=fake_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                r = classify_rank("美品", "動作確認済", "Sony WH-1000XM5")
        assert r.rank_code == "A"
        assert r.rank_label == "Excellent"  # _RANK_TABLE から上書き
        assert r.ebay_condition_id == "3000"
        assert r.confidence == 0.92

    def test_claude_invalid_rank_falls_back(self):
        resp = _make_claude_response({
            "rank_code": "Z_INVALID",  # 不正値
            "confidence": 0.9,
            "reasoning": "test",
        })
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        with patch("monitor.rank_classifier._get_client", return_value=fake_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                r = classify_rank("美品", None, None)
        # 不正値 → fallback。「美品」で A に戻る
        assert r.rank_code == "A"

    def test_claude_non_json_response_falls_back(self):
        block = MagicMock()
        block.type = "text"
        block.text = "申し訳ありません、判定できませんでした"
        resp = MagicMock()
        resp.content = [block]
        resp.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        with patch("monitor.rank_classifier._get_client", return_value=fake_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                r = classify_rank("ジャンク", None, None)
        assert r.rank_code == "As-Is"

    def test_claude_api_error_falls_back(self):
        # anthropic モジュールが無くても APIError を模せるよう一般例外で代用
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("boom")

        with patch("monitor.rank_classifier._get_client", return_value=fake_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                r = classify_rank("ジャンク", "動作未確認", None)
        assert r.rank_code == "As-Is"

    def test_no_api_key_fallback(self):
        """ANTHROPIC_API_KEY 未設定時は regex fallback。"""
        with patch("monitor.rank_classifier._get_client", return_value=None):
            r = classify_rank("新品未開封", None, None)
        assert r.rank_code == "N"
        assert r.ebay_condition_id == "1000"


# =========================================================================
# 全 rank の _RANK_TABLE / VALID_RANKS 整合性
# =========================================================================

class TestRankTableIntegrity:
    def test_all_ranks_have_entries(self):
        expected = {"N", "S", "A", "B", "C", "D", "PO", "As-Is"}
        assert set(_RANK_TABLE.keys()) == expected
        assert set(VALID_RANKS) == expected

    def test_ebay_condition_id_mapping(self):
        expected_mapping = {
            "N": "1000",
            "S": "1500",
            "A": "3000",
            "B": "3000",
            "C": "3000",
            "D": "3000",
            "PO": "3000",
            "As-Is": "7000",
        }
        for rank, expected_cond_id in expected_mapping.items():
            _, _, cond_id = _RANK_TABLE[rank]
            assert cond_id == expected_cond_id, f"{rank} → {cond_id} != {expected_cond_id}"

    def test_rank_classification_dataclass_fields(self):
        r = RankClassification(
            rank_code="A",
            rank_label="Excellent",
            rank_jp="Tested \u00b7 Minor Wear",
            ebay_condition_id="3000",
            confidence=0.8,
            reasoning="test",
        )
        assert r.rank_code == "A"
        assert r.confidence == 0.8
