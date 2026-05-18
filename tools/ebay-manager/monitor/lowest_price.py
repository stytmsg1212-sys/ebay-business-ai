"""
W98 最安値チェック helpers
- 商品ごとの最低利益価格 (binary search で逆算)
- ライバル一覧の upsert (UI からの編集を DB に反映)
- 商品ごとの設定保存 (purchase_yen / lp_min_price)
- 新規発見ライバルの送料取得 (Browse API)
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from .database import get_conn

logger = logging.getLogger(__name__)


# ────────────────────────────────────────
# 最低利益価格 (損益分岐) 計算
# ────────────────────────────────────────

def compute_breakeven_price_usd(
    purchase_yen: float,
    weight_g: float,
    length_cm: float,
    width_cm: float,
    height_cm: float,
    settings: dict,
    category_id: int = 58248,
    country_code: str = "US",
) -> Optional[float]:
    """
    profit >= 0 になる最低 USD 価格を binary search で求める.
    必須入力 (purchase_yen / weight_g) が欠けていれば None を返す.

    Returns:
        float (USD, 小数 2 桁丸め) or None
    """
    if not purchase_yen or purchase_yen <= 0:
        return None
    if not weight_g or weight_g <= 0:
        return None

    from calculator import CalcInput, calculate

    def _max_profit_at(price_usd: float) -> Optional[float]:
        """price_usd で計算し、最良サービスの profit を返す. 設定 / 入力起因の例外のみ吸収."""
        try:
            inp = CalcInput(
                purchase_yen=int(purchase_yen),
                item_price_usd=float(price_usd),
                weight_g=int(weight_g),
                length_cm=float(length_cm or 0),
                width_cm=float(width_cm or 0),
                height_cm=float(height_cm or 0),
                category_id=int(category_id),
                is_ddu=False,
                country_code=country_code,
            )
            res = calculate(inp, settings)
            if not res.service_results:
                return None
            return max(s.profit for s in res.service_results)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            # 設定 dict 欠落 / 型不正 / 為替 0 等. 上位 update_listing_breakeven が
            # 1 listing につき 1 回 warning ログするため、ここでは debug のみ.
            logger.debug(f"breakeven calc error at ${price_usd}: {e}")
            return None

    # binary search: 1.0 USD ~ 10000.0 USD
    lo, hi = 1.0, 10000.0
    p_hi = _max_profit_at(hi)
    if p_hi is None:
        # 上限値での計算自体が失敗 = settings or 入力起因 (broken config 等).
        # silent fail 防止のため例外を raise → 上位 update_listing_breakeven が warning ログ.
        raise RuntimeError(
            "breakeven calc setup error: calculator returned None at upper bound "
            "(settings dict 不正 or 為替/重量欠落の可能性)"
        )
    if p_hi < 0:
        # 上限でも赤字 = 仕入が極端に高い (実用上は赤字案件) → 計算不能
        return None
    p_lo = _max_profit_at(lo)
    if p_lo is None:
        raise RuntimeError(
            "breakeven calc setup error: calculator returned None at lower bound"
        )
    if p_lo >= 0:
        # $1.0 で既に黒字 = fee 構造の異常か、仕入価格が異常に低い
        logger.warning(
            f"breakeven returned $1.0 (sanity check): purchase_yen={purchase_yen}, "
            f"weight_g={weight_g}. fee 構造を確認してください."
        )
        return round(lo, 2)

    for _ in range(40):
        mid = (lo + hi) / 2
        p = _max_profit_at(mid)
        if p is None:
            return None
        if p < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.01:
            break
    return round(hi, 2)


def update_listing_breakeven(ebay_item_id: str, settings: dict) -> Optional[float]:
    """
    ebay_listings から必要データを SELECT して breakeven 計算 → lp_breakeven_usd に保存.
    Returns 計算結果 (USD) or None.
    例外は ここで warning log (silent fail 防止) → 戻り値 None で上流 UI が「-」表示.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT purchase_yen, weight_g, length_cm, width_cm, height_cm "
            "FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()
    if not row:
        return None
    try:
        breakeven = compute_breakeven_price_usd(
            purchase_yen=row[0] or 0,
            weight_g=row[1] or 0,
            length_cm=row[2] or 0,
            width_cm=row[3] or 0,
            height_cm=row[4] or 0,
            settings=settings,
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as e:
        # H7 fix: 例外型を絞る. 想定外の Exception はあえて表面化させる.
        # RuntimeError は compute 内で意図的 raise される setup error.
        logger.warning(
            f"breakeven calc failed for listing {ebay_item_id}: {e}"
        )
        breakeven = None
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET lp_breakeven_usd=? WHERE ebay_item_id=?",
            (breakeven, ebay_item_id)
        )
    return breakeven


