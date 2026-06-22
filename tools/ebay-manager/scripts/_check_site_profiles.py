"""商品が割り当たった MAG の各国設定欄 (shippingEbayProfiles) 出揃い状況を確認 (read-only)。

割当直後は eBaymag が各国版を非同期生成するため site profiles=0 → 時間差で増える。
値設定は 8 サイト (US/CA/UK/AU/DE/FR/IT/ES) 揃ってから (欠落サイト$0漏れ防止)。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

SITE = {0: "US", 2: "CA", 3: "UK", 15: "AU", 71: "FR", 77: "DE", 101: "IT", 186: "ES"}

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    profs = G.list_profiles(page, first=200)
    mags = [x for x in profs if (x.get("title") or "").startswith("MAG_")
            and (x.get("numberOfProducts") or 0) > 0]
    print(f"商品割当ありの MAG: {len(mags)}")
    ready = 0
    for x in sorted(mags, key=lambda v: v["title"]):
        prof = G.read_profile(page, x["id"])
        sids = sorted(ep["siteId"] for ep in prof["shippingEbayProfiles"])
        names = [SITE.get(s, str(s)) for s in sids]
        full = "✓8" if len(sids) >= 8 else f"{len(sids)}/8"
        if len(sids) >= 8:
            ready += 1
        print(f"  {x['title']}: products={x['numberOfProducts']} sites={full} {names}")
    print(f"\n=== 8サイト出揃い: {ready}/{len(mags)} MAG ===")
