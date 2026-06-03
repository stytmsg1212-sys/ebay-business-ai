"""W147 (2026-05-19): 商品管理 hero 利益サマリ表示の回帰.

ROADMAP の「calculator 算出済 = 表示のみ」は半分のみ正 (実コードは
breakeven 経由で 1 区分しか hero に出していなかった)。W147 は
calculator.calculate を is_ddu 切替で 2 回呼び「還付あり/なし ×
USA向け(DDP)/US以外(DDU)」を hero に可視化する表示専用ロジック。
**計算式は不変・eBay/DB 非書込** をテストで担保する。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def tpm(monkeypatch):
    import streamlit as st

    class _S(dict):
        pass
    ss = _S()
    monkeypatch.setattr(st, "session_state", ss)
    from tabs import tab_product_management as _tpm
    _tpm._cd_profit_breakdown.clear()
    yield _tpm, ss
    _tpm._cd_profit_breakdown.clear()


_P = {
    "ebay_item_id": "ITEM1", "current_price": 80.0,
    "purchase_yen": 9000, "weight_g": 300,
    "length_cm": 10, "width_cm": 10, "height_cm": 10,
    "primary_market": "US_only",
}


def _svc(profit, pwr, tax):
    return SimpleNamespace(profit=profit, profit_with_refund=pwr,
                           tax_refund=tax)


def _result(services, shipping_usd):
    return SimpleNamespace(service_results=services,
                           shipping_usd=shipping_usd)


# ── _profit_breakdown (入力解決層) ──

def test_missing_purchase_yen_returns_none(tpm):
    """仕入価格欠落 → None (calculator を呼ばない・hero は未入力維持)."""
    _tpm, _ = tpm
    p = dict(_P)
    p["purchase_yen"] = None
    with patch.object(_tpm, "_cd_profit_breakdown") as m:
        assert _tpm._profit_breakdown(p) is None
    m.assert_not_called()


def test_missing_weight_returns_none(tpm):
    """重量欠落 → None."""
    _tpm, _ = tpm
    p = dict(_P)
    p["weight_g"] = None
    with patch.object(_tpm, "_cd_profit_breakdown") as m:
        assert _tpm._profit_breakdown(p) is None
    m.assert_not_called()


def test_session_state_overrides_db_and_market_normalized(tpm):
    """編集フォーム入力 (仕入/重量) が DB を上書き・primary_market 正規化."""
    _tpm, ss = tpm
    ss["pm_pyen_ITEM1"] = 4500
    ss["pm_weight_ITEM1"] = 150
    captured = {}

    def _fake(price, pyen, wt, ln, wd, ht, cat, smt, dbv, adr=None):
        # W212: actual_duty_rate(adr) を 10 引数目に追加 (Section232 per-listing 配線)
        captured.update(price=price, pyen=pyen, wt=wt, cat=cat, smt=smt, adr=adr)
        return {"refund_us": 1, "refund_nonus": 2, "noref_us": 3,
                "noref_nonus": 4, "tax_refund": 5, "ddp_cost_jpy": 6}

    expected_mtime = _tpm._SETTINGS_FILE.stat().st_mtime
    with patch.object(_tpm, "_cd_profit_breakdown", side_effect=_fake):
        out = _tpm._profit_breakdown(dict(_P))
    assert captured["pyen"] == 4500.0 and captured["wt"] == 150.0
    assert captured["price"] == 80.0          # form 価格なし → DB current_price
    assert captured["cat"] == 58248           # category_id 欠落時の既存既定値
    # settings.json mtime が cache key 引数へ流れる (breakeven との 3 秒
    # 非対称を消す fix の配線担保。st.cache_data 内部挙動には依存しない)。
    assert captured["smt"] == expected_mtime
    assert out["primary_market"] == "us_only"  # "US_only" → 正規化
    assert out["refund_us"] == 1


# ── _cd_profit_breakdown (calculator マッピング層) ──

def test_two_calls_ddp_then_ddu_and_mapping(tpm):
    """calculate を DDP(is_ddu=False)→DDU(is_ddu=True) の 2 回呼び、
    最良サービスの profit/profit_with_refund を正しく 4 区分へ写像する."""
    _tpm, _ = tpm
    res_ddp = _result([_svc(100, 150, 30), _svc(120, 170, 30)], 0.8)
    res_ddu = _result([_svc(200, 250, 30), _svc(180, 240, 30)], 0.0)
    calls = []
    countries = []

    def _fake_calc(inp, settings):
        calls.append(bool(inp.is_ddu))
        countries.append(inp.country_code)
        return res_ddu if inp.is_ddu else res_ddp

    with patch("calculator.calculate", side_effect=_fake_calc), \
         patch.object(_tpm, "_calc_settings",
                      return_value={"exchange_rate": 150}):
        out = _tpm._cd_profit_breakdown(
            80.0, 9000.0, 300.0, 10.0, 10.0, 10.0, 58248, 0.0, 0)
    assert calls == [False, True]              # DDP 先 → DDU
    # Codex 2段 HIGH: country_code は両呼出 "US" 固定が意図的設計
    # (本システムは US 軸差分式、2 値の差 = 米国輸入関税分)。
    # 仕様として固定化しテストで恒久担保 (将来の誤"非US国"化を検出)。
    assert countries == ["US", "US"]
    assert out["refund_us"] == 170             # max profit_with_refund @ DDP
    assert out["refund_nonus"] == 250          # max profit_with_refund @ DDU
    assert out["noref_us"] == 120              # max profit @ DDP
    assert out["noref_nonus"] == 200           # max profit @ DDU
    assert out["tax_refund"] == 30
    assert out["ddp_cost_jpy"] == round(0.8 * 150)   # shipping_usd*fx, DDP のみ


def test_empty_service_results_returns_none(tpm):
    """送料サービス 0 件 → None (hero 非クラッシュ)."""
    _tpm, _ = tpm
    with patch("calculator.calculate", return_value=_result([], 0.0)), \
         patch.object(_tpm, "_calc_settings",
                      return_value={"exchange_rate": 150}):
        out = _tpm._cd_profit_breakdown(
            80.0, 9000.0, 300.0, 0.0, 0.0, 0.0, 58248, 0.0, 1)
    assert out is None


def test_calc_exception_returns_none_no_crash(tpm):
    """calculator 例外 → None で吸収 (hero は『未入力』表示で生存)."""
    _tpm, _ = tpm
    with patch("calculator.calculate",
               side_effect=ValueError("bad settings")), \
         patch.object(_tpm, "_calc_settings",
                      return_value={"exchange_rate": 150}):
        out = _tpm._cd_profit_breakdown(
            80.0, 9000.0, 300.0, 0.0, 0.0, 0.0, 58248, 0.0, 2)
    assert out is None


def test_no_ebay_or_db_write(tpm):
    """表示専用: get_conn を一切呼ばない (DB↔eBay 乖離の再生産防止)."""
    _tpm, _ = tpm
    res = _result([_svc(100, 150, 30)], 0.5)
    with patch("calculator.calculate", return_value=res), \
         patch.object(_tpm, "_calc_settings",
                      return_value={"exchange_rate": 150}), \
         patch.object(_tpm, "get_conn") as m_db:
        _tpm._cd_profit_breakdown(
            80.0, 9000.0, 300.0, 0.0, 0.0, 0.0, 58248, 0.0, 3)
    m_db.assert_not_called()
