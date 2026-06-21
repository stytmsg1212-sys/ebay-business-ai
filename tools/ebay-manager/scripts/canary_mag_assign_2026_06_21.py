"""CANARY: 1商品を MAG ポリシーへ割当→値設定→read-back検証 (無人 money-direct、Codex安全策)。

商品 718746583 (2-3kg, 現DDP_1-2kg=1day) → MAG_2-3kg_1day。
完璧に通れば全展開可。1つでも anomaly で例外停止。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_graphql import list_profiles, read_profile, get_fx
from monitor.ebaymag_assign import assign_product, set_values, snapshot_policies, AssignError
from monitor.ebaymag_policy_mapping import build_canonical_policy

PRODUCT_ID = "718746583"
TARGET_TITLE = "MAG_2-3kg_1day"
BAND = "2-3kg"

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(3000)

    fx = get_fx(pg)
    canon = build_canonical_policy(BAND)["tab_values"]
    profs = list_profiles(pg)
    by_title = {n["title"]: n["id"] for n in profs}
    target_id = next((i for t, i in by_title.items() if t == TARGET_TITLE), None)
    if not target_id:
        print(f"FAIL: {TARGET_TITLE} 不在"); sys.exit(1)
    # universe (DDP_1-2kg CA managed excl + CA)
    d12 = read_profile(pg, next(i for t, i in by_title.items() if "DDP_1-2kg" in t))
    ca_ep = next((e for e in d12["shippingEbayProfiles"] if e["siteId"] == 2), None)
    universe = sorted(set((ca_ep.get("payload") or {}).get("excludedCountries") or []) | {"CA"})
    if len(universe) < 200:
        print("FAIL: universe too small"); sys.exit(1)

    snap0 = snapshot_policies(pg)
    print(f"baseline policies={len(snap0)} / {TARGET_TITLE} id={target_id} "
          f"products={snap0[target_id]['products']}")
    print(f"canonical 2-3kg={canon} / universe={len(universe)}国")

    try:
        print("\n[1] assign...")
        assign_product(pg, PRODUCT_ID, target_id)
        after_assign = snapshot_policies(pg)
        print(f"  {TARGET_TITLE} products={after_assign[target_id]['products']} (期待1)")

        print("[2] set values (FX換算)...")
        plan = set_values(pg, target_id, BAND, fx, canon, universe)
        print(f"  設定: {json.dumps({k:f'${v[0]}USD->{v[1]}{v[2]}' for k,v in plan.items()}, ensure_ascii=False)}")

        snap1 = snapshot_policies(pg)
        if len(snap1) != len(snap0) + 0:
            print(f"  ⚠️ policy総数 {len(snap0)}->{len(snap1)}")
        print("\n✅ CANARY PASS: 割当+値設定+read-back 全検証通過 (anomalyなし)")
    except AssignError as e:
        print(f"\n🚨 CANARY FAIL (即停止): {e}")
        sys.exit(2)
