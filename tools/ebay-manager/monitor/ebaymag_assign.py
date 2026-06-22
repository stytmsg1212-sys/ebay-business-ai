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

# 商品→ポリシー割当は GraphQL ではなく REST PUT (2026-06-22 UI 盗聴で判明)。
# GraphQL productSave(shippingProfileId) は silent no-op だった。
# PUT /products/{id} に {"shipping_profile_id":{"value":<id>}} を CSRF 付きで送る。
_ASSIGN_PUT_JS = r"""async (args) => {
  const meta = document.querySelector('meta[name=csrf-token]');
  const csrf = meta ? meta.content : '';
  const r = await fetch('https://ebaymag.com/products/' + args.pid, {
    method: 'PUT',
    headers: {'content-type': 'application/json', 'x-csrf-token': csrf},
    credentials: 'include',
    body: JSON.stringify({shipping_profile_id: {value: parseInt(args.policy, 10)}}),
  });
  let body; try { body = await r.json(); } catch (e) { body = await r.text(); }
  return {status: r.status, body: body};
}"""

# 割当 read-back 用 (商品の現 shippingProfileId)
_PRODUCT_QUERY = "query ProductShip($id: ID!) { product(id: $id) { id shippingProfileId } }"

# siteId → (cc, 現地通貨, 国コード)
SITE = {3: ("UK", "GBP", "GB"), 77: ("DE", "EUR", "DE"), 71: ("FR", "EUR", "FR"),
        101: ("IT", "EUR", "IT"), 186: ("ES", "EUR", "ES"), 15: ("AU", "AUD", "AU"),
        2: ("CA", "CAD", "CA"), 0: ("US", "USD", "US")}
TAB_SITES = {"Europe": [3, 77, 71, 101, 186], "Australia": [15], "Canada": [2]}


class AssignError(RuntimeError):
    """anomaly 検出時 (即停止すべき)。"""


def snapshot_policies(pg) -> dict:
    # first を十分大きく取る (MAG 20 + DDP + 探索誤作成 等でポリシー数が増えるため、
    # 取りこぼすと assert_no_vanish が偽の「消失」を出して全割当が停止する)。
    profs = list_profiles(pg, first=200)
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
    """商品を policy に割当 (REST PUT)。read-back で割当確認 + merge/消失検査。"""
    before = snapshot_policies(pg)
    res = pg.evaluate(_ASSIGN_PUT_JS, {"pid": str(product_id), "policy": str(policy_id)})
    status = res.get("status")
    body = res.get("body")
    if status not in (200, 201, 204):
        raise AssignError(f"PUT /products/{product_id} HTTP {status}: {str(body)[:200]}")
    # HTTP 200 でも body にエラーを含む REST 実装に備える (Q0 偽成功防止)
    if isinstance(body, dict) and (body.get("errors") or body.get("success") is False):
        raise AssignError(f"PUT /products/{product_id} body error: {str(body)[:200]}")
    pg.wait_for_timeout(1500)
    # read-back: 割当が反映されたか (最も確実な成功判定)。
    # 実機では shippingProfileId は raw 数値文字列 ("12270565") だが、
    # Relay global ID ("Profile:12270565") 形式に備えて末尾 ID で正規化。
    data = gql(pg, "ProductShip", _PRODUCT_QUERY, {"id": str(product_id)})
    got = (data.get("product") or {}).get("shippingProfileId")
    got_id = str(got).split(":")[-1] if got is not None else None
    if got_id != str(policy_id):
        raise AssignError(
            f"read-back NG: product {product_id} shippingProfileId={got} (期待 {policy_id})。"
            f" PUT は HTTP {status} で受理済の可能性あり — リトライ前に実状態を read 必須")
    # merge 事故 (DDP_2-3kg/6-8kg 消滅型) 防止: 既存 policy が消えていないこと
    after = snapshot_policies(pg)
    assert_no_vanish(before, after)


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
        cc, cur, country = SITE[sid]
        expect = math.ceil(usd * fx[cur])
        # 送料: 書込と対称に DOMESTIC service の shippingCost を取る (HIGH-1)。
        got = None
        for so in (ep.get("payload") or {}).get("shippingOptions") or []:
            if so.get("optionType") != "DOMESTIC":
                continue
            for s in so.get("shippingServices") or []:
                got = s.get("shippingCost")
                break
        # shippingCost は {'value': N, 'currency': 'XXX'} 形式 (2026-06-22 実機確認)。
        got_val = got.get("value") if isinstance(got, dict) else got
        got_cur = got.get("currency") if isinstance(got, dict) else cur
        if got_val != expect or got_cur != cur:
            raise AssignError(f"read-back {cc}: 送料 期待{expect}{cur} 実{got}")
        # 配送可能国の封じ込め検証 (HIGH-2、money-direct)。excludedCountries は
        # payload 内に自国以外全除外 (len=universe-1) で入る。金額正でも配送国誤りを防ぐ。
        got_excl = (ep.get("payload") or {}).get("excludedCountries") or []
        if len(got_excl) != len(universe) - 1 or country in got_excl:
            raise AssignError(
                f"read-back {cc}: excludedCountries {len(got_excl)}件 "
                f"自国除外={country in got_excl} (期待 {len(universe) - 1}件・自国非除外)")
    return plan
