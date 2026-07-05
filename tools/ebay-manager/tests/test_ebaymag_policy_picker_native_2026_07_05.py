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
    _decide_save_button,
    _match_policy_option_indices,
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
        """canary#3 実測: 候補行の区切りは改行 ("token\\n説明文")。"""
        visible_texts = [
            "MAG_6-8kg_1day\n無料, 6 日でお届け, 営業日 60 受入を返します",
            "MAG_6-8kg_7day\n無料, 12 日でお届け, 営業日 60 受入を返します",
        ]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_6-8kg_1day")
            == "UNIQUE"
        )

    def test_space_separated_row_still_unique(self):
        """空白区切りの行 (旧想定) も引き続き UNIQUE (語単位一致は split() 採用)。"""
        visible_texts = ["MAG_6-8kg_1day 1日以内発送・6-8kg"]
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
        visible_texts = ["MAG_1-2kg_7day\n無料, 12 日でお届け, 営業日 60 受入を返します"]
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

    def test_icon_prefixed_row_is_unique(self):
        """canary#5 実測: 候補行の先頭にアイコン文字 'M'+改行が付く
        ('M\\nMAG_1-2kg_1day\\n無料, ...')。語単位一致なので UNIQUE になる。"""
        visible_texts = ["M\nMAG_1-2kg_1day\n無料, 6 日でお届け, 営業日 60 受入を返します"]
        assert (
            _decide_policy_option_selection(visible_texts, "MAG_1-2kg_1day")
            == "UNIQUE"
        )


class TestMatchPolicyOptionIndices:
    """クリック対象 index の特定 (H1: visible_handles[0] 固定の誤選択防止)。"""

    def test_returns_exact_match_index_not_first_row(self):
        """prefix 衝突行 (_v2) が先頭でも、exact 一致行の index を返す。"""
        visible_texts = [
            "M\nMAG_6-8kg_1day_v2\n説明",
            "M\nMAG_6-8kg_1day\n説明",
        ]
        assert _match_policy_option_indices(visible_texts, "MAG_6-8kg_1day") == [1]

    def test_returns_all_matching_indices(self):
        visible_texts = [
            "MAG_6-8kg_1day 説明A",
            "MAG_6-8kg_7day 説明",
            "MAG_6-8kg_1day 説明B",
        ]
        assert _match_policy_option_indices(visible_texts, "MAG_6-8kg_1day") == [0, 2]

    def test_empty_when_no_word_match(self):
        assert _match_policy_option_indices(["MAG_6-8kg_1day_v2 説明"], "MAG_6-8kg_1day") == []


class TestDecideSaveButton:
    """「N 変動 を保存」保存ボタン判定 (canary#6 probe 実測 2026-07-05)。
    N==1 のみ許可 = 複数の未保存変更を巻き込み保存しない (2026-06-21 merge 事故教訓)。"""

    def test_single_change_button_is_clickable(self):
        texts = ["キャンセル", "1 変動 を保存"]
        assert _decide_save_button(texts) == "SAVE:1"

    def test_multiple_pending_changes_aborts(self):
        """N>=2 = 自分以外の未保存変更が存在 → 巻き込み保存を拒否。"""
        texts = ["キャンセル", "2 変動 を保存"]
        assert _decide_save_button(texts) == "PENDING_CHANGES:2"

    def test_no_save_button_returns_not_found(self):
        """抽出不能 (該当ボタンなし) → NOT_FOUND (呼び出し側で render race poll)。"""
        texts = ["キャンセル", "保存", "変動を確認"]
        assert _decide_save_button(texts) == "SAVE_BUTTON_NOT_FOUND"

    def test_multiple_save_buttons_abort(self):
        texts = ["1 変動 を保存", "1 変動 を保存"]
        assert _decide_save_button(texts) == "MULTIPLE_SAVE_BUTTONS:2"

    def test_no_space_variant_matches(self):
        """「1変動を保存」(空白なし表記ゆれ) も許可。"""
        assert _decide_save_button(["1変動を保存"]) == "SAVE:0"


class TestPickerSelectors:
    """picker 用 CSS selector 定数の内容固定化 (誤編集時の回帰検知)。"""

    def test_input_selector_targets_placeholder(self):
        assert "配送ポリシー" in POLICY_PICKER_INPUT_SELECTOR
        assert POLICY_PICKER_INPUT_SELECTOR.startswith("input[")

    def test_candidate_selector_is_button_row(self):
        """canary#3 実測 (2026-07-05): 候補行は BUTTON > DIV.label 構造
        (role=option/menuitem/li は実在しない) のため button 単独に固定。"""
        assert POLICY_OPTION_CANDIDATE_SELECTOR == "button"
