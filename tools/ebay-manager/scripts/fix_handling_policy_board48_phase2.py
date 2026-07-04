#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード #48 Phase 2: 残 4 件 (DDP override 保持型) one-shot (money-direct T3).

Phase 1 (`fix_handling_policy_board48.py`) は BP 単独差替 (BP-only revise) で
per-listing の ShippingServiceCostOverrideList (DDP 関税 buffer, price*0.20 由来)
を送出しない。override 有 listing に BP-only revise を掛けると override が BP
default にリセットされる = **DDP buffer 喪失 = Section 232 数百ドル/件リスク**。

本 Phase 2 は **combined revise** (BP 差替 + 現行 override 再送) で override を
保持したまま BP を no-stock (7day) へ差し替える。使う関数は既存の
`revise_fixed_price_with_shipping` (W142 の combined 経路と同じ、
`force_seller_profiles=True` + 現行 ship_cost/+each の再送)。

対象: Phase 1 完了後の残 4 件 (2026-07-04 時点):
  - 356700630309 (Pioneer CD-9)      現送料 $0.00  override
  - 357835516768 (Heavy Unit MD)      現送料 $20.00 override
  - 358052065626 (Google Pixel Dock)  現送料 $52.00 override
  - 358351923331 (KEYENCE FD-Q20C)    現送料 $35.00 override

特異ケース:
  356700630309 は現行 override が $0.00 (送料無料設定) となっており、これを
  そのまま維持する = 実行後も送料 $0.00 のまま (user 判断で別途正常値へ戻す
  可能性はあるが、本 script は「override 現状維持」だけを行い、値の妥当性
  判断は user に委ねる)。

前提:
  - Phase 1 で 358132396521 が既に是正済 (残 4 件が対象)。
  - 現行 override 値は GetItem snapshot (実 eBay) から取得する (DB 由来値は
    使わない、W137 真実源)。
  - 是正後 BP の domestic sortOrder = 1 (Sell Account API 事前確認済で
    全 3 target BP が single-domestic priority=1)。

使い方:
  python scripts/fix_handling_policy_board48_phase2.py            # dry-run (既定)
  python scripts/fix_handling_policy_board48_phase2.py --execute  # 実反映
  python scripts/fix_handling_policy_board48_phase2.py --only <eid>  # 単一絞込

出力:
  data/board48/phase2_dry_run_<ts>.json           (dry-run 結果)
  data/board48/phase2_execute_snapshot_<ts>.json  (--execute 反映前 snapshot)
  data/board48/phase2_execute_result_<ts>.json    (--execute 成否別)
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.database import get_conn  # noqa: E402
from monitor.shipping_policy_selector import load_settings_policies  # noqa: E402
from ui_cache import bump_db_version  # noqa: E402

