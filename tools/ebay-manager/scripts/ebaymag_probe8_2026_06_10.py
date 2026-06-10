#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag サイトページ probe (read-only) — 各国サイトの枠/状態確認。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto("https://ebaymag.com/sites", timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)
        body = pg.locator("body").inner_text(timeout=5000)
        print(body[:3500])
        pg.screenshot(path="data/ebaymag_probe8.png", full_page=True)
        print("\nscreenshot: data/ebaymag_probe8.png")


if __name__ == "__main__":
    sys.exit(main())
