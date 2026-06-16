"""W7-A 市場戦略: Terapeak Research Products スクレイパー (CDP attach 方式).

PoC (scripts/terapeak_poc_cdp.py) で実証されたロジックの本実装版.

設計方針:
  - user が事前に Chrome を --remote-debugging-port=9222 で起動 + ログイン済 + Terapeak へ navigate
  - 本モジュールは CDP に attach して 1 SKU ずつ scrape
  - 結果は market_analysis テーブルに保存
  - 失敗時は Q0 (no silent skip) に従い必ず error を返す

呼出元:
  - tasks/task_market_analysis_refresh.py (週次 cron)
  - UI から手動 trigger (MonoDeck 市場戦略タブ)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from urllib.parse import quote

if sys.platform == "win32":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None
    PWTimeout = Exception

logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://localhost:9222"

# 動画 [60JJUZaMdpo] 引用の閾値 + 4 区分化 (W7-A 候補 C / reference_shipping_tariff_logic.md v2.1).
US_ONLY_THRESHOLD = 0.70       # US 比率 >= この値 → US_only
GLOBAL_ONLY_THRESHOLD = 0.30   # US 比率 <= この値 → global_only (= 非 US >= 70%)
# v2.1 (2026-05-15): US_only 含め一律 sample >= 3 で判定可能 (user 訂正).
# 旧 v2.0 (2026-05-09): US_only のみ sample >= 5 必須としていたが、機会損失リスクのため撤回.
MIN_SAMPLE_SIZE = 3             # 全区分共通 (sample < 3 で unknown)

# Condition filter 自動解除対象. ブラウザに残った New/Used 等の chip が
# Buyer Location 集計を歪めるため、scrape 前に解除する (= All conditions 集計).
# Seller Country / Format / Price 等の chip は触らない.
CONDITION_FILTER_LABELS = frozenset({
    "New",
    "Used",
    "Pre-owned",
    "Refurbished",
    "Open box",
    "For parts or not working",
    "Certified - Refurbished",
    "Excellent - Refurbished",
    "Very Good - Refurbished",
    "Good - Refurbished",
    "New with tags",
    "New without tags",
    "New with defects",
    "Seller refurbished",
    "Manufacturer refurbished",
})


@dataclass
class MarketAnalysisResult:
    """1 SKU の市場分析結果."""
    sku: str
    keyword: str
    success: bool = False
    error: Optional[str] = None
    # Buyer Location 集計
    total_sold: Optional[int] = None
    us_count: Optional[int] = None
    non_us_count: Optional[int] = None
    us_ratio: Optional[float] = None
    countries_breakdown: list = field(default_factory=list)
    # 補助メトリクス
    avg_sold_price_usd: Optional[float] = None
    avg_shipping_usd: Optional[float] = None
    sell_through_pct: Optional[float] = None
    total_sellers: Optional[int] = None
    # 判定結果
    primary_market: Optional[str] = None     # 'US_only' / 'mixed_global' / 'global_only' / 'unknown'
    primary_market_reason: Optional[str] = None
    # メタデータ
    page_url: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())
    day_range: int = 365
    # scrape 中に解除した Condition filter (debug 用)
    cleared_condition_filters: list[str] = field(default_factory=list)
    # プラグイン (Buyer Location helper) からの独立集計 (sanity check 用)
    plugin_sanity_check: Optional[dict[str, object]] = None


def build_terapeak_url(keyword: str, day_range: int = 365) -> str:
    """Terapeak Research Products URL を組み立てる.

    PoC で確認した URL 構造:
      https://www.ebay.com/sh/research?marketplace=EBAY-US&keywords=<KW>&
        dayRange=90&tabName=SOLD&sellerCountry=SellerLocation:::JP&...
    """
    base = "https://www.ebay.com/sh/research"
    params = [
        ("marketplace", "EBAY-US"),
        ("keywords", keyword),
        ("dayRange", str(day_range)),
        ("tabName", "SOLD"),
        ("sellerCountry", "SellerLocation:::JP"),
        ("offset", "0"),
        ("limit", "50"),
        ("tz", "Asia/Tokyo"),
    ]
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"{base}?{qs}"


_MONTH_ABBR_TO_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_terapeak_date(text: str) -> Optional[datetime]:
    """"Mar 30, 2026" → datetime. locale 非依存 (datetime.strptime "%b" を避ける)."""
    m = re.match(r'(\w{3})\s+(\d{1,2}),\s+(\d{4})', text.strip())
    if not m:
        return None
    mon = _MONTH_ABBR_TO_NUM.get(m.group(1))
    if mon is None:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(2)))
    except (ValueError, KeyError):
        return None


def _detect_actual_dayrange(html: str) -> Optional[int]:
    """results-header__left 内の active 日付 range から実 dayRange を逆算.

    eBay Terapeak の SPA は UI button / menu aria-checked / OptionValueSpan の
    selected:true は state ずれで信頼できない. dropdown option も全部 DOM に
    残るため、単純な「最初の日付 range」regex は dropdown menu item を誤検出
    する. authoritative source は results-header__left 内の <span> のみ.

    Returns:
        canonical dayRange (7/30/90/180/365/730/1095) or None
    """
    # 1) 優先: results-header__left 内の active range
    scoped = re.search(
        r'class="results-header__left"[^>]*>\s*<span[^>]*>\s*'
        r'([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s*[-–]\s*'
        r'([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s*</span>',
        html, re.DOTALL,
    )
    candidate_pairs: list[tuple[str, str]] = []
    if scoped:
        candidate_pairs.append((scoped.group(1), scoped.group(2)))
    else:
        # 2) fallback: 画面全体の date range を全部列挙 (active を含めば OK 扱い)
        candidate_pairs = re.findall(
            r'([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s*[-–]\s*'
            r'([A-Za-z]{3}\s+\d{1,2},\s+\d{4})',
            html,
        )
        if not candidate_pairs:
            return None
        logger.warning(
            "_detect_actual_dayrange: results-header__left 未検出, fallback 全 range 走査"
        )

    detected_canonicals: list[int] = []
    for s1, s2 in candidate_pairs:
        d1 = _parse_terapeak_date(s1)
        d2 = _parse_terapeak_date(s2)
        if d1 is None or d2 is None:
            continue
        days = (d2 - d1).days
        for canonical in (7, 30, 90, 180, 365, 730, 1095):
            # MEDIUM-B 相互参照: ±2 tolerance は build_harvest_url の 1 日 buffer (L1542 参照)
            # を吸収するための値. buffer を 3 日以上にするとここで検出が外れ恒常 failed 化する.
            if abs(days - canonical) <= 2:
                detected_canonicals.append(canonical)
                break
        else:
            detected_canonicals.append(days)
    if not detected_canonicals:
        return None
    # scoped が見つかっていればそれが最優先 (1 件のみ)
    return detected_canonicals[0]


def _clear_condition_filters(target, sku: str) -> list[str]:
    """applied filter chip rail から Condition (New/Used/Refurbished 等) を解除.

    business reason:
      W7-A の Buyer Location 集計は「全 condition での市場全体傾向」が業務基準.
      ブラウザに残っていた Condition: New 等が active だと dropdown 集計が
      新品のみに絞られ、母数激減 (例: 31 → 6 件) → 誤判定の主因となる.
      Seller Country 等の必須 filter は保持する (aria-label がフィルタ chip 内のみ評価).

    Returns:
        解除した label のリスト (空 = 既に clean か Condition filter 無し)
    """
    cleared: list[str] = []
    # 連続 close で DOM が変わるため "1 件 close → 再列挙" を最大 5 回回す.
    # eBay UI の applied chip は通常 1-2 個 (Seller Country + 1 Condition) なので 5 で十分.
    for _ in range(5):
        pills = target.locator("div.filter-pill")
        try:
            n = pills.count()
        except (PWTimeout, AttributeError) as e:
            logger.warning(f"{sku}: filter pill count 失敗 ({e})")
            break
        target_idx = -1
        target_label = ""
        for i in range(n):
            try:
                label = (
                    pills.nth(i)
                    .locator(".filter-pill__main")
                    .first.get_attribute("aria-label")
                ) or ""
            except (PWTimeout, AttributeError) as e:
                logger.debug(f"{sku}: pill {i} aria-label 取得失敗 ({e})")
                continue
            if label in CONDITION_FILTER_LABELS:
                target_idx = i
                target_label = label
                break
        if target_idx == -1:
            break  # Condition filter 無し or 全て解除済
        try:
            close_btn = pills.nth(target_idx).locator(".filter-pill__close").first
            close_btn.click(timeout=5000)
            cleared.append(target_label)
            logger.info(f"{sku}: Condition filter '{target_label}' を解除")
            target.wait_for_timeout(1000)  # render + network fetch 待ち
        except (PWTimeout, AttributeError) as e:
            logger.warning(f"{sku}: filter '{target_label}' close 失敗 ({e})")
            break
    if cleared:
        try:
            target.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
    return cleared


def _judge_primary_market(us_count: int, non_us_count: int) -> tuple[str, str]:
    """4 区分判定 (W7-A: US_only / mixed_global / global_only / unknown).

    詳細仕様: reference_shipping_tariff_logic.md v2.1 § 4.

    v2.1 (2026-05-15): US_only 含め一律 sample >= 3 で判定可能.
      - sample < 3: unknown (統計不能).
      - sample >= 3: 全判定可能 (US_only / mixed_global / global_only).

    Returns:
        (primary_market, reason)
    """
    total = us_count + non_us_count
    if total < MIN_SAMPLE_SIZE:
        return "unknown", f"sample {total} < {MIN_SAMPLE_SIZE}"
    us_ratio = us_count / total if total else 0
    if us_ratio >= US_ONLY_THRESHOLD:
        return "US_only", f"US {us_count}/{total} = {us_ratio*100:.0f}% >= {US_ONLY_THRESHOLD*100:.0f}%"
    if us_ratio <= GLOBAL_ONLY_THRESHOLD:
        return "global_only", f"US {us_count}/{total} = {us_ratio*100:.0f}% <= {GLOBAL_ONLY_THRESHOLD*100:.0f}%"
    return "mixed_global", f"US {us_count}/{total} = {us_ratio*100:.0f}% in middle range"


def _extract_from_html(html: str) -> dict:
    """HTML から Buyer Location + 補助メトリクスを抽出.

    PoC で実証された regex パターンを使う.
    """
    result = {
        "countries": [],
        "total_sold": None,
        "us_count": None,
        "non_us_count": None,
        "avg_sold_price_usd": None,
        "avg_shipping_usd": None,
        "sell_through_pct": None,
        "total_sellers": None,
    }

    # Buyer Location 国別 count
    country_pattern = re.compile(
        r'data="BuyerLocation:::(\w+)".*?<span class="filter-menu__text">([^<]+?)\s*\((\d+)\)</span>'
    )
    countries = []
    us_count = 0
    non_us_count = 0
    all_matches = country_pattern.findall(html)
    for code, name, n_str in all_matches:
        n = int(n_str)
        if n > 0:
            countries.append({"code": code, "name": name.strip(), "count": n})
            if code == "US":
                us_count += n
            else:
                non_us_count += n

    if countries:
        result["countries"] = countries
        result["us_count"] = us_count
        result["non_us_count"] = non_us_count
        result["total_sold"] = us_count + non_us_count
    elif all_matches:
        # 全 country count=0 = filter panel は描画済だが SOLD 0 件 = 90 日 sold 無し.
        # 業務判定上は valid な「データ無し」signal で、_judge_primary_market(0,0) が
        # "unknown / sample 0 < 5" を返す前提.
        # 2026-05-05 W7-A 検証: 214 country 全 0 の listing が再試行ループで auto-stop
        # を誘発していた. ここで us_count/non_us_count=0 を明示すれば DB 保存され、
        # 次回 batch から自然 skip される.
        result["us_count"] = 0
        result["non_us_count"] = 0
        result["total_sold"] = 0

    # 補助メトリクス (body innerText 経由でも HTML 経由でも regex で取れる)
    avg_price_match = re.search(r'\$([\d,.]+)\s*(?:Avg|平均)\s*sold\s*price', html)
    if avg_price_match:
        result["avg_sold_price_usd"] = float(avg_price_match.group(1).replace(",", ""))

    avg_ship_match = re.search(r'\$([\d,.]+)\s*Avg\s*shipping', html)
    if avg_ship_match:
        result["avg_shipping_usd"] = float(avg_ship_match.group(1).replace(",", ""))

    st_match = re.search(r'([\d.]+)%\s*Sell-through', html)
    if st_match:
        result["sell_through_pct"] = float(st_match.group(1))

    ts_match = re.search(r'(\d+)\s*Total\s*sellers', html, re.IGNORECASE)
    if ts_match:
        result["total_sellers"] = int(ts_match.group(1))

    # プラグイン (Buyer Location helper) の独立集計と sanity check.
    # 業務判定 (primary_market) は US 比率閾値で行うため、整合性も US 比率で比較する.
    # 旧: 絶対値 (main_total vs plugin_total) で 10% 超乖離 → 破棄
    # 新: US 比率 (main_us/main_total vs plugin_us/plugin_total) で 5pp 超乖離 → 破棄
    # 変更理由 (2026-05-05): プラグインが部分 render 状態だと絶対値は大きく乖離するが
    # 比率は一致することが多く、旧 logic は false positive 多発で正常データを捨てていた.
    # plugin が US を抽出できない (us_count==0) 時は sanity check 不能 → skip.
    # plugin サンプルサイズが小さすぎる (plugin_total < 20) と統計的にノイズ過多
    # = 偽陽性 sanity_mismatch を生みやすい (例: plugin total=8 で US=7 → 87.5% vs
    # main 62% で 25pp 乖離するが、これは plugin の部分 render が原因で実害なし) → skip.
    PLUGIN_MIN_SAMPLE = 20
    plugin = _extract_plugin_aggregation(html)
    if plugin is not None:
        result["plugin_sanity_check"] = plugin
        main_us = result["us_count"] or 0
        main_non_us = result["non_us_count"] or 0
        main_total = main_us + main_non_us
        plugin_us = plugin["us_count"] or 0
        plugin_total = plugin["total"]
        if main_total > 0 and plugin_total >= PLUGIN_MIN_SAMPLE and plugin_us > 0:
            main_us_ratio = main_us / main_total
            plugin_us_ratio = plugin_us / plugin_total
            ratio_diff = abs(main_us_ratio - plugin_us_ratio)  # 比率差 (0.0-1.0, 単位は percentage point の小数)
            if ratio_diff > 0.05:
                # 5pp 超乖離 = Condition filter 残存等で主集計が歪んだ疑い.
                # 主 regex 値を None 化して呼出側の success 判定を落とす.
                result["sanity_mismatch"] = {
                    "main_us": result["us_count"],
                    "main_non_us": result["non_us_count"],
                    "main_total": main_total,
                    "main_us_ratio": main_us_ratio,
                    "plugin_us": plugin["us_count"],
                    "plugin_non_us": plugin["non_us_count"],
                    "plugin_total": plugin_total,
                    "plugin_us_ratio": plugin_us_ratio,
                    "ratio_diff": ratio_diff,
                }
                logger.error(
                    f"sanity check 失敗 (US 比率乖離 {ratio_diff*100:.1f}pp): "
                    f"主集計 US 比率={main_us_ratio:.1%} (US={result['us_count']}/total={main_total}) vs "
                    f"プラグイン US 比率={plugin_us_ratio:.1%} (US={plugin['us_count']}/total={plugin_total}). "
                    f"主 regex 値を破棄"
                )
                # 主集計だけでなく補助メトリクスも全て None 化.
                # filter 状態が歪んでいる以上、avg_price 等も同 filter 下の歪んだ値で
                # UI に表示されると user 誤誘導になる.
                result["us_count"] = None
                result["non_us_count"] = None
                result["total_sold"] = None
                result["avg_sold_price_usd"] = None
                result["avg_shipping_usd"] = None
                result["sell_through_pct"] = None
                result["total_sellers"] = None

    return result


def _extract_plugin_aggregation(html: str) -> Optional[dict]:
    """Buyer Location helper プラグインが挿入した DOM から集計値を取り出す.

    プラグインは独立 source として「Condition filter 無視の全集計」を出してくれるので、
    主 regex (filter 反映の集計) との sanity check に使う. プラグイン無しなら None.

    Returns:
        {us_count, non_us_count, total, breakdown: [(code, name, count), ...]} or None
    """
    # US count: id="buyer-us-checkbox" の直近 label 内 <strong>N</strong>
    us_match = re.search(
        r'id="buyer-us-checkbox"[^>]*>\s*<label[^>]*>[^<]*<strong>(\d+)</strong>',
        html, re.DOTALL,
    )
    # 各非US国: class="buyer-country-checkbox" + value="BuyerLocation:::XX" を持つ input
    # と直近 label の "Country (N)". DOM 出力は attribute 順が逆 (value→class) のことも
    # あるので lookahead で順不同マッチ.
    country_pattern = re.compile(
        r'<input(?=[^>]*\bvalue="BuyerLocation:::(\w+)")'
        r'(?=[^>]*\bbuyer-country-checkbox\b)[^>]*>\s*'
        r'<label[^>]*>([^<]+?)\s*\((\d+)\)</label>',
        re.DOTALL,
    )
    countries = country_pattern.findall(html)

    if not us_match and not countries:
        return None  # プラグイン未インストール

    us_count = int(us_match.group(1)) if us_match else 0
    breakdown = [(code, name, int(cnt)) for code, name, cnt in countries]
    non_us_count = sum(c for _, _, c in breakdown)

    return {
        "us_count": us_count,
        "non_us_count": non_us_count,
        "total": us_count + non_us_count,
        "breakdown": breakdown,
    }


def _build_terapeak_search_url(keyword: str, day_range: int = 365, *,
                               now_ms: Optional[int] = None,
                               seller_jp: bool = True) -> str:
    """Terapeak Research SOLD タブの直接 URL を構築.

    背景 (2026-05-05): `dayRange=N` だけだと eBay 内部 state が 30 days default に
    fall back する. `startDate`/`endDate` の ms timestamp 必須 (= `_detect_actual_dayrange`
    で actual=30 days と判定されて error 化する事故予防).

    Args:
        keyword: 検索キーワード (URL encoding 内部実施)
        day_range: 集計期間 (日数). startDate = endDate - day_range * 86400_000 ms.
        now_ms: テスト用にエポックミリ秒を注入可能. None なら time.time() 利用.
        seller_jp: True (既定) = sellerCountry=JP を強制 (日本セラー基準、通常の harvest).
            False = sellerCountry を付けず全世界 (依頼ボード#23 / 2026-06-15 の
            グラット除外チェック用。非日本セラーの出品/販売状況を見る場合のみ).

    Returns:
        Terapeak Research SOLD タブ URL (絶対 URL).
    """
    import time as _time
    if now_ms is None:
        now_ms = int(_time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    # W110 延長 fix (2026-05-09): sellerCountry を URL に明示.
    # 旧: user が Chrome UI で 1 度設定した seller=Japan が session state に保持される
    #     前提だったが、Chrome OOM 後のタブ再読み込みで filter が消失するケースを観測.
    # 新: 全 scrape navigation で sellerCountry=JP を URL parameter として強制 (session
    #     state 非依存). seller_jp=False のときのみ全世界集計のため省略する。
    base = (
        f"https://www.ebay.com/sh/research?marketplace=EBAY-US"
        f"&keywords={quote(keyword)}"
        f"&dayRange={day_range}"
        f"&endDate={now_ms}&startDate={start_ms}"
        f"&categoryId=0&offset=0&limit=50&tabName=SOLD"
    )
    if seller_jp:
        base += f"&sellerCountry={quote('SellerLocation:::JP')}"
    return base


def _is_ebay_error_redirect(url: str) -> bool:
    """eBay の bot detection / rate limit / session expire 時に起こる error redirect を検知.

    背景 (2026-05-05 W7-A 検証): block 状態時 eBay は `/n/error` (bot 検知系) や
    `/errors/...` (rate limit 系) にリダイレクト. 早期検知で BuyerLocation 30s timeout
    を待たず 1s で fast-fail させ、連続 5 件 auto-stop trigger を早める.

    Args:
        url: 現在のページ URL.

    Returns:
        error redirect なら True.
    """
    return "/n/error" in url or "/errors" in url


def extract_from_current_page(
    sku: str,
    expected_keyword: Optional[str] = None,
    *,
    day_range: int = 365,
    cdp_endpoint: str = CDP_ENDPOINT,
) -> MarketAnalysisResult:
    """user が手動で開いた Terapeak タブの **現在の DOM** から抽出するだけ.

    PoC scripts/terapeak_poc_cdp.py と同パターン. navigation しない.
    page.goto() を呼ばないので React の render は完了している前提.

    使い方:
      1. user が CDP Chrome で対象 keyword の Terapeak URL を手動で開く
      2. 結果表示完了まで待つ (買い手 location 等が見える)
      3. 本関数を呼ぶ → 現在 DOM から抽出
    """
    result_holder: list = [None]
    error_holder: list = [None]

    def _runner():
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            result_holder[0] = _extract_from_current_page_impl(
                sku, expected_keyword, day_range=day_range, cdp_endpoint=cdp_endpoint,
            )
        except Exception as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # 内部処理時間: _clear_condition_filters 最大 5x6s + More filters 20s + DOM 取得 +
    # diagnostic write + regex = worst case 90s 強. 余裕を持って 180s.
    t.join(timeout=180)
    if t.is_alive():
        res = MarketAnalysisResult(sku=sku, keyword=expected_keyword or "",
                                    day_range=day_range)
        res.error = "extract thread timeout (>180s)"
        return res
    if error_holder[0]:
        res = MarketAnalysisResult(sku=sku, keyword=expected_keyword or "",
                                    day_range=day_range)
        res.error = f"extract exception: {error_holder[0]}"
        return res
    return result_holder[0] or MarketAnalysisResult(
        sku=sku, keyword=expected_keyword or "", day_range=day_range,
        error="no result",
    )


def _extract_from_current_page_impl(
    sku: str,
    expected_keyword: Optional[str],
    *,
    day_range: int,
    cdp_endpoint: str,
) -> MarketAnalysisResult:
    """sync_playwright で実際の抽出処理. 別 thread から呼ばれる."""
    res = MarketAnalysisResult(sku=sku, keyword=expected_keyword or "",
                                day_range=day_range)
    if sync_playwright is None:
        res.error = "playwright not installed"
        return res

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            res.error = f"CDP connect failed: {e}"
            return res
        if not browser.contexts:
            res.error = "no browser context"
            return res

        # 全タブを列挙してログ
        all_pages = []
        terapeak_pages = []
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    u = pg.url
                except (PWTimeout, AttributeError) as e:
                    logger.debug(f"page.url 取得失敗 ({e})")
                    continue
                all_pages.append(u)
                if "ebay.com/sh/research" in u:
                    terapeak_pages.append((pg, u))

        logger.info(f"全タブ数: {len(all_pages)}")
        for u in all_pages:
            logger.info(f"  open page: {u[:150]}")
        logger.info(f"Terapeak タブ数: {len(terapeak_pages)}")

        # Terapeak タブのうち、URL の keywords= と dayRange= が expected と
        # 両方一致するものを **必須** で照合 (Q0 防御).
        # keyword 不一致 → 別商品データ誤保存の危険.
        # dayRange 不一致 → 母数が変わり primary_market 誤判定の主因 (例: 90→30 days で 6 件激減).
        target = None
        url_mismatch_log: list[str] = []
        if expected_keyword:
            import urllib.parse as _up
            expected_norm = expected_keyword.strip().lower()
            for pg, u in terapeak_pages:
                try:
                    qs = _up.parse_qs(_up.urlparse(u).query)
                    url_kw = (qs.get("keywords", [""])[0] or "").strip().lower()
                    url_dr_str = (qs.get("dayRange", [""])[0] or "").strip()
                    url_dr = int(url_dr_str) if url_dr_str.isdigit() else None
                except (ValueError, TypeError, AttributeError) as e:
                    logger.debug(f"URL 解析失敗 ({e})")
                    url_kw = ""
                    url_dr = None
                kw_ok = (url_kw == expected_norm)
                dr_ok = (url_dr == day_range)
                if kw_ok and dr_ok:
                    target = pg
                    res.page_url = u
                    logger.info(
                        f"keyword/dayRange 完全一致タブ採用: kw='{url_kw}' dr={url_dr} url={u[:100]}"
                    )
                    break
                # keyword だけ一致 → dayRange 不一致を診断ログに残す
                if kw_ok and not dr_ok:
                    url_mismatch_log.append(
                        f"keyword 一致だが dayRange 不一致 (URL={url_dr} vs expected={day_range}): {u[:80]}"
                    )
            # 完全一致なし → fallback として substring 一致 (旧挙動) は **行わない**
            # (false positive で別商品データを保存するリスクが利得を上回るため)

        if target is None:
            # keyword/dayRange 不一致 — 安全のため抽出しない
            mismatch_detail = "\n".join(url_mismatch_log) if url_mismatch_log else ""
            res.error = (
                f"CDP Chrome に keyword='{expected_keyword}' / dayRange={day_range} に "
                f"完全一致する Terapeak タブが見つかりません.\n"
                f"開いている Terapeak タブの URL: "
                f"{[u[:80] for _, u in terapeak_pages] if terapeak_pages else 'なし'}\n"
                + (f"診断: {mismatch_detail}\n" if mismatch_detail else "")
                + "対策: CDP Chrome の URL バーに正しい URL (Last 90 days 含む) を貼り付けてから再実行."
            )
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        # extracted keyword を URL から推測
        try:
            import urllib.parse as _up
            parsed = _up.urlparse(target.url)
            qs = _up.parse_qs(parsed.query)
            if not res.keyword:
                res.keyword = (qs.get("keywords", [""])[0]) or ""
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug(f"URL から keyword 推測失敗 ({e})")

        try:
            target.bring_to_front()
            target.wait_for_timeout(500)

            # 1) Condition filter (New/Used 等) を applied chip rail から自動解除.
            #    ブラウザに残った前回 filter が dropdown 集計を絞って誤判定の原因に
            #    なるのを防ぐ. 解除した label は debug log に出る.
            cleared = _clear_condition_filters(target, sku)
            if cleared:
                res.cleared_condition_filters = cleared

            # NOTE (2026-05-05): aspect-filter-error 早期検知は撤回 (Path A 同様、
            # 詳細は scrape_via_search_box の同一コメント参照).

            # 2) 「More filters」パネル必須 (BuyerLocation の DOM load 条件)
            if target.locator('.aspect-filter-multiselect').count() == 0:
                logger.info(f"{sku}: More filters パネル開く")
                try:
                    more_filters_btn = target.locator(
                        'button[aria-label="More filters"]'
                    ).first
                    more_filters_btn.click(timeout=8000)
                    target.wait_for_selector(
                        '.aspect-filter-multiselect',
                        timeout=10000,
                    )
                    target.wait_for_timeout(1500)
                except (PWTimeout, Exception) as e:
                    logger.warning(f"{sku}: More filters open 失敗 (続行): {e}")

            # 複数の DOM 取得方法を試す (page.content() が SPA で取れない場合がある)
            # 1) page.content() — 標準
            # 2) page.evaluate("document.documentElement.outerHTML") — live DOM 強制
            html_v1 = target.content()
            html_v2 = target.evaluate("() => document.documentElement.outerHTML")
            inner_text_full = target.evaluate("() => document.body.innerText")
            # 補助: viewport / DOM ready state / page.title
            page_meta = target.evaluate("""() => ({
                title: document.title,
                url: location.href,
                readyState: document.readyState,
                bodyChildCount: document.body.childElementCount,
                visibleAreaText: document.body.innerText.length,
            })""")
            # diagnostic を debug 用に保存
            from pathlib import Path as _P
            debug_dir = _P(__file__).resolve().parent.parent / "data" / "scraper_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            slug = sku.replace(":", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (debug_dir / f"{slug}_v1_content_{ts}.html").write_text(html_v1, encoding="utf-8")
            (debug_dir / f"{slug}_v2_outerHTML_{ts}.html").write_text(html_v2, encoding="utf-8")
            (debug_dir / f"{slug}_v3_innerText_full_{ts}.txt").write_text(inner_text_full, encoding="utf-8")
            (debug_dir / f"{slug}_v4_meta_{ts}.json").write_text(
                json.dumps(page_meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(f"DOM diagnostic: v1={len(html_v1):,} v2={len(html_v2):,} text={len(inner_text_full):,}")
            logger.info(f"  page meta: {page_meta}")
            # v2 (live DOM) を優先、v1 fallback
            html = html_v2 if "BuyerLocation" in html_v2 else html_v1
        except Exception as e:
            res.error = f"DOM 取得失敗: {e}"
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        # 画面の実 dayRange を検証 (Q0 防御). SPA の UI button label がずれていることが
        # あるので、JSON-encoded server state の selected:true から authoritative な値を取る.
        actual_dr = _detect_actual_dayrange(html)
        if actual_dr is not None and actual_dr != day_range:
            res.error = (
                f"画面の実 dayRange={actual_dr} days (期待 {day_range} days). "
                f"UI ボタンが 'Last {day_range} days' に見えても、内部 state は "
                f"{actual_dr} days になっています.\n"
                f"対策: Terapeak ページの 'Last X days' dropdown を一度開いて "
                f"'Last {day_range} days' を再選択してから抽出ボタンを押してください."
            )
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        extracted = _extract_from_html(html)
        res.countries_breakdown = extracted["countries"]
        res.us_count = extracted["us_count"]
        res.non_us_count = extracted["non_us_count"]
        res.total_sold = extracted["total_sold"]
        res.avg_sold_price_usd = extracted["avg_sold_price_usd"]
        res.avg_shipping_usd = extracted["avg_shipping_usd"]
        res.sell_through_pct = extracted["sell_through_pct"]
        res.total_sellers = extracted["total_sellers"]
        res.plugin_sanity_check = extracted.get("plugin_sanity_check")

        # sanity check 失敗 → 主集計は _extract_from_html 内で破棄済 (None).
        # error として返し pending_market_changes 登録を防ぐ (Q0 適合).
        if extracted.get("sanity_mismatch"):
            sm = extracted["sanity_mismatch"]
            res.error = (
                f"sanity check 失敗 (US 比率乖離 {sm['ratio_diff']*100:.1f}pp): "
                f"主集計 US 比率={sm['main_us_ratio']:.1%} (US={sm['main_us']}/total={sm['main_total']}) vs "
                f"プラグイン US 比率={sm['plugin_us_ratio']:.1%} (US={sm['plugin_us']}/total={sm['plugin_total']}). "
                f"対策: Terapeak 画面の applied filter chip を全て手動で外してから再実行."
            )

        if res.us_count is not None and res.non_us_count is not None:
            total = res.us_count + res.non_us_count
            if total > 0:
                res.us_ratio = res.us_count / total
            pm, reason = _judge_primary_market(res.us_count, res.non_us_count)
            res.primary_market = pm
            res.primary_market_reason = reason
            res.success = True
        else:
            # debug 用 HTML 保存
            from pathlib import Path as _P
            debug_dir = _P(__file__).resolve().parent.parent / "data" / "scraper_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            slug = sku.replace(":", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_html = debug_dir / f"{slug}_extract_{ts}.html"
            debug_html.write_text(html, encoding="utf-8")
            # エラー時に全タブと選択タブの URL を表示
            tab_list_str = "\n".join(f"  - {u[:120]}" for u in all_pages)
            res.error = (
                f"BuyerLocation 未抽出 (DOM size={len(html):,}, "
                f"html_keyword_match={(expected_keyword in html) if expected_keyword else 'N/A'}).\n"
                f"選択タブ: {res.page_url[:120] if res.page_url else 'なし'}\n"
                f"全タブ一覧:\n{tab_list_str}\n"
                f"対策: 不要な eBay タブを全て閉じて、対象 keyword の Terapeak タブを 1 つだけ開いてください.\n"
                f"debug: {debug_html.name}"
            )

        try:
            browser.close()
        except Exception as e:
            logger.debug(f"browser close ignored: {e}")

    return res


def scrape_via_search_box(
    sku: str,
    keyword: str,
    *,
    day_range: int = 365,
    cdp_endpoint: str = CDP_ENDPOINT,
) -> MarketAnalysisResult:
    """検索 box 自動化方式 (Plan A).

    user の手動操作 (検索 box タイプ + Research ボタンクリック) を programmatic に
    再現する. page.goto() を使わないので React 側の automation 検知を回避する仮説.

    前提:
      - CDP Chrome で Terapeak (/sh/research) ページが既に開いている (初期検索済)
      - フィルタ「Seller Country - Japan」「Sold tab」「Last 90 days」が active
    """
    result_holder: list = [None]
    error_holder: list = [None]

    def _runner():
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            result_holder[0] = _scrape_via_search_box_impl(
                sku, keyword, day_range=day_range, cdp_endpoint=cdp_endpoint,
            )
        except Exception as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # 内部処理時間: _clear_condition_filters 最大 5x6s + 検索 box 入力 + Research click +
    # URL 変化待ち 20s + networkidle 30s + More filters 20s + DOM 取得.
    # 大量カラーセット等 keyword が長い商品は worst case 100s 超 → 180s に延長.
    t.join(timeout=180)
    if t.is_alive():
        res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
        res.error = "thread timeout (>180s)"
        return res
    if error_holder[0]:
        res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
        res.error = f"exception: {error_holder[0]}"
        return res
    return result_holder[0] or MarketAnalysisResult(
        sku=sku, keyword=keyword, day_range=day_range, error="no result",
    )


def _scrape_via_search_box_impl(
    sku: str, keyword: str, *, day_range: int, cdp_endpoint: str,
) -> MarketAnalysisResult:
    """sync_playwright で検索 box 自動化."""
    res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
    if sync_playwright is None:
        res.error = "playwright not installed"
        return res

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            res.error = f"CDP connect failed: {e}"
            return res
        if not browser.contexts:
            res.error = "no browser context"
            return res

        # Terapeak タブを探す
        target = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    u = pg.url
                except (PWTimeout, AttributeError) as e:
                    logger.debug(f"page.url 取得失敗 ({e})")
                    continue
                if "ebay.com/sh/research" in u:
                    target = pg
                    res.page_url = u
                    break
            if target is not None:
                break

        if target is None:
            res.error = (
                "Terapeak タブが見つかりません. CDP Chrome で /sh/research ページ"
                "を開いてからやり直してください."
            )
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        try:
            target.bring_to_front()

            # 直接 URL navigation で keyword 検索を実施 (旧: 検索 box automation).
            # 詳細: `_build_terapeak_search_url` docstring 参照.
            new_url = _build_terapeak_search_url(keyword, day_range)
            try:
                target.goto(new_url, wait_until="domcontentloaded", timeout=30000)
            except PWTimeout as e:
                res.error = f"page.goto timeout (30s): {e}"
                try:
                    browser.close()
                except Exception as ce:
                    logger.debug(f"browser close ignored: {ce}")
                return res
            # W110(1) (2026-05-09): DOM 残留 bug 対策.
            # 旧: wait_for_timeout(3000) のみで React render 待機 → 5/8 04:08 abort 4 batch.
            # 真因: domcontentloaded 後も SPA の data fetch が継続中で、3s では新 keyword
            # の検索結果が DOM に置換されず、「前 keyword の DOM 残留」状態のまま BuyerLocation
            # selector が timeout していた.
            # 新: networkidle (20s) で全 fetch 完了確認 + 3s React render = 全 DOM 置換保証.
            # day_range=365 で sample 数増の処理時間も吸収.
            try:
                target.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                logger.warning(
                    f"{sku}: networkidle timeout (20s) — DOM 置換不完全リスク、"
                    "3s 追加 wait のみで継続 (BuyerLocation timeout 時 retry あり)"
                )
            target.wait_for_timeout(3000)  # React 最終 render 安定化

            # eBay bot detection / rate limit / session expire の検知 (fast-fail).
            # 詳細: `_is_ebay_error_redirect` docstring 参照.
            current_url = target.url
            if _is_ebay_error_redirect(current_url):
                res.error = (
                    f"eBay error redirect (URL={current_url[:120]}). "
                    f"bot 検知 / rate limit / session expire 推定. "
                    f"対策: 1-3 時間待機 or 別 IP / Chrome 手動で eBay 再ログイン."
                )
                try:
                    browser.close()
                except Exception as ce:
                    logger.debug(f"browser close ignored: {ce}")
                return res

            # NOTE (2026-05-05): aspect-filter-error の早期検知は撤回.
            # 「None of the listings... contain item aspect values」は 0 hit でなく
            # 「aspect 未付与の listing も含む」という Terapeak の警告で、実 data あり
            # でも表示される (= maxell MXCP-P100 で 38 sellers 取れる状況でも error 表示).
            # 偽データ確定の真の予防は下記 BuyerLocation セレクタ timeout error 化 と
            # sanity check (US 比率乖離) で十分.
            # `_check_no_results_error` helper 自体は test と共に残置 (将来活用余地).

            # Condition filter (New/Used 等) を applied chip rail から自動解除.
            # Plan B 経由でも前回 filter が残っていることがあるため.
            cleared = _clear_condition_filters(target, sku)
            if cleared:
                res.cleared_condition_filters = cleared

            # 「More filters」パネルを開く. これがないと BuyerLocation が DOM に load されない.
            # 既に開いていれば skip.
            if target.locator('.aspect-filter-multiselect').count() == 0:
                logger.info(f"{sku}: More filters パネル開く")
                more_filters_btn = target.locator(
                    'button[aria-label="More filters"]'
                ).first
                try:
                    more_filters_btn.click(timeout=10000)
                    target.wait_for_selector(
                        '.aspect-filter-multiselect',
                        timeout=15000,
                    )
                    target.wait_for_timeout(3000)  # React render 安定化 (前 1500ms だと不足、5/5 timing 事故由来)
                except PWTimeout:
                    logger.warning(f"{sku}: More filters クリック後 timeout")

            # BuyerLocation セレクタが DOM に attach されるまで待つ. timeout なら即失敗.
            # 2026-05-05 W7-A 偽データ事故 (HIGH-1) の根因: ここで warning だけで処理続行
            # していたため、前 keyword の DOM が残った状態で extract が走り偽データ確定.
            # Q0 silent skip / fake success 禁止に従い error 返却.
            # state='attached' (default 'visible' だと More filters 内の country 一覧が
            # scroll-clip 等で hidden 扱いされ false negative; 5/5 W7-A timing 検証で判明).
            # timeout 30s: keyword 切替時の data fetch + React re-render 余裕.
            # W110(1) (2026-05-09): timeout 時 1 回 page reload retry を追加.
            # 5/8 04:08 abort の root cause = networkidle 待機なし + retry なし.
            try:
                target.wait_for_selector(
                    '[data^="BuyerLocation:::"]',
                    state='attached',
                    timeout=30000,
                )
            except PWTimeout:
                logger.warning(
                    f"{sku}: BuyerLocation 1st timeout (30s)、page reload で 1 回 retry"
                )
                try:
                    target.reload(wait_until="domcontentloaded", timeout=30000)
                    try:
                        target.wait_for_load_state("networkidle", timeout=20000)
                    except PWTimeout:
                        pass
                    target.wait_for_timeout(3000)
                    # More filters 再オープン (reload 後は閉じている)
                    if target.locator('.aspect-filter-multiselect').count() == 0:
                        try:
                            target.locator(
                                'button[aria-label="More filters"]'
                            ).first.click(timeout=10000)
                            target.wait_for_selector(
                                '.aspect-filter-multiselect', timeout=15000,
                            )
                            target.wait_for_timeout(3000)
                        except PWTimeout:
                            pass
                    target.wait_for_selector(
                        '[data^="BuyerLocation:::"]',
                        state='attached',
                        timeout=30000,
                    )
                    logger.info(f"{sku}: BuyerLocation reload retry 成功")
                except PWTimeout:
                    res.error = (
                        f"BuyerLocation セレクタ未 attach (30s timeout x 2 incl. reload). "
                        f"前 keyword の DOM 残留で偽データ確定リスクのため抽出せず. "
                        f"対策: 検索結果 load 完了を待つか、画面リロード後に再実行."
                    )
                    try:
                        browser.close()
                    except Exception as e:
                        logger.debug(f"browser close ignored: {e}")
                    return res

            # W110(1) extension (2026-05-09): OOM 防止. dayRange=365 で documentElement
            # outerHTML が 50MB+ になり Chrome V8 が "Out of Memory" タブクラッシュする
            # 事故を pilot 39/50 件で確認 (5/9 07:34, 44 件目以降 thread timeout 連発).
            # 対策: 必要 selector のみ outerHTML 抽出 + body innerText で metrics 用 text 確保.
            # 既存 regex (_extract_from_html / _extract_plugin_aggregation /
            # _detect_actual_dayrange) は対象 selector の outerHTML 連結内で機能維持.
            html = target.evaluate(r"""() => {
                const parts = [];
                // 1. dayRange 検証 (results-header__left)
                const headerEl = document.querySelector('.results-header__left');
                if (headerEl) parts.push(headerEl.outerHTML);
                // 2. BuyerLocation filter rows (主集計)
                document.querySelectorAll('[data^="BuyerLocation:::"]').forEach(el => {
                    parts.push(el.outerHTML);
                });
                // 3. Plugin: US checkbox の wrapper (id + label structure 保持)
                const usCheckbox = document.querySelector('#buyer-us-checkbox');
                if (usCheckbox) {
                    const wrapper = usCheckbox.closest('div, li, p') || usCheckbox.parentElement;
                    if (wrapper) parts.push(wrapper.outerHTML);
                }
                // 4. Plugin: 各国 checkbox + label
                document.querySelectorAll('.buyer-country-checkbox').forEach(el => {
                    const wrapper = el.closest('div, li, p') || el.parentElement;
                    if (wrapper) parts.push(wrapper.outerHTML);
                });
                // 5. Metrics text (avg price / shipping / sell-through / total sellers の regex 用).
                // body.innerText は outerHTML より遥かに小さい (5-50x 削減).
                parts.push('<!-- METRICS_TEXT -->');
                parts.push(document.body.innerText || '');
                return parts.join('\n');
            }""")
            res.page_url = target.url

        except Exception as e:
            res.error = f"検索 box 操作失敗: {e}"
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        # 抽出
        # 画面の実 dayRange を検証 (Q0 防御). SPA の UI button label がずれていることが
        # あるので、JSON-encoded server state の selected:true から authoritative な値を取る.
        actual_dr = _detect_actual_dayrange(html)
        if actual_dr is not None and actual_dr != day_range:
            res.error = (
                f"画面の実 dayRange={actual_dr} days (期待 {day_range} days). "
                f"UI ボタンが 'Last {day_range} days' に見えても、内部 state は "
                f"{actual_dr} days になっています.\n"
                f"対策: Terapeak ページの 'Last X days' dropdown を一度開いて "
                f"'Last {day_range} days' を再選択してから抽出ボタンを押してください."
            )
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return res

        extracted = _extract_from_html(html)
        res.countries_breakdown = extracted["countries"]
        res.us_count = extracted["us_count"]
        res.non_us_count = extracted["non_us_count"]
        res.total_sold = extracted["total_sold"]
        res.avg_sold_price_usd = extracted["avg_sold_price_usd"]
        res.avg_shipping_usd = extracted["avg_shipping_usd"]
        res.sell_through_pct = extracted["sell_through_pct"]
        res.total_sellers = extracted["total_sellers"]
        res.plugin_sanity_check = extracted.get("plugin_sanity_check")

        # sanity check 失敗 → 主集計は _extract_from_html 内で破棄済 (None).
        # error として返し pending_market_changes 登録を防ぐ (Q0 適合).
        if extracted.get("sanity_mismatch"):
            sm = extracted["sanity_mismatch"]
            res.error = (
                f"sanity check 失敗 (US 比率乖離 {sm['ratio_diff']*100:.1f}pp): "
                f"主集計 US 比率={sm['main_us_ratio']:.1%} (US={sm['main_us']}/total={sm['main_total']}) vs "
                f"プラグイン US 比率={sm['plugin_us_ratio']:.1%} (US={sm['plugin_us']}/total={sm['plugin_total']}). "
                f"対策: Terapeak 画面の applied filter chip を全て手動で外してから再実行."
            )

        if res.us_count is not None and res.non_us_count is not None:
            total = res.us_count + res.non_us_count
            if total > 0:
                res.us_ratio = res.us_count / total
            pm, reason = _judge_primary_market(res.us_count, res.non_us_count)
            res.primary_market = pm
            res.primary_market_reason = reason
            res.success = True
        else:
            from pathlib import Path as _P
            debug_dir = _P(__file__).resolve().parent.parent / "data" / "scraper_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            slug = sku.replace(":", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (debug_dir / f"{slug}_searchbox_{ts}.html").write_text(html, encoding="utf-8")
            res.error = (
                f"BuyerLocation 未抽出 (DOM size={len(html):,}). "
                f"検索 box 自動化で render 不発. debug: {slug}_searchbox_{ts}.html"
            )

        try:
            browser.close()
        except Exception as e:
            logger.debug(f"browser close ignored: {e}")

    return res


def scrape_one_sku(
    sku: str,
    keyword: str,
    *,
    day_range: int = 365,
    cdp_endpoint: str = CDP_ENDPOINT,
    timeout_ms: int = 30000,
) -> MarketAnalysisResult:
    """1 SKU を Terapeak で scrape (Streamlit/Windows対応の thread wrapper).

    Streamlit は Tornado の SelectorEventLoop を使い、Playwright が要求する
    ProactorEventLoop と非互換 (NotImplementedError). 別 thread で
    Proactor policy をセットして実行する.
    """
    result_holder: list = [None]
    error_holder: list = [None]

    def _runner():
        try:
            if sys.platform == "win32":
                # この thread 内で ProactorEventLoop policy を設定
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            result_holder[0] = _scrape_one_sku_impl(
                sku, keyword,
                day_range=day_range,
                cdp_endpoint=cdp_endpoint,
                timeout_ms=timeout_ms,
            )
        except Exception as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # SPA render 待ち含めて 180 秒 timeout
    t.join(timeout=180)
    if t.is_alive():
        # timeout
        res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
        res.error = "scrape thread timeout (>180s)"
        return res
    if error_holder[0]:
        res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
        res.error = f"scrape exception: {error_holder[0]}"
        logger.error(res.error)
        return res
    return result_holder[0] or MarketAnalysisResult(
        sku=sku, keyword=keyword, day_range=day_range, error="no result returned"
    )


def _scrape_one_sku_impl(
    sku: str,
    keyword: str,
    *,
    day_range: int = 365,
    cdp_endpoint: str = CDP_ENDPOINT,
    timeout_ms: int = 30000,
) -> MarketAnalysisResult:
    """実際の scrape 処理 (sync_playwright 直叩き).

    呼出元は scrape_one_sku() の thread wrapper. 直接呼ばないこと
    (Windows + Streamlit で event loop 問題発生).
    """
    res = MarketAnalysisResult(sku=sku, keyword=keyword, day_range=day_range)
    if sync_playwright is None:
        res.error = "playwright not installed"
        return res

    url = build_terapeak_url(keyword, day_range=day_range)
    res.page_url = url

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            res.error = f"CDP connect failed: {e}"
            logger.error(res.error)
            return res

        if not browser.contexts:
            res.error = "no browser context"
            return res
        context = browser.contexts[0]

        # 既存タブの中で Terapeak ページがあればそれを再利用 (React render が
        # 新規タブで動かない問題の回避). なければ最初のタブを使い回す.
        page = None
        for pg in context.pages:
            try:
                u = pg.url
                if "ebay.com/sh/research" in u:
                    page = pg
                    logger.debug(f"既存 Terapeak タブを再利用: {u[:100]}")
                    break
            except (PWTimeout, AttributeError) as e:
                logger.debug(f"page.url 取得失敗 ({e})")
                continue
        if page is None:
            # 既存タブの 1 個目を再利用 (新規タブ作成は React render 不安定のため避ける)
            if context.pages:
                page = context.pages[0]
                logger.debug(f"既存タブを再利用 (Terapeak 以外): {page.url[:100]}")
            else:
                page = context.new_page()
                logger.debug("新規タブ作成 (既存タブなし)")
        page.bring_to_front()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Terapeak は SPA. データ fetch + render 完了を multi-stage で待つ.
            # Stage 1: networkidle (XHR/API call 終了)
            try:
                page.wait_for_load_state("networkidle", timeout=45000)
            except PWTimeout:
                logger.debug(f"networkidle timeout for {sku} (continuing)")

            # Stage 2: keyword 自体が DOM に現れる (data load 完了 signal)
            try:
                page.wait_for_selector(
                    f'text="{keyword}"',
                    timeout=20000,
                )
            except PWTimeout:
                logger.debug(f"keyword text timeout for {sku} (continuing)")

            # Stage 3: BuyerLocation filter が render
            try:
                page.wait_for_selector(
                    '[data^="BuyerLocation:::"]',
                    timeout=20000,
                )
            except PWTimeout:
                logger.warning(f"BuyerLocation filter still not rendered for {sku}")

            # Stage 4: 最終 buffer (React の差分 render 余裕)
            page.wait_for_timeout(3000)

            html = page.content()
            # 画面の実 dayRange を検証 (Q0 防御).
            actual_dr = _detect_actual_dayrange(html)
            if actual_dr is not None and actual_dr != day_range:
                res.error = (
                    f"画面の実 dayRange={actual_dr} days (期待 {day_range} days). "
                    f"対策: Terapeak の 'Last X days' dropdown を再選択してください."
                )
                try:
                    browser.close()
                except Exception as e:
                    logger.debug(f"browser close ignored: {e}")
                return res

            extracted = _extract_from_html(html)

            res.countries_breakdown = extracted["countries"]
            res.us_count = extracted["us_count"]
            res.non_us_count = extracted["non_us_count"]
            res.total_sold = extracted["total_sold"]
            res.avg_sold_price_usd = extracted["avg_sold_price_usd"]
            res.avg_shipping_usd = extracted["avg_shipping_usd"]
            res.sell_through_pct = extracted["sell_through_pct"]
            res.total_sellers = extracted["total_sellers"]
            res.plugin_sanity_check = extracted.get("plugin_sanity_check")

            # sanity check 失敗 → 主集計は _extract_from_html 内で破棄済.
            if extracted.get("sanity_mismatch"):
                sm = extracted["sanity_mismatch"]
                res.error = (
                    f"sanity check 失敗 (US 比率乖離 {sm['ratio_diff']*100:.1f}pp): "
                    f"主集計 US 比率={sm['main_us_ratio']:.1%} (US={sm['main_us']}/total={sm['main_total']}) vs "
                    f"プラグイン US 比率={sm['plugin_us_ratio']:.1%} (US={sm['plugin_us']}/total={sm['plugin_total']}). "
                    f"対策: applied filter chip を全て外して再実行."
                )

            if res.us_count is not None and res.non_us_count is not None:
                total = res.us_count + res.non_us_count
                if total > 0:
                    res.us_ratio = res.us_count / total
                pm, reason = _judge_primary_market(res.us_count, res.non_us_count)
                res.primary_market = pm
                res.primary_market_reason = reason
                res.success = True
            else:
                # debug 用に HTML スナップショット保存 + ログイン状態を text で診断
                from pathlib import Path as _P
                debug_dir = _P(__file__).resolve().parent.parent / "data" / "scraper_debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                slug = sku.replace(":", "_")
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                debug_html = debug_dir / f"{slug}_{ts}.html"
                debug_html.write_text(html, encoding="utf-8")
                # ログイン状態判定 (Sign in リンク有無 / We looked everywhere 有無)
                if "We looked everywhere" in html or "page is missing" in html:
                    res.error = f"404 page (Akamai or URL issue). debug: {debug_html.name}"
                elif 'href="https://signin.ebay.com' in html and "Hi " not in html[:5000]:
                    res.error = f"login expired. debug: {debug_html.name}"
                elif "BuyerLocation" not in html:
                    res.error = f"BuyerLocation セクション見つからず (filter 折り畳み? ページ構造変化?). debug: {debug_html.name}"
                else:
                    res.error = f"抽出失敗 (BuyerLocation 存在するが parse 失敗). debug: {debug_html.name}"

        except Exception as e:
            res.error = f"scrape error: {e}"
            logger.error(res.error)
        finally:
            # page.close() しない (既存タブを再利用しているため、user の Chrome を閉じない)
            try:
                browser.close()  # CDP connection を切断するだけ (Chrome 自体は残る)
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")

    return res


def save_to_db(res: MarketAnalysisResult, ebay_item_id: Optional[str] = None) -> Optional[int]:
    """市場分析結果を market_analysis に insert. ebay_listings の primary_market も更新.

    Returns:
        inserted market_analysis.id (失敗時 None)
    """
    if not res.success:
        logger.warning(f"save_to_db skipped (not success): {res.sku} / {res.error}")
        return None

    import sqlite3
    from monitor.database import get_conn

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO market_analysis (
                sku, ebay_item_id, keyword, day_range,
                total_sold, us_count, non_us_count, countries_breakdown,
                avg_sold_price_usd, avg_shipping_usd, sell_through_pct, total_sellers,
                primary_market, primary_market_reason, scraped_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                res.sku, ebay_item_id, res.keyword, res.day_range,
                res.total_sold, res.us_count, res.non_us_count,
                json.dumps(res.countries_breakdown, ensure_ascii=False),
                res.avg_sold_price_usd, res.avg_shipping_usd,
                res.sell_through_pct, res.total_sellers,
                res.primary_market, res.primary_market_reason,
                res.scraped_at, "terapeak_cdp",
            ),
        )
        inserted_id = cur.lastrowid

        # ebay_listings の primary_market を更新 (即時反映でなく承認後に反映するため、
        # ここでは market_analysis_at と sample_size のみ更新. primary_market 自体は
        # 承認 UI 経由で確定する).
        if ebay_item_id:
            conn.execute(
                """UPDATE ebay_listings SET
                    market_analysis_at = ?,
                    market_sample_size = ?,
                    us_buyer_ratio = ?
                WHERE ebay_item_id = ?""",
                (res.scraped_at, res.total_sold, res.us_ratio, ebay_item_id),
            )

    return inserted_id


def propose_market_change_for_listing(
    *,
    ebay_item_id: str,
    sku: str,
    market_analysis_id: int,
    proposed_market: str,
    reason: str,
) -> int:
    """1 listing (ebay_item_id) に対して、現値 ≠ 提案なら pending に upsert.

    W7-A Phase 3 (2026-04-29 SKU 主キー設計崩壊事故 再発防止):
      - 旧版 propose_market_change_for_listings(sku=...) は SKU 配下の全 listing に
        同じ proposed_market を展開する設計だったが、stock:01 が 40 異商品で
        共有されているため「scrape 1 回 → 40 異商品に同じ判定」事故が発生.
      - 本版は 1 listing 1 propose で物理的に展開を排除.
      - 呼出元で listing ごとに scrape (or cache hit) してから本関数を呼ぶ.

    Returns:
        insert/replace された件数 (0 or 1).
    """
    from monitor.database import get_conn

    if not ebay_item_id:
        return 0  # ebay_item_id 必須 (PK / NOT NULL)

    with get_conn() as conn:
        # 2026-05-06: quantity_ebay >= 1 フィルタ削除.
        # W7-A は buyer 分布判定で在庫数は無関係. is_ended=0 で販売中判定.
        # 旧 qty フィルタで無在庫商品の pending 提案が偽装成功 (Q0 違反) になっていた.
        row = conn.execute(
            """SELECT COALESCE(primary_market, '') AS pm
               FROM ebay_listings
               WHERE ebay_item_id = ? AND COALESCE(is_ended, 0) = 0""",
            (ebay_item_id,),
        ).fetchone()
        if not row:
            return 0  # listing 不在または is_ended=1 (出品終了)
        current = row["pm"] or None
        if current == proposed_market:
            return 0  # 変化なし

        conn.execute(
            """INSERT OR REPLACE INTO pending_market_changes
               (ebay_item_id, sku, current_market, proposed_market,
                proposed_at, market_analysis_id, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ebay_item_id, sku, current, proposed_market,
             datetime.now().isoformat(), market_analysis_id, reason),
        )
    return 1


