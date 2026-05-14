#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
W183: ライバル価格 refresh + 自動値下げ task.

Phase 3 Clarify (2026-05-10) で確定した user 回答:
- L1: 6 時間に 1 回 (00:45 / 06:45 / 12:45 / 18:45 JST)
- L2: 1 listing につき 1 日 (JST) 4 回まで
- L3: 完全自動 (承認待ちなし)
- L4: price_rule デフォルト 'competitor - 0.01' USD
- L5: min_price 算出 = lp_min_price (user 設定) > 0 ? lp_min_price : lp_breakeven_usd

Pipeline:
1. active competitor を持つ全 listing を抽出
2. refresh_competitor_pricing で Browse API から価格・送料を更新
3. 各 listing について:
    a. 我々の合計 (current_price + shipping_cost) を計算
    b. ライバル最安 (min(competitor_price_usd + competitor_shipping_usd)) を計算
    c. ライバル < 我々 なら値下げ candidate
    d. price_rule から目標価格を計算
    e. min_price floor (lp_min_price > 0 ? lp_min_price : lp_breakeven_usd) でクランプ
    f. L2 (本日 success<4) チェック
    g. ReviseFixedPriceItem 実行 → price_change_log に記録
"""

import logging
import math
from typing import Dict, Optional

from monitor.lowest_price import refresh_competitor_pricing


# eBay Trading API min StartPrice (Fixed Price listing). 0.99 USD 未満は API reject.
EBAY_MIN_START_PRICE_USD = 0.99
# L2: 1 listing につき 1 日 (JST) の値下げ最大回数 (Phase 3 Clarify 2026-05-10).
DAILY_PRICE_CHANGE_CAP = 4

logger = logging.getLogger(__name__)


# ────────────────────────────────────────
# DB helpers
# ────────────────────────────────────────

def _get_listings_with_active_competitors() -> list[str]:
    """active competitor を 1 件以上持つ our_item_id のリストを返す."""
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT our_item_id FROM competitor_products "
            "WHERE is_active=1 AND our_item_id IS NOT NULL AND our_item_id != ''"
        ).fetchall()
    return [r[0] for r in rows]


def _count_today_changes_jst(ebay_item_id: str) -> int:
    """本日 (JST) その listing で success=1 だった値下げ回数を返す.

    SQLite は UTC 保存. JST 換算は +9 hours offset (sqlite-timezone rule 準拠).
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM price_change_log "
            "WHERE ebay_item_id=? AND success=1 "
            "  AND DATE(changed_at, '+9 hours') = DATE('now', '+9 hours')",
            (ebay_item_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def _log_price_change(
    ebay_item_id: str,
    old_price_usd: Optional[float],
    new_price_usd: Optional[float],
    competitor_item_id: Optional[str],
    competitor_total_usd: Optional[float],
    rule_applied: Optional[str],
    triggered_by: str,
    success: bool,
    error_message: Optional[str] = None,
):
    """price_change_log に履歴を 1 行 INSERT."""
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_change_log "
            "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
            " competitor_total_usd, rule_applied, triggered_by, success, "
            " error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ebay_item_id, old_price_usd, new_price_usd, competitor_item_id,
             competitor_total_usd, rule_applied, triggered_by,
             1 if success else 0, error_message)
        )


def _get_listing_state(ebay_item_id: str) -> Optional[dict]:
    """値下げ判定に必要な listing 情報を 1 件取得.

    Returns: {current_price, shipping_cost, lp_min_price, lp_breakeven_usd, is_ended}
    or None (listing 不在).
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT current_price, shipping_cost, lp_min_price, "
            "       lp_breakeven_usd, is_ended "
            "FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()
    if not row:
        return None
    return {
        'current_price': row[0],
        'shipping_cost': row[1] or 0.0,
        'lp_min_price': row[2],
        'lp_breakeven_usd': row[3],
        'is_ended': bool(row[4]),
    }


def _get_min_competitor(ebay_item_id: str) -> Optional[dict]:
    """active ライバルのうち competitor_total が最小の 1 件を返す.

    Returns: {competitor_id, competitor_item_id, price_usd, shipping_usd, total_usd}
    or None (価格未取得).
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, competitor_item_id, competitor_price_usd, "
            "       competitor_shipping_usd "
            "FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1 "
            "  AND competitor_price_usd IS NOT NULL",
            (ebay_item_id,)
        ).fetchall()
    if not rows:
        return None

    best = None
    for r in rows:
        price = r[2]
        ship = r[3] or 0.0
        total = float(price) + float(ship)
        if best is None or total < best['total_usd']:
            best = {
                'competitor_id': r[0],
                'competitor_item_id': r[1],
                'price_usd': float(price),
                'shipping_usd': float(ship),
                'total_usd': total,
            }
    return best


