"""canonical zone 定義 + 国セット ISO 正規化 bijection 照合 (Codex F5)。

manifest の rate_id は **盲信しない**。live getRateTable の各 row の正規化国セットを
manifest/zone_definitions と一意 bijection match し、そこで得た current rateId を採用する。
全 table で「9 row 完全一致・重複 signature なし・余剰 row なし」を満たすまで apply 禁止。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import config


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_zone_definitions() -> dict:
    """zone_definitions.json を読む。{zone_number(int): {"countries":[...], "iso":[...]}}。"""
    raw = _load_json(config.ZONE_DEFINITIONS)
    out = {}
    for _zk, v in raw["zones"].items():
        out[int(v["zone"])] = {"countries": v["countries"], "iso": v["iso"]}
    return out


def _country_iso_map() -> dict[str, str]:
    """国名(eBay 表示名) → ISO3。picker_country_ids.json。"""
    return _load_json(config.MANIFEST_DIR / "picker_country_ids.json")


def normalize_country_set(names: list[str], iso_map: Optional[dict[str, str]] = None) -> frozenset[str]:
    """国名リストを正規化 signature (ISO frozenset) に変換。

    ISO map にある国は ISO3、無い国は表示名を strip/lower した token を使う
    (表記揺れ・順序差に頑健、Codex F5)。
    """
    if iso_map is None:
        iso_map = _country_iso_map()
    sig = set()
    for n in names:
        iso = iso_map.get(n)
        sig.add(iso if iso else "name:" + n.strip().lower())
    return frozenset(sig)


def load_band_manifest(band: str) -> dict:
    """1 帯の canonical manifest を読む (rate_id/zone/usd/countries)。"""
    table_id = config.BAND_TO_TABLE[band]
    return _load_json(config.MANIFEST_DIR / f"phase6_manifest_{band}.json")


def match_live_rows_to_zones(
    live_rows: list[dict],
    zone_defs: dict,
    iso_map: Optional[dict[str, str]] = None,
) -> dict:
    """live getRateTable の rows を zone_definitions に bijection match (F5)。

    Args:
        live_rows: getRateTable の rates[] (各 {rateId, shippingRegionNames, shippingCost{...}})。
        zone_defs: load_zone_definitions() の戻り。

    Returns:
        {
            "ok": bool,                       # 完全 bijection 成立か
            "errors": [str, ...],             # 不一致の説明 (ok=False 時)
            "zone_to_rate": {zone: rateId},   # ok=True 時のみ意味を持つ
            "zone_to_old_usd": {zone: int},   # 現 USD (rollback/diff 用)
        }
    fail-closed: 余剰 row / 欠落 zone / 重複 signature / 未知 signature いずれも ok=False。
    """
    if iso_map is None:
        iso_map = _country_iso_map()

    expected_sig = {z: normalize_country_set(v["countries"], iso_map) for z, v in zone_defs.items()}
    sig_to_zone: dict[frozenset, int] = {}
    for z, sig in expected_sig.items():
        if sig in sig_to_zone:
            return {"ok": False, "errors": [f"zone_definitions 内で重複 signature: zone {z} と {sig_to_zone[sig]}"],
                    "zone_to_rate": {}, "zone_to_old_usd": {}}
        sig_to_zone[sig] = z

    errors: list[str] = []
    zone_to_rate: dict[int, str] = {}
    zone_to_old_usd: dict[int, int] = {}
    seen_zones: set[int] = set()

    for row in live_rows:
        names = row.get("shippingRegionNames") or []
        rate_id = row.get("rateId")
        sig = normalize_country_set(names, iso_map)
        z = sig_to_zone.get(sig)
        if z is None:
            errors.append(f"rateId {rate_id}: 未知の国セット (zone 定義に無い) names={names[:4]}...")
            continue
        if z in seen_zones:
            errors.append(f"zone {z} に複数 live row が対応 (重複): rateId {rate_id}")
            continue
        seen_zones.add(z)
        zone_to_rate[z] = rate_id
        try:
            zone_to_old_usd[z] = int(round(float(row["shippingCost"]["value"])))
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"zone {z} rateId {rate_id}: shippingCost 読取失敗 {e}")

    missing = set(zone_defs.keys()) - seen_zones
    if missing:
        errors.append(f"欠落 zone (live に row 無し): {sorted(missing)}")
    # 余剰 row (未知 signature / 同 zone 二重) は上のループで既に errors 計上済。

    ok = len(errors) == 0 and not missing
    return {"ok": ok, "errors": errors, "zone_to_rate": zone_to_rate, "zone_to_old_usd": zone_to_old_usd}
