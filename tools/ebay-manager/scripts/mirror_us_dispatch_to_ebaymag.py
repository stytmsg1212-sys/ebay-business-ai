"""US本体listingのDispatchTimeMaxをeBaymag各国版MAG割当へミラーする CLI (money-direct)。

設計書: .company/engineering/docs/2026-06-26-ebaymag-us-dispatch-mirror-design.md

使い方:
  python scripts/mirror_us_dispatch_to_ebaymag.py                 # dry-run (既定、変更なし)
  python scripts/mirror_us_dispatch_to_ebaymag.py --apply         # 値完備twinへの付替を実行
  python scripts/mirror_us_dispatch_to_ebaymag.py --force-product 718746868
                                                                  # 単品break-glass(値未完備twinへ)

前提: CDP Chrome (port 9222) に eBaymag ログイン済。/shipping で CSRF 更新 (/products goto 禁止)。
"""
import argparse
import os
import sys

sys.path.insert(0, '.')
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

from monitor import ebaymag_dispatch_mirror as M
from monitor import ebaymag_graphql as G
from monitor.inventory_sync import _get_credentials


def _find_ebaymag_page(browser):
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                return pg
    return None


def _notify_force(records):
    """force適用のleakリスクを Discord(SYSTEM) に痕跡として残す (best-effort, Q0)。"""
    url = os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("   (Discord webhook 未設定 — stdout ログのみ)")
        return
    try:
        from monitor.notifier import _send_webhook
        lines = [f"- {r['title']} ({r['product_id']}) → {r['to_policy']} "
                 f"[未完備: {r.get('hold_reason')}]" for r in records]
        _send_webhook(url, {"embeds": [{
            "title": "⚠️ eBaymag dispatch mirror --force-product 適用 (送料未完備twinへ移動)",
            "description": "\n".join(lines)[:1900],
            "color": 0xFFAA00,
        }]})
        print("   Discord(SYSTEM) へ force 痕跡を通知")
    except Exception as e:  # noqa: BLE001 — 通知失敗で本処理は止めない (stdoutに残る)
        print(f"   Discord通知失敗 (stdoutログは残る): {str(e)[:100]}")


