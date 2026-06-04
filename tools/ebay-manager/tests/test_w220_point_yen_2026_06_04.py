"""W220 point_yen (per-listing ポイント実額) のテスト (2026-06-04).

calculator.calculate の point_yen (opt-in) が:
1. None なら従来の global rate (purchase_yen × point_reward_rate) = 後方互換
2. 明示額なら point_return = point_yen で確定 (仕入先/カードで還元率が違う対応)
3. 0 なら 0 確定 (ポイント無しの明示、global rate に fallback しない)
ことを担保。money-direct (採算パネルの手取り判断材料に直結) なので regression test 必須。
出典: 2026-06-04 W220 code-reviewer HIGH-1。
"""
import calculator as C


def _inp(point_yen=None):
    return C.CalcInput(
        purchase_yen=10000,
        item_price_usd=80.0,
        weight_g=300,
        category_id=58248,
        point_yen=point_yen,
    )


def test_point_yen_none_uses_global_rate():
    """point_yen=None は従来 purchase_yen × point_reward_rate に一致 (後方互換)."""
    s = C.load_settings()
    res = C.calculate(_inp(None), s)
    expected = round(10000 * s["point_reward_rate"] / 100)
    assert res.point_return == expected


def test_point_yen_explicit_overrides_global():
    """point_yen 明示時は point_return = point_yen で確定 (global rate を上書き)."""
    s = C.load_settings()
    res = C.calculate(_inp(777), s)
    assert res.point_return == 777


def test_point_yen_zero_is_zero_not_fallback():
    """point_yen=0 は 0 確定 (ポイント無し明示、global rate に fallback しない)."""
    s = C.load_settings()
    res = C.calculate(_inp(0), s)
    assert res.point_return == 0