# 旧 propose_market_change (sku PK 前提) は W7-A Phase 3 で削除済.
# 全 caller が propose_market_change_for_listings に移行 (2026-04-29 grep 確認).


# ---------------------------------------------------------------------------
# W229 ハーベスト機能 (Phase 2 / 2026-06-10)
# 既存関数は一切変更しない (K2). 以下は全て新規追加.
# ---------------------------------------------------------------------------

import datetime as _dt
import html as _html_mod
import time as _time
# 依頼ボード#23 (2026-06-15): _scrape_worldwide_glut_signals が module-level で
# _random を参照する (jitter)。既存の _random は関数ローカル import (L1891/L2526) のみ
# だったため module-level に昇格 (helper の NameError → glut 判定 silent 無効化を防ぐ)。
import random as _random

# JST 固定 offset (Windows tzdata 不在リスク回避 / DST 無し)
_JST = _dt.timezone(_dt.timedelta(hours=9), name="JST")
# Pacific 固定 offset (UTC-7). probe6_tz_calib 実測で Terapeak 日付軸 = US Pacific 確定.
# HIGH-A 修正 (2026-06-10): UTC-8 から UTC-7 へ変更.
# 理由:
#   UTC-8 (PST) を使うと target 日 00:00 UTC-8 = 夏期 PDT では target 日 01:00 PDT となり、
#   target 日 00:00-01:00 PDT の売上が窓から漏れる (方向が逆だった).
#   UTC-7 固定にすることで:
#     - 夏期 PDT (UTC-7): target 日ちょうど 00:00 PDT 開始 = 漏れなし
#     - 冬期 PST (UTC-8): target 日 00:00 UTC-7 = 前日 23:00 PST 開始 = 余分包含だが
#       filter_harvest_window の target 一致採用と all_before_target continue が吸収する.
# 実測: startDate=2024-06-10 00:00 PDT(UTC-7) → ヘッダ "Jun 10, 2024" 確認 (2026-06-10).
_PST = _dt.timezone(_dt.timedelta(hours=-7), name="PACIFIC-7")


