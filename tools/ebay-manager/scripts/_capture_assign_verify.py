"""listener 健全性 self-test 付き捕捉 (v4)。

0件が続くので、まず私がメインフレームから無害な graphql fetch を1発撃ち、
listener が拾えるか確認 (= attach 健全性の切り分け)。その後 user 操作を待つ。
全 POST/PUT/PATCH/DELETE を記録 (graphql 限定を外し REST も拾う)。
money-direct: self-test は introspection query のみ (state 不変)。
"""
import sys, json, time
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

OUT = "data/tmp_assign_capture.json"
REQS = []


def make_handler(cdp):
    def h(params):
        try:
            req = params.get("request", {}) or {}
            url = req.get("url", "")
            method = req.get("method", "")
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                return
            pd = req.get("postData")
            if not pd and req.get("hasPostData"):
                try:
                    pd = cdp.send("Network.getRequestPostData",
                                  {"requestId": params["requestId"]}).get("postData")
                except Exception:
                    pd = ""
            rec = {"url": url[:140], "method": method, "pd": (pd or "")[:800],
                   "t": time.strftime("%H:%M:%S")}
            REQS.append(rec)
            json.dump(REQS, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"[{rec['t']}] {method} {url[:90]}", flush=True)
        except Exception as e:
            print("h err:", str(e)[:60], flush=True)
    return h


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    target_pg = None
    cdps = []
    for ctx in b.contexts:
        for pg in ctx.pages:
            try:
                cdp = ctx.new_cdp_session(pg)
                cdp.send("Network.enable")
                cdp.on("Network.requestWillBeSent", make_handler(cdp))
                cdps.append(cdp)
                if "ebaymag" in (pg.url or ""):
                    target_pg = pg
            except Exception as e:
                print("attach err:", str(e)[:80], flush=True)
    print(f"attached {len(cdps)} session(s); target={'yes' if target_pg else 'NO'}", flush=True)

    # ---- SELF-TEST: メインフレームから graphql fetch を1発 ----
    if target_pg:
        n0 = len(REQS)
        try:
            target_pg.evaluate("""() => {
                const tok = (document.querySelector('meta[name=csrf-token]')||{}).content || '';
                fetch('/graphql', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/json','x-csrf-token':tok},
                    body: JSON.stringify({operationName:'__selftest',
                                          query:'query __selftest{__typename}'})});
            }""")
        except Exception as e:
            print("selftest err:", str(e)[:80], flush=True)
        time.sleep(3)
        got = len(REQS) - n0
        print(f"SELFTEST: listener saw {got} POST after my fetch -> "
              f"{'OK (listener works)' if got > 0 else 'NG (listener blind to this page)'}",
              flush=True)

    print(">>> USER: この最前面Chromeで 配送ポリシー変更→保存 を再実行 <<<", flush=True)
    end = time.time() + 240
    while time.time() < end:
        time.sleep(2)
    print(f"\n=== done: {len(REQS)} reqs -> {OUT} ===", flush=True)
    for r in REQS:
        print(f"  {r['method']} {r['url']}  pd={r['pd'][:220]}", flush=True)
