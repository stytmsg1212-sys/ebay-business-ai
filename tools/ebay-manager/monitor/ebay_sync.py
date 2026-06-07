"""
eBay出品と仕入元在庫を同期
Get active listings from eBay Trading API and sync with source inventory
"""
import logging
import sqlite3
from typing import Optional

from .ebay_client import (
    get_active_listings, filter_items_with_sku, enrich_listings_with_metrics,
    get_single_listing,
)
from .database import (
    init_db, upsert_ebay_listing, get_ebay_listings, get_active_items,
    get_prev_status, update_ebay_listing_status, update_ebay_listing_quantity,
    update_ebay_listing_metrics, update_ebay_listing_growth_rates,
    update_ebay_listing_metrics_score, get_all_listing_metrics,
    get_rank_distribution_details,
    mark_ebay_listing_ended, unmark_ebay_listing_ended, get_stale_ebay_item_ids,
    cleanup_stale_supplier_candidates, update_ebay_listing_timing,
)
from .rank_calculator import (
    calculate_growth_rate, calculate_metrics_score, assign_rank
)

logger = logging.getLogger(__name__)

# 退役検出の安全閾値: ActiveList 取得件数がこれ未満なら退役検出自体をスキップ
# （API トラブルや pagination 失敗で空に近い応答を誤って退役扱いしないため）
RETIREMENT_SANITY_THRESHOLD = 100

# stale 判定の時間しきい値（時間）: sync 1回分程度のズレでは退役扱いしない
RETIREMENT_STALE_HOURS = 48