def _two_year_target(today: _dt.date) -> _dt.date:
    """today から 2 年前のカレンダー日付を返す (2/29 は 2/28 に丸め).

    build_harvest_url と filter_harvest_window の両方から呼ぶ共通 helper.
    二重実装による再乖離を防止する (HIGH-1 対応).
    """
    try:
        return today.replace(year=today.year - 2)
    except ValueError:
        # 2/29 → 前年は 2/28
        return today.replace(year=today.year - 2, day=28)


@dataclass
class HarvestedProduct:
    """Terapeak Product Research 結果リストの 1 行."""
    title: str
    avg_sold_price_usd: Optional[float]
    total_sold_count: Optional[int]
    date_last_sold: Optional[_dt.date]
    research_url: str
    image_url: Optional[str]
    avg_shipping_cost_usd: Optional[float]


@dataclass
class HarvestResult:
    """harvest_product_list の戻り値."""
    products: list[HarvestedProduct]
    pages_loaded: int
    error: Optional[str]
    success: bool


_HARVEST_PATTERNS = frozenset({"fresh_24h", "two_year_echo"})


def build_harvest_url(
    keyword: str,
    pattern: str,
    *,
    category_id: Optional[int] = None,
    min_price: int = 100,
    offset: int = 0,
    now_ms: Optional[int] = None,
) -> str:
    """Terapeak Product Research 結果リストの URL を構築 (W229 ハーベスト用).

    Args:
        keyword: 検索キーワード (必須、空文字は ValueError)
        pattern: 'fresh_24h' または 'two_year_echo'
        category_id: カテゴリ ID (None → 0)
        min_price: 価格下限 USD (デフォルト 100)
        offset: ページオフセット (50 刻み)
        now_ms: テスト用エポックミリ秒注入 (None なら time.time() 利用)

    Returns:
        Terapeak Research SOLD タブ URL

    Raises:
        ValueError: pattern が不正または keyword が空
    """
    if not keyword.strip():
        raise ValueError(f"keyword must not be empty: {keyword!r}")
    if pattern not in _HARVEST_PATTERNS:
        raise ValueError(
            f"pattern must be one of {sorted(_HARVEST_PATTERNS)}, got {pattern!r}"
        )

    if now_ms is None:
        now_ms = int(_time.time() * 1000)

    if pattern == "fresh_24h":
        day_range = 7
        sorting = "-datelastsold"
        start_ms = now_ms - day_range * 24 * 3600 * 1000
    else:  # two_year_echo
        day_range = 730
        sorting = "datelastsold"
        # probe6_tz_calib (2026-06-10) 実測: Terapeak 日付軸 = US Pacific.
        # startDate = target 日 00:00 UTC-7 (PACIFIC-7) に設定する.
        # HIGH-A 修正 (2026-06-10): UTC-8 → UTC-7 へ変更.
        # 理由: UTC-7 固定 (夏期 PDT に一致) で target 日 00:00 から開始し漏れを防ぐ.
        #   冬期 PST 時は前日 23:00 PST 開始だが余分包含で問題なし (filter が吸収).
        #   旧実装の「JST 00:00 − 1 日 buffer」は page 1 が buffer 日 (target-1) の
        #   行 50 件で埋まり、filtered=0 で即 break → 毎晩 0 件の空振りになっていた.
        # _detect_actual_dayrange ±2 tolerance との整合:
        #   target 00:00 UTC-7 → now の span ≈ 730 日, ±2 内に収まる (問題なし).
        # 2/29 丸め: _two_year_target が維持する (既存動作と同一).
        today_jst = _dt.datetime.fromtimestamp(now_ms / 1000, tz=_JST).date()
        target = _two_year_target(today_jst)
        # target 日 00:00 PACIFIC-7 = UTC-7 で epoch ms を計算
        target_midnight_pst = _dt.datetime.combine(target, _dt.time(0), tzinfo=_PST)
        start_ms = int(target_midnight_pst.timestamp() * 1000)
    cat_id = category_id if category_id is not None else 0

    return (
        f"https://www.ebay.com/sh/research?marketplace=EBAY-US"
        f"&keywords={quote(keyword)}"
        f"&dayRange={day_range}"
        f"&endDate={now_ms}&startDate={start_ms}"
        f"&categoryId={cat_id}"
        f"&offset={offset}&limit=50"
        f"&tabName=SOLD"
        f"&sellerCountry={quote('SellerLocation:::JP')}"
        f"&sorting={sorting}"
        f"&minPrice={min_price}"
    )


