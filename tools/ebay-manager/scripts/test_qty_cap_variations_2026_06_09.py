"""W239 数量キャップ 1 SKU 実機試行 v2 (multi-variation 対応, user 承認済).

発見: PLOTTER 356739322701 は 28 variations × qty3 × sold0 = 84。
本 script: 全 variation を qty 3→1 に下げ (合計84→28)、GetItem で検証。可逆。
ReviseInventoryStatus は ItemID+SKU+Quantity で variation 単位変更可 (最大4/call)。
"""
import sys, time
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
import httpx, xml.etree.ElementTree as ET
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import (_resolve_active_token, TRADING_API_URL, API_VERSION,
                                 get_out_of_stock_control_enabled)

ITEM = "356739322701"; NEW_QTY = 1
NS = {"n": "urn:ebay:apis:eBLBaseComponents"}
cr = get_ebay_credentials(); tok = _resolve_active_token(cr["user_token"])
H = {"X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
     "X-EBAY-API-APP-NAME": cr["app_id"], "X-EBAY-API-DEV-NAME": cr["dev_id"],
     "X-EBAY-API-CERT-NAME": cr["cert_id"], "Content-Type": "text/xml"}


def call(name, body):
    h = dict(H); h["X-EBAY-API-CALL-NAME"] = name
    r = httpx.post(TRADING_API_URL, content=body.encode(), headers=h, timeout=30)
    return ET.fromstring(r.text)


def get_variations():
    body = (f'<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
            f'<ItemID>{ITEM}</ItemID><IncludeVariations>true</IncludeVariations><Version>{API_VERSION}</Version></GetItemRequest>')
    root = call("GetItem", body)
    out = []
    for v in root.findall('.//n:Variations/n:Variation', namespaces=NS):
        out.append((v.findtext('n:SKU', namespaces=NS), v.findtext('n:Quantity', namespaces=NS)))
    return out


# OOS Control 確認 (qty 低下の安全性)
oos = get_out_of_stock_control_enabled(cr["app_id"], cr["dev_id"], cr["cert_id"], tok)
print(f"Out-of-Stock Control: {oos}")

vs = get_variations()
print(f"variation 数: {len(vs)} / 現 qty 合計: {sum(int(q) for _,q in vs if q)}")

# ReviseInventoryStatus を 4 件ずつ batch で qty=1 に
changed = 0
for i in range(0, len(vs), 4):
    batch = vs[i:i+4]
    items = "".join(
        f"<InventoryStatus><ItemID>{ITEM}</ItemID><SKU>{sku}</SKU><Quantity>{NEW_QTY}</Quantity></InventoryStatus>"
        for sku, _ in batch)
    body = (f'<?xml version="1.0" encoding="utf-8"?><ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
            f'{items}</ReviseInventoryStatusRequest>')
    root = call("ReviseInventoryStatus", body)
    ack = root.findtext("n:Ack", namespaces=NS)
    if ack in ("Success", "Warning"):
        changed += len(batch)
        print(f"  batch {i//4+1}: {ack} ({len(batch)}件 → qty{NEW_QTY})")
    else:
        errs = "; ".join(e.text for e in root.findall(".//n:Errors/n:LongMessage", namespaces=NS) if e.text)
        print(f"  batch {i//4+1}: FAIL {errs}")
        break
    time.sleep(1)

# 検証
vs2 = get_variations()
tot2 = sum(int(q) for _, q in vs2 if q)
print(f"\n=== 検証: revise 後 qty 合計 = {tot2} (期待 {len(vs)*NEW_QTY}) ===")
print("✅ 数量キャップ(variation単位) 実機反映成功" if tot2 == len(vs)*NEW_QTY else "⚠ 一部未反映")
