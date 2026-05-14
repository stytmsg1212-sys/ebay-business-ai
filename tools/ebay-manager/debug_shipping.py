#!/usr/bin/env python
"""eBay API レスポンスをデバッグして、送料情報を確認"""

import sys
from monitor.ebay_client import get_active_listings
from monitor.database import init_db, get_site_configs
from calculator import load_settings

init_db()

# 設定から eBay 認証情報を取得
s = load_settings()
app_id = s.get("ebay_app_id", "")
dev_id = s.get("ebay_dev_id", "")
cert_id = s.get("ebay_cert_id", "")
user_token = s.get("ebay_user_token", "")

if not all([app_id, dev_id, cert_id, user_token]):
    print("❌ eBay API 認証情報が設定されていません")
    sys.exit(1)

print("🔍 eBay API からアイテムを取得中...")
try:
    listings = get_active_listings(app_id, dev_id, cert_id, user_token)
    print(f"✅ {len(listings)} 件取得")

    if listings:
        print("\n📋 最初の3件の詳細:")
        for idx, item in enumerate(listings[:3]):
            print(f"\n[{idx+1}] {item['sku']} - {item['title'][:50]}")
            print(f"  Item ID: {item['item_id']}")
            print(f"  Price: ${item.get('current_price', 0):.2f}")
            print(f"  Shipping: ${item.get('shipping_cost', 0):.2f}")
            print(f"  Watch: {item.get('watch_count', 0)}")
            print(f"  View: {item.get('view_count', 0)}")
            print(f"  Sales: {item.get('sales_count_30d', 0)}")
except Exception as e:
    print(f"❌ エラー: {e}")
    import traceback
    traceback.print_exc()