# Phase 1 の抽出/裏取り関数を再利用 (K2 Surgical: DRY、Phase 2 独自の抽出は書かない)
from scripts.fix_handling_policy_board48 import (  # noqa: E402
    ABORT_THRESHOLD,
    EXPECTED_COUNT,
    _fetch_weight_audit,
    _try_fetch_policy_names,
    fetch_bp_domestic_defaults,
    find_candidates,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("board48_phase2_combined_revise")

_OUT_DIR = _ROOT / "data" / "board48"
_SLEEP_BASE = 1.5


# =============================================================================
# snapshot 収集 (Phase 2 は override 値の裏取りが核心)
# =============================================================================

def enrich_with_snapshot_and_priority(
    candidates: list[dict], app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> list[dict]:
    """各 listing の GetItem snapshot + 是正後 BP の domestic sortOrder を row に付与.

    snapshot が返す ship_cost_usd / ship_additional_usd は本 script の
    「combined revise で再送する override 値」の正源。DB 値は使わない
    (W137 真実源、DB stale は combined 経路で override 誤再送 = 事故)。
    """
    from monitor.ebay_account_policy import fetch_shipping_policies, resolve_domestic_priority
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    # 是正後 BP の resolve_domestic_priority を先に全件解決 (単一呼出)
    pol_list = fetch_shipping_policies({})
    if not pol_list.ok:
        raise RuntimeError(f"Sell Account API 失敗: {pol_list.error}")
    priority_by_bp: dict[str, tuple[Optional[int], str]] = {}
    for bp_id in {c["correct_policy_id"] for c in candidates}:
        pol = next((p for p in pol_list.policies if p.policy_id == bp_id), None)
        if pol is None:
            priority_by_bp[bp_id] = (None, "policy-not-found")
        else:
            priority_by_bp[bp_id] = resolve_domestic_priority(pol)

    out: list[dict] = []
    n = len(candidates)
    for i, c in enumerate(candidates, 1):
        snap = fetch_listing_snapshot(
            c["ebay_item_id"], app_id, dev_id, cert_id, user_token
        )
        prio, prio_reason = priority_by_bp.get(c["correct_policy_id"], (None, "?"))
        row = dict(c)
        row["live_ok"] = snap.ok
        row["live_error"] = snap.error
        row["live_shipping_profile_id"] = snap.shipping_profile_id
        row["live_payment_profile_id"] = snap.payment_profile_id
        row["live_return_profile_id"] = snap.return_profile_id
        row["ship_cost_usd"] = snap.ship_cost_usd
        row["ship_additional_usd"] = snap.ship_additional_usd
        row["ship_override_present"] = snap.ship_override_present
        row["ship_override_priority"] = snap.ship_override_priority
        row["target_bp_priority"] = prio
        row["target_bp_priority_reason"] = prio_reason
        row["start_price_usd"] = snap.start_price_usd
        row["db_matches_live"] = bool(
            snap.ok and snap.shipping_profile_id == c["current_policy_id"]
        )
        out.append(row)
        logger.info(
            f"[{i}/{n}] {c['ebay_item_id']} snap.ok={snap.ok} "
            f"ship_cost=${snap.ship_cost_usd} +each=${snap.ship_additional_usd} "
            f"override_present={snap.ship_override_present} "
            f"target_bp_prio={prio} ({prio_reason})"
        )
        if i < n:
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
    return out


# =============================================================================
# combined revise の preflight & plan 純関数 (pytest 対象)
# =============================================================================

def build_combined_revise_plans(enriched: list[dict]) -> list[dict]:
    """各 listing について combined revise の送信 plan を組み立てる (純関数).

    combined revise は BP 差替 + 現行 override 再送を **同一 XML** で行うため
    (revise_fixed_price_with_shipping + force_seller_profiles=True)、
    送信予定の (ship_cost_usd, ship_additional_usd, seller_profiles, ship_priority)
    が dry-run 段階で確定できる。この plan を JSON に書き出せば「実行後の
    買い手向け送料 = plan.ship_cost_usd」がそのまま「override 維持後の実効値」
    として証明できる (BP default にリセットされない、override 再送で維持される)。

    Returns:
        各 row に以下 key を足した list:
        - can_execute: bool (send-safe な入力が揃っているか)
        - abort_reasons: list[str] (can_execute=False の内訳)
        - send_ship_cost_usd: 送信予定 ship_cost (現行 override 値の再送)
        - send_ship_additional_usd: 送信予定 +each (現行 override 値の再送)
        - send_seller_profiles: {payment_id, return_id, shipping_id=correct_policy_id}
        - send_ship_priority: 是正後 BP の domestic sortOrder (target_bp_priority)
        - expected_buyer_ship_after: 実行後の buyer-facing 送料予測
          (= send_ship_cost_usd。BP-only 経路と違い override 再送で維持される)
        - buyer_ship_delta: 実行後 - 現行 = 0.00 のはず (override 完全維持)
    """
    plans: list[dict] = []
    for row in enriched:
        reasons: list[str] = []
        if not row.get("live_ok"):
            reasons.append(f"GetItem 失敗: {row.get('live_error')}")
        if not row.get("db_matches_live"):
            reasons.append(
                f"DB({row['current_policy_id']}) と実 eBay"
                f"({row['live_shipping_profile_id']}) が不一致 — 状況変化"
            )
        if not (row.get("live_payment_profile_id") and row.get("live_return_profile_id")):
            reasons.append("payment/return profile ID が snapshot に無い (3ID必須)")
        # HIGH-1 統一安全ガード (tab_product_management L4571-4595 と同型):
        # override 再送で ship_cost / +each のいずれか None (GetItem が返さなかった)
        # は不確定 → 送信すると None を 0.00 / BP default に潰す経路になり
        # DDP buffer を黙って喪失 (Section 232 数百ドル/件)。明示 abort。
        # ⚠️ +each は eBay で「未設定 = 単品と同額」の慣習があるため、
        # ship_cost が確定していれば +each の None は許容 (呼出側で ship_cost と
        # 同値を送る = 現行 GetItem 表示値と等価)。ship_cost None のみ abort。
        if row.get("ship_cost_usd") is None:
            reasons.append(
                "ship_cost_usd が snapshot に無い — override 再送不能 "
                "(DDP buffer 喪失リスクのため abort)"
            )
        if row.get("target_bp_priority") is None:
            reasons.append(
                f"是正後 BP {row['correct_policy_id']} の domestic priority 解決不能 "
                f"({row.get('target_bp_priority_reason')})"
            )

        # +each は ship_cost が確定していれば snap 値 (None なら ship_cost と同値)
        send_ship_cost = row.get("ship_cost_usd")
        raw_add = row.get("ship_additional_usd")
        send_ship_add = raw_add if raw_add is not None else send_ship_cost

        plan = {
            **row,
            "can_execute": not reasons,
            "abort_reasons": reasons,
            "send_ship_cost_usd": send_ship_cost,
            "send_ship_additional_usd": send_ship_add,
            "send_seller_profiles": {
                "payment_id": row.get("live_payment_profile_id"),
                "return_id": row.get("live_return_profile_id"),
                "shipping_id": row.get("correct_policy_id"),
            },
            "send_ship_priority": row.get("target_bp_priority"),
            # combined revise は override 再送で買い手表示送料を維持する仕様。
            # send_ship_cost が確定 = 実行後の buyer-facing 送料と一致 (BP default
            # にリセットされない、W142 combined revise の設計原理)。
            "expected_buyer_ship_after": send_ship_cost,
            "buyer_ship_delta": (
                round(send_ship_cost - row["ship_cost_usd"], 2)
                if (send_ship_cost is not None and row.get("ship_cost_usd") is not None)
                else None
            ),
        }
        plans.append(plan)
    return plans


# =============================================================================
# dry-run
# =============================================================================

def _dry_run_report(
    plans: list[dict], policy_names: dict, weight_audit: dict,
) -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _OUT_DIR / f"phase2_dry_run_{ts}.json"

    for p in plans:
        p["weight_audit"] = weight_audit.get(p["ebay_item_id"], {})

    payload = {
        "plans": plans,
        "notes": {
            "combined_revise_semantics": (
                "combined revise = ReviseFixedPriceItem に SellerProfiles + "
                "ShippingServiceCostOverrideList を同梱送信 (W142)。"
                "BP を差し替えつつ現行 override (DDP buffer) を再送で維持する。"
                "実行後の buyer-facing 送料 = expected_buyer_ship_after "
                "(= send_ship_cost_usd = 現行 ship_cost_usd) で保存される。"
                "BP-only 経路 (Phase 1) と違い override は BP default に "
                "リセットされない。"
            ),
            "special_case_356700630309": (
                "356700630309 (Pioneer CD-9) は現行 override が $0.00 = 送料無料 "
                "設定。本 Phase 2 は override 現状維持のみ行うため実行後も $0.00 "
                "のまま (BP default $30 にリセットされない)。$0 送料の妥当性 "
                "(赤字リスク vs 意図的な送料込価格) は user 判断で別途扱う。"
            ),
        },
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    logger.info(f"=== Phase 2 dry-run 内訳 ({len(plans)} 件) ===")
    logger.info(
        f"{'ebay_item_id':<14} {'現BP':>13} {'新BP':>13} "
        f"{'現送料':>7} {'実行後':>7} {'差額':>7} {'prio':>4} {'can_exec':>9}"
    )
    for p in plans:
        cur = p.get("ship_cost_usd")
        aft = p.get("expected_buyer_ship_after")
        delta = p.get("buyer_ship_delta")
        cur_s = f"${cur:.2f}" if cur is not None else "N/A"
        aft_s = f"${aft:.2f}" if aft is not None else "N/A"
        delta_s = (
            f"+${delta:.2f}" if delta and delta > 0
            else (f"-${abs(delta):.2f}" if delta and delta < 0
                  else "$0.00" if delta == 0 else "N/A")
        )
        logger.info(
            f"  {p['ebay_item_id']:<14} {p['current_policy_id']:>13} "
            f"{p['correct_policy_id']:>13} {cur_s:>7} {aft_s:>7} {delta_s:>7} "
            f"{str(p.get('send_ship_priority')):>4} {str(p['can_execute']):>9}"
        )
    logger.info("---- 詳細 ----")
    for p in plans:
        cur_name = policy_names.get(p["current_policy_id"], "?")
        new_name = policy_names.get(p["correct_policy_id"], "?")
        wa = p["weight_audit"] or {}
        logger.info(
            f"  {p['ebay_item_id']} | {p['title']}\n"
            f"    現在: {p['current_policy_id']} ({cur_name}) "
            f"buyer 送料=${p.get('ship_cost_usd')} +each=${p.get('ship_additional_usd')} "
            f"override_present={p.get('ship_override_present')} "
            f"price=${p.get('start_price_usd')}\n"
            f"    是正: {p['correct_policy_id']} ({new_name}) prio="
            f"{p.get('send_ship_priority')}\n"
            f"    送信: ship_cost=${p.get('send_ship_cost_usd')} "
            f"+each=${p.get('send_ship_additional_usd')} "
            f"seller_profiles={p.get('send_seller_profiles')}\n"
            f"    実行後 buyer 送料予測: ${p.get('expected_buyer_ship_after')} "
            f"(差額 ${p.get('buyer_ship_delta')})\n"
            f"    weight_g={p['weight_g']} "
            f"(source={wa.get('weight_source')} conf={wa.get('weight_confidence')})"
        )
        for r in p["abort_reasons"]:
            logger.warning(f"    ⚠️ abort: {r}")
    logger.info(f"dry-run 結果 JSON: {out_path}")
    return out_path


# =============================================================================
# execute
# =============================================================================

def _execute_combined(
    plans: list[dict], app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> int:
    """combined revise を実行し read-back verify + DB 同期を行う."""
    from monitor.ebay_client import revise_fixed_price_with_shipping
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    if len(plans) >= ABORT_THRESHOLD:
        raise RuntimeError(
            f"対象 {len(plans)} 件が閾値 {ABORT_THRESHOLD} 件以上 — 中止"
        )

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_path = _OUT_DIR / f"phase2_execute_snapshot_{ts}.json"
    snap_path.write_text(
        json.dumps(plans, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info(f"(1) 反映前 snapshot 保存: {snap_path}")

    succeeded: list[dict] = []
    failed: list[dict] = []
    n = len(plans)
    for i, p in enumerate(plans, 1):
        eid = p["ebay_item_id"]
        if not p["can_execute"]:
            failed.append({**p, "reason": "; ".join(p["abort_reasons"])})
            logger.warning(f"[{i}/{n}] {eid} skip: {p['abort_reasons']}")
            continue

        # combined revise (BP 差替 + override 再送)
        rb = revise_fixed_price_with_shipping(
            item_id=eid,
            new_price_usd=None,  # 価格変更しない
            ship_cost_usd=float(p["send_ship_cost_usd"]),
            ship_additional_usd=float(p["send_ship_additional_usd"])
            if p["send_ship_additional_usd"] is not None else None,
            app_id=app_id, dev_id=dev_id, cert_id=cert_id, user_token=user_token,
            seller_profiles=p["send_seller_profiles"],
            ship_priority=p["send_ship_priority"],
            force_seller_profiles=True,
        )
        if not rb.get("success"):
            failed.append({**p, "reason": f"revise API 失敗: {rb.get('message')}"})
            logger.warning(f"[{i}/{n}] {eid} 失敗: {rb.get('message')}")
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
            continue

        time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
        snap2 = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, user_token)
        # verify: BP 差替 (a) + override 維持 (b) + 送料額一致 (c)
        bp_ok = bool(snap2.ok and snap2.shipping_profile_id == p["correct_policy_id"])
        ship_ok = bool(
            snap2.ok and snap2.ship_cost_usd is not None
            and abs(snap2.ship_cost_usd - float(p["send_ship_cost_usd"])) < 0.01
        )
        override_kept = bool(snap2.ok and snap2.ship_override_present)
        if not (bp_ok and ship_ok and override_kept):
            failed.append({
                **p,
                "reason": (
                    f"verify 失敗: bp_ok={bp_ok} ship_ok={ship_ok} "
                    f"override_kept={override_kept} "
                    f"(実 BP={snap2.shipping_profile_id if snap2.ok else snap2.error} "
                    f"実 ship=${snap2.ship_cost_usd})"
                ),
            })
            logger.warning(f"[{i}/{n}] {eid} verify NG")
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
            continue

        # DB 同期 (BP + shipping_cost + shipping_additional_cost、_sync_db_to_actual 同型)
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET "
                "shipping_profile_id=?, "
                "shipping_profile_fetched_at=datetime('now'), "
                "shipping_cost=?, "
                "shipping_additional_cost=?, "
                "shipping_additional_fetched_at=datetime('now'), "
                "last_synced_at=datetime('now') "
                "WHERE ebay_item_id=?",
                (
                    snap2.shipping_profile_id,
                    snap2.ship_cost_usd,
                    snap2.ship_additional_usd,
                    eid,
                ),
            )
        bump_db_version()

        succeeded.append({
            **p,
            "post_shipping_profile_id": snap2.shipping_profile_id,
            "post_ship_cost_usd": snap2.ship_cost_usd,
            "post_ship_override_present": snap2.ship_override_present,
        })
        logger.info(
            f"[{i}/{n}] {eid} 成功: BP {p['current_policy_id']} -> {snap2.shipping_profile_id}, "
            f"ship ${snap2.ship_cost_usd} (override_kept)"
        )
        if i < n:
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))

    result_path = _OUT_DIR / f"phase2_execute_result_{ts}.json"
    result_path.write_text(
        json.dumps(
            {"succeeded": succeeded, "failed": failed},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    logger.info(
        f"完了: 成功 {len(succeeded)} / 失敗 {len(failed)} 件 (詳細: {result_path})"
    )
    return 0 if not failed else 1


# =============================================================================
# main
# =============================================================================

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="実反映 (既定は dry-run)")
    parser.add_argument("--only", type=str, default=None,
                         help="単一 ebay_item_id に絞る")
    args = parser.parse_args(argv)

    cfg = load_settings_policies()
    with get_conn() as conn:
        candidates = find_candidates(conn, cfg)

    if args.only:
        candidates = [c for c in candidates if c["ebay_item_id"] == args.only]
        logger.info(f"--only フィルタ: {args.only} → {len(candidates)} 件")

    if not candidates:
        logger.info("対象 0 件 — Phase 1/2 完了済 or 状況変化 (何もしない)")
        return 0

    logger.info(
        f"対象 {len(candidates)} 件 (Phase 2 想定 4 件 = Phase 1 完了後の残)"
    )

    from monitor.inventory_sync import _get_credentials
    creds = _get_credentials()
    if not creds:
        logger.error("eBay 認証取得失敗 — 中止")
        return 1
    app_id, dev_id, cert_id, user_token = creds

    enriched = enrich_with_snapshot_and_priority(
        candidates, app_id, dev_id, cert_id, user_token
    )
    plans = build_combined_revise_plans(enriched)

    policy_names = _try_fetch_policy_names(cfg)
    weight_audit = _fetch_weight_audit(candidates)

    if not args.execute:
        _dry_run_report(plans, policy_names, weight_audit)
        return 0

    return _execute_combined(plans, app_id, dev_id, cert_id, user_token)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
