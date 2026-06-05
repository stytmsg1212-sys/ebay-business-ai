#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W222 Stage 0+1: category_id backfill (安全) + floor DRY-RUN diff (書込なし).

Stage 0 (backfill): active listing の category_id を GetItem(batch) で埋める。
  → category_id は floor の利用が settings.use_category_fvf_floor=True まで gate される
    ため、backfill しても lp_breakeven_usd (floor) は変わらない (money-safe)。

Stage 1 (DRY-RUN): 各 active listing で
  old_floor = 現 lp_breakeven_usd (固定 58248 で計算された値)
  new_floor = compute_breakeven_price_usd(実 category_id, 同 duty/is_ddu) をメモリ計算
  → diff を CSV + サマリ出力 (DB は書かない)。user 共同検証用。

使い方: cd tools/ebay-manager && python scripts/backfill_and_dryrun_category_w222.py
出力: data/tmp/w222_floor_dryrun_<未指定なら固定名>.csv + stdout サマリ
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from monitor.database import init_db, get_conn  # noqa: E402
from monitor.lowest_price import compute_breakeven_price_usd  # noqa: E402
from calculator import load_settings, get_ebay_fvf_rate, category_in_fee_table  # noqa: E402


def _load_creds() -> dict:
    cfg_path = _ROOT / "config" / "schedule_config.json"
    cfg = json.load(io.open(cfg_path, encoding="utf-8"))
    from monitor.credentials import get_ebay_credentials
    return get_ebay_credentials(cfg)


def backfill_category_id() -> dict:
    """active listing で category_id NULL のものを GetItem batch で埋める。"""
    from monitor.ebay_client import get_item_details_batch
    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id FROM ebay_listings "
            "WHERE category_id IS NULL AND (is_ended IS NULL OR is_ended=0) "
            "AND ebay_item_id IS NOT NULL AND ebay_item_id!=''"
        ).fetchall()
    item_ids = [r[0] for r in rows]
    print(f"[backfill] category_id NULL の active listing: {len(item_ids)} 件")
    if not item_ids:
        return {"target": 0, "filled": 0, "failed": 0}
    creds = _load_creds()
    details = get_item_details_batch(
        item_ids, creds.get("app_id", ""), creds.get("dev_id", ""),
        creds.get("cert_id", ""), creds.get("user_token", ""),
    )
    filled = 0
    failed = 0
    with get_conn() as c:
        for eid in item_ids:
            cid = (details.get(eid) or {}).get("category_id")
            if cid:
                c.execute(
                    "UPDATE ebay_listings SET category_id=? WHERE ebay_item_id=?",
                    (int(cid), eid),
                )
                filled += 1
            else:
                failed += 1
    print(f"[backfill] 埋めた: {filled} / 取得失敗(NULL残): {failed}")
    return {"target": len(item_ids), "filled": filled, "failed": failed}


