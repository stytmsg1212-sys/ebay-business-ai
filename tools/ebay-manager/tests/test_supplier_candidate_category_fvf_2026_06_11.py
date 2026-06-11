"""_estimate_profit_for_candidate の category_id 配線テスト (2026-06-11).

W222 で ebay_listings.category_id (v65) が入ったが、仕入先候補の利益計算は
category_id=0 固定 = 既定 FVF レートのままだった。listing の実カテゴリを
CalcInput に渡す配線 (user 要望) を回帰保護する。
"""
import tasks.task_supplier_candidate_search as t


_LISTING_BASE = {
    "sku": "stock01",
    "current_price": 100.0,
    "weight_g": 500,
    "length_cm": 10,
    "width_cm": 10,
    "height_cm": 10,
}


def _capture_calc_input(monkeypatch):
    """calculate を差し替えて CalcInput を捕捉する (実計算はしない)."""
    captured = {}

    def fake_calculate(inp, settings):
        captured["inp"] = inp

        class _EmptyResult:
            service_results = []

        return _EmptyResult()

    monkeypatch.setattr(t, "calculate", fake_calculate)
    return captured


def test_listing_category_id_passed_to_calc_input(monkeypatch):
    cap = _capture_calc_input(monkeypatch)
    t._estimate_profit_for_candidate(
        listing={**_LISTING_BASE, "category_id": 625},
        purchase_yen=8000,
        settings={},
    )
    assert cap["inp"].category_id == 625


def test_category_id_missing_falls_back_to_zero(monkeypatch):
    # category_id 列が dict に無い (旧 caller / backfill 前 listing)
    cap = _capture_calc_input(monkeypatch)
    t._estimate_profit_for_candidate(
        listing=dict(_LISTING_BASE),
        purchase_yen=8000,
        settings={},
    )
    assert cap["inp"].category_id == 0


def test_category_id_none_falls_back_to_zero(monkeypatch):
    # DB 値 NULL → None
    cap = _capture_calc_input(monkeypatch)
    t._estimate_profit_for_candidate(
        listing={**_LISTING_BASE, "category_id": None},
        purchase_yen=8000,
        settings={},
    )
    assert cap["inp"].category_id == 0
