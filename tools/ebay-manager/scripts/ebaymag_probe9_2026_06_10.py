#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag ナビの サイト タブ URL 取得 → サイトページ観測 (read-only)。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto("https://ebaymag.com/stock", timeout=45000)
        pg.wait_for_timeout(3000)

        print("--- nav links ---")
        target = None
        for a in pg.locator("a").all():
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt in ("アイテム", "サイト", "ポリシー", "注文") and href:
                    print(f"{txt} -> {href}")
                    if txt == "サイト":
                        target = href
            except Exception:  # noqa: BLE001
                continue

        if target:
            url = target if target.startswith("http") else f"https://ebaymag.com{target}"
            pg.goto(url, timeout=45000)
            pg.wait_for_timeout(4000)
            print("\nURL:", pg.url)
            body = pg.locator("body").inner_text(timeout=5000)
            print(body[:3000])
            pg.screenshot(path="data/ebaymag_probe9.png", full_page=True)
            print("\nscreenshot: data/ebaymag_probe9.png")


if __name__ == "__main__":
    sys.exit(main())
