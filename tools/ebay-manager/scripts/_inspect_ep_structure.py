"""MAG_2-3kg_7day (canary済) の ebayProfile 構造を dump (read-only)。

excludedCountries が ep 直下か payload 内かを実機確認 (HIGH-2 検証追加の前提)。
"""
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
    page.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)
    profs = G.list_profiles(page, first=200)
    pid = next(str(x["id"]) for x in profs if x.get("title") == "MAG_2-3kg_7day")
    prof = G.read_profile(page, pid)
    for ep in prof["shippingEbayProfiles"]:
        if ep["siteId"] == 2:  # CA
            print("ep top-level keys:", list(ep.keys()))
            print("ep.excludedCountries:", ep.get("excludedCountries"))
            pl = ep.get("payload") or {}
            print("payload keys:", list(pl.keys()) if isinstance(pl, dict) else type(pl))
            if isinstance(pl, dict):
                exc = pl.get("excludedCountries")
                print(f"payload.excludedCountries: type={type(exc)} "
                      f"len={len(exc) if isinstance(exc, list) else 'N/A'} "
                      f"sample={exc[:5] if isinstance(exc, list) else exc}")
            break
