"""誤適用先 item が プランv2 でどう扱われているか + DB qty を確認 (read-only)."""
import json
import sqlite3
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TARGETS = ["358640917116", "357867054622", "356776795931", "357898016792"]

# 元プラン (全 784 国別) で検索
plan = json.load(open(r"data\ebaymag_publish_groups_2026_06_09.json", encoding="utf-8"))
print("=== 元プラン (2026_06_09) 内の扱い ===")
groups = plan["groups"] if isinstance(plan, dict) and "groups" in plan else plan
found = set()
if isinstance(groups, dict):
    for combo, items in groups.items():
        for it in items:
            iid = str(it.get("item_id", ""))
            if iid in TARGETS:
                print(f"  {iid} | group={combo} | {str(it.get('title',''))[:60]}")
                found.add(iid)
for t in TARGETS:
    if t not in found:
        print(f"  {t} | プラン対象外 (どのグループにも無し)")

print("\n=== DB 現況 ===")
c = sqlite3.connect(r"data\monitor.db")
q = ("SELECT ebay_item_id, COALESCE(is_ended,0), quantity_ebay, title "
     "FROM ebay_listings WHERE ebay_item_id IN (%s)" % ",".join("?" * len(TARGETS)))
for eid, ended, qty, title in c.execute(q, TARGETS):
    print(f"  {eid} | ended={ended} | qty={qty} | {title[:64]}")
