"""W239 数量キャップ 本番ロールアウト (2026-06-09, user 承認済).

対象: qty>=20 の非stock listing (PLOTTER multi-variation 群)。各 variation を qty1 へ。
安全弁:
  - sku が 'stock' で始まる listing は **実在庫連動のため絶対 skip** (user 指示)。
  - Out-of-Stock Control ON 前提 (qty1 → 売れて0 でも listing は hidden、終了しない)。
  - 各 listing 変更後に DB quantity_ebay を実機合計へ同期。
可逆 (各 variation を元数量へ戻せる)。
"""
import sys, time, sqlite3
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
import httpx, xml.etree.ElementTree as ET
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import _resolve_active_token, TRADING_API_URL, API_VERSION
from monitor.database import update_ebay_listing_quantity

NS = {"n": "urn:ebay:apis:eBLBaseComponents"}
NEW_QTY = 1
DB = ROOT / "data" / "monitor.db"
cr = get_ebay_credentials(); tok = _resolve_active_token(cr["user_token"])
H = {"X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
     "X-EBAY-API-APP-NAME": cr["app_id"], "X-EBAY-API-DEV-NAME": cr["dev_id"],
     "X-EBAY-API-CERT-NAME": cr["cert_id"], "Content-Type": "text/xml"}


def call(name, body):
    h = dict(H); h["X-EBAY-API-CALL-NAME"] = name
    r = httpx.post(TRADING_API_URL, content=body.encode(), headers=h, timeout=30)
    return ET.fromstring(r.text)


def get_variations(item):
    body = (f'<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
            f'<ItemID>{item}</ItemID><IncludeVariations>true</IncludeVariations><Version>{API_VERSION}</Version></GetItemRequest>')
    root = call("GetItem", body)
    return [(v.findtext('n:SKU', namespaces=NS), v.findtext('n:Quantity', namespaces=NS))
            for v in root.findall('.//n:Variations/n:Variation', namespaces=NS)]


# 対象抽出 (qty>=20, 非stock, 既処理356739322701除外)
conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
targets = conn.execute(
    "SELECT ebay_item_id, COALESCE(sku,'') sku, quantity_ebay q, title FROM ebay_listings "
    "WHERE COALESCE(is_ended,0)=0 AND quantity_ebay>=20 AND ebay_item_id!='356739322701' "
    "ORDER BY quantity_ebay DESC").fetchall()
conn.close()

print(f"対象候補: {len(targets)}件")
ok = skipped = 0
for r in targets:
    item, sku, t = r["ebay_item_id"], r["sku"], (r["title"] or "")[:36]
    if sku.lower().startswith("stock"):
        print(f"  🚫 SKIP stock-SKU (実在庫): {item} {sku} {t}")
        skipped += 1; continue
    vs = get_variations(item)
    if not vs:
        print(f"  ⚠ variation 取得0、skip: {item} {t}"); skipped += 1; continue
    before = sum(int(q) for _, q in vs if q)
    fail = False
    for i in range(0, len(vs), 4):
        batch = vs[i:i+4]
        items = "".join(f"<InventoryStatus><ItemID>{item}</ItemID><SKU>{sku2}</SKU>"
                        f"<Quantity>{NEW_QTY}</Quantity></InventoryStatus>" for sku2, _ in batch)
        root = call("ReviseInventoryStatus",
                    f'<?xml version="1.0" encoding="utf-8"?><ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                    f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>{items}</ReviseInventoryStatusRequest>')
        if root.findtext("n:Ack", namespaces=NS) not in ("Success", "Warning"):
            errs = "; ".join(e.text for e in root.findall(".//n:Errors/n:LongMessage", namespaces=NS) if e.text)
            print(f"  ❌ {item} batch fail: {errs}"); fail = True; break
        time.sleep(0.8)
    if fail:
        skipped += 1; continue
    after_vs = get_variations(item)
    after = sum(int(q) for _, q in after_vs if q)
    update_ebay_listing_quantity(item, after)
    print(f"  ✅ {item} {len(vs)}var qty {before}→{after} | {t}")
    ok += 1

print(f"\n=== 完了: キャップ {ok}件 / skip {skipped}件 ===")
