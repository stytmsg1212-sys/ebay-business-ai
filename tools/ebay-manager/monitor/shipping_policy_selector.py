#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShippingPolicy 自動選択 (W9 Phase 3)

出品物の重量 (g) × 在庫有無 から settings.json の
`ebay_business_policies.shipping_weight_mapping_(in_stock|no_stock)` を引いて
Shipping Policy ID を自動決定する。

設計方針:
  - settings.json の実構造は "0-500": "<policy_id>" の dict 形式
    (range_label → policy_id)。配列ではない点に注意。
  - 重量レンジは "min-max" (g) 形式で半開区間: min <= weight_g < max
    ただし最後のレンジ (10000-20000) は閉区間として扱う (20000g 以下は採用)。
  - weight_g=None の場合: 最小レンジ (0-500g) を採用し警告ログ。
    理由: 軽量 policy は送料が小さく、過剰請求リスクが低い。
  - weight_g が最大レンジを超える場合: 最大レンジ policy を採用し警告ログ。
  - shipping_auto_select=False の場合でも本関数は「どれを選ぶべきか」の
    提案値として動作する。実際の適用可否は呼出側 (ebay_lister) が判断。
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

# pythonw gotcha ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

logger = logging.getLogger(__name__)


# =========================================================================
# 定数
# =========================================================================

# 重量不明時のデフォルトレンジ (最小、安全側)
_DEFAULT_WEIGHT_RANGE_G: int = 1

# 在庫あり / なし それぞれの settings.json キー
_KEY_IN_STOCK = "shipping_weight_mapping_in_stock"
_KEY_NO_STOCK = "shipping_weight_mapping_no_stock"


# =========================================================================
# ヘルパ
# =========================================================================

def _parse_range_label(label: str) -> Optional[tuple[int, int]]:
    """'0-500' → (0, 500)。不正形式は None。"""
    if not label or not isinstance(label, str):
        return None
    m = re.match(r'^\s*(\d+)\s*-\s*(\d+)\s*$', label)
    if not m:
        return None
    try:
        lo = int(m.group(1))
        hi = int(m.group(2))
    except ValueError:
        return None
    if hi <= lo:
        return None
    return (lo, hi)


def _build_range_label(lo: int, hi: int) -> str:
    """(0, 500) → '0-500g' (人間向けラベル)。"""
    return f"{lo}-{hi}g"


def _sorted_ranges(mapping: dict) -> list[tuple[int, int, str]]:
    """settings の dict を (lo, hi, policy_id) の昇順リストに変換。

    不正エントリ (policy_id が非文字列、ラベルが parse 不能) はスキップ。
    """
    out: list[tuple[int, int, str]] = []
    if not isinstance(mapping, dict):
        return out
    for label, policy_id in mapping.items():
        rng = _parse_range_label(label)
        if rng is None:
            logger.debug(f"skip invalid range label: {label!r}")
            continue
        if not isinstance(policy_id, str) or not policy_id.strip():
            logger.debug(f"skip invalid policy_id for {label!r}: {policy_id!r}")
            continue
        out.append((rng[0], rng[1], policy_id.strip()))
    out.sort(key=lambda x: x[0])
    return out


# =========================================================================
# 公開 API
# =========================================================================

def select_shipping_policy(
    weight_g: Optional[int],
    in_stock: bool,
    config: dict,
) -> tuple[str, str]:
    """重量 × 在庫有無から ShippingPolicy ID を選択する。

    Args:
        weight_g: 出品物の重量 (g)。None の場合は最小レンジを採用。
        in_stock: True なら 1day 出荷系ポリシー、False なら 7day 出荷系。
        config: settings.json 全体の dict (ebay_business_policies キー必須)

    Returns:
        (policy_id, policy_label)
        例: ("377279091023", "In-stock 0-500g")

    Raises:
        ValueError: settings.json に ebay_business_policies / mapping キーが
                    欠落している場合。呼出側で catch して UI にエラー表示すること。
    """
    if not isinstance(config, dict):
        raise ValueError("config must be a dict")

    policies = config.get("ebay_business_policies")
    if not isinstance(policies, dict):
        raise ValueError(
            "settings.json missing 'ebay_business_policies' block"
        )

    key = _KEY_IN_STOCK if in_stock else _KEY_NO_STOCK
    mapping = policies.get(key)
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(
            f"settings.json missing '{key}' mapping (in_stock={in_stock})"
        )

    ranges = _sorted_ranges(mapping)
    if not ranges:
        raise ValueError(
            f"'{key}' has no valid weight ranges"
        )

    stock_label = "In-stock" if in_stock else "Out-of-stock"

    # weight_g が None の場合は最小レンジを採用
    effective_g: int
    if weight_g is None:
        logger.warning(
            "weight_g is None -> fallback to smallest range "
            f"({stock_label}, range={_build_range_label(*ranges[0][:2])})"
        )
        effective_g = _DEFAULT_WEIGHT_RANGE_G
    else:
        try:
            effective_g = int(weight_g)
        except (TypeError, ValueError):
            logger.warning(
                f"weight_g not int-coercible ({weight_g!r}) -> fallback smallest range"
            )
            effective_g = _DEFAULT_WEIGHT_RANGE_G

    # 負数・0 は最小レンジに倒す (購入側安全性)
    if effective_g < 0:
        logger.warning(f"weight_g negative ({effective_g}g) -> fallback smallest range")
        effective_g = _DEFAULT_WEIGHT_RANGE_G

    # レンジ選択: 半開区間 [lo, hi)
    # 最後のレンジは [lo, hi] (閉区間) として扱う
    last_idx = len(ranges) - 1
    for idx, (lo, hi, policy_id) in enumerate(ranges):
        is_last = (idx == last_idx)
        in_range = (lo <= effective_g < hi) if not is_last else (lo <= effective_g <= hi)
        if in_range:
            label = f"{stock_label} {_build_range_label(lo, hi)}"
            return (policy_id, label)

    # 全レンジを超過 → 最大レンジ policy を採用 + 警告
    lo, hi, policy_id = ranges[-1]
    label = f"{stock_label} {_build_range_label(lo, hi)} (exceeded, using largest)"
    logger.warning(
        f"weight_g={effective_g}g exceeds all ranges (max={hi}g) "
        f"-> using largest policy {policy_id}"
    )
    return (policy_id, label)


def load_settings_policies(settings_path: Optional[str] = None) -> dict:
    """settings.json をロードして config dict を返すヘルパ。

    ebay_business_policies ブロック検証付き。テストで有用。
    """
    import json
    from pathlib import Path

    if settings_path is None:
        p = Path(__file__).resolve().parent.parent / "settings.json"
    else:
        p = Path(settings_path)
    if not p.exists():
        raise FileNotFoundError(f"settings.json not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO)

    cfg = load_settings_policies()
    tests = [
        (100, True),
        (500, True),
        (501, False),
        (1000, True),
        (9999, True),
        (10000, True),
        (25000, False),
        (None, True),
    ]
    for w, stock in tests:
        try:
            pid, lbl = select_shipping_policy(w, stock, cfg)
            print(_json.dumps({
                "weight_g": w, "in_stock": stock,
                "policy_id": pid, "label": lbl,
            }, ensure_ascii=False))
        except ValueError as e:
            print(_json.dumps({
                "weight_g": w, "in_stock": stock,
                "error": str(e),
            }, ensure_ascii=False))
