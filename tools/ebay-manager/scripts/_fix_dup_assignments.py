"""重複 product (1 product に複数 eBay item) 4件を「重い方band + 遅い方(7day優先)」に統一。

money-direct。assign_product 内蔵の read-back + assert_no_vanish で各回検証。
既に正しい target の商品は skip。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

from monitor import ebaymag_assign as A
from monitor import ebaymag_graphql as G

PQ = "query ProductShip($id: ID!){ product(id:$id){ id shippingProfileId } }"

# 重い方 band + 遅い方(7day を優先) で確定した正しい target title
FIXES = {
    "718746535": "MAG_4-5kg_7day",    # 4800g(最大) + 7day
    "718746908": "MAG_0.5-1kg_1day",  # 600g(最大) + 1day (既に正しい想定)
    "718746698": "MAG_0-0.5kg_7day",  # 450g + 7day(遅い方)
    "718746695": "MAG_0.5-1kg_7day",  # 800g + 7day(遅い方、既に正しい想定)
}

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    profs = G.list_profiles(page, first=200)
    mag = {x["title"]: str(x["id"]) for x in profs if (x.get("title") or "").startswith("MAG_")}

    for pid, title in FIXES.items():
        tid = mag.get(title)
        if not tid:
            print(f"{pid}: target {title} が見つからない — SKIP (要確認)")
            continue
        cur = G.gql(page, "ProductShip", PQ, {"id": pid})
        cur_id = str((cur.get("product") or {}).get("shippingProfileId") or "").split(":")[-1]
        if cur_id == tid:
            print(f"{pid} 既に {title} = OK (変更なし)")
            continue
        A.assign_product(page, pid, tid)
        print(f"{pid} -> {title} 修正 OK (read-back PASS)")
    print("=== dup fix done ===")
