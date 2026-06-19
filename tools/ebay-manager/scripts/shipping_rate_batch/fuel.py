"""rate table 専用 燃料率の取得 (§3.2 / Codex F1)。

⚠️ calculator 用 `fuel_surcharge_fedex/dhl` (CPaSS、利益計算用) を **流用しない**。
rate table 差額式に入れるべきは「SpeedPAK 便の燃料率」であり、calculator の CPaSS 値
(現 49.5/47.75) とは別物の可能性がある。現 live rate table は Phase 3 の web FICP 値
(FedEx 41.50% / DHL 45.25%) で焼かれている。

専用 settings キー (user が手動維持):
  rate_table_fuel_fedex_pct, rate_table_fuel_dhl_pct
  rate_table_fuel_meta: {source, effective_week, last_verified_at(ISO), verified_by}

未設定 / stale(>30日) / 範囲外 なら auto 不可 (dry-run 強制)。
dry-run の計算には、未設定時は Phase 3 既定値を使い「この燃料で計算した」と通知に明示する。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

# Phase 3 で実際に live rate table を焼いた値 (6/19 web FICP/DHL)。
# 専用キー未設定時の dry-run 計算デフォルト (現状再現用、auto には使わない)。
PHASE3_DEFAULT_FEDEX_PCT = 41.50
PHASE3_DEFAULT_DHL_PCT = 45.25


def load_rate_table_fuel(settings: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """rate table 専用燃料率を取得。

    Returns:
        {
            "fedex_pct": float, "dhl_pct": float,   # 計算に使う値 (未設定なら Phase3 既定)
            "is_set": bool,                          # 専用キーが設定済か
            "auto_allowed": bool,                    # auto 適用に足る (設定済+fresh+範囲内)
            "source": str | None,
            "last_verified_at": str | None,
            "errors": [str, ...], "warnings": [str, ...],
        }
    """
    if settings is None:
        with open(config.SETTINGS_FILE, encoding="utf-8") as f:
            settings = json.load(f)
    if now is None:
        now = datetime.now()

    errors: list[str] = []
    warnings: list[str] = []

    fedex = settings.get("rate_table_fuel_fedex_pct")
    dhl = settings.get("rate_table_fuel_dhl_pct")
    meta = settings.get("rate_table_fuel_meta", {}) or {}

    is_set = fedex is not None and dhl is not None
    if not is_set:
        warnings.append(
            "rate_table_fuel_fedex_pct/dhl_pct 未設定 → Phase3 既定値 "
            f"(FedEx {PHASE3_DEFAULT_FEDEX_PCT}% / DHL {PHASE3_DEFAULT_DHL_PCT}%) で dry-run 計算。"
            " auto 化前に settings へ SpeedPAK 便の正しい燃料率を設定すること (Codex F1)。"
        )
        return {
            "fedex_pct": PHASE3_DEFAULT_FEDEX_PCT, "dhl_pct": PHASE3_DEFAULT_DHL_PCT,
            "is_set": False, "auto_allowed": False, "source": None,
            "last_verified_at": None, "errors": errors, "warnings": warnings,
        }

    fedex = float(fedex)
    dhl = float(dhl)

    # 範囲チェック
    for label, v in (("FedEx", fedex), ("DHL", dhl)):
        if not (config.FUEL_MIN_PCT <= v <= config.FUEL_MAX_PCT):
            errors.append(f"{label} 燃料 範囲外: {v}% not in [{config.FUEL_MIN_PCT},{config.FUEL_MAX_PCT}]")

    # source 必須 (Codex F1): どの便の燃料か証跡が無いと calculator CPaSS 値との取り違えを防げない
    if not meta.get("source"):
        errors.append("rate_table_fuel_meta.source 未設定 (SpeedPAK 便の燃料率である証跡が必要)")

    # freshness
    last_verified = meta.get("last_verified_at")
    days = None
    if last_verified:
        try:
            days = (now - datetime.fromisoformat(last_verified)).days
        except (ValueError, TypeError):
            warnings.append(f"last_verified_at 解析不能: {last_verified!r}")
    else:
        errors.append("rate_table_fuel_meta.last_verified_at 未設定")
    if days is not None and days < 0:
        errors.append(f"last_verified_at が未来日 (days={days}) = metadata 異常")
    if days is not None and days > config.FUEL_STALE_DAYS:
        errors.append(f"燃料 stale: last_verified {days} 日前 > {config.FUEL_STALE_DAYS} 日")

    auto_allowed = (not errors) and (days is not None and 0 <= days <= config.FUEL_STALE_DAYS)
    return {
        "fedex_pct": fedex, "dhl_pct": dhl,
        "is_set": True, "auto_allowed": auto_allowed, "source": meta.get("source"),
        "last_verified_at": last_verified, "errors": errors, "warnings": warnings,
    }
