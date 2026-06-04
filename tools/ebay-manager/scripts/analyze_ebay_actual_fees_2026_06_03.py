"""W219 段1-2 (2026-06-03) one-shot 分析: eBay Finances API で実手数料を
取得し、calculator.calculate の推定手数料と突合する read-only スクリプト.

目的:
  - sales_history.ebay_fee_usd = 0 のハードコードを実値で埋める根拠データ取得
  - calculator (FVF 12.7% / 国際 1.2% / Payoneer 2% / Promoted 2% / 固定 ¥62.8)
    の component 別 gap を可視化
  - calculator/settings/task_order_alert は ★ 一切変更しない ★ (分析のみ)

入力:
  - eBay Finances API (sell.finances scope. 既に consent 済)
  - sales_history (DB) / ebay_listings (DB) で sold_price_usd / qty を取得

出力:
  - .company/engineering/docs/2026-06-03-ebay-actual-fees-analysis.md (体裁は
    2026-06-03-cpass-us-duty-actuals.md を踏襲: サマリ→件数→実効率→component
    別 gap→結論)
  - print も併行 (進捗 / errors)

使い方:
  python scripts/analyze_ebay_actual_fees_2026_06_03.py            # 180 日
  python scripts/analyze_ebay_actual_fees_2026_06_03.py --days 90
  python scripts/analyze_ebay_actual_fees_2026_06_03.py --no-write # md 書出さない
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

OUTPUT_PATH = (
    _ROOT.parent.parent / ".company" / "engineering" / "docs"
    / "2026-06-03-ebay-actual-fees-analysis.md"
)


def _iso_z(dt: datetime) -> str:
    """eBay Finances API filter は `YYYY-MM-DDTHH:MM:SS.sssZ` UTC を要求."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _fmt(v: float, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _pct(v: float, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def fetch_finances(days: int) -> dict:
    """eBay Finances API から直近 N 日の transactions を取得."""
    from monitor.ebay_client import get_transactions

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    print(
        f"[fetch] Finances API GET /transaction "
        f"start={_iso_z(start)} end={_iso_z(end)} ({days} days)"
    )
    res = get_transactions(_iso_z(start), _iso_z(end), limit=200)
    print(
        f"[fetch] success={res['success']} fetched={res['fetched']} "
        f"pages={res['pages']} truncated={res['truncated']} "
        f"last_status={res['last_status']}"
    )
    if res["errors"]:
        print("[fetch] errors:")
        for e in res["errors"]:
            print(f"  - {e}")
    return res


def collect_sales_map(item_ids: set[str]) -> dict:
    """ebay_item_id → {sold_price_usd, title, sku, sold_at, buyer_country,
    shipping_cost_usd} (最新 1 件) を sales_history から拾う."""
    from monitor.database import get_conn
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    out: dict[str, dict] = {}
    with get_conn() as c:
        rows = c.execute(
            f"""
            SELECT ebay_item_id, ebay_order_id, sku, title, sold_price_usd,
                   shipping_cost_usd, buyer_country, sold_at
            FROM sales_history
            WHERE ebay_item_id IN ({placeholders})
            ORDER BY sold_at DESC
            """,
            tuple(item_ids),
        ).fetchall()
    for r in rows:
        d = dict(r)
        key = d["ebay_item_id"]
        # 同 item_id で複数 sale は最新 1 件のみ
        if key not in out:
            out[key] = d
    return out


def collect_orders_map(order_ids: set[str]) -> dict:
    """ebay_order_id → list of sales_history rows (1 order N 商品対応)."""
    from monitor.database import get_conn
    if not order_ids:
        return {}
    placeholders = ",".join("?" * len(order_ids))
    out: dict[str, list[dict]] = defaultdict(list)
    with get_conn() as c:
        rows = c.execute(
            f"""
            SELECT ebay_item_id, ebay_order_id, sku, title, sold_price_usd,
                   shipping_cost_usd, buyer_country, sold_at
            FROM sales_history
            WHERE ebay_order_id IN ({placeholders})
            """,
            tuple(order_ids),
        ).fetchall()
    for r in rows:
        d = dict(r)
        out[d["ebay_order_id"]].append(d)
    return dict(out)


