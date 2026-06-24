"""600円絶対床を「還付抜き利益」で判定する回帰テスト (2026-06-22 money-direct 是正).

背景 (user 判断):
  check_supplier_candidate_profitable の 600円絶対床は従来 **消費税還付込み利益**
  (profit_with_refund) で判定していた。還付込みだと還付分だけ下駄を履いて、本来不採用の
  薄利候補を採用してしまう (仕入れ=money-direct)。よって 600円床は **還付抜き利益**
  (profit_without_refund) で判定すべき = バグ修正。

  ※スライド利益率床 (10-20%) の分母は現状 還付込みのまま (率の基準は user 判断待ちの論点)。
    本テストは 600円床のみを固定する (率床は意図的に通過する条件で組む)。
"""
from calculator import check_supplier_candidate_profitable


def test_floor600_uses_profit_without_refund_rejects_thin_margin():
    """還付込みなら通る薄利候補を、還付抜き<600円で正しく不採用にする (本修正の核心)."""
    # purchase_yen=5000 → required_rate=0.10 → floor_by_rate=500 → floor_profit=max(600,500)=600
    # 還付込み=650 (>=600 で率床 pass_floor も通過) だが、還付抜き=400 (<600) → 不採用が正。
    ok, breakdown = check_supplier_candidate_profitable(
        profit_with_refund=650,
        purchase_yen=5000,
        profit_without_refund=400,
    )
    assert ok is False, "還付抜き 400円 < 600円床 なので不採用が正"
    assert breakdown["pass_600"] is False, "600円床は還付抜きで判定 → False"
    assert breakdown["pass_floor"] is True, "率床 (600) は還付込み 650 で通過"
    assert breakdown["profit_without_refund"] == 400


def test_old_logic_would_have_adopted_same_case():
    """対照: 旧ロジック (還付込みで 600円床) なら同ケースは採用されていた = 是正の効果確認."""
    # 旧挙動の再現 = profit_without_refund を渡さない (後方互換で還付込みを代用)。
    ok, breakdown = check_supplier_candidate_profitable(
        profit_with_refund=650,
        purchase_yen=5000,
    )
    assert ok is True, "旧ロジック (還付込み 650 >= 600) では採用されてしまっていた"
    assert breakdown["pass_600"] is True


def test_floor600_passes_when_no_refund_profit_above_600():
    """還付抜きでも 600円以上なら採用される (正常採用が退行しないこと)."""
    ok, breakdown = check_supplier_candidate_profitable(
        profit_with_refund=900,
        purchase_yen=5000,
        profit_without_refund=700,
    )
    assert ok is True
    assert breakdown["pass_600"] is True
    assert breakdown["pass_floor"] is True


def test_rate_floor_still_blocks_when_with_refund_below_rate():
    """率床は還付込みのまま (現挙動維持): 還付抜きが 600 超でも率床未達なら不採用."""
    # purchase_yen=100000 → required_rate=0.20 → floor_by_rate=20000 → floor_profit=20000
    # 還付込み=15000 (<20000 で率床 NG)、還付抜き=12000 (>=600 で 600床は通過)。
    ok, breakdown = check_supplier_candidate_profitable(
        profit_with_refund=15000,
        purchase_yen=100000,
        profit_without_refund=12000,
    )
    assert ok is False, "率床 (還付込み 15000 < 20000) で不採用"
    assert breakdown["pass_600"] is True, "600円床は還付抜き 12000 で通過"
    assert breakdown["pass_floor"] is False, "率床は還付込み 15000 で未達"
    assert breakdown["floor_profit"] == 20000


def test_breakdown_exposes_both_profit_values():
    """内訳 dict が還付込み/抜きの両値を返す (監査・UI 表示用)."""
    _ok, breakdown = check_supplier_candidate_profitable(
        profit_with_refund=1234,
        purchase_yen=8000,
        profit_without_refund=789,
    )
    assert breakdown["profit_with_refund"] == 1234
    assert breakdown["profit_without_refund"] == 789
