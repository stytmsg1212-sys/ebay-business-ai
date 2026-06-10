#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 試行 step2: D2 (357418890043) を指定 1 サイトで「掲載されている」へ切替。

usage: python ebaymag_trial_publish_2026_06_10.py ebay.it
1 サイトずつ実行し、クリック前後の状態とダイアログ有無を観測する。
"""
import sys

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"


def row_state(pg, site: str) -> dict:
    """サイト行の 2 ボタンの class/tabindex を読む (PSfVs = 選択中の想定)。"""
    return pg.evaluate(
        """(site) => {
            const spans = Array.from(document.querySelectorAll('span'))
                .filter(s => (s.textContent || '').trim() === site);
            for (const sp of spans) {
                let node = sp;
                for (let i = 0; i < 6 && node; i++) {
                    const btns = Array.from(node.querySelectorAll('button'))
                        .filter(b => (b.textContent || '').includes('掲載'));
                    if (btns.length >= 2) {
                        return btns.map(b => ({
                            text: (b.textContent || '').trim(),
                            cls: b.className,
                            tabindex: b.getAttribute('tabindex'),
                        }));
                    }
                    node = node.parentElement;
                }
            }
            return null;
        }""",
        site,
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: ebaymag_trial_publish_2026_06_10.py <site e.g. ebay.it>")
        return
    site = sys.argv[1]

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.goto(PRODUCT_URL, timeout=45000)
        pg.wait_for_timeout(4000)
        print("URL:", pg.url)

        # 誤対象ガード (最大 20 秒リトライ)
        ok = False
        for _ in range(10):
            ok = any(f"/{ITEM_ID}" in (a.get_attribute("href") or "")
                     for a in pg.locator("a").all())
            if ok:
                break
            pg.wait_for_timeout(2000)
        if not ok:
            print("ABORT: item_id link not found")
            return
        print("target verified:", ITEM_ID)

        before = row_state(pg, site)
        print(f"\nBEFORE {site}:", before)
        if not before:
            print("ABORT: site row not found")
            return
        # 既に掲載済みなら何もしない (冪等ガード)
        pub_btn = next((x for x in before if x["text"] == "掲載されている"), None)
        if pub_btn and "PSfVs" in pub_btn["cls"]:
            print("SKIP: already published state")
            return

        # サイト行内の「掲載されている」ボタンをクリック
        clicked = pg.evaluate(
            """(site) => {
                const spans = Array.from(document.querySelectorAll('span'))
                    .filter(s => (s.textContent || '').trim() === site);
                for (const sp of spans) {
                    let node = sp;
                    for (let i = 0; i < 6 && node; i++) {
                        const btn = Array.from(node.querySelectorAll('button'))
                            .find(b => (b.textContent || '').trim() === '掲載されている');
                        if (btn) { btn.click(); return true; }
                        node = node.parentElement;
                    }
                }
                return false;
            }""",
            site,
        )
        print("clicked:", clicked)
        pg.wait_for_timeout(4000)

        after = row_state(pg, site)
        print(f"AFTER  {site}:", after)

        # ダイアログ / 確認 / エラー文言の検出
        body = pg.locator("body").inner_text(timeout=5000)
        for kw in ("確認", "エラー", "保存", "制限", "上限", "失敗"):
            for line in body.splitlines():
                ls = line.strip()
                if kw in ls and 0 < len(ls) < 80:
                    print("MSG:", ls)

        safe = site.replace(".", "_")
        pg.screenshot(path=f"data/ebaymag_trial_step2_{safe}.png", full_page=False)
        print(f"screenshot: data/ebaymag_trial_step2_{safe}.png")


if __name__ == "__main__":
    sys.exit(main())
