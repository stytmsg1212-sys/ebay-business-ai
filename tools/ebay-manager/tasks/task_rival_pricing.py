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
# H5 (2026-05-17): 値下げ直後の eBay ActiveList 伝播遅延中に ebay_sync が
# 古い高値を current_price に巻き戻す race を回避するガード窓 (時間).
# 直近 success 値下げが本窓内 かつ その後 sync が走り DB 価格が「最後に
# 自分が設定した値」と食い違う時のみ、その回の判定を見送る (次サイクルで
# settle 済みを処理 = 最大 1 サイクル遅延、恒久凍結なし)。cron は 6h 間隔.
STALE_PRICE_GUARD_HOURS = 6

# H4 (2026-05-17 code-reviewer HIGH-1): 予約取得時に DB 書込ロック競合
# (sqlite3.OperationalError "database is locked") が出た場合の戻り値。
# None (本日上限到達) と区別し、skip_daily_cap への誤分類 + silent
# 取りこぼし (Q0 違反) を防ぐ専用 sentinel。
_SLOT_LOCKED = object()

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

    Returns: {current_price, shipping_cost, lp_min_price, lp_breakeven_usd,
              is_ended, last_synced_at} or None (listing 不在).

    last_synced_at は ebay_sync (upsert_ebay_listing) が書く JST naive
    文字列 ('%Y-%m-%d %H:%M:%S')。H5 stale 判定で使う (timezone 注意:
    price_change_log.changed_at は UTC なので比較時に揃える)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT current_price, shipping_cost, lp_min_price, "
            "       lp_breakeven_usd, is_ended, last_synced_at "
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
        'last_synced_at': row[5],
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
# H5: stale sync 上書き検知
# ────────────────────────────────────────

