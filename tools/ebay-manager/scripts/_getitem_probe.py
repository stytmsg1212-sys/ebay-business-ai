"""US item_id 1件を GetItem し、weight / rate table / DispatchTimeMax が取れるか確認。

89商品の帯決定に使える情報源を特定 (user 提案: US側のシッピングポリシー=質量帯)。read-only。
"""
import sys
import xml.etree.ElementTree as ET
import httpx
sys.path.insert(0, '.')
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import _resolve_active_token, TRADING_API_URL, API_VERSION

ITEM = sys.argv[1] if len(sys.argv) > 1 else "5726777441"
NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}

creds = get_ebay_credentials()
token = _resolve_active_token(creds["user_token"])
xml = (f'<?xml version="1.0" encoding="utf-8"?>'
       f'<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
       f'<RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>'
       f'<ItemID>{ITEM}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>')
headers = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
    "X-EBAY-API-CALL-NAME": "GetItem",
    "X-EBAY-API-APP-NAME": creds["app_id"],
    "X-EBAY-API-DEV-NAME": creds["dev_id"],
    "X-EBAY-API-CERT-NAME": creds["cert_id"],
    "Content-Type": "text/xml",
}
r = httpx.post(TRADING_API_URL, content=xml.encode("utf-8"), headers=headers, timeout=30)
root = ET.fromstring(r.text)
print("Ack:", root.findtext("ns:Ack", namespaces=NS))
for e in root.findall(".//ns:Errors", namespaces=NS)[:3]:
    print(f"  error {e.findtext('ns:ErrorCode', namespaces=NS)}: "
          f"{e.findtext('ns:LongMessage', namespaces=NS)}")
item = root.find(".//ns:Item", namespaces=NS)
if item is None:
    sys.exit(0)
print("Title:", (item.findtext("ns:Title", namespaces=NS) or "")[:50])
print("DispatchTimeMax:", item.findtext("ns:DispatchTimeMax", namespaces=NS))
print("WeightMajor:", item.findtext(".//ns:WeightMajor", namespaces=NS))
print("WeightMinor:", item.findtext(".//ns:WeightMinor", namespaces=NS))
print("DomesticRateTableId:", item.findtext(".//ns:DomesticRateTableId", namespaces=NS))
print("ShippingRateType:", item.findtext(".//ns:ShippingRateType", namespaces=NS))
spd = item.find(".//ns:ShippingPackageDetails", namespaces=NS)
if spd is not None:
    print("ShippingPackageDetails:", ET.tostring(spd, encoding="unicode")[:400])
