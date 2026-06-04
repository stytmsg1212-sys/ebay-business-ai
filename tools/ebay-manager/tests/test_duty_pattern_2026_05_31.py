"""関税パターン選択 (2026-05-31): 利益計算ツールの ①/②③/④ パターン回帰.

出典: 実測スクショ vs MonoDeck の利益 +¥1,461 過大評価 → US_only 商品 (①) を
②③ (関税を送料に乗せる) として誤計算していたバグの是正。本テストは:

- 後方互換: 旧 is_ddu (False≡②③shipping / True≡④ddu) が計算結果を1円も変えないこと
- ① included: 売上を膨らませず関税を profit から実費1回控除、FVF も item のみ
- ②③ shipping_usd_override: 手入力が効く / 0.0 入力が None と区別される
- override 負値の contract 検証 (ValueError)

を永続固定する (将来 calculator を触っても後方互換が崩れないこと)。
"""
from __future__ import annotations

import pytest

import calculator as C

COMMON = dict(
    purchase_yen=52400, item_price_usd=500.0, weight_g=3000,
    length_cm=20.0, width_cm=15.0, height_cm=10.0,
    category_id=58248, country_code="US",
)


def _settings() -> dict:
    return C.load_settings()


# golden リテラルを捕捉した時点 (2026-05-31) の settings ベースライン。
# exchange_rate / fuel_surcharge は運用で随時更新される (fuel は毎週) ため、live settings の
# まま絶対値を固定すると本テストが運用更新の度に壊れる。captured 時点の値に pin し、本テストを
# 「duty_pattern 分岐コード自体の回帰検知」に純化する (settings drift とは分離)。
# duty_rate も運用で動く config (W215 2026-06-03: 20→11%)。golden HEAD は捕捉時点の 20% で
# 算出済 (shipping_usd=100=item500×0.20) のため duty_rate=20 も pin し、本テストを duty_rate
# 改定から隔離する (live 11% を使うと shipping pattern の revenue/shipping_usd が変わり恒偽化)。
_GOLDEN_SETTINGS_BASELINE = dict(exchange_rate=157, fuel_surcharge_fedex=45.5, fuel_surcharge_dhl=48.0, duty_rate=20.0)


def _golden_settings() -> dict:
    s = C.load_settings()
    s.update(_GOLDEN_SETTINGS_BASELINE)
    return s


def _best_profit(res):
    return max((sr.profit for sr in res.service_results), default=None)


# ── 後方互換 (最重要: 旧 is_ddu 呼び出し元の計算不変) ──

# 変更前 (HEAD) の calculate() を `git show HEAD:...calculator.py` で復元し COMMON 入力で
# 実測した golden 値 (2026-05-31)。is_ddu/duty_pattern 経路の相互比較だけだと両者が同じ
# 新コードパスを通るため恒真に近く、shipping/ddu 分岐自体のリグレッションを検出できない。
# 旧コード由来の数値リテラルで固定し「1円も変わらない」絶対要件をダメ押し検証する。
# (settings は _golden_settings() で捕捉時点に pin 済 = fuel/FX の運用更新で壊れない)
_GOLDEN_HEAD = {
    "shipping": dict(revenue=94200, revenue_net=78500, shipping_usd=100.0,
                     ebay_cost_subtotal=32324, best_profit=3573),
    "ddu": dict(revenue=78500, revenue_net=78500, shipping_usd=0.0,
                ebay_cost_subtotal=13863, best_profit=6664),
}


@pytest.mark.parametrize("pattern", ["shipping", "ddu"])
def test_golden_head_values_unchanged(pattern):
    """変更前 HEAD の実測値リテラルと完全一致 (duty_pattern 分岐の回帰固定).

    settings は捕捉時点ベースラインに pin (exchange_rate / fuel は運用更新で動くため)。
    """
    s = _golden_settings()
    r = C.calculate(C.CalcInput(duty_pattern=pattern, **COMMON), s)
    g = _GOLDEN_HEAD[pattern]
    assert r.revenue == g["revenue"]
    assert r.revenue_net == g["revenue_net"]
    assert r.shipping_usd == g["shipping_usd"]
    assert r.ebay_cost_subtotal == g["ebay_cost_subtotal"]
    assert _best_profit(r) == g["best_profit"]


