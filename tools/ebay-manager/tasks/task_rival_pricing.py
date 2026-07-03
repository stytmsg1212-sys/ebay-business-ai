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

W301 S3 (2026-07-02, AI 店長 Phase1): 抽出は「active (is_active=1) かつ値下げ
適格 (pricing_eligible=1)」の競合のみに限定 (`.company/engineering/docs/
2026-06-24-ai-manager-phase1-design.md` §8)。Shadow / 未分類 (pricing_eligible
NULL/0) の競合は値下げ対象から除外される (除外件数は run summary ログに出す)。

Pipeline:
1. active かつ値下げ適格 (pricing_eligible=1) の competitor を持つ全 listing を抽出
2. refresh_competitor_pricing で Browse API から価格・送料を更新
3. 各 listing について:
    a. 我々の合計 (current_price + shipping_cost) を計算
    b. ライバル最安 (min(competitor_price_usd + competitor_shipping_usd)) を計算
    c. ライバル < 我々 なら値下げ candidate
    d. price_rule から目標価格を計算
    d2. (2026-07-02 user 指示・第 2 安全弁) 1 回の下げ幅は現価格の 5% まで clamp
    e. min_price floor (lp_min_price > 0 ? lp_min_price : lp_breakeven_usd) でクランプ
    f. L2 (本日 success<4) チェック
    g. ReviseFixedPriceItem 実行 → price_change_log に記録
    h. (2026-07-02 user 指示・第 3 安全弁) 直近 7 日で値下げ 3 連続 (間に値上げ
       なし) なら Discord アラート (通知のみ、値下げは止めない、1 日 1 回 dedupe)
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