# ────────────────────────────────────────
# 価格計算
# ────────────────────────────────────────

def _compute_target_price(
    competitor_total: float,
    our_shipping: float,
    rule: str,
) -> Optional[float]:
    """price_rule から目標 StartPrice (USD) を計算.

    現状サポート: 'competitor - 0.01' のみ (L4 default).
    将来 rule 拡張する場合はここに分岐追加.

    H2 fix: Python round の bankers' rounding が買い手 total を競合と同額化する
    事故を防ぐため、整数セントで切り捨て (math.floor) する.
    H3 fix: eBay min StartPrice (0.99 USD) 未満は None を返し API 呼出無駄打ち回避.

    Returns: 目標 StartPrice (USD, 0.99 以上) or None (rule 不明 / floor 未達).
    """
    rule_norm = (rule or '').strip().lower().replace(' ', '')
    if rule_norm in ('competitor-0.01', 'competitor-.01'):
        # 買い手 total を必ず competitor_total - 0.01 未満にしたい.
        # StartPrice = buyer_total - our_shipping
        # competitor_total は floor で下側確定 (eBay は 2 桁表示なので情報損失なし)、
        # our_shipping は ceil で上側確定 (DB 端数による上振れを最悪ケース吸収).
        # → 結果として StartPrice が 1 cent 余分に下がる方向に保守化される.
        competitor_cents = int(math.floor(competitor_total * 100 + 1e-9))
        shipping_cents = int(math.ceil(our_shipping * 100 - 1e-9))
        target_buyer_cents = competitor_cents - 1
        target_start_cents = target_buyer_cents - shipping_cents
        if target_start_cents < int(round(EBAY_MIN_START_PRICE_USD * 100)):
            return None
        return target_start_cents / 100.0
    return None


def _decide_floor_price(state: dict) -> Optional[float]:
    """min_price floor を決定 (L5).

    lp_min_price > 0 なら lp_min_price.
    そうでなく lp_breakeven_usd > 0 なら lp_breakeven_usd.
    両方欠けていれば None (= floor 未設定 → 安全側で値下げ skip).
    """
    lp_min = state.get('lp_min_price')
    if lp_min is not None and lp_min > 0:
        return float(lp_min)
    lp_be = state.get('lp_breakeven_usd')
    if lp_be is not None and lp_be > 0:
        return float(lp_be)
    return None


# ────────────────────────────────────────
# 値下げ実行
# ────────────────────────────────────────

