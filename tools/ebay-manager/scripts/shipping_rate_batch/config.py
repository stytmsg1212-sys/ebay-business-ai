"""Phase 9 月次バッチ 定数・パス・サニティ境界・ガード閾値。

設計 v2 (Codex VERDICT B 反映) の各種閾値をここに集約する。
金銭直結のため、数値の意味と出典をコメントで明記する。
"""
from __future__ import annotations

import math
from pathlib import Path

# ---- パス ----
# このファイル = tools/ebay-manager/scripts/shipping_rate_batch/config.py
PKG_DIR = Path(__file__).resolve().parent
EBAY_MANAGER_ROOT = PKG_DIR.parent.parent          # tools/ebay-manager/
MANIFEST_DIR = PKG_DIR / "manifest"
ZONE_DEFINITIONS = MANIFEST_DIR / "zone_definitions.json"

SETTINGS_FILE = EBAY_MANAGER_ROOT / "settings.json"
SCHEDULE_CONFIG = EBAY_MANAGER_ROOT / "config" / "schedule_config.json"

# バッチ専用 state (キャッシュ / snapshot / FX 履歴)。tmp ではなく永続。
STATE_DIR = EBAY_MANAGER_ROOT / "data" / "shipping_rate_batch"
BASE_RATES_CACHE = STATE_DIR / "base_rates_cache.json"
FX_STATE = STATE_DIR / "fx_state.json"
SNAPSHOT_DIR = STATE_DIR / "snapshots"

# SpeedPAK 料金 PDF (user 手動配置、年 1-2 回更新)
PDF_DIR = Path(r"C:\work\claude\eBaySpeedPAK")
FEDEX_PDF = PDF_DIR / "RATE GUIDE of eBay SpeedPAK Japan Ship via FedEx-JP (2).pdf"
DHL_PDF = PDF_DIR / "RATE GUIDE of eBay SpeedPAK Japan Ship via DHL-JP (2).pdf"

# ---- 帯 → 上限重量(kg) / table_id ----
# 代表重量 = 帯上限 (過小 = 赤字回避優先、Phase 3 設計)
BANDS: list[tuple[str, float]] = [
    ("0-0.5kg", 0.5), ("0.5-1kg", 1.0), ("1-2kg", 2.0), ("2-3kg", 3.0),
    ("3-4kg", 4.0), ("4-5kg", 5.0), ("5-6kg", 6.0), ("6-8kg", 8.0),
    ("8-10kg", 10.0), ("10-20kg", 20.0),
]
BAND_TO_TABLE: dict[str, str] = {
    "0-0.5kg": "5284239010", "0.5-1kg": "5284240010", "1-2kg": "5284241010",
    "2-3kg": "5284247010", "3-4kg": "5284249010", "4-5kg": "5284251010",
    "5-6kg": "5284252010", "6-8kg": "5284253010", "8-10kg": "5284254010",
    "10-20kg": "5284256010",
}
BAND_UPPER_KG: dict[str, float] = dict(BANDS)

# FedEx FICP 料金表の列 idx (A D E F G H I J K M U = 0..10)。US = E = idx 2 (USW、保守)
FEDEX_US_COL_IDX = 2
# この重量(kg)以下は DHL 非書類(荷物)料金を使う (書類層と分岐)
DHL_NONDOC_MAX = 2.0

# ---- サニティ境界 (fail-closed、§7) ----
FX_MIN, FX_MAX = 120.0, 200.0       # 円/$ の常識帯。外なら FX 据え置き + alert
FUEL_MIN_PCT, FUEL_MAX_PCT = 10.0, 70.0   # 燃料% の常識帯
FUEL_STALE_DAYS = 30                # rate_table_fuel last_verified がこれ超なら auto 不可

# PDF アンカー (パース正当性検証、phase3_calc.py 由来)
# (説明, kind, weight, zone, 期待値)。kind: 'fedex_us' | 'dhl'
PDF_ANCHORS = [
    ("FedEx US(E) 0.5kg", "fedex_us", 0.5, None, 2082),
    ("FedEx US(E) 2.0kg", "fedex_us", 2.0, None, 3061),
    ("FedEx US(E) 20kg", "fedex_us", 20.0, None, 16887),
    ("DHL nondoc 0.5 Z10(US)", "dhl", 0.5, 10, 2727),
    ("DHL nondoc 0.5 Z1", "dhl", 0.5, 1, 2054),
    ("DHL nondoc 2.0 Z8(IL)", "dhl", 2.0, 8, 6452),
    ("DHL single 3.0 Z8(IL)", "dhl", 3.0, 8, 7389),
]

# ---- Discord ----
DISCORD_CATEGORY = "pricing"

# ---- task / scheduler ----
TASK_KEY = "rate_table_monthly_update"
TASK_DISPLAY = "月次送料 rate table 自動更新 (DDP差額式)"


def variation_threshold(old_usd: int, new_usd: int) -> tuple[bool, str]:
    """per-rate 変動ハイブリッドガード (F6)。

    旧→新 の変動が「想定内」か判定。返値 (within_bound, reason)。
    within_bound=False なら異常値候補 (dry-run=通知 / auto=hold)。

    ルール (Codex F6):
      - $0 行: 0->正 / 正->0 は常に別扱いで alert (within=False, 必ず人手確認)
      - 軽帯感覚 (旧 < $5): 絶対 ±$3 まで
      - 通常帯 (旧 $5-$50): max($10, 30%) まで
      - 重帯 (旧 >= $50, Z8/Z9 等): max($30, 20%) まで
    閾値は「旧価格」基準。新規上昇は new 側でも判定。
    """
    delta = abs(new_usd - old_usd)
    # $0 境界は必ず人手確認
    if old_usd == 0 and new_usd > 0:
        return False, f"$0→${new_usd} (新規課金、要確認)"
    if old_usd > 0 and new_usd == 0:
        return False, f"${old_usd}→$0 (無料化、要確認)"
    if old_usd == 0 and new_usd == 0:
        return True, "0→0 変化なし"

    # 閾値は「旧価格」基準 (Codex F6)。旧が軽帯なら小幅でも alert。
    if old_usd < 5:
        limit = 3
    elif old_usd < 50:
        limit = max(10, math.ceil(old_usd * 0.30))
    else:
        limit = max(30, math.ceil(old_usd * 0.20))
    if delta <= limit:
        return True, f"Δ${delta} <= ${limit}"
    return False, f"Δ${delta} > ${limit} (変動過大、要確認)"