def collect_listings_for_calc(item_ids: set[str]) -> dict:
    """ebay_item_id → {weight_g, length_cm, width_cm, height_cm, category_id,
    primary_market} を ebay_listings から拾う. calculator.calculate 用."""
    from monitor.database import get_conn
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    out: dict[str, dict] = {}
    with get_conn() as c:
        # 利用可能なカラムだけ拾う (schema 変動耐性は SELECT *)
        try:
            rows = c.execute(
                f"""
                SELECT * FROM ebay_listings
                WHERE ebay_item_id IN ({placeholders})
                """,
                tuple(item_ids),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ebay_listings SELECT 失敗: {e}")
            return {}
    for r in rows:
        d = dict(r)
        out[d["ebay_item_id"]] = d
    return out


def predict_fees_for_order(
    *,
    sold_price_usd: float,
    qty: int,
    listing_row: dict | None,
    settings: dict,
) -> dict:
    """calculator.calculate を呼び、SALE 行に対する予測手数料 (FVF+intl+ad+
    payoneer+txn_fee) を USD で返す.

    送料/関税はここでは突合対象外 (W219 は eBay 手数料に focus).
    listing_row が無い (DB 未登録) なら category_id=0 / 重量推定で fallback.
    """
    from calculator import CalcInput, calculate

    cat_id = 0
    weight_g = 500.0
    length_cm = 0.0
    width_cm = 0.0
    height_cm = 0.0
    if listing_row:
        cat_id = int(listing_row.get("category_id") or 0)
        weight_g = float(listing_row.get("weight_g") or 500.0)
        length_cm = float(listing_row.get("length_cm") or 0.0)
        width_cm = float(listing_row.get("width_cm") or 0.0)
        height_cm = float(listing_row.get("height_cm") or 0.0)

    # 仕入価格は本分析では関係ない (手数料計算のみ取得). 0 で渡す.
    inp = CalcInput(
        purchase_yen=0.0,
        item_price_usd=float(sold_price_usd) * max(1, qty),
        weight_g=weight_g,
        length_cm=length_cm, width_cm=width_cm, height_cm=height_cm,
        category_id=cat_id,
        is_ddu=False,
        country_code="US",
    )
    try:
        result = calculate(inp, settings)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"calculate 失敗 (item={cat_id}): {e}")
        return {"fvf_usd": 0.0, "intl_usd": 0.0, "ad_usd": 0.0,
                "payoneer_usd": 0.0, "txn_fee_usd": 0.0,
                "total_usd": 0.0, "_error": str(e)}

    fx = settings["exchange_rate"]
    return {
        "fvf_usd": result.fvf / fx,
        "intl_usd": result.intl_payment / fx,
        "ad_usd": result.ad_fee / fx,
        "payoneer_usd": result.payoneer / fx,
        "txn_fee_usd": result.transaction_fee / fx,
        "total_usd": (
            result.fvf + result.intl_payment + result.ad_fee
            + result.payoneer + result.transaction_fee
        ) / fx,
    }


