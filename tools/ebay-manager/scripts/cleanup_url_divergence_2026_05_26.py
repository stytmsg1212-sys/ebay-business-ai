"""
W139-revisit 緊急 cleanup (2026-05-26): url_divergence 18 件の SKU を実 source_url に揃える.

背景: 5/26 朝の verify で SKU 派生 URL != ebay_listings.source_url の listing が 19 件あり、
うち 18 件が間違った URL を monitored_items で監視中 (money-direct risk = 実仕入先 OOS 見逃し).

修正方針: SKU を実 source_url に合わせて更新 (`update_ebay_listing_sku` 利用).
これで _sync_monitored_items_sku が cascade して monitored_items も同期される.

実行モード:
  python scripts/cleanup_url_divergence_2026_05_26.py           # dry-run (default)
  python scripts/cleanup_url_divergence_2026_05_26.py --apply   # 実 UPDATE

Q2 6-step:
  1. snapshot SELECT (CSV 保存)
  2. 1 件試行 → SELECT 確認
  3. 残り 17 件
  4. SELECT 再確認
  5. 24h 以内 retrospective code-reviewer
  6. HIGH 指摘 → 補正/rollback

⚠️ 再実行注意 (code-reviewer HIGH-2 2026-05-26):
  本 script は 2026-05-26 一度実行済 (19 件 cleanup 完了). 再実行する場合、
  本 file の SKU_TO_URL_PATTERNS / URL_TO_PREFIX_PATTERNS は **sku_mapping_manager.py
  の DEFAULT_MAPPINGS (data/sku_mappings.json で上書きされ得る) と二重管理**.
  user が `data/sku_mappings.json` を編集 (例: ebayRT_ の base url 変更) した場合、
  本 script の hardcode と drift して **listing.source_url を本来でない URL に
  書き換える money-direct risk** が発生する.
  再実行前に必ず `sku_mapping_manager.load_mappings()` の出力と本 file の
  SKU_TO_URL_PATTERNS を diff で照合すること.
"""
import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# project root を path に追加
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from monitor.database import get_conn, update_ebay_listing_sku  # noqa: E402

SKU_TO_URL_PATTERNS = {
    "ebayme_": lambda s: f"https://jp.mercari.com/item/m{s}",
    "ebayMS_": lambda s: f"https://jp.mercari.com/shops/product/{s}",
    "ebayrm_": lambda s: f"https://item.fril.jp/{s}",
    "ebayPF_": lambda s: f"https://paypayfleamarket.yahoo.co.jp/item/{s}",
    "ebayyh_": lambda s: f"https://page.auctions.yahoo.co.jp/jp/auction/{s}",
    "ebayRT_": lambda s: f"https://item.rakuten.co.jp/{s}/",
    "ebayRB_": lambda s: f"https://books.rakuten.co.jp/rb/{s}",
    "ebayAM_": lambda s: f"https://www.amazon.co.jp/dp/{s}",
}

URL_TO_PREFIX_PATTERNS = [
    ("ebayme_", r"mercari\.com/item/m([A-Za-z0-9]+)"),
    ("ebayMS_", r"mercari\.com/shops/product/([A-Za-z0-9]+)"),
    ("ebayrm_", r"item\.fril\.jp/([A-Za-z0-9]+)"),
    ("ebayPF_", r"paypayfleamarket\.yahoo\.co\.jp/item/([A-Za-z0-9]+)"),
    ("ebayyh_", r"auctions\.yahoo\.co\.jp/jp/auction/([A-Za-z0-9]+)"),
    ("ebayRT_", r"item\.rakuten\.co\.jp/([^/]+)/"),
    ("ebayRB_", r"books\.rakuten\.co\.jp/rb/([A-Za-z0-9]+)"),
    ("ebayAM_", r"amazon\.co\.jp/(?:[^/]+/)?dp/([A-Z0-9]+)"),
]


def derive_url_from_sku(sku: str) -> str | None:
    for prefix, builder in SKU_TO_URL_PATTERNS.items():
        if sku.startswith(prefix):
            return builder(sku[len(prefix) :])
    return None


def derive_sku_from_url(url: str) -> str | None:
    import re

    for prefix, pat in URL_TO_PREFIX_PATTERNS:
        m = re.search(pat, url)
        if m:
            return prefix + m.group(1)
    return None


