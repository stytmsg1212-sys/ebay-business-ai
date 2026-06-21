"""商品→ポリシー割当の GraphQL mutation を捕捉 (v2、picker の中身も確認)。

product modal で「別のポリシーを選択」→ picker の中身を dump → MAG_2-3kg_7day を
選択しようと試み、その際の全 graphql mutation を採取。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

PRODUCT_ID = "718746583"  # 2-3kg
TARGET = "MAG_2-3kg_7day"
MUT = []


def on_req(req):
    try:
        if "graphql" in req.url and req.method == "POST":
            j = json.loads(req.post_data or "{}")
            if "mutation" in (j.get("query") or ""):
                MUT.append({"op": j.get("operationName"), "q": (j.get("query") or "")[:200],
                            "v": j.get("variables")})
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

    # 現在の配送ポリシー周辺テキスト
    info = pg.evaluate(
        "() => { const t=document.body.innerText||''; "
        "return t.split('\\n').filter(l=>/配送ポリシー|ポリシー|別のポリシー/.test(l)).slice(0,8); }"
    )
    print("policy area:", json.dumps(info, ensure_ascii=False)[:300])

    # 「別のポリシーを選択」
    try:
        pg.get_by_text("別のポリシーを選択", exact=False).first.click(timeout=8000)
        pg.wait_for_timeout(2500)
    except Exception as e:
        print("picker open NG:", str(e)[:80])

    # picker に出ているポリシー候補を dump
    opts = pg.evaluate(
        "() => Array.from(document.querySelectorAll('option,li,[role=option],[role=menuitem]'))"
        ".map(e=>(e.innerText||'').trim()).filter(t=>t && t.length<50).slice(0,40)"
    )
    mag_opts = [o for o in opts if "MAG_" in o]
    print(f"picker options total={len(opts)} / MAG含む={len(mag_opts)}: {mag_opts[:8]}")

    MUT.clear()
    # MAG_2-3kg_7day を選択 (複数手段)
    selected = False
    for sel in (TARGET,):
        try:
            pg.get_by_text(sel, exact=True).first.click(timeout=5000)
            selected = True
            print(f"selected {sel} (exact)")
            break
        except Exception:
            try:
                pg.get_by_text(sel, exact=False).first.click(timeout=4000)
                selected = True
                print(f"selected {sel} (substr)")
                break
            except Exception as e:
                print(f"select {sel} NG:", str(e)[:80])
    pg.wait_for_timeout(3500)

    print(f"\n=== 割当時 mutation: {len(MUT)} ===")
    for m in MUT:
        print(f"  op={m['op']} q={m['q'][:120]}")
        print(f"  v={json.dumps(m['v'], ensure_ascii=False)[:400]}")
    if MUT:
        json.dump(MUT, open("data/tmp_assign_mutation.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("saved data/tmp_assign_mutation.json")