def _latest_successful_change(ebay_item_id: str) -> Optional[dict]:
    """この listing で最後に成功した値下げ記録を 1 件返す.

    price_change_log.changed_at は UTC (CURRENT_TIMESTAMP)、
    ebay_listings.last_synced_at は JST (datetime.now())。両者を比較する
    ため changed_at を +9h して JST 文字列も返す (sqlite-timezone.md 準拠:
    比較は同一 TZ・同一フォーマットで)。
    予約 (claim_status='pending') 行は確定前なので除外する。

    Returns: {
        'new_price_usd': float,            # 最後に自分が eBay に設定した価格
        'changed_at_utc': str,
        'changed_at_jst': str,             # last_synced_at(JST) と直接比較可
        'within_guard_window': bool,       # 直近値下げが GUARD_HOURS 以内か
    } or None (成功履歴なし).
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT new_price_usd, changed_at, "
            "       datetime(changed_at, '+9 hours') AS changed_at_jst, "
            "       (changed_at >= datetime('now', ?)) AS within_window "
            "FROM price_change_log "
            "WHERE ebay_item_id=? AND success=1 "
            "  AND (claim_status IS NULL OR claim_status='final') "
            "ORDER BY changed_at DESC LIMIT 1",
            (f'-{STALE_PRICE_GUARD_HOURS} hours', ebay_item_id)
        ).fetchone()
    if not row or row[0] is None:
        return None
    return {
        'new_price_usd': float(row[0]),
        'changed_at_utc': row[1],
        'changed_at_jst': row[2],
        'within_guard_window': bool(row[3]),
    }


def _is_price_stale_suspect(state: dict, ebay_item_id: str) -> Optional[str]:
    """current_price が「値下げ直後の stale sync 上書き」の疑いか判定 (H5).

    suspect (= 理由文字列を返す) の条件 (全 AND):
      1. 直近 success 値下げが STALE_PRICE_GUARD_HOURS 以内 (= 伝播遅延の
         危険窓内。古い値下げは settle 済みなので DB を信頼)
      2. last_synced_at が その値下げ後 (= sync が値下げ後に走り上書き
         し得た。JST 同士で比較)
      3. DB current_price が「最後に自分が設定した価格」と 0.005 USD 以上
         食い違う (= sync が我々の値下げを別値に書き換えた疑い)

    → この回は値下げ判定を見送る。stale-high を基準に再計算して eBay 実
    価格を誤って引き上げる事故を防ぐ。健全 (sync が正しい値を観測 / sync
    が値下げ前 / 値下げが古い) なら None で続行。

    Returns: skip 理由文字列 (suspect) or None (健全 → 続行).
    """
    last = _latest_successful_change(ebay_item_id)
    if last is None:
        return None  # 成功履歴なし = 巻戻し対象なし
    if not last['within_guard_window']:
        return None  # 直近値下げが古い = settle 済み、DB を信頼
    lsa = state.get('last_synced_at')
    if not lsa:
        return None  # sync 記録なし = 上書きされていない
    # last_synced_at(JST) と changed_at_jst(JST) を同 TZ・同フォーマットで比較
    if str(lsa) <= str(last['changed_at_jst']):
        return None  # sync は値下げより前 = 上書きしていない
    cp = state.get('current_price')
    if cp is None:
        return None
    if abs(float(cp) - last['new_price_usd']) < 0.005:
        return None  # 一致 = sync が正しい値を観測 = 健全
    return (
        f"stale-suspect: DB current=${float(cp):.2f} != last applied "
        f"${last['new_price_usd']:.2f} (synced_at={lsa} JST > reduced_at="
        f"{last['changed_at_jst']} JST); skip this cycle"
    )


# ────────────────────────────────────────
# H4: 値下げ枠の atomic 予約 (cross-process race 対策)
# ────────────────────────────────────────

def _claim_price_change_slot(
    ebay_item_id: str,
    old_price_usd: float,
    target_price: float,
    competitor_item_id: Optional[str],
    competitor_total_usd: Optional[float],
    rule_applied: Optional[str],
    triggered_by: str,
) -> Optional[int]:
    """本日 (JST) の値下げ枠を atomic に 1 つ予約する (H4 race 対策).

    add_api_cost と同じ BEGIN IMMEDIATE idiom: 書込ロックを取ってから
    本日消費済み枠を数え、上限未満なら予約行 (claim_status='pending') を
    INSERT して COMMIT。scheduler プロセスと Streamlit プロセスが同じ
    listing を同時に処理しても、2 人目は 1 人目の予約を見て弾かれる
    (SQLite は writer を直列化、IMMEDIATE で SELECT 前にロック確定)。

    消費枠 (本日 JST) = 次の OR:
      - success=1                                  : 確定消費
      - claim_status='pending' かつ直近 15 分以内    : eBay API 未呼出の予約。
        crash で漏れた pending は 15 分で時効 (= 実 eBay 変更が起きていない
        ことが確実なので解放してよい)
      - claim_status='api_inflight'  (時効なし)     : eBay API 呼出に進んだ
        予約。Codex 2 段レビュー HIGH (2026-05-17): API 成功直後〜finalize
        前に crash すると eBay 側は値下げ済なのに DB 未確定。これを 15 分で
        時効にすると同日さらに予約でき 1 日 4 回上限を超過 (過剰値下げ =
        margin 浸食)。保守的に「成功したかもしれない」前提で時効なしで枠を
        消費し続ける (確定失敗のみ _finalize で解放、user 確定仕様と非矛盾:
        user 決定は『確定失敗』対象、crash の結果不明は別ケース)。

    Returns:
        - 予約行 id (int)         : 確保成功
        - None                    : 本日上限到達 (skip_daily_cap)
        - _SLOT_LOCKED (sentinel) : DB 書込ロック競合 (skip_lock_contention)。
          HIGH-1 (code-reviewer 2026-05-17): BEGIN IMMEDIATE 自体が
          busy_timeout 超で OperationalError "database is locked" を投げる
          (refresh_competitor_pricing の Browse API 区間と重なると現実的に
          発生)。これは H4 が守る並行シナリオそのものなので、上限到達と
          区別し痕跡を残してその回見送りにする (silent 取りこぼし回避)。
    """
    import sqlite3 as _sq
    from monitor.database import get_conn
    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COUNT(*) FROM price_change_log "
                "WHERE ebay_item_id=? "
                "  AND DATE(changed_at, '+9 hours') = DATE('now', '+9 hours') "
                "  AND ( success=1 "
                "        OR claim_status='api_inflight' "
                "        OR (claim_status='pending' "
                "            AND changed_at >= datetime('now','-15 minutes')) )",
                (ebay_item_id,)
            ).fetchone()
            consumed = int(row[0]) if row else 0
            if consumed >= DAILY_PRICE_CHANGE_CAP:
                conn.execute("COMMIT")
                return None
            cur = conn.execute(
                "INSERT INTO price_change_log "
                "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
                " competitor_total_usd, rule_applied, triggered_by, success, "
                " claim_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending')",
                (ebay_item_id, old_price_usd, target_price, competitor_item_id,
                 competitor_total_usd, rule_applied, triggered_by)
            )
            claim_id = cur.lastrowid
            conn.execute("COMMIT")
            return claim_id
        except _sq.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except _sq.Error:
                pass
            if 'lock' in str(e).lower():
                # ロック競合: 上限到達と区別し痕跡を残してその回見送り (Q0)
                logger.warning(
                    f"W183 claim slot ロック競合 ({ebay_item_id}): {e} "
                    f"— この回見送り (次サイクル/再試行で解消)"
                )
                return _SLOT_LOCKED
            # lock 以外の OperationalError (schema 不整合等) は隠さず送出
            raise
        except _sq.Error:
            try:
                conn.execute("ROLLBACK")
            except _sq.Error:
                pass
            raise


def _mark_claim_inflight(claim_id: int):
    """予約行を 'pending' → 'api_inflight' に遷移 (eBay API 呼出直前).

    Codex 2 段レビュー HIGH (2026-05-17): この遷移以降の crash は「eBay 側で
    値下げが成功したかもしれない」状態。api_inflight は枠カウントで時効なし
    なので、crash しても同日その枠を消費し続け 1 日 4 回上限超過 (過剰値下げ)
    を防ぐ。遷移前 (claim INSERT 直後の極短窓、ネットワーク前) の crash は
    pending のまま 15 分時効 = 実 eBay 変更なし確実なので解放してよい。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE price_change_log SET claim_status='api_inflight' "
            "WHERE id=? AND claim_status='pending'",
            (claim_id,)
        )


