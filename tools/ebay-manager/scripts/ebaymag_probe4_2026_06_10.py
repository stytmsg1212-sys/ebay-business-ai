#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag タイトルフィルタ実用性テスト (read-only)。

目的: アーカイブ済み item をタイトルフィルタで特定できるか。
操作: reload → フィルター開く → name に 'Leica DISTO' 入力 → 適用 → 行数観測。
書込系 (unarchive/トグル/保存) は一切しない。
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto("https://ebaymag.com/stock", timeout=45000)
        pg.wait_for_timeout(3000)
        print("URL:", pg.url)

        # ヘッダの件数表示
        try:
            head = pg.locator("body").inner_text(timeout=5000)
            for line in head.splitlines():
                if "アイテム" in line and len(line) < 30:
                    print("count line:", line.strip())
        except Exception:  # noqa: BLE001
            pass

        # 「すべてのアイテムを表示」があれば押す (アーカイブ含む全表示の想定)
        show_all = pg.get_by_role("button", name="すべてのアイテムを表示")
        if show_all.count() > 0 and show_all.first.is_visible():
            show_all.first.click()
            pg.wait_for_timeout(2500)
            print("clicked すべてのアイテムを表示 → URL:", pg.url)

        # フィルター開いてタイトル入力
        fl = pg.get_by_role("button", name="フィルター")
        if fl.count() > 0 and fl.first.is_visible():
            fl.first.click()
            pg.wait_for_timeout(1000)
        name_inp = pg.locator("input[name=name]").first
        name_inp.fill("Leica DISTO")
        pg.wait_for_timeout(500)
        name_inp.press("Enter")
        pg.wait_for_timeout(3000)
        print("after filter URL:", pg.url)

        # 行観測: item リンク (ebay item URL or 内部リンク) を列挙
        body = pg.locator("body").inner_text(timeout=5000)
        for line in body.splitlines():
            ls = line.strip()
            if "Leica" in ls or ("アイテム" in ls and len(ls) < 30):
                print("ROW:", ls[:100])

        pg.screenshot(path="data/ebaymag_probe4.png", full_page=False)
        print("screenshot: data/ebaymag_probe4.png")


if __name__ == "__main__":
    sys.exit(main())
