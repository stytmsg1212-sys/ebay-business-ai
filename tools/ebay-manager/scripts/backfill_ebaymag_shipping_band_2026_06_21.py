"""eBaymag 送料 band 一括 backfill one-shot (W284 Phase3 / canary 用)。

reviewer MED-1 対応。送料ポリシー付替 (assign_policy) は band 変化 (lifecycle フック)
か relist でしか enqueue されないため、**既に $0 漏れしている現存の各国版** は
canary で flag を ON にしても自動では拾われない。本スクリプトが既存 listing の
band を weight から一括設定し、同時に反映キューへ enqueue する (set_ebaymag_
shipping_band_and_enqueue 経由 = band 更新と enqueue を同一トランザクション)。

対象: eBaymag に実インポート済 (ebaymag_products mapping あり) かつ weight_g 設定済。
  - mapping が無い = まだ eBaymag 各国版が無い → 付替対象外 (新規 import 時に
    discover 経路の band 同期が拾う、§8)。
  - weight 未設定 = band 不能 → スキップ (Q0: 黙って最小帯に落とさない)。warning で計上。

識別キーは ebay_item_id (SKU 禁止、sku-rules.md)。eBaymag / CDP は一切操作しない
(DB の band 設定 + enqueue のみ。実付替は flag ON 後の消化タスクが行う)。

使い方:
    python -m scripts.backfill_ebaymag_shipping_band_2026_06_21 --dry-run
    python -m scripts.backfill_ebaymag_shipping_band_2026_06_21 --apply

dry-run: 対象件数 / band 分布 / weight 欠落件数を表示するのみ (DB 書込なし)。
apply  : band 設定 + enqueue を実行 (consumer flag が OFF の間は付替は走らない)。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

_EBAY_MANAGER_ROOT = Path(__file__).resolve().parent.parent
if str(_EBAY_MANAGER_ROOT) not in sys.path:
    sys.path.insert(0, str(_EBAY_MANAGER_ROOT))

from monitor.database import get_conn, init_db, set_ebaymag_shipping_band_and_enqueue  # noqa: E402
from monitor.ebaymag_policy_mapping import band_for_weight_g  # noqa: E402


def _fetch_targets() -> list[dict]:
    """eBaymag 実インポート済 listing (product mapping あり) を取得する。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT l.ebay_item_id   AS eid,
                   l.title          AS title,
                   l.weight_g       AS weight_g,
                   l.ebaymag_shipping_band AS current_band
              FROM ebay_listings l
              JOIN ebaymag_products p ON l.ebay_item_id = p.ebay_item_id
             WHERE COALESCE(l.is_ended, 0) = 0
            """
        ).fetchall()
    return [dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="対象件数 / band 分布を表示のみ (DB 書込なし)")
    group.add_argument("--apply", action="store_true",
                       help="band 設定 + enqueue を実行")
    args = parser.parse_args(argv)

    init_db()
    targets = _fetch_targets()
    print(f"[backfill band] eBaymag 実インポート済 listing: {len(targets)} 件")

    band_dist: Counter[str] = Counter()
    no_weight: list[str] = []
    invalid_weight: list[tuple[str, object]] = []
    planned: list[tuple[str, str, str | None]] = []  # (eid, new_band, current_band)

    for t in targets:
        eid, weight_g, current = t["eid"], t["weight_g"], t["current_band"]
        if weight_g is None:
            no_weight.append(eid)
            continue
        try:
            new_band = band_for_weight_g(weight_g)
        except ValueError:
            invalid_weight.append((eid, weight_g))
            continue
        band_dist[new_band] += 1
        if new_band != current:
            planned.append((eid, new_band, current))

    print(f"  band 算出可: {sum(band_dist.values())} 件 / 分布: {dict(sorted(band_dist.items()))}")
    print(f"  weight 未設定 (band 不能でスキップ): {len(no_weight)} 件")
    if invalid_weight:
        print(f"  ⚠️ weight 異常値 (スキップ): {len(invalid_weight)} 件 {invalid_weight[:5]}")
    print(f"  band 変更が必要 (= enqueue 対象): {len(planned)} 件")

    if args.dry_run:
        print("\n[dry-run] DB 書込はスキップ。enqueue 対象サンプル:")
        for eid, nb, cb in planned[:10]:
            print(f"    eid={eid} {cb!r} → {nb}")
        return 0

    # ---- apply ----
    enqueued = 0
    failed: list[str] = []
    for eid, new_band, _cb in planned:
        try:
            if set_ebaymag_shipping_band_and_enqueue(eid, new_band):
                enqueued += 1
            else:
                failed.append(eid)  # listing 消滅等
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ enqueue 失敗 eid={eid}: {e}")
            failed.append(eid)

    print(f"\n[apply] band 設定 + enqueue 完了: {enqueued} 件")
    if failed:
        print(f"  ⚠️ 失敗 (listing 不在等): {len(failed)} 件 {failed[:10]}")
    print("eBaymag / CDP は未操作。実付替は ebaymag_policy_assign flag ON 後の消化タスクが行う。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
