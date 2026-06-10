#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 詳細パネル: item_id 対応付け + アクションメニュー probe (read-only)。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        # ebay.com への外部リンク (item_id 抽出)
        print("--- ebay.com links in panel ---")
        for a in pg.locator("a").all():
            try:
                href = a.get_attribute("href") or ""
                if "ebay.com" in href or "/itm/" in href:
                    print("LINK:", href[:120], "|", (a.inner_text() or "").strip()[:30])
            except Exception:  # noqa: BLE001
                continue

        # アクション メニューを開く (メニュー表示のみ、項目はクリックしない)
        print("\n--- アクション menu ---")
        act = pg.get_by_role("button", name="アクション")
        if act.count() == 0:
            act = pg.get_by_text("アクション", exact=True)
        if act.count() > 0:
            act.first.click()
            pg.wait_for_timeout(1200)
            body = pg.locator("body").inner_text(timeout=5000)
            # メニュー出現後の差分らしき行を表示
            for kw in ("アーカイブ", "削除", "復元", "コピー", "終了"):
                for line in body.splitlines():
                    if kw in line and len(line.strip()) < 40:
                        print("MENU?:", line.strip())
            pg.screenshot(path="data/ebaymag_probe7.png", full_page=False)
            print("screenshot: data/ebaymag_probe7.png")
            pg.keyboard.press("Escape")
        else:
            print("アクション button not found")


if __name__ == "__main__":
    sys.exit(main())
