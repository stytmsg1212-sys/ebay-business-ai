"""eBaymag 送料ポリシー canonical 生成 one-shot (Phase1 / 値レイヤ)。

漏れ 4 帯 (1-2kg / 2-3kg / 6-8kg / 10-20kg) について
`monitor.ebaymag_policy_mapping.build_canonical_policy` で canonical 値を算出し、

  1. `data/ebaymag_shipping_policies/canonical_{band}.json` に書き出し
  2. DB `ebaymag_shipping_policies` に status='draft' で upsert

する (設計書 §11 Phase1 / §12)。eBaymag / CDP / eBaymag 保存は一切しない。
DB への upsert は値定義の投入であり mutation ではない (DELETE / DROP なし)。

使い方:
    python -m scripts.gen_ebaymag_canonical_policies_2026_06_21 --dry-run
    python -m scripts.gen_ebaymag_canonical_policies_2026_06_21 --apply

dry-run: 算出した 4 帯の値を表示するのみ (ファイル書き出し / DB 書込なし)。
apply  : ファイル書き出し + DB upsert を実行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# tools/ebay-manager/ を import path に追加 (直接実行対応)
_EBAY_MANAGER_ROOT = Path(__file__).resolve().parent.parent
if str(_EBAY_MANAGER_ROOT) not in sys.path:
    sys.path.insert(0, str(_EBAY_MANAGER_ROOT))

from monitor.ebaymag_policy_mapping import build_canonical_policy  # noqa: E402
from monitor.database import get_conn, init_db  # noqa: E402

# 全 10 帯 (2026-06-21 user 指示: 漏れ4帯先行 → 全パターン作成に変更。
# 将来の新規リスティング import 時に全重量帯へ正ポリシーを割当てられるよう完全網羅)。
TARGET_BANDS: list[str] = [
    "0-0.5kg", "0.5-1kg", "1-2kg", "2-3kg", "3-4kg",
    "4-5kg", "5-6kg", "6-8kg", "8-10kg", "10-20kg",
]

_OUTPUT_DIR = _EBAY_MANAGER_ROOT / "data" / "ebaymag_shipping_policies"

# このバッチ実行を識別する run_id (監査用)。
SOURCE_RUN_ID = "gen_canonical_2026_06_21"


def _policy_title(band: str) -> str:
    """canonical ポリシーの表示名 (user が eBaymag で手動作成する際の命名と対応)。"""
    return f"DDP_{band}"


def _upsert_draft(conn, policy: dict) -> str:
    """ebaymag_shipping_policies に status='draft' で upsert する。

    UNIQUE(band, status) を使い、既存 draft 行があれば値を UPDATE、無ければ INSERT。
    ebaymag_policy_token は Phase1 では未確定 (user が eBaymag 手動作成後に backfill)
    のため触らない (既存 token を温存)。

    Returns: 'inserted' | 'updated'
    """
    band = policy["band"]
    # tab_values (eBaymag タブ別 USD) を site_values_json に格納 (DB schema 流用)。
    # region_values_json には Worldwide catch-all の扱い (無料維持) を記録。
    site_values_json = json.dumps(policy["tab_values"], ensure_ascii=False)
    region_values_json = json.dumps(
        {"worldwide_free": policy["worldwide_free"]}, ensure_ascii=False
    )
    excluded_json = json.dumps(policy["excluded_countries"], ensure_ascii=False)
    title = _policy_title(band)

    cur = conn.execute(
        """
        UPDATE ebaymag_shipping_policies
           SET policy_title = ?,
               site_values_json = ?,
               region_values_json = ?,
               excluded_countries_json = ?,
               source_run_id = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE band = ? AND status = 'draft'
        """,
        (title, site_values_json, region_values_json, excluded_json,
         SOURCE_RUN_ID, band),
    )
    if cur.rowcount and cur.rowcount > 0:
        return "updated"

    conn.execute(
        """
        INSERT INTO ebaymag_shipping_policies
            (band, policy_title, ebaymag_policy_token,
             site_values_json, region_values_json, excluded_countries_json,
             source_run_id, status)
        VALUES (?, ?, NULL, ?, ?, ?, ?, 'draft')
        """,
        (band, title, site_values_json, region_values_json, excluded_json,
         SOURCE_RUN_ID),
    )
    return "inserted"


def _print_policy(policy: dict) -> None:
    band = policy["band"]
    print(f"\n=== {band} (title={_policy_title(band)}) ===")
    print(f"  tab_values    : {policy['tab_values']}")
    print(f"  worldwide_free: {policy['worldwide_free']}")
    print(
        f"  excluded      : {len(policy['excluded_countries'])} 国 "
        f"{policy['excluded_countries']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true",
        help="算出値を表示のみ (ファイル / DB 書込なし)",
    )
    group.add_argument(
        "--apply", action="store_true",
        help="canonical_{band}.json 書き出し + DB に draft upsert",
    )
    args = parser.parse_args(argv)

    # 算出 (dry-run / apply 共通)。例外は握り潰さず伝播 (Q0)。
    policies = [build_canonical_policy(band) for band in TARGET_BANDS]

    print(f"[gen canonical] 対象 {len(policies)} 帯: {TARGET_BANDS}")
    for policy in policies:
        _print_policy(policy)

    if args.dry_run:
        print("\n[dry-run] ファイル / DB 書込はスキップ。")
        return 0

    # ---- apply: ファイル書き出し ----
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    for policy in policies:
        band = policy["band"]
        out = {
            "band": band,
            "policy_title": _policy_title(band),
            "tab_values": policy["tab_values"],
            "worldwide_free": policy["worldwide_free"],
            "excluded_countries": policy["excluded_countries"],
            "source_run_id": SOURCE_RUN_ID,
            "generated_at": generated_at,
        }
        path = _OUTPUT_DIR / f"canonical_{band}.json"
        path.write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"[apply] wrote {path}")

    # ---- apply: DB upsert (status='draft') ----
    init_db()  # v79/v80 が未適用なら適用
    with get_conn() as conn:
        for policy in policies:
            action = _upsert_draft(conn, policy)
            print(f"[apply] DB {action}: band={policy['band']} status=draft")

    print("\n[apply] 完了 (canonical draft 投入)。eBaymag / CDP は未操作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
