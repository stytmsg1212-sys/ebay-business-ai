"""eBaymag Canada(zone5) 送料データファイル生成 one-shot。

Canada は DHL SpeedPAK zone5 (CA+MX、PDF p10 実機確認 2026-06-21)。本体 rate table
の zone_definitions/manifest には US(z10)/CA(z5) が無い (rebuild 対象外) ため、
zone5 を本体に足すと W283 月次バッチが本体 rate table に Canada 行を push してしまう。
それを避けるため、eBaymag 専用の Canada データを独立ファイルに切り出す。

値は scripts.shipping_rate_batch.compute で本体 manifest と同一の式・燃料率・FX を使い
算出する (fuel=PHASE3_DEFAULT 45.25/41.50、FX=fx_state。AU=$62 で逆算検証済 2026-06-21)。
build_canonical_policy はこのファイルを読む (monitor→scripts 逆 import 回避 = データ読取)。

使い方: python -m scripts.gen_ebaymag_canada_zone5_2026_06_21
出力: scripts/shipping_rate_batch/manifest/ebaymag_canada_zone5.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.shipping_rate_batch import config  # noqa: E402
from scripts.shipping_rate_batch.compute import compute_band_zone_table  # noqa: E402
from scripts.shipping_rate_batch.fuel import (  # noqa: E402
    PHASE3_DEFAULT_DHL_PCT, PHASE3_DEFAULT_FEDEX_PCT,
)
from scripts.shipping_rate_batch.parse_base_rates import load_base_rates  # noqa: E402

CANADA_ZONE = 5
OUT = config.MANIFEST_DIR / "ebaymag_canada_zone5.json"


def _load_fx() -> float:
    """fx_state.json の最新 fx を読む (本体 manifest と同一 FX で整合)。"""
    fx_state = config.FX_STATE
    if fx_state.exists():
        hist = json.loads(fx_state.read_text(encoding="utf-8")).get("history", [])
        for h in reversed(hist):
            if h.get("ok") and h.get("fx"):
                return float(h["fx"])
    raise RuntimeError("fx_state.json から有効な fx を取得できない")


def main() -> int:
    base = load_base_rates()["base_rates"]
    fx = _load_fx()
    tbl = compute_band_zone_table(
        base, [CANADA_ZONE],
        PHASE3_DEFAULT_DHL_PCT, PHASE3_DEFAULT_FEDEX_PCT, fx, config.BANDS,
    )
    values = {band: tbl[band][CANADA_ZONE] for band, _ in config.BANDS}
    out = {
        "_source": (
            "DHL SpeedPAK zone5 (Canada). compute_band_zone_table "
            f"fuel(DHL={PHASE3_DEFAULT_DHL_PCT}/FedEx={PHASE3_DEFAULT_FEDEX_PCT}) "
            f"FX={fx}. 本体 manifest と同一式・AU=$62 逆算検証済 (2026-06-21)。"
        ),
        "zone": CANADA_ZONE,
        "fx": fx,
        "values": values,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT}")
    print(json.dumps(values, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
