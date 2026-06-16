"""W7-A terapeak scraper の regression test.

2026-04-28 停電後の 4 連続失敗事故由来. 以下を再発防止:
  - locale 依存の date parse (HIGH-4)
  - dropdown menu 残骸を active range と誤検出 (HIGH-3)
  - sanity_mismatch 時に主集計のみ None 化で補助メトリクス残存 (HIGH-A)
  - Seller Country pill が誤って close される回帰
"""
from __future__ import annotations

import locale
from datetime import datetime
from pathlib import Path

import pytest

from monitor.terapeak_scraper import (
    CONDITION_FILTER_LABELS,
    _build_terapeak_search_url,
    _detect_actual_dayrange,
    _extract_from_html,
    _extract_plugin_aggregation,
    _is_ebay_error_redirect,
    _parse_terapeak_date,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POC_HTML_PATH = PROJECT_ROOT / "data" / "terapeak_poc_cdp_stock_01.html"
EVENING_HTML_PATH = (
    PROJECT_ROOT / "data" / "scraper_debug"
    / "stock_01_v2_outerHTML_20260428_224733.html"
)


@pytest.fixture
def poc_success_html() -> str:
    """4/27 06:57 朝 PoC 成功時の HTML (Last 90 days, US 23/total 31)."""
    if not POC_HTML_PATH.exists():
        pytest.skip(f"PoC HTML not found: {POC_HTML_PATH}")
    return POC_HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture
def evening_30days_html() -> str:
    """4/28 22:47 失敗時の HTML (Last 30 days 残留, US 3/total 7)."""
    if not EVENING_HTML_PATH.exists():
        pytest.skip(f"evening HTML not found: {EVENING_HTML_PATH}")
    return EVENING_HTML_PATH.read_text(encoding="utf-8")


# ── _parse_terapeak_date: locale 非依存 ──────────────────────────────


def test_parse_terapeak_date_basic():
    assert _parse_terapeak_date("Mar 30, 2026") == datetime(2026, 3, 30)
    assert _parse_terapeak_date("Jan 1, 2026") == datetime(2026, 1, 1)
    assert _parse_terapeak_date("Dec 31, 2025") == datetime(2025, 12, 31)


def test_parse_terapeak_date_invalid_returns_none():
    assert _parse_terapeak_date("invalid") is None
    assert _parse_terapeak_date("XYZ 30, 2026") is None  # 月略記不明
    assert _parse_terapeak_date("") is None


def test_parse_terapeak_date_locale_independent():
    """JP locale 環境でも壊れないこと (datetime.strptime '%b' は locale 依存)."""
    saved = locale.setlocale(locale.LC_TIME)
    try:
        try:
            locale.setlocale(locale.LC_TIME, "Japanese_Japan.932")
        except locale.Error:
            pytest.skip("Japanese locale not available on this OS")
        # JP locale で Apr/May/Jun 等の英語略記は datetime.strptime("%b") では parse 不可
        assert _parse_terapeak_date("Apr 28, 2026") == datetime(2026, 4, 28)
    finally:
        locale.setlocale(locale.LC_TIME, saved)


# ── _detect_actual_dayrange: dropdown 残骸を誤検出しない ─────────────


def test_detect_actual_dayrange_from_poc(poc_success_html):
    """4/27 PoC HTML から 90 days を検出できること."""
    assert _detect_actual_dayrange(poc_success_html) == 90


def test_detect_actual_dayrange_evening_30days(evening_30days_html):
    """4/28 失敗 HTML は 30 days 検出 (=expected 90 と不一致 → 上位 error 化)."""
    assert _detect_actual_dayrange(evening_30days_html) == 30


def test_detect_actual_dayrange_picks_active_not_dropdown_residue():
    """dropdown menu に残った 30 days option があっても active = 90 を選ぶこと.

    朝の事故: results-header__left のスコープを設けず regex の最初一致を取った場合、
    dropdown menu の "Last 30 days" option text と同居する 1 ヶ月 range を誤検出する.
    """
    html = (
        '<li>Last 30 days option label</li>'
        '<span>Mar 1, 2026 - Apr 1, 2026</span>'  # dropdown option residue (30 days)
        '<div class="results-header__left">'
        '<span>Jan 28, 2026 - Apr 28, 2026</span>'  # active range (90 days)
        '</div>'
    )
    assert _detect_actual_dayrange(html) == 90


def test_detect_actual_dayrange_no_match_returns_none():
    assert _detect_actual_dayrange("<html>no date range</html>") is None


# ── プラグイン抽出 ──────────────────────────────────────────────────


def test_extract_plugin_aggregation_from_poc_morning(poc_success_html):
    """朝 PoC HTML はプラグイン未インストール時の HTML なので None."""
    assert _extract_plugin_aggregation(poc_success_html) is None


def test_extract_plugin_aggregation_from_evening(evening_30days_html):
    """4/28 22:47 はプラグイン挿入済 → US/非US の独立集計が取れる."""
    plugin = _extract_plugin_aggregation(evening_30days_html)
    assert plugin is not None
    assert plugin["us_count"] == 24
    assert plugin["non_us_count"] == 8
    assert plugin["total"] == 32
    breakdown_codes = {code for code, _, _ in plugin["breakdown"]}
    # AU, IE, PL, PT, CH, VN のいずれかが含まれているはず
    assert breakdown_codes.intersection({"AU", "IE", "PL", "PT", "CH", "VN"})


# ── _extract_from_html: sanity check 失敗時に全数値破棄 ────────────


def test_extract_sanity_mismatch_clears_all_metrics():
    """主集計とプラグイン集計が 10% 超乖離した場合、補助メトリクスも全 None 化されること.

    HIGH-A 検出用. avg_price 等が残ると UI 表示で歪んだ filter 状態の数値が
    user に提示される事故になる.
    """
    mock_html = """
    <div data="BuyerLocation:::US"><span class="filter-menu__text">United States (4)</span></div>
    <div data="BuyerLocation:::JP"><span class="filter-menu__text">Japan (2)</span></div>
    $53.51 Avg sold price
    $9.69 Avg shipping
    80% Sell-through
    5 Total sellers
    <input id="buyer-us-checkbox"><label><strong>24</strong></label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::JP"><label>Japan (5)</label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::DE"><label>Germany (3)</label>
    """
    out = _extract_from_html(mock_html)

    # 乖離検知済
    assert out.get("sanity_mismatch") is not None
    assert out["sanity_mismatch"]["main_total"] == 6
    assert out["sanity_mismatch"]["plugin_total"] == 32

    # 主集計は全て None
    assert out["us_count"] is None
    assert out["non_us_count"] is None
    assert out["total_sold"] is None
    # 補助メトリクスも全て None (HIGH-A 修正の要点)
    assert out["avg_sold_price_usd"] is None
    assert out["avg_shipping_usd"] is None
    assert out["sell_through_pct"] is None
    assert out["total_sellers"] is None


def test_extract_no_mismatch_keeps_metrics_when_consistent():
    """主集計とプラグインが一致する場合、数値はそのまま保持されること."""
    mock_html = """
    <div data="BuyerLocation:::US"><span class="filter-menu__text">United States (24)</span></div>
    <div data="BuyerLocation:::JP"><span class="filter-menu__text">Japan (8)</span></div>
    $53.51 Avg sold price
    <input id="buyer-us-checkbox"><label><strong>24</strong></label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::JP"><label>Japan (8)</label>
    """
    out = _extract_from_html(mock_html)
    assert out.get("sanity_mismatch") is None
    assert out["us_count"] == 24
    assert out["avg_sold_price_usd"] == 53.51


def test_extract_no_mismatch_when_us_ratio_agrees_despite_total_diff():
    """絶対値は乖離しても US 比率が一致する場合、sanity check は通過すること.

    2026-05-05 W7-A 失敗事例の retrofit:
      - 主集計 US=82/total=132 (62.1%)
      - プラグイン US=18/total=28 (64.3%) ← 部分 render
      - 絶対値乖離 79% (旧 logic では破棄) / US 比率乖離 2.2pp (新 logic では通過)
    business 判定 (primary_market) は US 比率 >=70% 等で行うため、比率が一致なら
    判定は正しい.
    """
    mock_html = """
    <div data="BuyerLocation:::US"><span class="filter-menu__text">United States (82)</span></div>
    <div data="BuyerLocation:::JP"><span class="filter-menu__text">Japan (50)</span></div>
    $40.00 Avg sold price
    <input id="buyer-us-checkbox"><label><strong>18</strong></label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::JP"><label>Japan (10)</label>
    """
    out = _extract_from_html(mock_html)
    # 旧 logic では破棄されていたが、US 比率が近い (62.1% vs 64.3%) ので通過
    assert out.get("sanity_mismatch") is None
    assert out["us_count"] == 82
    assert out["non_us_count"] == 50
    assert out["avg_sold_price_usd"] == 40.0


def test_extract_sanity_ratio_diff_unit_is_fraction():
    """sanity_mismatch dict の ratio_diff は 0.0-1.0 の fraction (= US 比率の小数差).

    HIGH-3 (2026-05-05 code-reviewer 指摘) の検証:
      - 旧 logic: 絶対値の相対差 (= max-norm の比、0.0-1.0)
      - 新 logic: US 比率の percentage point の小数 (例: 8.3pp = 0.083)
    どちらも 0.0-1.0 の fraction なので呼出側 format string は変更不要だが、
    意味が変わったので test で fix.
    """
    mock_html = """
    <div data="BuyerLocation:::US"><span class="filter-menu__text">United States (4)</span></div>
    <div data="BuyerLocation:::JP"><span class="filter-menu__text">Japan (2)</span></div>
    <input id="buyer-us-checkbox"><label><strong>24</strong></label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::JP"><label>Japan (5)</label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::DE"><label>Germany (3)</label>
    """
    out = _extract_from_html(mock_html)
    sm = out["sanity_mismatch"]
    # main: US 4/total 6 = 66.7%, plugin: US 24/total 32 = 75% → 8.3pp 差
    assert 0.0 <= sm["ratio_diff"] <= 1.0
    assert abs(sm["ratio_diff"] - 0.0833) < 0.001
    # 比率も 0.0-1.0 の小数
    assert abs(sm["main_us_ratio"] - 0.6667) < 0.001
    assert abs(sm["plugin_us_ratio"] - 0.75) < 0.001


def test_extract_skip_sanity_check_when_plugin_us_zero():
    """プラグインが US を抽出できない (us_count==0) 場合、sanity check skip.

    2026-05-05 W7-A 失敗事例の一部 HTML では plugin US 抽出が None になる
    (プラグインの部分 render or 拡張機能の動作不全). この場合は比較不能なので
    主 regex の値を信用して通過させる.
    """
    mock_html = """
    <div data="BuyerLocation:::US"><span class="filter-menu__text">United States (50)</span></div>
    <div data="BuyerLocation:::JP"><span class="filter-menu__text">Japan (30)</span></div>
    $30.00 Avg sold price
    <input class="buyer-country-checkbox" value="BuyerLocation:::JP"><label>Japan (5)</label>
    <input class="buyer-country-checkbox" value="BuyerLocation:::DE"><label>Germany (3)</label>
    """
    # 注: id="buyer-us-checkbox" が無い = プラグインの US 抽出が None
    out = _extract_from_html(mock_html)
    assert out.get("sanity_mismatch") is None
    assert out["us_count"] == 50
    assert out["non_us_count"] == 30


# ── _build_terapeak_search_url: URL 形式の regression 防止 ──────────


def test_build_terapeak_search_url_basic():
    """URL に必須クエリパラメータが全部含まれていること.

    回帰防止: dayRange だけだと eBay 内部 state が 30 days default に fall back する.
    startDate/endDate を ms timestamp で必ず併記しないと _detect_actual_dayrange で
    error 化 (2026-05-05 W7-A 検証由来).
    """
    from urllib.parse import urlparse, parse_qs

    fixed_now = 1_777_929_324_476  # 固定 ms timestamp (再現性のため)
    url = _build_terapeak_search_url("maxell MXCP-P100", day_range=90, now_ms=fixed_now)

    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.ebay.com"
    assert parsed.path == "/sh/research"

    qs = parse_qs(parsed.query)
    assert qs["marketplace"] == ["EBAY-US"]
    assert qs["keywords"] == ["maxell MXCP-P100"]
    assert qs["dayRange"] == ["90"]
    assert qs["endDate"] == [str(fixed_now)]
    expected_start = fixed_now - 90 * 24 * 3600 * 1000
    assert qs["startDate"] == [str(expected_start)]
    assert qs["tabName"] == ["SOLD"]
    assert qs["categoryId"] == ["0"]


def test_build_terapeak_search_url_seller_jp_default_includes_jp():
    """既定 (seller_jp=True) は sellerCountry=JP を含む (日本セラー基準)。"""
    from urllib.parse import urlparse, parse_qs

    url = _build_terapeak_search_url("foo", day_range=90, now_ms=1_777_929_324_476)
    qs = parse_qs(urlparse(url).query)
    assert "sellerCountry" in qs
    assert "JP" in qs["sellerCountry"][0]


def test_build_terapeak_search_url_seller_jp_false_omits_jp():
    """依頼ボード#23: seller_jp=False は sellerCountry を付けない (全世界集計)。"""
    from urllib.parse import urlparse, parse_qs

    url = _build_terapeak_search_url(
        "foo", day_range=90, now_ms=1_777_929_324_476, seller_jp=False
    )
    qs = parse_qs(urlparse(url).query)
    assert "sellerCountry" not in qs
    # 他の必須パラメータは維持
    assert qs["tabName"] == ["SOLD"]
    assert qs["dayRange"] == ["90"]


def test_build_terapeak_search_url_keyword_url_encoding():
    """空白 / 特殊文字を含む keyword が正しく URL encoding されること."""
    from urllib.parse import urlparse, parse_qs

    url = _build_terapeak_search_url(
        "Le Creuset Mini Cocotte 0.6L", day_range=90, now_ms=1_777_929_324_476
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["keywords"] == ["Le Creuset Mini Cocotte 0.6L"]
    # 生 URL に非エンコード空白が無いこと (URL parse で復元できれば encoding 正常)
    assert " Le " not in url
    assert "Le%20Creuset" in url or "Le+Creuset" in url


def test_build_terapeak_search_url_dayrange_30():
    """day_range=30 のとき startDate-endDate 差が 30 日 ms と一致."""
    from urllib.parse import urlparse, parse_qs

    fixed_now = 1_777_929_324_476
    url = _build_terapeak_search_url("foo", day_range=30, now_ms=fixed_now)
    qs = parse_qs(urlparse(url).query)
    diff_ms = int(qs["endDate"][0]) - int(qs["startDate"][0])
    assert diff_ms == 30 * 24 * 3600 * 1000
    assert qs["dayRange"] == ["30"]


def test_build_terapeak_search_url_uses_current_time_when_now_ms_omitted():
    """now_ms 省略時は time.time() ベースで現時刻を使う."""
    import time
    before = int(time.time() * 1000)
    url = _build_terapeak_search_url("foo", day_range=90)
    after = int(time.time() * 1000)
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    end = int(qs["endDate"][0])
    assert before <= end <= after  # 呼出間の time range 内に収まる


# ── _is_ebay_error_redirect: bot detection redirect 検知 ────────────


def test_is_ebay_error_redirect_n_error_path():
    """eBay の bot 検知 /n/error redirect を True で返すこと."""
    assert _is_ebay_error_redirect("https://www.ebay.com/n/error") is True
    assert _is_ebay_error_redirect("https://www.ebay.com/n/error?foo=bar") is True


def test_is_ebay_error_redirect_errors_path():
    """eBay の rate limit /errors redirect を True で返すこと."""
    assert _is_ebay_error_redirect("https://www.ebay.com/errors") is True
    assert _is_ebay_error_redirect("https://www.ebay.com/errors/page-not-found") is True


def test_is_ebay_error_redirect_normal_search_url_returns_false():
    """通常の Terapeak URL は False (false positive 防止)."""
    normal_url = (
        "https://www.ebay.com/sh/research?marketplace=EBAY-US"
        "&keywords=maxell+MXCP-P100&dayRange=90&tabName=SOLD"
    )
    assert _is_ebay_error_redirect(normal_url) is False


def test_is_ebay_error_redirect_keyword_containing_error_string_safe():
    """keyword に 'error' が含まれても false positive にならないこと.

    URL path で /n/error を検査するため、query string に 'error' があっても OK.
    """
    url = "https://www.ebay.com/sh/research?keywords=error+code+101&tabName=SOLD"
    assert _is_ebay_error_redirect(url) is False


# ── CONDITION_FILTER_LABELS の網羅性 ────────────────────────────────


def test_condition_filter_labels_covers_common_conditions():
    """eBay の主要 condition が漏れず登録されていること."""
    must_include = {
        "New", "Used", "Pre-owned", "Refurbished",
        "For parts or not working", "Open box",
    }
    assert must_include.issubset(CONDITION_FILTER_LABELS)


def test_condition_filter_labels_does_not_include_seller_country():
    """Seller Country (Japan) 等の必須 filter が誤って解除対象に入っていないこと."""
    must_not_include = {"Japan", "United States", "Germany", "Seller Country - Japan"}
    assert must_not_include.isdisjoint(CONDITION_FILTER_LABELS)


# ── 依頼ボード#23 (2026-06-15): _scrape_worldwide_glut_signals 回帰 ──────────
# code-reviewer HIGH-1 (2026-06-15): helper が module-level に無い _random を参照し
# NameError → broad except で (-1,-1) に倒れ glut 判定が silent 無効化していた。
# 純関数 gate テストでは helper を実行せず検出不能だったため、helper を直接呼ぶ
# テストで NameError 再発と navs 計上を固定する。
class _FakeTerapeakPage:
    """_scrape_worldwide_glut_signals 用の最小 fake page。"""

    def __init__(self, active_count: int, sold_html: str):
        self._active_count = active_count
        self._sold_html = sold_html
        self.url = "https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=ACTIVE"

    def goto(self, *a, **k):
        return None

    def evaluate(self, script: str):
        if "active-listing-row" in script:
            return self._active_count
        return self._sold_html  # outerHTML 相当


def _patch_polls(monkeypatch):
    from monitor import terapeak_scraper as ts
    monkeypatch.setattr(ts, "_poll_active_rows", lambda *a, **k: True)
    monkeypatch.setattr(ts, "_poll_harvest_rows", lambda *a, **k: True)
    monkeypatch.setattr(ts._time, "sleep", lambda *a, **k: None)
    return ts


def test_worldwide_glut_signals_no_nameerror_returns_counts(monkeypatch):
    """helper が NameError なく (active, sold, navs) を返すこと (HIGH-1 回帰)。"""
    ts = _patch_polls(monkeypatch)
    sold_html = '<tr class="research-table-row">x</tr>'  # 1 行
    page = _FakeTerapeakPage(active_count=12, sold_html=sold_html)
    ww_active, ww_sold, navs = ts._scrape_worldwide_glut_signals(
        page, "Test Keyword", navs_used=3, sleep_seconds=0.0,
    )
    assert ww_active == 12
    assert ww_sold == 1
    assert navs == 5  # 3 + ACTIVE 1 + SOLD 1


def test_worldwide_glut_signals_zero_sold_is_glut_input(monkeypatch):
    """全世界出品あり + 全世界 sold 0 → (12, 0) を返し gate の glut 入力になること。"""
    ts = _patch_polls(monkeypatch)
    page = _FakeTerapeakPage(active_count=12, sold_html="<div>no rows</div>")
    ww_active, ww_sold, navs = ts._scrape_worldwide_glut_signals(
        page, "Test Keyword", navs_used=3, sleep_seconds=0.0,
    )
    assert ww_active == 12
    assert ww_sold == 0
    # この (active>0, sold==0) が evaluate_sourcing_gate で reject_global_glut を導く
    from monitor.research_gate import evaluate_sourcing_gate, DECISION_REJECT_GLOBAL_GLUT
    decision, _ = evaluate_sourcing_gate(
        sold_90d=0, has_active_listing=False, sold_1_2yr=2,
        worldwide_active_count=ww_active, worldwide_sold_90d=ww_sold,
    )
    assert decision == DECISION_REJECT_GLOBAL_GLUT
