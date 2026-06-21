# -*- coding: utf-8 -*-
"""W284 Phase 3: eBaymag-aware relist (窓ゼロ).

eBaymag 各国版を持つ listing の End→Relist を CDP 在席時に「窓ゼロ」で完結する。
  - daily_relist (深夜 02:30) は eBaymag 商品 (segment != '出さない') を対象にしない (#6 維持)。
  - 本タスクは CDP+eBaymag ログイン在席時のみ、eBaymag 商品の relist を担う。

## 窓ゼロ原則 (design §3 Phase 3)
各 item につき以下を同一ウィンドウで一気に完結する (途中で止めない):
  ① RelistFixedPriceItem で新 item_id 取得
  ② inherit_listing_on_relist で desired 等を継承 (ebaymag_segment/ebaymag_desired_sites_json 含む)
  ③ 旧 ebaymag_products を relisted マーク (last_apply_result='relisted')
  ④ 新 item_id を discover_product_id で取得 → 未発見は enqueue_ebaymag_apply で Phase2 委譲
  ⑤ desired 国へ apply_site_changes で再公開
  ⑥ 定着検証 (fetch_site_states で新 product_id の実態確認)

途中失敗 → needs_manual + Discord 通知 (Q0: 偽装成功禁止、部分公開を放置しない)

## feature flag
tasks_enabled.ebaymag_relist.enabled (既定 False)。
False のうちは run 呼出で即 skip + log のみ (scheduler 登録はされる = flag-gated)。

## 識別キー
ebay_item_id 一択 (SKU 禁止、sku-rules.md)。

## timezone
UTC (sqlite-timezone.md)。next_attempt_at 系は使わないが log は utc timestamp。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

# top-level imports for monkeypatching in tests
# (lazy local imports inside helpers were not patchable via setattr on module)
from monitor.ebay_client import end_item, relist_item  # noqa: E402
from monitor.ebaymag_driver import (  # noqa: E402
    discover_product_id,
    fetch_site_states,
    apply_site_changes,
    SITE_MAP,
)
from tasks.task_daily_relist import inherit_listing_on_relist, END_REASON  # noqa: E402
from tasks.task_ebaymag_apply_queue import _probe_cdp_ebaymag  # noqa: E402
from monitor.database import enqueue_ebaymag_apply  # noqa: E402

logger = logging.getLogger(__name__)

# 1 run あたりの最大処理件数 (長時間 CDP 占有を避ける。canary 時は 1 でも可)
_MAX_PER_RUN_DEFAULT = 3

# relist API リトライ回数 (daily_relist と同じ 3 試行)
_RELIST_MAX_ATTEMPTS = 3

# relist 後 discover するまでのウォームアップ wait (秒)
# eBaymag 取込は非同期なので短い wait を入れてから discover を試みる
_DISCOVER_WAIT_SEC = 5


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _discord_notify(config: dict, message: str) -> None:
    """Discord 既定 ch に通知 (失敗は warn のみ)。"""
    try:
        from notifiers.discord_notifier import notifier_for
        notifier = notifier_for("default")
        notifier.send_message(message)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ebaymag_relist] Discord 通知失敗 (本処理に影響なし): %s", e)


def _get_ebay_credentials(config: dict) -> dict | None:
    """eBay 資格情報を取得。失敗時は None。"""
    try:
        from monitor.credentials import get_ebay_credentials, ebay_credentials_ok  # noqa: PLC0415
        creds = get_ebay_credentials(config)
        if not ebay_credentials_ok(creds):
            return None
        return creds
    except Exception as e:  # noqa: BLE001
        logger.error("[ebaymag_relist] eBay 資格情報取得失敗: %s", e)
        return None


def _select_ebaymag_relist_targets(limit: int, cooldown_days: int = 10) -> list[dict]:
    """eBaymag 商品 (segment∈{全国,優先国,カスタム}) のうち relist 条件を満たすものを選出。

    relist 条件 (daily_relist と同じ基本条件、但し segment フィルタは逆):
      - is_ended=0 かつ quantity_ebay>=1
      - watch_count=0
      - rank='E'
      - cooldown_days 以内に relist_history に old/new として成功記録なし
      - ebaymag_segment IN ('全国', '優先国', 'カスタム')  ← daily_relist の #6 と逆

    識別キー: ebay_item_id (SKU 禁止)。
    """
    from monitor.database import get_conn
    cd = f"-{max(1, int(cooldown_days))} days"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku, title, watch_count, rank,
                      time_left_seconds, start_time, current_price,
                      ebaymag_segment, ebaymag_desired_sites_json
               FROM ebay_listings
               WHERE (is_ended IS NULL OR is_ended = 0)
                 AND quantity_ebay >= 1
                 AND watch_count = 0
                 AND rank = 'E'
                 AND ebaymag_segment IN ('全国', '優先国', 'カスタム')
                 AND ebay_item_id NOT IN (
                     SELECT old_item_id FROM relist_history
                     WHERE created_at >= datetime('now', ?)
                       AND success = 1
                 )
                 AND ebay_item_id NOT IN (
                     SELECT new_item_id FROM relist_history
                     WHERE new_item_id IS NOT NULL
                       AND created_at >= datetime('now', ?)
                       AND success = 1
                 )
               ORDER BY
                 COALESCE(time_left_seconds, 9999999) ASC,
                 COALESCE(start_time, '9999-12-31') ASC
               LIMIT ?""",
            (cd, cd, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_old_product_relisted(old_item_id: str) -> None:
    """旧 ebaymag_products の last_apply_result を 'relisted' にマーク。

    ebaymag_products の PK は ebay_item_id なので、旧エントリは
    新 item_id への upsert 後も残存する (PK が異なる)。
    last_apply_result='relisted' で孤立を可視化し、Phase2 キュー消化でも
    旧 item_id を discover 対象にしないようにする。
    """
    try:
        from monitor.database import get_conn
        with get_conn() as conn:
            conn.execute(
                """UPDATE ebaymag_products
                   SET last_apply_result = 'relisted', updated_at = CURRENT_TIMESTAMP
                   WHERE ebay_item_id = ?""",
                (old_item_id,),
            )
    except Exception as e:  # noqa: BLE001
        # マーク失敗は孤立化リスクだが、窓ゼロ継続優先 (next sync で上書きされる)
        logger.warning("[ebaymag_relist] 旧 ebaymag_products マーク失敗 eid=%s: %s", old_item_id, e)


# ──────────────────────────────────────────────────────────────────────────────
# 1 件処理
# ──────────────────────────────────────────────────────────────────────────────

def _process_single_relist(
    target: dict,
    creds: dict,
    config: dict,
) -> dict:
    """eBaymag 商品 1 件の窓ゼロ relist を実行。

    Returns:
        {
          "old_item_id": str,
          "new_item_id": str | None,
          "success": bool,
          "step": str,          # 最後に到達した step (observability)
          "error_message": str | None,
          "discover_delegated": bool,  # True = discover 失敗→Phase2 委譲
          "sites_applied": list[str],  # 再公開できた国リスト
        }
    """
    from monitor.database import (
        get_ebaymag_product,
        upsert_ebaymag_product,
        get_conn,
    )
    # enqueue_ebaymag_apply は module-level import (monkeypatching のため)

    old_item_id: str = target["ebay_item_id"]
    sku: str = target.get("sku") or ""
    title: str = target.get("title") or ""
    current_price: float = float(target.get("current_price") or 0.0)
    desired_sites_json: str | None = target.get("ebaymag_desired_sites_json")

    result: dict = {
        "old_item_id": old_item_id,
        "new_item_id": None,
        "success": False,
        "step": "init",
        "error_message": None,
        "discover_delegated": False,
        "sites_applied": [],
    }

    # ── desired_sites 解決 ──
    desired_sites: list[str] = []
    if desired_sites_json:
        try:
            desired_sites = json.loads(desired_sites_json) or []
        except json.JSONDecodeError:
            logger.warning("[ebaymag_relist] desired_sites_json parse 失敗 eid=%s (継続)", old_item_id)

    # ── ① EndItem ──
    result["step"] = "end_item"
    logger.info("[ebaymag_relist] ① EndItem: eid=%s title=%r", old_item_id, title[:40])
    end_r = end_item(
        old_item_id,
        app_id=creds["app_id"],
        dev_id=creds["dev_id"],
        cert_id=creds["cert_id"],
        user_token=creds["user_token"],
        end_reason=END_REASON,
    )
    if not end_r.get("success"):
        err = f"EndItem 失敗: {end_r.get('message')}"
        logger.error("[ebaymag_relist] %s eid=%s", err, old_item_id)
        result["error_message"] = err
        # EndItem 失敗 = listing はまだ active → 旧 eBaymag リンクも生存、needs_manual 不要
        return result

    # EndItem 成功 → DB に is_ended=1 を反映 (relist 失敗に備えた記録)
    try:
        from monitor.database import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET is_ended=1, ended_at=CURRENT_TIMESTAMP, "
                "ended_reason='ebaymag_relist' WHERE ebay_item_id=?",
                (old_item_id,),
            )
    except Exception as _e:  # noqa: BLE001
        logger.warning("[ebaymag_relist] is_ended 更新失敗 (継続): %s", _e)

    time.sleep(2)  # eBay 側反映待ち

    # ── ② RelistFixedPriceItem ──
    result["step"] = "relist"
    new_item_id: str | None = None
    last_err: str | None = None
    for attempt in range(1, _RELIST_MAX_ATTEMPTS + 1):
        logger.info("[ebaymag_relist] ② Relist attempt %d/%d eid=%s", attempt, _RELIST_MAX_ATTEMPTS, old_item_id)
        rel = relist_item(
            old_item_id,
            app_id=creds["app_id"],
            dev_id=creds["dev_id"],
            cert_id=creds["cert_id"],
            user_token=creds["user_token"],
        )
        if rel.get("success"):
            new_item_id = rel.get("new_item_id")
            break
        last_err = rel.get("message")
        logger.warning("[ebaymag_relist] Relist attempt %d 失敗: %s", attempt, last_err)
        if attempt < _RELIST_MAX_ATTEMPTS:
            time.sleep(5 * attempt)

    if not new_item_id:
        err = (
            f"Relist 全失敗 ({_RELIST_MAX_ATTEMPTS}試行): {last_err} — "
            f"旧 eid={old_item_id} はend済。eBay Seller Hub から手動 Sell Similar が必要"
        )
        logger.error("[ebaymag_relist] %s", err)
        result["error_message"] = err
        # HIGH-3 (code-reviewer 2026-06-20): EndItem成功+Relist全失敗 = 旧版end済/新版なし
        # の宙吊り。relist_history に success=0 で記録し DB 追跡可能にする (Discord 通知のみ
        # 依存しない。success=0 は cooldown を発火させないので再試行余地は残る)。
        try:
            from monitor.database import record_relist
            record_relist(old_item_id, None, sku, title, END_REASON, False, err)
        except Exception as _e:  # noqa: BLE001
            logger.error("[ebaymag_relist] relist_history(失敗) 記録失敗: %s", _e)
        _discord_notify(config, f"[eBaymag relist] needs_manual: {title[:40]}\n{err}")
        return result

    result["new_item_id"] = new_item_id
    result["step"] = "inherit"
    logger.info("[ebaymag_relist] Relist OK: old=%s → new=%s", old_item_id, new_item_id)

    # HIGH-1 (code-reviewer 2026-06-20): relist 成功 = 新 item_id を cooldown 保護する。
    # これが無いと relist_history 未記録で cooldown 不発 → 継承された rank='E' の新
    # item_id が 4時間後に即再選定され、同一商品系譜を 1日に 2-3 回 End→Relist する暴走
    # ($1M枠消費 / listing 喪失)。inherit/discover が後段で失敗しても eBay 上 relist は
    # 成立済なので、ここで必ず記録する (daily_relist と同じ record_relist 経路)。
    try:
        from monitor.database import record_relist
        record_relist(old_item_id, new_item_id, sku, title, END_REASON, True)
    except Exception as _e:  # noqa: BLE001
        logger.error(
            "[ebaymag_relist] relist_history 記録失敗 (cooldown不発リスク) %s→%s: %s",
            old_item_id, new_item_id, _e,
        )
        _discord_notify(
            config,
            f"[eBaymag relist] relist_history 記録漏れ {old_item_id}→{new_item_id} "
            "(要手動 cooldown 確認)",
        )

    # ── ③ inherit_listing_on_relist (ebaymag_desired_sites_json 含む継承) ──
    # inherit_listing_on_relist は ebaymag_segment を継承するが、
    # ebaymag_desired_sites_json は継承列に含まれないため個別に UPDATE する。
    try:
        inherit_listing_on_relist(
            old_item_id=old_item_id,
            new_item_id=new_item_id,
            sku=sku,
            title=title,
            current_price=current_price,
            end_reason=END_REASON,
        )
        # ebaymag_desired_sites_json を新 item_id に継承 (inherit helper の継承列に含まれない)
        if desired_sites_json is not None:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE ebay_listings
                       SET ebaymag_desired_sites_json = ?,
                           ebaymag_desired_updated_at = CURRENT_TIMESTAMP
                       WHERE ebay_item_id = ?""",
                    (desired_sites_json, new_item_id),
                )
        # 送料 band を新 item_id へ継承 (§8 relist: band 継承)。weight は relist で
        # 変わらないため band も同一。新 item_id の applied_token は未設定 (= 付替が
        # 必要) のまま継承しない (新 listing は eBaymag 上で新規 = 付替やり直し)。
        with get_conn() as conn:
            _bandrow = conn.execute(
                "SELECT ebaymag_shipping_band FROM ebay_listings WHERE ebay_item_id=?",
                (old_item_id,),
            ).fetchone()
            _old_band = _bandrow["ebaymag_shipping_band"] if _bandrow else None
            if _old_band:
                conn.execute(
                    "UPDATE ebay_listings SET ebaymag_shipping_band=? "
                    "WHERE ebay_item_id=?",
                    (_old_band, new_item_id),
                )
    except Exception as e:  # noqa: BLE001
        # inherit 失敗でも relist は eBay 側では成功済。needs_manual + 継続判断
        err = f"inherit_listing_on_relist 失敗: {e} (new_eid={new_item_id} はeBay上で生存)"
        logger.error("[ebaymag_relist] %s", err)
        result["error_message"] = err
        _discord_notify(config, f"[eBaymag relist] inherit 失敗 needs_manual: {title[:40]}\n{err}")
        # relist 自体は成功なので新 item_id は記録して返す (孤児化防止)
        return result

    # ── ④ 旧 ebaymag_products を relisted マーク ──
    result["step"] = "mark_old"
    _mark_old_product_relisted(old_item_id)

    # ── ⑤ discover_product_id (新 item_id のeBaymag product_id を取得) ──
    result["step"] = "discover"
    time.sleep(_DISCOVER_WAIT_SEC)  # eBaymag 取込ラグ対策 (短い wait)

    # query: search_keyword または title
    query_for_discover = ""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT title, search_keyword FROM ebay_listings WHERE ebay_item_id=?",
                (new_item_id,),
            ).fetchone()
        if row:
            query_for_discover = (row["search_keyword"] or row["title"] or "").strip()
    except Exception as _e:  # noqa: BLE001
        logger.warning("[ebaymag_relist] discover query 取得失敗 eid=%s: %s", new_item_id, _e)
    if not query_for_discover:
        query_for_discover = title  # フォールバック

    logger.info("[ebaymag_relist] ⑤ discover: new_eid=%s query=%r", new_item_id, query_for_discover[:50])
    disc_result = discover_product_id(query_for_discover, expected_itm=new_item_id)

    if not (disc_result.ok and disc_result.product_id):
        # eBaymag 取込ラグ → Phase2 キュー委譲 (reason=relist_relink)
        disc_err = disc_result.error or "discover: 候補なし"
        logger.info(
            "[ebaymag_relist] discover 未発見 (取込ラグ) → Phase2 委譲 "
            "new_eid=%s error=%s", new_item_id, disc_err,
        )
        try:
            enqueue_ebaymag_apply(new_item_id, reason="relist_relink")
        except Exception as _e:  # noqa: BLE001
            logger.error("[ebaymag_relist] Phase2 enqueue 失敗 new_eid=%s: %s", new_item_id, _e)
        result["discover_delegated"] = True
        # discover 失敗は success ではないが、窓は許容範囲内 (Phase2 が継続)
        # eBay relist + inherit は成功しているので success=True に設定しない
        # (discover_delegated=True を確認して呼出側が適切に処理)
        result["step"] = "delegated_to_phase2"
        result["success"] = False
        result["error_message"] = f"discover 未発見 → Phase2 委譲 (query={query_for_discover!r})"
        return result

    new_product_id = disc_result.product_id
    logger.info("[ebaymag_relist] discover OK: new_eid=%s product_id=%s", new_item_id, new_product_id)

    # product_id を ebaymag_products に登録 (site_states は apply 後に確定)
    try:
        upsert_ebaymag_product(new_item_id, product_id=new_product_id)
    except Exception as _e:  # noqa: BLE001
        logger.error("[ebaymag_relist] upsert_ebaymag_product 失敗 eid=%s: %s", new_item_id, _e)

    # ── ⑥ desired 国へ apply_site_changes ──
    result["step"] = "apply"
    if not desired_sites:
        # desired が空 = '出さない' 相当。公開なしで完了
        logger.info("[ebaymag_relist] desired_sites 空 → 各国公開なし (ebaymag_segment はセグメント維持)")
        result["success"] = True
        result["step"] = "done_no_sites"
        return result

    # 実態を取得してから差分適用 (誤 OFF 防止)
    actual_result = fetch_site_states(new_product_id, expected_itm=new_item_id)
    if not actual_result.ok or not actual_result.site_states:
        # 取込直後は site_states が空の場合がある → Phase2 委譲
        logger.info(
            "[ebaymag_relist] fetch_site_states 空/失敗 → Phase2 委譲 "
            "new_eid=%s err=%s", new_item_id, actual_result.error,
        )
        try:
            enqueue_ebaymag_apply(new_item_id, reason="relist_relink")
        except Exception as _e:  # noqa: BLE001
            logger.error("[ebaymag_relist] Phase2 enqueue 失敗 new_eid=%s: %s", new_item_id, _e)
        result["discover_delegated"] = True
        result["step"] = "delegated_to_phase2_after_discover"
        result["success"] = False
        result["error_message"] = (
            f"fetch_site_states 空/失敗 (取込ラグ) → Phase2 委譲 "
            f"product_id={new_product_id}"
        )
        return result

    # MEDIUM (code-reviewer 2026-06-20): top-level import の SITE_MAP を使う
    # (関数内 re-import はテストの monkeypatch を無効化するため削除)。
    all_sites = set(SITE_MAP.keys())
    desired_set = {s for s in desired_sites if s in all_sites}
    actual_on = {s for s, v in actual_result.site_states.items() if v}
    turn_on = sorted(desired_set - actual_on)
    turn_off = sorted((actual_on - desired_set) & all_sites)

    if turn_on or turn_off:
        apply_result = apply_site_changes(new_product_id, new_item_id, turn_on, turn_off)
        if not apply_result.ok:
            err = f"apply_site_changes 失敗: {apply_result.error}"
            logger.error("[ebaymag_relist] %s new_eid=%s", err, new_item_id)
            # 部分公開状態の可能性 → Phase2 委譲して修正
            try:
                enqueue_ebaymag_apply(new_item_id, reason="relist_relink")
            except Exception as _e:  # noqa: BLE001
                logger.error("[ebaymag_relist] Phase2 enqueue 失敗: %s", _e)
            result["discover_delegated"] = True
            result["error_message"] = err + " → Phase2 委譲"
            result["step"] = "apply_failed_delegated"
            return result

        # 定着検証: apply 後の site_states を確認
        if apply_result.site_states:
            try:
                upsert_ebaymag_product(
                    new_item_id, product_id=new_product_id,
                    site_states=apply_result.site_states,
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning("[ebaymag_relist] upsert after apply 失敗 (継続): %s", _e)
            result["sites_applied"] = sorted(
                s for s, v in apply_result.site_states.items() if v
            )
        else:
            result["sites_applied"] = sorted(desired_set)
    else:
        # 既に目標状態
        result["sites_applied"] = sorted(actual_on)
        logger.info("[ebaymag_relist] 既に目標状態 new_eid=%s", new_item_id)

    result["success"] = True
    result["step"] = "done"
    logger.info(
        "[ebaymag_relist] 完了 old=%s → new=%s sites=%s",
        old_item_id, new_item_id, result["sites_applied"],
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# メインエントリ
# ──────────────────────────────────────────────────────────────────────────────

def run_ebaymag_relist(config: dict) -> dict:
    """W284 Phase 3: eBaymag-aware relist (窓ゼロ).

    feature flag (tasks_enabled.ebaymag_relist.enabled) が False の場合は即 skip。
    CDP+eBaymag ログイン不在時は skip + Discord 通知。
    処理対象: ebaymag_segment∈{全国,優先国,カスタム} かつ relist 条件を満たす listing。

    Returns:
        {"success": bool, "processed": int, "succeeded": int, "failed": int,
         "delegated": int, "skipped_reason": str | None, "message": str}
    """
    # ── feature flag チェック (既定 False) ──
    flag_cfg = (config.get("tasks_enabled") or {}).get("ebaymag_relist") or {}
    if not flag_cfg.get("enabled", False):
        msg = "ebaymag_relist: feature flag OFF (tasks_enabled.ebaymag_relist.enabled=false) — skip"
        logger.info("[ebaymag_relist] %s", msg)
        return {
            "success": True,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "delegated": 0,
            "skipped_reason": "feature_flag_off",
            "message": msg,
        }

    # ── CDP + eBaymag ログイン probe ──
    try:
        alive, probe_err = _probe_cdp_ebaymag()
    except Exception as e:  # noqa: BLE001
        alive, probe_err = False, str(e)

    if not alive:
        msg = (
            f"ebaymag_relist: CDP Chrome または eBaymag ログインが不在 → skip。"
            f"(error: {probe_err})"
        )
        logger.info("[ebaymag_relist] %s", msg)
        _discord_notify(
            config,
            "[eBaymag relist] CDP 不在のため relist をスキップ。"
            "CDP Chrome (localhost:9222) + eBaymag ログインを開いてください。",
        )
        return {
            "success": True,  # 正常 skip
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "delegated": 0,
            "skipped_reason": "cdp_absent",
            "message": msg,
        }

    # ── eBay 資格情報 ──
    creds = _get_ebay_credentials(config)
    if creds is None:
        msg = "ebaymag_relist: eBay 資格情報が不正 → skip"
        logger.error("[ebaymag_relist] %s", msg)
        return {
            "success": False,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "delegated": 0,
            "skipped_reason": "credentials_invalid",
            "message": msg,
        }

    # ── 対象選出 ──
    max_per_run: int = int(flag_cfg.get("max_per_run", _MAX_PER_RUN_DEFAULT))
    cooldown_days: int = int(
        (config.get("tasks_enabled") or {}).get("daily_relist", {}).get("cooldown_days", 10)
    )
    targets = _select_ebaymag_relist_targets(limit=max_per_run, cooldown_days=cooldown_days)

    if not targets:
        msg = "ebaymag_relist: 対象 listing なし (relist 条件を満たす eBaymag 商品がありません)"
        logger.info("[ebaymag_relist] %s", msg)
        return {
            "success": True,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "delegated": 0,
            "skipped_reason": "no_targets",
            "message": msg,
        }

    logger.info("[ebaymag_relist] 対象 %d 件 (max_per_run=%d)", len(targets), max_per_run)

    stats = {"processed": 0, "succeeded": 0, "failed": 0, "delegated": 0}
    result_details: list[dict] = []

    for target in targets:
        stats["processed"] += 1
        eid = target["ebay_item_id"]
        try:
            res = _process_single_relist(target, creds, config)
        except Exception as e:  # noqa: BLE001
            err_msg = f"{type(e).__name__}: {e}"
            logger.error("[ebaymag_relist] 予期せぬ例外 eid=%s: %s", eid, err_msg, exc_info=True)
            res = {
                "old_item_id": eid,
                "new_item_id": None,
                "success": False,
                "step": "unexpected_exception",
                "error_message": err_msg,
                "discover_delegated": False,
                "sites_applied": [],
            }
            _discord_notify(
                config,
                f"[eBaymag relist] 予期せぬ例外 needs_manual: {target.get('title', '')[:40]}\n{err_msg}",
            )

        result_details.append(res)
        if res.get("success"):
            stats["succeeded"] += 1
        elif res.get("discover_delegated"):
            stats["delegated"] += 1
        else:
            stats["failed"] += 1

        # 件間 sleep (eBay API rate limit 対策)
        sleep_sec = int(
            (config.get("tasks_enabled") or {}).get("daily_relist", {}).get("sleep_between_sec", 3)
        )
        if stats["processed"] < len(targets):
            time.sleep(sleep_sec)

    # ── HIGH-2 (code-reviewer 2026-06-20): 委譲分を同 run の CDP 在席中に即消化 ──
    # discover ラグで Phase2 委譲した relist_relink ジョブは、Phase2 定時 (:30:00) が
    # Phase3 (:30:30) より前に実行済のため同回では拾われず、次発火まで最大4時間「各国版
    # 未公開」の窓が開く (設計の窓ゼロと乖離)。ここで明示的に1回キュー消化を試み窓を縮める。
    if stats["delegated"] > 0:
        try:
            from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
            logger.info(
                "[ebaymag_relist] 委譲 %d 件 → 同 run でキュー即時消化を試行 (窓最小化)",
                stats["delegated"],
            )
            run_ebaymag_apply_queue(config)
        except Exception as _e:  # noqa: BLE001
            logger.error(
                "[ebaymag_relist] 委譲分の即時消化に失敗 (次の Phase2 定時で再試行): %s", _e
            )

    # ── 結果サマリー通知 ──
    failed_items = [r for r in result_details if not r.get("success") and not r.get("discover_delegated")]
    if failed_items:
        failed_titles = [
            f"{r.get('old_item_id')} → {r.get('error_message', '')[:60]}"
            for r in failed_items[:3]
        ]
        _discord_notify(
            config,
            f"[eBaymag relist] 失敗 {stats['failed']}件:\n" + "\n".join(failed_titles),
        )

    success = stats["failed"] == 0
    msg = (
        f"ebaymag_relist: processed={stats['processed']} "
        f"succeeded={stats['succeeded']} "
        f"delegated={stats['delegated']} "
        f"failed={stats['failed']}"
    )
    logger.info("[ebaymag_relist] 完了: %s", msg)

    return {
        "success": success,
        "processed": stats["processed"],
        "succeeded": stats["succeeded"],
        "failed": stats["failed"],
        "delegated": stats["delegated"],
        "skipped_reason": None,
        "message": msg,
    }
