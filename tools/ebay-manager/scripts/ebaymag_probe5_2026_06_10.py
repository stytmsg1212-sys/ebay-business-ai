#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag アーカイブ済み + タイトルフィルタ適用テスト (read-only)。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto("https://ebaymag.com/stock", timeout=45000)
        pg.wait_for_timeout(3000)

        pg.get_by_role("button", name="フィルター").click()
        pg.wait_for_timeout(1000)

        # アーカイブ済み chip
        pg.get_by_text("アーカイブ済み", exact=True).first.click()
        pg.wait_for_timeout(500)

        # タイトル入力
        pg.locator("input[name=name]").first.fill("Leica DISTO")
        pg.wait_for_timeout(300)

        # 適用
        pg.get_by_role("button", name="フィルターを適用する").click()
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        body = pg.locator("body").inner_text(timeout=5000)
        for line in body.splitlines():
            ls = line.strip()
            if ls and ("Leica" in ls or ("アイテム" in ls and len(ls) < 25)):
                print("ROW:", ls[:110])

        pg.screenshot(path="data/ebaymag_probe5.png", full_page=False)
        print("screenshot: data/ebaymag_probe5.png")


if __name__ == "__main__":
    sys.exit(main())
