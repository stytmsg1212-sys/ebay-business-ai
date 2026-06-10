#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag エクスポート完了ポーリング (read-only): IT/UK 行に eBay 実 URL が
出るまで 30 秒間隔で最大 10 回リロードして確認。
"""
import sys

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"
TARGETS = ("ebay.it", "ebay.co.uk")


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]

        for attempt in range(10):
            pg.goto(PRODUCT_URL, timeout=45000)
            pg.wait_for_timeout(5000)

            ok = any(f"/{ITEM_ID}" in (a.get_attribute("href") or "")
                     for a in pg.locator("a").all())
            if not ok:
                print(f"[{attempt}] panel not loaded, retry")
                pg.wait_for_timeout(5000)
                continue

            result = pg.evaluate(
                """(sites) => {
                    const out = {};
                    for (const site of sites) {
                        const spans = Array.from(
                            document.querySelectorAll('span'))
                            .filter(s => (s.textContent || '').trim() === site);
                        for (const sp of spans) {
                            let node = sp;
                            for (let i = 0; i < 8 && node; i++) {
                                if ((node.className || '').toString()
                                        .includes('Nl9zw')) {
                                    out[site] = {
                                        text: (node.innerText || '')
                                            .replace(/\\n/g, ' | ').slice(0, 160),
                                        links: Array.from(
                                            node.querySelectorAll('a'))
                                            .map(a => a.href),
                                    };
                                    break;
                                }
                                node = node.parentElement;
                            }
                            if (out[site]) break;
                        }
                    }
                    return out;
                }""",
                list(TARGETS),
            )
            done = True
            for site in TARGETS:
                info = result.get(site, {})
                links = info.get("links", [])
                print(f"[{attempt}] {site}: links={links} "
                      f"text={info.get('text', 'N/A')[:80]}")
                if not links:
                    done = False
            if done:
                print("\nEXPORT COMPLETE — both links present")
                return
            pg.wait_for_timeout(25000)

        print("\nTIMEOUT: links not yet present after ~5 min (export pending)")


if __name__ == "__main__":
    sys.exit(main())