def parse_harvest_rows(html: str) -> list[HarvestedProduct]:
    """HTML から research-table-row を走査して HarvestedProduct リストを返す.

    probe 確定の実 DOM 構造に基づく regex パース (K1: 新規 BS4 依存は追加しない).

    - パース不能行は skip + logger.warning (Q0 silent drop 禁止)
    - 価格 "$333.51 Fixed price" → float 333.51 (カンマ除去対応)
    - "-" や空は None
    """
    # tr.research-table-row を全件抽出
    row_pattern = re.compile(
        r'<tr class="research-table-row">(.*?)</tr>',
        re.DOTALL,
    )

    # 各 td class suffix の内容を抽出するパターン
    # td 全体 (</td> まで) を取得してから内部を解析する.
    # 理由: avgSoldPrice 等は <div class="item-with-subtitle"><div>$385.00</div>...
    # という 2 段ネスト構造のため、><div[^>]*> パターンだと外側 div を消費して
    # "$385.00" → "85.00" を返す誤りが生じる. td 全体を取得して最初の <div>text</div>
    # を探す方式に統一する (probe 確定構造).
    _TD = lambda suffix: re.compile(  # noqa: E731
        r'class="research-table-row__item research-table-row__'
        + re.escape(suffix)
        + r'">(.*?)</td>',
        re.DOTALL,
    )
    _pat_price = _TD("avgSoldPrice")
    _pat_count = _TD("totalSoldCount")
    _pat_date = _TD("dateLastSold")
    _pat_ship = _TD("avgShippingCost")

    # product-info から title / url / image を抽出
    _pat_title = re.compile(r'<span data-item-id="\d+">(.*?)</span>', re.DOTALL)
    _pat_url = re.compile(r'href="(https://www\.ebay\.com/itm/[^"]+)"')
    _pat_img = re.compile(r'<img class="small" src="([^"]+)"')

    products: list[HarvestedProduct] = []
    skip_count = 0

    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)

        # --- title ---
        t_m = _pat_title.search(row_html)
        if not t_m:
            skip_count += 1
            logger.warning("parse_harvest_rows: title 未取得のため行をスキップ")
            continue
        title = _html_mod.unescape(t_m.group(1).strip())

        # --- research_url ---
        url_m = _pat_url.search(row_html)
        research_url = (
            _html_mod.unescape(url_m.group(1)) if url_m else ""
        )

        # --- image_url ---
        img_m = _pat_img.search(row_html)
        raw_img = img_m.group(1) if img_m else None
        if raw_img and raw_img.startswith("//"):
            raw_img = "https:" + raw_img
        image_url: Optional[str] = raw_img or None

        # --- avg_sold_price_usd ---
        p_m = _pat_price.search(row_html)
        avg_sold_price_usd: Optional[float] = None
        if p_m:
            # 最初の <div> 内テキスト: "$385.00" or "$48,358.97" or "-"
            inner = re.search(r'<div>([^<]+)</div>', p_m.group(1))
            if inner:
                raw_price = inner.group(1).strip()
                price_num = re.search(r'\$([\d,.]+)', raw_price)
                if price_num:
                    try:
                        avg_sold_price_usd = float(
                            price_num.group(1).replace(",", "")
                        )
                    except ValueError:
                        pass

        # --- total_sold_count ---
        # MEDIUM-1: カンマ付き "1,234" も取得できるよう ([\d,]+) + replace(",", "") に変更.
        # 価格側は対応済みだったが count 側が非対称だった.
        c_m = _pat_count.search(row_html)
        total_sold_count: Optional[int] = None
        if c_m:
            inner = re.search(r'<div>([\d,]+)</div>', c_m.group(1))
            if inner:
                try:
                    total_sold_count = int(inner.group(1).replace(",", ""))
                except ValueError:
                    pass

        # --- date_last_sold ---
        d_m = _pat_date.search(row_html)
        date_last_sold: Optional[_dt.date] = None
        if d_m:
            inner = re.search(r'<div>([^<]+)</div>', d_m.group(1))
            if inner:
                raw_date = inner.group(1).strip()
                parsed_dt = _parse_terapeak_date(raw_date)
                if parsed_dt is not None:
                    date_last_sold = parsed_dt.date()

        # --- avg_shipping_cost_usd ---
        s_m = _pat_ship.search(row_html)
        avg_shipping_cost_usd: Optional[float] = None
        if s_m:
            inner = re.search(r'<div>([^<]+)</div>', s_m.group(1))
            if inner:
                raw_ship = inner.group(1).strip()
                ship_num = re.search(r'\$([\d,.]+)', raw_ship)
                if ship_num:
                    try:
                        avg_shipping_cost_usd = float(
                            ship_num.group(1).replace(",", "")
                        )
                    except ValueError:
                        pass

        products.append(HarvestedProduct(
            title=title,
            avg_sold_price_usd=avg_sold_price_usd,
            total_sold_count=total_sold_count,
            date_last_sold=date_last_sold,
            research_url=research_url,
            image_url=image_url,
            avg_shipping_cost_usd=avg_shipping_cost_usd,
        ))

    if skip_count:
        logger.warning(
            f"parse_harvest_rows: {skip_count} 行をスキップ (title 未取得)"
        )

    return products


