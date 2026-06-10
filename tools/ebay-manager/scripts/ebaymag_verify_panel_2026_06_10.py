#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag D2 パネルの保存後検証 (read-only): 各サイトの選択状態 + 出品リンク。

usage: python ebaymag_verify_panel_2026_06_10.py [productId] [item_id] [name_query]
default = D2 試行対象。
"""
import sys

from playwright.sync_api import sync_playwright

SITES = ("ebay.ca", "ebay.co.uk", "ebay.de", "ebay.it",
         "ebay.fr", "ebay.es", "ebay.com.au")


def main() -> None:
    product_id = sys.argv[1] if len(sys.argv) > 1 else "718746654"
    item_id = sys.argv[2] if len(sys.argv) > 2 else "357418890043"
    name_q = sys.argv[3] if len(sys.argv) > 3 else "Leica DISTO"

    url = (f"https://ebaymag.com/stock?name={name_q.replace(' ', '%20')}"
           f"&productId={product_id}")
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto(url, timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        ok = False
        for _ in range(10):
            ok = any(f"/{item_id}" in (a.get_attribute("href") or "")
                     for a in pg.locator("a").all())
            if ok:
                break
            pg.wait_for_timeout(2000)
        if not ok:
            print("ABORT: item_id link not found")
            return
        print("target verified:", item_id)

        # 各サイト行: 選択中ボタン (PSfVs) + 行内リンク
        result = pg.evaluate(
            """(sites) => {
                const out = {};
                for (const site of sites) {
                    const spans = Array.from(document.querySelectorAll('span'))
                        .filter(s => (s.textContent || '').trim() === site);
                    for (const sp of spans) {
                        let node = sp;
                        for (let i = 0; i < 6 && node; i++) {
                            const btns = Array.from(node.querySelectorAll('button'))
                                .filter(b => (b.textContent || '').includes('掲載'));
                            if (btns.length >= 2) {
                                const sel = btns.find(
                                    b => b.className.includes('PSfVs'));
                                const links = Array.from(
                                    node.querySelectorAll('a'))
                                    .map(a => a.href).filter(Boolean);
                                out[site] = {
                                    selected: sel ? sel.textContent.trim() : '?',
                                    links: links,
                                    rowText: (node.innerText || '')
                                        .replace(/\\n/g, ' | ').slice(0, 120),
                                };
                                node = null;
                                break;
                            }
                            if (node) node = node.parentElement;
                        }
                        if (out[site]) break;
                    }
                }
                return out;
            }""",
            list(SITES),
        )
        print()
        for site in SITES:
            info = result.get(site)
            if not info:
                print(f"{site}: ROW NOT FOUND")
                continue
            print(f"{site}: selected=[{info['selected']}]")
            for lk in info["links"]:
                print(f"   link: {lk[:110]}")

        pg.screenshot(path="data/ebaymag_verify_panel.png", full_page=False)
        print("\nscreenshot: data/ebaymag_verify_panel.png")


if __name__ == "__main__":
    sys.exit(main())
