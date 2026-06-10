#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保存後の ebay.it / ebay.co.uk 行の全文 dump (read-only)。現パネルを直接読む。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        result = pg.evaluate(
            """() => {
                const out = [];
                for (const site of ['ebay.it', 'ebay.co.uk']) {
                    const spans = Array.from(document.querySelectorAll('span'))
                        .filter(s => (s.textContent || '').trim() === site);
                    for (const sp of spans) {
                        // 行コンテナ: class Nl9zw を上方向に探す
                        let node = sp;
                        for (let i = 0; i < 8 && node; i++) {
                            if ((node.className || '').toString()
                                    .includes('Nl9zw')) {
                                out.push({
                                    site: site,
                                    text: (node.innerText || '')
                                        .replace(/\\n/g, ' | '),
                                    links: Array.from(
                                        node.querySelectorAll('a'))
                                        .map(a => a.href),
                                    buttons: Array.from(
                                        node.querySelectorAll('button'))
                                        .map(b => b.textContent.trim()),
                                });
                                break;
                            }
                            node = node.parentElement;
                        }
                    }
                }
                return out;
            }"""
        )
        for r in result:
            print(f"\n=== {r['site']} ===")
            print("TEXT:", r["text"][:300])
            print("LINKS:", r["links"])
            print("BUTTONS:", r["buttons"])


if __name__ == "__main__":
    sys.exit(main())
