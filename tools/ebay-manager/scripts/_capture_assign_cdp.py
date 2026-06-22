"""商品→ポリシー割当 mutation を CDP セッション直叩きで確実に捕捉 (v3)。

v1/v2 の page.on("request") は connect_over_cdp の既存タブ attach が不完全で
0件だった。本版は各 page に new_cdp_session を張り Network.requestWillBeSent を
直接購読 → graphql は query/mutation 問わず全記録 (user 操作が飛んでいるかの
切り分けも兼ねる)。postData が省略される場合は getRequestPostData で補完。

money-direct: 本スクリプトは read-only (listener のみ)。state 変更は user の手動操作。
"""
import sys, json, time
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

OUT = "data/tmp_assign_capture.json"
WAIT_SEC = 300
REQS = []


def make_handler(cdp):
    def h(params):
        try:
            req = params.get("request", {}) or {}
            url = req.get("url", "")
            if "graphql" not in url or req.get("method") != "POST":
                return
            pd = req.get("postData")
            if not pd and req.get("hasPostData"):
                try:
                    pd = cdp.send("Network.getRequestPostData",
                                  {"requestId": params["requestId"]}).get("postData")
                except Exception:
                    pd = ""
            j = json.loads(pd) if pd else {}
            q = j.get("query") or ""
            rec = {"op": j.get("operationName"),
                   "kind": "mutation" if "mutation" in q else "query",
                   "q": q[:500], "v": j.get("variables"),
                   "t": time.strftime("%H:%M:%S")}
            REQS.append(rec)
            json.dump(REQS, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"[{rec['t']}] {rec['kind']} op={rec['op']} "
                  f"vkeys={list((rec['v'] or {}).keys())}", flush=True)
        except Exception as e:
            print("handler err:", str(e)[:80], flush=True)
    return h


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    urls = []
    for ctx in b.contexts:
        for pg in ctx.pages:
            try:
                cdp = ctx.new_cdp_session(pg)
                cdp.send("Network.enable")
                cdp.on("Network.requestWillBeSent", make_handler(cdp))
                urls.append(pg.url)
            except Exception as e:
                print("attach err:", str(e)[:80], flush=True)
    print(f"CDP attached to {len(urls)} page(s): {urls}", flush=True)
    # 操作対象の eBaymag タブを最前面に出す (user がどの Chrome か分かるように)
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                try:
                    pg.bring_to_front()
                    print(f"BROUGHT TO FRONT: {pg.url}", flush=True)
                except Exception as e:
                    print("bring_to_front err:", str(e)[:80], flush=True)
    print(">>> USER: 最前面に出た eBaymag タブで 商品1件 の配送ポリシーを別の MAG_* に変更→保存 <<<",
          flush=True)
    end = time.time() + WAIT_SEC
    while time.time() < end:
        time.sleep(2)
        if any(r["kind"] == "mutation" for r in REQS):
            time.sleep(3)
            break
    print(f"\n=== done: {len(REQS)} graphql req captured -> {OUT} ===", flush=True)
    for r in REQS:
        print(f"  {r['kind']} op={r['op']} v={json.dumps(r['v'], ensure_ascii=False)[:350]}",
              flush=True)
