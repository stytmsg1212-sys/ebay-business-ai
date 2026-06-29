#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日次 End→Relist SEO ブースト タスク

Watch が付いていない & rank=E の listing を毎日 7件、手動で End→Relist することで
eBay アクティブセラー評価を高め、検索順位の底上げを狙う。

選定条件 (AND):
  - is_ended=0 かつ quantity_ebay>=1
  - watch_count=0 (売れていない)
  - rank='E' (低ランク)
  - 直近 cooldown_days (既定30 / 現行 config=10) 内に relist_history に
    old/new ItemID として出現していない (cooldown、config で調整可)

並び順:
  1. time_left_seconds 小 → 大 (自動relistが近いもの先取り)
  2. start_time 古 → 新 (古い listing 優先で鮮度更新)

処理フロー:
  1. 対象 listing (ebay_item_id 単位) を最大 max_per_run 件選出
  2. VerifyRelistFixedPriceItem で dry-run（任意、config で on/off）
  3. EndItem で listing 終了 (EndingReason=Incorrect)
  4. RelistFixedPriceItem で再出品 → 新 ItemID 取得
  5. relist_history 記録、ebay_listings の新 ItemID へ差し替え
  6. 秘書 inbox に日次レポート

安全装置:
  - config で max_per_run を制限（デフォルト 7）
  - 1件失敗しても他は続行、結果は secretary 通知
  - dry_run=true モードで本番影響なしで検証可
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import (  # noqa: E402
    get_conn,
    record_relist,
    update_ebay_listing_sku,
    upsert_ebay_listing,
)
from monitor.credentials import get_ebay_credentials, ebay_credentials_ok  # noqa: E402
from monitor.ebay_client import (  # noqa: E402
    end_item, relist_item, verify_relist_item,
)
from monitor.task_execution_log import is_completed_today  # noqa: E402
from monitor import cdp_lock  # noqa: E402

# プロセス間排他ロック (Layer3 / 並行二重 relist 防止)。
# msvcrt advisory lock: プロセス死で OS 自動解放、stale デッドロックなし。
_DAILY_RELIST_LOCK = Path(__file__).resolve().parent.parent / "data" / "daily_relist.lock"

logger = logging.getLogger(__name__)

END_REASON = "Incorrect"  # "listing details are incorrect" = SEO boost 目的に無難


