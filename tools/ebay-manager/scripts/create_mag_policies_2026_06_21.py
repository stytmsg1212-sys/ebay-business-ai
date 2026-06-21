"""MAG_* 配送ポリシー 安全一括作成 (Codex レビュー反映、money-direct)。

10重量帯 × 2発送日数(7day=7 / 1day=1) = 20 ポリシー MAG_{band}_{disp} を作成。
作成のみ (割当・値設定は後続)。eBaymag consolidation 事故 (2026-06-21) の再発防止:
  - 1個ずつ作成 → 毎回 totalCount==prev+1 AND 既存ポリシー消失=0 を検証、不一致で即 abort
  - 作成前後で全ポリシー snapshot
  - 既存同名はスキップ (冪等)

使い方:
  python -m scripts.create_mag_policies_2026_06_21 --pair-test   # 7day/1dayペア2個だけ先行実証
  python -m scripts.create_mag_policies_2026_06_21 --all         # 残り全20まで作成
  python -m scripts.create_mag_policies_2026_06_21 --dry-run     # 計画表示のみ
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
from monitor.ebaymag_graphql import (  # noqa: E402
    list_profiles, read_profile, gql, SAVE_MUTATION,
)

BANDS = ["0-0.5kg", "0.5-1kg", "1-2kg", "2-3kg", "3-4kg",
         "4-5kg", "5-6kg", "6-8kg", "8-10kg", "10-20kg"]
DISP = {"7day": 7, "1day": 1}
# eBaymag は「内容が重複するポリシー」の作成を拒否する (2026-06-21 実証)。
# 空ポリシーは中身が title 以外同一になるため、Worldwide tariff の timeMax (配送日数
# 目安、cosmetic・送料額に無影響) を帯別 unique にして distinct 化する。
# 既存作成分と整合: 2-3kg=3 (既作), 0-0.5kg=5 (既作) を踏襲。同 dispatch 群内で全 unique。
BAND_TIMEMAX = {
    "0-0.5kg": 5, "0.5-1kg": 4, "1-2kg": 6, "2-3kg": 3, "3-4kg": 7,
    "4-5kg": 8, "5-6kg": 9, "6-8kg": 10, "8-10kg": 11, "10-20kg": 12,
}
_SNAP_DIR = _ROOT / "data" / "ebaymag_policy_snapshots"


def target_titles() -> list[tuple[str, int, str]]:
    """[(title, dispatchTime, band), ...] 20 件。"""
    out = []
    for d, dt in DISP.items():
        for band in BANDS:
            out.append((f"MAG_{band}_{d}", dt, band))
    return out


def snapshot(pg) -> dict:
    profs = list_profiles(pg)
    return {"total": len(profs),
            "by_id": {n["id"]: n["title"] for n in profs}}


def create_one(pg, title: str, dispatch_time: int, template: dict, time_max: int) -> str:
    inp = {"profile": {
        "title": title, "color": 0, "dispatchTime": dispatch_time,
        "returnsWithin": template.get("returnsWithin") or 60,
        "returnsPaidByBuyer": template.get("returnsPaidByBuyer") or False,
        "excludedCountries": [],
        "country": template.get("country"), "city": template.get("city"),
        "postalCode": template.get("postalCode"),
        "tariffs": [{"locations": ["Worldwide"], "timeMax": time_max, "prices": []}],
        "ebayProfiles": [],
    }}
    res = gql(pg, "ShippingProfileSave", SAVE_MUTATION, {"input": inp})
    up = res.get("upsertProfile") or {}
    if not up.get("success"):
        raise RuntimeError(f"create {title} failed: {up.get('errors')}")
    return (up.get("profile") or {}).get("id")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--pair-test", action="store_true",
                   help="MAG_2-3kg_7day/1day ペアのみ (merge しないか実証)")
    g.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)

    targets = target_titles()
    if args.dry_run:
        print(f"作成対象 {len(targets)} 件:")
        for t, dt in targets:
            print(f"  {t} (dispatchTime={dt})")
        return 0

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        pg = b.contexts[0].pages[0]
        pg.bring_to_front()
        pg.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(3000)

        snap0 = snapshot(pg)
        _SNAP_DIR.mkdir(parents=True, exist_ok=True)
        (_SNAP_DIR / "create_mag_before.json").write_text(
            json.dumps(snap0, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"作成前 totalCount={snap0['total']}")

        # テンプレ (country/city/postalCode 流用元)
        src_id = next((i for i, t in snap0["by_id"].items() if "DDP_1-2kg" in t), None)
        if not src_id:
            print("FAIL: テンプレ元 DDP_1-2kg が無い"); return 1
        template = read_profile(pg, src_id)

        if args.pair_test:
            targets = [("MAG_2-3kg_7day", 7, "2-3kg"), ("MAG_2-3kg_1day", 1, "2-3kg")]

        existing_titles = set(snap0["by_id"].values())
        prev = snap0["total"]
        prev_ids = set(snap0["by_id"])
        created, skipped = [], []
        for title, dt, band in targets:
            if any(title in t for t in existing_titles):
                skipped.append(title)
                print(f"  skip (既存): {title}")
                continue
            new_id = create_one(pg, title, dt, template, BAND_TIMEMAX[band])
            pg.wait_for_timeout(1500)
            cur = snapshot(pg)
            vanished = prev_ids - set(cur["by_id"])
            if cur["total"] != prev + 1 or vanished:
                print(f"\n🚨 ABORT: {title} 作成後 異常 "
                      f"(total {prev}→{cur['total']} 期待{prev+1}, 消失={vanished})")
                (_SNAP_DIR / "create_mag_abort.json").write_text(
                    json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
                return 1
            created.append((title, new_id))
            existing_titles.add(title)
            prev = cur["total"]
            prev_ids = set(cur["by_id"])
            print(f"  ✓ created {title} id={new_id} (total={cur['total']})")

        snap1 = snapshot(pg)
        (_SNAP_DIR / "create_mag_after.json").write_text(
            json.dumps(snap1, ensure_ascii=False, indent=1), encoding="utf-8")
        mag_now = [t for t in snap1["by_id"].values() if t.startswith("MAG_")]
        print(f"\n=== 完了 ===")
        print(f"作成 {len(created)} / skip {len(skipped)} / 現在 MAG_ 総数 {len(mag_now)}")
        print(f"totalCount {snap0['total']} → {snap1['total']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