# ────────────────────────────────────────
# 商品ごとの設定保存
# ────────────────────────────────────────

def set_listing_lowest_price_fields(
    ebay_item_id: str,
    purchase_yen: Optional[float],
    lp_min_price: Optional[float],
):
    """編集 UI からの保存. purchase_yen / lp_min_price を ebay_listings に書く.
    両カラムを同時 UPDATE するので、片方だけ更新したい場合は本関数を使わず
    `update_listing_purchase_yen` 等の単独 helper を使うこと.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET purchase_yen=?, lp_min_price=? "
            "WHERE ebay_item_id=?",
            (purchase_yen, lp_min_price, ebay_item_id)
        )


def update_listing_purchase_yen(ebay_item_id: str, purchase_yen: float) -> None:
    """purchase_yen 単独 UPDATE (lp_min_price は触らない).

    W119 wizard Step 2 で supplier から fetch した値を補完する用途.
    set_listing_lowest_price_fields は両カラム同時 UPDATE のため lp_min_price=None で
    呼ぶと user 設定値を破壊するので、本 helper で単独更新する.
    (2026-05-10 W119 Round 2 review C-3 fix)
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET purchase_yen=? WHERE ebay_item_id=?",
            (purchase_yen, ebay_item_id)
        )


# ────────────────────────────────────────
# ライバル一覧の upsert
# ────────────────────────────────────────

def get_competitors_grouped(our_item_ids: list[str]) -> dict[str, list[str]]:
    """
    指定 our_item_id 群について、ライバルの competitor_item_id リストを our_item_id 別に返す.
    順序は登録 id 昇順.
    """
    if not our_item_ids:
        return {}
    placeholder = ",".join("?" * len(our_item_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT our_item_id, competitor_item_id FROM competitor_products "
            f"WHERE our_item_id IN ({placeholder}) AND is_active=1 "
            f"ORDER BY our_item_id, id",
            our_item_ids
        ).fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r[0], []).append(r[1])
    return result