def _select_relist_targets(limit: int = 7, cooldown_days: int = 30) -> list[dict]:
    """選出クエリ。条件: watch=0 / rank=E / cooldown N日 / active / 出さない限定。
    並び順: time_left 小 → start_time 古。

    cooldown_days (2026-06-07 config 化): 同一 listing を再 relist しない日数。
    プール(watch=0 & rank=E)が小さいと 30 日では供給不足で max_per_run に届かない
    ため config で調整可能にした (実効上限 ≒ プール件数 / cooldown_days)。
    SQL injection 防止のため int 化して `datetime('now', ?)` にバインドする。

    識別キーは ebay_item_id (sku-rules.md: SKU は listing 識別キーに使わない)。
    W284 (2026-06-20): #5 (SKU 条件) を撤廃。SKU 空の産業機器等 17 件が
    relist プールから除外されていたため対象化。#6 (ebaymag_segment='出さない')
    は維持し eBaymag 各国版の relist 衝突を防ぐ。
    """
    # int 化で安全な modifier 文字列を構築 (例: '-10 days')。負値/0 は最小 1 日に矯正。
    cd = f"-{max(1, int(cooldown_days))} days"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku, title, watch_count, rank,
                      time_left_seconds, start_time, current_price
               FROM ebay_listings
               WHERE (is_ended IS NULL OR is_ended=0)
                 AND quantity_ebay >= 1
                 AND watch_count = 0
                 AND rank = 'E'
                 -- W242 (2026-06-09): eBaymag 対象 (全国/優先国) は relist (end→sell
                 -- similar で新 ItemID 化) すると eBaymag 各国版とのリンクが壊れるため
                 -- 除外。'出さない' (eBaymag 非対象=US本体のみ) のみ relist する。
                 -- NULL (未分類) も安全側で除外 (eBaymag 上か不明な listing は触らない)。
                 AND ebaymag_segment = '出さない'
                 AND ebay_item_id NOT IN (
                     SELECT old_item_id FROM relist_history
                     WHERE created_at >= datetime('now', ?)
                       AND success = 1  -- FINDING 4: 失敗 relist で cooldown 発火させない
                 )
                 AND ebay_item_id NOT IN (
                     SELECT new_item_id FROM relist_history
                     WHERE new_item_id IS NOT NULL
                       AND created_at >= datetime('now', ?)
                       AND success = 1  -- 同上
                 )
               ORDER BY
                 COALESCE(time_left_seconds, 9999999) ASC,
                 COALESCE(start_time, '9999-12-31') ASC
               LIMIT ?""",
            (cd, cd, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def inherit_listing_on_relist(
    old_item_id: str,
    new_item_id: str,
    sku: str,
    title: str,
    current_price: float,
    end_reason: str = END_REASON,
    success: bool = True,
) -> dict:
    """Sell Similar relist 時の永続データ継承 + 関連テーブル ebay_item_id 追従 + 履歴記録.

    1 トランザクション原子化. 失敗時は呼出側で握る.

    ## Precondition (呼出側の責務)

    **本 helper は relist API が成功 (new_item_id を eBay から取得済) のケースのみ呼出**.
    失敗パス (Verify 失敗 / EndItem 失敗 / Relist 全試行失敗) では `record_relist(...,
    success=False, ...)` を呼ぶこと. 直接 helper を呼ぶと `relist_history.success=1` が
    刻まれ cooldown が誤って発火する (= 同 listing が 30 日間 relist 対象から外れる
    silent skip リスク).

    success=False を渡す経路は本 helper の責務外なので、必ず別 path で record_relist を
    呼んでから return すること.

    ## 継承列 (SKU 固有で物理商品が同じ + user 設定の永続値):
      物理属性:        weight_g / weight_source / weight_confidence / weight_estimated_at
                       length_cm / width_cm / height_cm / includes / warranty
      在庫・仕入:      quantity_ebay / source / source_url / classification / purchase_yen
      W119 検索ワード: search_keyword / search_keyword_source / search_keyword_generated_at
      W98 最安値設定:  lp_min_price / lp_breakeven_usd
      W110 市場分析:   primary_market / us_buyer_ratio / market_analysis_at /
                       market_sample_size
      W242 eBaymag:    ebaymag_segment (継承しないと新 ItemID が NULL になり
                       relist プールから永久離脱 → daily_relist 無言枯渇)

    継承しない (ライフサイクルでリセット):
      watch_count / view_count / sales_count_30d / rank / metrics_score
      source_status / source_last_checked / source_out_of_stock_since
      is_ended (0 リセット) / time_left_seconds / start_time

    関連テーブル更新:
      - supplier_candidates: pending/accepted のみ ebay_item_id 追従 (rejected/applied は履歴)
      - monitored_items: ebay_item_id 追従 (SKU 経由禁止、有在庫 SKU 共有時に他 listing 破壊)
      - competitor_products: is_active=1 のみ our_item_id 追従
        (relist 時に競合登録が孤立すると W183 値下げ pipeline が機能停止 = 金銭損失リスク)
      - listing_notes: W140 メモ (発送/通関の注意点) を旧→新へ引き継ぐ
        (End→Sell similar で ItemID が変わってもメモが残る。user 確定
        2026-05-19 = 引き継ぎ。新側に既存メモがあれば尊重 = 上記
        INSERT OR IGNORE と同方針)
      - keyword_watches (W206): ebay_item_id 追従。任意紐付けが relist で孤立しないように。

    Returns: {"inherited_columns": int, "competitor_rows": int, "supplier_rows": int,
              "monitored_rows": int, "note_rows": int, "keyword_watch_rows": int}

    出典: 2026-05-11 W119 ふりかえりで silent skip 発見 (lp_min_price 消失 / 競合孤立).
    """
    inherited_count = 0
    competitor_rows = 0
    supplier_rows = 0
    monitored_rows = 0
    note_rows = 0
    keyword_watch_rows = 0
    with get_conn() as conn:
        # OLD 行から永続データ取得 (継承列をフラットに SELECT)
        old_row = conn.execute(
            """SELECT quantity_ebay, weight_g, weight_source, weight_confidence,
                      weight_estimated_at, length_cm, width_cm, height_cm,
                      includes, warranty, source, source_url, classification,
                      purchase_yen,
                      search_keyword, search_keyword_source, search_keyword_generated_at,
                      lp_min_price, lp_breakeven_usd,
                      primary_market, us_buyer_ratio, market_analysis_at,
                      market_sample_size, ebaymag_segment
               FROM ebay_listings WHERE ebay_item_id=?""",
            (old_item_id,),
        ).fetchone()
        old_data = dict(old_row) if old_row else {}

        # 新 ItemID INSERT (INSERT OR IGNORE: 万一既存なら継承スキップ)
        cur = conn.execute(
            """INSERT OR IGNORE INTO ebay_listings (
                ebay_item_id, sku, title, current_price, quantity_ebay,
                weight_g, weight_source, weight_confidence, weight_estimated_at,
                length_cm, width_cm, height_cm, includes, warranty,
                source, source_url, classification,
                purchase_yen,
                search_keyword, search_keyword_source, search_keyword_generated_at,
                lp_min_price, lp_breakeven_usd,
                primary_market, us_buyer_ratio, market_analysis_at,
                market_sample_size, ebaymag_segment,
                last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP)""",
            (
                new_item_id, sku or "", title or "",
                current_price or 0.0,
                old_data.get("quantity_ebay") or 1,
                old_data.get("weight_g") or 0,
                old_data.get("weight_source"),
                old_data.get("weight_confidence"),
                old_data.get("weight_estimated_at"),
                old_data.get("length_cm") or 0,
                old_data.get("width_cm") or 0,
                old_data.get("height_cm") or 0,
                old_data.get("includes"),
                old_data.get("warranty"),
                old_data.get("source"),
                old_data.get("source_url"),
                old_data.get("classification"),
                old_data.get("purchase_yen"),
                old_data.get("search_keyword"),
                old_data.get("search_keyword_source"),
                old_data.get("search_keyword_generated_at"),
                old_data.get("lp_min_price"),
                old_data.get("lp_breakeven_usd"),
                old_data.get("primary_market"),
                old_data.get("us_buyer_ratio"),
                old_data.get("market_analysis_at"),
                old_data.get("market_sample_size"),
                old_data.get("ebaymag_segment"),
            ),
        )
        if cur.rowcount == 0:
            # 既存行あって INSERT OR IGNORE で skip された場合 (異常系).
            # relist API は新 ItemID を返したはずなので、本来このケースは起きない.
            logger.warning(
                f"⚠ new_item_id={new_item_id} が既に ebay_listings に存在. "
                f"継承が skip された可能性あり. next ebay_sync で補修される見込み."
            )
        else:
            inherited_count = 1

        # 関連テーブル ebay_item_id 参照更新
        # - supplier_candidates: 同一 SKU のため新 ItemID に継承 (pending/accepted のみ).
        #   rejected/applied は履歴なので触らない.
        cur_sc = conn.execute(
            """UPDATE supplier_candidates SET ebay_item_id=?
               WHERE ebay_item_id=? AND status IN ('pending','accepted')""",
            (new_item_id, old_item_id),
        )
        supplier_rows = cur_sc.rowcount

        # - monitored_items: relist 対象 listing のみ追従. SKU 経由 UPDATE は
        #   有在庫 SKU 共有時に無関係 listing の追跡を破壊するため ebay_item_id 経由で限定
        #   (W68 Step 2 / .claude/rules/sku-rules.md)
        cur_mi = conn.execute(
            "UPDATE monitored_items SET ebay_item_id=? WHERE ebay_item_id=?",
            (new_item_id, old_item_id),
        )
        monitored_rows = cur_mi.rowcount

        # - competitor_products: W183 値下げ pipeline の競合登録を新 listing に追従.
        #   is_active=1 のみ移動 (inactive 化された旧競合は history、触らない).
        #   relist の度に W183 が「該当 listing の競合 0 件」になる silent skip を防ぐ.
        cur_cp = conn.execute(
            """UPDATE competitor_products SET our_item_id=?
               WHERE our_item_id=? AND is_active=1""",
            (new_item_id, old_item_id),
        )
        competitor_rows = cur_cp.rowcount

        # - keyword_watches (W206): UI から任意紐付けされた eBay Item ID を追従.
        #   relist で旧 ItemID が新 ItemID に切替わった時、Discord 通知 embed の
        #   「eBay 販売価格」併記が引き続き機能するように.
        cur_kw = conn.execute(
            "UPDATE keyword_watches SET ebay_item_id=? WHERE ebay_item_id=?",
            (new_item_id, old_item_id),
        )
        keyword_watch_rows = cur_kw.rowcount

        # - listing_notes: W140 メモを旧→新 ebay_item_id へ引き継ぐ.
        #   End→Sell similar で ItemID が変わってもメモ (発送/通関の注意点)
        #   が残るように (user 確定 2026-05-19 = 引き継ぎ). 空メモ
        #   (TRIM='') はコピーしない. 新側に既存メモがあれば DO NOTHING
        #   (= 上記 ebay_listings INSERT OR IGNORE と同じ「既存は尊重」方針).
        cur_ln = conn.execute(
            """INSERT INTO listing_notes (ebay_item_id, note_text, updated_at)
               SELECT ?, note_text, datetime('now') FROM listing_notes
               WHERE ebay_item_id=? AND note_text IS NOT NULL
                 AND TRIM(note_text) != ''
               ON CONFLICT(ebay_item_id) DO NOTHING""",
            (new_item_id, old_item_id),
        )
        note_rows = cur_ln.rowcount

        # 履歴記録. success は呼出側責任で渡されること (default True、precondition 参照).
        conn.execute(
            """INSERT INTO relist_history
               (old_item_id, new_item_id, sku, title, end_reason, success)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (old_item_id, new_item_id, sku, title, end_reason, 1 if success else 0),
        )

    return {
        "inherited_columns": inherited_count,
        "competitor_rows": competitor_rows,
        "supplier_rows": supplier_rows,
        "monitored_rows": monitored_rows,
        "note_rows": note_rows,
        "keyword_watch_rows": keyword_watch_rows,
    }


