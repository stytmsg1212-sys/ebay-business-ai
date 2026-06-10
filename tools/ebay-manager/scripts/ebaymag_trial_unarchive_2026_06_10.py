#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 試行 step1: Leica DISTO D2 (357418890043) のアーカイブ解除のみ。

プランv2 グループ [IT,UK] の 1 件目。解除後の状態を観測して終了
(国トグルは次 step で別実行)。
"""
import sys

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?archived=true&name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto(PRODUCT_URL, timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        # 対象確認: リストを表示 リンクが item_id を含むこと (誤対象ガード)
        ok = False
        for a in pg.locator("a").all():
            href = a.get_attribute("href") or ""
            if f"/{ITEM_ID}" in href:
                ok = True
                print("target verified:", href[:100])
                break
        if not ok:
            print("ABORT: item_id link not found — 対象不一致の可能性")
            return

        # アクション → 戻す
        pg.get_by_role("button", name="アクション").first.click()
        pg.wait_for_timeout(1000)
        pg.get_by_text("戻す", exact=True).first.click()
        pg.wait_for_timeout(4000)
        print("after 戻す URL:", pg.url)

        # ダイアログ/確認が出ていないか + パネル状態
        body = pg.locator("body").inner_text(timeout=5000)
        for kw in ("戻す", "確認", "アーカイブ", "復元", "エラー"):
            for line in body.splitlines():
                ls = line.strip()
                if kw in ls and 0 < len(ls) < 60:
                    print("STATE:", ls)

        pg.screenshot(path="data/ebaymag_trial_step1.png", full_page=False)
        print("screenshot: data/ebaymag_trial_step1.png")


if __name__ == "__main__":
    sys.exit(main())
