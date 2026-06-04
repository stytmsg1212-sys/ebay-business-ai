"""W219 段3 前段 (2026-06-04) read-only 診断: FVF の「真の系統 gap」再算定。

背景:
  2026-06-03 の analyze_ebay_actual_fees は「FVF 実 vs 予測 +8.5%」「rate 最大
  95.20%」を出したが、これは
    (a) rate% = 実fee / sold_price_usd (= 商品代のみ。FVF が課金される
        「商品代 + buyer送料」を分母にしていない) という計測アーティファクト
    (b) 予測側が listing 重量欠損時 500g fallback で buyer送料を過小推定し、
        予測 FVF ベースが過小 = 予測 FVF 過小 = 「実 > 予測」gap
  が主因の疑いが濃い (重い産業機器ほど顕著)。

本スクリプトは calculator/settings/DB を ★ 一切変更しない ★。
matched SALE について **正しいベース (sold_price + 実 buyer送料) で actual FVF
率を再算定** し、CSV カテゴリ率と突合して
  - 重量アーティファクト (ベース補正で説明できる gap)
  - 真の異常値 (補正後も率が CSV を大きく超える = 返金/紛争/調整の混入)
を分離し、正常注文だけの「真の系統 FVF gap」を出す。

使い方:
  python scripts/analyze_fvf_true_gap_2026_06_04.py            # 180 日
  python scripts/analyze_fvf_true_gap_2026_06_04.py --days 90
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


DOC = (_ROOT.parent.parent / ".company" / "engineering" / "docs"
       / "2026-06-03-ebay-actual-fees-analysis.md")


def _load_matched_from_doc() -> list[dict]:
    """分析 md 末尾の ```json ブロック (matched 119 件) を抽出して返す。
    API token 失効中でも DB join で正base率を再算定できるようにするため。"""
    import json
    text = DOC.read_text(encoding="utf-8")
    marker = "## 全 matched 行"
    i = text.find(marker)
    j = text.find("```json", i)
    k = text.find("```", j + 7)
    return json.loads(text[j + 7:k].strip())


