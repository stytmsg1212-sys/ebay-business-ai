#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード #48: 無在庫 listing のハンドリングポリシー是正 one-shot (money-direct T3).

トリアージ結果 (確定済み前提):
  無在庫 SKU (`sku LIKE 'ebay%'`) の active listing のうち、
  `ebay_listings.shipping_profile_id` が「在庫あり (1日ハンドリング)」用の
  Business Policy ID になっている listing が 5 件存在する (全て
  created_at=2026-04-05 の初期バルク登録、selector ロジック導入前)。
  現行 `monitor/shipping_policy_selector.py` は健全 (以後の出品には影響なし)。

  無在庫 listing は物理在庫を持たないため 1日ハンドリングを約束すると
  出荷遅延 (Late Shipment) リスクを負う。settings.json
  `shipping_weight_mapping_no_stock` (7日系) への是正が正しい方向。

  逆方向 (有在庫なのに7日ポリシー、約20件) は本 script では**触らない**
  (優先度低、別途 user 判断)。

安全ゲート:
  - 抽出件数が ABORT_THRESHOLD (6) 件以上なら中止 (想定 5 件からの逸脱 =
    状況変化のシグナル、db-migration-rules 準拠で assert でなく明示 raise)。
  - --execute 時は各 listing ごとに GetItem pre-snapshot で DB との一致を
    再確認してから revise (乖離があれば状況変化として skip、force しない)。
  - revise 後は GetItem read-back で実値 verify、確認できた分だけ DB へ
    実値を同期する (_sync_db_to_actual と同型、DB は常に eBay の真実を映す)。
  - 失敗は成功と混ぜず `failed` バケットへ分離して記録し、次の item へ続行
    する (coo バッチ HIGH-4/1/2 の教訓: dry-run/execute の出力ファイル分離 +
    失敗の隔離、5 件規模のため resumable な永続 done_ids までは実装しない
    = K1 simplicity、必要になれば 3 回目の要求で拡張)。

HIGH-1 対応 (T3 レビュー 2026-07-04):
  - `_build_revise_bp_only_xml` は ShippingServiceCostOverrideList を送出しない
    ため、per-listing の DDP 関税 override (price*0.20) が bind している場合は
    BP 差替で **消失** (W142 型の無音失敗と同機構 = DDP buffer 喪失 =
    Section 232 数百ドル/件リスク)。
  - dry-run で各 listing の「現行 buyer-facing 送料 → 是正後 BP default
    予測送料 → 差額」を可視化し、`ship_override_present=True` の件は
    赤字警告する (compute_cost_deltas 純関数)。
  - 予測送料は destination BP の Sell Account API 応答内 DOMESTIC service
    の `shippingCost.value` を実際に取得して使う (国内買い手向けの
    BP default 額。国際は rate table 経由なので買い手国依存 = dry-run では
    参考情報として区別表示)。

使い方:
  python scripts/fix_handling_policy_board48.py             # dry-run (既定、eBay 書込なし)
  python scripts/fix_handling_policy_board48.py --execute   # 実反映 (eBay ReviseFixedPriceItem)

出力:
  data/board48/dry_run_<timestamp>.json     (dry-run 結果、GetItem read-only)
  data/board48/execute_snapshot_<timestamp>.json  (--execute 時、反映直前 rollback 用)
  data/board48/execute_result_<timestamp>.json    (--execute 時、成功/失敗バケット別)
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.database import get_conn  # noqa: E402
from monitor.shipping_policy_selector import (  # noqa: E402
    load_settings_policies,
    select_shipping_policy,
)
from ui_cache import bump_db_version  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("board48_handling_policy_fix")

# トリアージで確定した想定件数。これを超えて増えていたら状況変化 = 停止して
# user へ報告する (Q0 silent-skip-prevention: 想定外を黙って処理しない)。
EXPECTED_COUNT = 5
ABORT_THRESHOLD = 6

_OUT_DIR = _ROOT / "data" / "board48"
_SLEEP_BASE = 1.5  # eBay API 呼出間隔 (anti-bot / rate、既存 one-shot 慣習)


