#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag フィルターパネル probe (read-only)。フィルター開閉と入力欄観測のみ。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        pg.get_by_role("button", name="フィルター").click()
        pg.wait_for_timeout(1500)

        print("\n--- inputs/selects after filter open ---")
        for i, el in enumerate(pg.locator("input, select, textarea").all()[:30]):
            try:
                ph = el.get_attribute("placeholder") or ""
                nm = el.get_attribute("name") or ""
                tp = el.get_attribute("type") or el.evaluate("e=>e.tagName")
                print(f"{i} | {tp} | name={nm} | ph={ph} | vis={el.is_visible()}")
            except Exception as ex:  # noqa: BLE001
                print(i, "err", str(ex)[:60])

        print("\n--- filter panel text ---")
        try:
            panel = pg.locator("form, [class*=filter], [class*=Filter]").first
            print(panel.inner_text(timeout=3000)[:600])
        except Exception as ex:  # noqa: BLE001
            print("panel text miss:", str(ex)[:80])

        # チェックボックス/ラジオの label 一覧 (アーカイブ filter があるか)
        print("\n--- labels ---")
        for lab in pg.locator("label").all()[:30]:
            try:
                if lab.is_visible():
                    print("label:", (lab.inner_text() or "").strip()[:50])
            except Exception:  # noqa: BLE001
                continue


if __name__ == "__main__":
    sys.exit(main())