def sync_listings_from_ebay(app_id: str, dev_id: str, cert_id: str, user_token: str) -> dict:
    """
    eBay Trading APIからアクティブ出品を取得してDB同期

    Returns: {
        synced: count,           # upsert 成功件数
        matched: count,          # source_status マッチング件数
        ended: count,            # 今回新たに退役マーキングされた件数
        reactivated: count,      # ActiveList に復活して is_ended=0 に戻した件数
        errors: count,           # 各ステップのエラー総数
        retirement_skipped: bool, # sanity 閾値未達で退役検出をスキップしたか
        messages: []
    }
    """
    init_db()
    stats = {
        "synced": 0, "matched": 0, "ended": 0, "reactivated": 0,
        "errors": 0, "retirement_skipped": False, "intl_skipped": 0,
        "messages": [],
    }

    if not all([app_id, dev_id, cert_id, user_token]):
        msg = "eBay credentials not configured"
        logger.warning(msg)
        stats["messages"].append(msg)
        return stats

    # Step 1: eBayからアクティブ出品を取得
    try:
        logger.info("Fetching active listings from eBay...")
        all_listings = get_active_listings(app_id, dev_id, cert_id, user_token)
        # 2026-05-20 user 緊急修正: SKU 空 listing も DB に取り込む (Q0 silent gap 解消)。
        # 旧 `filter_items_with_sku(all_listings)` は SKU 未設定 listing を一律除外し、
        # eBay 535件 vs DB active 421件の差 114件が「商品管理に永久に見えない」silent
        # gap を発生させていた (例: 358178581550)。filter_items_with_sku 関数自体は
        # app.py L2948-2950 / monitor/ebay_competitor_monitoring.py で別用途に使われ
        # ているため関数は残存、本 sync 経路でのみ filter を外す surgical 変更。
        # downstream の sku-prefix 判定 (sku.startswith("stock") 等) は SKU 空で
        # False となり「在庫種別なし」扱い = 既存挙動と互換 (元々 DB に無かった = 全
        # 経路で無視されていたのが、DB 存在 + SKU 空 = 同じく全経路で無視 + 商品管理
        # にだけは表示される、というのが本変更の目的)。
        # 2026-06-07: eBaymag 各国版を除外。eBaymag を全国 ON にすると各国サイト
        # (CA/UK/DE/AU 等) の複製 listing が同一アカウントの GetMyeBaySelling に
        # currency=CAD/GBP/EUR/AUD で混入する (1 SKU が最大 8 item_id に複製)。
        # これらは eBaymag が US 在庫連動で自前管理するため、MonoDeck の定時処理
        # (relist/値下げ/在庫/仕入先) が触ると二重管理で破壊する。currency!=USD は
        # 取り込まない (US 本体のみ処理 = user 承認 2026-06-07)。<Site> は
        # GetMyeBaySelling が返さないため通貨で判別 (ebay_client 実機確認済)。
        usd_listings = [l for l in all_listings if (l.get("currency") or "USD") == "USD"]
        intl_skipped = len(all_listings) - len(usd_listings)
        listings_with_sku = usd_listings  # 変数名は downstream 互換のため維持
        logger.info(
            f"Got {len(all_listings)} active listings from eBay; "
            f"US本体(USD) {len(usd_listings)}件を取込、"
            f"eBaymag各国版(非USD) {intl_skipped}件をスキップ"
        )
        stats["intl_skipped"] = intl_skipped
        stats["messages"].append(
            f"eBay API: {len(all_listings)}件中 US本体 {len(usd_listings)}件取込 / "
            f"eBaymag各国版 {intl_skipped}件除外 (currency≠USD)"
        )
    except Exception as e:
        msg = f"eBay API error: {e}"
        logger.error(msg)
        stats["messages"].append(msg)
        stats["errors"] += 1
        return stats

    # Step 1.5: GetItem APIで詳細メトリクスを取得（HitCount, QuantitySold）
    # 失敗した場合は enrichment 前の値（GetMyeBaySelling 由来）を使い続ける。
    # errors は stats.errors に積み上げる（以前は log のみで見えなくなっていた）
    try:
        logger.info("Enriching listings with detailed metrics (Watch/View/Sales via GetItem API)...")
        listings_with_sku = enrich_listings_with_metrics(
            listings_with_sku, app_id, dev_id, cert_id, user_token
        )
        logger.info("Metrics enrichment completed")
        stats["messages"].append("Metrics enriched with GetItem API")
    except Exception as e:
        logger.warning(f"Failed to enrich metrics (will continue with partial data): {e}")
        stats["messages"].append(f"Metrics enrichment warning: {e}")
        stats["errors"] += 1

    # 退役検出に使う: 今回の ActiveList に入っていた ebay_item_id 集合
    active_item_ids: set[str] = {
        l["item_id"] for l in listings_with_sku if l.get("item_id")
    }

    # Step 2: 取得した出品をDBに同期
    for listing in listings_with_sku:
        try:
            ebay_item_id = listing["item_id"]
            sku = listing["sku"]
            title = listing.get("title", "")
            qty = listing.get("quantity", 0)

            # 価格と送料を取得
            price = listing.get('current_price', 0.0)
            shipping = listing.get('shipping_cost', 0.0)

            # DBに登録 (W222: category_id があれば保存、無ければ None=COALESCE で既存維持)
            upsert_ebay_listing(
                ebay_item_id=ebay_item_id,
                sku=sku,
                title=title,
                current_price=price,
                quantity_ebay=qty,
                shipping_cost=shipping,
                category_id=listing.get("category_id"),
            )

            # メトリクスを保存（Watch数、View数、販売数）
            metrics = {
                'watch_count': listing.get('watch_count', 0),
                'view_count': listing.get('view_count', 0),
                'sales_count_30d': listing.get('sales_count_30d', 0),
            }
            update_ebay_listing_metrics(ebay_item_id, metrics)

            # End→Relist 選定用: TimeLeft と StartTime
            update_ebay_listing_timing(
                ebay_item_id,
                listing.get('time_left_seconds'),
                listing.get('start_time'),
            )

            # 復活検出: 以前 ended 扱いになったものが ActiveList に戻ってきたらクリア
            if unmark_ebay_listing_ended(ebay_item_id):
                stats["reactivated"] += 1
                logger.info(f"Reactivated previously-ended listing: {ebay_item_id}")

            stats["synced"] += 1
        except Exception as e:
            logger.warning(f"Failed to sync listing {listing.get('item_id', '?')}: {e}")
            stats["errors"] += 1

    # Step 3: 仕入元在庫ステータスをマッチ
    try:
        matched = match_source_status_to_ebay()
        stats["matched"] = matched
        logger.info(f"Matched {matched} eBay listings to source items")
    except Exception as e:
        logger.warning(f"Failed to match source status: {e}")
        stats["errors"] += 1

    # Step 4: 退役検出（ActiveListに無くなったlistingをマーキング）
    # 安全ガード:
    #   (a) active_item_ids が SANITY 閾値未満なら、API部分失敗の疑いで退役判定スキップ
    #   (b) last_synced_at が STALE_HOURS 未満の行は対象外（sync1回分の遅延では退役扱いしない）
    if len(active_item_ids) < RETIREMENT_SANITY_THRESHOLD:
        stats["retirement_skipped"] = True
        msg = (
            f"Skipping retirement detection: ActiveList has only "
            f"{len(active_item_ids)} items (threshold={RETIREMENT_SANITY_THRESHOLD})"
        )
        logger.warning(msg)
        stats["messages"].append(msg)
    else:
        stale_candidates = get_stale_ebay_item_ids(threshold_hours=RETIREMENT_STALE_HOURS)
        ended_count = 0
        for item_id in stale_candidates:
            if item_id not in active_item_ids:
                if mark_ebay_listing_ended(item_id, reason="not_in_active_list"):
                    ended_count += 1
                    logger.info(f"Marked ended: {item_id}")
        stats["ended"] = ended_count
        if ended_count:
            stats["messages"].append(f"Retirement: {ended_count} listings marked as ended")

    # Step 5: 退役/孤児 SKU に紐づく pending supplier_candidates を auto-reject
    try:
        cleanup = cleanup_stale_supplier_candidates()
        stats["candidates_cleaned"] = cleanup
        if cleanup["rejected_ended"] or cleanup["rejected_orphan"]:
            logger.info(
                f"supplier_candidates cleanup: "
                f"ended={cleanup['rejected_ended']}, orphan={cleanup['rejected_orphan']}"
            )
    except Exception as e:
        logger.warning(f"Failed to cleanup supplier_candidates: {e}")
        stats["errors"] += 1

    return stats