def filter_harvest_window(
    products: list[HarvestedProduct],
    pattern: str,
    *,
    today_jst: Optional[_dt.date] = None,
) -> list[HarvestedProduct]:
    """date_last_sold に基づいて採取窓フィルタを適用する.

    Args:
        products: parse_harvest_rows の出力
        pattern: 'fresh_24h' または 'two_year_echo'
        today_jst: テスト注入用 (None なら JST 現在日付)

    Returns:
        窓内の HarvestedProduct リスト

    Raises:
        ValueError: pattern が不正
    """
    if pattern not in _HARVEST_PATTERNS:
        raise ValueError(
            f"pattern must be one of {sorted(_HARVEST_PATTERNS)}, got {pattern!r}"
        )

    if today_jst is None:
        # MEDIUM-2: ZoneInfo("Asia/Tokyo") → 固定 offset に置換 (Windows tzdata 不在リスク)
        today_jst = _dt.datetime.now(tz=_JST).date()

    none_count = 0
    results: list[HarvestedProduct] = []

    for p in products:
        if p.date_last_sold is None:
            none_count += 1
            continue

        if pattern == "fresh_24h":
            # JST 今日 or 昨日 (日付粒度のみのため 2 日マッチ近似)
            yesterday = today_jst - _dt.timedelta(days=1)
            if p.date_last_sold in (today_jst, yesterday):
                results.append(p)

        else:  # two_year_echo
            # HIGH-1: 共通 helper で target を計算 (build_harvest_url と一致保証)
            target = _two_year_target(today_jst)
            if p.date_last_sold == target:
                results.append(p)

    if none_count:
        logger.warning(
            f"filter_harvest_window: date_last_sold=None の行 {none_count} 件を除外"
        )

    return results


