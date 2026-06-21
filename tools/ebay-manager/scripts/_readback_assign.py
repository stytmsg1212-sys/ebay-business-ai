"""user 操作の read-back: 12270565 の正体 + product 733361103 の現割当 (read-only)。"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    if not page:
        print("NO ebaymag tab"); sys.exit(1)

    profs = G.list_profiles(page, first=60)
    print(f"=== {len(profs)} profiles ===")
    by_id = {str(x["id"]): x for x in profs}
    t = by_id.get("12270565")
    print("TARGET 12270565 =", json.dumps(t, ensure_ascii=False) if t else "NOT FOUND")
    # MAG_* 一覧 (id + title + 商品数)
    for x in profs:
        if "MAG_" in (x.get("title") or ""):
            print(f"  MAG id={x['id']} title={x['title']} n={x.get('numberOfProducts')}")

    # product read-back (field 名 2 候補を試す)
    for q in ("query P($id: ID!){ product(id:$id){ id title shippingProfileId } }",
              "query P($id: ID!){ product(id:$id){ id title shipping_profile_id } }"):
        try:
            d = G.gql(page, "P", q, {"id": "733361103"})
            print("PRODUCT 733361103 =", json.dumps(d.get("product"), ensure_ascii=False))
            break
        except Exception as e:
            print("product query try err:", str(e)[:100])
