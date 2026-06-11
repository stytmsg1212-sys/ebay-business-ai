# -*- coding: utf-8 -*-
"""個別出品タブの STEP ラベル DRY ヘルパー unit test (2026-06-04).

`tabs/tab_individual_listing.py` の `_il_step_label` が、置換前の 8 か所のインライン
markdown と HTML レベルで完全一致することを検証する。見た目不変の DRY 化を担保する目的。
"""
from __future__ import annotations

import pytest

from tabs.tab_individual_listing import _il_step_label


# 置換前の 8 か所と HTML レベルで完全一致すべき期待値。
# (num, title_en, margin) -> expected HTML.
# 既存リテラルから一字一句コピーして起こした (margin の差分含む)。
# 2026-06-12: W261-fix (92b0f4a) の Neumorphic Cream 化で color が
# rgba(180,220,255,0.55) → #8d927f に変更されたため期待値を追従。
_LEGACY_LABELS: list[tuple[tuple[str, str, str], str]] = [
    (
        ("1", "S O U R C E &nbsp; U R L", "4px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:4px 0 6px;">'
        "S T E P &nbsp; 1 &nbsp; — &nbsp; S O U R C E &nbsp; U R L</div>",
    ),
    (
        ("2", "S C R A P E D &nbsp; D A T A", "16px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:16px 0 6px;">'
        "S T E P &nbsp; 2 &nbsp; — &nbsp; S C R A P E D &nbsp; D A T A</div>",
    ),
    (
        ("2.5", "B R A N D &nbsp; H E R O &nbsp; C O M P O S E", "20px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:20px 0 6px;">'
        "S T E P &nbsp; 2 . 5 &nbsp; — &nbsp; "
        "B R A N D &nbsp; H E R O &nbsp; C O M P O S E</div>",
    ),
    (
        (
            "2.6",
            "O T H E R &nbsp; I M A G E S &nbsp; B A C K G R O U N D &nbsp; U N I F Y",
            "18px 0 6px",
        ),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:18px 0 6px;">'
        "S T E P &nbsp; 2 . 6 &nbsp; — &nbsp; "
        "O T H E R &nbsp; I M A G E S &nbsp; "
        "B A C K G R O U N D &nbsp; U N I F Y</div>",
    ),
    (
        ("2.7", "E B A Y &nbsp; E P S &nbsp; U P L O A D", "18px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:18px 0 6px;">'
        "S T E P &nbsp; 2 . 7 &nbsp; — &nbsp; "
        "E B A Y &nbsp; E P S &nbsp; U P L O A D</div>",
    ),
    (
        ("3", "L I S T I N G &nbsp; S E T T I N G S", "16px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:16px 0 6px;">'
        "S T E P &nbsp; 3 &nbsp; — &nbsp; L I S T I N G &nbsp; S E T T I N G S</div>",
    ),
    (
        ("4", "G E N E R A T E D &nbsp; P R E V I E W", "16px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:16px 0 6px;">'
        "S T E P &nbsp; 4 &nbsp; — &nbsp; G E N E R A T E D &nbsp; P R E V I E W</div>",
    ),
    (
        ("5", "V E R I F Y &nbsp; &amp; &nbsp; S A V E", "16px 0 6px"),
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin:16px 0 6px;">'
        "S T E P &nbsp; 5 &nbsp; — &nbsp; V E R I F Y &nbsp; &amp; &nbsp; S A V E</div>",
    ),
]


@pytest.mark.parametrize("args,expected", _LEGACY_LABELS)
def test_il_step_label_matches_legacy_html(
    args: tuple[str, str, str], expected: str
) -> None:
    """置換前の 8 か所と HTML レベルで完全一致 (見た目不変 DRY 化の担保)."""
    num, title_en, margin = args
    got = _il_step_label(num, title_en, margin=margin)
    assert got == expected, (
        f"\nnum={num!r}\nexpected:\n  {expected!r}\ngot:\n  {got!r}"
    )


def test_il_step_label_default_margin() -> None:
    """margin 省略時のデフォルトが既存標準値 (16px 0 6px) と一致."""
    got = _il_step_label("9", "X")
    assert "margin:16px 0 6px;" in got
    # 連結時のスペース挿入も確認 (1 文字番号 / 1 文字タイトル).
    assert "S T E P &nbsp; 9 &nbsp; — &nbsp; X</div>" in got


def test_il_step_label_spaces_multi_char_num() -> None:
    """複数文字 (2.5 など) の番号も 1 文字ずつスペース連結."""
    got = _il_step_label("2.5", "A")
    assert "S T E P &nbsp; 2 . 5 &nbsp; — &nbsp; A</div>" in got


def test_il_step_label_returns_str() -> None:
    """戻り値が str (st.markdown に渡せる型)."""
    assert isinstance(_il_step_label("1", "X"), str)