def match_source_status_to_ebay() -> int:
    """
    eBay出品と監視アイテムをSKUでマッチング、仕入元在庫ステータスを更新。

    BUG-2 修正: source_status の遷移に合わせて `source_out_of_stock_since` も
    適切に更新する（在庫有→在庫無 開始日付与、在庫有回復で NULL クリア）。
    これがないと sweep/ダッシュボードの「在庫切れ継続日数」が実際と乖離する。
    """
    from .database import get_conn as _get_conn

    ebay_listings = get_ebay_listings()
    source_items = {item["sku"]: item for item in get_active_items()}

    matched = 0
    for ebay_item in ebay_listings:
        sku = ebay_item.get("sku")
        if not sku or sku not in source_items:
            continue

        source_item = source_items[sku]
        source_status = source_item.get("last_status", "unknown")
        ebay_item_id = ebay_item["ebay_item_id"]
        prev_status = ebay_item.get("source_status")
        prev_oos_since = ebay_item.get("source_out_of_stock_since")

        update_ebay_listing_status(ebay_item_id, source_status)

        # source_out_of_stock_since の同期ロジック
        # 2026-06-05 user 要望: 「ページなし」(仕入先ページ消滅) も 「在庫無」(売切) と
        # 同じ OOS 扱い (long-term OOS 追跡 → supplier_sweep/select の研究対象化)。
        _oos_states = ('在庫無', 'ページなし')
        try:
            if source_status in _oos_states and prev_status not in _oos_states:
                # 新規に仕入先OOS (在庫無 or ページなし) → 開始日セット
                with _get_conn() as conn:
                    conn.execute(
                        "UPDATE ebay_listings SET source_out_of_stock_since=CURRENT_TIMESTAMP "
                        "WHERE ebay_item_id=? AND source_out_of_stock_since IS NULL",
                        (ebay_item_id,),
                    )
            elif source_status == '在庫有' and prev_oos_since:
                # 在庫復活 → 切れ開始日クリア
                with _get_conn() as conn:
                    conn.execute(
                        "UPDATE ebay_listings SET source_out_of_stock_since=NULL "
                        "WHERE ebay_item_id=?",
                        (ebay_item_id,),
                    )
        except Exception as e:
            logger.warning(f"source_out_of_stock_since 更新失敗 {ebay_item_id}: {e}")
        matched += 1
        logger.debug(f"{sku} -> {source_status}")

    return matched


