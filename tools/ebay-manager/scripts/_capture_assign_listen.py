"""商品→ポリシー割当の GraphQL mutation を「user の UI 操作を裏で盗聴」して捕捉。

_capture_assign_v2.py は自前 click で商品モーダルが flaky だったため、
listener だけ張って navigate せず待機し、user が UI で配送ポリシーを変更した
瞬間に打たれる graphql mutation を採取する。捕捉した mutation は逐次 JSON に書き出す。

money-direct: 本スクリプト自身は何も mutate しない (read-only listener)。
state を変えるのは user の手動操作のみ (捕捉後に元へ戻すこと)。
"""
import sys, json, time
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

OUT = "data/tmp_assign_capture.json"
WAIT_SEC = 300
MUT = []


def _record(req):
    try:
        if "graphql" not in req.url or req.method != "POST":
            return
        j = json.loads(req.post_data or "{}")
        q = j.get("query") or ""
        if "mutation" not in q:
            return
        rec = {"op": j.get("operationName"), "q": q[:400],
               "v": j.get("variables"), "t": time.strftime("%H:%M:%S")}
        MUT.append(rec)
        json.dump(MUT, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[{rec['t']}] CAPTURED op={rec['op']}  vars_keys={list((rec['v'] or {}).keys())}",
              flush=True)
    except Exception as e:
        print("parse err:", str(e)[:80], flush=True)


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    attached = 0
    for ctx in b.contexts:
        # 既存 page 全部に listener
        for pg in ctx.pages:
            pg.on("request", _record)
            attached += 1
        # 新規 page (user が新タブ開いた場合) にも自動で張る
        ctx.on("page", lambda pg: (pg.on("request", _record)))
    print(f"listening on {attached} page(s) for {WAIT_SEC}s.", flush=True)
    print(">>> USER: eBaymag UI で 商品1件 の配送ポリシーを別の MAG_* へ変更してください <<<",
          flush=True)
    keep = b.contexts[0].pages[0] if b.contexts and b.contexts[0].pages else None
    end = time.time() + WAIT_SEC
    while time.time() < end:
        if keep:
            keep.wait_for_timeout(1500)
        else:
            time.sleep(1.5)
        if MUT and any(m["op"] not in (None, "productSave") for m in MUT):
            # productSave 以外の mutation を1つでも掴んだら少し余韻を取って終了
            time.sleep(3)
            break
    print(f"\n=== done: {len(MUT)} mutation(s) captured -> {OUT} ===", flush=True)
    for m in MUT:
        print(f"  op={m['op']}  vars={json.dumps(m['v'], ensure_ascii=False)[:300]}", flush=True)