def upsert_listing_competitors(our_item_id: str, competitor_item_ids: list[str]):
    """
    指定 our_item_id について、ライバル list を完全に置換する.
    - 空文字 / 重複は除去
    - 既存 active 行のうち new list に無いものは is_active=0
    - new list にあって既存に無いものは INSERT
    - 既存にあるものはそのまま (price_rule / min_price は維持)
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for c in competitor_item_ids:
        if not c:
            continue
        s = c.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)

    with get_conn() as conn:
        existing_rows = conn.execute(
            "SELECT competitor_item_id, id FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1",
            (our_item_id,)
        ).fetchall()
        existing_active: dict[str, int] = {r[0]: r[1] for r in existing_rows}

        new_set = set(cleaned)
        existing_set = set(existing_active.keys())

        # 削除対象 (active → inactive)
        for cid in existing_set - new_set:
            conn.execute(
                "UPDATE competitor_products SET is_active=0 WHERE id=?",
                (existing_active[cid],)
            )

        # 追加対象
        for cid in cleaned:
            if cid in existing_active:
                continue
            # competitor_item_id UNIQUE 制約のため、過去 inactive 行があれば再 active 化.
            # 旧 our_item_id 用の price_rule / min_price / max_discount は **デフォルト値で
            # 上書き** する (review H2). 旧 owner のカスタム設定が黙って引き継がれる事故を防ぐ.
            past = conn.execute(
                "SELECT id FROM competitor_products WHERE competitor_item_id=?",
                (cid,)
            ).fetchone()
            if past:
                conn.execute(
                    "UPDATE competitor_products SET is_active=1, our_item_id=?, "
                    "price_rule='competitor - 0.01', min_price=0.0, max_discount=10.0 "
                    "WHERE id=?",
                    (our_item_id, past[0])
                )
            else:
                conn.execute(
                    "INSERT INTO competitor_products "
                    "(our_item_id, competitor_item_id, price_rule, min_price, "
                    " max_discount, is_active) "
                    "VALUES (?, ?, 'competitor - 0.01', 0.0, 10.0, 1)",
                    (our_item_id, cid)
                )


# ────────────────────────────────────────
# 新規発見ライバル: 送料取得
# ────────────────────────────────────────

def _get_browse_client(config: dict):
    """Browse API client を初期化. credentials 不在時は None."""
    try:
        from monitor.credentials import get_ebay_credentials
        creds = get_ebay_credentials(config)
        app_id = creds.get('app_id', '')
        cert_id = creds.get('cert_id', '')
        if not app_id or not cert_id:
            logger.warning("Browse API credentials 未設定")
            return None
        from tasks.ebay_browse_api import BrowseAPIClient
        return BrowseAPIClient(app_id, cert_id)
    except Exception as e:
        logger.warning(f"Browse API client init error: {e}")
        return None


def fetch_alert_shipping_usd(alert_id: int, config: dict) -> Optional[float]:
    """
    new_competitor_alerts.found_shipping を Browse API で埋める.
    Returns 取得した送料 (USD) or None.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT found_item_id FROM new_competitor_alerts WHERE id=?",
            (alert_id,)
        ).fetchone()
    if not row:
        return None
    item_id = row[0]
    if not item_id:
        return None

    client = _get_browse_client(config)
    if client is None:
        return None
    try:
        result = client.get_item_pricing(item_id)
    except Exception as e:
        logger.warning(f"Browse API pricing fetch error (item={item_id}): {e}")
        return None
    if result is None:
        return None

    shipping_usd = result["shipping_usd"]
    with get_conn() as conn:
        conn.execute(
            "UPDATE new_competitor_alerts SET found_shipping=? WHERE id=?",
            (float(shipping_usd), alert_id)
        )
    return float(shipping_usd)


# ────────────────────────────────────────
# ライバル価格・送料の自動取得 (商品ごと)
# ────────────────────────────────────────

def refresh_competitor_pricing(our_item_id: str, config: dict) -> dict:
    """
    指定 our_item_id の active ライバル全件を Browse API で価格・送料取得 → DB 保存.
    Returns: {'fetched': N件取得成功, 'failed': N件取得失敗}

    H3 fix: connection 1 つで全 UPDATE (ループ毎開閉を回避).
    """
    from datetime import datetime
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, competitor_item_id FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1",
            (our_item_id,)
        ).fetchall()

    if not rows:
        return {'fetched': 0, 'failed': 0}

    client = _get_browse_client(config)
    if client is None:
        return {'fetched': 0, 'failed': len(rows)}

    fetched = 0
    failed = 0
    now = datetime.now().isoformat()
    with get_conn() as conn:
        for cp_id, cid in rows:
            if not cid or not cid.isdigit() or len(cid) < 11 or len(cid) > 14:
                failed += 1
                continue
            try:
                result = client.get_item_pricing(cid)
            except Exception as e:
                logger.warning(f"competitor pricing fetch error ({cid}): {e}")
                failed += 1
                continue
            if result is None:
                failed += 1
                continue
            conn.execute(
                "UPDATE competitor_products "
                "SET competitor_price_usd=?, competitor_shipping_usd=?, "
                "    min_delivery_date=?, max_delivery_date=?, "
                "    last_priced_at=? "
                "WHERE id=?",
                (
                    float(result["price_usd"]), float(result["shipping_usd"]),
                    result.get("min_delivery_date"), result.get("max_delivery_date"),
                    now, cp_id,
                )
            )
            fetched += 1
    return {'fetched': fetched, 'failed': failed}


