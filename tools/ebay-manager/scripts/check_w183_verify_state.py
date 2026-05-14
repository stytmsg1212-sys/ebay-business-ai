"""W183 verify 状態確認 (one-shot diagnostic). 5/10 verify session 用."""
import sqlite3
import sys
from pathlib import Path

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

DB = Path(__file__).parent.parent / 'data' / 'monitor.db'
conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row
c = conn.cursor()

print('=== 1. price_change_log 直近 24h ===')
rows = c.execute(
    "SELECT id, ebay_item_id, old_price_usd, new_price_usd, "
    "       competitor_item_id, competitor_total_usd, rule_applied, "
    "       triggered_by, success, error_message, changed_at "
    "FROM price_change_log "
    "WHERE changed_at >= datetime('now', '-24 hours') "
    "ORDER BY id DESC LIMIT 10"
).fetchall()
if not rows:
    print('  (履歴なし)')
else:
    for r in rows:
        print(f"  id={r['id']} ItemID={r['ebay_item_id']} "
              f"old={r['old_price_usd']} new={r['new_price_usd']} "
              f"success={r['success']} by={r['triggered_by']} {r['changed_at']}")
        if r['error_message']:
            print(f"    err: {(r['error_message'] or '')[:160]}")

print()
print('=== 2. active competitor_products ===')
rows = c.execute(
    "SELECT cp.id, cp.our_item_id, cp.competitor_item_id, cp.price_rule, "
    "       cp.competitor_price_usd, cp.competitor_shipping_usd, "
    "       cp.last_priced_at, "
    "       el.title, el.current_price, el.shipping_cost, "
    "       el.lp_min_price, el.lp_breakeven_usd, el.purchase_yen, el.weight_g "
    "FROM competitor_products cp "
    "LEFT JOIN ebay_listings el ON el.ebay_item_id = cp.our_item_id "
    "WHERE cp.is_active = 1 "
    "ORDER BY cp.id DESC"
).fetchall()
if not rows:
    print('  (active competitor なし)')
else:
    for r in rows:
        title = (r['title'] or '')[:55]
        print(f"  cp_id={r['id']} our={r['our_item_id']}")
        print(f"    title: {title}")
        print(f"    現価={r['current_price']} 送料={r['shipping_cost']} "
              f"重量={r['weight_g']} 仕入=¥{r['purchase_yen']}")
        print(f"    min={r['lp_min_price']} breakeven={r['lp_breakeven_usd']}")
        print(f"    competitor={r['competitor_item_id']} "
              f"price={r['competitor_price_usd']} "
              f"ship={r['competitor_shipping_usd']} "
              f"last_priced={r['last_priced_at']}")

print()
print('=== 3. lp_min_price 設定済 listing (上位 5 件) ===')
rows = c.execute(
    "SELECT ebay_item_id, title, current_price, shipping_cost, "
    "       purchase_yen, lp_min_price, lp_breakeven_usd, weight_g "
    "FROM ebay_listings "
    "WHERE lp_min_price IS NOT NULL AND lp_min_price > 0 "
    "ORDER BY ebay_item_id DESC LIMIT 5"
).fetchall()
for r in rows:
    print(f"  {r['ebay_item_id']} title={(r['title'] or '')[:35]}")
    print(f"    現価={r['current_price']} 送料={r['shipping_cost']} "
          f"重量={r['weight_g']} 仕入=¥{r['purchase_yen']} "
          f"min={r['lp_min_price']} BE={r['lp_breakeven_usd']}")