def _poll_harvest_rows(page: object, timeout_s: float = 30.0) -> bool:
    """tr.research-table-row が DOM に出現するまでポーリング.

    networkidle 禁止 (SPA 常時通信) のため domcontentloaded + ポーリングで代替.

    Args:
        page: Playwright page オブジェクト
        timeout_s: タイムアウト秒

    Returns:
        True なら出現、False はタイムアウト
    """
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            count = page.evaluate(
                "() => document.querySelectorAll('tr.research-table-row').length"
            )
            if count and count > 0:
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"_poll_harvest_rows: evaluate 失敗 ({e})")
        _time.sleep(1.0)
    return False


def harvest_product_list(
    keyword: str,
    pattern: str,
    *,
    category_id: Optional[int] = None,
    min_price: int = 100,
    max_pages: int = 2,
    cdp_endpoint: str = CDP_ENDPOINT,
    sleep_seconds: float = 3.0,
) -> HarvestResult:
    """Terapeak Product Research 結果リストを取得し窓フィルタを適用する.

    既存 extract_from_current_page と同じ thread wrapper パターン (_runner + queue).
    新規タブを開いて navigate し、完了後にタブを閉じる.

    Args:
        keyword: 検索キーワード
        pattern: 'fresh_24h' または 'two_year_echo'
        category_id: カテゴリ ID (None → 0)
        min_price: 価格下限 USD
        max_pages: 最大ページ数 (50件/ページ)
        cdp_endpoint: CDP エンドポイント
        sleep_seconds: ページ間 sleep 秒 (jitter 0.7-1.5 倍)

    Returns:
        HarvestResult
    """
    # MEDIUM-C: max_pages ガード (0/負値で join(0) 即時 return → 偽装成功を防ぐ)
    if max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {max_pages}")

    result_holder: list[HarvestResult] = [HarvestResult(
        products=[], pages_loaded=0, error="thread not started", success=False
    )]
    error_holder: list[Optional[Exception]] = [None]

    def _runner() -> None:
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            result_holder[0] = _harvest_product_list_impl(
                keyword=keyword,
                pattern=pattern,
                category_id=category_id,
                min_price=min_price,
                max_pages=max_pages,
                cdp_endpoint=cdp_endpoint,
                sleep_seconds=sleep_seconds,
            )
        except Exception as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # MEDIUM-3: timeout を max_pages 連動 (90s × max_pages) に変更.
    # 固定 180s は max_pages=2 前提だったが、より大きな max_pages で不十分になる.
    thread_timeout = 90 * max_pages
    t.join(timeout=thread_timeout)
    if t.is_alive():
        return HarvestResult(
            products=[], pages_loaded=0,
            error=f"harvest thread timeout (>{thread_timeout}s)", success=False,
        )
    if error_holder[0] is not None:
        return HarvestResult(
            products=[], pages_loaded=0,
            error=f"harvest exception: {error_holder[0]}", success=False,
        )
    return result_holder[0]


def _harvest_product_list_impl(
    keyword: str,
    pattern: str,
    *,
    category_id: Optional[int],
    min_price: int,
    max_pages: int,
    cdp_endpoint: str,
    sleep_seconds: float,
) -> HarvestResult:
    """harvest_product_list の実処理. 別 thread から呼ばれる."""
    import random as _random

    if sync_playwright is None:
        return HarvestResult(
            products=[], pages_loaded=0,
            error="playwright not installed", success=False,
        )

    # today_jst を事前に取得 (全ページで共通)
    # MEDIUM-2: ZoneInfo("Asia/Tokyo") → 固定 offset に置換 (Windows tzdata 不在リスク)
    today_jst = _dt.datetime.now(tz=_JST).date()

    all_products: list[HarvestedProduct] = []
    pages_loaded = 0

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            return HarvestResult(
                products=[], pages_loaded=0,
                error=f"CDP connect failed: {e}", success=False,
            )

        if not browser.contexts:
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return HarvestResult(
                products=[], pages_loaded=0,
                error="no browser context", success=False,
            )

        ctx = browser.contexts[0]
        new_tab = None

        try:
            new_tab = ctx.new_page()

            # HIGH-C: two_year_echo で target 日に到達したかどうかを追跡.
            # fresh_24h は全ページが有効範囲なので常に reached=True 扱い.
            # two_year_echo では all_before_target continue が続いてループ自然終了した場合、
            # 「正常 0 件」と「ページ予算切れ」を区別するために使う.
            target_date = _two_year_target(today_jst) if pattern == "two_year_echo" else None
            reached_target = (pattern != "two_year_echo")
            # MEDIUM-1: break 理由を保持して文言出し分けに使う。
            stop_reason = "max_pages"  # デフォルト (ループ自然終了)

            for page_idx in range(max_pages):
                # HIGH-B 修正 (2026-06-10): sleep をループ先頭に移動.
                # 旧実装はループ末尾 sleep だったため all_before_target / 日付なし の
                # continue 2 箇所が sleep をスキップし、anti-bot バイパスになっていた.
                if page_idx > 0:
                    jitter = _random.uniform(0.7, 1.5)
                    _time.sleep(sleep_seconds * jitter)

                offset = page_idx * 50
                now_ms = int(_time.time() * 1000)
                url = build_harvest_url(
                    keyword, pattern,
                    category_id=category_id,
                    min_price=min_price,
                    offset=offset,
                    now_ms=now_ms,
                )

                logger.info(
                    f"harvest navigate: page={page_idx+1}/{max_pages} "
                    f"offset={offset} url={url[:120]}"
                )

                try:
                    new_tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                except (PWTimeout, Exception) as e:
                    logger.warning(f"harvest goto timeout/error: {e}")
                    return HarvestResult(
                        products=all_products, pages_loaded=pages_loaded,
                        error=f"goto failed: {e}", success=False,
                    )

                # error redirect 検知 (anti-bot / rate limit)
                current_url = ""
                try:
                    current_url = new_tab.url
                except (PWTimeout, AttributeError) as e:
                    logger.debug(f"page.url 取得失敗 ({e})")
                if _is_ebay_error_redirect(current_url):
                    logger.error(
                        f"harvest: eBay error redirect 検知 ({current_url}). 即停止."
                    )
                    return HarvestResult(
                        products=all_products, pages_loaded=pages_loaded,
                        error=f"eBay error redirect: {current_url}", success=False,
                    )

                # tr.research-table-row 出現ポーリング
                appeared = _poll_harvest_rows(new_tab, timeout_s=30.0)
                if not appeared:
                    logger.warning(
                        f"harvest: page {page_idx+1} で行未出現 (timeout)"
                    )
                    # HIGH-2: page 1 から 0 行 = anti-bot / セッション切れ / DOM 変更の疑い.
                    # 「正常 0 件収穫」と区別するため success=False で返す (Q0 偽装成功防止).
                    if pages_loaded == 0:
                        return HarvestResult(
                            products=all_products, pages_loaded=0,
                            error="no rows appeared on page 1 (timeout)", success=False,
                        )
                    # page 2 以降は到達済データで終了 (success=True 可)
                    stop_reason = "poll_timeout"
                    break

                # live DOM 取得 (probe 確定: page.content() は SSR でテーブル無し)
                try:
                    html = new_tab.evaluate(
                        "() => document.documentElement.outerHTML"
                    )
                except (PWTimeout, Exception) as e:
                    logger.warning(f"harvest: outerHTML 取得失敗 ({e})")
                    return HarvestResult(
                        products=all_products, pages_loaded=pages_loaded,
                        error=f"outerHTML failed: {e}", success=False,
                    )

                # 実 dayRange 検証 (Q0 / _detect_actual_dayrange 流用)
                expected_dr = 7 if pattern == "fresh_24h" else 730
                actual_dr = _detect_actual_dayrange(html)
                if actual_dr is not None and actual_dr != expected_dr:
                    logger.error(
                        f"harvest: dayRange 不一致 actual={actual_dr} expected={expected_dr}"
                    )
                    return HarvestResult(
                        products=all_products, pages_loaded=pages_loaded,
                        error=(
                            f"dayRange mismatch: actual={actual_dr} "
                            f"expected={expected_dr}"
                        ),
                        success=False,
                    )

                rows = parse_harvest_rows(html)
                pages_loaded += 1

                if not rows:
                    # MEDIUM-D: page 1 で poll が行存在を確認済みなのに parse=0 の場合、
                    # selector drift (eBay が class 属性を追加した等) を疑い success=False.
                    # poll は querySelectorAll('tr.research-table-row') (class 含有判定)、
                    # parse は class="research-table-row" 完全一致 regex のため乖離しうる.
                    # page 2 以降は到達済データで break (従来どおり).
                    if pages_loaded == 1:
                        logger.error(
                            "harvest: page 1 で poll=True だが parse=0 行 "
                            "(selector drift の可能性)"
                        )
                        return HarvestResult(
                            products=all_products, pages_loaded=0,
                            error="rows present in DOM but parse yielded 0 (selector drift?)",
                            success=False,
                        )
                    logger.info(
                        f"harvest: page {page_idx+1} で 0 行 → ページ取得終了"
                    )
                    stop_reason = "no_rows"
                    break

                # 窓フィルタを適用
                filtered = filter_harvest_window(
                    rows, pattern, today_jst=today_jst
                )
                all_products.extend(filtered)

                # 打ち切り判定 (pattern によって方向が逆)
                #
                # fresh_24h (新着順 = 新しい→古い):
                #   filtered=0 → target 日より古いページに入った → 以降も古い → 打ち切り
                #
                # two_year_echo (古い順 = 古い→新しい):
                #   filtered=0 だけでは判断不十分. page 1 が buffer 日の行で埋まって
                #   いる場合は filtered=0 でも target 日はまだ先にある.
                #   行日付は Terapeak 表示軸 (US Pacific). target は JST 今日-2年の
                #   日付ラベル (同一カレンダー日付で比較するため整合 OK).
                #   判定ロジック:
                #     - ページ内の全行 date < target   → target はまだ先 → 続行 (スキップ)
                #     - ページ内に date > target の行  → target を過ぎた → filtered を回収して打ち切り
                #     - それ以外 (target 行あり)       → filtered を回収して続行 (max_pages 上限まで)
                if pattern == "fresh_24h":
                    if not filtered:
                        logger.info(
                            f"harvest: pattern=fresh_24h page {page_idx+1} "
                            f"で窓内 0 件 → 以降ページ打ち切り"
                        )
                        stop_reason = "window_empty"
                        break
                else:  # two_year_echo
                    # target_date はループ外で計算済み (HIGH-C / LOW 同時解消)
                    dated_rows = [r for r in rows if r.date_last_sold is not None]
                    if not dated_rows:
                        # 日付なし行のみ → target 不明、安全のため続行
                        logger.info(
                            f"harvest: two_year_echo page {page_idx+1} "
                            f"日付あり行なし → 続行"
                        )
                        continue  # noqa: PLC0116 — continue in for loop intentional
                    all_before_target = all(
                        r.date_last_sold < target_date for r in dated_rows
                    )
                    any_after_target = any(
                        r.date_last_sold > target_date for r in dated_rows
                    )
                    if all_before_target:
                        # target 日にまだ到達していない → スキップして次ページへ
                        skipped_dates = sorted(
                            {r.date_last_sold for r in dated_rows}
                        )
                        logger.info(
                            f"harvest: two_year_echo page {page_idx+1} "
                            f"全行 target({target_date}) 未満 "
                            f"(先頭={skipped_dates[0]}, 末尾={skipped_dates[-1]}) "
                            f"→ 次ページへ続行 (スキップ {len(dated_rows)} 行)"
                        )
                        # filtered=0 でも break しない (ここが旧バグの根源)
                        continue  # noqa: PLC0116
                    elif any_after_target:
                        # MEDIUM-2 修正 (2026-06-10): target 超過行が存在する場合、
                        # 回収後に即 break (旧実装は filtered=0 の時だけ break し、
                        # filtered>0 の時は余分な次ページを fetch していた).
                        # 昇順ソートでは target 超過行以降に target 行は出現しないため.
                        reached_target = True
                        stop_reason = "reached_target"
                        logger.info(
                            f"harvest: two_year_echo page {page_idx+1} "
                            f"target({target_date}) 超過行あり → 回収して即打ち切り"
                        )
                        break
                    else:
                        # target 行が存在する (all_before=False, any_after=False → ==target)
                        # MEDIUM-1 修正 (2026-06-10): 旧実装の第 3 分岐
                        # (target 行混在するが filtered=0 → break) は論理的に到達不能.
                        # target 行 (date == target_date) があれば filter_harvest_window が
                        # 必ず filtered に追加する。到達するとすれば date_last_sold=None 行が
                        # target 判定されるケースだが、dated_rows 計算で除外済みのため不可能.
                        # 防御的に: filtered=0 なら logger.error + success=False で返す
                        # (偽装成功防止 Q0). filtered>0 なら通常継続.
                        reached_target = True
                        if not filtered:
                            logger.error(
                                f"harvest: two_year_echo page {page_idx+1} "
                                f"target 行あり (all_before=False, any_after=False) "
                                f"だが filtered=0 — 内部矛盾 (None 除去漏れ?)"
                            )
                            return HarvestResult(
                                products=all_products, pages_loaded=pages_loaded,
                                error=(
                                    f"two_year_echo internal inconsistency: "
                                    f"target rows present but filtered=0 on page {page_idx+1}"
                                ),
                                success=False,
                            )

        except Exception as e:  # noqa: BLE001
            logger.error(f"harvest: 予期しない例外: {e}", exc_info=True)
            _close_tab_safe(new_tab)
            try:
                browser.close()
            except Exception as e2:
                logger.debug(f"browser close ignored: {e2}")
            return HarvestResult(
                products=all_products, pages_loaded=pages_loaded,
                error=f"unexpected error: {e}", success=False,
            )

        _close_tab_safe(new_tab)
        try:
            browser.close()
        except Exception as e:
            logger.debug(f"browser close ignored: {e}")

    # HIGH-C 修正 (2026-06-10): two_year_echo で max_pages 消費しても target 未到達の場合、
    # 「正常 0 件収穫の夜」と「ページ予算切れ」を区別するため success=False で返す (Q0).
    # MEDIUM-1: stop_reason 別に文言を出し分ける (判定ロジック・戻り値構造は不変)。
    if not reached_target and pages_loaded > 0:
        logger.warning(
            f"harvest: two_year_echo max_pages={max_pages} を消費したが "
            f"target {target_date} に到達せず (pages_loaded={pages_loaded}, "
            f"stop_reason={stop_reason})"
        )
        if stop_reason == "poll_timeout":
            err_msg = (
                f"poll timeout: 窓内 0 件が続き target {target_date} 未到達 "
                f"(pages_loaded={pages_loaded})"
            )
        elif stop_reason == "no_rows":
            err_msg = (
                f"no_rows: 行なしページに達し target {target_date} 未到達 "
                f"(pages_loaded={pages_loaded})"
            )
        else:
            err_msg = (
                f"max_pages={max_pages} exhausted before reaching target {target_date}"
            )
        return HarvestResult(
            products=all_products,
            pages_loaded=pages_loaded,
            error=err_msg,
            success=False,
        )

    return HarvestResult(
        products=all_products,
        pages_loaded=pages_loaded,
        error=None,
        success=True,
    )


