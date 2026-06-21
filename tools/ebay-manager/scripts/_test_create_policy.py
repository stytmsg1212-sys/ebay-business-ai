"""GraphQL で新規ポリシー1個を安全にテスト作成 (totalCount 監視、前回事故の再発防止)。

create パスが clean か (totalCount +1、merge/junk なし) を確認する。
失敗時は手動で削除できるよう、作成したら id を表示。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_graphql import gql, list_profiles, read_profile, SAVE_MUTATION

TEST_TITLE = "MAG_2-3kg_7day"


def total(pg):
    return len(list_profiles(pg))


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(3000)

    before = list_profiles(pg)
    print(f"作成前 totalCount={len(before)}")
    if any(TEST_TITLE in n.get("title", "") for n in before):
        print(f"既に {TEST_TITLE} 存在 → スキップ")
        sys.exit(0)

    # テンプレ用に既存 DDP_1-2kg を読む (country/city/postalCode/tariffs 流用)
    src_id = next(n["id"] for n in before if "DDP_1-2kg" in n.get("title", ""))
    src = read_profile(pg, src_id)
    print(f"template src: {src['title']} country={src.get('country')} city={src.get('city')}")

    # 新規作成 input: id 無し・ebayProfiles 無し (server 自動生成)・Worldwide free
    new_input = {
        "profile": {
            "title": TEST_TITLE,
            "color": 0,
            "dispatchTime": 7,
            "returnsWithin": src.get("returnsWithin") or 60,
            "returnsPaidByBuyer": src.get("returnsPaidByBuyer") or False,
            "excludedCountries": [],
            "country": src.get("country"),
            "city": src.get("city"),
            "postalCode": src.get("postalCode"),
            "tariffs": [{"locations": ["Worldwide"], "timeMax": 3, "prices": []}],
            "ebayProfiles": [],
        }
    }
    res = gql(pg, "ShippingProfileSave", SAVE_MUTATION, {"input": new_input})
    up = res.get("upsertProfile") or {}
    print("create result: success=", up.get("success"), "errors=", up.get("errors"),
          "new id=", (up.get("profile") or {}).get("id"))

    pg.wait_for_timeout(2000)
    after = list_profiles(pg)
    print(f"\n作成後 totalCount={len(after)} (期待 {len(before)+1})")
    mag = [n for n in after if TEST_TITLE in n.get("title", "")]
    print(f"{TEST_TITLE} present: {len(mag)} → {[(n['id'],n['title']) for n in mag]}")
    # 他ポリシーが消えていないか (merge 検知)
    before_ids = {n["id"] for n in before}
    after_ids = {n["id"] for n in after}
    vanished = before_ids - after_ids
    print(f"消えた既存ポリシー (merge検知): {vanished if vanished else 'なし ✓'}")
    if len(after) == len(before) + 1 and not vanished and mag:
        print("\n✅ create パス CLEAN (+1, merge/消失なし)")
    else:
        print("\n⚠️ 想定外 — 要確認")
