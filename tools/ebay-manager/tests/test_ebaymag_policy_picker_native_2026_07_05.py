# -*- coding: utf-8 -*-
"""assign_policy の picker 操作修正 (2026-07-05) の回帰テスト。

live probe で確定した真因:
  1. 「別のポリシーを選択」押下後、typeahead 入力欄 (placeholder に
     「配送ポリシー」を含む) を native click して初めて候補が描画される。
  2. 候補行の innerText は「token + 説明文」の複合のため、完全一致は
     候補描画後も成立しない (contains 判定が必要)。

CDP/ブラウザ操作そのものは live 実行禁止のため、切り出した純関数
`_decide_policy_option_selection` (可視候補 0/1/複数件の判定 = 誤選択
防止の安全弁) のみを unit test 対象にする。他の CDP 依存部分は
既存の ebaymag テスト群 (higher layer で assign_policy 自体を mock) で
回帰確認する。
"""
from __future__ import annotations

from monitor.ebaymag_driver import (
    _decide_policy_option_selection,
    POLICY_PICKER_INPUT_SELECTOR,
    POLICY_OPTION_CANDIDATE_SELECTOR,
)


class TestDecidePolicyOptionSelection:
    def test_zero_matches_returns_not_found(self):
        visible_texts = ["MAG_2-3kg_1day 説明文", "MAG_2-3kg_7day 説明文"]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "OPTION_NOT_FOUND"
        )

    def test_empty_list_returns_not_found(self):
        assert _decide_policy_option_selection([], "MAG_6-8kg_1day") == "OPTION_NOT_FOUND"

    def test_single_match_returns_unique(self):
        visible_texts = [
            "MAG_6-8kg_1day 1日以内発送・6-8kg",
            "MAG_6-8kg_7day 7日以内発送・6-8kg",
        ]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "UNIQUE"
        )

    def test_multiple_matches_returns_ambiguous_with_count(self):
        """token が他候補の innerText にも部分一致してしまうケース (誤選択防止)。"""
        visible_texts = [
            "MAG_6-8kg_1day 1日以内発送・6-8kg",
            "MAG_6-8kg_1day (旧) 1日以内発送・6-8kg",
        ]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "AMBIGUOUS:2"
        )

    def test_exact_row_text_with_description_suffix_matches(self):
        """候補行 innerText = token + 説明文の複合でも contains 判定で 1 件確定できる
        (live probe が確認した『完全一致は成立しない』ケースの回帰)。"""
        visible_texts = ["MAG_1-2kg_7day 7日以内発送・1-2kg・DDP対応"]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_1-2kg_7day")
            == "UNIQUE"
        )

    def test_leading_token_mismatch_rejected_even_when_single_candidate(self):
        """Codex 指摘 (2026-07-05): target を部分文字列に含む別ポリシー行が 1 件だけ
        可視の場合 (leading token 完全一致でない)、UNIQUE にせず OPTION_NOT_FOUND
        にする (誤選択穴を閉じる)。"""
        visible_texts = ["MAG_6-8kg_1day_v2 説明"]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "OPTION_NOT_FOUND"
        )

    def test_leading_token_exact_match_no_suffix_is_unique(self):
        """行 = token ちょうど (説明文なし) も UNIQUE として通す (境界ケース)。"""
        visible_texts = ["MAG_6-8kg_1day"]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "UNIQUE"
        )


class TestPickerSelectors:
    """picker 用 CSS selector 定数の内容固定化 (誤編集時の回帰検知)。"""

    def test_input_selector_targets_placeholder(self):
        assert "配送ポリシー" in POLICY_PICKER_INPUT_SELECTOR
        assert POLICY_PICKER_INPUT_SELECTOR.startswith("input[")

    def test_candidate_selector_covers_option_and_list_roles(self):
        assert "option" in POLICY_OPTION_CANDIDATE_SELECTOR
        assert "menuitem" in POLICY_OPTION_CANDIDATE_SELECTOR
