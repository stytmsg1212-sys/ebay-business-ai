"""eBaymag 送料ポリシー canonical 値マッピング (Phase1 / 値レイヤ、mutation なし)。

設計書: `.company/engineering/docs/2026-06-21-ebaymag-shipping-policy-automation-design.md`
§4 (マッピング) / §6 (本モジュール) / §15 (Codex 反映: HIGH-3 / MED-1)。

役割:
  - `band_for_weight_g(weight_g)`: 重量(g) → 重量帯 (band)
  - `build_canonical_policy(band)`: rate table manifest の zone 別 USD を読み、
    eBaymag の各国上書きタブ (US/Europe/Australia/Canada) 別 USD + Worldwide 無料維持
    + 除外国 にマップした dict を返す (タブ構造は 2026-06-21 実機確定)

値の源泉 = `scripts/shipping_rate_batch/manifest/phase6_manifest_{band}.json` の
zone 別 USD と `manifest/zone_definitions.json` の zone→iso[]。ハードコード禁止
(実 manifest を読んで算出。設計 §6 / md-files-can-be-wrong)。

mutation (eBaymag / CDP / eBaymag 保存) は一切しない。canonical 値の生成のみ。
band は重量帯 (= 値集合キー) であって listing 識別ではない (sku-rules.md / §15)。
"""
from __future__ import annotations

import json
from pathlib import Path

# ---- パス (manifest は shipping_rate_batch の正本を読む) ----
# このファイル = tools/ebay-manager/monitor/ebaymag_policy_mapping.py
_MONITOR_DIR = Path(__file__).resolve().parent
_EBAY_MANAGER_ROOT = _MONITOR_DIR.parent  # tools/ebay-manager/
_MANIFEST_DIR = _EBAY_MANAGER_ROOT / "scripts" / "shipping_rate_batch" / "manifest"
_ZONE_DEFINITIONS = _MANIFEST_DIR / "zone_definitions.json"

# ---- 重量帯 上限(kg) ----
# 出典: scripts/shipping_rate_batch/config.py:45 BAND_UPPER_KG (BANDS L34-38)。
# monitor → scripts 逆 import を避けるため本モジュール内に複製で保持する
# (§15 MED-1。値は config と一致させること。config 変更時は本定数も追従 = cascade)。
# 代表重量 = 帯上限 (過小 = 赤字回避優先、config の設計と同思想)。
_BAND_UPPER_KG: dict[str, float] = {
    "0-0.5kg": 0.5, "0.5-1kg": 1.0, "1-2kg": 2.0, "2-3kg": 3.0,
    "3-4kg": 4.0, "4-5kg": 5.0, "5-6kg": 6.0, "6-8kg": 8.0,
    "8-10kg": 10.0, "10-20kg": 20.0,
}
# 帯の昇順 (band_for_weight_g の判定順)。最重帯の上限超は最重帯に丸める。
_BANDS_ASC: list[tuple[str, float]] = sorted(
    _BAND_UPPER_KG.items(), key=lambda kv: kv[1]
)

# ---- zone → eBaymag タブ別マッピング (2026-06-21 実機確定、設計 §4) ----
# eBaymag ポリシー編集の各国上書きタブ実構造 = Europe / US / Canada / Australia /
# Worldwide (実機確認 2026-06-21、各国版 active サイト = DE/CA/AU/UK/IT/FR/ES)。
# タブごとに値源 zone を割当てる:
#   US:        本体 rate table が課金するため eBaymag 側は $0 固定 (二重課金回避)
#   Europe:    zone6 (UK/DE/IT/FR/ES 全 EU を 1 タブで一括)
#   Australia: zone11
#   Canada:    zone5 (DHL SpeedPAK で CA+MX=北米のうち US 以外。DHL PDF p10 で
#              CA カナダ Zone 5 を実機確認 2026-06-21。各国版に MX は無い = CA のみ)
# Asia は eBaymag 対象外 (user 確定 2026-06-21) のため region から廃止。
# Worldwide catch-all は無料維持 (Asia 等 = 対象外で buyer 想定なし、user 確定)。
# 本体 manifest (phase6_manifest_{band}.json) に存在する zone から引くタブ。
# Canada(zone5) は本体 zone_definitions に無い (US/CA は本体 rate table 対象外、
# zone5 を本体に足すと W283 月次バッチが本体へ Canada 行を push する) ため、
# 別ファイル ebaymag_canada_zone5.json から引く (下記 _CANADA_FILE)。
_TAB_ZONE: dict[str, int] = {
    "Europe": 6,
    "Australia": 11,
}
# 本体課金で $0 固定するタブ (zone 値を引かない)。
_TAB_FIXED_ZERO: dict[str, int] = {"US": 0}
# Canada(zone5) 専用データ (gen_ebaymag_canada_zone5 が生成、本体と同一式・FX で算出)。
_CANADA_FILE = _MANIFEST_DIR / "ebaymag_canada_zone5.json"
_CANADA_TAB = "Canada"

# ---- 除外国の源泉 zone (設計 §4 / §6) ----
# 高コスト zone は配送不可で除外 (undercharge 無し、§4 確定済デフォルト)。
# Worldwide=無料運用のため、ここで除外しないと高コスト国が無料漏れする = 保護的に維持。
#   zone4 India / zone7 Iceland 等 / zone8 Israel / zone9 ME。
# iso[] は zone_definitions から展開 = 手書き禁止 (cascade 安全、§6)。
_EXCLUDED_ZONES: tuple[int, ...] = (4, 7, 8, 9)