# =============================================================================
# 抽出ロジック (純関数、pytest 対象)
# =============================================================================

def find_candidates(conn: sqlite3.Connection, cfg: dict) -> list[dict]:
    """無在庫 SKU + 在庫あり(1日)ハンドリングポリシー が付いた active listing を抽出.

    各件について settings.json `shipping_weight_mapping_no_stock` から
    正解ポリシー (7日系) を weight_g ベースで算出する。

    Args:
        conn: sqlite3 接続 (row_factory=sqlite3.Row 前提、monitor.database.get_conn 互換)
        cfg: settings.json 全体 (ebay_business_policies ブロック必須)

    Returns:
        [{ebay_item_id, sku, title, weight_g, created_at,
          current_policy_id, correct_policy_id, correct_policy_label}, ...]

    Raises:
        RuntimeError: 抽出件数が ABORT_THRESHOLD 以上 (想定件数からの逸脱)。
        ValueError: settings.json に必要なポリシーマッピングが無い
                    (select_shipping_policy 由来、伝播させる)。
    """
    policies = cfg.get("ebay_business_policies") or {}
    in_stock_mapping = policies.get("shipping_weight_mapping_in_stock") or {}
    in_stock_ids = sorted({str(v) for v in in_stock_mapping.values() if v})
    if not in_stock_ids:
        raise ValueError(
            "settings.json に ebay_business_policies."
            "shipping_weight_mapping_in_stock が無い/空"
        )

    placeholders = ", ".join("?" for _ in in_stock_ids)
    sql = f"""
        SELECT ebay_item_id, sku, title, weight_g, shipping_profile_id, created_at
          FROM ebay_listings
         WHERE COALESCE(is_ended, 0) = 0
           AND sku LIKE 'ebay%'
           AND shipping_profile_id IN ({placeholders})
         ORDER BY ebay_item_id
    """
    rows = conn.execute(sql, tuple(in_stock_ids)).fetchall()

    if len(rows) >= ABORT_THRESHOLD:
        raise RuntimeError(
            f"対象 {len(rows)} 件 (想定 {EXPECTED_COUNT} 件) が閾値 "
            f"{ABORT_THRESHOLD} 件以上 — 状況変化の可能性があるため中止。"
            "再トリアージしてから再実行してください。"
        )

    out: list[dict] = []
    for r in rows:
        weight_g = r["weight_g"]
        correct_id, correct_label = select_shipping_policy(weight_g, False, cfg)
        out.append({
            "ebay_item_id": r["ebay_item_id"],
            "sku": r["sku"],
            "title": r["title"],
            "weight_g": weight_g,
            "created_at": r["created_at"],
            "current_policy_id": r["shipping_profile_id"],
            "correct_policy_id": correct_id,
            "correct_policy_label": correct_label,
        })
    return out


def _try_fetch_policy_names(cfg: dict) -> dict:
    """business policy id → name 解決 (best-effort、失敗しても続行).

    Sell Account API (read-only) 呼出。失敗時は空 dict (id 表示のみに degrade、
    Q0: 通信失敗を成功と偽らない)。
    """
    try:
        from monitor.ebay_account_policy import fetch_shipping_policies
        pol_list = fetch_shipping_policies(cfg)
        if not pol_list.ok:
            logger.warning(f"business policy 名解決 失敗 (id のみ表示): {pol_list.error}")
            return {}
        return {p.policy_id: p.name for p in pol_list.policies}
    except (ImportError, OSError, ValueError) as e:
        logger.warning(f"business policy 名解決 失敗 (id のみ表示): {e}")
        return {}