# 2026-07-02 (user 指示): 値下げ合戦スパイラル抑止のための第 2・第 3 の安全弁。
# 既存 L5 (lp_min_price 床) は一切変更せず併用する独立ガード
# (`.company/engineering/docs/2026-06-24-ai-manager-phase1-design.md` L65
#  「⚠️ 2026-07-02 user 改訂 (実装必須)」)。
# 第 2 安全弁: 1 回の実行での値下げ幅は現価格の 5% まで (段階的降下)。
MAX_SINGLE_DROP_PCT = 0.05
# 第 3 安全弁: 同一 listing が直近 WINDOW_DAYS 以内に THRESHOLD 回連続値下げ
# (間に値上げなし) したら Discord アラート (通知のみ、値下げ自体は止めない)。
CONSECUTIVE_REDUCTION_ALERT_THRESHOLD = 3
CONSECUTIVE_REDUCTION_WINDOW_DAYS = 7

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
    """active かつ値下げ適格 (pricing_eligible=1) の competitor を 1 件以上持つ
    our_item_id のリストを返す.

    W301 S3 (2026-07-02, AI 店長 Phase1): `pricing_eligible` は採用(is_active)
    とは独立した値下げ適格フラグ (migration v86)。Shadow / 未分類 (NULL/0) の
    競合は W183 の対象から除外する (設計書 §8「pricing_eligible分離」)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT our_item_id FROM competitor_products "
            "WHERE is_active=1 AND COALESCE(pricing_eligible,0)=1 "
            "  AND our_item_id IS NOT NULL AND our_item_id != ''"
        ).fetchall()
    return [r[0] for r in rows]


def _count_gated_out_competitors() -> int:
    """active (is_active=1) だが値下げ適格でない (pricing_eligible!=1) 競合の件数.

    Q0 (silent-skip-prevention): ゲートで W183 対象から除外された競合が
    存在する run では、対象が黙って減ったように見えないよう run summary に
    必ず件数を出す (呼出元で 0 件でもログに残す運用と合わせる)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM competitor_products "
            "WHERE is_active=1 AND COALESCE(pricing_eligible,0)!=1"
        ).fetchone()
    return int(row[0]) if row else 0


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
    """active かつ値下げ適格 (pricing_eligible=1) なライバルのうち
    competitor_total が最小の 1 件を返す.

    W301 S3 (2026-07-02): pricing_eligible ゲート (`_get_listings_with_active_
    competitors` と同一趣旨、両方に必要 — 前者は listing 単位の一次フィルタ、
    本関数は listing 内の競合単位フィルタ)。

    Returns: {competitor_id, competitor_item_id, price_usd, shipping_usd, total_usd}
    or None (価格未取得 / 値下げ適格な競合なし).
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, competitor_item_id, competitor_price_usd, "
            "       competitor_shipping_usd "
            "FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1 "
            "  AND COALESCE(pricing_eligible,0)=1 "
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
# 2026-07-02 (user 指示): 値下げ合戦スパイラル抑止 — 第 2・第 3 の安全弁
# 既存 L5 (lp_min_price 床、_decide_floor_price) は本節の外側で無変更のまま
# 併用される (clamp 後の価格が床を割るなら床側が勝つ)。
# ────────────────────────────────────────

def _apply_max_drop_clamp(our_price: float, target_price: float) -> tuple[float, bool]:
    """1 回の値下げ幅を現価格の MAX_SINGLE_DROP_PCT (5%) までに制限する (第 2 安全弁).

    目標価格が current_price * 0.95 未満なら current_price * 0.95 に clamp する
    (次回実行でまた最大 5% 下がる = 段階的降下)。ちょうど 5% の下げは clamp
    しない (境界は許容側)。

    H2 (_compute_target_price) と同じ理由で整数セント判定にする (float 誤差で
    境界を誤判定しない)。5% ライン自体は ceil で確定し、実際の下落率が
    5% を "超えない" 方向に丸める (5% ちょうどは非 clamp のまま許容).

    Returns: (適用する target_price, clamp が発動したか).
    """
    our_cents = int(round(float(our_price) * 100))
    floor_cents = math.ceil(our_cents * (1 - MAX_SINGLE_DROP_PCT) - 1e-9)
    target_cents = int(round(float(target_price) * 100))
    if target_cents < floor_cents:
        return floor_cents / 100.0, True
    return target_price, False


def _check_consecutive_reduction_streak(ebay_item_id: str) -> Optional[dict]:
    """直近 CONSECUTIVE_REDUCTION_WINDOW_DAYS 日以内で値下げ
    CONSECUTIVE_REDUCTION_ALERT_THRESHOLD 回連続 (間に値上げなし) か判定する
    (第 3 安全弁: 値下げ合戦スパイラル検知)。

    判定対象: price_change_log の success=1 行を changed_at 降順に閾値件数だけ
    取得する。
      - 件数が閾値未満                          → 非対象 (None)
      - いずれか 1 件でも「値下げでない」        → 非対象 (None、ストリーム
        リセット済み。値上げ/同額/価格欠損のいずれか)
      - 最も古い 1 件が WINDOW_DAYS 超前         → 非対象 (None、window 外)
      - 上記いずれにも当たらない                → 発火情報を返す

    アラートは通知のみ (値下げ自体は止めない、停止判断は user)。

    Returns: {'count': int, 'oldest_changed_at': str, 'newest_changed_at': str,
              'prices': list[float] (新しい順の new_price_usd)} or None.
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT old_price_usd, new_price_usd, changed_at "
            "FROM price_change_log WHERE ebay_item_id=? AND success=1 "
            "ORDER BY changed_at DESC, id DESC LIMIT ?",
            (ebay_item_id, CONSECUTIVE_REDUCTION_ALERT_THRESHOLD)
        ).fetchall()
    if len(rows) < CONSECUTIVE_REDUCTION_ALERT_THRESHOLD:
        return None
    for old_p, new_p, _ in rows:
        if old_p is None or new_p is None or not (float(new_p) < float(old_p)):
            return None  # 値上げ / 同額 / 欠損 = ストリーク非成立 (リセット済)
    oldest_changed_at = rows[-1][2]
    with get_conn() as conn:
        in_window = conn.execute(
            "SELECT ? >= datetime('now', ?)",
            (oldest_changed_at, f'-{CONSECUTIVE_REDUCTION_WINDOW_DAYS} days')
        ).fetchone()[0]
    if not in_window:
        return None
    return {
        'count': len(rows),
        'oldest_changed_at': oldest_changed_at,
        'newest_changed_at': rows[0][2],
        'prices': [float(r[1]) for r in rows],
    }


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

    # L5 min_price floor (raw target で判定).
    # 2026-07-02 main HIGH 修正: clamp を先に適用すると raw が床割れ (旧: skip)
    # の商品も clamp 後価格が床超になり値下げが「解禁」→ 競合には勝てないのに
    # 利幅だけ削る純損経路が発生した。clamp は「承認された値下げの幅を制限」
    # であり「旧来ブロックされていた値下げを解禁」ではない、という仕様に沿って
    # 床判定を raw target のまま行い、旧意味論を完全維持する.
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

    # 値下げ方向のみ許容 (現価以上に上げる方向は別タスクの責務、raw target で判定).
    if target_price >= float(our_price):
        return {
            'action': 'skip_already_cheapest',
            'message': f"target=${target_price:.2f} >= current=${our_price:.2f}",
            'competitor_total': competitor['total_usd'],
        }

    # 2026-07-02 (user 指示・第 2 安全弁): 床 + 方向を通過した「承認済み値下げ」
    # に対してのみ、1 回の下げ幅を現価格の 5% まで clamp する.
    # clamp は target を raw より "現価格側" にしか動かさない (max(raw, 0.95*our)):
    #   - raw ≥ floor が既に確認済 ∧ clamp 後 ≥ raw → 自明に clamp 後 ≥ floor
    #   - raw < our_price ∧ clamp 後 ≤ 0.95*our_price < our_price → 方向反転しない
    # よって clamp 後の再チェックは不要 (旧来ブロックの解禁は構造的に不可能).
    rule_applied = rule
    raw_target_price = target_price
    target_price, _was_clamped = _apply_max_drop_clamp(float(our_price), target_price)
    if _was_clamped:
        logger.info(
            f"W183 5%clamp: {ebay_item_id} raw_target=${raw_target_price:.2f} "
            f"→ clamped=${target_price:.2f} (current=${float(our_price):.2f}, "
            f"上限 {MAX_SINGLE_DROP_PCT * 100:.0f}%/回)"
        )
        rule_applied = f"{rule} [clamp{int(MAX_SINGLE_DROP_PCT * 100)}%]"

    # 認証情報チェック (恒久設定不備で予約行を作らないよう claim より前).
    from monitor.credentials import get_ebay_credentials, ebay_credentials_ok
    creds = get_ebay_credentials(config)
    if not ebay_credentials_ok(creds):
        msg = 'eBay 認証情報未設定'
        _log_price_change(
            ebay_item_id, float(our_price), target_price,
            competitor['competitor_item_id'], competitor['total_usd'],
            rule_applied, triggered_by, success=False, error_message=msg,
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
        rule_applied, triggered_by,
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
        # 2026-07-02 (user 指示・第 3 安全弁): 値下げは既に確定済み (成功) なので、
        # ここで例外が出ても値下げ結果自体は覆さない (Q0: 偽装失敗にしない)。
        # 例外は握り潰さず必ず warning で痕跡を残す。
        try:
            _streak = _check_consecutive_reduction_streak(ebay_item_id)
            if _streak is not None:
                _send_discord_spiral_alert(config, ebay_item_id, _streak)
        except Exception as e:
            logger.warning(
                f"W183 spiral streak check/alert 失敗 ({ebay_item_id}): {e}"
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
# W245: Discord 通知 (値下げ結果 + 失敗 alert)
# ────────────────────────────────────────

def _resolve_pricing_webhook(config: Dict) -> str:
    """ライバル専用 webhook 優先, 未設定なら既定へ fallback (W153 と同方針).

    inject_webhook_into_config が entrypoint (daily_scheduler load_config) で
    DISCORD_RIVAL_WEBHOOK_URL / DISCORD_WEBHOOK_URL を config['discord'] に注入済。
    """
    disc = config.get('discord') or {}
    return (disc.get('rival_webhook_url') or disc.get('webhook_url') or "").strip()


def _get_listing_title(ebay_item_id: str) -> str:
    """通知の商品呼称 (SKU 禁止 / title で呼ぶ規約)。不在時は item_id を返す."""
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()
    return (row[0] if row and row[0] else str(ebay_item_id))


def _send_discord_reduced(config: Dict, reduced_items: list) -> None:
    """値下げ実行があった run の結果通知 (1 run 1 message)。

    W245 (2026-06-10): money-direct な自動値下げが「いつ・何を・いくらに」
    変えたかを user が Discord で受動的に知れるようにする (従来は
    price_change_log を能動的に見ない限り不可視だった)。
    """
    webhook = _resolve_pricing_webhook(config)
    if not webhook:
        logger.warning("W245: Discord webhook 未設定 — 値下げ結果通知をスキップ (痕跡)")
        return
    from notifiers.discord_notifier import DiscordNotifier
    lines = []
    for it in reduced_items[:15]:
        eid = str(it['ebay_item_id'])
        title = _get_listing_title(eid)
        comp = it.get('competitor_total')
        comp_s = f" (ライバル ${comp:.2f})" if comp is not None else ""
        lines.append(
            f"- **{title}** ({eid[-4:]}): "
            f"${it['old_price']:.2f} → ${it['new_price']:.2f}{comp_s}"
        )
    content = (
        f"💲 **W183 自動値下げ実行** ({len(reduced_items)} 件)\n" + "\n".join(lines)
    )
    if len(reduced_items) > 15:
        content += f"\n... 他 {len(reduced_items) - 15} 件"
    try:
        # 依頼ボード#39 S2 follow-up (2026-07-03): 値下げ実行結果サマリは severity='warning'
        # (money-direct だが blocking ではない、DASHBOARD/S4 で拾える通知過多防止優先)。
        DiscordNotifier(webhook, bypass_env=True).send_message(content, severity="warning")
    except Exception as e:
        logger.warning(f"W245: discord reduced notify failed: {e}")


def _send_discord_spiral_alert(config: Dict, ebay_item_id: str, streak: dict) -> None:
    """値下げ合戦スパイラル疑い alert (2026-07-02 user 指示・第 3 安全弁)。

    通知のみ、値下げ自体は止めない (停止判断は user)。同一商品への重複通知は
    1 日 1 回に dedupe (既存 claim_alert_dedupe パターンを踏襲、
    task_scheduler_health_check.py の url_divergence 通知と同方式)。
    """
    try:
        from monitor.task_execution_log import claim_alert_dedupe
        fresh = claim_alert_dedupe(
            task_key=f"__w183_spiral_{ebay_item_id}__", expected_hour=0
        )
    except Exception as e:  # noqa: BLE001 — dedupe DB 失敗で alert 自体を止めない
        logger.warning(f"W183 spiral alert dedupe DB error ({ebay_item_id}): {e}")
        fresh = True  # フェールセーフで通知側に倒す
    if not fresh:
        logger.info(
            f"W183 spiral alert: 本日通知済みのため dedupe suppress ({ebay_item_id})"
        )
        return
    webhook = _resolve_pricing_webhook(config)
    if not webhook:
        logger.warning(
            f"W245: Discord webhook 未設定 — spiral alert をスキップ ({ebay_item_id})"
        )
        return
    from notifiers.discord_notifier import DiscordNotifier
    title = _get_listing_title(ebay_item_id)
    prices_s = " → ".join(f"${p:.2f}" for p in reversed(streak['prices']))
    content = (
        f"⚠️ **W183 値下げ合戦アラート**: {title} ({ebay_item_id[-4:]})\n"
        f"直近 {streak['count']} 回連続値下げ (間に値上げなし、"
        f"{CONSECUTIVE_REDUCTION_WINDOW_DAYS} 日以内): {prices_s}\n"
        f"値下げは継続中 (床 lp_min_price / 1 回 {int(MAX_SINGLE_DROP_PCT * 100)}% 上限は既存通り適用)。"
        f"ライバルとの値下げ合戦の疑いあり、必要なら手動確認をお願いします。"
    )
    try:
        # 依頼ボード#39 S2 follow-up (2026-07-03): 値下げ合戦スパイラルアラート =
        # 2026-07-02 user 制定の第 3 安全弁。severity='critical' で
        # _ALWAYS_SEND_SEVERITIES bypass を効かせ、万一 pricing gate=OFF でも黙殺
        # されない (user 手動確認促し、money-direct)。
        DiscordNotifier(webhook, bypass_env=True).send_message(content, severity="critical")
        logger.info(f"W183 spiral alert 送信: {ebay_item_id}")
    except Exception as e:
        logger.warning(f"W183 spiral alert send failed ({ebay_item_id}): {e}")


def _send_discord_failure_alert(
    config: Dict, reason: str, summary: str, api_failures: list,
) -> None:
    """run 失敗 (failed_api / fetch 全滅 / eval 例外) の専用 alert."""
    webhook = _resolve_pricing_webhook(config)
    if not webhook:
        logger.warning("W245: Discord webhook 未設定 — 失敗 alert をスキップ (痕跡)")
        return
    from notifiers.discord_notifier import DiscordNotifier
    excerpt = []
    for f in api_failures[:5]:
        eid = str(f['ebay_item_id'])
        excerpt.append(f"- {_get_listing_title(eid)} ({eid[-4:]}): {f['message'][:100]}")
    content = (
        f"⚠️ **W183 ライバル価格 refresh 失敗** — {reason}\n"
        f"{summary}"
        + ("\n" + "\n".join(excerpt) if excerpt else "")
        + (f"\n... 他 {len(api_failures) - 5} 件" if len(api_failures) > 5 else "")
    )
    try:
        # 依頼ボード#39 S2 follow-up (2026-07-03): refresh 失敗 alert は severity='error'
        # で _ALWAYS_SEND_SEVERITIES bypass を効かせ、pricing gate=OFF でも
        # 障害検知が黙殺されない (silent skip 防止、Q0)。
        DiscordNotifier(webhook, bypass_env=True).send_message(content, severity="error")
    except Exception as e:
        logger.warning(f"W245: discord failure alert failed: {e}")


# ────────────────────────────────────────
# entry point
# ────────────────────────────────────────

def run_rival_pricing_refresh(config: Dict) -> Dict:
    """
    全 listing で active ライバル価格を refresh し、必要に応じて値下げを実行.

    Returns:
        {
            'success': bool,   # W245: failed_api>0 / fetch 全滅 / eval 例外 で False
            'listings_processed': int,
            'fetched_total': int,
            'failed_total': int,
            'reduced': int,
            'skipped_already_cheapest': int,
            'skipped_below_floor': int,
            'skipped_daily_cap': int,
            'skipped_other': int,
            'failed_api': int,
            'gated_out_ineligible': int,  # W301 S3: pricing_eligible ゲートで
                                           # 除外された active 競合数 (Q0 可視化)
            'message': str,
        }
    """
    logger.info("【開始】W183 ライバル価格 refresh + 値下げ判定")

    # W301 S3 (2026-07-02): pricing_eligible ゲートで対象外になった競合件数を
    # run 開始時に必ず計上する (Q0: 対象が黙って減ったように見えない)。
    gated_out = _count_gated_out_competitors()
    if gated_out:
        logger.info(
            f"W183 pricing_eligible gate: active だが値下げ対象外の競合 "
            f"{gated_out} 件 (Shadow/未分類、W183 対象から除外)"
        )

    listing_ids = _get_listings_with_active_competitors()
    if not listing_ids:
        logger.info("値下げ適格な active ライバルを持つ listing なし — skip")
        msg = 'no listings with pricing-eligible active competitors'
        if gated_out:
            msg += f' (gated_out_ineligible={gated_out})'
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
            'gated_out_ineligible': gated_out,
            'message': msg,
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
    # W245: 通知 / 失敗判定用の詳細収集
    reduced_items: list[dict] = []
    api_failures: list[dict] = []
    eval_errors = 0

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
            eval_errors += 1
            continue

        action = decision.get('action', 'skipped_other')
        if action == 'reduced':
            counts['reduced'] += 1
            reduced_items.append({
                'ebay_item_id': our_item_id,
                'old_price': decision.get('old_price'),
                'new_price': decision.get('new_price'),
                'competitor_total': decision.get('competitor_total'),
            })
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
            api_failures.append({
                'ebay_item_id': our_item_id,
                'message': decision.get('message', ''),
            })
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
    if gated_out:
        # W301 S3: pricing_eligible ゲートで対象外の競合件数を summary にも残す
        # (Q0: DB を能動的に見ない限り不可視にしない)。
        summary += f" | gated_out_ineligible={gated_out}"

    # W245 (2026-06-10): 偽装成功の根絶。従来は全 API 失敗 / Browse fetch 全滅でも
    # 無条件 success: True を返し、6/4-6/8 の OAuth トークン破損時に money-direct
    # タスクが 5 日間 silent に死んでいた (F12)。失敗条件:
    #   - failed_api > 0          : ReviseFixedPriceItem 失敗 (eBay 書込失敗)
    #   - fetch 全滅              : fetched=0 かつ failed>0 (= Browse API 系の systemic 障害)
    #   - eval_errors > 0         : 値下げ判定で例外 (コードバグ / DB 異常)
    failure_reasons = []
    if counts['failed_api'] > 0:
        failure_reasons.append(f"eBay 値下げ API 失敗 {counts['failed_api']} 件")
    if fetched_total == 0 and failed_total > 0:
        failure_reasons.append(f"ライバル価格取得 全滅 (failed={failed_total})")
    if eval_errors > 0:
        failure_reasons.append(f"値下げ判定 例外 {eval_errors} 件")
    run_success = not failure_reasons

    if not run_success:
        summary += f" | FAILED: {'; '.join(failure_reasons)}"
        _send_discord_failure_alert(
            config, '; '.join(failure_reasons), summary, api_failures,
        )
    if reduced_items:
        _send_discord_reduced(config, reduced_items)

    logger.info(f"W183 完了: {summary}")
    return {
        'success': run_success,
        'listings_processed': len(listing_ids),
        'fetched_total': fetched_total,
        'failed_total': failed_total,
        **counts,
        'gated_out_ineligible': gated_out,
        'message': summary,
    }