def band_for_weight_g(weight_g: float) -> str:
    """重量 (g) を重量帯 (band) に変換する。

    閾値は _BAND_UPPER_KG (config.BAND_UPPER_KG と一致)。上限以下の最小帯を返す。
    最重帯の上限を超える重量は最重帯に丸める (赤字回避は別途 manifest 値で吸収)。

    Raises:
        ValueError: weight_g が None / 非数 / 非正の場合 (Q0 silent skip 禁止、
                    無効重量を黙って最小帯に落とさない)。
    """
    if weight_g is None:
        raise ValueError("weight_g is None — 重量未設定では band を決定できない")
    try:
        weight_g_f = float(weight_g)
    except (TypeError, ValueError) as e:
        raise ValueError(f"weight_g が数値でない: {weight_g!r}") from e
    if weight_g_f <= 0:
        raise ValueError(f"weight_g は正の値である必要がある: {weight_g_f}")

    weight_kg = weight_g_f / 1000.0
    for band, upper_kg in _BANDS_ASC:
        if weight_kg <= upper_kg:
            return band
    # 最重帯の上限超過 → 最重帯に丸める
    return _BANDS_ASC[-1][0]


def _load_zone_definitions() -> dict[int, list[str]]:
    """zone_definitions.json を読み、{zone(int): iso[]} を返す。"""
    if not _ZONE_DEFINITIONS.exists():
        raise FileNotFoundError(
            f"zone_definitions.json が見つからない: {_ZONE_DEFINITIONS}"
        )
    data = json.loads(_ZONE_DEFINITIONS.read_text(encoding="utf-8"))
    zones = data.get("zones")
    if not zones:
        raise ValueError(
            f"zone_definitions.json に 'zones' が無い: {_ZONE_DEFINITIONS}"
        )
    result: dict[int, list[str]] = {}
    for _key, entry in zones.items():
        zone_no = int(entry["zone"])
        iso = entry.get("iso") or []
        result[zone_no] = list(iso)
    return result


def _load_manifest_zone_usd(band: str) -> dict[int, int]:
    """phase6_manifest_{band}.json を読み、{zone(int): usd(int)} を返す。"""
    path = _MANIFEST_DIR / f"phase6_manifest_{band}.json"
    if not path.exists():
        raise FileNotFoundError(f"manifest が見つからない: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows")
    if not rows:
        raise ValueError(f"manifest に 'rows' が無い: {path}")
    zone_usd: dict[int, int] = {}
    for row in rows:
        zone_no = int(row["zone"])
        zone_usd[zone_no] = int(row["usd"])
    return zone_usd


def _load_canada_usd(band: str) -> int:
    """ebaymag_canada_zone5.json から band の Canada(zone5) USD を読む。

    Canada は本体 manifest に無いため専用ファイルから引く (gen_ebaymag_canada_zone5
    が本体と同一式・FX で生成)。欠落は Q0 で ValueError (黙って $0 にしない)。
    """
    if not _CANADA_FILE.exists():
        raise FileNotFoundError(
            f"Canada データが無い: {_CANADA_FILE} "
            "(python -m scripts.gen_ebaymag_canada_zone5_2026_06_21 で生成)"
        )
    data = json.loads(_CANADA_FILE.read_text(encoding="utf-8"))
    values = data.get("values") or {}
    if band not in values:
        raise ValueError(
            f"band={band}: Canada(zone5) 値が {_CANADA_FILE.name} に無い "
            "(値欠落を黙って補完しない)"
        )
    return int(values[band])


def build_canonical_policy(band: str) -> dict:
    """重量帯 band の canonical な eBaymag 送料ポリシー値を生成する。

    実 manifest と zone_definitions を読んで算出 (ハードコード禁止、§6)。

    Returns:
        dict: {
            "band": str,
            "tab_values": {"US":0,"Europe":0,"Australia":62,"Canada":11},
                          # eBaymag 各国上書きタブ別 USD (実機確定構造、2026-06-21)
            "worldwide_free": True,                       # Worldwide catch-all は無料維持
            "excluded_countries": ["IND","ISR",...],      # iso3 (高コスト zone 由来)
        }

    Raises:
        ValueError: band が未知、または必要 zone が manifest に無い場合
                    (Q0: 値欠落を黙って $0 等に落とさない)。
    """
    if band not in _BAND_UPPER_KG:
        raise ValueError(
            f"未知の band: {band!r} (有効値: {sorted(_BAND_UPPER_KG)})"
        )

    zone_usd = _load_manifest_zone_usd(band)
    zone_iso = _load_zone_definitions()

    def _require_zone(zone_no: int) -> int:
        if zone_no not in zone_usd:
            raise ValueError(
                f"band={band}: zone {zone_no} が manifest に無い "
                "(値欠落を黙って補完しない)"
            )
        return zone_usd[zone_no]

    # ---- eBaymag タブ別 ----
    tab_values: dict[str, int] = {}
    for tab, fixed in _TAB_FIXED_ZERO.items():
        tab_values[tab] = fixed  # US=$0 固定 (本体課金、§4/§6)
    for tab, zone_no in _TAB_ZONE.items():
        tab_values[tab] = _require_zone(zone_no)  # Europe/Australia は本体 manifest
    tab_values[_CANADA_TAB] = _load_canada_usd(band)  # Canada は専用ファイル (zone5)

    # ---- 除外国 (zone 由来、手書き禁止) ----
    excluded: list[str] = []
    for zone_no in _EXCLUDED_ZONES:
        iso_list = zone_iso.get(zone_no)
        if not iso_list:
            raise ValueError(
                f"除外対象 zone {zone_no} が zone_definitions に無い "
                "(cascade 不整合)"
            )
        excluded.extend(iso_list)
    # 重複排除しつつ安定順序 (zone 順 → iso 入力順) を保つ
    excluded_countries = list(dict.fromkeys(excluded))

    return {
        "band": band,
        "tab_values": tab_values,
        "worldwide_free": True,
        "excluded_countries": excluded_countries,
    }
