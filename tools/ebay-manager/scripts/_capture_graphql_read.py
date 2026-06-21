"""ポリシー読込時の GraphQL response を捕捉し、ebayProfiles の id→サイト対応を得る。

upsert response / profile query response から各 ebayProfile の
{id, marketplace/site, serviceCode, domsEbayTariffs} を抽出する (read-only、開くだけ)。
"""
import sys, json
from pathlib import Path
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_policy_editor import _open_policy_editor

RESPS = []


def on_response(resp):
    try:
        if "graphql" in resp.url:
            body = resp.text()
            if "ebayProfiles" in body or "Profile" in body:
                RESPS.append(body)
    except Exception:
        pass


def find_ebay_profiles(obj, out):
    if isinstance(obj, dict):
        if "ebayProfiles" in obj and isinstance(obj["ebayProfiles"], list):
            for ep in obj["ebayProfiles"]:
                if isinstance(ep, dict):
                    out.append(ep)
        for v in obj.values():
            find_ebay_profiles(v, out)
    elif isinstance(obj, list):
        for x in obj:
            find_ebay_profiles(x, out)


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.on("response", on_response)
    _open_policy_editor(pg, "DDP_1-2kg")
    pg.wait_for_timeout(3000)

    print(f"captured graphql responses w/ profile: {len(RESPS)}")
    eps = []
    for body in RESPS:
        try:
            find_ebay_profiles(json.loads(body), eps)
        except Exception:
            pass
    # 重複排除 (id 単位、最も richな方)
    by_id = {}
    for ep in eps:
        i = ep.get("id")
        if i and (i not in by_id or len(json.dumps(ep)) > len(json.dumps(by_id[i]))):
            by_id[i] = ep
    print(f"\nunique ebayProfiles: {len(by_id)}")
    for i, ep in by_id.items():
        # サイト/marketplace を示すキーを探す
        site_keys = {k: ep[k] for k in ep
                     if any(s in k.lower() for s in ("site", "market", "domain", "code", "country", "ebay"))
                     and not isinstance(ep[k], (list, dict))}
        doms = ep.get("domsEbayTariffs") or ep.get("domsTariffs") or []
        print(f"  id={i} site_hint={site_keys} doms={json.dumps(doms,ensure_ascii=False)[:160]}")
    Path("data/tmp_graphql_read_capture.json").write_text(
        json.dumps(list(by_id.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n全文 → data/tmp_graphql_read_capture.json")
