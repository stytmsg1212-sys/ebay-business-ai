#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
過去出品送料 audit script (2026-05-01 H-1 対応)

背景:
  4/26 fix の ShippingType=Flat 追加では <SellerShippingProfile> + <ShippingDetails>
  同居で送料 override が silently ignored される問題を解決できていなかった.
  過去出品で Domestic shipping が BP default ($30 等) のまま走っていた可能性大.
  5/1 β fix (ShippingServiceCostOverrideList = eBay 公式機構) 適用後の expected
  (price * 0.20) と実際の eBay 側設定値の差分を金額単位で可視化する.

実装:
  1. listing_drafts (status='applied' AND ebay_item_id IS NOT NULL) を全件取得
  2. 各 ebay_item_id に対して GetItem (DetailLevel=ReturnAll, IncludeSelector=Details,Shipping)
  3. <ShippingServiceOptions><ShippingServiceCost> の Domestic 値を抽出
  4. expected = listing_price_usd * 0.20 と比較、diff を計算
  5. 全件結果を .company/finance/audit-YYYY-MM-DD-shipping.md に保存

Usage:
  cd tools/ebay-manager && python scripts/audit_past_listings_shipping_2026_05_01.py
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import _call_trading_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("audit_shipping")

NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}


def _build_get_item_xml(item_id: str) -> str:
    """ShippingDetails 含む GetItem リクエスト."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">\n'
        '  <RequesterCredentials>\n'
        '    <eBayAuthToken>{USER_TOKEN}</eBayAuthToken>\n'
        '  </RequesterCredentials>\n'
        f'  <ItemID>{item_id}</ItemID>\n'
        '  <DetailLevel>ReturnAll</DetailLevel>\n'
        '  <IncludeSelector>Details,Shipping</IncludeSelector>\n'
        '</GetItemRequest>\n'
    )


def fetch_listing_shipping(item_id: str, creds: dict) -> dict:
    """eBay GetItem で listing の Domestic ShippingServiceCost を取得.

    Returns: {success: bool, domestic_cost / additional_cost / service_name, error?}
    """
    xml_body = _build_get_item_xml(item_id)
    result = _call_trading_api(
        "GetItem", xml_body,
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )

    if not result.get("success"):
        return {"success": False, "error": result.get("message")}

    raw = result.get("raw") or ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"success": False, "error": f"XML parse error: {e}"}

    item = root.find(".//ns:Item", NS)
    if item is None:
        return {"success": False, "error": "no <Item> in response"}

    sd = item.find("ns:ShippingDetails", NS)
    if sd is None:
        return {"success": False, "error": "no <ShippingDetails>"}

    # ShippingServiceOptions = Domestic services (複数あり得る)
    services = sd.findall("ns:ShippingServiceOptions", NS)
    if not services:
        return {"success": False, "error": "no <ShippingServiceOptions> (calculated shipping?)"}

    # 最初の Domestic service を採用 (Priority=1 相当)
    svc = services[0]
    cost_el = svc.find("ns:ShippingServiceCost", NS)
    add_el = svc.find("ns:ShippingServiceAdditionalCost", NS)
    name_el = svc.find("ns:ShippingService", NS)

    domestic_cost = None
    additional_cost = None
    if cost_el is not None and cost_el.text:
        try:
            domestic_cost = float(cost_el.text)
        except ValueError:
            pass
    if add_el is not None and add_el.text:
        try:
            additional_cost = float(add_el.text)
        except ValueError:
            pass

    return {
        "success": True,
        "domestic_cost": domestic_cost,
        "additional_cost": additional_cost,
        "service_name": name_el.text if name_el is not None and name_el.text else "?",
    }


def main() -> int:
    db_path = _PROJECT_ROOT / "data" / "monitor.db"
    if not db_path.exists():
        logger.error(f"DB not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, sku, ebay_item_id, ebay_title, listing_price_usd, "
        "status, created_at "
        "FROM listing_drafts "
        "WHERE status='applied' AND ebay_item_id IS NOT NULL "
        "ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        logger.info("audit 対象 0 件 (status='applied' な listing_drafts なし)")
        return 0

    logger.info(f"audit 対象 {len(rows)} 件")

    creds = get_ebay_credentials()
    missing = [k for k in ("app_id", "dev_id", "cert_id", "user_token") if not creds.get(k)]
    if missing:
        logger.error(f"eBay 認証情報が不足: {missing}")
        return 1

    results: list[dict] = []
    for r in rows:
        d = dict(r)
        item_id = d["ebay_item_id"]
        price = float(d["listing_price_usd"] or 0)
        expected = round(price * 0.20, 2)

        title_short = (d["ebay_title"] or "")[:60]
        logger.info(f"GetItem id={d['id']} item={item_id} ({title_short})")
        ship = fetch_listing_shipping(item_id, creds)

        if not ship.get("success"):
            err = ship.get("error", "unknown")
            logger.warning(f"  ERROR: {err}")
            results.append({
                **d,
                "expected": expected,
                "actual": None,
                "additional": None,
                "diff": None,
                "service": None,
                "audit_status": "error",
                "error": err,
            })
            continue

        actual = ship.get("domestic_cost")
        additional = ship.get("additional_cost")
        diff = (actual - expected) if actual is not None else None
        results.append({
            **d,
            "expected": expected,
            "actual": actual,
            "additional": additional,
            "diff": diff,
            "service": ship.get("service_name"),
            "audit_status": "ok" if actual is not None else "no_cost",
            "error": None,
        })

        if actual is None:
            logger.warning("  cost null (calculated shipping?)")
        else:
            sign = "+" if diff is not None and diff > 0 else ""
            add_disp = f"${additional:.2f}" if additional is not None else "(none)"
            logger.info(
                f"  expected ${expected:.2f} / actual ${actual:.2f} "
                f"(additional {add_disp}) / diff {sign}${diff:.2f} / service={ship.get('service_name')}"
            )

    # ─── markdown report ───
    out_dir = _PROJECT_ROOT.parent.parent / ".company" / "finance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"audit-{datetime.now().strftime('%Y-%m-%d')}-shipping.md"

    lines: list[str] = [
        "# 過去出品送料 audit (2026-05-01 H-1 対応)",
        "",
        f"生成日時: {datetime.now().isoformat(timespec='seconds')}",
        f"対象: listing_drafts WHERE status='applied' AND ebay_item_id IS NOT NULL = **{len(results)} 件**",
        "",
        "## 背景",
        "",
        "4/26 fix (ShippingType=Flat 追加) では `<SellerShippingProfile>` + `<ShippingDetails>` 同居で",
        "送料 override が silently ignored される問題が解決していなかった. 5/1 β fix で eBay 公式の",
        "`<ShippingServiceCostOverrideList>` 機構に切替済. 過去出品の実 Domestic shipping cost を",
        "GetItem で取得し expected (`price * 0.20`) との差分を audit する.",
        "",
        "## 結果",
        "",
        "| id | sku | price ($) | expected ($) | actual ($) | additional ($) | diff ($) | service | title (60 字) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    total_diff = 0.0
    audit_count_ok = 0
    audit_count_err = 0
    audit_count_nocost = 0
    matched_count = 0  # |diff| < $0.01 = 一致

    for r in results:
        title_disp = (r["ebay_title"] or "")[:60].replace("|", "\\|")
        if r["audit_status"] == "error":
            audit_count_err += 1
            lines.append(
                f"| {r['id']} | {r['sku'] or '-'} | {r['listing_price_usd']:.2f} | "
                f"{r['expected']:.2f} | ERROR | - | - | - | {title_disp} |"
            )
        elif r["audit_status"] == "no_cost":
            audit_count_nocost += 1
            lines.append(
                f"| {r['id']} | {r['sku'] or '-'} | {r['listing_price_usd']:.2f} | "
                f"{r['expected']:.2f} | (null) | - | - | {r.get('service') or '-'} | {title_disp} |"
            )
        else:
            audit_count_ok += 1
            sign = "+" if r["diff"] >= 0 else ""
            additional_disp = f"{r['additional']:.2f}" if r['additional'] is not None else "-"
            if abs(r["diff"]) < 0.01:
                matched_count += 1
            lines.append(
                f"| {r['id']} | {r['sku'] or '-'} | {r['listing_price_usd']:.2f} | "
                f"{r['expected']:.2f} | {r['actual']:.2f} | {additional_disp} | "
                f"{sign}{r['diff']:.2f} | {r['service']} | {title_disp} |"
            )
            total_diff += r["diff"]

    lines.extend([
        "",
        "## 集計",
        "",
        f"- 取得成功 (cost 取得済): **{audit_count_ok} 件**",
        f"  - うち expected 一致 (`|diff| < $0.01`): **{matched_count} 件**",
        f"  - うち diff あり: **{audit_count_ok - matched_count} 件**",
        f"- cost null (calculated shipping 等): {audit_count_nocost} 件",
        f"- GetItem error: {audit_count_err} 件",
        f"- **diff 合計: ${total_diff:+.2f}**",
        "",
        "## 解釈",
        "",
        "- **diff > 0** = 実 actual > expected → buyer が余分に払っている",
        "  - DDP 出荷では buyer が overcharge → buyer dispute / negative feedback リスク",
        "  - 出品者は表面上問題なし、ただし eBay UI の整合性問題",
        "- **diff < 0** = 実 actual < expected → 出品者が余分に負担",
        "  - 想定 20% より低い shipping cost で出品 → 出品者の利益直撃 (赤字方向)",
        "- **diff ≈ 0** = override が正しく機能 (5/1 β fix 後の出品 or 偶然 BP=20% 一致)",
        "",
        "## 推奨アクション",
        "",
        "1. diff > 0 の listing → buyer 連絡 / 価格調整検討 (eBay UI で送料を 20% に修正)",
        "2. diff < 0 の listing → 損失額把握、必要なら listing 一旦 end → 価格調整して revise",
        "3. β fix 後の新規出品で 0 になる(べき) ことを継続観測 (本 audit を定期実行)",
        "",
        "## Q1 DoD chain",
        "",
        "- pytest (unit): test_shipping_service_cost_override_list 4 件 PASS",
        "- 実機 verify (β fix): 2026-05-01 user 実施・通過確認済",
        "- 実機 audit (本 script): **本 report = 過去出品の ground truth**",
        "",
        "---",
        "",
        f"_Generated by `scripts/audit_past_listings_shipping_2026_05_01.py` at {datetime.now().isoformat(timespec='seconds')}._",
    ])

    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"=== report saved to {out_path}")
    logger.info(
        f"  ok={audit_count_ok} (matched={matched_count}) / "
        f"no_cost={audit_count_nocost} / err={audit_count_err}"
    )
    logger.info(f"  diff total: ${total_diff:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
