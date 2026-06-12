#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: データストア統合
sku_conversion_results.json のデータを monitor.db の ebay_listings に統合する。
inventory_check_results.json の在庫ステータスも ebay_listings に反映する。

これにより、2つの分離したデータストアを1つのDBに統一する。
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

from monitor.database import get_conn  # noqa: E402  WAL+busy_timeout適用で安全

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / 'data' / 'monitor.db'  # 後方互換（他モジュール参照用）


def sync_sku_conversion_to_db() -> Dict:
    """
    sku_conversion_results.json → ebay_listings テーブルに統合

    source, source_url, classification カラムを更新する。
    """
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
    if not sku_file.exists():
        logger.warning("sku_conversion_results.json が見つかりません")
        return {'updated': 0, 'not_found': 0}

    with open(sku_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = get_conn()
    updated = 0
    not_found = 0

    # sourced items
    for item in data.get('sourced', []):
        ebay_id = item.get('ebay_id', '')
        if not ebay_id:
            continue

        result = conn.execute(
            "SELECT id FROM ebay_listings WHERE ebay_item_id=?", (ebay_id,)
        ).fetchone()

        if result:
            conn.execute(
                """UPDATE ebay_listings SET source=?, source_url=?, classification='sourced'
                   WHERE ebay_item_id=?""",
                (item.get('source', ''), item.get('source_url', ''), ebay_id),
            )
            updated += 1
        else:
            not_found += 1

    # self_stock items
    for item in data.get('self_stock', []):
        ebay_id = item.get('ebay_id', '')
        if not ebay_id:
            continue

        result = conn.execute(
            "SELECT id FROM ebay_listings WHERE ebay_item_id=?", (ebay_id,)
        ).fetchone()

        if result:
            conn.execute(
                """UPDATE ebay_listings SET classification='self_stock'
                   WHERE ebay_item_id=?""",
                (ebay_id,),
            )
            updated += 1

    conn.commit()
    conn.close()

    logger.info(f"SKU変換データ統合: {updated}件更新, {not_found}件未一致")
    return {'updated': updated, 'not_found': not_found}


def _should_auto_zero(status: str, prev_status) -> bool:
    """仕入先OOS → eBay在庫 自動0化 の候補判定 (純関数、2026-06-05)。

    - ページなし: 即時 (Yahoo等のページ消滅 = 確定終了。'エラー'(fetch失敗) とは別分類で信頼度高)。
    - 在庫無: prev も在庫無 = 2回連続検知のみ (一時的 scrape 誤検知で正常listingを0化しない)。
    - それ以外 (在庫有/unknown/エラー/初回在庫無): 0化しない。
    """
    if status == 'ページなし':
        return True
    if status == '在庫無' and prev_status == '在庫無':
        return True
    return False


def sync_inventory_status_to_db() -> Dict:
    """
    inventory_check_results.json → ebay_listings テーブルに反映

    source_status, source_last_checked, source_out_of_stock_since を更新する。
    """
    inv_file = BASE_DIR / 'data' / 'inventory_check_results.json'
    if not inv_file.exists():
        logger.warning("inventory_check_results.json が見つかりません")
        return {'updated': 0, 'not_found': 0}

    with open(inv_file, 'r', encoding='utf-8') as f:
        inv_data = json.load(f)

    results = inv_data.get('results', [])
    if not results:
        return {'updated': 0, 'not_found': 0}

    conn = get_conn()
    updated = 0
    not_found = 0
    # 2026-06-05: 仕入先OOS → eBay在庫 自動0化 の候補 (履行不能販売の防止)。
    # 仕入先(Yahoo等)が売切なのに eBay在庫が残り売れてしまう事故の恒久対策。
    # ページなし = 即時 (ページ消滅 = 確定終了)。在庫無 = prev も在庫無 (2回連続) で誤検知回避。
    oos_to_zero: list[str] = []

    for item in results:
        sku = item.get('sku', '')  # log 用 (識別 key としては使わない、SKU rule 準拠)
        url = (item.get('url') or '').strip()
        json_eid = (item.get('ebay_id') or '').strip()

        if not url and not json_eid:
            # 識別 key 不在 → silent skip 防止のため警告 log + 計上
            not_found += 1
            logger.warning(
                f"sync_inventory_status_to_db: identifier 不在 "
                f"(url + ebay_id 両方欠落) sku={sku!r}"
            )
            continue

        status = item.get('status', '不明')
        checked_at = item.get('checked_at', '')

        # 2026-05-01 W75 4c: 旧 SKU 経由 lookup (SKU rule 違反) を解消.
        # 優先順位: (1) JSON ebay_id 直接利用 (2) source_url 逆引き
        # 旧 SQL は SKU で listing 1 件特定 → 同 SKU 多 listing (有在庫 stock*)
        # ケースで非決定論動作だった. 本データは ebay* SKU のみ (stock* は inventory_check
        # スコープ外) で実質的影響は無いが、ルール上の根本是正.
        if json_eid:
            row = conn.execute(
                "SELECT ebay_item_id, source_status, source_out_of_stock_since "
                "FROM ebay_listings WHERE ebay_item_id=?",
                (json_eid,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ebay_item_id, source_status, source_out_of_stock_since "
                "FROM ebay_listings "
                "WHERE source_url=? AND (is_ended IS NULL OR is_ended=0) "
                "LIMIT 1",
                (url,),
            ).fetchone()

        if not row:
            not_found += 1
            continue

        ebay_item_id = row[0]
        prev_status = row[1]

        # BUG-2: source_out_of_stock_since の管理は ebay_sync.match_source_status_to_ebay に一元化
        # 以前はここで checked_at を使っていたが、ebay_sync 側が CURRENT_TIMESTAMP で
        # セットする値と競合し上書きされる問題があった。この関数は status と last_checked のみ更新。
        conn.execute(
            """UPDATE ebay_listings SET
               source_status=?, source_last_checked=?
               WHERE ebay_item_id=?""",
            (status, checked_at, ebay_item_id),
        )

        # 在庫切れ開始日は、ebay_sync が未処理 (source_out_of_stock_since IS NULL) かつ
        # ここで新規に仕入先OOS になる場合のみ、フォールバックで UTC タイムスタンプをセット。
        # 2026-06-05: 「ページなし」も「在庫無」と同じ OOS 扱い (long-term OOS 追跡)。
        # 2026-06-11 BUG-2: checked_at は Python datetime.now().isoformat() = JST naive で
        # 消費側 (task_supplier_sweep / task_inventory_check) の datetime('now') UTC 比較と
        # 9h ズレる。UTC に統一して常に UTC "%Y-%m-%d %H:%M:%S" 形式で保存する。
        # 依頼ボード#17 MEDIUM (2026-06-12 review): sync 実行時刻でなく **検知時刻**
        # (checked_at の UTC 換算) を書く。即時探索が sync より先に走った item は
        # 探索マーカー < oos_since となり throttle が効かず 1 回重複探索していた
        # (検知時刻意味論としてもこちらが正)。parse 不能時は従来の now() fallback。
        if status in ('在庫無', 'ページなし') and prev_status not in ('在庫無', 'ページなし'):
            oos_since_utc = None
            try:
                if checked_at:
                    # checked_at = datetime.now().isoformat() (JST naive) → UTC へ -9h
                    dt_jst = datetime.fromisoformat(checked_at)
                    if dt_jst.tzinfo is None:
                        dt_utc = dt_jst - timedelta(hours=9)
                    else:
                        dt_utc = dt_jst.astimezone(timezone.utc)
                    oos_since_utc = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass  # fallback 側で必ず値が入る (silent drop ではない)
            if not oos_since_utc:
                oos_since_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE ebay_listings SET source_out_of_stock_since=?
                   WHERE ebay_item_id=? AND source_out_of_stock_since IS NULL""",
                (oos_since_utc, ebay_item_id),
            )
        elif status == '在庫有':
            # 在庫復活 → 開始日クリア
            conn.execute(
                "UPDATE ebay_listings SET source_out_of_stock_since=NULL "
                "WHERE ebay_item_id=? AND source_out_of_stock_since IS NOT NULL",
                (ebay_item_id,),
            )

        # 仕入先OOS → eBay在庫 自動0化 の候補収集 (実 0化は run_sync_data_stores で creds 使用)。
        if _should_auto_zero(status, prev_status):
            oos_to_zero.append(ebay_item_id)

        updated += 1

    conn.commit()
    conn.close()

    logger.info(f"在庫ステータス統合: {updated}件更新, {not_found}件未一致")
    return {'updated': updated, 'not_found': not_found, 'oos_to_zero': oos_to_zero}


def sync_enrichment_to_db() -> Dict:
    """
    sku_conversion_results.json の物理データ（weight, dimensions）→ ebay_listings に統合
    """
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
    if not sku_file.exists():
        return {'updated': 0}

    with open(sku_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = get_conn()
    updated = 0

    for item in data.get('sourced', []):
        ebay_id = item.get('ebay_id', '')
        weight = item.get('weight_g', 0)

        if not ebay_id or not weight:
            continue

        conn.execute(
            """UPDATE ebay_listings SET
               weight_g=?, length_cm=?, width_cm=?, height_cm=?,
               includes=?, warranty=?
               WHERE ebay_item_id=?""",
            (
                weight,
                item.get('length_cm', 0),
                item.get('width_cm', 0),
                item.get('height_cm', 0),
                item.get('includes', ''),
                item.get('warranty', ''),
                ebay_id,
            ),
        )
        updated += 1

    conn.commit()
    conn.close()

    logger.info(f"物理データ統合: {updated}件更新")
    return {'updated': updated}


def _notify_auto_zero(done: list, config) -> None:
    """auto-zero した listing を 1 メッセージで Discord 通知 (R-11: 到達は user 視認)."""
    try:
        webhook = ((config or {}).get("notifications", {})
                   .get("discord", {}).get("webhook_url") or "").strip()
        if not webhook:
            logger.warning(f"[auto-zero] Discord webhook 未設定 = {len(done)} 件通知 skip")
            return
        from notifiers.discord_notifier import DiscordNotifier
        lines = [f"⚠️ 仕入先OOS → eBay在庫0化 (履行不能防止) {len(done)} 件"]
        for r in done[:20]:
            lines.append(
                f"・[{r.get('source_status')}] {r['ebay_item_id']} "
                f"{(r.get('title') or '')[:40]}"
            )
        if len(done) > 20:
            lines.append(f"...他 {len(done) - 20} 件")
        DiscordNotifier(webhook).send_message("\n".join(lines))
    except Exception as e:  # noqa: BLE001 — 通知失敗は本処理を止めない (0化は完了済)
        logger.warning(f"[auto-zero] Discord 通知失敗: {e}")


def auto_zero_supplier_oos(ebay_item_ids: list, config) -> Dict:
    """[DEPRECATED 2026-06-05] user 決定で eBay在庫0化は手動運用に変更 → 本関数は未使用
    (run_sync_data_stores からの呼出を撤去)。緊急時の一括0化は scripts/zero_oos_*.py を参照。

    仕入先OOS 検知 listing の eBay 在庫を 0 化 (履行不能販売の防止、2026-06-05)。

    候補 (sync で収集: ページなし=即時 / 在庫無=2回連続) のうち、
    **qty>=1 + ebay* SKU + 未退役 + 未確認** のみ実 0 化 (account-direct = 厳格絞り込み)。
    仕入先ページ消滅・在庫切れ = 仕入不可なので 0 化に損失なし (安全側)。各件 log + Discord 通知。
    """
    if not ebay_item_ids:
        return {'zeroed': 0, 'skipped': 0, 'failed': 0}
    from monitor.credentials import get_ebay_credentials
    from monitor.database import get_conn, update_ebay_listing_quantity
    from monitor.ebay_client import revise_inventory_quantity
    from ui_cache import bump_db_version

    creds = get_ebay_credentials(config or {})
    if not all(creds.get(k) for k in ("app_id", "dev_id", "cert_id", "user_token")):
        logger.warning("[auto-zero] eBay 認証情報不足 = 0化 skip (silent skip 防止のため明示)")
        return {'zeroed': 0, 'skipped': len(ebay_item_ids), 'failed': 0, 'reason': 'no_creds'}

    with get_conn() as conn:
        ph = ",".join("?" * len(ebay_item_ids))
        rows = [dict(r) for r in conn.execute(
            f"""SELECT ebay_item_id, sku, title, quantity_ebay, source_status
                FROM ebay_listings
                WHERE ebay_item_id IN ({ph})
                  AND quantity_ebay >= 1 AND COALESCE(is_ended,0)=0
                  AND sku GLOB 'ebay*' AND COALESCE(risk_confirmed,0)=0""",
            ebay_item_ids,
        ).fetchall()]

    zeroed = failed = 0
    done = []
    for r in rows:
        eid = r["ebay_item_id"]
        res = revise_inventory_quantity(
            eid, 0, creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
        )
        if res.get("success"):
            update_ebay_listing_quantity(eid, 0)
            bump_db_version()
            zeroed += 1
            done.append(r)
            logger.info(
                f"[auto-zero] {eid} 仕入先{r.get('source_status')} → eBay在庫0 "
                f"| {(r.get('title') or '')[:40]}"
            )
        else:
            failed += 1
            logger.warning(f"[auto-zero] {eid} 0化失敗: {res.get('message')}")
    if done:
        _notify_auto_zero(done, config)
    logger.info(
        f"[auto-zero] 完了: 0化 {zeroed} 件 / 対象外 {len(ebay_item_ids) - len(rows)} 件 "
        f"(qty=0等) / 失敗 {failed} 件"
    )
    return {'zeroed': zeroed, 'skipped': len(ebay_item_ids) - len(rows), 'failed': failed}


def run_sync_data_stores(config) -> Dict:
    """
    データストア統合タスク

    1. sku_conversion_results.json → ebay_listings（source, classification）
    2. inventory_check_results.json → ebay_listings（source_status）
    3. enrichment data → ebay_listings（weight, dimensions）

    Returns:
        {'success': bool, 'sku_sync': dict, 'inventory_sync': dict, 'enrichment_sync': dict}
    """
    logger.info("【開始】データストア統合タスク")

    try:
        # DB初期化（マイグレーション実行）
        sys.path.insert(0, str(BASE_DIR / 'monitor'))
        from monitor.database import init_db
        init_db()

        # Step 1: SKU変換データ統合
        sku_result = sync_sku_conversion_to_db()

        # Step 2: 在庫ステータス統合
        inv_result = sync_inventory_status_to_db()

        # 2026-06-05 (user 決定): eBay在庫0化は手動運用に戻す → 自動0化を無効化。
        # 仕入先OOS は在庫監視に表示するのみ。0化は user が手動で実施 (対応完了の判断も user)。
        # (旧 Step 2b auto_zero_supplier_oos 呼出を撤去。関数は dead code として残置)。
        zero_result = {'zeroed': 0, 'skipped': 0, 'failed': 0, 'disabled': True}

        # Step 3: 物理データ統合
        enrich_result = sync_enrichment_to_db()

        total = sku_result['updated'] + inv_result['updated'] + enrich_result['updated']
        logger.info(f"データストア統合完了: 合計{total}件更新")

        return {
            'success': True,
            'sku_sync': sku_result,
            'inventory_sync': inv_result,
            'auto_zero': zero_result,
            'enrichment_sync': enrich_result,
            'total_updated': total,
            'message': f'データストア統合完了: {total}件更新'
        }

    except Exception as e:
        logger.error(f"データストア統合エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e)
        }
