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
import time
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
    actual_duty_rate: Optional[float] = None,
    is_ddu: bool = False,
) -> Optional[float]:
    """
    profit >= 0 になる最低 USD 価格を binary search で求める.
    必須入力 (purchase_yen / weight_g) が欠けていれば None を返す.

    W212 (2026-06-02): actual_duty_rate (商品ごとの実関税率、小数 0.30/0.55 等) を
    渡すと washing 撤廃 (Section 232 該当品の実関税を seller 実費計上) した breakeven を
    返す。None = 従来 (global duty_rate 20% washed)。callers が listing の
    duty_rate_pct/100 を渡すことで per-listing 化。

    is_ddu (2026-06-03): True で DDU (US以外) 基準の breakeven。global_only listing
    (非US客中心) は US 関税 (Section 232 含む) を載せず floor を出す業務方針
    (reference_shipping_tariff_logic §4.3「US客自腹リスク許容・global SEO重視」)。
    is_ddu=True 時 calculator は pattern="ddu" で関税ゼロ = actual_duty_rate は無視される。

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
                is_ddu=is_ddu,
                country_code=country_code,
                actual_duty_rate=actual_duty_rate,
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
            "SELECT purchase_yen, weight_g, length_cm, width_cm, height_cm, "
            "duty_rate_pct, primary_market, category_id "
            "FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()
    if not row:
        return None
    # W215 (2026-06-03): Section 232 該当品の per-listing 実関税率 (duty_rate_pct
    # 25-55%) は **適用しない** (= global duty_rate 11% で washed)。CPaSS 実請求全数
    # 調査 (US配送208件) で OC SpeedPAK DDP は原産国別 flat 率 (日本発10%) を課金し、
    # Section 232 該当品 (炊飯器8516.60等) も例外なく flat 10% だったため、25-55% を
    # 載せると当該品の floor が過大 = 機会損失になる (Codex/user 承認 2026-06-03)。
    # duty_rate_pct / section232_class は DB に保持 (法定分類の記録 + 警告バッジ用) し、
    # true-up リスクは UI 警告で別管理する (率には畳み込まない)。詳細:
    # .company/engineering/docs/2026-06-03-cpass-us-duty-actuals.md / settings.us_duty。
    # row[5] (duty_rate_pct) は SELECT のみ残置 (将来の警告ロジック用)、breakeven 率には不使用。
    actual_duty_rate = None
    # W212 (2026-06-03): global_only (非US客中心) は DDU 基準で floor を出す = US 関税
    # (Section 232 含む) を載せない (reference_shipping_tariff_logic §4.3、user 承認 2026-06-03)。
    # US_only/mixed_global/unknown/NULL は従来どおり US DDP (保守、関税込)。
    is_ddu = (row[6] == "global_only")
    # W222 (2026-06-05): per-listing 実カテゴリで FVF を計算 (従来は固定 58248=12.7%)。
    # ⚠️ money-direct (floor=自動値下げ下限)。settings.use_category_fvf_floor=True で
    # 初めて実カテゴリを使う (default False = 従来 58248 固定で floor 不変)。
    # 「category_id の保存 (列/backfill/同期)」と「floor 計算での利用」を分離し、
    # DRY-RUN→user 承認後に flag を ON にして全件再計算する安全ロールアウト
    # (use_batch_api / use_candidate_ranker と同じ pattern)。flag OFF の間は
    # backfill 済み category_id があっても floor は従来値のまま = 共同検証ゲートを守る。
    # 防御 (2026-06-06 W226 全件test で発覚): settings が dict 以外 (float 誤渡し等)
    # でも try 前で AttributeError を出さず、{} に畳んで try 内の compute で TypeError
    # → graceful None + warning log に倒す (C-1 regression guard 契約の復旧)。
    _use_cat = bool((settings if isinstance(settings, dict) else {}).get(
        "use_category_fvf_floor", False))
    _cat_id = (int(row[7]) if row[7] else 58248) if _use_cat else 58248
    try:
        breakeven = compute_breakeven_price_usd(
            purchase_yen=row[0] or 0,
            weight_g=row[1] or 0,
            length_cm=row[2] or 0,
            width_cm=row[3] or 0,
            height_cm=row[4] or 0,
            settings=settings,
            category_id=_cat_id,
            actual_duty_rate=actual_duty_rate,
            is_ddu=is_ddu,
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
        # W301 HIGH-1 統一方針 (2026-07-02): 停止 (is_active 1→0) 時は
        # pricing_eligible も必ず 0 にクリア。1→0 の変化時のみ
        # pricing_eligible_change_log に changed_by='deactivate_clear' で記録
        # (Q0 痕跡)。W183 ゲート表と裏をライフサイクル 1 方針で閉じる。
        for cid in existing_set - new_set:
            cp_id = existing_active[cid]
            prev_row = conn.execute(
                "SELECT our_item_id, COALESCE(pricing_eligible, 0) AS pricing_eligible "
                "FROM competitor_products WHERE id=?",
                (cp_id,),
            ).fetchone()
            conn.execute(
                "UPDATE competitor_products SET is_active=0, pricing_eligible=0 "
                "WHERE id=?",
                (cp_id,)
            )
            if prev_row is not None and prev_row[1] == 1:
                try:
                    conn.execute(
                        """INSERT INTO pricing_eligible_change_log
                           (competitor_product_id, our_item_id, competitor_item_id,
                            old_value, new_value, changed_by)
                           VALUES (?,?,?,?,?,?)""",
                        (cp_id, prev_row[0], cid, 1, 0, 'deactivate_clear'),
                    )
                except sqlite3.OperationalError as e:
                    logger.warning(
                        f"[W301 HIGH-1] pricing_eligible_change_log INSERT "
                        f"skipped (v87 未適用?): {e}"
                    )

        # 追加対象
        for cid in cleaned:
            if cid in existing_active:
                continue
            # competitor_item_id UNIQUE 制約のため、過去 inactive 行があれば再 active 化.
            # 旧 our_item_id 用の price_rule / min_price / max_discount は **デフォルト値で
            # 上書き** する (review H2). 旧 owner のカスタム設定が黙って引き継がれる事故を防ぐ.
            # W301 HIGH-1 統一方針 (2026-07-02): この再活性化経路 (第 2 復活経路) にも
            # pricing_eligible=0 を明示追加。past 行が別 our_item_id 経由で以前
            # eligible=1 だったとしても、UI 経由の再登録は「新規再採用 = Shadow 起点」
            # として扱う (add_or_reactivate_competitor と統一の 1 方針)。
            past = conn.execute(
                "SELECT id, our_item_id, COALESCE(pricing_eligible, 0) AS pricing_eligible "
                "FROM competitor_products WHERE competitor_item_id=?",
                (cid,)
            ).fetchone()
            if past:
                past_id = past[0]
                past_prev_our_iid = past[1]
                past_prev_eligible = past[2]
                conn.execute(
                    "UPDATE competitor_products SET is_active=1, "
                    "pricing_eligible=0, our_item_id=?, "
                    "price_rule='competitor - 0.01', min_price=0.0, max_discount=10.0 "
                    "WHERE id=?",
                    (our_item_id, past_id)
                )
                # W301 MEDIUM fix (2026-07-02): 監査痕跡の対称化. add_or_reactivate
                # の reactivate_reset ログと同型で、prev eligible=1 → 0 の遷移を必ず
                # 記録する (Q0 silent-skip-prevention)。旧 owner が別 our_item_id で
                # 採用中 (eligible=1) だった行を UI 経由で別 owner へ再割当てるケースは
                # 稀だが、監査ログが片側だけ空欄になる非対称性を解消する。
                if past_prev_eligible == 1:
                    try:
                        conn.execute(
                            """INSERT INTO pricing_eligible_change_log
                               (competitor_product_id, our_item_id, competitor_item_id,
                                old_value, new_value, changed_by)
                               VALUES (?,?,?,?,?,?)""",
                            (past_id, past_prev_our_iid, cid, 1, 0, 'reactivate_reset'),
                        )
                    except sqlite3.OperationalError as e:
                        logger.warning(
                            f"[W301 MEDIUM] pricing_eligible_change_log INSERT "
                            f"skipped (v87 未適用?): {e}"
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

def refresh_competitor_pricing(
    our_item_id: str, config: dict, *, rate_sleep_sec: float = 0.0
) -> dict:
    """
    指定 our_item_id の active ライバル全件を Browse API で価格・送料取得 → DB 保存.
    Returns: {'fetched': N件取得成功, 'failed': N件取得失敗}

    H3 fix: connection 1 つで全 UPDATE (ループ毎開閉を回避).
    rate_sleep_sec: W119②一括登録 auto-fetch の様に大量 listing を連続 fetch
      する呼出元が Browse API quota (日次) / W183 cron との競合を緩和するため
      の自主 rate 制限 (call 間 sleep 秒). default 0.0 = 現挙動不変
      (W183 scheduler / 手動再取得は従来どおり sleep なし).
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
        for _idx, (cp_id, cid) in enumerate(rows):
            # 2 件目以降は呼出元指定の rate sleep (default 0 = 現挙動不変).
            # 先頭で sleep しない / validation skip も含め一律 = 上限 rate を
            # 超えない conservative 実装 (skip は稀で過剰待ちは無害).
            if rate_sleep_sec > 0 and _idx > 0:
                time.sleep(rate_sleep_sec)
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
    指定 our_item_id の active ライバル詳細 (id + 価格 + 送料 + 合計 + 配送日 +
    値下げ適格 + AI 判定). UI 表示用.

    W301 AI 店長 Phase1 S6 (2026-07-02): pricing_eligible (competitor_products) と
    rival_classifications の競合単位最新判定 (LEFT JOIN、MAX(id) GROUP BY
    competitor_item_id) を追加。listing 識別は competitor_products.id / SKU 不使用
    (sku-rules.md 準拠)。既存呼出元 (tab_product_management.py) は追加キーを
    参照しないため後方互換 (K2 surgical、既存挙動不変)。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cp.id, cp.competitor_item_id, cp.competitor_price_usd,
                   cp.competitor_shipping_usd, cp.last_priced_at,
                   cp.min_delivery_date, cp.max_delivery_date,
                   COALESCE(cp.pricing_eligible, 0) AS pricing_eligible,
                   rc.classification AS ai_classification,
                   rc.confidence AS ai_confidence,
                   rc.reason AS ai_reason,
                   rc.would_be_eligible AS ai_would_be_eligible,
                   rc.created_at AS ai_classified_at
            FROM competitor_products cp
            LEFT JOIN (
                SELECT * FROM rival_classifications
                WHERE id IN (
                    SELECT MAX(id) FROM rival_classifications GROUP BY competitor_item_id
                )
            ) rc ON rc.competitor_item_id = cp.competitor_item_id
            WHERE cp.our_item_id=? AND cp.is_active=1
            ORDER BY cp.id
            """,
            (our_item_id,)
        ).fetchall()
    result = []
    for r in rows:
        price = r["competitor_price_usd"]
        ship = r["competitor_shipping_usd"]
        total = (price or 0) + (ship or 0) if (price is not None) else None
        ai_would_be_eligible = r["ai_would_be_eligible"]
        result.append({
            'id': r["id"],
            'competitor_item_id': r["competitor_item_id"],
            'price_usd': price,
            'shipping_usd': ship,
            'total_usd': total,
            'last_priced_at': r["last_priced_at"],
            'min_delivery_date': r["min_delivery_date"],
            'max_delivery_date': r["max_delivery_date"],
            'pricing_eligible': bool(r["pricing_eligible"]),
            'ai_classification': r["ai_classification"],
            'ai_confidence': r["ai_confidence"],
            'ai_reason': r["ai_reason"],
            'ai_would_be_eligible': (
                bool(ai_would_be_eligible) if ai_would_be_eligible is not None else None
            ),
            'ai_classified_at': r["ai_classified_at"],
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
