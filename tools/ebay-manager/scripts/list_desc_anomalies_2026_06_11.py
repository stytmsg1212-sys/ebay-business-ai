"""description 破損 (test 等) スキャン結果の一覧表示."""
import json
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

d = json.load(open(r"data\description_scan_2026_06_10.json", encoding="utf-8"))
a = d["anomalies"]
print(len(a), "anomalies / scanned", d["scanned"])
for x in a:
    print(f"{x['item_id']} | {x['listing_status']:<8} | avail={x['qty_available']} "
          f"| len={x['desc_len']:<3} | {x['desc_head'][:20]!r} | {x['title'][:58]}")