def get_competitors_with_pricing(our_item_id: str) -> list[dict]:
    """
    指定 our_item_id の active ライバル詳細 (id + 価格 + 送料 + 合計 + 配送日).
    UI 表示用.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, competitor_item_id, competitor_price_usd, "
            "       competitor_shipping_usd, last_priced_at, "
            "       min_delivery_date, max_delivery_date "
            "FROM competitor_products "
            "WHERE our_item_id=? AND is_active=1 ORDER BY id",
            (our_item_id,)
        ).fetchall()
    result = []
    for r in rows:
        price = r[2]
        ship = r[3]
        total = (price or 0) + (ship or 0) if (price is not None) else None
        result.append({
            'id': r[0],
            'competitor_item_id': r[1],
            'price_usd': price,
            'shipping_usd': ship,
            'total_usd': total,
            'last_priced_at': r[4],
            'min_delivery_date': r[5],
            'max_delivery_date': r[6],
        })
    return result


# ────────────────────────────────────────
# 無在庫商品の仕入価格自動取得
# ────────────────────────────────────────

def fetch_supplier_purchase_yen(ebay_item_id: str) -> Optional[int]:
    """
    SKU が ebay** (無在庫) の商品について、source_url から price_jpy を scrape して
    ebay_listings.purchase_yen に保存.

    H-A2 fix: 既存 purchase_yen がある場合の上書きを logger.warning で必ず記録
    (silent overwrite 防止). 30% 以上の変動はさらに ERROR レベルで surface.

    Returns: 取得した JPY 金額 / None (失敗時)
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sku, source_url, purchase_yen FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()
    if not row:
        return None
    sku, source_url, old_pyen = row[0], row[1], row[2]
    if not sku or not sku.startswith('ebay'):
        # 在庫品 (stock**) は source_url scrape 対象外
        return None
    if not source_url:
        return None

    try:
        from monitor.supplier_scraper import scrape_supplier_url
        scraped = scrape_supplier_url(source_url, timeout_sec=20)
    except Exception as e:
        logger.warning(f"supplier scrape error ({source_url}): {e}")
        return None

    if not scraped or scraped.price_jpy is None:
        return None
    purchase = int(scraped.price_jpy)

    # 既存値の上書き検知 → 必ず log (silent overwrite 防止)
    if old_pyen is not None and int(old_pyen) != purchase:
        diff_pct = abs(purchase - int(old_pyen)) / max(int(old_pyen), 1) * 100
        if diff_pct >= 30:
            logger.error(
                f"purchase_yen LARGE OVERWRITE: {ebay_item_id} "
                f"¥{int(old_pyen):,} → ¥{purchase:,} ({diff_pct:.1f}%). "
                f"breakeven が大きく変動するため要確認."
            )
        else:
            logger.warning(
                f"purchase_yen overwrite: {ebay_item_id} "
                f"¥{int(old_pyen):,} → ¥{purchase:,} ({diff_pct:.1f}%)"
            )

    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET purchase_yen=? WHERE ebay_item_id=?",
            (purchase, ebay_item_id)
        )
    return purchase


# ────────────────────────────────────────
# 区分 (primary_market) の優先度付き取得
# ────────────────────────────────────────