def dryrun_floor_diff() -> None:
    """flag ON 相当の new_floor をメモリ計算し old_floor と比較 (DB 書込なし)。"""
    settings = load_settings()
    fx = float(settings.get("exchange_rate", 155.0))
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ebay_item_id, title, category_id, purchase_yen, weight_g, "
            "length_cm, width_cm, height_cm, primary_market, current_price, "
            "lp_breakeven_usd "
            "FROM ebay_listings "
            "WHERE (is_ended IS NULL OR is_ended=0) "
            "AND purchase_yen IS NOT NULL AND purchase_yen>0 "
            "AND weight_g IS NOT NULL AND weight_g>0"
        ).fetchall()]

    out_lines = ["ebay_item_id,tail4,category_id,in_fee_table,old_fvf%,new_fvf%,"
                 "old_floor,new_floor,diff_usd,current_price,flag"]
    n_down = n_up = n_same = n_below_price = n_missing_cat = n_not_in_table = 0
    n_calc_fail = 0
    examples = []
    for r in rows:
        cat = r.get("category_id")
        if not cat:
            n_missing_cat += 1
            cat_eff = 58248
        else:
            cat_eff = int(cat)
        if not category_in_fee_table(cat_eff):
            n_not_in_table += 1
        is_ddu = (r.get("primary_market") == "global_only")
        price = float(r.get("current_price") or 0)
        # 概算 FVF (total_sale=current_price で代表、Premium store)
        try:
            old_fvf = get_ebay_fvf_rate(58248, price or 50.0, "Premium")
            new_fvf = get_ebay_fvf_rate(cat_eff, price or 50.0, "Premium")
        except Exception:
            old_fvf = new_fvf = 0.0
        try:
            new_floor = compute_breakeven_price_usd(
                purchase_yen=r["purchase_yen"], weight_g=r["weight_g"],
                length_cm=r.get("length_cm") or 0, width_cm=r.get("width_cm") or 0,
                height_cm=r.get("height_cm") or 0, settings=settings,
                category_id=cat_eff, actual_duty_rate=None, is_ddu=is_ddu,
            )
        except Exception as e:  # noqa: BLE001 DRY-RUN は壊さず継続
            n_calc_fail += 1
            new_floor = None
        old_floor = r.get("lp_breakeven_usd")
        if new_floor is None or old_floor is None:
            flag = "calc_or_oldfloor_none"
        else:
            diff = round(new_floor - old_floor, 2)
            flags = []
            if diff < -0.01:
                n_down += 1
                flags.append("down")
            elif diff > 0.01:
                n_up += 1
                flags.append("up")
            else:
                n_same += 1
            if price > 0 and new_floor > price:
                n_below_price += 1
                flags.append("FLOOR>PRICE")
            flag = "|".join(flags) or "same"
            if abs(diff) > 5 or (price > 0 and new_floor > price):
                examples.append(
                    (r["ebay_item_id"], (r.get("title") or "")[:40], cat_eff,
                     old_floor, new_floor, diff, price, flag)
                )
        tail4 = (r["ebay_item_id"] or "")[-4:]
        out_lines.append(
            f"{r['ebay_item_id']},{tail4},{cat or ''},"
            f"{category_in_fee_table(cat_eff)},{old_fvf*100:.2f},{new_fvf*100:.2f},"
            f"{old_floor if old_floor is not None else ''},"
            f"{new_floor if new_floor is not None else ''},"
            f"{(round(new_floor-old_floor,2)) if (new_floor is not None and old_floor is not None) else ''},"
            f"{price},{flag if (new_floor is not None and old_floor is not None) else 'na'}"
        )

    out_dir = _ROOT / "data" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "w222_floor_dryrun.csv"
    out_csv.write_text("\n".join(out_lines), encoding="utf-8")

    print("\n===== W222 floor DRY-RUN サマリ (DB 未書込) =====")
    print(f"対象 active listing (purchase_yen+weight 有): {len(rows)} 件")
    print(f"  floor 下降 (値下げ余地拡大): {n_down}")
    print(f"  floor 上昇 (値下げ余地縮小=要注意): {n_up}")
    print(f"  変化なし: {n_same}")
    print(f"  🔴 new_floor > current_price (即赤字下限): {n_below_price}")
    print(f"  category_id 未backfill(58248 fallback): {n_missing_cat}")
    print(f"  EbayFeeRates.csv 未収録カテゴリ(既定12.7% fallback): {n_not_in_table}")
    print(f"  floor 計算失敗(skip): {n_calc_fail}")
    print(f"\n  大変動/危険 listing (|diff|>$5 or floor>price) 上位:")
    for ex in examples[:25]:
        print(f"    {ex[0]} cat={ex[2]} old=${ex[3]} new=${ex[4]} diff=${ex[5]} "
              f"price=${ex[6]} [{ex[7]}] {ex[1]}")
    print(f"\n  全件 CSV: {out_csv}")


if __name__ == "__main__":
    init_db()
    print("=== W222 Stage 0: category_id backfill ===")
    backfill_category_id()
    print("\n=== W222 Stage 1: floor DRY-RUN diff ===")
    dryrun_floor_diff()