def _close_tab_safe(page: object) -> None:
    """新規タブを安全に閉じる."""
    if page is None:
        return
    try:
        page.close()  # type: ignore[union-attr]
    except Exception as e:
        logger.debug(f"tab close ignored: {e}")


# ---------------------------------------------------------------------------
# W229 商品詳細取得 (Phase 2 / scrape_product_detail)
# 既存関数は一切変更しない (K2). 以下は全て新規追加.
# ---------------------------------------------------------------------------

import dataclasses as _dc


@_dc.dataclass
class ProductGateData:
    """scrape_product_detail の戻り値.

    evaluate_sourcing_gate に渡す全入力値 + 補助情報を格納する.
    """
    keyword: str
    sold_90d: int = 0
    has_active_listing: bool = False
    listing_start_date: "str | None" = None   # "YYYY-MM" 形式 (_parse_listing_start_date 互換)
    sold_1_2yr: int = 0
    avg_sold_price_usd: "float | None" = None
    # 依頼ボード#23 (2026-06-15): 全世界グラット除外用シグナル。
    # -1 = 未チェック (target_oos_watch 予定の候補のみ取得)。
    # worldwide_active_count: sellerCountry フィルタ無しの ACTIVE 出品行数
    #   (JP active=0 が前提なので >0 は非日本セラーが出品中の意)。
    # worldwide_sold_90d: sellerCountry フィルタ無しの直近 90 日 sold 件数。
    # 「全世界で出ているのに売れていない (worldwide_active>0 かつ worldwide_sold_90d=0)」
    # = 需要消失 (死に筋) を gate で reject_global_glut に落とす根拠。
    worldwide_active_count: int = -1
    worldwide_sold_90d: int = -1
    success: bool = False
    error: "str | None" = None
    # H-2: 実際に消費した navigate 回数 (クォータ合算に使用).
    # Q6 skip 時 = 1, フル経路 = 3, 途中失敗 = その時点まで.
    navigates_used: int = 0


def _extract_sold_count(html: str, day_range: int) -> int:  # noqa: ARG001
    """SOLD タブ HTML から research-table-row の行数を返す (sold 件数の proxy).

    業務根拠 (設計書 §5-1):
      ゲート閾値は sold_90d >= 2 (2 件以上) という粗い基準のため、
      「行数 = sold 件数の proxy」で十分。Total sold メトリクスの
      厳密抽出に固執しない (K1)。

    Args:
        html: SOLD タブの live DOM HTML
        day_range: 期間 (90 or 730)。現実装では行数カウントのみで未使用だが、
                   シグネチャを統一して caller で day_range を引き渡す (K0 透明性)。

    Returns:
        int: tr.research-table-row の行数 (0 以上)
    """
    rows = re.findall(r'<tr[^>]*class="research-table-row"', html)
    return len(rows)


def _extract_avg_sold_price(html: str) -> "float | None":
    """SOLD タブ HTML から最初の avgSoldPrice を抽出する補助関数.

    probe7_sold90.html で確認したセレクタ:
      td class="...research-table-row__avgSoldPrice"
      > div.research-table-row__item-with-subtitle > div > "$102.89"

    Args:
        html: SOLD タブの live DOM HTML

    Returns:
        float or None: 最初の行の平均売却価格 (USD)。抽出不可なら None。
    """
    pat = re.compile(
        r'class="research-table-row__item research-table-row__avgSoldPrice">(.*?)</td>',
        re.DOTALL,
    )
    m = pat.search(html)
    if not m:
        return None
    inner = re.search(r'<div>([^<]+)</div>', m.group(1))
    if not inner:
        return None
    raw = inner.group(1).strip()
    price_m = re.search(r'\$([\d,.]+)', raw)
    if not price_m:
        return None
    try:
        return float(price_m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_active_listing_start_dates(html: str) -> "list[_dt.date]":
    """ACTIVE タブ HTML から出品開始日を抽出する純関数.

    probe7_active.html で確認したセレクタ:
      td class="active-listing-row__item active-listing-row__startedDate"
      > div.active-listing-row__inner-item > div > "Feb 17, 2024"

    Args:
        html: ACTIVE タブの live DOM HTML

    Returns:
        list[date]: 有効な日付のリスト (パース不能はスキップ)。空の場合あり。
    """
    pat = re.compile(
        r'class="active-listing-row__item active-listing-row__startedDate">(.*?)</td>',
        re.DOTALL,
    )
    dates: list[_dt.date] = []
    for m in pat.finditer(html):
        inner = re.search(r'<div>([^<]+)</div>', m.group(1))
        if not inner:
            continue
        text = inner.group(1).strip()
        dt = _parse_terapeak_date(text)
        if dt is not None:
            dates.append(dt.date())
    return dates


def _poll_active_rows(page: object, timeout_s: float = 30.0) -> bool:
    """ACTIVE タブの active-listing-row が DOM に出現するまでポーリング.

    SOLD タブは tr.research-table-row を待つが、ACTIVE タブは
    tr.active-listing-row (probe7_active.html 確認) を待つ。

    Args:
        page: Playwright page オブジェクト
        timeout_s: タイムアウト秒

    Returns:
        True なら出現、False はタイムアウト
    """
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            count = page.evaluate(
                "() => document.querySelectorAll('tr.active-listing-row').length"
            )
            if count and count > 0:
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"_poll_active_rows: evaluate 失敗 ({e})")
        _time.sleep(1.0)
    return False


def _scrape_worldwide_glut_signals(
    page: object, keyword: str, navs_used: int, sleep_seconds: float,
) -> "tuple[int, int, int]":
    """依頼ボード#23 (2026-06-15): 全世界 (sellerCountry フィルタ無し) の
    ACTIVE 出品行数 / 直近 90 日 sold 件数を追加取得する。

    呼出条件 (scrape_product_detail 側): JP active=0 かつ JP sold_1_2yr>=2
    (= target_oos_watch 予定の候補) のみ。非日本セラーが出品しているのに
    全世界でも売れていない『需要消失 (死に筋)』を gate で除外するための根拠。

    Q0 (サイレントスキップ防止 / 捏造しない): scrape 失敗時は (-1, -1) を返し
    「未チェック」を明示する。gate は -1 を「判定材料なし」として扱い、
    保守的に target_oos_watch を維持する (誤除外しない)。

    Returns:
        (worldwide_active_count, worldwide_sold_90d, navs_used)
        失敗時は active/sold が -1。navs_used は実消費分を加算して返す。
    """
    ww_active = -1
    ww_sold_90d = -1
    try:
        # ── 全世界 ACTIVE 出品行数 ──
        jitter = _random.uniform(0.7, 1.5)
        _time.sleep(sleep_seconds * jitter)
        now_ms_a = int(_time.time() * 1000)
        url_ww_active = _build_terapeak_search_url(
            keyword, 90, now_ms=now_ms_a, seller_jp=False,
        ).replace("tabName=SOLD", "tabName=ACTIVE")
        logger.info(
            f"_scrape_worldwide_glut_signals: navigate WW ACTIVE "
            f"url={url_ww_active[:120]}"
        )
        page.goto(url_ww_active, wait_until="domcontentloaded", timeout=30000)
        navs_used += 1
        if _is_ebay_error_redirect(getattr(page, "url", "") or ""):
            logger.warning("_scrape_worldwide_glut_signals: WW ACTIVE error redirect")
            return (-1, -1, navs_used)
        appeared = _poll_active_rows(page, timeout_s=30.0)
        if not appeared:
            _time.sleep(2.0)
        try:
            ww_active = int(page.evaluate(
                "() => document.querySelectorAll('tr.active-listing-row').length"
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"_scrape_worldwide_glut_signals: WW ACTIVE count 失敗 ({e})")
            return (-1, -1, navs_used)

        # ── 全世界 SOLD 直近 90 日 ──
        jitter = _random.uniform(0.7, 1.5)
        _time.sleep(sleep_seconds * jitter)
        now_ms_s = int(_time.time() * 1000)
        url_ww_sold = _build_terapeak_search_url(
            keyword, 90, now_ms=now_ms_s, seller_jp=False,
        )
        logger.info(
            f"_scrape_worldwide_glut_signals: navigate WW SOLD 90d "
            f"url={url_ww_sold[:120]}"
        )
        page.goto(url_ww_sold, wait_until="domcontentloaded", timeout=30000)
        navs_used += 1
        if _is_ebay_error_redirect(getattr(page, "url", "") or ""):
            logger.warning("_scrape_worldwide_glut_signals: WW SOLD error redirect")
            return (ww_active, -1, navs_used)
        appeared_s = _poll_harvest_rows(page, timeout_s=30.0)
        if not appeared_s:
            _time.sleep(2.0)
        try:
            html_ww = page.evaluate("() => document.documentElement.outerHTML")
            ww_sold_90d = _extract_sold_count(html_ww, 90)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"_scrape_worldwide_glut_signals: WW SOLD parse 失敗 ({e})")
            return (ww_active, -1, navs_used)

        logger.info(
            f"_scrape_worldwide_glut_signals: WW active={ww_active} "
            f"sold_90d={ww_sold_90d}"
        )
        return (ww_active, ww_sold_90d, navs_used)
    except (PWTimeout, Exception) as e:  # noqa: BLE001
        logger.warning(f"_scrape_worldwide_glut_signals: 失敗 ({e})")
        return (ww_active, ww_sold_90d, navs_used)


