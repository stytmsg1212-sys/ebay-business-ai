"""W183 実機 E2E: listing 357039030883 (user が楽天商品を登録) の仕入先在庫確認.

DB は read-only (SELECT のみ)。check_items_batch は本番 inventory_check と同じ経路
(httpx → 判定不能なら Playwright fallback) で実 web fetch する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.database import get_conn, find_site_config_by_url  # noqa: E402
from monitor.scrapers import check_items_batch  # noqa: E402

EID = "357039030883"

with get_conn() as c:
    el = c.execute(
        "SELECT ebay_item_id, sku, title, source_url, source_url_manual, "
        "source_status, source_last_checked "
        "FROM ebay_listings WHERE ebay_item_id=?", (EID,),
    ).fetchone()
    mi = c.execute(
        "SELECT id, sku, source_url, source_url_manual, site_config_id, "
        "last_status, is_active "
        "FROM monitored_items WHERE ebay_item_id=?", (EID,),
    ).fetchall()

print("=== ebay_listings ===")
print(dict(el) if el else "listing not found")
print("=== monitored_items ===")
for r in mi:
    print(dict(r))

url = (mi[0]["source_url"] if mi else None) or (el["source_url"] if el else None)
print("=== check 対象 URL ===")
print(url)

if not url:
    print("RESULT: source_url 未設定 = チェック不能 (UI で仕入先 URL を保存してください)")
    sys.exit(0)

cfg = find_site_config_by_url(url)
print("=== 解決した site_config ===")
if cfg:
    print(f"{cfg['site_name']} | in_stock={[cfg.get('in_stock_text1'), cfg.get('in_stock_text2')]} "
          f"| sold_out={cfg.get('sold_out_text')!r} | no_page={cfg.get('no_page_text')!r}")
else:
    print("None (site_config 未解決 → 在庫判定は unknown になり得る)")
    sys.exit(0)

item_id = mi[0]["id"] if mi else 999999
batch = [{
    "id": item_id,
    "url": url,
    "in_stock": [cfg.get("in_stock_text1", ""), cfg.get("in_stock_text2", "")],
    "sold_out": [cfg.get("sold_out_text", "")],
    "no_page": [cfg.get("no_page_text", "")],
}]
print("=== check_items_batch 実行 (httpx → Playwright fallback) ===")
res = check_items_batch(batch)
status = res.get(item_id)
label = {
    "available": "在庫あり ✅",
    "unavailable": "在庫切れ ❌",
    "not_found": "ページ無し / 販売終了 ⚠️",
    None: "判定不能 unknown ❓",
}.get(status, str(status))
print("RESULT raw:", res)
print("在庫判定:", label)