def _notify_secretary(results: list[dict], summary: str) -> None:
    """秘書 inbox に日次レポート追記。失敗しても主処理は続行。"""
    try:
        inbox_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company" / "secretary" / "inbox"
        if not inbox_dir.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H:%M")
        f = inbox_dir / f"{today}.md"

        lines = [f"\n## {ts} 日次 End→Relist SEO ブースト", f"\n{summary}\n"]
        for r in results:
            status_icon = "OK" if r.get("success") else "NG"
            lines.append(
                f"- [{status_icon}] **{(r.get('title') or '')[:60]}** "
                f"({r.get('old_item_id')} → {r.get('new_item_id') or '-'})"
            )
            if not r.get("success"):
                lines.append(f"  - エラー: {r.get('error_message', '')[:200]}")
        block = "\n".join(lines) + "\n"

        if f.exists():
            f.write_text(f.read_text(encoding='utf-8') + block, encoding='utf-8')
        else:
            f.write_text(f"# {today} 秘書 inbox\n" + block, encoding='utf-8')
    except Exception as e:
        logger.debug(f"秘書通知失敗: {e}")


def process_single_relist(
    target: dict, creds: dict, dry_run: bool = False,
    skip_verify: bool = False,
) -> dict:
    """1件分の End→Relist。

    dry_run=True: VerifyRelistFixedPriceItem のみ実行、実listingは変更しない
    skip_verify=True: Verify を飛ばして直接 End→Relist（本番時）
    """
    old_item_id = target["ebay_item_id"]
    sku = target.get("sku")
    title = target.get("title")
    result = {
        "old_item_id": old_item_id, "new_item_id": None,
        "sku": sku, "title": title, "success": False, "error_message": None,
        "dry_run": dry_run,
    }

    # Step A: Verify (dry-run 相当、relist 可能性チェック)
    if not skip_verify:
        logger.info(f"[verify] ItemID {old_item_id}...")
        v = verify_relist_item(old_item_id, **{k: creds[k] for k in ("app_id", "dev_id", "cert_id", "user_token")})
        if not v.get("success"):
            result["error_message"] = f"VerifyRelist失敗: {v.get('message')}"
            logger.warning(result["error_message"])
            record_relist(old_item_id, None, sku, title, END_REASON, False, result["error_message"])
            return result
        logger.info(f"  Verify OK: fees preview = {(v.get('fees') or [])[:3]}")

    if dry_run:
        # dry-run モードは Verify までで終了
        result["success"] = True
        result["error_message"] = "dry_run: Verify のみ実行。EndItem/RelistItem はスキップ"
        return result

    # Step B: EndItem
    logger.info(f"[end] ItemID {old_item_id}...")
    end_r = end_item(old_item_id, **{k: creds[k] for k in ("app_id", "dev_id", "cert_id", "user_token")},
                     end_reason=END_REASON)
    if not end_r.get("success"):
        result["error_message"] = f"EndItem失敗: {end_r.get('message')}"
        logger.error(result["error_message"])
        record_relist(old_item_id, None, sku, title, END_REASON, False, result["error_message"])
        return result

    # EndItem 成功時点で即座にDBへ反映（Relistが失敗しても旧listingの状態は正しく保つ）
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET is_ended=1, ended_at=CURRENT_TIMESTAMP, "
                "ended_reason='daily_relist_seo' WHERE ebay_item_id=?",
                (old_item_id,),
            )
    except Exception as _e:
        logger.warning(f"旧ItemID is_ended 更新失敗 (継続): {_e}")

    time.sleep(2)  # eBay 側の反映待ち

    # Step C: RelistFixedPriceItem — リトライ付き
    new_item_id = None
    last_err = None
    for attempt in range(1, 4):  # 最大3試行
        logger.info(f"[relist] ItemID {old_item_id} (attempt {attempt}/3)...")
        rel = relist_item(old_item_id, **{k: creds[k] for k in ("app_id", "dev_id", "cert_id", "user_token")})
        if rel.get("success"):
            new_item_id = rel.get("new_item_id")
            break
        last_err = rel.get("message")
        logger.warning(f"Relist attempt {attempt} 失敗: {last_err}")
        if attempt < 3:
            time.sleep(5 * attempt)  # バックオフ

    if not new_item_id:
        result["error_message"] = (
            f"Relist全失敗 (3試行): {last_err} — ⚠ 旧ItemID {old_item_id} はend済、"
            f"eBay Seller Hub から手動で Sell Similar 実行が必要"
        )
        logger.error(result["error_message"])
        record_relist(old_item_id, None, sku, title, END_REASON, False, result["error_message"])
        return result

    result["new_item_id"] = new_item_id
    result["success"] = True
    logger.info(f"  New ItemID: {new_item_id}")

    # Step D: 新 ItemID の upsert + 永続データ継承 + 履歴記録 — 1トランザクションで原子化.
    # 詳細な継承列リストと根拠は inherit_listing_on_relist() docstring 参照.
    try:
        inherit_result = inherit_listing_on_relist(
            old_item_id=old_item_id,
            new_item_id=new_item_id,
            sku=sku,
            title=title,
            current_price=target.get("current_price") or 0.0,
            end_reason=END_REASON,
        )
        # observability: 引継ぎ件数を result に載せて report で見えるように.
        result["inherit_competitor_rows"] = inherit_result.get("competitor_rows", 0)
        result["inherit_supplier_rows"] = inherit_result.get("supplier_rows", 0)
    except Exception as _e:
        # H2 fix (2026-05-11 code-reviewer): helper 内 INSERT INTO relist_history が失敗した場合、
        # cooldown が発火せず同 listing が翌日も relist 対象に選ばれる silent skip リスクあり.
        # logger.exception でスタックトレース残し + record_relist fallback で cooldown 死守.
        logger.exception(f"⚠ DB 追従エラー (eBay 側 relist は成功). fallback で record_relist 実行")
        try:
            record_relist(old_item_id, new_item_id, sku, title, END_REASON, True,
                          f"helper failed but eBay relist succeeded: {_e}")
        except Exception as _e2:
            logger.error(f"⚠⚠ record_relist fallback も失敗: {_e2}. "
                         f"old_item_id={old_item_id} new_item_id={new_item_id} cooldown 未発火、"
                         f"翌日 cron で重複 End→Relist リスクあり、手動 SELECT で確認推奨")

    return result


