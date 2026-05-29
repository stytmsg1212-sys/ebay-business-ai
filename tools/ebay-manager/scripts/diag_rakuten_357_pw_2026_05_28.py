"""357 楽天: Playwright レンダリング後の真の在庫を確認 (raw httpx=OutOfStock との差分検証)."""
import asyncio
import re
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://item.rakuten.co.jp/tuzukiya/m20-5806/"
OUT = Path(__file__).resolve().parents[1] / "data" / "tmp" / "rakuten_357_pw.html"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        await page.goto(URL, wait_until="networkidle", timeout=45000)
        html = await page.content()
        await browser.close()
    OUT.write_text(html, encoding="utf-8")
    print(f"rendered len={len(html)} saved={OUT}")
    print(f"InStock 出現: {html.count('schema.org/InStock')}")
    print(f"OutOfStock 出現: {html.count('schema.org/OutOfStock')}")
    for label in ["かごに追加", "買い物かご", "ご購入手続き", "購入手続き",
                  "在庫あり", "売り切れ", "在庫切れ", "SOLD OUT", "再入荷",
                  "数量", "個数"]:
        print(f"  [{'Y' if label in html else ' '}] {label}")
    # availability microdata 文脈
    for m in re.finditer(r'itemprop="availability"[^>]*', html):
        s = max(0, m.start() - 120)
        print("ctx:", re.sub(r"\s+", " ", html[s:m.end() + 40]))


asyncio.run(main())