def _evaluate_and_apply_one(
    ebay_item_id: str,
    config: Dict,
    triggered_by: str = 'auto_6h_batch',
) -> dict:
    """1 listing について評価 → 必要なら ReviseFixedPriceItem 実行.

    Returns:
        {
            'action': 'reduced' / 'skip_no_competitor' / 'skip_competitor_price_unknown'
                    / 'skip_already_cheapest' / 'skip_no_floor' / 'skip_below_floor'
                    / 'skip_daily_cap' / 'skip_listing_ended' / 'skip_invalid_state'
                    / 'failed_api',
            'old_price': float / None,
            'new_price': float / None,
            'competitor_total': float / None,
            'message': str,
        }
    """
    state = _get_listing_state(ebay_item_id)
    if state is None:
        return {'action': 'skip_invalid_state', 'message': 'listing 不在'}
    if state['is_ended']:
        return {'action': 'skip_listing_ended', 'message': 'is_ended=1'}
    our_price = state['current_price']
    if our_price is None or our_price <= 0:
        return {'action': 'skip_invalid_state', 'message': f'current_price={our_price}'}
    our_total = float(our_price) + float(state['shipping_cost'])

    competitor = _get_min_competitor(ebay_item_id)
    if competitor is None:
        return {'action': 'skip_competitor_price_unknown', 'message': 'competitor price 未取得'}
    if competitor['total_usd'] >= our_total:
        return {
            'action': 'skip_already_cheapest',
            'message': f"our_total=${our_total:.2f} <= competitor=${competitor['total_usd']:.2f}",
            'competitor_total': competitor['total_usd'],
        }

    # 既存出品の price_rule を取得 (L4 default は upsert で 'competitor - 0.01' 設定済)
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT price_rule FROM competitor_products WHERE id=?",
            (competitor['competitor_id'],)
        ).fetchone()
    rule = row[0] if row else 'competitor - 0.01'

    target_price = _compute_target_price(
        competitor['total_usd'], state['shipping_cost'], rule
    )
    if target_price is None:
        # H7 fix (Q0): 痕跡を必ず log に残す. unsupported rule / target<min_StartPrice の
        # 両ケースを区別したいが、現状 None 返しで識別困難. 今は msg で判別.
        msg = f'price calc failed (rule={rule}, comp=${competitor["total_usd"]:.2f}, ship=${state["shipping_cost"]:.2f})'
        _log_price_change(
            ebay_item_id, float(our_price), None,
            competitor['competitor_item_id'], competitor['total_usd'],
            rule, triggered_by, success=False, error_message=msg,
        )
        return {
            'action': 'skip_invalid_state',
            'message': msg,
        }

    # L5 min_price floor
    floor = _decide_floor_price(state)
    if floor is None:
        return {
            'action': 'skip_no_floor',
            'message': 'lp_min_price / lp_breakeven_usd 両方欠落 — 安全側で skip',
        }
    if target_price < floor:
        return {
            'action': 'skip_below_floor',
            'message': f'target=${target_price:.2f} < floor=${floor:.2f}',
            'competitor_total': competitor['total_usd'],
        }

    # 値下げ方向のみ許容 (現価以上に上げる方向は別タスクの責務)
    if target_price >= float(our_price):
        return {
            'action': 'skip_already_cheapest',
            'message': f"target=${target_price:.2f} >= current=${our_price:.2f}",
            'competitor_total': competitor['total_usd'],
        }

    # L2 daily cap (本日 JST、success=1 のみ count、auto/manual 共通枠).
    # 注: 別プロセス間の race (scheduler vs Streamlit) は完全に防げない (確認は H4 で TODO).
    today_count = _count_today_changes_jst(ebay_item_id)
    if today_count >= DAILY_PRICE_CHANGE_CAP:
        return {
            'action': 'skip_daily_cap',
            'message': f'本日 {today_count}/{DAILY_PRICE_CHANGE_CAP} 回到達済み',
            'competitor_total': competitor['total_usd'],
        }

    # ReviseFixedPriceItem 実行
    from monitor.credentials import get_ebay_credentials, ebay_credentials_ok
    creds = get_ebay_credentials(config)
    if not ebay_credentials_ok(creds):
        msg = 'eBay 認証情報未設定'
        _log_price_change(
            ebay_item_id, float(our_price), target_price,
            competitor['competitor_item_id'], competitor['total_usd'],
            rule, triggered_by, success=False, error_message=msg,
        )
        return {
            'action': 'failed_api',
            'message': msg,
            'old_price': float(our_price),
            'new_price': target_price,
        }

    from monitor.ebay_client import revise_fixed_price_item
    api_result = revise_fixed_price_item(
        ebay_item_id, target_price,
        creds['app_id'], creds['dev_id'], creds['cert_id'], creds['user_token'],
    )

    if api_result.get('success'):
        # DB 反映 (eBay 側も更新済) + log
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET current_price=? WHERE ebay_item_id=?",
                (target_price, ebay_item_id)
            )
        _log_price_change(
            ebay_item_id, float(our_price), target_price,
            competitor['competitor_item_id'], competitor['total_usd'],
            rule, triggered_by, success=True,
        )
        logger.info(
            f"W183 値下げ: {ebay_item_id} ${our_price:.2f}→${target_price:.2f} "
            f"(competitor=${competitor['total_usd']:.2f}, by={triggered_by})"
        )
        return {
            'action': 'reduced',
            'old_price': float(our_price),
            'new_price': target_price,
            'competitor_total': competitor['total_usd'],
            'message': f'reduced ${our_price:.2f}→${target_price:.2f}',
        }

    err = api_result.get('message', 'unknown API error')
    _log_price_change(
        ebay_item_id, float(our_price), target_price,
        competitor['competitor_item_id'], competitor['total_usd'],
        rule, triggered_by, success=False, error_message=err,
    )
    logger.warning(f"W183 値下げ失敗: {ebay_item_id} → {err}")
    return {
        'action': 'failed_api',
        'old_price': float(our_price),
        'new_price': target_price,
        'message': err,
    }