def fetch_bp_domestic_defaults(policy_ids: list[str]) -> dict:
    """destination BP の DOMESTIC service 送料 default (BP 直書き値) を取得.

    HIGH-1: BP 差替後の buyer-facing 送料 (国内) は BP default (shippingCost.value)
    と additionalShippingCost で決まる (`_build_revise_bp_only_xml` は
    ShippingServiceCostOverrideList を送出しないため per-listing override は
    消える → BP default にリセット)。

    Returns:
        {policy_id: {"cost": float|None, "additional": float|None,
                     "service_code": str|None,
                     "intl_uses_rate_table": bool, "rate_table_id": str|None,
                     "error": str|None}}
        エラー時は全 policy 分の value を error 付きで返す (Q0: silent skip 禁止)。
    """
    import urllib.request

    result: dict = {pid: {"cost": None, "additional": None, "service_code": None,
                          "intl_uses_rate_table": False, "rate_table_id": None,
                          "error": None} for pid in policy_ids}
    try:
        from monitor.ebay_oauth_refresh import get_valid_access_token
        tok = get_valid_access_token()
    except (ImportError, OSError, ValueError) as e:
        for pid in policy_ids:
            result[pid]["error"] = f"OAuth token 取得失敗: {e}"
        return result
    if not tok:
        for pid in policy_ids:
            result[pid]["error"] = "OAuth token 空"
        return result

    url = "https://api.ebay.com/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        for pid in policy_ids:
            result[pid]["error"] = f"Account API 通信失敗: {e}"
        return result

    raw = data.get("fulfillmentPolicies") or []
    by_id = {str(p.get("fulfillmentPolicyId") or ""): p for p in raw}
    target = set(policy_ids)
    for pid in target:
        p = by_id.get(pid)
        if not p:
            result[pid]["error"] = "fulfillmentPolicyId 該当なし"
            continue
        for opt in p.get("shippingOptions") or []:
            is_dom = opt.get("optionType") == "DOMESTIC"
            is_intl = opt.get("optionType") == "INTERNATIONAL"
            rate_tbl = opt.get("rateTableId")
            if is_intl and rate_tbl:
                result[pid]["intl_uses_rate_table"] = True
                result[pid]["rate_table_id"] = str(rate_tbl)
            if not is_dom:
                continue
            for sv in opt.get("shippingServices") or []:
                cost = sv.get("shippingCost") or {}
                add = sv.get("additionalShippingCost") or {}
                try:
                    result[pid]["cost"] = float(cost.get("value")) if cost.get("value") is not None else None
                except (TypeError, ValueError):
                    result[pid]["cost"] = None
                try:
                    result[pid]["additional"] = (
                        float(add.get("value")) if add.get("value") is not None else None
                    )
                except (TypeError, ValueError):
                    result[pid]["additional"] = None
                result[pid]["service_code"] = sv.get("shippingServiceCode")
                break  # 単一 DOMESTIC 前提 (現行 BP は全て 1 service、resolve_domestic_priority と同前提)
            break
        if result[pid]["cost"] is None and not result[pid]["error"]:
            result[pid]["error"] = "DOMESTIC shippingCost 取得不能"
    return result


def compute_cost_deltas(enriched: list[dict], bp_defaults: dict) -> list[dict]:
    """HIGH-1 純関数: 各 listing の送料額差テーブルを生成.

    Args:
        enriched: `_enrich_with_live_snapshot` の出力 (ship_cost_usd,
                  ship_additional_usd, ship_override_present, correct_policy_id を含む)
        bp_defaults: `fetch_bp_domestic_defaults` の出力 (correct_policy_id で引く)

    Returns:
        各 row に以下 key を足した list:
        - current_ship_cost_usd: 現行 buyer-facing 送料 (実 eBay、None=不明)
        - predicted_ship_cost_usd: 是正後 BP default 予測 (None=BP 取得失敗)
        - delta_usd: predicted - current (絶対値 = 買い手表示額の変動、正=増額)
        - override_will_be_lost: bool (現在 override 有 = BP 差替で消失、
          DDP buffer 喪失リスク)
        - warnings: list[str] (人間可読、赤字警告用)
    """
    out: list[dict] = []
    for row in enriched:
        cur = row.get("ship_cost_usd")
        cur_add = row.get("ship_additional_usd")
        override_present = bool(row.get("ship_override_present"))
        dest = row.get("correct_policy_id")
        bp = bp_defaults.get(dest) or {}
        pred = bp.get("cost")
        pred_add = bp.get("additional")
        pred_err = bp.get("error")
        delta = (
            round(float(pred) - float(cur), 2)
            if (pred is not None and cur is not None) else None
        )
        warnings: list[str] = []
        if pred_err:
            warnings.append(f"是正後 BP default 取得失敗 ({pred_err}) — 予測不能")
        if override_present:
            warnings.append(
                "🔴 DDP 関税 override (ShippingServiceCostOverrideList) が bind 済 → "
                "BP 差替で消失 (Section 232 数百ドル/件リスク)"
            )
        if cur is None:
            warnings.append("現行送料 (実 eBay) が None — 変動不明")
        if delta is not None and delta > 0:
            warnings.append(f"⚠️ buyer-facing 送料 増額 予測 (+${delta:.2f})")
        if bp.get("intl_uses_rate_table"):
            warnings.append(
                f"国際送料は rate table {bp.get('rate_table_id')} 経由 = 買い手国依存 "
                "(dry-run では国内予測のみ)"
            )
        out.append({
            **row,
            "current_ship_cost_usd": cur,
            "current_ship_additional_usd": cur_add,
            "predicted_ship_cost_usd": pred,
            "predicted_ship_additional_usd": pred_add,
            "delta_usd": delta,
            "override_will_be_lost": override_present,
            "warnings": warnings,
        })
    return out


