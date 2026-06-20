#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 各国版 (非USD) と US 本体の現状監査 (READ ONLY, 2026-06-20).

目的 (依頼ボード #4 W263 / #5 W264 の現状監査):
  - ライブ GetMyeBaySelling から US 本体 (USD) と 各国版 (非USD) を分離集計
  - 各国版の通貨別件数 / qty 分布を把握 (eBaymag 実反映状態)
  - US 本体 (MonoDeck DB) と突き合わせ、qty 乖離 = オーバーセル危険を炙り出す
      * 各国版 qty>0 なのに対応 US 本体が ended / qty=0 → #4 の本丸リスク

注意:
  - **完全 READ ONLY**。DB も eBay も一切変更しない (GetMyeBaySelling は参照のみ)。
  - 各国版↔US本体 の対応付けは SKU で行うが、これは「監査グルーピング」であり
    listing 識別キーではない (SKU 規約: listing 識別は ebay_item_id)。eBaymag 複製は
    US 本体と同一 SKU/タイトルを共有するため、突合の手掛かりとして使い、曖昧さは
    ambiguous フラグで明示する。
  - 結果は data/tmp/ に JSON 保存 + 標準出力にサマリ。

使い方:
  python scripts/audit_ebaymag_intl_2026_06_20.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = _ROOT / "data" / "monitor.db"
OUT_DIR = _ROOT / "data" / "tmp"


def fetch_live_listings() -> list[dict]:
    """ライブ GetMyeBaySelling から全 active listing (US本体 + 各国版) を取得."""
    from monitor.credentials import get_ebay_credentials
    from monitor.ebay_client import get_active_listings

    cr = get_ebay_credentials()
    return get_active_listings(cr["app_id"], cr["dev_id"], cr["cert_id"], cr["user_token"])


def load_us_baseline() -> dict[str, dict]:
    """MonoDeck DB の US 本体 active listing を ebay_item_id キーで取得."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ebay_item_id, sku, title, current_price, quantity_ebay,
               COALESCE(is_ended,0) AS is_ended, ebaymag_segment, primary_market
        FROM ebay_listings
        """
    ).fetchall()
    conn.close()
    return {str(r["ebay_item_id"]): dict(r) for r in rows if r["ebay_item_id"]}


