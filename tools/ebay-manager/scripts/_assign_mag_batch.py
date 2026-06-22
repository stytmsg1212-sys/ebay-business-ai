"""MAG 割当バッチ (money-direct、段階実行対応)。

data/mag_assignment_plan.json の plan を順に assign_product で割当。
- 既に target policy の商品は skip (無駄 PUT 回避)
- assign_product 内蔵の read-back + assert_no_vanish で各回検証
- 失敗 (AssignError 等) で即停止 (Q0、money-direct)
- 開始/終了で全 policy snapshot を保存 (監査・rollback 参照)

引数: 件数 (例 "10") または "all"。デフォルト 10。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright

from monitor import ebaymag_assign as A
from monitor import ebaymag_graphql as G

PQ = "query ProductShip($id: ID!){ product(id:$id){ id shippingProfileId } }"

arg = sys.argv[1] if len(sys.argv) > 1 else "10"
plan = json.load(open("data/mag_assignment_plan.json", encoding="utf-8"))["plan"]
batch = plan if arg == "all" else plan[:int(arg)]

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg

    before = A.snapshot_policies(page)
    tot_b = sum((v["products"] or 0) for v in before.values())
    json.dump(before, open("data/mag_assign_snapshot_before.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"BEFORE: {len(before)} policies, total products={tot_b}", flush=True)
    print(f"batch size: {len(batch)} (arg={arg})", flush=True)

    ok = skip = fail = 0
    for i, x in enumerate(batch):
        pid, tid, title = str(x["product_id"]), x["target_id"], x["target_title"]
        try:
            cur = G.gql(page, "ProductShip", PQ, {"id": pid})
            cur_id = str((cur.get("product") or {}).get("shippingProfileId") or "").split(":")[-1]
            if cur_id == tid:
                skip += 1
                print(f"  [{i+1}/{len(batch)}] {pid} 既に {title} = skip", flush=True)
                continue
            A.assign_product(page, pid, tid)
            ok += 1
            print(f"  [{i+1}/{len(batch)}] {pid} -> {title} OK", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [{i+1}/{len(batch)}] {pid} -> {title} FAIL: {str(e)[:160]}", flush=True)
            print("STOP (money-direct: 失敗で即停止)", flush=True)
            break

    after = A.snapshot_policies(page)
    json.dump(after, open("data/mag_assign_snapshot_after.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    vanished = set(before) - set(after)
    print(f"\nAFTER: {len(after)} policies", flush=True)
    print(f"VANISHED policies: {sorted(vanished) if vanished else 'none (OK)'}", flush=True)
    print(f"=== ok={ok} skip={skip} fail={fail} ===", flush=True)
