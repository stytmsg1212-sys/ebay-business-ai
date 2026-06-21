"""配送ポリシー割当 mutation を JS フックで盗聴 (v5)。

CDP Network ドメインが connect_over_cdp 既存タブで blind だったため、
page.evaluate (Runtime は生きている) で window.fetch / XHR をラップして
graphql リクエストを window.__cap に貯め、ループで読み出す。
self-test で私の fetch がフックに乗るか先に検証。
money-direct: self-test は introspection query のみ。state 変更は user 操作のみ。
"""
import sys, json, time
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

OUT = "data/tmp_assign_capture.json"
REQS = []

HOOK = r"""() => {
  if (window.__cap) return 'already';
  window.__cap = [];
  const rec = (url, method, body) => {
    try {
      const u = String(url);
      const m = String(method || 'GET').toUpperCase();
      if (u.includes('graphql') || m !== 'GET') {
        window.__cap.push({url: u, method: m,
          body: body ? String(body).slice(0, 1500) : null, t: Date.now()});
      }
    } catch (e) {}
  };
  const of = window.fetch;
  window.fetch = function(...a) {
    try {
      const url = (a[0] && a[0].url) || a[0];
      const opt = a[1] || {};
      rec(url, opt.method, opt.body);
    } catch (e) {}
    return of.apply(this, a);
  };
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__m = m; this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(b) { rec(this.__u, this.__m, b); return os.apply(this, arguments); };
  return 'hooked';
}"""

SELFTEST = r"""() => {
  const tok = (document.querySelector('meta[name=csrf-token]') || {}).content || '';
  fetch('/graphql', {method:'POST', credentials:'include',
    headers:{'content-type':'application/json','x-csrf-token':tok},
    body: JSON.stringify({operationName:'__selftest', query:'query __selftest{__typename}'})});
}"""

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    target = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                target = pg
    if not target:
        print("NO ebaymag tab found", flush=True)
        sys.exit(1)
    target.bring_to_front()
    print("hook:", target.evaluate(HOOK), flush=True)

    # ---- SELF-TEST ----
    target.evaluate(SELFTEST)
    time.sleep(2)
    cap = target.evaluate("() => window.__cap || []")
    hit = [c for c in cap if "__selftest" in str(c.get("body") or "")]
    print(f"SELFTEST: __cap={len(cap)} selftest_hit={len(hit)} -> "
          f"{'OK (hook works)' if hit else 'NG (hook blind)'}", flush=True)

    print(">>> USER: この最前面Chromeで 配送ポリシー変更→保存 を再実行 <<<", flush=True)
    seen = len(cap)
    end = time.time() + 240
    done = False
    while time.time() < end and not done:
        time.sleep(2)
        try:
            cap = target.evaluate("() => window.__cap || []")
        except Exception as e:
            print("poll err:", str(e)[:60], flush=True)
            continue
        if len(cap) > seen:
            for r in cap[seen:]:
                REQS.append(r)
                print(f"[CAP] {r.get('method')} {str(r.get('url'))[:90]} "
                      f"body={str(r.get('body'))[:120]}", flush=True)
                if "mutation" in str(r.get("body") or ""):
                    done = True
            seen = len(cap)
            json.dump(REQS, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if done:
        time.sleep(2)
        cap = target.evaluate("() => window.__cap || []")
        REQS[:] = cap
        json.dump(REQS, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n=== done: {len(REQS)} req(s) -> {OUT} ===", flush=True)
    for r in REQS:
        print(f"  {r.get('method')} {str(r.get('url'))[:90]}\n     body={str(r.get('body'))[:400]}",
              flush=True)
