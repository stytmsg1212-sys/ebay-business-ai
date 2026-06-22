"""eBaymag 全商品の eBay 本体 handling time (DispatchTimeMax) を GetItem で取得し分布集計。

read-only。発送日数 = eBay 本体 handling time に合わせる (user 確定 2026-06-22) ための
実態調査。丸めルール (1day/7day へどう寄せるか) を決めるための分布を出す。
"""
import sys, json
sys.path.insert(0, '.')
import xml.etree.ElementTree as ET
import httpx

from monitor import database as db
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import (
    _build_get_item_xml, _resolve_active_token, TRADING_API_URL, API_VERSION,
)

OUT = "data/ebaymag_handling_times.json"
NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}

creds = get_ebay_credentials()
token = _resolve_active_token(creds["user_token"])
headers = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
    "X-EBAY-API-CALL-NAME": "GetItem",
    "X-EBAY-API-APP-NAME": creds["app_id"],
    "X-EBAY-API-DEV-NAME": creds["dev_id"],
    "X-EBAY-API-CERT-NAME": creds["cert_id"],
    "Content-Type": "text/xml",
}

prods = db.get_ebaymag_products()
items = [(p["ebay_item_id"], p.get("product_id")) for p in prods if p.get("ebay_item_id")]
print(f"eBaymag products with ebay_item_id: {len(items)}", flush=True)

dist, out, err = {}, [], 0
for i, (iid, pid) in enumerate(items):
    try:
        xml = _build_get_item_xml(iid).replace("{USER_TOKEN}", token)
        r = httpx.post(TRADING_API_URL, content=xml.encode("utf-8"), headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        if root.findtext("ns:Ack", namespaces=NS) not in ("Success", "Warning"):
            err += 1
            continue
        item = root.find(".//ns:Item", namespaces=NS)
        dtm = item.findtext("ns:DispatchTimeMax", namespaces=NS) if item is not None else None
        sku = item.findtext("ns:SKU", namespaces=NS) if item is not None else None
        dist[dtm] = dist.get(dtm, 0) + 1
        out.append({"ebay_item_id": iid, "product_id": pid, "sku": sku, "dispatch_time_max": dtm})
    except Exception as e:
        err += 1
        print(f"  err {iid}: {str(e)[:60]}", flush=True)
    if (i + 1) % 20 == 0:
        print(f"  progress {i + 1}/{len(items)}", flush=True)

print(f"\n=== DispatchTimeMax distribution (errors={err}) ===", flush=True)
for k in sorted(dist, key=lambda x: (x is None, x)):
    print(f"  DispatchTimeMax={k}: {dist[k]} 件", flush=True)
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"saved {OUT}", flush=True)
