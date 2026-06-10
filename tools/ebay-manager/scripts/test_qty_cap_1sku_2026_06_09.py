"""W239 数量キャップ 1 SKU 実機試行 (2026-06-09, user 承認済).

対象: 356739323243? いや 356739322701 PLOTTER 5003 Mini (qty84, $136.5, 売上0)
手順: GetItem before → ReviseInventoryStatus 84→5 → GetItem after 検証。
完全に可逆 (失敗/不要なら同関数で 84 に戻す)。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import revise_inventory_quantity, get_single_listing

ITEM = "356739322701"
NEW_QTY = 5

cr = get_ebay_credentials()
app, dev, cert, tok = cr["app_id"], cr["dev_id"], cr["cert_id"], cr["user_token"]

print(f"=== GetItem before (item {ITEM}) ===")
before = get_single_listing(ITEM, app, dev, cert, tok)
if not before:
    print("GetItem 失敗 (None)"); sys.exit(2)
print(f"  title: {(before.get('title') or '')[:50]}")
print(f"  現 quantity: {before.get('quantity_ebay') or before.get('quantity')}")
print(f"  price: {before.get('current_price')}")

print(f"\n=== ReviseInventoryStatus: {ITEM} qty → {NEW_QTY} ===")
res = revise_inventory_quantity(ITEM, NEW_QTY, app, dev, cert, tok)
print(f"  結果: {res}")
if not res.get("success"):
    print("revise 失敗 → abort"); sys.exit(2)

print(f"\n=== GetItem after (実反映確認) ===")
after = get_single_listing(ITEM, app, dev, cert, tok)
q_after = after.get('quantity_ebay') or after.get('quantity') if after else None
print(f"  反映後 quantity: {q_after} (期待: {NEW_QTY})")
if str(q_after) == str(NEW_QTY):
    print("\n✅ 数量キャップ実機反映 成功 (84→5)")
else:
    print(f"\n⚠ 反映未確認: after={q_after}")
