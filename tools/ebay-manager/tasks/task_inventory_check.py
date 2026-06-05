#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 仕入先在庫チェック (W50 統合本体 / 2026-04-30)
scheduler cron 経路 (daily_scheduler.execute_daily_tasks) と
Streamlit button 経路 (app.py) の両方が呼ぶ単一の在庫監視本体.
データソース = monitored_items table (DB), scraper = monitor/scrapers.check_items_batch.
"""

import sys
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))


# ──────────────────────────────────────────────────────────────────────
# Step A 統合実装用 helpers (W50 / 2026-04-30)
# scheduler 経路 (cron) と Streamlit 経路 (button) を 1 本体に統合するための
# status マッピング + 結果整形ユーティリティ. Step B で run_inventory_check
# 本体差替時に使用される. silent skip 防止のため raw に無い item_id は
# "error" 扱い (Q0 ルール準拠).
# ──────────────────────────────────────────────────────────────────────

_EN_TO_JP_STATUS = {
    "available":   "在庫有",
    "unavailable": "在庫無",
    "not_found":   "ページなし",
    "error":       "エラー",
    "unknown":     "不明",
}

_JP_TO_STATS_KEY = {
    "在庫有":     "in_stock",
    "在庫無":     "out_of_stock",
    "ページなし": "page_not_found",
    "エラー":     "error",
    "不明":       "error",  # 旧実装互換: 不明は error 集計
}


def _resolve_source_label(sku: str, configs_by_prefix: dict) -> str:
    """sku の prefix を site_configs.site_name に解決. 未知 prefix は "Unknown"."""
    if not sku:
        return "Unknown"
    for prefix, cfg in configs_by_prefix.items():
        if prefix and sku.startswith(prefix):
            return cfg.get("site_name", "Unknown")
    return "Unknown"


def _build_results(items: list, raw: dict, configs_by_prefix: dict) -> list:
    """monitored_items rows + check_items_batch raw → 既存 json schema の results list.

    raw に無い item_id は "error" 扱い (Q0 silent skip 防止:
    「結果が来なかった = success」を物理的に許さない).
    """
    out = []
    now_iso = datetime.now().isoformat()
    for it in items:
        item_id = it.get("id")
        en = raw.get(item_id, "error")
        jp = _EN_TO_JP_STATUS.get(en, "不明")
        source = _resolve_source_label(it.get("sku", ""), configs_by_prefix)
        out.append({
            "id":         it.get("id"),  # monitored_items.id (last_status update 用)
            "ebay_id":    it.get("ebay_item_id"),
            "sku":        it.get("sku"),
            "source":     source,
            "url":        it.get("source_url"),
            "status":     jp,
            "checked_at": now_iso,
        })
    return out


def _aggregate_stats(results: list) -> dict:
    """results list から既存 stats + by_source 形式を生成."""
    stats = {
        "in_stock":       0,
        "out_of_stock":   0,
        "page_not_found": 0,
        "error":          0,
    }
    by_source: dict = {}
    for r in results:
        jp_status = r.get("status", "不明")
        key = _JP_TO_STATS_KEY.get(jp_status, "error")
        stats[key] += 1
        source = r.get("source", "Unknown")
        if source not in by_source:
            by_source[source] = {
                "total":          0,
                "in_stock":       0,
                "out_of_stock":   0,
                "page_not_found": 0,
                "error":          0,
            }
        by_source[source]["total"] += 1
        by_source[source][key] += 1
    stats["by_source"] = by_source
    return stats


def load_previous_results() -> dict:
    """前回の在庫チェック結果を読み込み"""
    results_file = BASE_DIR / 'data' / 'inventory_check_results.json'

    if not results_file.exists():
        logger.info("前回の在庫チェック結果が見つかりません")
        return {}

    try:
        with open(results_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"前回の在庫チェック結果を読み込み: {len(data.get('results', []))}件")
        return data
    except Exception as e:
        logger.warning(f"前回の結果読み込みエラー: {e}")
        return {}


def detect_inventory_changes(current_results: list, previous_results: dict) -> dict:
    """
    在庫状態の変化を検出
    「在庫有 → 在庫無」になった商品を抽出
    """

    if not previous_results or 'results' not in previous_results:
        logger.info("前回の結果がないため、変化検出をスキップ")
        return {'changed_items': [], 'became_out_of_stock': []}

    # 前回の結果をSKU/URL でマッピング
    prev_by_url = {
        item['url']: item for item in previous_results.get('results', [])
    }

    changed_items = []
    became_out_of_stock = []

    # 現在の結果と前回を比較
    for current in current_results:
        url = current.get('url')
        current_status = current.get('status')

        if url in prev_by_url:
            prev_status = prev_by_url[url].get('status')

            # 状態が変わった場合
            if prev_status != current_status:
                changed_items.append({
                    'url': url,
                    'sku': current.get('sku'),
                    'source': current.get('source'),
                    'prev_status': prev_status,
                    'current_status': current_status,
                    'changed_at': datetime.now().isoformat()
                })

                # 「在庫有 → 仕入先OOS」を追跡。2026-06-05 user 要望: 仕入先ページ消滅
                # (ページなし) も 在庫無(売切) と同じ OOS 扱い → 代替仕入先の候補探索を起動。
                # Yahoo の grace (24h 再出品待ち) は下流 _classify_yahoo_grace が判定するため
                # ここで両方含めても整合 (ページ消滅で end_status 読めない時は即リサーチに倒れる)。
                if prev_status == '在庫有' and current_status in ('在庫無', 'ページなし'):
                    became_out_of_stock.append({
                        'url': url,
                        'sku': current.get('sku'),
                        'source': current.get('source')
                    })

    logger.info(f"状態変化を検出: {len(changed_items)}件（うち在庫切れ: {len(became_out_of_stock)}件）")

    return {
        'changed_items': changed_items,
        'became_out_of_stock': became_out_of_stock
    }


def _classify_yahoo_grace(
    target_pairs: list,
) -> tuple:
    """[DEPRECATED 2026-06-05] W100 24h 再出品猶予は user 要望で撤廃。本関数は未使用
    (呼出箇所削除済)。Yahoo 終了/売切も即リサーチ。将来 grace を完全削除する際に本関数も除去。

    W100 (2026-05-06): ヤフオク URL の OOS listing を 24h 猶予対象と即リサーチに分類.

    - 落札者なし終了 + end_time あり → yahoo_grace_until = end_time + 24h セット (探索 skip)
    - 落札済 / 進行中 / 取得失敗 → 即リサーチ対象 (target に残す)
    - mercari / paypay 等の非ヤフオク URL → 即リサーチ対象 (touch しない)

    Args:
        target_pairs: list of (ebay_item_id, sku) tuples

    Returns:
        (immediate, grace_set_count): grace セット対象は immediate から除外、
        grace_set_count は grace_until をセットした件数
    """
    from datetime import timedelta, timezone
    from monitor.database import get_conn, set_yahoo_grace_until
    from monitor.yahoo_auction_status import fetch_yahoo_end_status

    if not target_pairs:
        return [], 0

    # eid → source_url を一括取得 (N+1 SQL 回避)
    eids = [eid for eid, _ in target_pairs]
    placeholder = ",".join("?" * len(eids))
    url_map: dict = {}
    try:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT ebay_item_id, source_url FROM ebay_listings "
                f"WHERE ebay_item_id IN ({placeholder})",
                eids,
            ).fetchall()
            for r in rows:
                url_map[r["ebay_item_id"]] = r["source_url"] or ""
    except Exception as e:
        logger.warning(f"[grace] source_url 一括取得失敗、grace 判定 skip: {e}")
        return target_pairs, 0

    immediate = []
    grace_set_count = 0

    for eid, sku in target_pairs:
        url = url_map.get(eid, "")
        if "auctions.yahoo.co.jp" not in url:
            # 非ヤフオク → 即リサーチ
            immediate.append((eid, sku))
            continue

        # ヤフオク URL → 終了状態確認
        try:
            status = fetch_yahoo_end_status(url, timeout_sec=15)
        except Exception as e:
            logger.warning(f"[grace] item={eid} fetch_yahoo_end_status 例外: {e}")
            immediate.append((eid, sku))  # 取得失敗 → 即リサーチ (Q0 silent skip 防止)
            continue

        if (status.is_ended
                and status.has_winner is False
                and status.end_time_utc is not None):
            # 落札者なし終了 → 24h 後に再出品される慣行を待つ
            until = status.end_time_utc + timedelta(hours=24)
            try:
                # H-NEW-1 fix (2026-05-06): SQLite datetime('now') と lexicographic 比較
                # するため、ISO 8601 (T 区切り + offset 付き) ではなく
                # naive UTC 形式 ("YYYY-MM-DD HH:MM:SS") で保存.
                # 旧 isoformat() は WHERE yahoo_grace_until <= datetime('now') が
                # 永遠に false 評価される silent regression を引き起こす.
                until_str = until.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                set_yahoo_grace_until(eid, until_str)
                grace_set_count += 1
                logger.info(
                    f"[grace] item={eid} 再出品待ち until={until_str} UTC "
                    f"(end_time={status.end_time_utc.isoformat()}) supplier 探索 skip"
                )
            except Exception as e:
                logger.warning(f"[grace] item={eid} grace_until セット失敗: {e}")
                immediate.append((eid, sku))  # 失敗 → 即リサーチ fallback
        else:
            # 進行中 / 落札済 / 取得不能 → 即リサーチ
            immediate.append((eid, sku))
            logger.debug(
                f"[grace] item={eid} not graced: is_ended={status.is_ended}, "
                f"has_winner={status.has_winner}, end_time={status.end_time_utc}, "
                f"err={status.raw_error}"
            )

    if grace_set_count:
        logger.info(
            f"[grace] {grace_set_count} 件を 24h 猶予セット "
            f"(残 immediate={len(immediate)} 件で探索)"
        )
    return immediate, grace_set_count


def _start_supplier_candidate_search_async(changes: dict, config: dict,
                                            synchronous: bool = False) -> None:
    """Pattern 1 拡張: OOS 検知時に、対象SKUの仕入先候補を探索。

    対象:
      (a) 新たに「在庫有→在庫無」になった SKU (newly_oos)
      (b) 現在 OOS で、過去 N 日以内に候補探索されていない SKU (continuing_oos)
          → N は config.tasks_enabled.supplier_sweep.skip_if_searched_within_days (デフォルト 7日)

    max_per_run で1回の inventory_check あたりの上限を制限（API コスト対策）。

    synchronous=True:
      foreground で直列実行（**手動 CLI 実行時のみ** - daemon では Python exit で殺される）
      ⚠ Streamlit / scheduler 経由では使用禁止 (W100 grace 判定で max 7.5 分の UI hang)
    synchronous=False (デフォルト):
      daemon thread で非同期実行（scheduler 常駐プロセス用 - 本体処理を早く返す）
      daemon=True のため scheduler 停止時は即時終了する（best-effort）。
      scheduler プロセス内で他に Playwright を並行利用しないこと前提
      （sync_playwright は複数スレッドで同時使用するとクラッシュする）。
    """
    from monitor.database import get_conn

    task_cfg = (config or {}).get('tasks_enabled', {}).get('supplier_sweep') or {}
    skip_days = int(task_cfg.get('skip_if_searched_within_days', 7))
    max_per_run = int(task_cfg.get('max_skus_per_run', 30))

    # 2026-05-01 W75 4b: run_supplier_candidate_search が ebay_item_id 主導 signature に
    # 変更されたため、target を (ebay_item_id, sku) tuple で揃える。
    # newly_oos は supplier 由来 dict (url/sku/source) で eid を持たない → source_url で
    # ebay_listings から逆引き (SKU rule 準拠 = listing 識別に SKU を使わない).
    newly_oos = changes.get('became_out_of_stock', []) if changes else []
    newly_pairs: list[tuple[str, str]] = []  # (ebay_item_id, sku)
    seen_eids: set[str] = set()
    try:
        with get_conn() as conn:
            for c in newly_oos:
                url = (c.get('url') or '').strip()
                sku = (c.get('sku') or '').strip()
                if not url or not sku:
                    continue
                row = conn.execute(
                    "SELECT ebay_item_id FROM ebay_listings "
                    "WHERE source_url=? AND (is_ended IS NULL OR is_ended=0) "
                    "LIMIT 1",
                    (url,),
                ).fetchone()
                eid = row["ebay_item_id"] if row else None
                if eid and eid not in seen_eids:
                    newly_pairs.append((eid, sku))
                    seen_eids.add(eid)
    except Exception as e:
        logger.warning(f"newly_oos の eid 解決に失敗: {e}")

    # 現在 OOS のうち、最近探索されていない listing を追加 (ebay_item_id + sku 両方取得)
    continuing_pairs: list[tuple[str, str]] = []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                # 業務ロジック: 在庫監視 = 無在庫出品で 仕入先OOS が生じた RISK の検知。
                # qty=0 は「既に販売停止済で RISK ではない」=監視対象外（2026-04-20 業務確認）。
                # FINDING 2 (2026-05-05): sku GLOB 'ebay*' で stock prefix 除外
                # + NOT EXISTS を ebay_item_id 単位に (兄弟 listing silent 抜け解消).
                # 2026-06-05 user 要望: 「ページなし」も「在庫無」と同じ OOS 扱い +
                # Yahoo 24h 再出品猶予 (W100 grace) 撤廃 → yahoo_grace_until 除外条件も削除
                # (Yahoo 終了も即リサーチ対象)。
                """SELECT l.ebay_item_id, l.sku FROM ebay_listings l
                    WHERE l.source_status IN ('在庫無', 'ページなし')
                      AND (l.is_ended IS NULL OR l.is_ended=0)
                      AND l.quantity_ebay >= 1
                      AND l.sku GLOB 'ebay*'
                      AND NOT EXISTS (
                          SELECT 1 FROM supplier_candidates sc
                          WHERE sc.ebay_item_id = l.ebay_item_id
                            AND sc.created_at >= datetime('now', ?)
                      )""",
                (f"-{skip_days} days",),
            ).fetchall()
            for r in rows:
                eid = r["ebay_item_id"]
                sku = r["sku"]
                if not eid or not sku or eid in seen_eids:
                    continue
                continuing_pairs.append((eid, sku))
                seen_eids.add(eid)
    except Exception as e:
        logger.warning(f"continuing_oos の収集に失敗: {e}")

    # 新規OOSが優先、残り枠に継続OOSを詰める
    ordered_pairs = newly_pairs + continuing_pairs
    target_pairs = ordered_pairs[:max_per_run]

    if not target_pairs:
        return

    newly_eids = {eid for eid, _ in newly_pairs}

    def _do_search():
        # 2026-06-05 user 要望: Yahoo 24h 再出品猶予 (W100 grace) を撤廃。
        # Yahoo 終了/売切も メルカリ等と同様に即リサーチ (= 全 target を即探索)。
        # 「再出品されたら次回在庫チェックで在庫有に戻る + 仕入先候補が拾う」ため
        # 待機不要。grace 分類 (_classify_yahoo_grace) は廃止 (関数は dead code として残置)。
        immediate_pairs = target_pairs

        from tasks.task_supplier_candidate_search import run_supplier_candidate_search
        for eid, sku in immediate_pairs:
            src = "pattern_1_newly_oos" if eid in newly_eids else "pattern_1_continuing_oos"
            try:
                r = run_supplier_candidate_search(
                    ebay_item_id=eid, sku=sku, config=config, discovered_via=src,
                )
                logger.info(
                    f"[supplier] item={eid} sku={sku} ({src}): {r.get('message', '(no message)')}"
                )
            except Exception as e:
                logger.warning(f"[supplier] item={eid} sku={sku} failed: {e}")

    if synchronous:
        # foreground 実行（手動CLI実行時）
        logger.info(f"仕入先候補探索を同期実行: 合計{len(target_pairs)} 件 (foreground)")
        _do_search()
        logger.info("仕入先候補探索 同期実行完了")
        return

    # daemon thread で非同期実行（scheduler 常駐プロセス用）
    import threading
    t = threading.Thread(
        target=_do_search,
        name=f"supplier_bg_{len(target_pairs)}items",
        daemon=True,
    )
    t.start()
    logger.info(
        f"仕入先候補探索を非同期起動: 合計{len(target_pairs)} 件 "
        f"(newly={len(newly_pairs)}, continuing={len(target_pairs) - len(newly_pairs)})"
    )


def run_inventory_check(config):
    """
    348件の仕入先商品の在庫状態をチェック
    前回結果と比較して変化を検出

    Args:
        config: 設定辞書

    Returns:
        {'success': bool, 'checked_count': int, 'results': dict}
    """

    logger.info("【開始】在庫チェックタスク (統合本体 W50 / 2026-04-30)")

    # H-1 (code-reviewer 2026-04-30): broad except Exception を撤去.
    # silent skip 防止用の RuntimeError raise を scheduler.run_task / Streamlit の
    # try/except に伝播させ、retry / traceback / Discord 通知を機能させる.
    from monitor.database import (
        get_active_items, get_site_configs, update_item_status
    )
    from monitor.scrapers import prepare_batch_items, check_items_batch

    # データソース = monitored_items table (DB 真実源)
    items = get_active_items()
    if not items:
        logger.error("monitored_items に active アイテムなし (silent skip 防止のため明示記録)")
        return {
            'success': False,
            'checked_count': 0,
            'results': {},
            'error': 'no_active_items'
        }

    # site_configs prefix → site_name マッピング
    configs = get_site_configs()
    configs_by_prefix = {c["convert_url"]: c for c in configs}

    # batch 準備 (sku prefix 一致しないものは prepare_batch_items が除外)
    batch = prepare_batch_items(items, configs_by_prefix)
    if len(batch) < len(items):
        logger.warning(
            f"site_config 不一致で {len(items) - len(batch)} 件除外 "
            f"(items={len(items)} → batch={len(batch)}). prefix 未登録の sku あり"
        )
    if not batch:
        # Q0 silent skip 防止: 全件除外で空 batch のまま success: True を返さない
        logger.error("batch 空 = prepare_batch_items 全件除外 (silent skip 防止のため raise)")
        raise RuntimeError(
            f"prepare_batch_items returned empty for {len(items)} items "
            f"(silent skip prevention)"
        )

    logger.info(f"ステップ1: 在庫チェック実行中... 対象 {len(batch)}件")

    # scrape (httpx → Playwright headless → Chrome headed の 3 段)
    raw = check_items_batch(batch)
    if not raw:
        # Q0 silent skip 防止: 結果 dict 空 = scrape 全失敗で success 偽装を許さない
        logger.error("check_items_batch 戻り値空 (silent skip 防止のため raise)")
        raise RuntimeError(
            f"check_items_batch returned empty for {len(batch)} items "
            f"(silent skip prevention)"
        )

    # ステップ2: 結果整形 + 統計
    logger.info("ステップ2: 結果整形中...")
    results = _build_results(items, raw, configs_by_prefix)
    stats = _aggregate_stats(results)

    # ステップ3: 前回比較 (既存 helper 再利用)
    logger.info("ステップ3: 前回の結果と比較中...")
    prev_results = load_previous_results()
    changes = detect_inventory_changes(results, prev_results)

    # ステップ4: JSON 保存 (既存 schema 維持)
    logger.info("ステップ4: 結果を保存中...")
    out_path = BASE_DIR / 'data' / 'inventory_check_results.json'
    payload = {
        "checked_at":  datetime.now().isoformat(),
        "total_items": len(results),
        "stats":       stats,
        "by_source":   stats["by_source"],  # 後方互換 (旧 schema は top-level に展開)
        "results":     results,
        "changes":     changes,
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"結果を保存: {out_path}")

    # ステップ5a: W121 (2026-05-12) — last_status 更新 **直前** に restock alert 評価.
    # 旧 last_status='unavailable' から新 status='在庫有' 遷移を検知して price_alert_state='restock'.
    # update_item_status の直前で実行する (旧値ベース判定のため).
    n_restock = _evaluate_restock_alerts(results)
    if n_restock > 0:
        logger.info(f"[price] restock alert 立て: {n_restock} 件")

    # ステップ5: monitored_items.last_status 更新 (UI metric 用)
    # 旧 InventoryCheckerSelenium 経路では更新されず、4/29 mtime stuck の真因の 1 つ.
    updated = 0
    for r in results:
        item_id = r.get("id")
        if item_id is None:
            continue
        try:
            update_item_status(item_id, r["status"])
            updated += 1
        except sqlite3.Error as e:
            logger.warning(f"last_status 更新失敗 sku={r.get('sku')}: {e}")
    logger.info(f"monitored_items.last_status 更新: {updated}/{len(results)} 件")

    # ステップ5b: W120+W121+W192+W193 — Amazon/楽天/Yahoo の価格 fetch + baseline 評価 + Discord 通知.
    # 既存 check_items_batch は status のみ返すため、価格抽出は別経路で httpx fetch.
    # 16 件規模なので per-item 2 度 fetch は許容 (~1-2 分追加).
    n_price = _fetch_and_store_prices(results, config)
    if n_price > 0:
        logger.info(f"[price] 価格更新: {n_price} 件 (Amazon/楽天/Yahoo)")

    logger.info(f"在庫チェック完了: {len(results)}件")

    # ステップ6: Pattern 1 — 新規 OOS の仕入先候補探索 (既存維持)
    # scheduler 常駐プロセス内なら daemon (async)、手動CLI実行なら foreground
    _synchronous = bool(config.get("tasks_enabled", {})
                        .get("inventory_check", {}).get("synchronous_supplier_search", False))
    _start_supplier_candidate_search_async(changes, config, synchronous=_synchronous)

    # 結果をまとめる (既存 return 形式維持)
    return {
        'success': True,
        'checked_count': len(results),
        'results': {
            'in_stock':       stats['in_stock'],
            'out_of_stock':   stats['out_of_stock'],
            'page_not_found': stats['page_not_found'],
            'error':          stats['error'],
            'by_source':      stats['by_source'],
        },
        'changes': changes,
        'message': f'在庫チェック完了: {len(results)}件確認'
    }


# =============================================================================
# W120 + W121 (2026-05-12): 仕入先 価格変動検知
# Amazon (ebayAM_) / 楽天市場 (ebayRT_) / Yahoo!ショッピング (ebayYS_) の商品ページから
# 価格を抽出し、baseline (初回値) からの ±5% 変動を検知して price_alert_state を更新.
# W192+W193 (2026-05-30): Yahoo 追加 + 閾値 ±5% 統一 + 遷移時 Discord 通知 (基準=最初の価格).
# 詳細設計: code-architect ブループリント参照 / `reference_shipping_method_vs_ddu_taxonomy.md`.
# =============================================================================

_PRICE_THRESHOLD = 0.05  # ±5% (user 確定要件 W193, dashboard と統一. K1 hard-code: 3 回出てから column 化)
_PRICE_FETCH_TIMEOUT_SEC = 15
_PRICE_TARGET_PREFIXES = ("ebayAM_", "ebayRT_", "ebayYS_")  # Amazon / 楽天 / Yahoo
# W183 手動 URL 仕入先 (元 SKU を保持し prefix が ebayXX_ にならない) も価格追跡するため、
# source_url のドメイン部分一致でも価格対象に含める (extract_price_by_url で振り分け).
_PRICE_TARGET_URL_DOMAINS = ("amazon.co.jp", "item.rakuten", "shopping.yahoo.co.jp")


def _evaluate_restock_alerts(results: list) -> int:
    """在庫切れ → 在庫有 遷移を検知して price_alert_state='restock' を立てる.

    monitored_items.last_status は update_item_status で **これから更新される値**.
    旧値 (= 現 DB 値) と新 status を比較するため、update 前に呼ぶ必要がある.

    H4 fix (2026-05-12): 24h 以上経過した restock state を normal に自動降格.
    旧実装は restock 永久 sticky で DASHBOARD「在庫復活」が肥大化.

    Returns: restock alert 立て件数.
    """
    from monitor.database import get_conn

    n_restock = 0
    with get_conn() as conn:
        # H4: 24h 経過 restock を normal 降格 (sqlite-timezone.md A パターン)
        conn.execute(
            """UPDATE monitored_items
               SET price_alert_state='normal'
               WHERE price_alert_state='restock'
                 AND last_check IS NOT NULL
                 AND last_check < datetime('now', '-24 hours')"""
        )

        for r in results:
            item_id = r.get("id")
            new_status = r.get("status")
            if item_id is None or new_status != "在庫有":
                continue
            row = conn.execute(
                "SELECT last_status FROM monitored_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row and row["last_status"] == "在庫無":
                # 在庫切れ → 在庫有 遷移
                conn.execute(
                    "UPDATE monitored_items SET price_alert_state='restock' WHERE id=?",
                    (item_id,),
                )
                n_restock += 1
    return n_restock


def _fetch_and_store_prices(results: list, config: dict) -> int:
    """Amazon / 楽天 / Yahoo の商品ページ価格を fetch + baseline / surge / drop 評価.

    既存 check_items_batch は status のみ返すため、価格抽出は別経路で httpx fetch.
    対象 listing 数が少ない (~16 件) ので per-item 2 度 fetch でも許容コスト.

    W192 (2026-05-30): 対象選定を 2 経路化 — (a) SKU prefix が ebayAM_/RT_/YS_、または
    (b) source_url ドメインが amazon.co.jp / item.rakuten / shopping.yahoo.co.jp.
    後者は W183 手動 URL 仕入先 (元 SKU 保持で prefix が ebayXX_ にならない) も追跡するため.

    W193 (2026-05-30): normal/restock → surge/drop へ遷移した item のみ集めて、
    バッチ末尾で 1 メッセージにまとめ Discord 通知 (基準=最初の価格、圏内復帰まで再通知なし).
    Q0: Discord 送信の成否に関わらず DB の state は評価関数内で確定済 = 送信失敗で sticky 化しない.

    Returns: 価格更新できた件数 (fetch 失敗 / 抽出 None は除外).
    """
    import random
    import httpx
    from monitor.database import get_conn
    from monitor.price_extractor import extract_price, extract_price_by_url
    from monitor.scrapers import USER_AGENTS

    # 対象 listing 抽出 (2 経路: SKU prefix または source_url ドメイン)
    targets = []
    with get_conn() as conn:
        for r in results:
            item_id = r.get("id")
            sku = r.get("sku") or ""
            row = conn.execute(
                "SELECT source_url, title, ebay_item_id FROM monitored_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if not (row and row["source_url"]):
                continue
            url = row["source_url"]
            url_l = url.lower()
            is_prefix = sku.startswith(_PRICE_TARGET_PREFIXES)
            is_domain = any(d in url_l for d in _PRICE_TARGET_URL_DOMAINS)
            if not (is_prefix or is_domain):
                continue
            targets.append({
                "id": item_id, "sku": sku, "url": url,
                "title": row["title"] or "",
            })

    if not targets:
        return 0

    import time
    updated = 0
    crossings = []  # W193: normal/restock → surge/drop 遷移 item を収集
    for idx, it in enumerate(targets):
        # H9 fix (2026-05-12): bot 検知緩和の jitter sleep.
        # Amazon は同 IP 連続 GET に厳しく、scrapers.check_items_batch の数秒後に
        # 同 URL を 2 度目 GET する設計なので 503/CAPTCHA リスク. 1.5-3.5s ランダム.
        if idx > 0:
            time.sleep(random.uniform(1.5, 3.5))
        try:
            resp = httpx.get(
                it["url"],
                headers={
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=_PRICE_FETCH_TIMEOUT_SEC,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                logger.debug(
                    f"[price] fetch non-200: id={it['id']} sku={it['sku']} "
                    f"status={resp.status_code}"
                )
                continue
            # anti-bot ページ (Amazon Robot Check / CAPTCHA) は価格抽出させない.
            # scrapers._check_with_httpx と同じガード. これが無いと CAPTCHA HTML の
            # stray price を baseline に固定 → 以降の正常値で永続 surge/drop 誤通知
            # (baseline は再記録しない要件のため自己回復しない / 金銭直結 W193).
            low = resp.text.lower()
            if "robot check" in low or "validatecaptcha" in low:
                logger.debug(
                    f"[price] anti-bot page detected, skip: id={it['id']} sku={it['sku']}"
                )
                continue
            # SKU prefix で振り分け、未対応 prefix (W183 手動 URL) は URL ドメインで振り分け
            price = extract_price(resp.text, it["sku"]) \
                or extract_price_by_url(resp.text, it["url"])
            if price is None:
                logger.debug(
                    f"[price] extraction miss: id={it['id']} sku={it['sku']} "
                    f"(selector miss or bot trap)"
                )
                continue
            evald = _update_price_and_evaluate_alert(it["id"], price)
            updated += 1
            # W193: 圏内 (normal/restock) → 圏外 (surge/drop) へ遷移した瞬間のみ収集.
            # 基準確立 (None→normal) や surge/drop 継続中は再通知しない.
            if evald:
                old_state, new_state, baseline = evald
                if new_state in ("surge", "drop") \
                        and old_state not in ("surge", "drop"):
                    crossings.append({
                        "title": it["title"], "sku": it["sku"], "url": it["url"],
                        "state": new_state, "current": price, "baseline": baseline,
                    })
        except httpx.TimeoutException:
            logger.debug(f"[price] fetch timeout: id={it['id']} sku={it['sku']}")
        except (httpx.RequestError, httpx.HTTPError, ValueError) as e:
            logger.debug(
                f"[price] fetch error: id={it['id']} sku={it['sku']}: "
                f"{type(e).__name__}: {e}"
            )

    # Q0: per-item の失敗は debug (noise 抑制) だが、対象に対する fetch 成功率は INFO で
    # 集約記録. Yahoo 等 1 サイトの selector が壊れて「全件 0 fetched」になった構造的
    # 劣化を scheduler.log で検知可能にする (debug のみだと本番 INFO level で観測不能).
    logger.info(
        f"[price] 価格 fetch: 対象 {len(targets)} 件中 {updated} 件取得 "
        f"(失敗/抽出 miss {len(targets) - updated} 件, 価格変動遷移 {len(crossings)} 件)"
    )

    # W193: 遷移を 1 メッセージにまとめて Discord 通知 (DB state は更新済 = 送信失敗でも整合)
    if crossings:
        from notifiers.discord_notifier import inject_webhook_into_config
        webhook = (inject_webhook_into_config(config or {})
                   .get("discord", {}).get("webhook_url") or "").strip()
        if webhook:
            ok = _send_price_alert_discord(webhook, crossings)
            if ok:
                logger.info(
                    f"[price] 価格変動 Discord 通知: {len(crossings)} 件 送信成功"
                )
            else:
                # DB state=surge/drop は反映済 = ダッシュボードには表示される. ただし同 state の
                # ままでは次回 batch で再通知しない設計 (圏内復帰まで 1 回通知) = この crossing の
                # Discord 送達はこの回で失われる. 「次回再評価」と誤記せず WARNING で観測可能化 (Q0).
                logger.warning(
                    f"[price] 価格変動 Discord 通知 {len(crossings)} 件 送信失敗 "
                    f"(DB state は反映済・ダッシュボード表示あり、同 state では再通知しないため本通知は未送達)"
                )
        else:
            logger.warning(
                f"[price] 価格変動 {len(crossings)} 件あるが Discord webhook 未設定 = 通知 skip"
            )
    return updated


def _send_price_alert_discord(webhook: str, crossings: list) -> bool:
    """仕入先 価格変動 (surge/drop 遷移) を 1 embed にまとめ Discord 送信.

    W148 _send_discord_for_hit と同じ堅牢化: 1 回失敗 → 1s backoff → 1 回 retry.
    webhook は呼び側で存在チェック済 (URL は print しない = security.md 順守).
    """
    if not webhook or not crossings:
        return False
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook)
        lines = []
        for c in crossings[:20]:
            base = c["baseline"] or 0
            cur = c["current"]
            pct = ((cur - base) / base * 100) if base > 0 else 0
            arrow = "📈 値上がり" if c["state"] == "surge" else "📉 値下がり"
            title = (c["title"] or "(無題)")[:60]
            lines.append(
                f"{arrow}  {title}\n"
                f"　¥{base:,} → ¥{cur:,}  ({pct:+.1f}%)  | {c['sku']}\n{c['url']}"
            )
        if len(crossings) > 20:
            lines.append(f"…他 {len(crossings) - 20} 件")
        embed = {
            "title": f"💰 仕入先 価格変動 {len(crossings)} 件 (基準から ±5% 超)",
            "description": "\n\n".join(lines)[:4000],
            "color": 15844367,  # amber
        }
        content = "🔔 仕入先の販売価格が基準から ±5% を超えて変動しました"
        if notifier.send_message(content, embed=embed):
            return True
        import time
        time.sleep(1.0)  # backoff
        return bool(notifier.send_message(content, embed=embed))
    except Exception:
        logger.exception("W193 価格変動 Discord 送信失敗")
        return False


def _update_price_and_evaluate_alert(item_id: int, current_price: int):
    """価格更新 + 状態遷移評価.

    - baseline=NULL なら初回値で固定 (state='normal')
    - 既 baseline あり → ±_PRICE_THRESHOLD 判定で surge / drop / normal を state に設定

    H3 fix (2026-05-12): 現在 state='restock' のときは price 評価で state を上書きしない.
    旧実装は `_evaluate_restock_alerts` が立てた restock を `_fetch_and_store_prices` が
    上書きする論理事故あり (Amazon/楽天 SKU で在庫復活 alert が消える).

    W193 (2026-05-30): Discord 通知判定のため (旧 state, 新 state, baseline) を返す.
    baseline は初回のみ記録し以降一切上書きしない = user 要件「最初の価格から±5%」を満たす.
    Returns: (old_state, new_state, baseline) | None (行が無い等で更新不能時).
    """
    from monitor.database import get_conn

    with get_conn() as conn:
        row = conn.execute(
            "SELECT baseline_price_jpy, price_alert_state FROM monitored_items WHERE id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return None

        baseline = row["baseline_price_jpy"]
        current_state = row["price_alert_state"]
        if baseline is None:
            # 初回取得 → baseline 確定 (基準確立は通知対象外 = 新 state を normal で返す).
            # WHERE baseline_price_jpy IS NULL で「最初に書いた値を上書きしない」を保証.
            # 手動 UI 在庫チェックと 02:30 batch が同時実行されると baseline が二重確定する
            # race があり (両経路に mutex なし)、後発 writer が user 確定要件「最初の価格」を
            # 壊す。単一 UPDATE 文は atomic = 後発は 0 行更新 → 既存 baseline 経路へ fall through.
            cur = conn.execute(
                """UPDATE monitored_items
                   SET baseline_price_jpy=?, current_price_jpy=?,
                       baseline_at=CURRENT_TIMESTAMP, price_alert_state='normal'
                   WHERE id=? AND baseline_price_jpy IS NULL""",
                (current_price, current_price, item_id),
            )
            if cur.rowcount > 0:
                return (current_state, "normal", current_price)
            # 別経路が先に baseline を確定済 → 確定値を読み直し、既存 baseline 評価へ続行
            # (current_price は下の restock / 閾値評価ブロックが記録する).
            row = conn.execute(
                "SELECT baseline_price_jpy, price_alert_state FROM monitored_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if not row or row["baseline_price_jpy"] is None:
                return None
            baseline = row["baseline_price_jpy"]
            current_state = row["price_alert_state"]

        # H3: state='restock' は保持 (24h 後に自動降格は _evaluate_restock_alerts 側で実施)
        if current_state == "restock":
            conn.execute(
                "UPDATE monitored_items SET current_price_jpy=? WHERE id=?",
                (current_price, item_id),
            )
            return (current_state, "restock", baseline)

        # ±_PRICE_THRESHOLD 判定
        if baseline <= 0:
            new_state = "normal"  # 異常値防御
        else:
            delta_ratio = (current_price - baseline) / baseline
            if delta_ratio >= _PRICE_THRESHOLD:
                new_state = "surge"
            elif delta_ratio <= -_PRICE_THRESHOLD:
                new_state = "drop"
            else:
                new_state = "normal"

        conn.execute(
            """UPDATE monitored_items
               SET current_price_jpy=?, price_alert_state=?
               WHERE id=?""",
            (current_price, new_state, item_id),
        )
        return (current_state, new_state, baseline)
