"""商品→ポリシー割当の GraphQL mutation を捕捉する (商品モーダルの「別のポリシーを選択」)。

pid 718746583 (2-3kg, 事故で誤帯) を MAG_2-3kg_7day へ割当てながら mutation 採取。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

PRODUCT_ID = "718746583"
TARGET_POLICY = "MAG_2-3kg_7day"
REQS = []


def on_req(req):
    try:
        if "graphql" in req.url and req.method == "POST":
            j = json.loads(req.post_data or "{}")
            op = j.get("operationName") or ""
            # mutation らしきもの (assign/policy/profile 関連)
            q = j.get("query") or ""
            if "mutation" in q:
                REQS.append({"op": op, "query": q[:300], "variables": j.get("variables")})
    except Exception:
        pass


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.on("request", on_req)
    pg.goto(f"https://ebaymag.com/stock?productId={PRODUCT_ID}",
            wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(4000)

    # 「別のポリシーを選択」を押す
    try:
        pg.get_by_text("別のポリシーを選択", exact=False).first.click(timeout=8000)
        pg.wait_for_timeout(2000)
    except Exception as e:
        print("picker open NG:", str(e)[:100])

    # dropdown から MAG_2-3kg_7day を選択
    REQS.clear()  # 選択/保存の mutation だけ採取
    try:
        pg.get_by_text(TARGET_POLICY, exact=False).first.click(timeout=6000)
        pg.wait_for_timeout(3000)
    except Exception as e:
        print(f"select {TARGET_POLICY} NG:", str(e)[:120])

    print(f"=== 割当時 mutation: {len(REQS)} ===")
    for i, r in enumerate(REQS):
        print(f"\n--- {r['op']} ---")
        print("query:", r["query"][:200])
        print("variables:", json.dumps(r["variables"], ensure_ascii=False)[:400])
