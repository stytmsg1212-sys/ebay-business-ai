"""mag_assignment_plan.json の product_id 重複を洗い出す (read-only)。

1 product_id が複数 ebay_item_id にマップされ band/day が食い違うケースを検出。
PUT /products/{id} は product 単位なので重複は最後勝ち = 矛盾の温床。
"""
import json
from collections import Counter

plan = json.load(open("data/mag_assignment_plan.json", encoding="utf-8"))["plan"]
c = Counter(str(x["product_id"]) for x in plan)
dups = {pid: n for pid, n in c.items() if n > 1}
print(f"plan rows={len(plan)} / unique product_id={len(c)} / 重複 product_id={len(dups)}")

conflict = 0
for pid in dups:
    rows = [x for x in plan if str(x["product_id"]) == pid]
    titles = {r["target_title"] for r in rows}
    flag = "  <-- band/day CONFLICT" if len(titles) > 1 else ""
    if len(titles) > 1:
        conflict += 1
    print(f"\nproduct_id={pid} ({len(rows)}件){flag}")
    for r in rows:
        print(f"  item={r['ebay_item_id']} w={r['weight_g']}g band={r['band']} "
              f"dispatch={r['dispatch']} -> {r['target_title']}")
print(f"\n=== 重複 {len(dups)} 件中 band/day 矛盾 = {conflict} 件 ===")
