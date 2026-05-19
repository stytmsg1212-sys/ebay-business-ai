"""2026-05-19 user 指示回帰: データFIX 判定で 0 と空白(None) を区別。

US_only 送料等を **あえて 0** に設定する運用があるため、0 は「設定済み」、
DB NULL→None のみ未FIX。旧実装は falsy `not x` で 0 も未設定扱い = 誤判定。
対象: tabs.tab_data_fix._render_status_chips / _compute_stats
(_apply_filter は streamlit widget 依存のため本 unit では対象外、同一
`is None` 規約に揃えて改修済)。
"""
from __future__ import annotations

from tabs.tab_data_fix import _render_status_chips, _compute_stats


def test_zero_is_set_not_unfixed():
    it = {"purchase_yen": 0, "weight_g": 0, "length_cm": 0,
          "width_cm": 0, "height_cm": 0, "lp_breakeven_usd": 0}
    assert _render_status_chips(it) == "✅ 全 FIX 済", \
        "0 を未設定扱いした (US_only 等の意図的 0 が誤って未FIX)"


def test_none_is_unfixed():
    it = {"purchase_yen": None, "weight_g": None, "length_cm": None,
          "width_cm": None, "height_cm": None, "lp_breakeven_usd": None}
    chips = _render_status_chips(it)
    for kw in ("仕入¥", "重量", "寸法", "breakeven"):
        assert kw in chips, f"None なのに {kw} が未FIX判定されていない"


def test_partial_dims_none_flags_dim_only():
    it = {"purchase_yen": 100, "weight_g": 50, "length_cm": 0,
          "width_cm": None, "height_cm": 0, "lp_breakeven_usd": 5}
    chips = _render_status_chips(it)
    assert "寸法" in chips, "width=None なら寸法は未FIX (3つ全部 not-None 必要)"
    assert "仕入¥" not in chips and "重量" not in chips \
        and "breakeven" not in chips, "0/正値の項目を誤って未FIX判定"


def test_stats_count_zero_as_done():
    lst = [{"purchase_yen": 0, "weight_g": 0, "length_cm": 0,
            "width_cm": 0, "height_cm": 0, "lp_breakeven_usd": 0,
            "lp_min_price": 0}]
    s = _compute_stats(lst)
    assert (s["pyen_done"] == 1 and s["weight_done"] == 1
            and s["dim_done"] == 1 and s["breakeven_done"] == 1
            and s["min_price_done"] == 1), \
        f"統計が 0 を未設定扱い: {s}"


def test_stats_count_none_as_not_done():
    lst = [{"purchase_yen": None, "weight_g": None, "length_cm": None,
            "width_cm": None, "height_cm": None, "lp_breakeven_usd": None,
            "lp_min_price": None}]
    s = _compute_stats(lst)
    assert (s["pyen_done"] == 0 and s["weight_done"] == 0
            and s["dim_done"] == 0 and s["breakeven_done"] == 0
            and s["min_price_done"] == 0), f"None を設定済み誤計上: {s}"
