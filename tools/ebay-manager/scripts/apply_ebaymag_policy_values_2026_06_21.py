"""eBaymag 配送ポリシー各サイト送料 一括設定 orchestrator (1 ポリシー逐次)。

canonical (build_canonical_policy) のタブ値を eBaymag サイト別 cc 値に展開し、
monitor.ebaymag_policy_editor.set_policy_site_values で 1 ポリシーずつ設定する。

タブ→サイト展開:
  US       → com (本体 $0、設定しない)
  Europe   → uk/de/fr/it/es (EU 各サイトに同値)
  Australia→ au
  Canada   → ca

使い方:
  python -m scripts.apply_ebaymag_policy_values_2026_06_21 --policy DDP_6-8kg --dry-run
  python -m scripts.apply_ebaymag_policy_values_2026_06_21 --policy DDP_6-8kg --apply

dry-run: 値入力後 reload 破棄 (保存なし)。apply: 「変更を適用」保存 + read-back 検証。
必ず 1 ポリシーずつ。apply 後は実 ebay.{site} ページ目視を別途行うこと (Q1)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from monitor.ebaymag_policy_mapping import build_canonical_policy  # noqa: E402
from monitor.ebaymag_policy_editor import set_policy_site_values  # noqa: E402

_SNAPSHOT_DIR = _ROOT / "data" / "ebaymag_policy_snapshots"


def _band_from_title(policy_title: str) -> str:
    """DDP_6-8kg → 6-8kg。"""
    if policy_title.startswith("DDP_"):
        return policy_title[len("DDP_"):]
    raise ValueError(f"policy_title から band を導出できない: {policy_title}")


def _tab_to_site_cc(tab_values: dict) -> dict[str, int]:
    """canonical tab_values → eBaymag サイト cc 値。

    Europe を EU 5 サイトへ展開。US(com) は本体課金で設定しない (除外)。
    """
    eu = tab_values["Europe"]
    out = {
        "uk": eu, "de": eu, "fr": eu, "it": eu, "es": eu,
        "au": tab_values["Australia"],
        "ca": tab_values["Canada"],
    }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, help="例: DDP_6-8kg")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    band = _band_from_title(args.policy)
    canonical = build_canonical_policy(band)
    site_cc_values = _tab_to_site_cc(canonical["tab_values"])
    print(f"[orchestrator] policy={args.policy} band={band}")
    print(f"  canonical tab_values: {canonical['tab_values']}")
    print(f"  → site_cc_values: {site_cc_values}")
    print(f"  worldwide_free: {canonical['worldwide_free']} / "
          f"excluded: {len(canonical['excluded_countries'])} 国")

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = ctx.pages[0]
        page.bring_to_front()
        result = set_policy_site_values(
            page, args.policy, site_cc_values,
            dry_run=args.dry_run, snapshot_dir=_SNAPSHOT_DIR,
        )
    print("\n=== result ===")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.apply and not result.get("verified"):
        print("\n⚠️ apply だが verified=False — read-back 検証未達。確認要。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
