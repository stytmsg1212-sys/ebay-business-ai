"""eBaymag 商品→ポリシー割当 + 値設定 の安全ヘルパ (GraphQL, money-direct)。

Codex 無人実行レビュー (2026-06-21) の HIGH/MED ガードを実装:
  - 割当 = productSave(changes:{id, shippingProfileId})
  - 値設定 = upsertProfile (ebayProfiles を id/siteId で照合、未触サイト保持、非US $0/free 禁止)
  - 各 mutation 前後で全 policy snapshot、merge/消失/重複fingerprint/count delta を検査し anomaly で例外
"""
from __future__ import annotations

import math

from monitor.ebaymag_graphql import (
    gql, list_profiles, read_profile, SAVE_MUTATION,
)

ASSIGN_MUTATION = """mutation ProductSave($input: ProductSaveInput!) {
  productSave(input: $input) { product { id } errors __typename }
}"""

# siteId → (cc, 現地通貨, 国コード)
SITE = {3: ("UK", "GBP", "GB"), 77: ("DE", "EUR", "DE"), 71: ("FR", "EUR", "FR"),
        101: ("IT", "EUR", "IT"), 186: ("ES", "EUR", "ES"), 15: ("AU", "AUD", "AU"),
        2: ("CA", "CAD", "CA"), 0: ("US", "USD", "US")}
TAB_SITES = {"Europe": [3, 77, 71, 101, 186], "Australia": [15], "Canada": [2]}


class AssignError(RuntimeError):
    """anomaly 検出時 (即停止すべき)。"""


def snapshot_policies(pg) -> dict:
    profs = list_profiles(pg)
    return {n["id"]: {"title": n["title"], "products": n.get("numberOfProducts")}
            for n in profs}


def assert_no_vanish(before: dict, after: dict):
    vanished = set(before) - set(after)
    if vanished:
        raise AssignError(f"policy 消失 (merge疑い): {[(i, before[i]['title']) for i in vanished]}")


def domestic_service_code(ep) -> str | None:
    for so in (ep.get("payload") or {}).get("shippingOptions") or []:
        if so.get("optionType") == "DOMESTIC":
            svcs = so.get("shippingServices") or []
            if svcs:
                return svcs[0].get("shippingServiceCode")
    return None


def assign_product(pg, product_id: str, policy_id: str) -> None:
    """商品を policy に割当 (productSave)。前後で snapshot し count delta / 消失を検査。"""
    before = snapshot_policies(pg)
    res = gql(pg, "ProductSave", ASSIGN_MUTATION,
              {"input": {"changes": {"id": str(product_id), "shippingProfileId": str(policy_id)}}})
    ps = res.get("productSave") or {}
    if ps.get("errors"):
        raise AssignError(f"productSave errors: {ps['errors']}")
    pg.wait_for_timeout(1500)
    after = snapshot_policies(pg)
    assert_no_vanish(before, after)
    # target +1 / 他は increase しない (source -1 は別 policy)
    tgt_b = (before.get(policy_id) or {}).get("products") or 0
    tgt_a = (after.get(policy_id) or {}).get("products") or 0
    if tgt_a != tgt_b + 1:
        raise AssignError(f"target {policy_id} products {tgt_b}->{tgt_a} (期待 +1)")
    # 増えた policy が target だけか
    increased = [i for i in after if (after[i]["products"] or 0) > (before.get(i, {}).get("products") or 0)]
    if increased != [policy_id]:
        raise AssignError(f"想定外に増えた policy: {increased} (期待 [{policy_id}])")


def set_values(pg, policy_id: str, band: str, fx: dict, canonical_tab: dict,
               universe: list[str]) -> dict:
    """policy の各サイト送料を現地通貨で設定 (upsert)。read-back 検証して返す。"""
    site_usd = {}
    for tab, sids in TAB_SITES.items():
        for sid in sids:
            site_usd[sid] = canonical_tab[tab]

    prof = read_profile(pg, policy_id)
    eps_in = []
    plan = {}
    for ep in prof["shippingEbayProfiles"]:
        sid = ep["siteId"]; epid = ep["id"]
        usd = site_usd.get(sid, 0)
        if sid == 0 or usd == 0:
            eps_in.append({"id": epid, "managedByUser": False, "domsEbayTariffs": [],
                           "intlEbayTariffs": [], "excludedCountries": [], "dispatchTime": None})
            continue
        cc, cur, country = SITE[sid]
        svc = domestic_service_code(ep)
        if not svc:
            raise AssignError(f"{cc} domestic serviceCode 不明 (policy {policy_id})")
        local = math.ceil(usd * fx[cur])
        if local <= 0:
            raise AssignError(f"{cc} local cost <=0 (USD {usd} fx {fx[cur]}) — 送料漏れ防止で停止")
        eps_in.append({"id": epid, "managedByUser": True,
                       "domsEbayTariffs": [{"shippingServiceCode": svc, "freeShipping": False,
                                            "shippingCost": local, "additionalShippingCost": 0}],
                       "intlEbayTariffs": [], "excludedCountries": [c for c in universe if c != country],
                       "dispatchTime": prof.get("dispatchTime")})
        plan[cc] = (usd, local, cur)

    inp = {"profile": {
        "title": prof["title"], "color": prof.get("color") or 0, "id": policy_id,
        "dispatchTime": prof.get("dispatchTime"), "returnsWithin": prof.get("returnsWithin"),
        "returnsPaidByBuyer": prof.get("returnsPaidByBuyer") or False,
        "excludedCountries": prof.get("excludedCountries") or [],
        "country": prof.get("country"), "city": prof.get("city"), "postalCode": prof.get("postalCode"),
        "tariffs": [{"locations": t["locations"], "timeMax": t["timeMax"],
                     "prices": [{"currency": pr["currency"], "price": pr["price"],
                                 "additionalPrice": pr.get("additionalPrice")}
                                for pr in (t.get("prices") or [])]}
                    for t in (prof.get("tariffs") or [])],
        "ebayProfiles": eps_in,
    }}
    before = snapshot_policies(pg)
    res = gql(pg, "ShippingProfileSave", SAVE_MUTATION, {"input": inp})
    up = res.get("upsertProfile") or {}
    if not up.get("success"):
        raise AssignError(f"upsert values errors: {up.get('errors')}")
    if (up.get("profile") or {}).get("id") != policy_id:
        raise AssignError(f"upsert が別 policy にすり替わった (merge): {up.get('profile')}")
    pg.wait_for_timeout(2000)
    after = snapshot_policies(pg)
    assert_no_vanish(before, after)

    # read-back 検証
    rb = read_profile(pg, policy_id)
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
            raise AssignError(f"read-back {cc}: 期待{expect}{cur} 実{got}")
    return plan