def analyze(days: int = 180, write_md: bool = True) -> int:
    from calculator import load_settings
    from collections import Counter as _Counter
    from monitor.database import init_db
    from monitor.ebay_client import parse_non_sale_charge, parse_sale_fees

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()

    res = fetch_finances(days)
    if not res["success"] and not res["transactions"]:
        # Q0: 完全失敗時は md を書かず exit. token / scope の問題を表に出す.
        print("[abort] Finances API 完全失敗 (transaction 0 件). md 出力 skip.")
        print(
            "[hint] 401/403 なら scope=sell.finances の user consent が"
            " 必要か token 失効. ebay_oauth_refresh.py --force で更新試行."
        )
        return 2

    # transactionType 分布を出す (実観測: 非SALE が大半 = Promoted/Adjust/Transfer)
    type_counter: _Counter = _Counter()
    for t in res["transactions"]:
        type_counter[str(t.get("transactionType") or "UNKNOWN")] += 1
    print("[parse] transactionType 分布:")
    for k, v in type_counter.most_common():
        print(f"  {k}: {v}")

    sales = [
        t for t in res["transactions"]
        if str(t.get("transactionType") or "").upper() == "SALE"
    ]
    non_sale_charges = [
        t for t in res["transactions"]
        if str(t.get("transactionType") or "").upper() == "NON_SALE_CHARGE"
    ]
    print(
        f"[parse] SALE={len(sales)} NON_SALE_CHARGE={len(non_sale_charges)}"
    )

    parsed_sales: list[dict] = []
    item_ids: set[str] = set()
    order_ids: set[str] = set()
    for t in sales:
        p = parse_sale_fees(t)
        parsed_sales.append(p)
        if p["order_id"]:
            order_ids.add(p["order_id"])
        for li in p["line_items"]:
            if li["item_id"]:
                item_ids.add(li["item_id"])

    parsed_charges: list[dict] = []
    for t in non_sale_charges:
        p = parse_non_sale_charge(t)
        parsed_charges.append(p)
        if p["item_id"]:
            item_ids.add(p["item_id"])
        if p["order_id_ref"]:
            order_ids.add(p["order_id_ref"])

    print(f"[parse] orderId 種類={len(order_ids)} itemId 種類={len(item_ids)}")

    # DB lookup
    orders_map = collect_orders_map(order_ids)
    listings_map = collect_listings_for_calc(item_ids)
    print(
        f"[db] sales_history hit orders={len(orders_map)} "
        f"/ ebay_listings hit items={len(listings_map)}"
    )

    settings = load_settings()
    fx = settings["exchange_rate"]

    # ─── 集計: SALE 全体 (DB 突合不要) ───
    total_fee_usd_sum = sum(p["total_fee_usd"] for p in parsed_sales)
    total_amount_usd_sum = sum(p["amount_usd"] for p in parsed_sales)
    fees_by_type_agg: Counter = Counter()
    for p in parsed_sales:
        for ft, v in p["fees_by_type"].items():
            fees_by_type_agg[ft] += v

    # NON_SALE_CHARGE 由来 fee (Promoted Listings 等) を集計に統合.
    # SALE 単体集約だと AD=$0 になる (実観測) ため、これを足さないと calculator
    # の AD 2% 予測と突合できない. CREDIT (戻し) は debit から差し引く.
    non_sale_fee_total_debit = sum(
        p["amount_usd_debit"] for p in parsed_charges
    )
    non_sale_fee_total_credit = sum(
        p["amount_usd_credit"] for p in parsed_charges
    )
    non_sale_net = non_sale_fee_total_debit - non_sale_fee_total_credit
    # feeType 別 (NON_SALE_CHARGE 内訳)
    non_sale_by_type: Counter = Counter()
    for p in parsed_charges:
        ft = p["fee_type"] or "UNKNOWN_NON_SALE"
        # net = debit - credit (CREDIT は対象 fee の戻しと仮定)
        non_sale_by_type[ft] += (p["amount_usd_debit"] - p["amount_usd_credit"])
        fees_by_type_agg[ft] += (p["amount_usd_debit"] - p["amount_usd_credit"])

    print(
        f"[parse] NON_SALE_CHARGE 合計: debit ${non_sale_fee_total_debit:.2f} "
        f"/ credit ${non_sale_fee_total_credit:.2f} / "
        f"net ${non_sale_net:.2f}"
    )
    # SALE 手数料 + NON_SALE_CHARGE net = 真の eBay 手数料合計
    total_fee_with_non_sale_usd = total_fee_usd_sum + non_sale_net

    eff_rate_overall = (
        total_fee_usd_sum / total_amount_usd_sum
        if total_amount_usd_sum > 0 else None
    )
    eff_rate_overall_with_promoted = (
        total_fee_with_non_sale_usd / total_amount_usd_sum
        if total_amount_usd_sum > 0 else None
    )

    # ─── per-order 突合 (sales_history + ebay_listings join) ───
    rows: list[dict] = []
    for p in parsed_sales:
        oid = p["order_id"]
        sh_rows = orders_map.get(oid, [])
        if not sh_rows:
            # DB 未登録 (古い注文 等). amount でしか分析できない (price not known).
            rows.append({
                "order_id": oid,
                "txn_date": p["transaction_date"],
                "amount_usd": p["amount_usd"],
                "sold_price_usd": None,
                "total_fee_usd": p["total_fee_usd"],
                "fees_by_type": dict(p["fees_by_type"]),
                "predicted_total_usd": None,
                "predicted_by_type": {},
                "title": "(DB 未登録)",
                "buyer_country": "?",
                "qty": 0,
                "matched": False,
            })
            continue

        # 1 注文 N 商品: line_item ごとに price/qty を sales_history から束ねる.
        sold_price_total = sum(
            float(r.get("sold_price_usd") or 0.0) for r in sh_rows
        )
        qty_total = len(sh_rows)
        title = (sh_rows[0].get("title") or "")[:60]
        buyer_country = sh_rows[0].get("buyer_country") or ""

        # 予測: line_item 毎に calculate しても category_id が項目別で異なるため、
        # 「order 合算の amount」を 1 件として calculate (calculator は item 単位
        # 想定だが、本分析は order 合算の実効率を見るため十分な近似).
        # listing は line_item の最初の item_id を代表に使う.
        first_item_id = ""
        for li in p["line_items"]:
            if li["item_id"]:
                first_item_id = li["item_id"]
                break
        pred = predict_fees_for_order(
            sold_price_usd=sold_price_total / max(1, qty_total),
            qty=qty_total,
            listing_row=listings_map.get(first_item_id),
            settings=settings,
        )

        rows.append({
            "order_id": oid,
            "txn_date": p["transaction_date"],
            "amount_usd": p["amount_usd"],
            "sold_price_usd": sold_price_total,
            "total_fee_usd": p["total_fee_usd"],
            "fees_by_type": dict(p["fees_by_type"]),
            "predicted_total_usd": pred["total_usd"],
            "predicted_by_type": {
                "FVF": pred["fvf_usd"],
                "INTERNATIONAL": pred["intl_usd"],
                "AD": pred["ad_usd"],
                "PAYONEER": pred["payoneer_usd"],
                "TXN_FEE": pred["txn_fee_usd"],
            },
            "title": title,
            "buyer_country": buyer_country,
            "qty": qty_total,
            "matched": True,
        })

    matched_rows = [r for r in rows if r["matched"]]
    print(f"[match] matched={len(matched_rows)} / total SALE rows={len(rows)}")

    # ─── 実効率分布 ───
    eff_rates = []
    for r in matched_rows:
        if r["sold_price_usd"] and r["sold_price_usd"] > 0:
            eff_rates.append(r["total_fee_usd"] / r["sold_price_usd"])
    eff_mean = statistics.mean(eff_rates) if eff_rates else None
    eff_median = statistics.median(eff_rates) if eff_rates else None
    eff_stdev = statistics.stdev(eff_rates) if len(eff_rates) > 1 else None
    eff_min = min(eff_rates) if eff_rates else None
    eff_max = max(eff_rates) if eff_rates else None

    # ─── component 別 gap (matched のみ) ───
    # 実 fee_type と calculator の対応:
    #   FINAL_VALUE_FEE_*           ↔ FVF (FINAL_VALUE_FEE_FIXED_PER_ORDER は固定 + FINAL_VALUE_FEE は率)
    #   INTERNATIONAL_FEE           ↔ INTERNATIONAL (1.2%)
    #   AD_FEE / AD_FEE_OVER_LISTING_CAP / PROMOTED_LISTINGS_STANDARD_FEE 等 ↔ AD (2.0%)
    #   PAYONEER_*                  ↔ (実は eBay 側に payoneer fee は無く、別経路で控除. calculator 内 2% は別)
    # 観測上は eBay 側 fee は FVF + INTL + AD + REGULATORY_OPERATING_FEE が主.
    actual_by_type: Counter = Counter()
    predicted_by_type: Counter = Counter()
    for r in matched_rows:
        for ft, v in r["fees_by_type"].items():
            actual_by_type[ft] += v
        for ft, v in r["predicted_by_type"].items():
            predicted_by_type[ft] += v

    # ─── md 出力 ───
    if write_md:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_PATH.open("w", encoding="utf-8") as f:
            f.write(_build_markdown(
                days=days,
                fetch_result=res,
                total_sales=len(parsed_sales),
                total_non_sale_charges=len(parsed_charges),
                type_counter=dict(type_counter),
                matched_rows=matched_rows,
                rows=rows,
                eff_rate_overall=eff_rate_overall,
                eff_rate_overall_with_promoted=eff_rate_overall_with_promoted,
                total_amount_usd=total_amount_usd_sum,
                total_fee_usd=total_fee_usd_sum,
                total_fee_with_non_sale_usd=total_fee_with_non_sale_usd,
                non_sale_by_type=dict(non_sale_by_type),
                fees_by_type_agg=dict(fees_by_type_agg),
                eff_stats={
                    "mean": eff_mean, "median": eff_median,
                    "stdev": eff_stdev, "min": eff_min, "max": eff_max,
                    "n": len(eff_rates),
                },
                actual_by_type=dict(actual_by_type),
                predicted_by_type=dict(predicted_by_type),
                settings=settings,
            ))
        print(f"[write] {OUTPUT_PATH}")
    else:
        print("[skip] md 出力 skip (--no-write)")

    # ─── headline 出力 ───
    print("")
    print("=" * 60)
    print(f"HEADLINE (直近 {days} 日 / SALE={len(parsed_sales)} / "
          f"matched={len(matched_rows)})")
    print("=" * 60)
    print(f"実効手数料率 (matched 平均 SALE 内のみ): {_pct(eff_mean)}  "
          f"median {_pct(eff_median)}  range {_pct(eff_min)}–{_pct(eff_max)}")
    print(f"全 SALE 集約: 売上 ${_fmt(total_amount_usd_sum)} / "
          f"SALE 手数料 ${_fmt(total_fee_usd_sum)} / 率 {_pct(eff_rate_overall)}")
    print(f"Promoted 等 NON_SALE_CHARGE 込み: 手数料 "
          f"${_fmt(total_fee_with_non_sale_usd)} / "
          f"率 {_pct(eff_rate_overall_with_promoted)}")
    print("")
    print("feeType 別実績 (USD):")
    for ft, v in sorted(actual_by_type.items(), key=lambda x: -x[1]):
        print(f"  {ft:42s} ${_fmt(v)}")
    print("")
    print("calculator 予測 component 別 (matched 合計 USD):")
    for ft, v in sorted(predicted_by_type.items(), key=lambda x: -x[1]):
        print(f"  {ft:42s} ${_fmt(v)}")
    print("")

    # gap 推測 (FVF + INTL + AD のみ突合)
    a_fvf = sum(v for k, v in actual_by_type.items()
                if "FINAL_VALUE" in k.upper())
    a_intl = sum(v for k, v in actual_by_type.items()
                 if "INTERNATIONAL" in k.upper())
    # AD は SALE 内 fees_by_type と NON_SALE_CHARGE の Promoted を合算
    a_ad_sale = sum(
        v for k, v in actual_by_type.items()
        if "AD" in k.upper() or "PROMOTED" in k.upper()
    )
    a_ad_promoted = sum(
        v for k, v in fees_by_type_agg.items()
        if "AD" in k.upper() or "PROMOTED" in k.upper()
    )
    # 注: actual_by_type は matched SALE 内のみ. promoted (NON_SALE_CHARGE) は
    # 全 ad-fee を集約する fees_by_type_agg で見るのが正しい.
    a_reg = sum(v for k, v in actual_by_type.items()
                if "REGULATORY" in k.upper() or "OPERATING" in k.upper())
    p_fvf = predicted_by_type.get("FVF", 0.0)
    p_intl = predicted_by_type.get("INTERNATIONAL", 0.0)
    p_ad = predicted_by_type.get("AD", 0.0)
    print("=== component gap (実 - 予測, USD) ===")
    print(f"  FVF系          : ${_fmt(a_fvf - p_fvf):>10s}  "
          f"(実 ${_fmt(a_fvf)} vs 予測 ${_fmt(p_fvf)})")
    print(f"  INTERNATIONAL  : ${_fmt(a_intl - p_intl):>10s}  "
          f"(実 ${_fmt(a_intl)} vs 予測 ${_fmt(p_intl)})")
    print(f"  AD/PROMOTED    : ${_fmt(a_ad_promoted - p_ad):>10s}  "
          f"(実 ${_fmt(a_ad_promoted)} [NON_SALE_CHARGE 込み] "
          f"vs 予測 ${_fmt(p_ad)})")
    print(f"    内訳 SALE 内 AD: ${_fmt(a_ad_sale)} / "
          f"全 AD(SALE+NON): ${_fmt(a_ad_promoted)}")
    print(f"  REGULATORY     : ${_fmt(a_reg):>10s}  "
          f"(計算式に該当 component なし = 全額 gap)")
    print("")
    return 0


