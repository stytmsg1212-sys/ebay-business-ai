"""1 つの MAG ポリシーに各サイト送料 (FX換算現地通貨) を GraphQL で設定し read-back 検証。

end-to-end 実証用 (read→build→upsert→verify)。money-direct。
使い方: python -m scripts._set_mag_policy_values <profile_title> <band>
"""
import sys, json, math
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_graphql import (
    gql, list_profiles, read_profile, get_fx, SAVE_MUTATION,
)
from monitor.ebaymag_policy_mapping import build_canonical_policy

TITLE = sys.argv[1] if len(sys.argv) > 1 else "MAG_2-3kg_7day"
BAND = sys.argv[2] if len(sys.argv) > 2 else "2-3kg"

# eBaymag siteId → (cc, 現地通貨, 国コード)
SITE = {
    3:  ("UK", "GBP", "GB"),
    77: ("DE", "EUR", "DE"),
    71: ("FR", "EUR", "FR"),
    101: ("IT", "EUR", "IT"),
    186: ("ES", "EUR", "ES"),
    15: ("AU", "AUD", "AU"),
    2:  ("CA", "CAD", "CA"),
    0:  ("US", "USD", "US"),  # 本体、触らない
}
# canonical tab → どの siteId 群か
TAB_SITES = {"Europe": [3, 77, 71, 101, 186], "Australia": [15], "Canada": [2]}


def domestic_service_code(ep):
    pl = ep.get("payload") or {}
    for so in (pl.get("shippingOptions") or []):
        if so.get("optionType") == "DOMESTIC":
            svcs = so.get("shippingServices") or []
            if svcs:
                return svcs[0].get("shippingServiceCode")
    return None


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(3000)

    fx = get_fx(pg)
    canon = build_canonical_policy(BAND)["tab_values"]  # {US,Europe,Australia,Canada} USD
    # siteId → USD
    site_usd = {}
    for tab, sids in TAB_SITES.items():
        for sid in sids:
            site_usd[sid] = canon[tab]

    # 全220国 universe = いずれかの managed プロファイル(DDP_1-2kg CA)の excludedCountries(219) + CA
    profs = list_profiles(pg)
    by_title = {n["title"]: n["id"] for n in profs}
    pid = next((i for t, i in by_title.items() if TITLE in t), None)
    if not pid:
        print(f"FAIL: {TITLE} not found"); sys.exit(1)
    d12 = read_profile(pg, next(i for t, i in by_title.items() if "DDP_1-2kg" in t))
    ca_ep = next((e for e in d12["shippingEbayProfiles"] if e["siteId"] == 2), None)
    ca_excl = (ca_ep.get("payload") or {}).get("excludedCountries") or []
    universe = sorted(set(ca_excl) | {"CA"})
    print(f"universe countries: {len(universe)} (CA managed excl {len(ca_excl)} + CA)")
    if len(universe) < 200:
        print("FAIL: universe too small (DDP_1-2kg CA not managed?)"); sys.exit(1)

    prof = read_profile(pg, pid)
    print(f"target: {prof['title']} id={pid} dispatchTime={prof.get('dispatchTime')}")

    # ebayProfiles 構築
    eps = []
    plan = {}
    for ep in prof["shippingEbayProfiles"]:
        sid = ep["siteId"]
        epid = ep["id"]
        cc, cur, country = SITE.get(sid, ("?", "USD", "?"))
        usd = site_usd.get(sid, 0)
        if sid == 0 or usd == 0:
            # US本体 or 値0 = 無料維持 (managedByUser:false)
            eps.append({"id": epid, "managedByUser": False, "domsEbayTariffs": [],
                        "intlEbayTariffs": [], "excludedCountries": [], "dispatchTime": None})
            continue
        svc = domestic_service_code(ep)
        if not svc:
            print(f"FAIL: {cc} domestic serviceCode 不明"); sys.exit(1)
        local = math.ceil(usd * fx[cur])
        excl = [c for c in universe if c != country]
        eps.append({
            "id": epid, "managedByUser": True,
            "domsEbayTariffs": [{"shippingServiceCode": svc, "freeShipping": False,
                                 "shippingCost": local, "additionalShippingCost": 0}],
            "intlEbayTariffs": [], "excludedCountries": excl,
            "dispatchTime": prof.get("dispatchTime"),
        })
        plan[cc] = (f"${usd}USD", f"{local}{cur}", svc)

    print("設定計画:", json.dumps(plan, ensure_ascii=False))

    inp = {"profile": {
        "title": prof["title"], "color": prof.get("color") or 0, "id": pid,
        "dispatchTime": prof.get("dispatchTime"), "returnsWithin": prof.get("returnsWithin"),
        "returnsPaidByBuyer": prof.get("returnsPaidByBuyer") or False,
        "excludedCountries": prof.get("excludedCountries") or [],
        "country": prof.get("country"), "city": prof.get("city"),
        "postalCode": prof.get("postalCode"),
        "tariffs": [{"locations": t["locations"], "timeMax": t["timeMax"],
                     "prices": [{"currency": pr["currency"], "price": pr["price"],
                                 "additionalPrice": pr.get("additionalPrice")}
                                for pr in (t.get("prices") or [])]}
                    for t in (prof.get("tariffs") or [])],
        "ebayProfiles": eps,
    }}

    before_total = len(profs)
    res = gql(pg, "ShippingProfileSave", SAVE_MUTATION, {"input": inp})
    up = res.get("upsertProfile") or {}
    print("save: success=", up.get("success"), "errors=", up.get("errors"))
    pg.wait_for_timeout(2500)

    # read-back verify + totalCount diff
    after = list_profiles(pg)
    print(f"totalCount: {before_total} → {len(after)} (期待 同数)")
    rb = read_profile(pg, pid)
    ok = True
    for ep in rb["shippingEbayProfiles"]:
        sid = ep["siteId"]; usd = site_usd.get(sid, 0)
        if sid == 0 or usd == 0:
            continue
        cc, cur, _ = SITE[sid]
        expect = math.ceil(usd * fx[cur])
        got = None
        for so in (ep.get("payload") or {}).get("shippingOptions") or []:
            for s in so.get("shippingServices") or []:
                got = s.get("shippingCost")
        if got != expect:
            print(f"  ✗ {cc}: 期待{expect}{cur} 実{got}"); ok = False
        else:
            print(f"  ✓ {cc}: {got}{cur}")
    print("\nread-back", "PASS ✅" if ok and len(after) == before_total else "FAIL ⚠️")
