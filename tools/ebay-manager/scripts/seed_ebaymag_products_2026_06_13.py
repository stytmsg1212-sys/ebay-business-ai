# -*- coding: utf-8 -*-
"""ebaymag_products (v75) の seed one-shot (依頼ボード#10 / 2026-06-13).

ソース (6/11 プラン v2 反映の実機ログ、いずれも itm 照合済の実証データ):
  1. data/ebaymag_batch_log_2026_06_11.json  (18 件: item_id + productId)
  2. data/ebaymag_batch_log_2026_06_11b.json (97 件: item_id + productId)
  3. data/ebaymag_audit_2026_06_11.json      (18 件: productId + itm + 国別状態)

順に適用 (後勝ち)。audit は国別状態も保持しているため site_states まで seed。
batch のみの行は mapping だけ seed (状態は UI の「状態取得」で初回キャッシュ)。
既定 dry-run、--apply で書込。冪等 (upsert)。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import get_conn, upsert_ebaymag_product  # noqa: E402
from monitor.ebaymag_driver import DOMAIN_TO_CODE, _label_is_on  # noqa: E402

DATA = BASE / "data"


def collect() -> dict[str, dict]:
    """item_id -> {product_id, site_states|None} (後勝ちマージ)."""
    out: dict[str, dict] = {}
    for name in ("ebaymag_batch_log_2026_06_11.json",
                 "ebaymag_batch_log_2026_06_11b.json"):
        doc = json.loads((DATA / name).read_text(encoding="utf-8"))
        for e in doc.get("results", []):
            item_id, pid = e.get("item_id"), e.get("productId")
            if item_id and pid:
                out[str(item_id)] = {"product_id": str(pid), "site_states": None}

    audit = json.loads((DATA / "ebaymag_audit_2026_06_11.json").read_text(encoding="utf-8"))
    for e in audit:
        item_id, pid = e.get("itm"), e.get("productId")
        if not (item_id and pid):
            continue
        states = {}
        for s in e.get("sites", []):
            code = DOMAIN_TO_CODE.get(s.get("site"))
            if code:
                states[code] = _label_is_on(s.get("state"))
        out[str(item_id)] = {"product_id": str(pid), "site_states": states or None}
    return out


def main(apply: bool) -> None:
    mapping = collect()
    with get_conn() as conn:
        known = {r[0] for r in conn.execute("SELECT ebay_item_id FROM ebay_listings")}
    in_db = {k: v for k, v in mapping.items() if k in known}
    orphan = sorted(set(mapping) - set(in_db))
    with_states = sum(1 for v in in_db.values() if v["site_states"])
    print(f"収集: {len(mapping)} 件 / ebay_listings 在籍: {len(in_db)} 件 "
          f"(うち状態付き {with_states}) / 不在 listing: {len(orphan)} 件")
    if orphan:
        print("  不在 (seed 対象外):", ", ".join(orphan[:10]),
              "..." if len(orphan) > 10 else "")
    if not apply:
        print("dry-run: 書込なし。--apply で実行")
        return

    # Q2: 1 件試行 → 残り → SELECT 検証
    items = sorted(in_db.items())
    first_id, first = items[0]
    upsert_ebaymag_product(first_id, first["product_id"], first["site_states"])
    with get_conn() as conn:
        row = conn.execute(
            "SELECT product_id FROM ebaymag_products WHERE ebay_item_id=?",
            (first_id,)).fetchone()
    assert row and row[0] == first["product_id"], f"試行失敗: {first_id}"
    print(f"試行 OK: {first_id} -> productId={first['product_id']}")

    for item_id, v in items[1:]:
        upsert_ebaymag_product(item_id, v["product_id"], v["site_states"])

    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(1) FROM ebaymag_products").fetchone()[0]
        n_states = conn.execute(
            "SELECT COUNT(1) FROM ebaymag_products WHERE site_states_json IS NOT NULL"
        ).fetchone()[0]
    print(f"検証: ebaymag_products={n} 件 (状態付き {n_states}) — "
          f"{'OK' if n == len(in_db) else 'NG: 期待 ' + str(len(in_db))}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
