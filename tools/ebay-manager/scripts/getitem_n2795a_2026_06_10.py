"""Agilent N2795A (358223832012) の eBay US 実物確認 (read-only one-shot).

eBaymag 詳細パネルで「在庫切れ」「説明=test」が見えたため、US 本体の
実数量・実説明文を GetItem で裏取りする。副作用ゼロ。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import xml.etree.ElementTree as ET

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

ITEM_ID = "358223832012"

creds = _get_credentials()
if not creds:
    print("FAIL: creds 解決不可")
    sys.exit(1)
app_id, dev_id, cert_id, user_token = creds

xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{ITEM_ID}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""

res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
if not res.get("success"):
    print("GetItem 失敗:", res.get("message"))
    sys.exit(1)

root = ET.fromstring(res["raw"])
ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
item = root.find("ns:Item", ns)
g = lambda p: (item.findtext(p, namespaces=ns) or "").strip()

desc = g("ns:Description")
print("Title          :", g("ns:Title"))
print("ListingStatus  :", g("ns:SellingStatus/ns:ListingStatus"))
print("Quantity       :", g("ns:Quantity"))
print("QuantitySold   :", g("ns:SellingStatus/ns:QuantitySold"))
print("Price          :", g("ns:SellingStatus/ns:CurrentPrice"))
print("Description len:", len(desc))
print("Description 先頭 300 字:")
print(" ", desc[:300].replace("\n", " "))
