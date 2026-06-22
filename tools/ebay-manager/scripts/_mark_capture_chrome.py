"""CDP 9222 の eBaymag タブに赤バナー+タブ印を inject して、user が
『どの Chrome を操作すべきか』を 100% 識別できるようにする。"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    n = 0
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                try:
                    pg.bring_to_front()
                    pg.evaluate("""() => {
                        const old = document.getElementById('__cap_banner__');
                        if (old) old.remove();
                        const d = document.createElement('div');
                        d.id = '__cap_banner__';
                        d.textContent = '⚠ Claude capture中 — このChromeで商品の配送ポリシーを変更してください ⚠';
                        d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                          + 'background:#d00;color:#fff;font-size:18px;padding:16px;'
                          + 'text-align:center;font-weight:bold;font-family:sans-serif;';
                        document.body.appendChild(d);
                        if (!document.title.startsWith('★')) document.title = '★操作するChrome★ ' + document.title;
                    }""")
                    print(f"banner injected -> {pg.url}")
                    n += 1
                except Exception as e:
                    print("inject err:", str(e)[:100])
    print(f"done. {n} eBaymag tab(s) marked.")
