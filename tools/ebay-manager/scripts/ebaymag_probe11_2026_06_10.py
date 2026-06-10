#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag D2 詳細パネル「サイト」セクションの DOM dump (read-only)。

掲載されています/掲載されていません トグルの実体 (input/button/label) と
各サイト行のコンテナ構造を outerHTML で特定する。
"""
import sys

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto(PRODUCT_URL, timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        # パネル読込待ち: item_id リンク出現まで最大 20 秒リトライ
        ok = False
        for _ in range(10):
            ok = any(f"/{ITEM_ID}" in (a.get_attribute("href") or "")
                     for a in pg.locator("a").all())
            if ok:
                break
            pg.wait_for_timeout(2000)
        if not ok:
            pg.screenshot(path="data/ebaymag_probe11_abort.png", full_page=False)
            print("ABORT: item_id link not found (screenshot saved)")
            return
        print("target verified")

        # 「サイト」見出しへスクロール
        heading = pg.get_by_text("サイト", exact=True)
        if heading.count() > 0:
            heading.last.scroll_into_view_if_needed()
            pg.wait_for_timeout(1500)

        # 掲載されてい* を含む要素の行コンテナを outerHTML で dump
        rows = pg.evaluate(
            """() => {
                const out = [];
                const els = Array.from(document.querySelectorAll('*')).filter(
                    e => e.children.length === 0 &&
                         (e.textContent || '').includes('掲載されてい'));
                for (const el of els) {
                    let node = el;
                    // サイト名 (ebay.xx) を含む最小祖先 = 行コンテナ
                    for (let i = 0; i < 10 && node; i++) {
                        const t = node.innerText || '';
                        if (/ebay\\.(com|ca|co\\.uk|de|it|fr|es|com\\.au)/.test(t)) {
                            out.push({
                                rowText: t.replace(/\\n/g, ' | ').slice(0, 150),
                                html: node.outerHTML.slice(0, 1500),
                            });
                            break;
                        }
                        node = node.parentElement;
                    }
                }
                return out;
            }"""
        )
        print(f"\n--- {len(rows)} toggle rows ---")
        seen = set()
        for r in rows:
            key = r["rowText"][:60]
            if key in seen:
                continue
            seen.add(key)
            print("\nROW:", r["rowText"])
            print("HTML:", r["html"][:1200])

        pg.screenshot(path="data/ebaymag_probe11.png", full_page=False)
        print("\nscreenshot: data/ebaymag_probe11.png")


if __name__ == "__main__":
    sys.exit(main())
