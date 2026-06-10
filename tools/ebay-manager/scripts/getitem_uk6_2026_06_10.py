"""UK グループ 6 商品の eBay US 実物チェック (read-only one-shot).

数量 0 / 説明文破損 ("test" 等の極端に短い説明) を出品前に検出する。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import xml.etree.ElementTree as ET

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

ITEMS = [
    "358223832012",  # Agilent N2795A
    "358107955974",  # Cohana Sewing Kit
    "357947963186",  # TE Connectivity Crimp Tool
    "358333759462",  # Mitsubishi FX5U
    "357636314239",  # Niigata Seiki WGA-65
    "356733099716",  # SONY ICD-ST25
]

creds = _get_credentials()
app_id, dev_id, cert_id, user_token = creds
ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

for item_id in ITEMS:
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        print(f"{item_id} | GetItem 失敗: {res.get('message')}")
        continue
    root = ET.fromstring(res["raw"])
    item = root.find("ns:Item", ns)
    g = lambda p: (item.findtext(p, namespaces=ns) or "").strip()
    desc = g("ns:Description")
    flag = []
    if g("ns:Quantity") == "0":
        flag.append("数量0")
    if len(desc) < 200:
        flag.append(f"説明短すぎ({len(desc)}字: {desc[:40]!r})")
    status = "⚠ " + " / ".join(flag) if flag else "OK"
    print(f"{item_id} | qty={g('ns:Quantity')} sold={g('ns:SellingStatus/ns:QuantitySold')} "
          f"| desc={len(desc)}字 | {g('ns:Title')[:45]} | {status}")
