"""eBaymag FX レート取得 + 正規クエリで GraphQL read が API で動くか確認 (read-only)。"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

GQL_JS = r"""async (args) => {
  const r = await fetch('https://ebaymag.com/graphql', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify({operationName: args.op, query: args.q, variables: args.v}),
  });
  return {status: r.status, json: await r.json()};
}"""

LIST_Q = """query ShippingProfilesList($first: Int) {
  profiles(first: $first) {
    nodes { id title color dispatchTime numberOfProducts __typename }
    __typename
  }
}"""

FX_Q = """query ShippingProfileAdditional {
  currencies { code rate __typename }
  viewer { id currency __typename }
}"""


def gql(pg, op, q, v):
    return pg.evaluate(GQL_JS, {"op": op, "q": q, "v": v})


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(3000)

    fx = gql(pg, "ShippingProfileAdditional", FX_Q, {})
    print("=== FX (currencies) ===")
    print("status:", fx.get("status"))
    data = (fx.get("json") or {}).get("data", {})
    print("viewer currency:", data.get("viewer", {}).get("currency"))
    for c in data.get("currencies", []):
        if c.get("code") in ("USD", "CAD", "AUD", "GBP", "EUR", "JPY"):
            print(f"  {c['code']}: rate={c['rate']}")

    lst = gql(pg, "ShippingProfilesList", LIST_Q, {"first": 30})
    print("\n=== ShippingProfilesList ===")
    print("status:", lst.get("status"))
    nodes = (lst.get("json") or {}).get("data", {}).get("profiles", {}).get("nodes", [])
    print(f"total nodes: {len(nodes)}")
    for n in nodes:
        if "DDP" in (n.get("title") or ""):
            print(f"  id={n['id']} title={n['title']} numProducts={n.get('numberOfProducts')}")
