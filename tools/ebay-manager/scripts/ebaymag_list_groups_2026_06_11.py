"""フィルタ済み出品グループの一覧 (小→大) を表示."""
import json
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

d = json.load(open(r"data\ebaymag_publish_groups_2026_06_11_filtered.json", encoding="utf-8"))
for k, v in sorted(d["groups"].items(), key=lambda kv: len(kv[1])):
    print(f"{len(v):3d} | {k}")
    if len(v) <= 4:
        for it in v:
            print(f"      - {it['item_id']} | {it['title'][:70]} | qty={it['qty_available']}")
print("---", sum(len(v) for v in d["groups"].values()), "items /", len(d["groups"]), "groups")
