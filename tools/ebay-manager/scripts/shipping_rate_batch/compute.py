"""差額式 surcharge 計算 (phase3_calc.py のコアを汎化)。

式 (Phase 3 設計 §3):
    surcharge(USD) = max(0, ceil[ (DHL実費×(1+DHL燃料) − FedEx_US実費×(1+FedEx燃料)) / FX ])
  - DHL実費 = 非書類(荷物)料金, 帯上限重量, 該当 DHLゾーン (円)
  - FedEx_US実費 = FedEx FICP 列E(USW), 帯上限重量 (円)
  - US 送料は商品価格に内包 (US 無料運用) → 差額のみ転嫁で損益分岐
"""
from __future__ import annotations

import math


def compute_surcharge_usd(
    dhl_base_jpy: int,
    fedex_us_base_jpy: int,
    dhl_fuel_pct: float,
    fedex_fuel_pct: float,
    jpy_per_usd: float,
) -> int:
    """1 (帯, ゾーン) の表示送料 USD を計算する。

    Args:
        dhl_base_jpy: DHL 非書類 料金 (該当ゾーン・帯上限重量、円)
        fedex_us_base_jpy: FedEx 列E(USW) 料金 (帯上限重量、円)
        dhl_fuel_pct: DHL 燃料率 (%, 例 45.25)
        fedex_fuel_pct: FedEx 燃料率 (%, 例 41.50)
        jpy_per_usd: 為替 (円/$, 例 157)

    Returns:
        切上げ・マイナス 0 止めの USD 整数。
    """
    if jpy_per_usd <= 0:
        raise ValueError(f"jpy_per_usd must be positive: {jpy_per_usd}")
    dhl = dhl_base_jpy * (1 + dhl_fuel_pct / 100.0)
    fed = fedex_us_base_jpy * (1 + fedex_fuel_pct / 100.0)
    usd = (dhl - fed) / jpy_per_usd
    return max(0, math.ceil(usd))


def compute_band_zone_table(
    base_rates: dict,
    zone_numbers: list[int],
    dhl_fuel_pct: float,
    fedex_fuel_pct: float,
    jpy_per_usd: float,
    bands: list[tuple[str, float]],
) -> dict[str, dict[int, int]]:
    """全 (帯, ゾーン) の surcharge を計算する。

    Args:
        base_rates: parse_base_rates.load_base_rates() の戻り。
            {"dhl": {weight: {zone: jpy}}, "fedex_us": {weight: jpy}} 形式。
        zone_numbers: 対象 DHL ゾーン番号のリスト (例 [1,2,3,4,6,7,8,9,11])。
        bands: [(band_label, upper_kg), ...]。

    Returns:
        {band_label: {zone_number: usd}}。
    """
    out: dict[str, dict[int, int]] = {}
    for band, upper_kg in bands:
        dhl_for_w = base_rates["dhl"].get(_wkey(upper_kg))
        fedex_for_w = base_rates["fedex_us"].get(_wkey(upper_kg))
        if dhl_for_w is None or fedex_for_w is None:
            raise KeyError(f"base_rates に帯上限 {upper_kg}kg が無い (band={band})")
        zrate: dict[int, int] = {}
        for z in zone_numbers:
            dhl_jpy = dhl_for_w.get(str(z))
            if dhl_jpy is None:
                raise KeyError(f"DHL base に zone {z} が無い (weight={upper_kg})")
            zrate[z] = compute_surcharge_usd(
                dhl_jpy, fedex_for_w, dhl_fuel_pct, fedex_fuel_pct, jpy_per_usd
            )
        out[band] = zrate
    return out


def _wkey(weight: float) -> str:
    """重量を base_rates dict のキー文字列に正規化 (JSON 由来は str キー)。"""
    # 0.5 -> "0.5", 2.0 -> "2.0" (parse_base_rates と統一)
    return str(weight)