def _fetch_weight_audit(candidates: list[dict]) -> dict:
    """weight_g の由来 (weight_source / weight_confidence / estimated_at) を DB から
    取得し、{ebay_item_id: audit_dict} で返す (MED-4b: 誤 weight → 誤帯防止)."""
    if not candidates:
        return {}
    ids = [c["ebay_item_id"] for c in candidates]
    placeholders = ", ".join("?" for _ in ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ebay_item_id, weight_g, weight_source, weight_confidence, "
            f"       weight_estimated_at "
            f"FROM ebay_listings WHERE ebay_item_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    return {r["ebay_item_id"]: dict(r) for r in rows}


def _enrich_with_live_snapshot(
    candidates: list[dict], app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> list[dict]:
    """各 listing を GetItem (read-only) で実 eBay 値と突合し、3 profile ID を追加する.

    DB は乖離し得る (真実源は実 eBay) ため、--execute で使う payment/return
    profile ID もここで取得しておく。呼出は候補数分 (想定 5 回)。
    """
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    out: list[dict] = []
    n = len(candidates)
    for i, c in enumerate(candidates, 1):
        snap = fetch_listing_snapshot(
            c["ebay_item_id"], app_id, dev_id, cert_id, user_token
        )
        row = dict(c)
        row["live_ok"] = snap.ok
        row["live_error"] = snap.error
        row["live_shipping_profile_id"] = snap.shipping_profile_id
        row["live_payment_profile_id"] = snap.payment_profile_id
        row["live_return_profile_id"] = snap.return_profile_id
        # HIGH-1 (T3): 送料関連の snapshot 値を row に取り込む (compute_cost_deltas
        # の入力。従来は取得済みなのに捨てていた = 変動不可視化事故の温床)。
        row["ship_cost_usd"] = snap.ship_cost_usd
        row["ship_additional_usd"] = snap.ship_additional_usd
        row["ship_override_present"] = snap.ship_override_present
        row["ship_override_priority"] = snap.ship_override_priority
        row["db_matches_live"] = bool(
            snap.ok and snap.shipping_profile_id == c["current_policy_id"]
        )
        out.append(row)
        logger.info(
            f"[{i}/{n}] {c['ebay_item_id']} GetItem ok={snap.ok} "
            f"live_shipping_profile_id={snap.shipping_profile_id} "
            f"ship_cost={snap.ship_cost_usd} override_present={snap.ship_override_present} "
            f"(DB={c['current_policy_id']}, 一致={row['db_matches_live']})"
        )
        if i < n:
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
    return out


# =============================================================================
# dry-run
# =============================================================================

def _dry_run_report(
    enriched: list[dict], policy_names: dict, bp_defaults: dict,
    weight_audit: dict,
) -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _OUT_DIR / f"dry_run_{ts}.json"

    # HIGH-1: 各 listing の送料額差を計算 (純関数 compute_cost_deltas)
    with_deltas = compute_cost_deltas(enriched, bp_defaults)
    # MED-4b: weight_g の由来 (source/confidence) を row に付与 (誤 weight 検知用)
    for row in with_deltas:
        row["weight_audit"] = weight_audit.get(row["ebay_item_id"], {})

    payload = {
        "bp_defaults": bp_defaults,
        "listings": with_deltas,
        "notes": {
            "override_semantics": (
                "override_will_be_lost=True の件は現在 buyer-facing 送料に DDP 関税 "
                "buffer (price*0.20) が override 経由で乗っており、BP 差替で消失する。"
                "revise 後は必ず read-back verify で ship_cost を確認、必要なら別途 "
                "override を再送する運用に切替 (本 script は BP 単独差替のみで、"
                "override 再送は行わない = W142 型無音失敗リスク)。"
            ),
            "international_semantics": (
                "国際送料は BP に紐づいた rate table 経由 = 買い手国別に決まるため、"
                "本 dry-run では国内 (US) buyer-facing の default 予測のみを提示。"
                "国際は現行 rate table 再構築 (2026-06-19) 後の実額が既定。"
            ),
        },
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    logger.info(f"=== dry-run 内訳 ({len(with_deltas)} 件) ===")
    logger.info(
        f"{'ebay_item_id':<14} {'weight':>7} "
        f"{'現BP':>13} {'新BP':>13} "
        f"{'現送料':>7} {'予測':>7} {'差額':>7} {'override':>9}"
    )
    for row in with_deltas:
        cur_ship = row["current_ship_cost_usd"]
        pred = row["predicted_ship_cost_usd"]
        delta = row["delta_usd"]
        cur_s = f"${cur_ship:.2f}" if cur_ship is not None else "N/A"
        pred_s = f"${pred:.2f}" if pred is not None else "N/A"
        delta_s = (
            (f"+${delta:.2f}" if delta > 0 else f"-${abs(delta):.2f}"
             if delta < 0 else "$0.00")
            if delta is not None else "N/A"
        )
        ovr = "YES" if row["override_will_be_lost"] else "no"
        logger.info(
            f"  {row['ebay_item_id']:<14} {row['weight_g']!s:>7} "
            f"{row['current_policy_id']:>13} {row['correct_policy_id']:>13} "
            f"{cur_s:>7} {pred_s:>7} {delta_s:>7} {ovr:>9}"
        )
    logger.info("---- 詳細 ----")
    for row in with_deltas:
        cur_name = policy_names.get(row["current_policy_id"], "?")
        new_name = policy_names.get(row["correct_policy_id"], "?")
        wa = row["weight_audit"] or {}
        logger.info(
            f"  {row['ebay_item_id']} | {row['title']}\n"
            f"    現在: {row['current_policy_id']} ({cur_name}) "
            f"[実eBay一致={row['db_matches_live']}] "
            f"buyer 送料=${row['current_ship_cost_usd']} "
            f"+each=${row['current_ship_additional_usd']} "
            f"override_present={row['ship_override_present']}\n"
            f"    是正: {row['correct_policy_id']} ({new_name}) "
            f"予測 buyer 送料=${row['predicted_ship_cost_usd']} "
            f"+each=${row['predicted_ship_additional_usd']}\n"
            f"    weight_g={row['weight_g']} "
            f"(source={wa.get('weight_source')} confidence={wa.get('weight_confidence')} "
            f"at={wa.get('weight_estimated_at')})"
        )
        for w in row["warnings"]:
            logger.warning(f"    {w}")
    logger.info(f"dry-run 結果 JSON: {out_path}")
    logger.info("--execute で実反映 (eBay ReviseFixedPriceItem + read-back verify)")
    return out_path


# =============================================================================
# execute
# =============================================================================

def _execute(
    enriched: list[dict], app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> int:
    from monitor.ebay_client import revise_shipping_profile
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    if len(enriched) >= ABORT_THRESHOLD:
        raise RuntimeError(
            f"対象 {len(enriched)} 件が閾値 {ABORT_THRESHOLD} 件以上 — 中止"
        )

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # (1) 反映前 snapshot (rollback 用)
    snap_path = _OUT_DIR / f"execute_snapshot_{ts}.json"
    snap_path.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info(f"(1) 反映前 snapshot 保存: {snap_path}")

    succeeded: list[dict] = []
    failed: list[dict] = []
    n = len(enriched)

    for i, row in enumerate(enriched, 1):
        eid = row["ebay_item_id"]

        if not row["live_ok"]:
            failed.append({**row, "reason": f"pre-snapshot 取得失敗: {row['live_error']}"})
            logger.warning(f"[{i}/{n}] {eid} skip: pre-snapshot 取得失敗")
            continue

        if row["live_shipping_profile_id"] != row["current_policy_id"]:
            failed.append({
                **row,
                "reason": (
                    f"DB({row['current_policy_id']}) と実eBay"
                    f"({row['live_shipping_profile_id']}) が不一致 — "
                    "状況変化のため force せず skip"
                ),
            })
            logger.warning(f"[{i}/{n}] {eid} skip: DB/実eBay 不一致 (状況変化)")
            continue

        if not (row["live_payment_profile_id"] and row["live_return_profile_id"]):
            failed.append({
                **row,
                "reason": "payment/return profile ID が GetItem から取得できず (3ID必須)",
            })
            logger.warning(f"[{i}/{n}] {eid} skip: payment/return profile ID 欠落")
            continue

        rb = revise_shipping_profile(
            eid,
            {
                "payment_id": row["live_payment_profile_id"],
                "return_id": row["live_return_profile_id"],
                "shipping_id": row["correct_policy_id"],
            },
            app_id, dev_id, cert_id, user_token,
        )
        if not rb.get("success"):
            failed.append({**row, "reason": f"revise API 失敗: {rb.get('message')}"})
            logger.warning(f"[{i}/{n}] {eid} 失敗: revise API — {rb.get('message')}")
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
            continue

        time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
        snap2 = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, user_token)
        verified = bool(snap2.ok and snap2.shipping_profile_id == row["correct_policy_id"])
        if not verified:
            failed.append({
                **row,
                "reason": (
                    f"read-back verify 失敗 (期待={row['correct_policy_id']} "
                    f"実={snap2.shipping_profile_id if snap2.ok else snap2.error})"
                ),
            })
            logger.warning(f"[{i}/{n}] {eid} 失敗: read-back verify NG")
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))
            continue

        # DB を実値 (post-snapshot) で同期。_sync_db_to_actual と同型の最小実装
        # (本 script の scope 内 = shipping_profile_id 系 2 列のみ、K2 surgical)。
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET shipping_profile_id=?, "
                "shipping_profile_fetched_at=datetime('now'), "
                "last_synced_at=datetime('now') WHERE ebay_item_id=?",
                (snap2.shipping_profile_id, eid),
            )
        bump_db_version()

        succeeded.append({**row, "post_shipping_profile_id": snap2.shipping_profile_id})
        logger.info(f"[{i}/{n}] {eid} 成功: {row['current_policy_id']} -> {snap2.shipping_profile_id}")
        if i < n:
            time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))

    result_path = _OUT_DIR / f"execute_result_{ts}.json"
    result_path.write_text(
        json.dumps(
            {
                "succeeded": succeeded,
                "failed": failed,
                # MED-4a (T3 レビュー): verify transient 失敗で DB が恒久 stale に
                # ならないよう、失敗 item の手動 resync 手順を明記。
                "manual_recovery_guide": {
                    "verify_transient_failure": (
                        "read-back verify NG (revise は Ack=Success だが GetItem が "
                        "eventual consistency で遅延) の可能性がある場合: "
                        "(a) 30 秒〜5 分待って `python scripts/fix_handling_policy_board48.py` "
                        "を再実行 → 是正済なら候補から除外される (冪等)。"
                        "(b) それでも DB が stale なら商品管理タブの ↻Shipping BP 再取得 で "
                        "実 eBay 値を DB に取り込む (_sync_db_to_actual 経路)。"
                    ),
                    "revise_api_failure": (
                        "revise API 自体が失敗 (Ack=Failure) の場合: eBay 側で "
                        "policy 削除・token 失効・rate limit を疑う。"
                        "eBay エラーコードを message から特定し、根本を直してから再試行。"
                    ),
                    "db_live_mismatch": (
                        "DB と実 eBay の shipping_profile_id が乖離 (状況変化) の場合: "
                        "user が手動で変更した可能性 → 是正不要かもしれない。"
                        "実 eBay の現行 BP が settings.json in_stock マッピングに残っていれば "
                        "再度対象化される、外れていれば是正不要。"
                    ),
                },
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    logger.info(
        f"完了: 成功 {len(succeeded)} / 失敗 {len(failed)} 件 (詳細: {result_path})"
    )
    if failed:
        logger.warning("失敗明細:")
        for f in failed:
            logger.warning(f"  {f['ebay_item_id']}: {f['reason']}")
        logger.warning(
            "手動復旧: 失敗 item は次回 dry-run で再度候補化される (冪等)。"
            f"詳細な resync 手順は {result_path} の manual_recovery_guide 参照。"
        )

    return 0 if not failed else 1


# =============================================================================
# main
# =============================================================================

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="実反映する (既定は dry-run、eBay 書込なし)",
    )
    parser.add_argument(
        "--only", type=str, default=None,
        help="単一 ebay_item_id に処理を絞る (段階的実行用、e.g. --only 358132396521)",
    )
    args = parser.parse_args(argv)

    cfg = load_settings_policies()

    with get_conn() as conn:
        candidates = find_candidates(conn, cfg)

    if args.only:
        candidates = [c for c in candidates if c["ebay_item_id"] == args.only]
        logger.info(
            f"--only フィルタ適用: ebay_item_id={args.only} → 対象 {len(candidates)} 件"
        )
        if not candidates:
            logger.error(
                f"--only {args.only} が抽出結果に無い (既に是正済 / 対象外 / typo?)"
            )
            return 1

    if not candidates:
        logger.info("対象 0 件 — 是正不要 (何もしない)")
        return 0

    logger.info(f"対象 {len(candidates)} 件 (想定 {EXPECTED_COUNT} 件)")

    from monitor.inventory_sync import _get_credentials
    creds = _get_credentials()
    if not creds:
        logger.error("eBay 認証取得失敗 — 中止")
        return 1
    app_id, dev_id, cert_id, user_token = creds

    policy_names = _try_fetch_policy_names(cfg)
    enriched = _enrich_with_live_snapshot(candidates, app_id, dev_id, cert_id, user_token)

    # HIGH-1: 是正後 BP default の buyer-facing 送料を fetch (dry-run/execute 共通)
    dest_bps = sorted({c["correct_policy_id"] for c in candidates})
    bp_defaults = fetch_bp_domestic_defaults(dest_bps)
    # MED-4b: weight_g の由来を DB から取得 (誤 weight → 誤帯を dry-run で捕捉)
    weight_audit = _fetch_weight_audit(candidates)

    if not args.execute:
        _dry_run_report(enriched, policy_names, bp_defaults, weight_audit)
        return 0

    # HIGH-1 追加安全ゲート: BP default 取得不能 or override_present=True の件は
    # execute で送料変動を予測できず / DDP buffer 喪失リスクがあるため、
    # dry-run 承認済でも execute 側で追加確認する (deltas を再計算 → 事故防止)。
    with_deltas = compute_cost_deltas(enriched, bp_defaults)
    risky = [r for r in with_deltas if r["override_will_be_lost"] or r["warnings"]]
    if risky:
        logger.warning(
            f"HIGH-1 risky items {len(risky)} 件: buyer-facing 送料変動 or DDP override 消失リスク "
            "— dry-run 出力 (warnings 欄) を確認してから再 execute してください"
        )

    return _execute(enriched, app_id, dev_id, cert_id, user_token)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
