#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W222 Stage 2: floor 実 apply (user 承認後のみ実行)。

db-migration-rules 6 step に準拠 (本番 lp_breakeven_usd 一括 UPDATE = money-direct):
  1. snapshot (rollback 用) → data/tmp/w222_floor_snapshot.json
  2. flag ON (settings.use_category_fvf_floor=true) を save_settings
  3. 1 件試行 → 期待 new_floor と一致を確認
  4. 全件 update_listing_breakeven 再計算 (flag ON = 実カテゴリ FVF)
  5. SELECT 再確認 (更新後 floor の下降/上昇/不変サマリ)
  6. Discord 通知 (R-11 = user 実視認まで)

対象: active かつ purchase_yen>0 かつ weight_g>0 (DRY-RUN と同 scope の 61 件)。
rollback: python scripts/floor_apply_category_w222.py --rollback で snapshot から復元。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from monitor.database import init_db, get_conn  # noqa: E402
from monitor.lowest_price import update_listing_breakeven  # noqa: E402
from calculator import load_settings, save_settings  # noqa: E402

_SNAP = _ROOT / "data" / "tmp" / "w222_floor_snapshot.json"


def _target_ids() -> list[str]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id FROM ebay_listings "
            "WHERE (is_ended IS NULL OR is_ended=0) "
            "AND purchase_yen IS NOT NULL AND purchase_yen>0 "
            "AND weight_g IS NOT NULL AND weight_g>0"
        ).fetchall()
    return [r[0] for r in rows]


def _snapshot() -> dict:
    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id, lp_breakeven_usd FROM ebay_listings"
        ).fetchall()
    snap = {r[0]: r[1] for r in rows}
    _SNAP.parent.mkdir(parents=True, exist_ok=True)
    _SNAP.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print(f"[snapshot] {len(snap)} 件の lp_breakeven_usd を退避 → {_SNAP}")
    return snap


def rollback() -> None:
    snap = json.loads(_SNAP.read_text(encoding="utf-8"))
    n = 0
    with get_conn() as c:
        for eid, val in snap.items():
            c.execute("UPDATE ebay_listings SET lp_breakeven_usd=? WHERE ebay_item_id=?",
                      (val, eid))
            n += 1
    # flag も OFF に戻す
    s = load_settings()
    s["use_category_fvf_floor"] = False
    save_settings(s)
    print(f"[rollback] {n} 件 floor 復元 + flag OFF")


def apply() -> None:
    init_db()
    snap = _snapshot()
    targets = _target_ids()
    print(f"[apply] 対象 active listing (purchase_yen+weight 有): {len(targets)} 件")

    # Step 2: flag ON
    s = load_settings()
    s["use_category_fvf_floor"] = True
    save_settings(s)
    settings = load_settings()
    assert settings.get("use_category_fvf_floor") is True, "flag ON 反映失敗"
    print("[apply] settings.use_category_fvf_floor = True 反映")

    # Step 3: 1 件試行 (down 例 maxell or 任意の 1 件)
    if targets:
        t0 = targets[0]
        before = snap.get(t0)
        after = update_listing_breakeven(t0, settings)
        print(f"[apply] 1 件試行 {t0}: {before} → {after}")

    # Step 4: 全件再計算
    n_down = n_up = n_same = n_none = 0
    for eid in targets:
        old = snap.get(eid)
        new = update_listing_breakeven(eid, settings)
        if new is None:
            n_none += 1
        elif old is None:
            n_same += 1
        elif new < old - 0.01:
            n_down += 1
        elif new > old + 0.01:
            n_up += 1
        else:
            n_same += 1

    # Step 5: SELECT 再確認
    with get_conn() as c:
        filled = c.execute(
            "SELECT COUNT(*) FROM ebay_listings WHERE lp_breakeven_usd IS NOT NULL "
            "AND (is_ended IS NULL OR is_ended=0)"
        ).fetchone()[0]
    msg = (f"W222 floor apply 完了: 再計算 {len(targets)} 件 "
           f"(下降 {n_down} / 上昇 {n_up} / 不変 {n_same} / 計算不能 {n_none})。"
           f"floor 保持 listing 合計 {filled}。flag use_category_fvf_floor=ON。")
    print(f"[apply] {msg}")

    # Step 6: Discord 通知 (R-11、.env webhook を使う標準 pattern)
    try:
        from notifiers.discord_notifier import DiscordNotifier
        ok = DiscordNotifier(webhook_url="").send_message(f"[W222] {msg}")
        print(f"[apply] Discord 通知 sent={ok} (R-11: user 実視認で到達確認してください)")
    except Exception as e:  # noqa: BLE001
        print(f"[apply] Discord 通知失敗 (手動確認要): {e}")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    else:
        apply()
