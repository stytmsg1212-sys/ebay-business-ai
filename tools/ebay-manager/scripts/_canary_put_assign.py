"""canary: 修正後 assign_product (REST PUT) が実機で効くか検証 (可逆)。

HITACHI EH-XD16 (733361103) を空の MAG_5-6kg_1day へ動かし read-back 確認 →
元の MAG_5-6kg_7day へ戻す。assign_product は read-back hard-abort 内蔵。
money-direct: state を一時変更して必ず戻す。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_assign as A
from monitor import ebaymag_graphql as G

PID = "733361103"
ORIG = "12270565"   # MAG_5-6kg_7day (現在地)
TEMP = "12270574"   # MAG_5-6kg_1day (空, 検証用の一時移動先)


def cur_policy(page):
    d = G.gql(page, "ProductShip",
              "query ProductShip($id: ID!){ product(id:$id){ id shippingProfileId } }",
              {"id": PID})
    return (d.get("product") or {}).get("shippingProfileId")


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    print("start policy:", cur_policy(page), "(期待 ORIG", ORIG, ")")
    try:
        print("step1: assign ->", TEMP)
        A.assign_product(page, PID, TEMP)
        print("  OK read-back =", cur_policy(page))
    finally:
        print("step2: restore ->", ORIG)
        A.assign_product(page, PID, ORIG)
        print("  OK read-back =", cur_policy(page))
    final = cur_policy(page)
    print("CANARY", "PASS" if final == ORIG else f"FAIL (final={final})")
