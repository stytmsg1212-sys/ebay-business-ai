"""Phase 9 送料月次バッチ 純粋ロジックの回帰テスト (W283)。

UI/外部 API/PDF/DB に依存しない層 (compute / guards / manifest bijection /
fx range / fuel gating) を網羅。Codex F1-F6 の各ゲートを検証。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from scripts.shipping_rate_batch import compute, config, ebay_api, fetch_fx, fuel, manifest


# ---- compute (差額式) ----
def test_surcharge_formula_israel_anchor():
    # 1-2kg Z8: DHL nondoc 2.0=6452, FedEx US 2.0=3061, FICP 41.50/45.25, FX 157 → $33 (live 一致)
    assert compute.compute_surcharge_usd(6452, 3061, 45.25, 41.50, 157) == 33


def test_surcharge_floor_zero():
    # DHL が FedEx_US より安い → マイナスは 0 止め
    assert compute.compute_surcharge_usd(1000, 5000, 45.25, 41.50, 157) == 0


def test_surcharge_ceil():
    # 端数は切上げ (過小=赤字回避)
    v = compute.compute_surcharge_usd(2000, 1000, 0, 0, 157)  # (2000-1000)/157=6.37 → 7
    assert v == 7


def test_surcharge_invalid_fx():
    with pytest.raises(ValueError):
        compute.compute_surcharge_usd(1, 1, 0, 0, 0)


# ---- variation_threshold (F6 ハイブリッドガード) ----
@pytest.mark.parametrize("old,new,within", [
    (0, 0, True),     # 変化なし
    (0, 5, False),    # 新規課金 → 必ず alert
    (5, 0, False),    # 無料化 → 必ず alert
    (1, 2, True),     # 軽帯 Δ1 <= $3
    (1, 5, False),    # 軽帯 Δ4 > $3
    (33, 35, True),   # 通常帯 Δ2 <= max(10,30%)
    (40, 318, False), # 重帯 Δ278 過大
    (100, 118, True), # 重帯 Δ18 <= max(30, 20%=20)
    (100, 135, False),# 重帯 Δ35 > max(30,20)
])
def test_variation_threshold(old, new, within):
    ok, _ = config.variation_threshold(old, new)
    assert ok is within


# ---- manifest bijection (F5) ----
def _zdef():
    return {
        1: {"countries": ["Korea, South", "Taiwan"], "iso": ["KOR", "TWN"]},
        2: {"countries": ["China", "Hong Kong"], "iso": ["CHN", "HKG"]},
    }


def _iso():
    return {"Korea, South": "KOR", "Taiwan": "TWN", "China": "CHN", "Hong Kong": "HKG"}


def _row(rate_id, names, usd):
    return {"rateId": rate_id, "shippingRegionNames": names, "shippingCost": {"value": f"{usd}.00", "currency": "USD"}}


def test_bijection_ok_and_rateid_dynamic():
    # rateId が manifest と違っても (再採番) 国セットで照合し current rateId を採用
    live = [
        _row("9", ["Taiwan", "Korea, South"], 0),   # 順序逆 + rateId=9
        _row("3", ["Hong Kong", "China"], 1),
    ]
    r = manifest.match_live_rows_to_zones(live, _zdef(), _iso())
    assert r["ok"]
    assert r["zone_to_rate"] == {1: "9", 2: "3"}
    assert r["zone_to_old_usd"] == {1: 0, 2: 1}


def test_bijection_missing_zone():
    live = [_row("1", ["Korea, South", "Taiwan"], 0)]  # zone 2 欠落
    r = manifest.match_live_rows_to_zones(live, _zdef(), _iso())
    assert not r["ok"]
    assert any("欠落 zone" in e for e in r["errors"])


def test_bijection_unknown_country_set():
    live = [
        _row("1", ["Korea, South", "Taiwan"], 0),
        _row("2", ["Mars"], 1),  # 未知
    ]
    r = manifest.match_live_rows_to_zones(live, _zdef(), _iso())
    assert not r["ok"]
    assert any("未知の国セット" in e for e in r["errors"])


def test_bijection_partial_zone_set_mismatch():
    # 国セットが部分的に違う (Taiwan 欠落) → 未知 signature として弾く
    live = [
        _row("1", ["Korea, South"], 0),
        _row("2", ["China", "Hong Kong"], 1),
    ]
    r = manifest.match_live_rows_to_zones(live, _zdef(), _iso())
    assert not r["ok"]


# ---- fx range (F2) ----
def test_prev_month_range_jan():
    # 1月基準 → 前年12月
    first, last = fetch_fx._prev_month_range(date(2026, 1, 15))
    assert first == "2025-12-01" and last == "2025-12-31"


def test_prev_month_range_mar_leap():
    first, last = fetch_fx._prev_month_range(date(2026, 3, 10))
    assert first == "2026-02-01" and last == "2026-02-28"


# ---- fuel gating (F1) ----
def test_fuel_unset_blocks_auto():
    r = fuel.load_rate_table_fuel(settings={}, now=datetime(2026, 6, 19))
    assert r["is_set"] is False
    assert r["auto_allowed"] is False
    assert r["fedex_pct"] == fuel.PHASE3_DEFAULT_FEDEX_PCT  # dry-run 既定で計算可能


def test_fuel_set_fresh_allows_auto():
    s = {
        "rate_table_fuel_fedex_pct": 41.5, "rate_table_fuel_dhl_pct": 45.25,
        "rate_table_fuel_meta": {"source": "CPaSS SpeedPAK", "last_verified_at": "2026-06-15T00:00:00"},
    }
    r = fuel.load_rate_table_fuel(settings=s, now=datetime(2026, 6, 19))
    assert r["is_set"] and r["auto_allowed"]


def test_fuel_stale_blocks_auto():
    s = {
        "rate_table_fuel_fedex_pct": 41.5, "rate_table_fuel_dhl_pct": 45.25,
        "rate_table_fuel_meta": {"last_verified_at": "2026-04-01T00:00:00"},  # >30日
    }
    r = fuel.load_rate_table_fuel(settings=s, now=datetime(2026, 6, 19))
    assert not r["auto_allowed"]
    assert any("stale" in e for e in r["errors"])


# ---- eBay API: marketplace ヘッダ回帰ガード (code-reviewer HIGH-1) ----
def test_marketplace_header_sent_on_get(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"rates": []}
        return R()

    monkeypatch.setattr(ebay_api.httpx, "get", fake_get)
    ebay_api.get_rate_table("5284241010", token="x")
    assert captured["headers"].get("X-EBAY-C-MARKETPLACE-ID") == "EBAY_US"


def test_marketplace_header_sent_on_update(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers

        class R:
            status_code = 204
            text = ""
        return R()

    monkeypatch.setattr(ebay_api.httpx, "post", fake_post)
    res = ebay_api.update_shipping_cost("5284241010", [{"rateId": "1", "usd": 5}], token="x")
    assert res["ok"]
    assert captured["headers"].get("X-EBAY-C-MARKETPLACE-ID") == "EBAY_US"


def test_fuel_out_of_range_blocks_auto():
    s = {
        "rate_table_fuel_fedex_pct": 200.0, "rate_table_fuel_dhl_pct": 45.25,
        "rate_table_fuel_meta": {"source": "CPaSS SpeedPAK", "last_verified_at": "2026-06-18T00:00:00"},
    }
    r = fuel.load_rate_table_fuel(settings=s, now=datetime(2026, 6, 19))
    assert not r["auto_allowed"]
    assert any("範囲外" in e for e in r["errors"])


def test_fuel_missing_source_blocks_auto():
    # 値も freshness も OK だが source 欠落 → auto 不可 (Codex HIGH-3, calculator CPaSS 取り違え防止)
    s = {
        "rate_table_fuel_fedex_pct": 41.5, "rate_table_fuel_dhl_pct": 45.25,
        "rate_table_fuel_meta": {"last_verified_at": "2026-06-18T00:00:00"},
    }
    r = fuel.load_rate_table_fuel(settings=s, now=datetime(2026, 6, 19))
    assert not r["auto_allowed"]
    assert any("source" in e for e in r["errors"])


def test_fuel_future_date_blocks_auto():
    # last_verified_at が未来日 → metadata 異常で auto 不可 (Codex MED)
    s = {
        "rate_table_fuel_fedex_pct": 41.5, "rate_table_fuel_dhl_pct": 45.25,
        "rate_table_fuel_meta": {"source": "CPaSS SpeedPAK", "last_verified_at": "2026-08-01T00:00:00"},
    }
    r = fuel.load_rate_table_fuel(settings=s, now=datetime(2026, 6, 19))
    assert not r["auto_allowed"]
    assert any("未来日" in e for e in r["errors"])