def run_daily_relist(config: dict) -> dict:
    """daily_scheduler から呼ばれる entry point。"""
    task_cfg = (config or {}).get("tasks_enabled", {}).get("daily_relist") or {}
    max_per_run = int(task_cfg.get("max_per_run", 7))
    dry_run = bool(task_cfg.get("dry_run", False))
    skip_verify = bool(task_cfg.get("skip_verify", True))  # 本番は active listing から開始でVerify不要
    sleep_between = float(task_cfg.get("sleep_between_sec", 3))
    cooldown_days = int(task_cfg.get("cooldown_days", 30))  # 2026-06-07 config 化 (既定30、現行10)

    # run-once guard (Layer2 money セーフティネット):
    # 当日すでに daily_relist が completed(success=1) なら即 return (relist せず)。
    # completed のみを見て in-flight (started/NULL) は見ない — 自己検出回避。
    # 理由: run_task が log_task_start で 'started' を記録した後に本関数が呼ばれるため、
    # in-flight を含めると 1 回目の実行が自己 skip してしまう。
    if is_completed_today("daily_relist"):
        logger.info(
            "[daily_relist] run-once guard: 当日すでに completed(success=1) が記録済。"
            "autofix 等による二重起動を阻止。"
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "already completed today (run-once guard)",
            "processed": 0,
        }

    # Layer3 プロセス間排他ロック:
    # Layer2 (completed のみ) では 02:30 バッチが未到達の瞬間に 04:00 autofix が
    # 同時突入するレースを塞げない。msvcrt advisory lock (プロセス死で OS 自動解放)
    # で物理的に 1 プロセスのみが relist 本体に入れるよう保護する。
    try:
        with cdp_lock.acquire(blocking=False, lock_path=_DAILY_RELIST_LOCK):
            creds = get_ebay_credentials(config)
            if not ebay_credentials_ok(creds):
                return {"success": False, "message": "eBay 認証情報不足", "processed": 0}

            targets = _select_relist_targets(limit=max_per_run, cooldown_days=cooldown_days)
            logger.info(
                f"End→Relist 対象: {len(targets)}件 "
                f"(dry_run={dry_run}, max={max_per_run}, cooldown={cooldown_days}日)"
            )

            # W242 (2026-06-09): ebaymag_segment=NULL (未分類) の relist 候補が滞留していると
            # relist プールから漏れ続ける (silent skip)。market_analysis_refresh の segment
            # 再計算が止まっている時に user が気付けるよう件数を可視化 (Q0 痕跡)。
            # W284 (2026-06-20): #5(SKU条件) 撤廃に合わせ、SKU 条件を除去。
            # NULL 滞留判定は ebaymag_segment のみで行う (識別キー = ebay_item_id)。
            try:
                with get_conn() as _c:
                    _null_pool = _c.execute(
                        "SELECT COUNT(*) FROM ebay_listings WHERE (is_ended IS NULL OR is_ended=0) "
                        "AND quantity_ebay>=1 AND watch_count=0 AND rank='E' "
                        "AND ebaymag_segment IS NULL"
                    ).fetchone()[0]
                if _null_pool:
                    logger.warning(
                        f"⚠ relist 候補のうち ebaymag_segment=NULL が {_null_pool}件滞留 "
                        f"(未分類で relist 対象外)。market_analysis_refresh の区分再計算が"
                        f"動いているか確認を。"
                    )
            except Exception as e:  # noqa: BLE001 — 可視化失敗は relist 本体を妨げない
                logger.debug(f"null-segment 可視化 skip: {e}")

            if not targets:
                return {"success": True, "message": "対象listingなし", "processed": 0, "results": []}

            results = []
            ok = 0
            fail = 0
            for i, t in enumerate(targets, 1):
                logger.info(f"--- [{i}/{len(targets)}] {t.get('title','')[:60]} ---")
                r = process_single_relist(t, creds, dry_run=dry_run, skip_verify=skip_verify)
                results.append(r)
                if r.get("success"):
                    ok += 1
                else:
                    fail += 1
                if i < len(targets) and sleep_between > 0:
                    time.sleep(sleep_between)

            summary = f"{ok}/{len(targets)} 件成功 / {fail} 件失敗"
            if dry_run:
                summary = f"[DRY-RUN] {summary}"
            _notify_secretary(results, summary)
            logger.info(f"End→Relist 完了: {summary}")
            # FINDING 9: partial failure を全件失敗と区別 (alert noise 解消、true crash のみ alert).
            return {
                "success": ok > 0,  # 1 件でも成功したら True (= alert なし); 全件失敗時のみ False
                "processed": len(targets),
                "ok": ok, "failed": fail,
                "partial_failure": fail > 0 and ok > 0,
                "dry_run": dry_run,
                "results": results,
                "message": summary,
            }
    except cdp_lock.LockBusy:
        logger.info(
            "[daily_relist] 並行 run 検出 → skip (daily_relist.lock 保持中)。"
            "別の daily_relist run がすでに実行中のため relist をスキップ。"
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "another daily_relist run in progress (lock held)",
            "processed": 0,
        }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", help="単一 ItemID で実行（テスト用）")
    parser.add_argument("--dry-run", action="store_true", help="VerifyRelistのみ、実listing変更なし")
    parser.add_argument("--skip-verify", action="store_true", help="Verifyスキップして本番実行")
    args = parser.parse_args()

    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    if args.single:
        creds = get_ebay_credentials(cfg)
        if not ebay_credentials_ok(creds):
            print("ERROR: eBay 認証情報不足")
            sys.exit(1)
        # 対象を直接指定
        with get_conn() as c:
            row = c.execute(
                "SELECT ebay_item_id, sku, title, current_price FROM ebay_listings WHERE ebay_item_id=?",
                (args.single,),
            ).fetchone()
        if not row:
            print(f"ERROR: ItemID {args.single} not found in ebay_listings")
            sys.exit(1)
        target = dict(row)
        r = process_single_relist(target, creds, dry_run=args.dry_run, skip_verify=args.skip_verify)
        print(json.dumps(r, indent=2, ensure_ascii=False).encode('utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8', errors='replace'))
    else:
        cfg.setdefault("tasks_enabled", {}).setdefault("daily_relist", {})
        if args.dry_run:
            cfg["tasks_enabled"]["daily_relist"]["dry_run"] = True
        r = run_daily_relist(cfg)
        # CP932 コンソール対策: 半角記号等 (½など) を stdout encoding で encode失敗しないよう通す
        _out = json.dumps(r, indent=2, ensure_ascii=False, default=str)
        _enc = sys.stdout.encoding or 'utf-8'
        print(_out.encode(_enc, errors='replace').decode(_enc, errors='replace'))
