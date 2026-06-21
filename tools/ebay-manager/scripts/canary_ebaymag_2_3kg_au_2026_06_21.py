"""CANARY: DDP_2-3kg の Australia タブに $8 を私主導で保存し、読み戻し検証する。

私 (Claude) がネイティブ実入力で「送料無料解除→価格入力→変更を適用 (保存)」まで
完遂できるかの money-direct canary。AU 2-3kg = $8 は canonical (zone11) と一致 = 実漏れ修正。

手順:
  1. DDP_2-3kg 編集を開く
  2. AU (com.au) タブを開く / switcher ON
  3. cost.free を native uncheck → cost.price に 8 を native fill
  4. 「変更を適用」で保存
  5. reload して AU の free=False / price=8 が永続したか読み戻し検証

検証のみで終わらず保存する (canary)。失敗時は Q0 で痕跡を出す。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright

TARGET_BAND_TITLE = "DDP_2-3kg"
AU_VALUE = "8"


def au_state(pg, pid):
    free = pg.locator(f'input[name="{pid}-cp-au-ds-0.cost.free"]')
    price = pg.locator(f'input[name="{pid}-cp-au-ds-0.cost.price"]')
    return {
        "free": free.is_checked() if free.count() else None,
        "price": price.input_value() if price.count() else None,
        "price_enabled": price.is_enabled() if price.count() else None,
    }


def open_editor_au(pg):
    pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(3000)
    pg.get_by_text(TARGET_BAND_TITLE, exact=False).first.click(timeout=8000)
    pg.wait_for_timeout(3000)
    # AU タブ
    for t in ("ebay.com.au", "com.au"):
        try:
            pg.get_by_text(t, exact=False).first.click(timeout=3000)
            break
        except Exception:
            pass
    pg.wait_for_timeout(1500)


def get_pid(pg):
    names = pg.eval_on_selector_all(
        'input[name*="-cp-au-ds-0.cost.free"]', "els => els.map(e => e.name)"
    )
    return names[0].split("-cp-au")[0] if names else None


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        pg = ctx.pages[0]
        pg.bring_to_front()

        open_editor_au(pg)
        pid = get_pid(pg)
        if not pid:
            print("FAIL: AU タブの cost.free を特定できない"); return 1
        print(f"pid={pid}")

        # switcher (各国調整トグル) を必ず ON にする。OFF だと cost.free が disabled。
        sw = pg.locator(f'input[name="{pid}-cp-au-switcher"]')
        if sw.count() and not sw.is_checked():
            print("AU switcher OFF → native check で ON")
            sw.check(timeout=4000)
            pg.wait_for_timeout(1800)
        print("保存前 AU:", au_state(pg, pid))

        # cost.free uncheck + price fill (native)
        free = pg.locator(f'input[name="{pid}-cp-au-ds-0.cost.free"]')
        if free.is_checked():
            free.uncheck(timeout=4000)
            pg.wait_for_timeout(800)
        price = pg.locator(f'input[name="{pid}-cp-au-ds-0.cost.price"]')
        price.fill(AU_VALUE, timeout=4000)
        pg.wait_for_timeout(600)
        print("入力後 AU:", au_state(pg, pid))

        # 保存 (変更を適用)
        try:
            pg.get_by_text("変更を適用", exact=False).first.click(timeout=6000)
            print("「変更を適用」クリック")
        except Exception as e:
            print(f"FAIL: 保存ボタン押下失敗: {str(e)[:140]}"); return 1
        pg.wait_for_timeout(6000)

        # 読み戻し検証 (reload → 再度開く)
        open_editor_au(pg)
        pid2 = get_pid(pg)
        after = au_state(pg, pid2) if pid2 else {"err": "reload後 pid 不在"}
        print("\n=== 読み戻し検証 (reload後) ===")
        print("保存後 AU:", after)
        ok = after.get("free") is False and str(after.get("price")) == AU_VALUE
        print(f"\nCANARY {'PASS' if ok else 'FAIL'}: AU free=False & price={AU_VALUE} 永続"
              f"={'達成' if ok else '未達'}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