def _build_markdown(
    *,
    days: int,
    fetch_result: dict,
    total_sales: int,
    total_non_sale_charges: int,
    type_counter: dict,
    matched_rows: list[dict],
    rows: list[dict],
    eff_rate_overall: float | None,
    eff_rate_overall_with_promoted: float | None,
    total_amount_usd: float,
    total_fee_usd: float,
    total_fee_with_non_sale_usd: float,
    non_sale_by_type: dict,
    fees_by_type_agg: dict,
    eff_stats: dict,
    actual_by_type: dict,
    predicted_by_type: dict,
    settings: dict,
) -> str:
    lines: list[str] = []
    lines.append(
        f"# eBay 実手数料 vs 計算式予測 突合分析 ({datetime.now():%Y-%m-%d})"
    )
    lines.append("")
    lines.append(
        "出典: eBay Finances API `/sell/finances/v1/transaction` "
        f"(直近 {days} 日, scope=sell.finances, GET only). "
        "calculator.calculate (FVF / INTERNATIONAL / AD / PAYONEER / "
        "TXN_FEE) を sales_history + ebay_listings の category_id /"
        " weight_g で再計算し突合."
    )
    lines.append("")
    lines.append(
        f"対象: SALE transaction. SALE={total_sales}, "
        f"NON_SALE_CHARGE={total_non_sale_charges} (Promoted Listings等 別 entry), "
        f"DB と紐付け成功 (sales_history.ebay_order_id 一致) = {len(matched_rows)}. "
        f"全 SALE 集約売上 ${_fmt(total_amount_usd)} / SALE 内 手数料 "
        f"${_fmt(total_fee_usd)} / 実効率 {_pct(eff_rate_overall)} / "
        f"NON_SALE_CHARGE 込み実効率 {_pct(eff_rate_overall_with_promoted)}."
    )
    lines.append("")
    lines.append("### transactionType 分布")
    lines.append("")
    lines.append("| transactionType | 件数 |")
    lines.append("|---|---:|")
    for k, v in sorted(type_counter.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## 核心結論")
    lines.append("")
    if eff_stats["n"] == 0:
        lines.append(
            "**matched 件数 0 = 実効率算定不能**. sales_history が直近 90 日しか "
            "保持していないため、Finances 取得期間と sales_history backfill 期間が "
            "ズレている可能性が高い (W149 backfill は 89 日)."
        )
    else:
        # gap 計算
        a_fvf = sum(v for k, v in actual_by_type.items()
                    if "FINAL_VALUE" in k.upper())
        a_intl = sum(v for k, v in actual_by_type.items()
                     if "INTERNATIONAL" in k.upper())
        a_ad = sum(v for k, v in actual_by_type.items()
                   if "AD" in k.upper() or "PROMOTED" in k.upper())
        a_reg = sum(v for k, v in actual_by_type.items()
                    if "REGULATORY" in k.upper() or "OPERATING" in k.upper())
        p_fvf = predicted_by_type.get("FVF", 0.0)
        p_intl = predicted_by_type.get("INTERNATIONAL", 0.0)
        p_ad = predicted_by_type.get("AD", 0.0)
        lines.append(
            f"- **matched 平均 実効率**: {_pct(eff_stats['mean'])} "
            f"(median {_pct(eff_stats['median'])}, "
            f"range {_pct(eff_stats['min'])}–{_pct(eff_stats['max'])}, n={eff_stats['n']})"
        )
        lines.append(
            "- **calculator 予測平均 (FVF+INTL+AD+Payoneer+TxnFee)** = "
            "現在の settings (FVF カテゴリ別, INTL 1.2%, AD 2.0%, Payoneer 2.0%, "
            "TxnFee ¥62.8/件) で再計算."
        )
        lines.append("- **component 別 gap (実 - 予測, USD, matched 合計)**:")
        lines.append(f"  - FVF 系: ${_fmt(a_fvf - p_fvf)} "
                     f"(実 ${_fmt(a_fvf)} vs 予測 ${_fmt(p_fvf)})")
        lines.append(f"  - INTERNATIONAL: ${_fmt(a_intl - p_intl)} "
                     f"(実 ${_fmt(a_intl)} vs 予測 ${_fmt(p_intl)})")
        lines.append(f"  - AD / PROMOTED: ${_fmt(a_ad - p_ad)} "
                     f"(実 ${_fmt(a_ad)} vs 予測 ${_fmt(p_ad)})")
        lines.append(f"  - REGULATORY_OPERATING_FEE: ${_fmt(a_reg)} "
                     "(calculator に該当 component なし = 全額 gap、新規追加候補)")
    lines.append("")

    lines.append("## fetch 結果")
    lines.append("")
    lines.append(f"- success: {fetch_result['success']}")
    lines.append(f"- fetched (transactions): {fetch_result['fetched']}")
    lines.append(f"- pages: {fetch_result['pages']}")
    lines.append(f"- truncated: {fetch_result['truncated']}")
    lines.append(f"- last_status: {fetch_result['last_status']}")
    if fetch_result["errors"]:
        lines.append("- errors:")
        for e in fetch_result["errors"]:
            lines.append(f"  - `{e}`")
    lines.append("")

    lines.append("## feeType 別実績 (全 SALE + NON_SALE_CHARGE 集約)")
    lines.append("")
    lines.append("| feeType | USD 合計 (net) | 比率 (vs 売上) |")
    lines.append("|---|---:|---:|")
    for ft, v in sorted(fees_by_type_agg.items(), key=lambda x: -x[1]):
        ratio = (v / total_amount_usd) if total_amount_usd > 0 else 0.0
        lines.append(f"| {ft} | ${_fmt(v)} | {_pct(ratio)} |")
    lines.append("")
    if non_sale_by_type:
        lines.append("### うち NON_SALE_CHARGE 内訳 (net = debit - credit)")
        lines.append("")
        lines.append("| feeType | USD 合計 (net) |")
        lines.append("|---|---:|")
        for ft, v in sorted(non_sale_by_type.items(), key=lambda x: -x[1]):
            lines.append(f"| {ft} | ${_fmt(v)} |")
        lines.append("")
        lines.append(
            f"NON_SALE_CHARGE 合計 (net): ${_fmt(total_fee_with_non_sale_usd - total_fee_usd)}"
        )
        lines.append("")

    lines.append("## 実効率分布 (matched, sold_price_usd で割った値)")
    lines.append("")
    lines.append(f"- n = {eff_stats['n']}")
    lines.append(f"- 平均: {_pct(eff_stats['mean'])}")
    lines.append(f"- median: {_pct(eff_stats['median'])}")
    if eff_stats["stdev"] is not None:
        lines.append(f"- stdev: {_pct(eff_stats['stdev'])}")
    lines.append(f"- 範囲: {_pct(eff_stats['min'])} – {_pct(eff_stats['max'])}")
    lines.append("")

    lines.append("## per-order 突合 (matched のみ, 上位 50 件 amount 降順)")
    lines.append("")
    lines.append(
        "| order_id | date | title | qty | sold_USD | 実fee | 予fee | gap | rate% |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    sorted_rows = sorted(
        matched_rows, key=lambda r: r["amount_usd"], reverse=True
    )[:50]
    for r in sorted_rows:
        gap = (r["total_fee_usd"] - (r["predicted_total_usd"] or 0.0))
        rate = (r["total_fee_usd"] / r["sold_price_usd"]
                if r["sold_price_usd"] else 0.0)
        oid_short = r["order_id"][-10:] if r["order_id"] else ""
        date_short = (r["txn_date"] or "")[:10]
        title_short = (r["title"] or "")[:30]
        lines.append(
            f"| {oid_short} | {date_short} | {title_short} | "
            f"{r['qty']} | ${_fmt(r['sold_price_usd'])} | "
            f"${_fmt(r['total_fee_usd'])} | "
            f"${_fmt(r['predicted_total_usd'])} | "
            f"${_fmt(gap)} | {_pct(rate)} |"
        )
    lines.append("")

    lines.append("## 含意 (money-direct)")
    lines.append("")
    if eff_stats["n"] > 0:
        a_reg = sum(v for k, v in actual_by_type.items()
                    if "REGULATORY" in k.upper() or "OPERATING" in k.upper())
        a_intl = sum(v for k, v in actual_by_type.items()
                     if "INTERNATIONAL" in k.upper())
        a_ad = sum(v for k, v in actual_by_type.items()
                   if "AD" in k.upper() or "PROMOTED" in k.upper())
        a_fvf = sum(v for k, v in actual_by_type.items()
                    if "FINAL_VALUE" in k.upper())
        p_fvf = predicted_by_type.get("FVF", 0.0)
        p_intl = predicted_by_type.get("INTERNATIONAL", 0.0)
        p_ad = predicted_by_type.get("AD", 0.0)
        lines.append(
            "1. **`sales_history.ebay_fee_usd = 0.0` ハードコード "
            "(task_order_alert.py:653) は実値で埋められる**. Finances API は "
            "scope=sell.finances で叩け、order_id 経由で sales_history に "
            "INSERT/UPDATE 可能 (本 W は分析のみ、書込は別段)."
        )
        if a_reg > 0:
            lines.append(
                f"2. **REGULATORY_OPERATING_FEE ${_fmt(a_reg)} は calculator に "
                "ない**. EU/UK 等の規制手数料で、米国 buyer 主体でも数% 発生. "
                "calculator に component 追加候補."
            )
        else:
            lines.append(
                "2. REGULATORY_OPERATING_FEE は本期間 0 (米国 DDP 主体ゆえ妥当)."
            )
        if abs(a_fvf - p_fvf) / max(p_fvf, 1) > 0.05:
            lines.append(
                "3. **FVF 実 vs 予測 が 5% 超ズレ**. カテゴリ別 fee table "
                "(`data/EbayFeeRates.csv`) の rate / threshold が現行 eBay 課金体系と "
                "一致していない可能性. EbayFeeRates.csv の再検証 + Top Rated "
                "Plus 10% 割引 (settings.seller_level=\"Top Rated\") 適用有無を確認."
            )
        if abs(a_intl - p_intl) / max(p_intl, 1) > 0.10:
            lines.append(
                f"4. **INTERNATIONAL 実 ${_fmt(a_intl)} vs 予測 ${_fmt(p_intl)} が "
                "10% 超ズレ**. 1.2% rate の見直し or 米国 buyer 比率の見直し."
            )
        if abs(a_ad - p_ad) / max(p_ad, 1) > 0.10:
            lines.append(
                f"5. **AD/PROMOTED 実 ${_fmt(a_ad)} vs 予測 ${_fmt(p_ad)} が "
                "10% 超ズレ**. campaign bid 2% override が一部 listing に "
                "未適用 (キャンペーン rate 9% が直接かかっている) 可能性, "
                "または over-listing cap 追加 fee."
            )
    lines.append("")
    lines.append(
        "⚠️ 本 W219 段1-2 は分析のみ. calculator/settings/task_order_alert は "
        "★ 触っていない ★. 較正は分析結果を user が確認後、別段で実施."
    )
    lines.append("")
    lines.append(
        "## 注: Payoneer 手数料は eBay Finances API には現れない"
    )
    lines.append("")
    lines.append(
        "calculator の payoneer 2% は eBay → Payoneer 口座への送金時に "
        "Payoneer 側で控除される手数料で、eBay の totalFeeAmount には含まれない. "
        "本分析の \"calculator 予測\" は Payoneer 込みで予測値が膨らむが、実 "
        "(actual_by_type) は eBay 控除分のみで Payoneer 分は含まない. "
        "gap を見るときは PAYONEER component を除外して FVF/INTL/AD のみで比較するのが正しい."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 全 matched 行 (JSON 形式 backup, raw データ)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(matched_rows, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180,
                        help="lookback 日数 (default 180)")
    parser.add_argument("--no-write", action="store_true",
                        help="md 出力を skip (print のみ)")
    args = parser.parse_args()
    sys.exit(analyze(days=args.days, write_md=not args.no_write))
