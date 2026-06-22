"""eBaymag 各 MAG に各国送料を FX 換算で一括設定 (money-direct)。

set_values (ebaymag_assign) を各 MAG に適用。出ている managed サイト (US以外) に
値設定し、未生成サイトは set_values 内で touch しない。managed サイトが1つも無い MAG
(各国版未生成) は skip。後で各国版が増えたら再実行で追加設定 (set_values は冪等)。
set_values 内蔵の read-back + assert_no_vanish で各回検証、失敗で即停止。
冒頭1回 /shipping reload で CSRF 更新 (バッチは数分以内想定)。途中で CSRF が
失効した場合は gql が例外で即停止し、再実行で続行 (set_values は冪等)。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

from monitor import ebaymag_assign as A
from monitor import ebaymag_graphql as G
from monitor.ebaymag_policy_mapping import build_canonical_policy

REQUIRED = {2, 3, 15, 71, 77, 101, 186}  # US(0)以外の7サイト (US は本体課金$0固定)

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    if page is None:
        print("FAIL: eBaymag タブが見つからない (CDP 9222)")
        sys.exit(1)
    page.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)

    fx = G.get_fx(page)
    profs = G.list_profiles(page, first=200)
    by_title = {n["title"]: n["id"] for n in profs}
    # universe (全220国) = DDP_1-2kg の CA managed excludedCountries(219) + CA
    d12_id = next(i for t, i in by_title.items() if "DDP_1-2kg" in t)
    d12 = G.read_profile(page, d12_id)
    ca_ep = next(e for e in d12["shippingEbayProfiles"] if e["siteId"] == 2)
    ca_excl = (ca_ep.get("payload") or {}).get("excludedCountries") or []
    universe = sorted(set(ca_excl) | {"CA"})
    print(f"fx={fx}")
    print(f"universe countries: {len(universe)}")
    if len(universe) < 200:
        print("FAIL: universe too small (DDP_1-2kg CA not managed?)")
        sys.exit(1)

    ok = skip = fail = 0
    for x in profs:
        title = x.get("title") or ""
        if not title.startswith("MAG_") or (x.get("numberOfProducts") or 0) == 0:
            continue
        prof = G.read_profile(page, x["id"])
        sids = {ep["siteId"] for ep in prof["shippingEbayProfiles"]}
        present = REQUIRED & sids  # 出ている managed サイト (US以外)
        if not present:
            skip += 1
            print(f"  SKIP {title}: managed サイト未生成 (sites={sorted(sids)})")
            continue
        band = title.replace("MAG_", "", 1).rsplit("_", 1)[0]  # MAG_2-3kg_7day -> 2-3kg
        canon = build_canonical_policy(band)["tab_values"]
        try:
            plan = A.set_values(page, str(x["id"]), band, fx, canon, universe)
            ok += 1
            print(f"  OK {title}: {plan}")
        except Exception as e:
            fail += 1
            print(f"  FAIL {title}: {str(e)[:160]}")
            print("STOP (money-direct: 失敗で即停止)")
            break
    print(f"\n=== set_values batch: ok={ok} skip={skip} fail={fail} ===")