def test_backcompat_is_ddu_false_equals_shipping():
    s = _settings()
    old = C.calculate(C.CalcInput(is_ddu=False, **COMMON), s)
    new = C.calculate(C.CalcInput(duty_pattern="shipping", **COMMON), s)
    assert old.revenue == new.revenue
    assert old.revenue_net == new.revenue_net
    assert old.shipping_usd == new.shipping_usd
    assert old.ebay_cost_subtotal == new.ebay_cost_subtotal
    assert _best_profit(old) == _best_profit(new)


def test_backcompat_is_ddu_true_equals_ddu():
    s = _settings()
    old = C.calculate(C.CalcInput(is_ddu=True, **COMMON), s)
    new = C.calculate(C.CalcInput(duty_pattern="ddu", **COMMON), s)
    assert old.revenue == new.revenue
    assert old.revenue_net == new.revenue_net
    assert old.shipping_usd == new.shipping_usd
    assert old.ebay_cost_subtotal == new.ebay_cost_subtotal
    assert _best_profit(old) == _best_profit(new)


def test_default_pattern_is_shipping():
    """duty_pattern / is_ddu 未指定 → 従来の ②③ shipping 挙動 (バッチ呼び出し元)."""
    s = _settings()
    default = C.calculate(C.CalcInput(**COMMON), s)
    ship = C.calculate(C.CalcInput(duty_pattern="shipping", **COMMON), s)
    assert default.revenue == ship.revenue
    assert _best_profit(default) == _best_profit(ship)


# ── ① included ──

def test_included_revenue_not_inflated():
    s = _settings()
    inc = C.calculate(C.CalcInput(duty_pattern="included", **COMMON), s)
    # 送料0 (Free)、売上は item のみ (関税で膨らまない)
    assert inc.shipping_usd == 0.0
    assert inc.revenue == inc.revenue_net
    assert inc.revenue == round(COMMON["item_price_usd"] * s["exchange_rate"])


def test_included_duty_charged_once_lowers_profit():
    """① は関税を実費負担するため ②③ より profit が低い (duty_rate>0 前提)."""
    s = _settings()
    assert s["duty_rate"] > 0  # 前提が崩れたら気付けるよう明示
    inc = C.calculate(C.CalcInput(duty_pattern="included", **COMMON), s)
    ship = C.calculate(C.CalcInput(duty_pattern="shipping", **COMMON), s)
    assert _best_profit(inc) < _best_profit(ship)
    # ④ddu (関税0) と ①included は revenue/FVF/送料が同一 (どちらも shipping_usd=0)。
    # 両者の profit 差 = ① が追加負担する関税元本 + 米国関税処理手数料(元本×~2.1%)。
    # 「元本以上、元本×1.10倍未満」で関税が ちょうど1回だけ 計上されたことを固定
    # (二重計上なら ~2倍、取りこぼしなら ~0 になり検出できる)。
    ddu = C.calculate(C.CalcInput(duty_pattern="ddu", **COMMON), s)
    duty_jpy = COMMON["item_price_usd"] * (s["duty_rate"] / 100) * s["exchange_rate"]
    diff = _best_profit(ddu) - _best_profit(inc)
    assert duty_jpy <= diff < duty_jpy * 1.10


# ── ②③ shipping_usd_override ──

def test_shipping_override_zero_distinct_from_none():
    """0.0 入力 (バイヤー徴収0) が None (自動算出) と区別される (is not None 判定)."""
    s = _settings()
    zero = C.calculate(C.CalcInput(duty_pattern="shipping", shipping_usd_override=0.0, **COMMON), s)
    auto = C.calculate(C.CalcInput(duty_pattern="shipping", **COMMON), s)
    assert zero.shipping_usd == 0.0
    assert auto.shipping_usd > 0.0
    assert zero.revenue == round(COMMON["item_price_usd"] * s["exchange_rate"])


def test_shipping_override_explicit_value():
    s = _settings()
    r = C.calculate(C.CalcInput(duty_pattern="shipping", shipping_usd_override=40.0, **COMMON), s)
    assert r.shipping_usd == 40.0
    assert r.revenue == round((COMMON["item_price_usd"] + 40.0) * s["exchange_rate"])


def test_negative_override_raises():
    s = _settings()
    with pytest.raises(ValueError):
        C.calculate(C.CalcInput(duty_pattern="shipping", shipping_usd_override=-10.0, **COMMON), s)


def test_unknown_pattern_raises():
    """未知パターンは silent に CPaSS 関税処理費を 0 に落として profit を過大化するため拒否 (Q0 fail loud)."""
    s = _settings()
    with pytest.raises(ValueError):
        C.calculate(C.CalcInput(duty_pattern="typo_pattern", **COMMON), s)
