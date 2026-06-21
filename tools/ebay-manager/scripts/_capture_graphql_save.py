"""保存時の GraphQL mutation を捕捉する (DDP_1-2kg で ca=$1 を実保存しながら)。

eBaymag の配送ポリシー保存が叩く GraphQL operation (query text + variables) を採取し、
全ポリシーへ API 適用するための payload 構造を解析する。
ca=$1 は 1-2kg の canonical 正値なので保存は実害なし (実修正兼ねる)。
"""
import sys, json
from pathlib import Path
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor.ebaymag_policy_editor import _open_policy_editor, _discover_pid, _select_site_tab

REQS = []


def on_request(req):
    try:
        if "graphql" in req.url and req.method == "POST":
            REQS.append({"post": req.post_data, "url": req.url})
    except Exception:
        pass


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = b.contexts[0].pages[0]
    pg.bring_to_front()
    pg.on("request", on_request)

    _open_policy_editor(pg, "DDP_1-2kg")
    pid = _discover_pid(pg)
    print("pid:", pid)

    # ca タブで switcher ON → free uncheck → price=1
    _select_site_tab(pg, "ca")
    sw = pg.locator(f'input[name="{pid}-cp-ca-switcher"]')
    if sw.count() and not sw.is_checked():
        sw.check(timeout=4000); pg.wait_for_timeout(1500)
    free = pg.locator(f'input[name="{pid}-cp-ca-ds-0.cost.free"]')
    if free.count() and free.is_checked():
        free.uncheck(timeout=4000); pg.wait_for_timeout(700)
    price = pg.locator(f'input[name="{pid}-cp-ca-ds-0.cost.price"]')
    price.fill("1", timeout=4000); pg.wait_for_timeout(500)

    REQS.clear()  # 保存前のクエリを捨て、保存 mutation だけ採取
    pg.get_by_text("変更を適用", exact=False).first.click(timeout=8000)
    pg.wait_for_timeout(6000)

    print(f"\n=== 保存時 GraphQL requests: {len(REQS)} ===")
    for i, r in enumerate(REQS):
        post = r["post"] or ""
        try:
            j = json.loads(post)
            opname = j.get("operationName")
            variables = j.get("variables")
            out = Path("data/tmp_graphql_save_capture.json")
            out.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"req {i}: op={opname} → 全文 {out}")
            # ebayProfiles の構造を要約 (per-site override の場所特定)
            prof = (variables or {}).get("input", {}).get("profile", {})
            eps = prof.get("ebayProfiles", [])
            print(f"  ebayProfiles 件数: {len(eps)}")
            for ep in eps:
                doms = ep.get("domsEbayTariffs") or []
                intl = ep.get("intlEbayTariffs") or []
                if doms or intl or ep.get("excludedCountries"):
                    print(f"    ep id={ep.get('id')} managedByUser={ep.get('managedByUser')} "
                          f"doms={json.dumps(doms,ensure_ascii=False)[:200]} "
                          f"intl={json.dumps(intl,ensure_ascii=False)[:200]} "
                          f"excl={len(ep.get('excludedCountries') or [])}")
        except Exception as e:
            print(f"req {i} parse err: {e}; raw:", post[:300])
