"""description='test' 16 件の eBay 実物データ取得 (read-only one-shot).

ConditionDescription / ItemSpecifics / CategoryID / 価格 を GetItem で取得し、
description 再生成の素材として data/testdesc16_getitem_2026_06_11.json に保存。
"""
import json
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

ITEM_IDS = [
    "358027482174", "358042514439", "358046641356", "358062291688",
    "358120580440", "358120654421", "358147979341", "358158853598",
    "358166322333", "358207286305", "358223832012", "358244264123",
    "358274785765", "358334960391", "358335153622", "358403831980",
]

creds = _get_credentials()
if not creds:
    print("FAIL: creds 解決不可")
    sys.exit(1)
app_id, dev_id, cert_id, user_token = creds

ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
out = []
for iid in ITEM_IDS:
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{iid}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        print(f"{iid} GetItem 失敗: {res.get('message')}")
        out.append({"item_id": iid, "error": res.get("message")})
        continue
    root = ET.fromstring(res["raw"])
    item = root.find("ns:Item", ns)
    g = lambda p: (item.findtext(p, namespaces=ns) or "").strip()
    specifics = []
    for nvl in item.findall("ns:ItemSpecifics/ns:NameValueList", ns):
        name = (nvl.findtext("ns:Name", namespaces=ns) or "").strip()
        vals = [v.text or "" for v in nvl.findall("ns:Value", ns)]
        specifics.append([name, "; ".join(vals)])
    pics = item.findall("ns:PictureDetails/ns:PictureURL", ns)
    rec = {
        "item_id": iid,
        "title": g("ns:Title"),
        "status": g("ns:SellingStatus/ns:ListingStatus"),
        "price": g("ns:SellingStatus/ns:CurrentPrice"),
        "quantity": g("ns:Quantity"),
        "quantity_sold": g("ns:SellingStatus/ns:QuantitySold"),
        "condition_id": g("ns:ConditionID"),
        "condition_display": g("ns:ConditionDisplayName"),
        "condition_description": g("ns:ConditionDescription"),
        "category_id": g("ns:PrimaryCategory/ns:CategoryID"),
        "category_name": g("ns:PrimaryCategory/ns:CategoryName"),
        "description_raw": g("ns:Description"),
        "item_specifics": specifics,
        "n_pictures": len(pics),
    }
    out.append(rec)
    print(f"{iid} | {rec['condition_display']:<14} | condDesc={rec['condition_description'][:40]!r} "
          f"| specs={len(specifics)} | {rec['title'][:46]}")

with open(r"data\testdesc16_getitem_2026_06_11.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n保存: data/testdesc16_getitem_2026_06_11.json ({len(out)} 件)")
