#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 試行 step2 前の read-only probe: D2 (非アーカイブ) 詳細パネルの
サイト行ごとのトグル (checkbox/switch) 構造を特定する。クリックは一切しない。
"""
import sys

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"
SITES = ("ebay.com", "ebay.ca", "ebay.co.uk", "ebay.de", "ebay.it",
         "ebay.fr", "ebay.es", "ebay.com.au")


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto(PRODUCT_URL, timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        # 誤対象ガード
        ok = False
        for a in pg.locator("a").all():
            href = a.get_attribute("href") or ""
            if f"/{ITEM_ID}" in href:
                ok = True
                print("target verified:", href[:100])
                break
        if not ok:
            print("ABORT: item_id link not found")
            return

        # サイト名テキストを含む行コンテナを特定し、内部の input/スイッチを dump
        for site in SITES:
            row = pg.get_by_text(site, exact=True)
            if row.count() == 0:
                print(f"{site}: NOT FOUND")
                continue
            # 祖先を遡って checkbox/switch を含む最小コンテナを探す
            handle = row.first.element_handle()
            info = pg.evaluate(
                """(el) => {
                    let node = el;
                    for (let i = 0; i < 8 && node; i++) {
                        const inputs = node.querySelectorAll('input');
                        if (inputs.length > 0) {
                            const states = [];
                            inputs.forEach(inp => states.push({
                                type: inp.type,
                                checked: inp.checked,
                                disabled: inp.disabled,
                                name: inp.name || '',
                                id: inp.id || '',
                            }));
                            return {
                                depth: i,
                                tag: node.tagName,
                                cls: (node.className || '').toString().slice(0, 80),
                                text: (node.innerText || '').replace(/\\n/g, ' | ').slice(0, 120),
                                inputs: states,
                            };
                        }
                        node = node.parentElement;
                    }
                    return null;
                }""",
                handle,
            )
            print(f"\n=== {site} ===")
            print(info)

        # 保存ボタンの有無
        print("\n--- save-like buttons ---")
        for btn in pg.locator("button").all():
            try:
                t = (btn.inner_text() or "").strip()
                if t and any(k in t for k in ("保存", "適用", "更新", "公開")):
                    print("BTN:", t)
            except Exception:  # noqa: BLE001
                continue

        pg.screenshot(path="data/ebaymag_probe10.png", full_page=False)
        print("\nscreenshot: data/ebaymag_probe10.png")


if __name__ == "__main__":
    sys.exit(main())