def scrape_product_detail(
    keyword: str,
    *,
    cdp_endpoint: str = CDP_ENDPOINT,
    sleep_seconds: float = 3.0,
) -> ProductGateData:
    """1 商品の詳細データを Terapeak から取得し ProductGateData を返す.

    処理フロー (設計書 §5-1):
      1. SOLD dayRange=90 ページを load → sold_90d を行数で計算 + avg_sold_price_usd 取得
      2. Q6 最適化: sold_90d >= 2 なら gate は target_instock 確定 → 即 return (1 navigate のみ)
      3. sold_90d < 2 の場合のみ:
         a. ACTIVE タブ load → has_active_listing, listing_start_date (最古)
         b. SOLD dayRange=730 load → c730 行数カウント
         → sold_1_2yr = max(0, c730 - sold_90d) [proxy 方式: 設計書で設計判断済み]

    proxy 方式の注記:
      1〜2年厳密窓は CUSTOM 過去窓未検証のため count(730d) − count(90d) で代替する。
      730d 期間全体の sold 数から 90d 分を引いた残りを「1〜2年分」の proxy とする。
      厳密には「91〜730日前」を指すが、ゲート判定 (sold_1_2yr >= 2) の粗い閾値に対して
      proxy の誤差は許容範囲 (K1 Simplicity)。

    Args:
        keyword: 商品キーワード (_build_terapeak_search_url に渡す)
        cdp_endpoint: CDP エンドポイント (デフォルト localhost:9222)
        sleep_seconds: navigate 間 sleep 秒 (jitter 0.7-1.5 倍を適用)

    Returns:
        ProductGateData: success=True なら取得成功、False なら error に理由を格納
    """
    result_holder: "list[ProductGateData | None]" = [None]
    error_holder: "list[Exception | None]" = [None]

    def _runner() -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        try:
            result_holder[0] = _scrape_product_detail_impl(
                keyword,
                cdp_endpoint=cdp_endpoint,
                sleep_seconds=sleep_seconds,
            )
        except Exception as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # thread join timeout。CDP hang で 03:30 バッチが無期限待ちになるのを防ぐ hang-guard。
    # 依頼ボード#23 (2026-06-15): target_oos_watch 予定候補のみ全世界 active/sold の
    # 追加 2 navigate が走る (_scrape_worldwide_glut_signals)。基本経路 3 nav (~80s 実測)
    # + 全世界 2 nav (各最大 ~40s) = worst case ~200s。旧 120s だと oos 候補で全体が
    # timeout=success=False に倒れ、本来 target_oos_watch / reject_global_glut にすべき
    # 候補が needs_review に落ちる改悪になるため、上限を 240s へ拡張 (hang-guard は維持)。
    _SCRAPE_JOIN_TIMEOUT_S = 240
    t.join(timeout=_SCRAPE_JOIN_TIMEOUT_S)
    if t.is_alive():
        logger.error(
            "scrape_product_detail: thread join timeout (%ss)", _SCRAPE_JOIN_TIMEOUT_S
        )
        return ProductGateData(
            keyword=keyword,
            success=False,
            error=f"thread join timeout ({_SCRAPE_JOIN_TIMEOUT_S}s)",
        )

    if error_holder[0] is not None:
        logger.error(f"scrape_product_detail: 例外 ({error_holder[0]})", exc_info=error_holder[0])
        return ProductGateData(
            keyword=keyword,
            success=False,
            error=f"thread exception: {error_holder[0]}",
        )
    if result_holder[0] is None:
        return ProductGateData(
            keyword=keyword,
            success=False,
            error="no result (thread returned None)",
        )
    return result_holder[0]


def _scrape_product_detail_impl(
    keyword: str,
    *,
    cdp_endpoint: str,
    sleep_seconds: float,
) -> ProductGateData:
    """scrape_product_detail の実処理. 別 thread から呼ばれる."""
    import random as _random

    if sync_playwright is None:
        return ProductGateData(
            keyword=keyword,
            success=False,
            error="playwright not installed",
        )

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            return ProductGateData(
                keyword=keyword,
                success=False,
                error=f"CDP connect failed: {e}",
            )

        if not browser.contexts:
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
            return ProductGateData(
                keyword=keyword,
                success=False,
                error="no browser context",
            )

        ctx = browser.contexts[0]
        new_tab = None
        _navs_used = 0  # try/except 両方から参照できるよう try 外で初期化

        try:
            new_tab = ctx.new_page()
            now_ms = int(_time.time() * 1000)
            # H-2: 実 navigate 回数をカウントして戻り値に乗せる。

            # ---- Step 1: SOLD dayRange=90 ----
            url_90 = _build_terapeak_search_url(keyword, 90, now_ms=now_ms)
            logger.info(f"scrape_product_detail: navigate SOLD 90d url={url_90[:120]}")

            try:
                new_tab.goto(url_90, wait_until="domcontentloaded", timeout=30000)
            except (PWTimeout, Exception) as e:
                return ProductGateData(
                    keyword=keyword,
                    success=False,
                    error=f"goto SOLD 90d failed: {e}",
                    navigates_used=_navs_used,
                )
            _navs_used += 1

            # error redirect 検知
            try:
                current_url = new_tab.url
            except (PWTimeout, AttributeError):
                current_url = ""
            if _is_ebay_error_redirect(current_url):
                logger.error(f"scrape_product_detail: eBay error redirect ({current_url})")
                return ProductGateData(
                    keyword=keyword,
                    success=False,
                    error=f"eBay error redirect on SOLD 90d: {current_url}",
                    navigates_used=_navs_used,
                )

            # tr.research-table-row 出現ポーリング (timeout は正常 0 件と区別)
            appeared = _poll_harvest_rows(new_tab, timeout_s=30.0)
            if not appeared:
                # 0 行の場合: ポーリング timeout = 行未出現
                # ただし「正常 0 件 (売れていない)」と「DOM 未出現 (timeout)」を区別するため、
                # 短時間追加待ちの後 outerHTML を確認し research-table-row が 1 件もなければ
                # 「0 件正常」として扱う (anti-bot による page load 失敗と区別できないが、
                # 短 sleep 後に DOM を取れた = SPA が render 済 → 0 件が確定)
                _time.sleep(2.0)
                try:
                    html_90 = new_tab.evaluate(
                        "() => document.documentElement.outerHTML"
                    )
                except (PWTimeout, Exception) as e:
                    return ProductGateData(
                        keyword=keyword,
                        success=False,
                        error=f"outerHTML failed on SOLD 90d (poll timeout): {e}",
                        navigates_used=_navs_used,
                    )
                sold_90d = _extract_sold_count(html_90, 90)
                # sold_90d が 0 なら timeout でなく正常 0 件と判定
                if sold_90d > 0:
                    # poll が False なのに行がある = poll selector ずれの可能性
                    logger.warning(
                        f"scrape_product_detail: poll=False だが parse={sold_90d} 行 "
                        f"(selector drift 可能性, 続行)"
                    )
            else:
                try:
                    html_90 = new_tab.evaluate(
                        "() => document.documentElement.outerHTML"
                    )
                except (PWTimeout, Exception) as e:
                    return ProductGateData(
                        keyword=keyword,
                        success=False,
                        error=f"outerHTML failed on SOLD 90d: {e}",
                        navigates_used=_navs_used,
                    )
                sold_90d = _extract_sold_count(html_90, 90)

            avg_price = _extract_avg_sold_price(html_90)
            logger.info(
                f"scrape_product_detail: SOLD 90d sold_90d={sold_90d} "
                f"avg_price={avg_price}"
            )

            # ---- Q6 最適化: sold_90d >= 2 で gate 確定 → 即 return ----
            if sold_90d >= 2:
                logger.info(
                    f"scrape_product_detail: sold_90d={sold_90d} >= 2 → "
                    f"target_instock 確定、ACTIVE/730d navigate スキップ"
                )
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    has_active_listing=False,      # gate は sold_90d で確定のため不問
                    listing_start_date=None,
                    sold_1_2yr=0,
                    avg_sold_price_usd=avg_price,
                    success=True,
                    error=None,
                    navigates_used=_navs_used,
                )

            # ---- Step 3a: ACTIVE タブ ----
            jitter = _random.uniform(0.7, 1.5)
            _time.sleep(sleep_seconds * jitter)

            url_active = url_90.replace("tabName=SOLD", "tabName=ACTIVE")
            logger.info(
                f"scrape_product_detail: navigate ACTIVE url={url_active[:120]}"
            )

            try:
                new_tab.goto(url_active, wait_until="domcontentloaded", timeout=30000)
            except (PWTimeout, Exception) as e:
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"goto ACTIVE failed: {e}",
                    navigates_used=_navs_used,
                )
            _navs_used += 1

            try:
                current_url = new_tab.url
            except (PWTimeout, AttributeError):
                current_url = ""
            if _is_ebay_error_redirect(current_url):
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"eBay error redirect on ACTIVE: {current_url}",
                    navigates_used=_navs_used,
                )

            appeared_active = _poll_active_rows(new_tab, timeout_s=30.0)
            if not appeared_active:
                _time.sleep(2.0)

            try:
                html_active = new_tab.evaluate(
                    "() => document.documentElement.outerHTML"
                )
            except (PWTimeout, Exception) as e:
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"outerHTML failed on ACTIVE: {e}",
                    navigates_used=_navs_used,
                )

            active_dates = _extract_active_listing_start_dates(html_active)
            # H-4: 行の存在と parse 結果を分離する。
            # _poll_active_rows=True (行あり) でも _extract_active_listing_start_dates=[]
            # (全パース失敗) の場合は has_active_listing=True, listing_start_date=None で返す。
            # gate は branch 3 の skip_too_new に保守的に落ちる。
            if appeared_active and not active_dates:
                logger.warning(
                    "scrape_product_detail: ACTIVE 行は存在するが全パース失敗 "
                    "(has_active_listing=True, listing_start_date=None で保守的に扱う)"
                )
                has_active_listing = True
                listing_start_date: "str | None" = None
            else:
                has_active_listing = len(active_dates) > 0
                listing_start_date = None
                if active_dates:
                    oldest = min(active_dates)
                    listing_start_date = f"{oldest.year:04d}-{oldest.month:02d}"

            logger.info(
                f"scrape_product_detail: ACTIVE has_active={has_active_listing} "
                f"dates_count={len(active_dates)} oldest={listing_start_date}"
            )

            # ---- Step 3b: SOLD dayRange=730 ----
            jitter = _random.uniform(0.7, 1.5)
            _time.sleep(sleep_seconds * jitter)

            now_ms2 = int(_time.time() * 1000)
            url_730 = _build_terapeak_search_url(keyword, 730, now_ms=now_ms2)
            logger.info(
                f"scrape_product_detail: navigate SOLD 730d url={url_730[:120]}"
            )

            try:
                new_tab.goto(url_730, wait_until="domcontentloaded", timeout=30000)
            except (PWTimeout, Exception) as e:
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    has_active_listing=has_active_listing,
                    listing_start_date=listing_start_date,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"goto SOLD 730d failed: {e}",
                    navigates_used=_navs_used,
                )
            _navs_used += 1

            try:
                current_url = new_tab.url
            except (PWTimeout, AttributeError):
                current_url = ""
            if _is_ebay_error_redirect(current_url):
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    has_active_listing=has_active_listing,
                    listing_start_date=listing_start_date,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"eBay error redirect on SOLD 730d: {current_url}",
                    navigates_used=_navs_used,
                )

            appeared_730 = _poll_harvest_rows(new_tab, timeout_s=30.0)
            if not appeared_730:
                _time.sleep(2.0)

            try:
                html_730 = new_tab.evaluate(
                    "() => document.documentElement.outerHTML"
                )
            except (PWTimeout, Exception) as e:
                return ProductGateData(
                    keyword=keyword,
                    sold_90d=sold_90d,
                    has_active_listing=has_active_listing,
                    listing_start_date=listing_start_date,
                    avg_sold_price_usd=avg_price,
                    success=False,
                    error=f"outerHTML failed on SOLD 730d: {e}",
                    navigates_used=_navs_used,
                )

            c730 = _extract_sold_count(html_730, 730)
            # proxy 方式: sold_1_2yr = count(730d) - count(90d)
            # 負にならないよう max(0, ...) でクランプ
            sold_1_2yr = max(0, c730 - sold_90d)
            logger.info(
                f"scrape_product_detail: SOLD 730d c730={c730} "
                f"sold_1_2yr={sold_1_2yr}"
            )

            # ── 依頼ボード#23 (2026-06-15): 全世界グラット除外シグナル ──
            # JP active=0 かつ JP sold_1_2yr>=2 (= target_oos_watch 予定) の候補のみ、
            # 全世界 (sellerCountry 無し) の active 出品 / 直近90日 sold を追加取得。
            # 非日本セラーが出品しているのに全世界でも売れていない品 (需要消失) を
            # gate 側で reject_global_glut に落とすため。条件外候補には navigate しない
            # (anti-bot / クォータ節約)。失敗時は (-1,-1) = 未チェックで保守的に維持。
            ww_active_count = -1
            ww_sold_90d = -1
            if (not has_active_listing) and sold_1_2yr >= 2:
                ww_active_count, ww_sold_90d, _navs_used = (
                    _scrape_worldwide_glut_signals(
                        new_tab, keyword, _navs_used, sleep_seconds,
                    )
                )

            return ProductGateData(
                keyword=keyword,
                sold_90d=sold_90d,
                has_active_listing=has_active_listing,
                listing_start_date=listing_start_date,
                sold_1_2yr=sold_1_2yr,
                avg_sold_price_usd=avg_price,
                worldwide_active_count=ww_active_count,
                worldwide_sold_90d=ww_sold_90d,
                success=True,
                error=None,
                navigates_used=_navs_used,
            )

        except Exception as e:  # noqa: BLE001
            logger.error(
                f"scrape_product_detail: 予期しない例外: {e}", exc_info=True
            )
            return ProductGateData(
                keyword=keyword,
                success=False,
                error=f"unexpected error: {e}",
                navigates_used=_navs_used,
            )
        finally:
            _close_tab_safe(new_tab)
            try:
                browser.close()
            except Exception as e:
                logger.debug(f"browser close ignored: {e}")