def main(days: int = 180) -> int:
    from calculator import get_ebay_fvf_rate, load_settings
    from monitor.database import get_conn, init_db

    init_db()
    settings = load_settings()
    store_plan = settings.get("store_plan", "Premium")

    matched = _load_matched_from_doc()
    print(f"[doc] matched 行 読込: {len(matched)} 件 ({DOC.name})")

    # ── per-order: FVF 課金ベース = 総額(gross) = amount_usd(純入金) + total_fee。
    # これは「単価×個数 + buyer送料」に等しい (qty/送料を sales_history に頼らず
    # Finances 実額のみで正しく出せる)。
    # 注: sales_history.sold_price_usd は **単価1個分・qty列なし** のため、これで
    #     割ると複数個購入で率が膨張する (2026-06-04 user 指摘: BMUD200-A は 5個/
    #     2個のバルク販売だった)。よって旧 rate_item(単価割り) は誤り。
    # CSV 率: ebay_listings に eBay category_id 列が無く category 別取得不能 →
    #     元分析と同じ default 率 (store 12.7%) を「calculator が使った値」として比較。
    recs: list[dict] = []
    for m in matched:
        oid = m.get("order_id")
        amount = float(m.get("amount_usd") or 0.0)       # 純入金 (= gross - 全fee)
        total_fee = float(m.get("total_fee_usd") or 0.0)
        unit_price = float(m.get("sold_price_usd") or 0.0)  # 単価1個分 (参考)
        gross = amount + total_fee                        # 総額 = 単価×個数 + 送料
        fvf_actual = sum(v for k, v in (m.get("fees_by_type") or {}).items()
                         if "FINAL_VALUE_FEE" in k.upper()
                         and "FIXED" not in k.upper())  # 率部分のみ (固定 $0.44 除外)
        if fvf_actual <= 0 or gross <= 0:
            continue
        cat = 0  # category 別不能 → default 率
        csv_rate = get_ebay_fvf_rate(cat, gross, store_plan)
        rate_on_unit = fvf_actual / unit_price if unit_price else 0.0  # 旧(誤): 単価割り
        rate_on_gross = fvf_actual / gross                            # 正: 総額割り
        recs.append({
            "oid": (oid or "")[-10:], "title": (m.get("title") or "")[:28],
            "price": unit_price, "ship": round(gross - unit_price, 1),  # gross-単価(参考)
            "fvf": fvf_actual,
            "rate_item": rate_on_unit, "rate_base": rate_on_gross,
            "csv": csv_rate, "cat": cat,
        })

    if not recs:
        print("[abort] matched 0 (DB 突合 0). sales_history backfill 期間を確認.")
        return 2

    # ── 異常値分離: 正しいベースでも CSV を 1.5x 超 or 25% 超 = 真の異常 (返金/紛争) ──
    def is_anomaly(r: dict) -> bool:
        return r["rate_base"] > max(0.25, r["csv"] * 1.5)

    clean = [r for r in recs if not is_anomaly(r)]
    anom = [r for r in recs if is_anomaly(r)]

    def _mean(xs):
        return statistics.mean(xs) if xs else 0.0

    print("\n" + "=" * 72)
    print(f"FVF 真の系統 gap 診断 (n={len(recs)} matched / clean={len(clean)} "
          f"/ 異常={len(anom)})  store_plan={store_plan}")
    print("=" * 72)
    print(f"[誤] 実FVF率 (÷単価1個分, 個数/送料無視) 平均 {_mean([r['rate_item'] for r in recs])*100:6.2f}%  "
          f"max {max(r['rate_item'] for r in recs)*100:6.2f}%   ← qty無視で膨張(誤)")
    print(f"[正] 実FVF率 (÷総額gross=単価×個数+送料)  平均 {_mean([r['rate_base'] for r in recs])*100:6.2f}%  "
          f"max {max(r['rate_base'] for r in recs)*100:6.2f}%")
    print(f"[基準] CSV カテゴリ率 (calculator が使う値)  平均 {_mean([r['csv'] for r in recs])*100:6.2f}%")
    print("")
    print(f"clean (正常) 注文の 実FVF率(正base) 平均 = {_mean([r['rate_base'] for r in clean])*100:.2f}%  "
          f"vs CSV {_mean([r['csv'] for r in clean])*100:.2f}%")
    gap = _mean([r["rate_base"] for r in clean]) - _mean([r["csv"] for r in clean])
    print(f"  → 真の系統 FVF gap (clean, 正base) = {gap*100:+.2f} 絶対pt "
          f"({gap / max(_mean([r['csv'] for r in clean]), 1e-9) * 100:+.1f}% 相対)")
    print("")
    print(f"--- 真の異常値 ({len(anom)} 件: 正base補正後も CSV を大きく超過 = 返金/紛争/調整疑い) ---")
    for r in sorted(anom, key=lambda x: -x["rate_base"]):
        print(f"  {r['oid']} {r['title']:30s} price=${r['price']:.0f} ship=${r['ship']:.0f} "
              f"fvf=${r['fvf']:.0f}  実率(正base)={r['rate_base']*100:5.1f}% vs CSV {r['csv']*100:.1f}%")
    print("")
    print("※ 重量アーティファクト検証: 旧率(膨張) と 新率(正base) の差が大きい注文 top5")
    for r in sorted(recs, key=lambda x: -(x["rate_item"] - x["rate_base"]))[:5]:
        print(f"  {r['oid']} {r['title']:30s} 旧={r['rate_item']*100:5.1f}% → 新={r['rate_base']*100:5.1f}% "
              f"(ship=${r['ship']:.0f} が分母に入った効果)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args()
    sys.exit(main(days=args.days))
