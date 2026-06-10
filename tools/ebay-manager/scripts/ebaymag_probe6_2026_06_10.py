#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag item 詳細パネル probe (read-only)。

Leica DISTO D2 (item 357418890043, グループ IT,UK) の行を開き、
- eBay item_id / SKU の表示有無 (行→worksheet 対応付けの鍵)
- 国トグルの構造と現在状態
- unarchive (アーカイブ解除) コントロール
を観測する。トグル変更・保存は一切しない。
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        # D2 行クリック (タイトル一意)
        row = pg.get_by_text("Leica DISTO D2 Laser Distance Meter", exact=False).first
        row.click()
        pg.wait_for_timeout(3500)
        print("after click URL:", pg.url)

        body = pg.locator("body").inner_text(timeout=5000)
        print("--- panel text (first 2500 chars) ---")
        print(body[:2500])

        # item_id らしき文字列
        if "357418890043" in body:
            print("\n*** eBay item_id 357418890043 FOUND in panel ***")
        else:
            print("\n*** eBay item_id NOT visible in panel text ***")

        # トグル (checkbox/switch) 列挙
        print("\n--- switches/checkboxes ---")
        for i, el in enumerate(pg.locator("input[type=checkbox], [role=switch]").all()[:25]):
            try:
                nm = el.get_attribute("name") or ""
                checked = el.is_checked() if el.get_attribute("type") == "checkbox" else el.get_attribute("aria-checked")
                print(f"{i} | name={nm} | checked={checked} | vis={el.is_visible()}")
            except Exception as ex:  # noqa: BLE001
                print(i, "err", str(ex)[:60])

        pg.screenshot(path="data/ebaymag_probe6.png", full_page=False)
        print("screenshot: data/ebaymag_probe6.png")


if __name__ == "__main__":
    sys.exit(main())
