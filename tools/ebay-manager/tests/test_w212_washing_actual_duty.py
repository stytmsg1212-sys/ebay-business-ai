"""W212 washing 修正のテスト (2026-06-02).

calculator.calculate の actual_duty_rate (opt-in) が:
1. None なら legacy 挙動と完全一致 (後方互換)
2. 設定時は seller 実関税を分離計上し、Section 232 (実関税 > display) で profit が
   過大計上されず正しく下がる
ことを担保。legacy 全体の不変は test_duty_pattern_2026_05_31.py が担保済。
"""
import calculator as C


def _inp(actual=None, pattern="shipping", price=600.0):
    return C.CalcInput(
        purchase_yen=65000,
        item_price_usd=price,
        weight_g=3000,
        category_id=58248,
        duty_pattern=pattern,
        actual_duty_rate=actual,
    )


def _best_profit(res):
    return max(r.profit for r in res.service_results)


def test_actual_none_is_legacy_shipping():
    """actual_duty_rate=None かつ actual=display率 が一致 = washing と整合."""
    s = C.load_settings()
    disp = s["duty_rate"] / 100  # 0.20
    p_none = _best_profit(C.calculate(_inp(None), s))
    p_disp = _best_profit(C.calculate(_inp(disp), s))
    assert abs(p_none - p_disp) < 1.0, "actual=display率は washing(None)と一致すべき"


def test_section232_ib_reduces_profit_by_exact_delta():
    """I-B 30%: profit_new = profit_legacy + item*fx*(display - actual)."""
    s = C.load_settings()
    fx = s["exchange_rate"]
    disp = s["duty_rate"] / 100
    p_base = _best_profit(C.calculate(_inp(None), s))
    p_232 = _best_profit(C.calculate(_inp(0.30), s))
    expected_delta = 600.0 * fx * (disp - 0.30)  # 負値 (利益減)
    assert abs((p_232 - p_base) - expected_delta) < 2.0
    assert p_232 < p_base, "Section232 該当は実関税>displayで利益が下がる"


def test_ia_55_worse_than_ib_30():
    """I-A 55% は I-B 30% よりさらに利益が低い (純金属の重課税)."""
    s = C.load_settings()
    p_ib = _best_profit(C.calculate(_inp(0.30), s))
    p_ia = _best_profit(C.calculate(_inp(0.55), s))
    assert p_ia < p_ib


def test_included_pattern_uses_actual_duty():
    """included(US_only)でも actual 設定時は実関税で seller 実費が増える."""
    s = C.load_settings()
    p_base = _best_profit(C.calculate(_inp(None, pattern="included"), s))
    p_232 = _best_profit(C.calculate(_inp(0.30, pattern="included"), s))
    assert p_232 < p_base, "included でも実関税>displayで利益減"


def test_ddu_unaffected_by_actual_duty():
    """ddu(US以外)は関税なし = actual_duty_rate を設定しても profit 不変."""
    s = C.load_settings()
    p_none = _best_profit(C.calculate(_inp(None, pattern="ddu"), s))
    p_set = _best_profit(C.calculate(_inp(0.50, pattern="ddu"), s))
    assert abs(p_none - p_set) < 1.0, "ddu は関税ゼロなので actual の影響を受けない"


def test_negative_actual_duty_rate_rejected():
    """負の actual_duty_rate は fail-loud (profit 不正過大化を防ぐ、2段レビュー指摘)."""
    import pytest
    s = C.load_settings()
    with pytest.raises(ValueError):
        C.calculate(_inp(-0.10), s)


def test_ebay_cost_subtotal_reflects_actual_duty():
    """actual 設定時、合計コスト集計も実関税を反映 (profit と表示の不整合防止)."""
    s = C.load_settings()
    base = C.calculate(_inp(None), s)
    s232 = C.calculate(_inp(0.30), s)
    # I-B 30% は display 20% より関税コストが大きい → 集計コストも増える
    assert s232.ebay_cost_subtotal > base.ebay_cost_subtotal


def test_shipping_override_with_actual_duty():
    """shipping_usd_override 併用時も profit が壊れない (override=buyer徴収 / actual=実関税)."""
    s = C.load_settings()
    inp = C.CalcInput(
        purchase_yen=65000, item_price_usd=600.0, weight_g=3000,
        category_id=58248, duty_pattern="shipping",
        shipping_usd_override=150.0, actual_duty_rate=0.30,
    )
    res = C.calculate(inp, s)
    assert res.service_results, "計算が成立すること"
    # override(buyer徴収150) は revenue/FVF に効き、actual(実関税0.30)は別途 cost 計上
    assert res.shipping_usd == 150.0