def _finalize_price_change(
    claim_id: int,
    success: bool,
    new_price_usd: Optional[float] = None,
    error_message: Optional[str] = None,
):
    """予約行 (claim_status='pending' / 'api_inflight') を確定する.

    success=True  → success=1, claim_status='final' (枠を恒久消費)
    success=False → success=0, claim_status='final' (枠を解放 = 失敗は
                    本日 4 回にカウントしない / user 確定 2026-05-17)。
                    行自体は残るので Q0 silent skip 違反にならない
                    (error_message に痕跡)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        if success:
            conn.execute(
                "UPDATE price_change_log "
                "SET success=1, claim_status='final', "
                "    new_price_usd=COALESCE(?, new_price_usd) "
                "WHERE id=?",
                (new_price_usd, claim_id)
            )
        else:
            conn.execute(
                "UPDATE price_change_log "
                "SET success=0, claim_status='final', error_message=? "
                "WHERE id=?",
                (error_message, claim_id)
            )


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
                    / 'skip_stale_price' / 'skip_lock_contention' / 'failed_api',
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

    # H5 (2026-05-17): 値下げ直後の stale sync 上書き疑いならこの回は見送る.
    # stale-high な current_price を基準に再計算して eBay 実価格を誤って
    # 引き上げる事故 (最安ポジション喪失) を防ぐ. 次サイクルで settle 済みを処理.
    _stale_reason = _is_price_stale_suspect(state, ebay_item_id)
    if _stale_reason is not None:
        logger.warning(f"W183 skip_stale_price {ebay_item_id}: {_stale_reason}")
        return {'action': 'skip_stale_price', 'message': _stale_reason}

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

    # 認証情報チェック (恒久設定不備で予約行を作らないよう claim より前).
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

    # H4 (2026-05-17): L2 daily cap を atomic 予約で強制 (auto/manual 共通枠).
    # COUNT→API→INSERT の隙間で scheduler/Streamlit が同 listing を二重
    # 値下げして cap=4 を 5 にする race を、API 実行前の予約で防ぐ.
    # 予約が取れなければ本日上限到達 = skip_daily_cap、ロック競合は別 action.
    claim_id = _claim_price_change_slot(
        ebay_item_id, float(our_price), target_price,
        competitor['competitor_item_id'], competitor['total_usd'],
        rule, triggered_by,
    )
    if claim_id is _SLOT_LOCKED:
        # HIGH-1: lock 競合を skip_daily_cap と混同しない (user 誤誘導防止 +
        # Q0 痕跡。warning は _claim_price_change_slot 内で既出、summary は
        # skipped_lock counter で可視化).
        return {
            'action': 'skip_lock_contention',
            'message': 'DB 書込ロック競合のため値下げ判定を見送り (次サイクル/再試行で解消)',
            'competitor_total': competitor['total_usd'],
        }
    if claim_id is None:
        return {
            'action': 'skip_daily_cap',
            'message': f'本日 {DAILY_PRICE_CHANGE_CAP}/{DAILY_PRICE_CHANGE_CAP} 回 (予約上限) 到達済み',
            'competitor_total': competitor['total_usd'],
        }

    # Codex HIGH (2026-05-17): API 呼出に進む = この先 crash すると eBay 側
    # で値下げ成功済の可能性。予約を api_inflight に上げ、時効なしで枠を
    # 消費し続けさせる (crash 後の上限超過 = 過剰値下げ防止)。
    _mark_claim_inflight(claim_id)

    # ReviseFixedPriceItem 実行 (予約済 = 確定失敗時のみ枠解放、成功で枠確定)
    from monitor.ebay_client import revise_fixed_price_item
    api_result = revise_fixed_price_item(
        ebay_item_id, target_price,
        creds['app_id'], creds['dev_id'], creds['cert_id'], creds['user_token'],
    )

    if api_result.get('success'):
        # DB 反映 (eBay 側も更新済) + 予約を成功確定
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET current_price=? WHERE ebay_item_id=?",
                (target_price, ebay_item_id)
            )
        _finalize_price_change(claim_id, success=True, new_price_usd=target_price)
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
    _finalize_price_change(claim_id, success=False, error_message=err)
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
            'skipped_stale': 0,
            'skipped_lock': 0,
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
        'skipped_stale': 0,
        'skipped_lock': 0,
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
        elif action == 'skip_stale_price':
            counts['skipped_stale'] += 1
        elif action == 'skip_lock_contention':
            counts['skipped_lock'] += 1
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
        f"below_floor={counts['skipped_below_floor']} stale={counts['skipped_stale']} "
        f"lock={counts['skipped_lock']} cap={counts['skipped_daily_cap']} "
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