def find_divergent_listings(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT ebay_item_id, sku, title, source_url
           FROM ebay_listings
           WHERE COALESCE(is_ended,0)=0
             AND (quantity_ebay IS NULL OR quantity_ebay >= 1)
             AND sku LIKE 'ebay%'
             AND source_url IS NOT NULL AND source_url != ''"""
    ).fetchall()

    divergent = []
    for r in rows:
        eid, sku, title, source_url = r["ebay_item_id"], r["sku"], r["title"], r["source_url"]
        derived = derive_url_from_sku(sku)
        if derived and derived != source_url:
            new_sku = derive_sku_from_url(source_url)
            divergent.append(
                {
                    "ebay_item_id": eid,
                    "old_sku": sku,
                    "new_sku": new_sku,
                    "title": title,
                    "listing_source_url": source_url,
                    "derived_from_old_sku": derived,
                    "derived_from_new_sku": derive_url_from_sku(new_sku) if new_sku else None,
                }
            )
    return divergent


def snapshot(divergent: list[dict]) -> Path:
    """Q2 Step 1: snapshot を CSV 保存."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    snapshot_path = PROJECT_ROOT / "data" / "tmp" / f"w139_revisit_cleanup_snapshot_{ts}.csv"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    # ebay_listings + monitored_items の元状態を CSV 保存
    with get_conn() as conn:
        eids = [d["ebay_item_id"] for d in divergent]
        placeholders = ",".join("?" * len(eids))

        ebay_rows = conn.execute(
            f"""SELECT ebay_item_id, sku, source_url, source_status,
                       source_last_checked, source_out_of_stock_since,
                       risk_confirmed, last_synced_at
               FROM ebay_listings WHERE ebay_item_id IN ({placeholders})""",
            eids,
        ).fetchall()

        monitored_rows = conn.execute(
            f"""SELECT id, ebay_item_id, sku, source_url, site_config_id,
                       is_active, last_status, last_check
               FROM monitored_items
               WHERE ebay_item_id IN ({placeholders})""",
            eids,
        ).fetchall()

    with snapshot_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# divergent_summary"])
        w.writerow(
            [
                "ebay_item_id",
                "old_sku",
                "new_sku",
                "title",
                "listing_source_url",
                "derived_from_old_sku",
                "derived_from_new_sku",
            ]
        )
        for d in divergent:
            w.writerow(
                [
                    d["ebay_item_id"],
                    d["old_sku"],
                    d["new_sku"],
                    (d["title"] or "")[:80],
                    d["listing_source_url"],
                    d["derived_from_old_sku"],
                    d["derived_from_new_sku"],
                ]
            )
        w.writerow([])
        w.writerow(["# ebay_listings_state_before"])
        if ebay_rows:
            w.writerow(list(ebay_rows[0].keys()))
            for r in ebay_rows:
                w.writerow([r[k] for k in r.keys()])
        w.writerow([])
        w.writerow(["# monitored_items_state_before"])
        if monitored_rows:
            w.writerow(list(monitored_rows[0].keys()))
            for r in monitored_rows:
                w.writerow([r[k] for k in r.keys()])

    return snapshot_path


def apply_one(d: dict) -> dict:
    """Q2 Step 2/3: 1 件 update."""
    if not d["new_sku"]:
        return {"ebay_item_id": d["ebay_item_id"], "skipped": True, "reason": "new_sku derive failed"}
    update_ebay_listing_sku(d["ebay_item_id"], d["new_sku"])
    # Verify
    with get_conn() as conn:
        e = conn.execute(
            "SELECT sku, source_url FROM ebay_listings WHERE ebay_item_id = ?",
            (d["ebay_item_id"],),
        ).fetchone()
        m = conn.execute(
            "SELECT id, sku, source_url FROM monitored_items WHERE ebay_item_id = ?",
            (d["ebay_item_id"],),
        ).fetchone()
    return {
        "ebay_item_id": d["ebay_item_id"],
        "skipped": False,
        "ebay_listings.sku": e["sku"] if e else None,
        "ebay_listings.source_url": e["source_url"] if e else None,
        "monitored.id": m["id"] if m else None,
        "monitored.sku": m["sku"] if m else None,
        "monitored.source_url": m["source_url"] if m else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="実行 (省略時 dry-run)")
    parser.add_argument("--first-only", action="store_true", help="Q2 Step 2: 1 件のみ試行")
    args = parser.parse_args()

    print("=" * 80)
    print(f"W139-revisit url_divergence cleanup ({datetime.now().isoformat()})")
    print("=" * 80)

    with get_conn() as conn:
        divergent = find_divergent_listings(conn)

    print(f"\n対象 listing: {len(divergent)} 件")
    print()
    for i, d in enumerate(divergent, 1):
        print(f"  [{i}] eid={d['ebay_item_id']} title={(d['title'] or '')[:50]}")
        print(f"      old_sku: {d['old_sku']}")
        print(f"      new_sku: {d['new_sku']}")
        print(f"      listing.source_url: {d['listing_source_url']}")
        if d["new_sku"] is None:
            print(f"      ⚠️ new_sku derive 失敗 (URL pattern 未対応)")
    print()

    if not args.apply:
        print("DRY-RUN モード (--apply で実行).")
        snapshot_path = snapshot(divergent)
        print(f"snapshot 保存: {snapshot_path}")
        return

    # snapshot
    snapshot_path = snapshot(divergent)
    print(f"\nQ2 Step 1: snapshot 保存 → {snapshot_path}")

    # Step 2: 1 件試行
    targets = divergent
    if args.first_only:
        targets = divergent[:1]
        print(f"\nQ2 Step 2: 1 件試行 (eid={targets[0]['ebay_item_id']})")
    else:
        print(f"\nQ2 Step 3: 全 {len(targets)} 件 update 実行")

    results = []
    for d in targets:
        try:
            r = apply_one(d)
            results.append(r)
            status = "SKIP" if r["skipped"] else "OK"
            print(f"  [{status}] eid={r['ebay_item_id']}")
            if not r["skipped"]:
                print(f"         ebay_listings.sku → {r['ebay_listings.sku']}")
                print(f"         monitored.sku     → {r['monitored.sku']}")
                print(f"         monitored.url     → {r['monitored.source_url']}")
        except Exception as e:
            print(f"  [FAIL] eid={d['ebay_item_id']}: {e}")
            results.append({"ebay_item_id": d["ebay_item_id"], "error": str(e)})

    # Step 4: SELECT 再確認
    print(f"\nQ2 Step 4: SELECT 再確認")
    with get_conn() as conn:
        divergent_after = find_divergent_listings(conn)
    print(f"  url_divergence 残: {len(divergent_after)} 件 (期待: 0 if --apply 全件)")
    for d in divergent_after:
        print(
            f"    残 eid={d['ebay_item_id']} sku={d['old_sku']} url={d['listing_source_url']}"
        )

    print("\nQ2 Step 5/6 (24h retrospective code-reviewer / 補正) は session 内で実施.")


if __name__ == "__main__":
    main()
