"""GetMyeBaySelling で非 USD (= eBaymag 各国版) の active 出品を列挙 (read-only).

eBaymag UK 出品 (TE Connectivity 91512-1) が eBay 実機に作成されたかの裏取り。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import xml.etree.ElementTree as ET

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

creds = _get_credentials()
app_id, dev_id, cert_id, user_token = creds
ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

page = 1
non_usd = []
total_active = 0
while True:
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ActiveList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>"""
    res = _call_trading_api("GetMyeBaySelling", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        print("失敗:", res.get("message"))
        sys.exit(1)
    root = ET.fromstring(res["raw"])
    items = root.findall(".//ns:ActiveList/ns:ItemArray/ns:Item", ns)
    if not items:
        break
    total_active += len(items)
    for it in items:
        price_el = it.find(".//ns:SellingStatus/ns:CurrentPrice", ns)
        cur = price_el.get("currencyID") if price_el is not None else "?"
        if cur != "USD":
            non_usd.append({
                "item_id": it.findtext("ns:ItemID", namespaces=ns),
                "title": (it.findtext("ns:Title", namespaces=ns) or "")[:60],
                "price": f"{price_el.text} {cur}" if price_el is not None else "?",
            })
    total_pages_el = root.find(".//ns:ActiveList/ns:PaginationResult/ns:TotalNumberOfPages", ns)
    total_pages = int(total_pages_el.text) if total_pages_el is not None else 1
    if page >= total_pages:
        break
    page += 1

print(f"active 合計: {total_active} 件 / 非 USD: {len(non_usd)} 件")
for r in non_usd:
    print(f"  {r['item_id']} | {r['price']:<14} | {r['title']}")
