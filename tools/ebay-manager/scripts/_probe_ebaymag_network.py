"""read-only: /shipping の XHR を捕捉し、全ポリシー一覧 JSON / 編集 URL / 内部id を探す。

Codex 提案 #1 (network capture)。仮想化リスト DOM を回避し、ネットワーク層から
全ポリシー (DDP_*) の内部 id・編集 URL・API を得られるか確認する。保存・mutateしない。
"""
import sys, json, re
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

CAPTURED = []


def on_response(resp):
    try:
        url = resp.url
        ct = resp.headers.get("content-type", "")
        if "json" not in ct and not url.endswith(".json"):
            return
        if "ebaymag" not in url:
            return
        body = resp.text()
        # DDP_ または policy/shipping らしき body のみ記録
        if "DDP_" in body or "policy" in url.lower() or "shipping" in url.lower():
            CAPTURED.append({"url": url, "status": resp.status, "len": len(body), "body": body})
    except Exception:
        pass


def _find_policies(body):
    """body JSON から DDP_ を含む policy エントリ (id 付き) を抽出。"""
    try:
        data = json.loads(body)
    except Exception:
        return []
    found = []

    def walk(o, path=""):
        if isinstance(o, dict):
            title = o.get("title") or o.get("name")
            if isinstance(title, str) and "DDP_" in title:
                idv = o.get("id") or o.get("policyId") or o.get("uuid") or o.get("token")
                found.append({"title": title, "id": idv, "keys": list(o.keys())[:12]})
            for k, v in o.items():
                walk(v, path + "." + k)
        elif isinstance(o, list):
            for x in o:
                walk(x, path)
    walk(data)
    return found


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.on("response", on_response)
    pg.goto("https://ebaymag.com/shipping", wait_until="networkidle", timeout=45000)
    pg.wait_for_timeout(6000)

    print(f"captured JSON responses: {len(CAPTURED)}")
    all_policies = []
    for c in CAPTURED:
        pols = _find_policies(c["body"])
        if pols:
            print(f"\n--- {c['url'][:90]} (status {c['status']}, {c['len']}B) ---")
            print(f"  policies with DDP_: {len(pols)}")
            for pl in pols[:15]:
                print(f"    title={pl['title']!r} id={pl['id']!r} keys={pl['keys']}")
            all_policies.extend(pols)
    # 編集 URL/route パターンらしき値
    print("\n=== 全 DDP policy (network) ===")
    seen = {}
    for pl in all_policies:
        seen[pl["title"]] = pl["id"]
    print(json.dumps(seen, ensure_ascii=False, indent=1))
    # URL 一覧 (API endpoint 把握)
    print("\n=== captured endpoint URLs ===")
    for c in CAPTURED:
        print("  ", c["status"], c["url"][:110])