def _print_plan(plan):
    print(f"\n=== plan: 移動{len(plan['moves'])} / 保留{len(plan['holds'])} / "
          f"skip{len(plan['skips'])} / SKU矛盾{len(plan['sku_conflicts'])} ===")
    if plan["moves"]:
        print("\n--- 移動 (値完備twin、apply対象) ---")
        for m in plan["moves"]:
            print(f"  {m['from_title']} → MAG_{m['band']}_{m['to_series']}  "
                  f"product={m['product_id']} item={m['us_item']}")
    if plan["holds"]:
        print("\n--- 保留 (twin値未完備=leak回避、--force-product で個別実行可) ---")
        for h in plan["holds"]:
            print(f"  {h['from_title']} → {h['to_series']}  product={h['product_id']} "
                  f"理由: {h['hold_reason']}")
    if plan["sku_conflicts"]:
        print("\n--- SKU矛盾 (US手編集 vs 在庫ルール、ミラーはUS通り実行・通知のみ) ---")
        for c in plan["sku_conflicts"]:
            print(f"  product={c['product_id']} US={c['us_series']} "
                  f"SKUルール={c['sku_rule']} sku={c['sku']} ({c['title']})")
    if plan["skips"]:
        print("\n--- skip (US dispatch取得不能、要確認) ---")
        for s in plan["skips"]:
            print(f"  {s.get('title')} product={s['product_id']} 理由: {s['reason']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="値完備twinへの付替を実行")
    ap.add_argument("--force-product", dest="force_product", default=None,
                    help="単品break-glass: 値未完備twinへの移動を1商品だけ許可")
    args = ap.parse_args()

    # C: mutation 経路 (--apply / --force-product) は navigation の前に lock を取得し、
    #    navigation → plan → apply → verify の全体を 1 lock で包む。
    #    dry-run は mutation しないため lock 不要 (navigate するが他 mutator と衝突し得る点に注意)。
    # B: lock timeout=300s (apply と同等)。
    is_mutation = args.apply or bool(args.force_product)
    from contextlib import nullcontext
    from monitor.cdp_lock import acquire as _cdp_lock_acquire, LockBusy as _LockBusy
    lock_cm = _cdp_lock_acquire(blocking=True, timeout=300) if is_mutation else nullcontext()

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        page = _find_ebaymag_page(b)
        if page is None:
            print("FAIL: eBaymag タブが見つからない (CDP 9222)")
            sys.exit(1)

        try:
            with lock_cm:
                # CSRF 更新 (/shipping は安全、/products は goto 禁止)
                page.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded",
                          timeout=40000)
                page.wait_for_timeout(2500)

                creds = _get_credentials()
                if creds is None or not all(creds):
                    print("FAIL: eBay認証情報が取得できない (_get_credentials)")
                    sys.exit(1)
                fx = G.get_fx(page)
                profs = G.list_profiles(page, first=200)
                twin_index = M.build_twin_index(profs)
                # dispatch軸 整合 (Codex H1): series ラベルと dispatchTime 実値の乖離を起動時検出
                M.assert_dispatch_axis(page, twin_index)
                print(f"twin_index: MAG {len(twin_index)}件 / dispatch軸 整合OK / fx取得OK")

                print("\nUS dispatch 読取中 (全MAG商品 GetItem、~1分)...")
                plan = M.plan_mirror(page, creds, fx, twin_index)
                _print_plan(plan)

                if not args.apply and not args.force_product:
                    print("\n[dry-run] 変更なし。実行は --apply / --force-product <id>")
                    return

                if args.apply:
                    print("\n[apply] 値完備twinへの付替を実行...")
                    res = M.apply_moves(page, plan["moves"])
                    print(f"  done={len(res['done'])}")
                    if res["failed"]:
                        print(f"  FAIL: {res['failed']}\n  STOP (money-direct)")
                        sys.exit(1)

                if args.force_product:
                    target = [h for h in plan["holds"]
                              if h["product_id"] == str(args.force_product)]
                    if not target:
                        print(f"\n[force] product {args.force_product} は保留リストに無い "
                              "(既に一致 or 値完備で通常apply対象 or skip)")
                    else:
                        print(f"\n⚠️ [force] product {args.force_product} を値未完備twinへ移動 "
                              "(各国版で送料漏れ露出リスク):")
                        for h in target:
                            print(f"   {h['from_title']} → {h['to_policy']} "
                                  f"未完備: {h['hold_reason']}")
                        _notify_force(target)
                        res = M.apply_moves(page, target)
                        print(f"  done={len(res['done'])}")
                        if res["failed"]:
                            print(f"  FAIL: {res['failed']}\n  STOP (money-direct)")
                            sys.exit(1)

                # 冪等性検証 (Codex M2): 再planで move=0 に収束したか
                print("\n[verify] 冪等性チェック (再plan、~1分)...")
                plan2 = M.plan_mirror(page, creds, fx, twin_index)
                applied_ids = {m["product_id"] for m in plan["moves"]}
                if args.force_product:
                    applied_ids.add(str(args.force_product))
                residual = [m for m in plan2["moves"] if m["product_id"] in applied_ids]
                residual += [h for h in plan2["holds"] if h["product_id"] in applied_ids]
                if residual:
                    print(f"  ⚠️ 適用したはずの {len(residual)}件 がまだdrift (要調査): "
                          f"{[r['product_id'] for r in residual]}")
                else:
                    print(f"  ✅ 適用分は drift=0 に収束 (残 move={len(plan2['moves'])} "
                          f"hold={len(plan2['holds'])} は未適用分)")
        except _LockBusy as _lbe:
            print(f"FAIL: CDP lock timeout ({_lbe}) — 別の eBaymag 操作と競合中")
            sys.exit(1)


if __name__ == "__main__":
    main()
