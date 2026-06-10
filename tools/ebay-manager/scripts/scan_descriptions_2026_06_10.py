"""全 active 出品の説明文・在庫スキャン (read-only one-shot).

出典 2026-06-10: Agilent N2795A (358223832012) の US 本体説明文が「test」4 字に
破損していた事故。他出品への波及を GetItem 全件で洗い出す。

出力: data/description_scan_2026_06_10.json + 異常のみ stdout
"""
import json
import sys
import time

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
import xml.etree.ElementTree as ET

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

OUT = "data/description_scan_2026_06_10.json"
SHORT_DESC_THRESHOLD = 200  # 字未満 = 破損疑い

conn = sqlite3.connect("data/monitor.db")
item_ids = [r[0] for r in conn.execute(
    "SELECT ebay_item_id FROM ebay_listings "
    "WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '' "
    "ORDER BY ebay_item_id"
)]
conn.close()
print(f"対象: {len(item_ids)} 件", flush=True)

creds = _get_credentials()
app_id, dev_id, cert_id, user_token = creds
ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

results = []
anomalies = []
start = time.time()
for i, item_id in enumerate(item_ids, 1):
    if time.time() - start > 7200:  # 上限 2h (progress-touchpoint rule 3)
        print(f"TIMEOUT: {i-1}/{len(item_ids)} で打ち切り", flush=True)
        break
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        results.append({"item_id": item_id, "error": res.get("message", "")[:120]})
        continue
    root = ET.fromstring(res["raw"])
    item = root.find("ns:Item", ns)
    if item is None:
        results.append({"item_id": item_id, "error": "no Item node"})
        continue
    g = lambda p: (item.findtext(p, namespaces=ns) or "").strip()
    desc = g("ns:Description")
    listing_status = g("ns:SellingStatus/ns:ListingStatus")
    qty = int(g("ns:Quantity") or 0)
    sold = int(g("ns:SellingStatus/ns:QuantitySold") or 0)
    rec = {
        "item_id": item_id,
        "title": g("ns:Title"),
        "listing_status": listing_status,
        "qty_total": qty,
        "qty_sold": sold,
        "qty_available": qty - sold,
        "desc_len": len(desc),
        "desc_head": desc[:80],
    }
    results.append(rec)
    if listing_status == "Active" and len(desc) < SHORT_DESC_THRESHOLD:
        anomalies.append(rec)
        print(f"⚠ {item_id} | desc={len(desc)}字 {desc[:40]!r} | {rec['title'][:50]}",
              flush=True)
    if i % 50 == 0:
        print(f"  ... {i}/{len(item_ids)} ({int(time.time()-start)}s)", flush=True)
    time.sleep(0.25)  # API 負荷配慮

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({"scanned": len(results), "anomalies": anomalies,
               "results": results}, f, ensure_ascii=False, indent=1)
print(f"完了: {len(results)} 件 scan / 説明文異常 {len(anomalies)} 件 → {OUT}",
      flush=True)
