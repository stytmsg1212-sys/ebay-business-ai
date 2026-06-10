#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag /stock の UI 構造 probe (read-only、2026-06-10 B方式 支援用)。

CDP Chrome (port 9222) に attach し、フィルター入力欄・アーカイブ切替の
有無を観測する。書込/トグル操作は一切しない。
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        print("\n--- visible inputs/selects ---")
        for i, el in enumerate(pg.locator("input, select").all()[:25]):
            try:
                ph = el.get_attribute("placeholder") or ""
                nm = el.get_attribute("name") or ""
                tp = el.get_attribute("type") or el.evaluate("e=>e.tagName")
                print(f"{i} | {tp} | name={nm} | ph={ph} | vis={el.is_visible()}")
            except Exception as ex:  # noqa: BLE001 - probe 用途、観測継続優先
                print(i, "err", str(ex)[:60])

        print("\n--- links containing archiv ---")
        for a in pg.locator("a").all():
            try:
                href = a.get_attribute("href") or ""
                if "archiv" in href.lower():
                    print("ARCHIVE LINK:", href, "|", (a.inner_text() or "")[:40])
            except Exception:  # noqa: BLE001
                continue

        print("\n--- buttons (first 15) ---")
        for i, btn in enumerate(pg.locator("button").all()[:15]):
            try:
                if btn.is_visible():
                    print(f"btn{i}:", (btn.inner_text() or "").strip()[:50])
            except Exception:  # noqa: BLE001
                continue


if __name__ == "__main__":
    sys.exit(main())
