"""MAG 割当計画 dry-run (read-only、割当はしない)。

各 eBaymag 商品を weight_g→重量帯 × handling time(1/7)→MAG_{帯}_{日}day に対応付け、
target policy_id を決定。割当不能 (weight 未設定 / MAG 不在 / handling 異常) を洗い出す。
出力: data/mag_assignment_plan.json + 分布サマリ。mutation 一切なし。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

from monitor import database as db
from monitor import ebaymag_graphql as G
from monitor.ebaymag_policy_mapping import band_for_weight_g

# handling time (ebay_item_id -> dispatch_time_max '1'/'7')
ht = {r["ebay_item_id"]: str(r["dispatch_time_max"])
      for r in json.load(open("data/ebaymag_handling_times.json", encoding="utf-8"))}

# weight_g (ebay_item_id -> g)
with db.get_conn() as c:
    wmap = {row["ebay_item_id"]: row["weight_g"]
            for row in c.execute(
                "SELECT ebay_item_id, weight_g FROM ebay_listings").fetchall()}

prods = db.get_ebaymag_products()
print(f"eBaymag products: {len(prods)}")

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    profs = G.list_profiles(page, first=200)
    mag = {x["title"]: str(x["id"]) for x in profs if (x.get("title") or "").startswith("MAG_")}
    print(f"MAG policies: {len(mag)}")

plan, issues = [], []
for pr in prods:
    iid, pid = pr.get("ebay_item_id"), pr.get("product_id")
    dtm = ht.get(iid)
    wg = wmap.get(iid)
    if wg is None or (isinstance(wg, (int, float)) and wg <= 0):
        issues.append({"ebay_item_id": iid, "product_id": pid, "reason": f"weight={wg}"})
        continue
    if dtm not in ("1", "7"):
        issues.append({"ebay_item_id": iid, "product_id": pid, "reason": f"dispatch={dtm}"})
        continue
    try:
        band = band_for_weight_g(wg)
    except ValueError as e:
        issues.append({"ebay_item_id": iid, "product_id": pid, "reason": str(e)[:60]})
        continue
    day = "1" if dtm == "1" else "7"
    title = f"MAG_{band}_{day}day"
    tid = mag.get(title)
    if not tid:
        issues.append({"ebay_item_id": iid, "product_id": pid, "reason": f"no policy {title}"})
        continue
    plan.append({"product_id": pid, "ebay_item_id": iid, "weight_g": wg,
                 "band": band, "dispatch": dtm, "target_title": title, "target_id": tid})

# 分布サマリ
by_title = {}
for x in plan:
    by_title[x["target_title"]] = by_title.get(x["target_title"], 0) + 1
print(f"\n=== assignable: {len(plan)} / issues: {len(issues)} ===")
for t in sorted(by_title):
    print(f"  {t}: {by_title[t]} 件")
if issues:
    print("\n--- issues ---")
    for it in issues[:30]:
        print(f"  {it['ebay_item_id']} / {it['product_id']}: {it['reason']}")

json.dump({"plan": plan, "issues": issues},
          open("data/mag_assignment_plan.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("\nsaved data/mag_assignment_plan.json")