def get_price_change_log(ebay_item_id: str, limit: int = 20) -> list[dict]:
    """W183 値下げ履歴を新しい順で返す.

    UI 表示用. JST 換算は呼出側で行う (DB は UTC 保存).

    claim_status='pending' (H4 予約確保済・API 実行中、または process crash
    で漏れた予約) は確定前なので監査ログから除外する.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, old_price_usd, new_price_usd, competitor_item_id, "
            "       competitor_total_usd, rule_applied, triggered_by, "
            "       success, error_message, changed_at "
            "FROM price_change_log "
            "WHERE ebay_item_id=? "
            "  AND (claim_status IS NULL OR claim_status != 'pending') "
            "ORDER BY changed_at DESC LIMIT ?",
            (ebay_item_id, limit)
        ).fetchall()
    return [
        {
            'id': r[0],
            'old_price_usd': r[1],
            'new_price_usd': r[2],
            'competitor_item_id': r[3],
            'competitor_total_usd': r[4],
            'rule_applied': r[5],
            'triggered_by': r[6],
            'success': bool(r[7]),
            'error_message': r[8],
            'changed_at': r[9],
        }
        for r in rows
    ]


def count_today_price_changes_jst(ebay_item_id: str) -> int:
    """本日 (JST) の success=1 値下げ回数 (L2 cap visualization 用)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM price_change_log "
            "WHERE ebay_item_id=? AND success=1 "
            "  AND DATE(changed_at, '+9 hours') = DATE('now', '+9 hours')",
            (ebay_item_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def get_listing_market_displays(ebay_item_ids: list[str]) -> dict[str, str]:
    """
    listing 群について、区分の表示値を返す.
    優先度: market_strategy_decisions.final_market (確定) >
            pending_market_changes.proposed_market (承認待ち) >
            market_analysis.primary_market (解析最新) >
            ebay_listings.primary_market > '-'

    H5 fix: IN (?, ?, ...) は SQLite の placeholder 上限 (CPython バンドル次第で
    999 / 32766) に依存. listing 数増加時の silent broken を防ぐため、json_each で
    1 placeholder 化する.
    """
    if not ebay_item_ids:
        return {}
    import json
    ids_json = json.dumps(ebay_item_ids)
    result: dict[str, str] = {eid: '-' for eid in ebay_item_ids}

    # 段階的に上書き。各 layer の中で「最新を保持」するため seen set で重複を弾く.
    with get_conn() as conn:
        # Layer 4 (lowest): ebay_listings.primary_market
        for r in conn.execute(
            "SELECT ebay_item_id, primary_market FROM ebay_listings "
            "WHERE ebay_item_id IN (SELECT value FROM json_each(?))",
            (ids_json,)
        ).fetchall():
            if r[1]:
                result[r[0]] = r[1]

        # Layer 3: market_analysis (latest scraped_at per ebay_item_id)
        seen3: set[str] = set()
        for r in conn.execute(
            "SELECT ebay_item_id, primary_market FROM market_analysis "
            "WHERE ebay_item_id IN (SELECT value FROM json_each(?)) "
            "  AND primary_market IS NOT NULL "
            "ORDER BY scraped_at DESC",
            (ids_json,)
        ).fetchall():
            if r[0] in seen3:
                continue
            seen3.add(r[0])
            result[r[0]] = r[1]

        # Layer 2: pending_market_changes.proposed_market (1 row per ebay_item_id by PK)
        for r in conn.execute(
            "SELECT ebay_item_id, proposed_market FROM pending_market_changes "
            "WHERE ebay_item_id IN (SELECT value FROM json_each(?))",
            (ids_json,)
        ).fetchall():
            if r[1]:
                result[r[0]] = r[1]

        # Layer 1 (highest): market_strategy_decisions.final_market (latest approved)
        seen1: set[str] = set()
        for r in conn.execute(
            "SELECT ebay_item_id, final_market FROM market_strategy_decisions "
            "WHERE ebay_item_id IN (SELECT value FROM json_each(?)) "
            "  AND action='approved' "
            "ORDER BY decided_at DESC",
            (ids_json,)
        ).fetchall():
            if r[0] in seen1:
                continue
            seen1.add(r[0])
            if r[1]:
                result[r[0]] = r[1]

    return result