# ────────────────────────────────────────
# entry point
# ────────────────────────────────────────

def run_rival_pricing_refresh(config: Dict) -> Dict:
    """
    全 listing で active ライバル価格を refresh し、必要に応じて値下げを実行.

    Returns:
        {
            'success': bool,
            'listings_processed': int,
            'fetched_total': int,
            'failed_total': int,
            'reduced': int,
            'skipped_already_cheapest': int,
            'skipped_below_floor': int,
            'skipped_daily_cap': int,
            'skipped_other': int,
            'failed_api': int,
            'message': str,
        }
    """
    logger.info("【開始】W183 ライバル価格 refresh + 値下げ判定")

    listing_ids = _get_listings_with_active_competitors()
    if not listing_ids:
        logger.info("active ライバルを持つ listing なし — skip")
        return {
            'success': True,
            'listings_processed': 0,
            'fetched_total': 0,
            'failed_total': 0,
            'reduced': 0,
            'skipped_already_cheapest': 0,
            'skipped_below_floor': 0,
            'skipped_daily_cap': 0,
            'skipped_other': 0,
            'failed_api': 0,
            'message': 'no listings with active competitors',
        }

    fetched_total = 0
    failed_total = 0
    counts = {
        'reduced': 0,
        'skipped_already_cheapest': 0,
        'skipped_below_floor': 0,
        'skipped_daily_cap': 0,
        'skipped_other': 0,
        'failed_api': 0,
    }

    for our_item_id in listing_ids:
        # Phase 1: ライバル価格 refresh
        try:
            r = refresh_competitor_pricing(our_item_id, config)
        except Exception as e:
            logger.warning(f"refresh_competitor_pricing 失敗 ({our_item_id}): {e}")
            failed_total += 1
            counts['skipped_other'] += 1
            continue
        fetched_total += r.get('fetched', 0)
        failed_total += r.get('failed', 0)

        # Phase 2: 値下げ判定 + 実行
        try:
            decision = _evaluate_and_apply_one(our_item_id, config, 'auto_6h_batch')
        except Exception as e:
            logger.warning(f"_evaluate_and_apply_one 失敗 ({our_item_id}): {e}")
            counts['skipped_other'] += 1
            continue

        action = decision.get('action', 'skipped_other')
        if action == 'reduced':
            counts['reduced'] += 1
        elif action == 'skip_already_cheapest':
            counts['skipped_already_cheapest'] += 1
        elif action == 'skip_below_floor':
            counts['skipped_below_floor'] += 1
        elif action == 'skip_daily_cap':
            counts['skipped_daily_cap'] += 1
        elif action == 'failed_api':
            counts['failed_api'] += 1
        else:
            counts['skipped_other'] += 1

    summary = (
        f'{len(listing_ids)} listings | '
        f"fetched={fetched_total} failed={failed_total} | "
        f"reduced={counts['reduced']} cheapest={counts['skipped_already_cheapest']} "
        f"below_floor={counts['skipped_below_floor']} cap={counts['skipped_daily_cap']} "
        f"api_fail={counts['failed_api']} other={counts['skipped_other']}"
    )
    logger.info(f"W183 完了: {summary}")
    return {
        'success': True,
        'listings_processed': len(listing_ids),
        'fetched_total': fetched_total,
        'failed_total': failed_total,
        **counts,
        'message': summary,
    }
