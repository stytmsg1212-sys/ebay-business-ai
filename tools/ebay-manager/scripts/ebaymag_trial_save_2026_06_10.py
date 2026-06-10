#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 試行 step2 続き: 既に IT トグル済みのパネルで UK もトグル → 保存。

前提: 現在のページ = D2 詳細パネル (1 変動 を保存 が表示中)。
ページ遷移せず現状パネルをそのまま操作する。
"""
import sys

from playwright.sync_api import sync_playwright

ITEM_ID = "357418890043"


def click_publish(pg, site: str) -> bool:
    return pg.evaluate(
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


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        print("URL:", pg.url)

        # ガード: 同じパネルが開いたままか (productId + 保存ボタン)
        if "productId=718746654" not in pg.url:
            print("ABORT: panel URL mismatch — 手動で状態確認要")
            return
        body = pg.locator("body").inner_text(timeout=5000)
        if "変動 を保存" not in body:
            print("ABORT: 保存ボタンが見当たらない (パネル状態が変わった)")
            return

        # UK をトグル
        ok = click_publish(pg, "ebay.co.uk")
        print("UK clicked:", ok)
        pg.wait_for_timeout(2000)

        body = pg.locator("body").inner_text(timeout=5000)
        save_label = None
        for line in body.splitlines():
            if "変動 を保存" in line and len(line.strip()) < 30:
                save_label = line.strip()
        print("save button label:", save_label)  # 「2 変動 を保存」期待

        if not save_label or not save_label.startswith("2"):
            print("WARN: 期待した「2 変動」でない — screenshot して中断")
            pg.screenshot(path="data/ebaymag_trial_step2_warn.png")
            return

        # 保存実行
        pg.get_by_role("button", name=save_label).first.click()
        print("save clicked")
        pg.wait_for_timeout(6000)

        body = pg.locator("body").inner_text(timeout=5000)
        for kw in ("保存", "エラー", "失敗", "成功", "掲載", "公開", "上限", "制限"):
            for line in body.splitlines():
                ls = line.strip()
                if kw in ls and 0 < len(ls) < 80:
                    print("MSG:", ls)

        pg.screenshot(path="data/ebaymag_trial_step2_saved.png", full_page=False)
        print("screenshot: data/ebaymag_trial_step2_saved.png")


if __name__ == "__main__":
    sys.exit(main())
