"""W217 (2026-06-03): 商品管理 UI ヘルパー 3 純関数の回帰.

採算パネルと同じ思想で 3 ゾーン整理した際に追加した純関数 (Streamlit 非依存):
- _kw_state_badge — ライバル監視「検索ワード」状態バッジ (🟢 / 🟡 / ⚪)
- _lowest_rival_marker — 登録済 active ライバル dataframe の最安行に 🥇 付与
- _competitor_gap_line — 自社総額 vs 競合最安 → 競合差 1 行 HTML

各関数は session_state / pricing_rows / 競合最安 のみ参照する純関数で、
保存ロジック / 競合データの DB 書込 / dirty-flag 機構には一切触れない。
"""
from __future__ import annotations


# -----------------------------------------------------------------------------
# _kw_state_badge
# -----------------------------------------------------------------------------

def test_kw_state_badge_idle_when_db_empty_no_ui():
    """DB 値が空 + UI 未描画 (session_state に key 不在) → ⚪ 未設定."""
    from tabs.tab_product_management import _kw_state_badge
    badge = _kw_state_badge("EID1", "", {})
    assert "pm-kw-badge-idle" in badge
    assert "⚪" in badge


def test_kw_state_badge_idle_when_both_empty():
    """DB 値が空 + UI 入力も空 → ⚪ 未設定."""
    from tabs.tab_product_management import _kw_state_badge
    ss = {"pm_rival_kw_EID1": ""}
    badge = _kw_state_badge("EID1", "", ss)
    assert "pm-kw-badge-idle" in badge


def test_kw_state_badge_ok_when_db_only_no_ui():
    """DB 値あり + UI 未描画 (session_state に key 不在) → 🟢 cron 反映済.

    UI 未描画の場合は DB 値が cron に渡るので OK 扱い (バッジは DB の状態を映す)。
    """
    from tabs.tab_product_management import _kw_state_badge
    badge = _kw_state_badge("EID1", "maxell MXCP-P100", {})
    assert "pm-kw-badge-ok" in badge
    assert "🟢" in badge


def test_kw_state_badge_ok_when_ui_matches_db():
    """DB 値 = UI 編集値 (一致) → 🟢 cron 反映済."""
    from tabs.tab_product_management import _kw_state_badge
    ss = {"pm_rival_kw_EID1": "maxell MXCP-P100"}
    badge = _kw_state_badge("EID1", "maxell MXCP-P100", ss)
    assert "pm-kw-badge-ok" in badge


def test_kw_state_badge_warn_when_ui_diverges():
    """DB 値 != UI 編集値 (未保存) → 🟡 未保存."""
    from tabs.tab_product_management import _kw_state_badge
    ss = {"pm_rival_kw_EID1": "maxell MXCP-P100 Player"}
    badge = _kw_state_badge("EID1", "maxell MXCP-P100", ss)
    assert "pm-kw-badge-warn" in badge
    assert "🟡" in badge


def test_kw_state_badge_normalizes_whitespace():
    """legacy \\n 区切り data + 余分な空白を normalize して比較."""
    from tabs.tab_product_management import _kw_state_badge
    # DB は legacy \n 区切り (空白に normalize)、UI は半角空白 1 個 → 一致扱い
    ss = {"pm_rival_kw_EID1": "a b c"}
    badge = _kw_state_badge("EID1", "a\nb  c", ss)
    assert "pm-kw-badge-ok" in badge


# -----------------------------------------------------------------------------
# _lowest_rival_marker
# -----------------------------------------------------------------------------

def test_lowest_rival_marker_empty_input():
    """空リスト入力 → そのまま空リスト返却."""
    from tabs.tab_product_management import _lowest_rival_marker
    assert _lowest_rival_marker([]) == []


def test_lowest_rival_marker_picks_min_total():
    """合計 (total) 最小の行に 🥇 を付与."""
    from tabs.tab_product_management import _lowest_rival_marker
    rows = [
        {"item id": "111", "合計": 138.0},
        {"item id": "222", "合計": 100.0},  # 最安
        {"item id": "333", "合計": 120.0},
    ]
    out = _lowest_rival_marker(rows)
    assert len(out) == 3
    assert out[0]["item id"] == "111"  # 非最安はそのまま
    assert out[1]["item id"].startswith("🥇")  # 最安に絵文字
    assert "222" in out[1]["item id"]
    assert out[2]["item id"] == "333"


def test_lowest_rival_marker_skips_none_total():
    """合計 None の行は最安対象外、数値行のみで判定."""
    from tabs.tab_product_management import _lowest_rival_marker
    rows = [
        {"item id": "111", "合計": None},  # 対象外
        {"item id": "222", "合計": 100.0},  # 最安
        {"item id": "333", "合計": 120.0},
    ]
    out = _lowest_rival_marker(rows)
    assert out[0]["item id"] == "111"
    assert out[1]["item id"].startswith("🥇")
    assert out[2]["item id"] == "333"


def test_lowest_rival_marker_all_none_no_marker():
    """全行 合計 None → 何も付与しない (None 安全)."""
    from tabs.tab_product_management import _lowest_rival_marker
    rows = [
        {"item id": "111", "合計": None},
        {"item id": "222", "合計": None},
    ]
    out = _lowest_rival_marker(rows)
    assert all(not r["item id"].startswith("🥇") for r in out)


def test_lowest_rival_marker_does_not_double_mark():
    """既に 🥇 が付いている item id を二重マークしない (idempotent)."""
    from tabs.tab_product_management import _lowest_rival_marker
    rows = [
        {"item id": "🥇 222", "合計": 100.0},
        {"item id": "333", "合計": 120.0},
    ]
    out = _lowest_rival_marker(rows)
    # "🥇 222" は既に🥇付きなのでそのまま (二重付与しない)
    assert out[0]["item id"] == "🥇 222"


# -----------------------------------------------------------------------------
# _competitor_gap_line
# -----------------------------------------------------------------------------

def test_competitor_gap_line_none_competitor_empty_string():
    """競合最安 None → 空文字列 (render しない)."""
    from tabs.tab_product_management import _competitor_gap_line
    assert _competitor_gap_line(140.0, None) == ""


def test_competitor_gap_line_invalid_competitor_empty_string():
    """競合最安 が float 化不可 → 空文字列 (例外で UI が落ちない)."""
    from tabs.tab_product_management import _competitor_gap_line
    assert _competitor_gap_line(140.0, "not-a-number") == ""


def test_competitor_gap_line_own_higher_bad_class():
    """自社 > 競合最安 → 🔴 劣位 (pm-gap-line-bad)."""
    from tabs.tab_product_management import _competitor_gap_line
    html = _competitor_gap_line(140.0, 100.0)
    assert "pm-gap-line-bad" in html
    assert "🔴" in html
    assert "劣位" in html
    assert "$140" in html
    assert "$100" in html


def test_competitor_gap_line_own_lower_ok_class():
    """自社 < 競合最安 → 🟢 有利 (pm-gap-line-ok)."""
    from tabs.tab_product_management import _competitor_gap_line
    html = _competitor_gap_line(90.0, 100.0)
    assert "pm-gap-line-ok" in html
    assert "🟢" in html
    assert "有利" in html


def test_competitor_gap_line_equal_eq_class():
    """自社 = 競合最安 → ⚪ 同 (pm-gap-line-eq)."""
    from tabs.tab_product_management import _competitor_gap_line
    html = _competitor_gap_line(100.0, 100.0)
    assert "pm-gap-line-eq" in html
    assert "⚪" in html
