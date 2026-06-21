"""プロファイル読取の GraphQL query (operation/query/variables) を捕捉する。

editor を開く際に飛ぶ query を採取し、policy id から page.request で profile を
取得できるようにする (GraphQL クライアント基盤)。read-only。
"""
import sys, json
from pathlib import Path
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_policy_editor import _open_policy_editor

REQS = []


def on_request(req):
    try:
        if "graphql" in req.url and req.method == "POST":
            j = json.loads(req.post_data or "{}")
            REQS.append({"op": j.get("operationName"), "query": j.get("query"),
                         "variables": j.get("variables")})
    except Exception:
        pass


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.on("request", on_request)
    _open_policy_editor(pg, "DDP_1-2kg")
    pg.wait_for_timeout(4000)

    print(f"captured graphql ops: {[r['op'] for r in REQS]}")
    # profile / shipping を返しそうな query を探す
    for r in REQS:
        q = r["query"] or ""
        if "shippingEbayProfiles" in q or ("profile" in q.lower() and "query" in q.lower()):
            print(f"\n=== op={r['op']} variables={json.dumps(r['variables'],ensure_ascii=False)} ===")
            print(q[:900])
            Path(f"data/tmp_gql_read_query_{r['op']}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  → saved data/tmp_gql_read_query_{r['op']}.json")
