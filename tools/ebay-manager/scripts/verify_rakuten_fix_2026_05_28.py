"""修正後 _detect_rakuten_purchase_status を実 2 サンプルで検証.

期待: 在庫あり (357) -> available / 売り切れ (oos) -> unavailable
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.scrapers import _detect_rakuten_purchase_status  # noqa: E402

BASE = Path(__file__).resolve().parents[1] / "data" / "tmp"
cases = [
    ("在庫あり 357 (tuzukiya/m20-5806)", BASE / "rakuten_357_raw.html",
     "https://item.rakuten.co.jp/tuzukiya/m20-5806/", "available"),
    ("売り切れ sample (probe)", BASE / "ec_direct_url_probe" / "rakuten_oos_raw.html",
     "https://item.rakuten.co.jp/shop/oos/", "unavailable"),
    ("在庫あり sample (probe)", BASE / "ec_direct_url_probe" / "rakuten_in_raw.html",
     "https://item.rakuten.co.jp/shop/in/", "available"),
]
ok = True
for label, path, url, expected in cases:
    if not path.exists():
        print(f"[SKIP] {label}: file not found {path}")
        continue
    html = path.read_text(encoding="utf-8", errors="ignore")
    got = _detect_rakuten_purchase_status(url, html)
    mark = "OK" if got == expected else "FAIL"
    if got != expected:
        ok = False
    print(f"[{mark}] {label}: got={got!r} expected={expected!r}")
print("ALL PASS" if ok else "SOME FAILED")
