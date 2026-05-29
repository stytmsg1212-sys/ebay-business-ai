"""357039030883 楽天 false-negative 診断: availability microdata の位置/個数を全列挙.

httpx raw HTML を保存し、'itemprop="availability"' / 'schema.org/InStock|OutOfStock'
の全出現位置とその前後文脈を出す。本体商品 vs 関連商品の判別材料を集める。
"""
import re
from pathlib import Path
import httpx

URL = "https://item.rakuten.co.jp/tuzukiya/m20-5806/"
OUT = Path(__file__).resolve().parents[1] / "data" / "tmp" / "rakuten_357_raw.html"
OUT.parent.mkdir(parents=True, exist_ok=True)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
}
r = httpx.get(URL, headers=headers, timeout=20, follow_redirects=True)
html = r.text
OUT.write_text(html, encoding="utf-8")
print(f"HTTP {r.status_code} | len={len(html)} | saved={OUT}")
print(f"InStock 出現数: {html.count('schema.org/InStock')}")
print(f"OutOfStock 出現数: {html.count('schema.org/OutOfStock')}")
print(f"itemprop=availability 出現数: {html.count('itemprop=\"availability\"')}")
print()

# 各 availability microdata の前後文脈 (本体か関連商品か判別)
print("=== 各 availability microdata の前後 160 文字 ===")
for i, m in enumerate(re.finditer(r'itemprop="availability"[^>]*', html)):
    s = max(0, m.start() - 160)
    e = min(len(html), m.end() + 80)
    ctx = re.sub(r"\s+", " ", html[s:e])
    print(f"[{i}] pos={m.start()}: ...{ctx}...")
    print()

# ld+json の availability (Rakuten は ld+json も持つ)
print("=== application/ld+json 内 availability ===")
for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
    blob = m.group(1)
    if "availability" in blob:
        snip = re.sub(r"\s+", " ", blob)
        idx = snip.find("availability")
        print(f"  ld+json availability: ...{snip[max(0,idx-80):idx+80]}...")

# カート/購入ボタン系の存在
print()
print("=== 購入導線シグナル ===")
for label in ["かごに追加", "買い物かご", "ご購入手続き", "在庫あり", "売り切れ",
              "在庫切れ", "SOLD OUT", "購入手続き", "1個", "数量"]:
    print(f"  [{'Y' if label in html else ' '}] {label}")
