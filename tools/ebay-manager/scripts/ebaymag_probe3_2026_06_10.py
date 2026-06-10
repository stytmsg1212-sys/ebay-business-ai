#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag status/site フィルター選択肢 probe (read-only)。"""
import sys

from playwright.sync_api import sync_playwright


def dump_options(pg, input_name: str) -> None:
    print(f"\n--- options for {input_name} ---")
    inp = pg.locator(f"input[name={input_name}]").first
    inp.click()
    pg.wait_for_timeout(1200)
    # ドロップダウンの選択肢 (role=option or li)
    opts = pg.locator("[role=option], [class*=option], li").all()
    seen = set()
    for o in opts[:40]:
        try:
            if o.is_visible():
                t = (o.inner_text() or "").strip()
                if t and t not in seen and len(t) < 40:
                    seen.add(t)
                    print("opt:", t)
        except Exception:  # noqa: BLE001
            continue
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(500)


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)
        dump_options(pg, "status")
        dump_options(pg, "selectedOnSiteId")


if __name__ == "__main__":
    sys.exit(main())