def sync_single_listing(
    ebay_item_id: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """W176-followup (2026-05-27): 1 ItemID のみ GetItem → ebay_listings upsert +
    metrics 更新を実行する高速 sync。eBay連携タブの「1 件のみ同期」ボタンから呼ぶ。

    Returns: {
        success: bool,
        ebay_item_id: str,
        sku: str,
        title: str,
        current_price: float,
        message: str,
    }

    HIGH-1 ガード: SKU 空での upsert は monitored_items 整合性を崩すため warning +
    skip (tab_individual_listing.py W176 patch と同 semantic)。
    """
    init_db()
    out = {
        "success": False,
        "ebay_item_id": ebay_item_id,
        "sku": "",
        "title": "",
        "current_price": 0.0,
        "message": "",
    }

    if not all([app_id, dev_id, cert_id, user_token]):
        out["message"] = "eBay credentials not configured"
        return out
    if not ebay_item_id or not str(ebay_item_id).strip():
        out["message"] = "ebay_item_id is empty"
        return out

    listing = get_single_listing(
        str(ebay_item_id).strip(), app_id, dev_id, cert_id, user_token
    )
    if listing is None:
        out["message"] = (
            f"GetItem returned no item (not found / API error / parse fail). "
            f"item_id={ebay_item_id}"
        )
        return out

    sku = listing.get("sku") or ""
    title = listing.get("title") or ""
    price = float(listing.get("current_price") or 0.0)
    qty = int(listing.get("quantity") or 0)
    shipping = float(listing.get("shipping_cost") or 0.0)

    out["sku"] = sku
    out["title"] = title
    out["current_price"] = price

    # 2026-06-07: eBaymag 各国版(非USD)は取り込まない (bulk sync と同 policy)。
    # 単一同期での再混入を塞ぐ (HIGH-3 fix)。US 本体のみ MonoDeck 管理。
    currency = (listing.get("currency") or "USD")
    if currency != "USD":
        out["message"] = (
            f"eBaymag各国版({currency})のため取込skip (US本体のみ管理)。"
            f"title='{title[:50]}'"
        )
        logger.info(
            f"sync_single_listing skip non-USD: item_id={ebay_item_id} currency={currency}"
        )
        return out

    if not sku.strip():
        out["message"] = (
            "SKU が空のため upsert skip (monitored_items 整合性保護)。"
            f"title='{title[:60]}' price=${price:.2f}"
        )
        logger.warning(
            f"sync_single_listing skip empty SKU: item_id={ebay_item_id} title='{title[:60]}'"
        )
        return out

    try:
        upsert_ebay_listing(
            ebay_item_id=str(ebay_item_id),
            sku=sku,
            title=title,
            current_price=price,
            quantity_ebay=qty,
            shipping_cost=shipping,
            category_id=listing.get("category_id"),  # W222: GetItem の実カテゴリを保存
        )
        update_ebay_listing_metrics(str(ebay_item_id), {
            "watch_count": listing.get("watch_count", 0),
            "view_count": listing.get("view_count", 0),
            "sales_count_30d": listing.get("sales_count_30d", 0),
        })
        out["success"] = True
        out["message"] = (
            f"OK: sku={sku}, qty={qty}, price=${price:.2f}, shipping=${shipping:.2f}"
        )
        logger.info(f"sync_single_listing success: {ebay_item_id} sku={sku}")
    except sqlite3.OperationalError as e:
        # HIGH-2 fix: scheduler の全体同期と競合した時の friendly message
        out["message"] = (
            f"DB ロック競合 (scheduler の全体同期中の可能性)。"
            f"数秒後に再試行してください。詳細: {e}"
        )
        logger.warning(
            f"sync_single_listing SQLite locked: {ebay_item_id} ({e})"
        )
    except Exception as e:  # noqa: BLE001
        out["message"] = f"DB upsert error: {e}"
        logger.exception(f"sync_single_listing DB error: {ebay_item_id}")

    return out


def get_sync_report() -> dict:
    """
    現在の同期状態をレポート
    Returns: {total_ebay, with_source, status_breakdown}
    """
    ebay_listings = get_ebay_listings()
    source_items = {item["sku"]: item for item in get_active_items()}

    status_count = {}
    with_source = 0

    for ebay_item in ebay_listings:
        sku = ebay_item.get("sku")
        status = ebay_item.get("source_status", "unknown")

        if sku in source_items:
            with_source += 1

        status_count[status] = status_count.get(status, 0) + 1

    return {
        "total_ebay": len(ebay_listings),
        "with_source": with_source,
        "status_breakdown": status_count,
    }


def auto_rank_all_listings_in_db() -> dict:
    """
    すべてのeBay出品に対して伸び率を計算しランクを自動割り当て
    Returns: {
        rank_assigned: int,
        errors: int,
        summary: {S: items, A: items, ...},
        distribution: {rank: {count, avg_watch, avg_view, ...}},
    }
    """
    init_db()

    try:
        # 全出品のメトリクスを取得
        all_metrics = get_all_listing_metrics()
        logger.info(f"Processing {len(all_metrics)} listings for auto-ranking")

        rank_assigned = 0
        errors = 0

        # 各出品に対して伸び率計算・ランク割り当て・保存
        for item in all_metrics:
            try:
                ebay_item_id = item['ebay_item_id']

                # 伸び率計算
                watch_rate = calculate_growth_rate(
                    item['watch_count'], item['last_watch_count']
                )
                view_rate = calculate_growth_rate(
                    item['view_count'], item['last_view_count']
                )
                sales_rate = calculate_growth_rate(
                    item['sales_count_30d'], item['last_sales_count_30d']
                )

                # 伸び率をDB保存
                update_ebay_listing_growth_rates(
                    ebay_item_id, watch_rate, view_rate, sales_rate
                )

                # スコア計算 (UI 表示用、ランク判定には使わない)
                score = calculate_metrics_score({
                    'view_count': item['view_count'],
                    'watch_count': item['watch_count'],
                    'sales_count_30d': item['sales_count_30d'],
                    'view_growth_rate': view_rate,
                    'watch_growth_rate': watch_rate,
                })

                # ランク割り当て (Option C: watch/sales 直接マッピング)
                rank = assign_rank({
                    'watch_count': item['watch_count'],
                    'sales_count_30d': item['sales_count_30d'],
                })

                # スコアとランクをDB保存
                update_ebay_listing_metrics_score(ebay_item_id, score, rank)

                rank_assigned += 1
                logger.debug(f"{ebay_item_id}: score={score:.1f}, rank={rank}")

            except Exception as e:
                logger.warning(f"Failed to rank {item.get('ebay_item_id', '?')}: {e}")
                errors += 1

        # ランク分布を取得
        distribution = get_rank_distribution_details()

        logger.info(f"Auto-ranking completed: {rank_assigned} assigned, {errors} errors")

        return {
            'rank_assigned': rank_assigned,
            'errors': errors,
            'distribution': distribution,
        }

    except Exception as e:
        logger.error(f"Failed to auto-rank listings: {e}")
        return {
            'rank_assigned': 0,
            'errors': 1,
            'distribution': {},
        }
