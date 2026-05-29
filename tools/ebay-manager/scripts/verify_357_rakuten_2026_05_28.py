"""357039030883 の楽天在庫判定 (unavailable) が誤判定でないか生 HTML で検算."""
import httpx

URL = "https://item.rakuten.co.jp/tuzukiya/m20-5806/"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
}
r = httpx.get(URL, headers=headers, timeout=20, follow_redirects=True)
html = r.text
print("HTTP:", r.status_code, "| len:", len(html))
checks = {
    "InStock microdata": 'itemprop="availability" content="http://schema.org/InStock"',
    "OutOfStock microdata": 'itemprop="availability" content="http://schema.org/OutOfStock"',
    "no_page (ご指定のページ)": "ご指定のページは見つかりません",
    "売り切れ": "売り切れ",
    "在庫切れ": "在庫切れ",
    "かごに追加": "かごに追加",
}
for label, needle in checks.items():
    print(f"  [{'Y' if needle in html else ' '}] {label}")
# title 確認 (正しい商品ページか)
import re
m = re.search(r"<title>(.*?)</title>", html, re.S)
print("title:", (m.group(1).strip()[:120] if m else "(none)"))
