#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag エクスポート完了のバックグラウンド監視 (read-only)。

D2 (productId=718746654) の ebay.it / ebay.co.uk 行に eBay 実 URL が出るまで
10 分間隔で最大 2 時間チェック。結果は stdout + JSON に書く。
"""
import json
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

PRODUCT_URL = "https://ebaymag.com/stock?name=Leica%20DISTO&productId=718746654"
ITEM_ID = "357418890043"
TARGETS = ("ebay.it", "ebay.co.uk")
RESULT_PATH = "data/ebaymag_bg_watch_result.json"
INTERVAL_SEC = 600
MAX_ROUNDS = 12  # 2h


def check_once(pg) -> dict:
    pg.goto(PRODUCT_URL, timeout=45000)
    pg.wait_for_timeout(6000)
    ok = False
    for _ in range(5):
        ok = any(f"/{ITEM_ID}" in (a.get_attribute("href") or "")
                 for a in pg.locator("a").all())
        if ok:
            break
        pg.wait_for_timeout(2000)
    if not ok:
        return {"panel": False}

    rows = pg.evaluate(
        """(sites) => {
            const out = {};
            for (const site of sites) {
                const spans = Array.from(document.querySelectorAll('span'))
                    .filter(s => (s.textContent || '').trim() === site);
                for (const sp of spans) {
                    let node = sp;
                    for (let i = 0; i < 8 && node; i++) {
                        if ((node.className || '').toString()
                                .includes('Nl9zw')) {
                            out[site] = {
                                text: (node.innerText || '')
                                    .replace(/\\n/g, ' | ').slice(0, 160),
                                links: Array.from(node.querySelectorAll('a'))
                                    .map(a => a.href),
                            };
                            break;
                        }
                        node = node.parentElement;
                    }
                    if (out[site]) break;
                }
            }
            return out;
        }""",
        list(TARGETS),
    )
    return {"panel": True, "rows": rows}


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        pg = b.contexts[0].pages[0]
        for rnd in range(MAX_ROUNDS):
            ts = datetime.now().strftime("%H:%M:%S")
            try:
                r = check_once(pg)
            except Exception as e:  # noqa: BLE001 — 監視継続が目的、種別不問で記録
                print(f"[{ts}] round {rnd}: ERROR {e}", flush=True)
                time.sleep(INTERVAL_SEC)
                continue
            if not r.get("panel"):
                print(f"[{ts}] round {rnd}: panel not loaded", flush=True)
                time.sleep(INTERVAL_SEC)
                continue
            rows = r["rows"]
            links_all = all(rows.get(s, {}).get("links") for s in TARGETS)
            for s in TARGETS:
                info = rows.get(s, {})
                print(f"[{ts}] round {rnd}: {s} links={info.get('links')} "
                      f"text={info.get('text', 'N/A')[:70]}", flush=True)
            if links_all:
                result = {"completed_at": ts, "rows": rows}
                with open(RESULT_PATH, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"\nEXPORT COMPLETE — result saved to {RESULT_PATH}",
                      flush=True)
                return
            time.sleep(INTERVAL_SEC)
        print("\nTIMEOUT: 2h 経過してもエクスポート未完了 (要調査)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