def _to_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=== eBaymag 各国版 現状監査 (READ ONLY) 2026-06-20 ===")
    print("ライブ GetMyeBaySelling 取得中 ...")
    live = fetch_live_listings()
    print(f"  取得 listing 総数: {len(live)}")

    us_live = [l for l in live if (l.get("currency") or "USD") == "USD"]
    intl_live = [l for l in live if (l.get("currency") or "USD") != "USD"]
    print(f"  US本体 (USD): {len(us_live)} / 各国版 (非USD): {len(intl_live)}")

    # --- 各国版 通貨別 / qty 分布 ---
    cur_counter = Counter(l.get("currency") or "?" for l in intl_live)
    intl_qty_pos = [l for l in intl_live if _to_int(l.get("quantity")) > 0]
    intl_qty_zero = [l for l in intl_live if _to_int(l.get("quantity")) <= 0]
    print("\n-- 各国版 通貨別件数 --")
    for cur, n in cur_counter.most_common():
        print(f"  {cur}: {n}")
    print(f"-- 各国版 qty>0: {len(intl_qty_pos)} / qty<=0: {len(intl_qty_zero)} --")

    # --- 各国版 SKU パターン (API でのマッピング可否を確定) ---
    def _sku_kind(s: str) -> str:
        s = (s or "").strip()
        if not s:
            return "(empty)"
        sl = s.lower()
        if sl.startswith("stock"):
            return "stock**(共有)"
        if sl.startswith("ebay"):
            return "ebay**(無在庫)"
        return "other"
    intl_sku_kind = Counter(_sku_kind(l.get("sku")) for l in intl_live)
    print("\n-- 各国版 SKU パターン (US本体突合の可否) --")
    for k, n in intl_sku_kind.most_common():
        print(f"  {k}: {n}")

    # --- US本体 DB ベースライン (ebay_item_id キー) ---
    us_db = load_us_baseline()

    # --- US本体 を SKU でグルーピング (各国版突合の手掛かり) ---
    # 注: SKU は監査グルーピング用。listing 識別ではない (規約遵守)。
    us_by_sku: dict[str, list[dict]] = defaultdict(list)
    for iid, row in us_db.items():
        sku = (row.get("sku") or "").strip()
        if sku:
            us_by_sku[sku].append(row)

    # --- オーバーセル危険炙り出し ---
    # 各国版 qty>0 で、同 SKU の US本体が「全て ended or qty=0」= US在庫切れなのに
    # 各国版に在庫が残っている = 買われると履行不能 (#4 本丸)。
    oversell_risk = []
    intl_no_us_match = []  # SKU で US本体に対応が見つからない各国版 (要確認)
    for l in intl_qty_pos:
        sku = (l.get("sku") or "").strip()
        us_matches = us_by_sku.get(sku, [])
        if not us_matches:
            intl_no_us_match.append({
                "intl_item_id": l.get("item_id"), "sku": sku,
                "title": l.get("title"), "currency": l.get("currency"),
                "qty": _to_int(l.get("quantity")),
            })
            continue
        # US本体側で「生きている在庫」があるか
        us_alive_qty = any(
            (not m.get("is_ended")) and _to_int(m.get("quantity_ebay")) > 0
            for m in us_matches
        )
        if not us_alive_qty:
            oversell_risk.append({
                "intl_item_id": l.get("item_id"), "sku": sku,
                "title": l.get("title"), "currency": l.get("currency"),
                "intl_qty": _to_int(l.get("quantity")),
                "us_matches": [
                    {"ebay_item_id": m.get("ebay_item_id"),
                     "is_ended": m.get("is_ended"),
                     "us_qty": _to_int(m.get("quantity_ebay"))}
                    for m in us_matches
                ],
            })

    print(f"\n🚨 オーバーセル危険 (各国版 qty>0 だが US本体在庫なし): {len(oversell_risk)} 件")
    print(f"❓ 各国版だが US本体 SKU 不一致 (要確認): {len(intl_no_us_match)} 件")

    # --- 各国版がカバーしている US本体 SKU 数 vs プランv2 出品対象 ---
    intl_skus = {(l.get("sku") or "").strip() for l in intl_qty_pos if (l.get("sku") or "").strip()}
    print(f"\n-- 各国版が現在カバーしている US本体 SKU 数 (qty>0): {len(intl_skus)} --")

    result = {
        "generated_at": stamp,
        "live_total": len(live),
        "us_count": len(us_live),
        "intl_count": len(intl_live),
        "intl_by_currency": dict(cur_counter),
        "intl_qty_pos": len(intl_qty_pos),
        "intl_qty_zero": len(intl_qty_zero),
        "oversell_risk_count": len(oversell_risk),
        "oversell_risk": oversell_risk,
        "intl_no_us_match_count": len(intl_no_us_match),
        "intl_no_us_match": intl_no_us_match[:50],
        "intl_covered_us_skus": len(intl_skus),
        "intl_sku_kind": dict(intl_sku_kind),
    }
    # 設計フェーズ用 raw snapshot (各国版 全件: item_id/sku/title/currency/qty)
    raw_intl = [
        {"item_id": l.get("item_id"), "sku": (l.get("sku") or "").strip(),
         "title": l.get("title"), "currency": l.get("currency"),
         "qty": _to_int(l.get("quantity"))}
        for l in intl_live
    ]
    (OUT_DIR / f"audit_ebaymag_intl_raw_{stamp}.json").write_text(
        json.dumps(raw_intl, ensure_ascii=False, indent=2), encoding="utf-8")
    out_path = OUT_DIR / f"audit_ebaymag_intl_{stamp}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
