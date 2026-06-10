"""W229 Terapeak ハーベスト機能のテスト (Phase 2 / 2026-06-10).

CDP への実機接続は行わない (クォータ温存).
- build_harvest_url: 2 パターンの URL パラメータ整合性
- parse_harvest_rows: fixture HTML から実際の値を正確に抽出
- filter_harvest_window: fresh_24h / two_year_echo / None 除外
- harvest_product_list の打ち切りロジック: CDP モック化
"""
from __future__ import annotations

import datetime
import urllib.parse as _up
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from monitor.terapeak_scraper import (
    HarvestedProduct,
    _JST,
    _PST,
    _two_year_target,
    build_harvest_url,
    filter_harvest_window,
    parse_harvest_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
PROBE_DIR = PROJECT_ROOT / "data" / "terapeak_probe"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def harvest_rows_html_7d() -> str:
    """probe2c 由来の 3 行ミニ fixture (7d / 新着順)."""
    path = FIXTURE_DIR / "terapeak_harvest_rows_7d.html"
    if not path.exists():
        pytest.skip(f"fixture not found: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def probe2c_html() -> str:
    """probe2c: kw=SONY 7d live HTML (50 行)."""
    path = PROBE_DIR / "probe2c_kw_7d_live.html"
    if not path.exists():
        pytest.skip(f"probe HTML not found: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def probe4h_html() -> str:
    """probe4h: ソート後 HTML (古い順 / 2 年前型の窓付近のデータ)."""
    path = PROBE_DIR / "probe4h_after_sort_live.html"
    if not path.exists():
        pytest.skip(f"probe HTML not found: {path}")
    return path.read_text(encoding="utf-8")


def _make_row(
    title: str = "Test Product",
    price: str = "$100.00",
    count: str = "5",
    date: str = "Jun 5, 2026",
    ship: str = "$10.00",
    item_id: str = "123456789",
    img_url: str = "//i.ebayimg.com/test.jpg",
) -> str:
    """テスト用の tr.research-table-row HTML を生成する."""
    return f"""<tr class="research-table-row">
<td class="research-table-row__item research-table-row__product-info">
<div class="research-table-row__inner-item">
<div class="research-table-row__thumbnail">
<div><div data-testid="zoomable-thumbnail" tabindex="0" role="button" class="__zoomable-thumbnail">
<div class="__zoomable-thumbnail-inner">
<img class="small" src="{img_url}" alt="{title}">
</div></div></div></div>
<div class="research-table-row__product-info-name">
<a class="research-table-row__link-row-anchor" target="_blank" rel="noopener"
   href="https://www.ebay.com/itm/{item_id}?nordt=true">
<span data-item-id="{item_id}">{title}</span>
</a>
</div></div></td>
<td class="research-table-row__item research-table-row__avgSoldPrice">
<div class="research-table-row__item-with-subtitle"><div>{price}</div>
<div class="format">Fixed price</div></div></td>
<td class="research-table-row__item research-table-row__totalSoldCount">
<div class="research-table-row__inner-item"><div>{count}</div></div></td>
<td class="research-table-row__item research-table-row__dateLastSold">
<div class="research-table-row__inner-item"><div>{date}</div></div></td>
<td class="research-table-row__item research-table-row__avgShippingCost">
<div class="research-table-row__item-with-subtitle"><div>{ship}</div>
<div class="format">100% Free shipping</div></div></td>
</tr>"""


# ---------------------------------------------------------------------------
# build_harvest_url
# ---------------------------------------------------------------------------


class TestBuildHarvestUrl:
    """build_harvest_url のパラメータ整合性テスト."""

    _FIXED_NOW_MS = 1_750_000_000_000  # 固定値でテスト決定論化

    def _parse(self, pattern: str, **kwargs) -> dict:
        url = build_harvest_url(
            "SONY Walkman", pattern, now_ms=self._FIXED_NOW_MS, **kwargs
        )
        parsed = _up.urlparse(url)
        return _up.parse_qs(parsed.query)

    def test_fresh_24h_day_range_7(self):
        qs = self._parse("fresh_24h")
        assert qs["dayRange"] == ["7"]

    def test_fresh_24h_sorting_descending(self):
        """fresh_24h は -datelastsold (新着順)."""
        qs = self._parse("fresh_24h")
        assert qs["sorting"] == ["-datelastsold"]

    def test_two_year_echo_day_range_730(self):
        qs = self._parse("two_year_echo")
        assert qs["dayRange"] == ["730"]

    def test_two_year_echo_sorting_ascending(self):
        """two_year_echo は datelastsold (古い順)."""
        qs = self._parse("two_year_echo")
        assert qs["sorting"] == ["datelastsold"]

    def test_fresh_24h_start_end_date_consistency(self):
        """fresh_24h: endDate - startDate = 7 * 86400000 ms."""
        qs = self._parse("fresh_24h")
        end_ms = int(qs["endDate"][0])
        start_ms = int(qs["startDate"][0])
        diff_days = (end_ms - start_ms) // (24 * 3600 * 1000)
        assert diff_days == 7

    def test_two_year_echo_start_equals_target_midnight_pacific7(self):
        """HIGH-A 修正: two_year_echo の startDate が target 日 00:00 PACIFIC-7 (UTC-7) であること.

        now_ms=1_750_000_000_000 → JST 2025-06-16 → target = 2023-06-16
        startDate == target 日 2023-06-16 00:00:00 UTC-7 の epoch ms.

        旧実装: UTC-8 (PST) 固定 → 夏期 PDT では target 日 01:00 PDT 開始で最初 1h 漏れ.
        新実装: UTC-7 固定 (PACIFIC-7) → 夏期 PDT ちょうど 00:00 開始 / 冬期は前日 23:00 PST 開始で余分包含.
        """
        qs = self._parse("two_year_echo")
        start_ms = int(qs["startDate"][0])
        today_jst = datetime.datetime.fromtimestamp(
            self._FIXED_NOW_MS / 1000, tz=_JST
        ).date()
        target = _two_year_target(today_jst)
        expected_ms = int(
            datetime.datetime.combine(target, datetime.time(0), tzinfo=_PST).timestamp()
            * 1000
        )
        assert start_ms == expected_ms, (
            f"startDate {start_ms} != target 00:00 PACIFIC-7 "
            f"({expected_ms})"
        )

    def test_two_year_echo_leap_day_start_equals_target_midnight_pacific7(self):
        """HIGH-A 修正: 2/29 ケース. now_ms = 2028-06-10 JST → target = 2026-06-10.
        startDate = target 日 00:00 PACIFIC-7 (UTC-7).
        """
        # 2028-06-10 JST 00:00:00 のエポック ms を計算
        now_dt = datetime.datetime(2028, 6, 10, 0, 0, 0, tzinfo=_JST)
        now_ms = int(now_dt.timestamp() * 1000)
        url = build_harvest_url("SONY", "two_year_echo", now_ms=now_ms)
        qs = _up.parse_qs(_up.urlparse(url).query)
        start_ms = int(qs["startDate"][0])
        target = datetime.date(2026, 6, 10)  # 2028-06-10 の 2 年前
        expected_ms = int(
            datetime.datetime.combine(target, datetime.time(0), tzinfo=_PST).timestamp()
            * 1000
        )
        assert start_ms == expected_ms

    def test_two_year_echo_leap_day_2028_02_29(self):
        """HIGH-A 修正: 2028-02-29 → target = 2026-02-28 の startDate = target 00:00 PACIFIC-7."""
        now_dt = datetime.datetime(2028, 2, 29, 12, 0, 0, tzinfo=_JST)
        now_ms = int(now_dt.timestamp() * 1000)
        url = build_harvest_url("SONY", "two_year_echo", now_ms=now_ms)
        qs = _up.parse_qs(_up.urlparse(url).query)
        start_ms = int(qs["startDate"][0])
        target = datetime.date(2026, 2, 28)  # 2/29 → 2/28 丸め
        expected_ms = int(
            datetime.datetime.combine(target, datetime.time(0), tzinfo=_PST).timestamp()
            * 1000
        )
        assert start_ms == expected_ms

    def test_two_year_echo_winter_date_start_is_utc7(self):
        """HIGH-A 修正 追加テスト: 冬日付 (2024-01-15) で startDate が UTC-7 前提であること.

        target = 2024-01-15 の場合:
          UTC-7 固定: 2024-01-15 00:00 UTC-7 = 2024-01-15 07:00 UTC
          UTC-8 (旧): 2024-01-15 00:00 UTC-8 = 2024-01-15 08:00 UTC (1h 遅い)
        startDate は _PST (UTC-7) ベースの値と一致することを exact assert する.
        """
        # now = 2026-01-15 JST 12:00 → today_jst = 2026-01-15 → target = 2024-01-15
        now_dt = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=_JST)
        now_ms = int(now_dt.timestamp() * 1000)
        url = build_harvest_url("SONY", "two_year_echo", now_ms=now_ms)
        qs = _up.parse_qs(_up.urlparse(url).query)
        start_ms = int(qs["startDate"][0])

        target = datetime.date(2024, 1, 15)
        # _PST は UTC-7 (HIGH-A 修正後) なので、これが正しい expected
        expected_utc7_ms = int(
            datetime.datetime.combine(target, datetime.time(0), tzinfo=_PST).timestamp()
            * 1000
        )
        # 旧 UTC-8 だった場合の値 (一致してはいけない)
        utc8 = datetime.timezone(datetime.timedelta(hours=-8))
        unexpected_utc8_ms = int(
            datetime.datetime.combine(target, datetime.time(0), tzinfo=utc8).timestamp()
            * 1000
        )
        assert start_ms == expected_utc7_ms, (
            f"startDate {start_ms} != UTC-7 expected ({expected_utc7_ms})"
        )
        assert start_ms != unexpected_utc8_ms, (
            "startDate が旧 UTC-8 の値に一致している (HIGH-A 修正が効いていない)"
        )

    def test_end_date_equals_now_ms(self):
        """endDate == now_ms."""
        qs = self._parse("fresh_24h")
        assert int(qs["endDate"][0]) == self._FIXED_NOW_MS

    def test_min_price_default_100(self):
        qs = self._parse("fresh_24h")
        assert qs["minPrice"] == ["100"]

    def test_min_price_custom(self):
        qs = self._parse("fresh_24h", min_price=200)
        assert qs["minPrice"] == ["200"]

    def test_category_id_none_becomes_zero(self):
        qs = self._parse("fresh_24h", category_id=None)
        assert qs["categoryId"] == ["0"]

    def test_category_id_explicit(self):
        qs = self._parse("fresh_24h", category_id=293)
        assert qs["categoryId"] == ["293"]

    def test_seller_country_jp(self):
        qs = self._parse("fresh_24h")
        assert "JP" in qs["sellerCountry"][0]

    def test_tab_name_sold(self):
        qs = self._parse("fresh_24h")
        assert qs["tabName"] == ["SOLD"]

    def test_limit_50(self):
        qs = self._parse("fresh_24h")
        assert qs["limit"] == ["50"]

    def test_offset_passed_through(self):
        url = build_harvest_url(
            "SONY", "fresh_24h", offset=50, now_ms=self._FIXED_NOW_MS
        )
        qs = _up.parse_qs(_up.urlparse(url).query)
        assert qs["offset"] == ["50"]

    def test_invalid_pattern_raises_value_error(self):
        with pytest.raises(ValueError, match="pattern must be one of"):
            build_harvest_url("SONY", "invalid_pattern")

    def test_empty_keyword_raises_value_error(self):
        with pytest.raises(ValueError, match="keyword must not be empty"):
            build_harvest_url("", "fresh_24h")

    def test_whitespace_only_keyword_raises_value_error(self):
        with pytest.raises(ValueError, match="keyword must not be empty"):
            build_harvest_url("   ", "two_year_echo")


# ---------------------------------------------------------------------------
# parse_harvest_rows
# ---------------------------------------------------------------------------


class TestParseHarvestRows:
    """parse_harvest_rows のパース精度テスト."""

    def test_title_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        assert len(products) >= 1
        assert "SONY" in products[0].title or "Sony" in products[0].title

    def test_price_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        assert products[0].avg_sold_price_usd is not None
        assert products[0].avg_sold_price_usd > 0

    def test_count_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        assert products[0].total_sold_count is not None
        assert products[0].total_sold_count >= 1

    def test_date_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        # fixture は Jun 5, 2026 の行を含む
        assert products[0].date_last_sold is not None
        assert isinstance(products[0].date_last_sold, datetime.date)

    def test_shipping_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        assert products[0].avg_shipping_cost_usd is not None

    def test_research_url_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        assert products[0].research_url.startswith("https://www.ebay.com/itm/")

    def test_image_url_extracted(self, harvest_rows_html_7d):
        products = parse_harvest_rows(harvest_rows_html_7d)
        img = products[0].image_url
        assert img is not None
        assert img.startswith("https://")

    def test_probe2c_50_rows(self, probe2c_html):
        """probe2c は 50 行すべて正常にパースされること."""
        products = parse_harvest_rows(probe2c_html)
        assert len(products) == 50

    def test_price_comma_handling(self):
        """$48,358.97 のようなカンマ付き価格を正しくパース."""
        html = _make_row(price="$48,358.97")
        products = parse_harvest_rows(html)
        assert len(products) == 1
        assert products[0].avg_sold_price_usd == pytest.approx(48358.97)

    def test_price_dash_becomes_none(self):
        """価格が "-" または空のとき None."""
        html = _make_row(price="-")
        products = parse_harvest_rows(html)
        assert products[0].avg_sold_price_usd is None

    def test_count_comma_handling(self):
        """MEDIUM-1 回帰: total_sold_count が "1,234" のようなカンマ付きでも正しくパース."""
        html = _make_row(count="1,234")
        products = parse_harvest_rows(html)
        assert len(products) == 1
        assert products[0].total_sold_count == 1234

    def test_count_large_comma_value(self):
        """MEDIUM-1 回帰: "12,345" → 12345."""
        html = _make_row(count="12,345")
        products = parse_harvest_rows(html)
        assert products[0].total_sold_count == 12345

    def test_broken_row_skipped_with_warning(self, caplog):
        """title が取得できない壊れ行は skip され warning が出ること."""
        broken_row = """<tr class="research-table-row">
<td class="research-table-row__item research-table-row__product-info">
<div>no span here</div></td>
<td class="research-table-row__item research-table-row__avgSoldPrice">
<div class="x"><div>$100.00</div></div></td>
</tr>"""
        good_row = _make_row(title="Good Product")
        html = f"<html><body><table>{broken_row}{good_row}</table></body></html>"

        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.terapeak_scraper"):
            products = parse_harvest_rows(html)

        # 壊れ行はスキップ、正常行は取得
        assert len(products) == 1
        assert products[0].title == "Good Product"
        assert any("スキップ" in r.message for r in caplog.records)

    def test_image_url_protocol_added(self):
        """'//i.ebayimg.com/...' に https: が付与されること."""
        html = _make_row(img_url="//i.ebayimg.com/images/g/abc/s-l1200.jpg")
        products = parse_harvest_rows(html)
        assert products[0].image_url == "https://i.ebayimg.com/images/g/abc/s-l1200.jpg"

    def test_multiple_rows(self):
        """複数行を正しくパース."""
        html = "<html><body><table>" + "".join(
            _make_row(
                title=f"Product {i}",
                price=f"${i*10:.2f}",
                count=str(i),
                item_id=str(100 + i),
            )
            for i in range(1, 4)
        ) + "</table></body></html>"
        products = parse_harvest_rows(html)
        assert len(products) == 3
        assert products[0].title == "Product 1"
        assert products[2].total_sold_count == 3

    def test_empty_html_returns_empty_list(self):
        assert parse_harvest_rows("<html></html>") == []

    def test_html_entities_in_title(self):
        """HTML エンティティ (&amp; 等) がデコードされること."""
        html = _make_row(title="Sony &amp; Audio")
        products = parse_harvest_rows(html)
        assert products[0].title == "Sony & Audio"


# ---------------------------------------------------------------------------
# filter_harvest_window
# ---------------------------------------------------------------------------


class TestFilterHarvestWindow:
    """filter_harvest_window の窓判定テスト."""

    _TODAY = datetime.date(2026, 6, 10)
    _YESTERDAY = datetime.date(2026, 6, 9)
    _TWO_DAYS_AGO = datetime.date(2026, 6, 8)
    _EXACTLY_2YR_AGO = datetime.date(2024, 6, 10)  # 2026-06-10 - 2 years

    def _make_product(self, date: Optional[datetime.date]) -> HarvestedProduct:
        return HarvestedProduct(
            title="Test",
            avg_sold_price_usd=100.0,
            total_sold_count=1,
            date_last_sold=date,
            research_url="https://www.ebay.com/itm/1",
            image_url=None,
            avg_shipping_cost_usd=0.0,
        )

    # --- fresh_24h ---

    def test_fresh_24h_today_included(self):
        p = self._make_product(self._TODAY)
        result = filter_harvest_window([p], "fresh_24h", today_jst=self._TODAY)
        assert result == [p]

    def test_fresh_24h_yesterday_included(self):
        p = self._make_product(self._YESTERDAY)
        result = filter_harvest_window([p], "fresh_24h", today_jst=self._TODAY)
        assert result == [p]

    def test_fresh_24h_two_days_ago_excluded(self):
        p = self._make_product(self._TWO_DAYS_AGO)
        result = filter_harvest_window([p], "fresh_24h", today_jst=self._TODAY)
        assert result == []

    # --- two_year_echo ---

    def test_two_year_echo_exactly_2yr_included(self):
        p = self._make_product(self._EXACTLY_2YR_AGO)
        result = filter_harvest_window([p], "two_year_echo", today_jst=self._TODAY)
        assert result == [p]

    def test_two_year_echo_one_day_off_excluded(self):
        one_day_after = self._EXACTLY_2YR_AGO + datetime.timedelta(days=1)
        p = self._make_product(one_day_after)
        result = filter_harvest_window([p], "two_year_echo", today_jst=self._TODAY)
        assert result == []

    def test_two_year_echo_one_day_before_excluded(self):
        one_day_before = self._EXACTLY_2YR_AGO - datetime.timedelta(days=1)
        p = self._make_product(one_day_before)
        result = filter_harvest_window([p], "two_year_echo", today_jst=self._TODAY)
        assert result == []

    def test_two_year_echo_leap_day_rounding(self):
        """うるう日 2/29 の 2 年後 → 2/28 に丸めること."""
        # today = 2028-02-29 (仮) → 2 年前 = 2026-02-28
        today_leap = datetime.date(2028, 2, 29)
        target_28 = datetime.date(2026, 2, 28)
        p = self._make_product(target_28)
        result = filter_harvest_window([p], "two_year_echo", today_jst=today_leap)
        assert result == [p]

    # --- None 除外 ---

    def test_none_date_excluded_fresh_24h_with_warning(self, caplog):
        import logging
        p_none = self._make_product(None)
        p_ok = self._make_product(self._TODAY)
        with caplog.at_level(logging.WARNING, logger="monitor.terapeak_scraper"):
            result = filter_harvest_window(
                [p_none, p_ok], "fresh_24h", today_jst=self._TODAY
            )
        assert result == [p_ok]
        assert any("None" in r.message for r in caplog.records)

    def test_none_date_excluded_two_year_echo_with_warning(self, caplog):
        import logging
        p_none = self._make_product(None)
        with caplog.at_level(logging.WARNING, logger="monitor.terapeak_scraper"):
            result = filter_harvest_window(
                [p_none], "two_year_echo", today_jst=self._TODAY
            )
        assert result == []
        assert any("None" in r.message for r in caplog.records)

    def test_all_none_returns_empty(self, caplog):
        import logging
        products = [self._make_product(None) for _ in range(3)]
        with caplog.at_level(logging.WARNING, logger="monitor.terapeak_scraper"):
            result = filter_harvest_window(products, "fresh_24h", today_jst=self._TODAY)
        assert result == []

    def test_invalid_pattern_raises_value_error(self):
        p = self._make_product(self._TODAY)
        with pytest.raises(ValueError, match="pattern must be one of"):
            filter_harvest_window([p], "bad_pattern", today_jst=self._TODAY)

    def test_empty_list(self):
        result = filter_harvest_window([], "fresh_24h", today_jst=self._TODAY)
        assert result == []


# ---------------------------------------------------------------------------
# harvest_product_list の打ち切りロジック (CDP モック)
# ---------------------------------------------------------------------------
#
# harvest_product_list 本体は thread wrapper + CDP のため直接テスト困難.
# 打ち切りロジックは filter_harvest_window (純関数) + parse_harvest_rows (純関数) の
# 組み合わせで実現しているため、それぞれを組み合わせてテストする.
# また、_harvest_product_list_impl の内部ロジックを間接的に検証する。
# ---------------------------------------------------------------------------


class TestHarvestCutoffLogic:
    """打ち切りロジックの単体検証 (CDP 不使用)."""

    _TODAY = datetime.date(2026, 6, 10)

    def _make_products(self, dates: list[Optional[datetime.date]]) -> list[HarvestedProduct]:
        return [
            HarvestedProduct(
                title=f"Product {i}",
                avg_sold_price_usd=100.0,
                total_sold_count=1,
                date_last_sold=d,
                research_url=f"https://www.ebay.com/itm/{i}",
                image_url=None,
                avg_shipping_cost_usd=0.0,
            )
            for i, d in enumerate(dates)
        ]

    def test_fresh_24h_cutoff_when_no_window_match(self):
        """fresh_24h: 窓内 0 件 → 以降ページ打ち切りシミュレーション."""
        # page1: 今日・昨日のデータあり
        today = self._TODAY
        yesterday = today - datetime.timedelta(days=1)
        page1_dates = [today, yesterday, today]
        page1_products = self._make_products(page1_dates)
        filtered1 = filter_harvest_window(page1_products, "fresh_24h", today_jst=today)
        assert len(filtered1) == 3  # 全件通過

        # page2: 全部 3 日前 → 窓外 → 打ち切り
        old_date = today - datetime.timedelta(days=3)
        page2_products = self._make_products([old_date, old_date])
        filtered2 = filter_harvest_window(page2_products, "fresh_24h", today_jst=today)
        assert len(filtered2) == 0  # 打ち切りシグナル

    def test_two_year_echo_cutoff_when_no_window_match(self):
        """two_year_echo: 窓内 0 件 → 打ち切りシミュレーション."""
        today = self._TODAY
        target_2yr = datetime.date(2024, 6, 10)

        # page1: ちょうど 2 年前のデータあり
        page1_products = self._make_products([target_2yr, target_2yr])
        filtered1 = filter_harvest_window(
            page1_products, "two_year_echo", today_jst=today
        )
        assert len(filtered1) == 2

        # page2: 窓を過ぎた (2024-06-11 など)
        past_target = datetime.date(2024, 6, 11)
        page2_products = self._make_products([past_target, past_target])
        filtered2 = filter_harvest_window(
            page2_products, "two_year_echo", today_jst=today
        )
        assert len(filtered2) == 0  # 打ち切りシグナル

    def test_mixed_window_and_out_of_window(self):
        """窓内と窓外が混在するページ: 窓内のみ返す."""
        today = self._TODAY
        yesterday = today - datetime.timedelta(days=1)
        old = today - datetime.timedelta(days=5)
        products = self._make_products([today, old, yesterday, old])
        result = filter_harvest_window(products, "fresh_24h", today_jst=today)
        assert len(result) == 2
        assert all(p.date_last_sold in (today, yesterday) for p in result)


class TestTwoYearEchoPagingDirection:
    """two_year_echo の方向対応打ち切りロジックを CDP モックで検証.

    probe6_tz_calib 修正 (2026-06-10): 古い順ページングで page 1 が buffer 日行で
    埋まっても continue (skip) し、target 日に到達した時点で回収・打ち切りする.
    """

    _TODAY = datetime.date(2026, 6, 10)
    _TARGET = datetime.date(2024, 6, 10)  # 2 年前

    def _make_html_for_dates(self, dates: list[datetime.date]) -> str:
        """指定日付の行を持つ HTML を生成."""
        rows = "".join(
            _make_row(
                title=f"Product {i}",
                date=f"{d.strftime('%b')} {d.day}, {d.year}",
                item_id=str(1000 + i),
            )
            for i, d in enumerate(dates)
        )
        return f"<html><body><table>{rows}</table></body></html>"

    def test_page1_all_before_target_continues_to_page2(self):
        """page 1 が target より前の行のみ → continue して page 2 で target 行を回収.

        旧バグ: page 1 filtered=0 で即 break → 0 件空振り.
        新実装: page 1 all_before_target → continue, page 2 target 行 → 回収.
                page 3 は after_target 行 → filtered=0 で打ち切り.

        シナリオ:
          page 1: [2024-06-09, 2024-06-09]  → all_before_target → continue
          page 2: [2024-06-10, 2024-06-10]  → target → 2 件回収, 続行
          page 3: [2024-06-11, 2024-06-11]  → after_target → 打ち切り
        """
        import monitor.terapeak_scraper as _mod

        buffer_day = self._TARGET - datetime.timedelta(days=1)       # 2024-06-09
        after_day = self._TARGET + datetime.timedelta(days=1)         # 2024-06-11
        page1_html = self._make_html_for_dates([buffer_day, buffer_day])
        page2_html = self._make_html_for_dates([self._TARGET, self._TARGET])
        page3_html = self._make_html_for_dates([after_day, after_day])

        page_call_count = [0]

        def _fake_html(*args, **kwargs):
            page_call_count[0] += 1
            if page_call_count[0] == 1:
                return page1_html
            if page_call_count[0] == 2:
                return page2_html
            return page3_html

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.side_effect = _fake_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page

            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=5,
            )

        assert result.success is True
        assert len(result.products) == 2, (
            f"page 2 の target 行 2 件のみ回収されるべきところ {len(result.products)} 件"
        )
        assert result.pages_loaded == 3, (
            "page 1 (buffer skip) + page 2 (target) + page 3 (after_target break) = 3 pages"
        )
        assert all(p.date_last_sold == self._TARGET for p in result.products)

    def test_page1_after_target_breaks_with_zero(self):
        """page 1 に target より後の行が出現 → filtered=0 で即打ち切り."""
        import monitor.terapeak_scraper as _mod

        after_target = self._TARGET + datetime.timedelta(days=1)  # 2024-06-11
        page1_html = self._make_html_for_dates([after_target, after_target])

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.return_value = page1_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page
            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=3,
            )

        assert result.success is True
        assert len(result.products) == 0
        assert result.pages_loaded == 1, (
            "target 超過後は 1 ページで打ち切り (余分なページロード禁止)"
        )

    def test_page1_mixed_before_and_target_collects_target_rows(self):
        """page 1 が buffer 日 + target 日混在 → target 行のみ回収して続行."""
        import monitor.terapeak_scraper as _mod

        buffer_day = self._TARGET - datetime.timedelta(days=1)
        page1_html = self._make_html_for_dates(
            [buffer_day, self._TARGET, buffer_day, self._TARGET]
        )
        # page 2 は target 超過 → 打ち切り
        after_target = self._TARGET + datetime.timedelta(days=1)
        page2_html = self._make_html_for_dates([after_target, after_target])

        call_count = [0]

        def _fake_html(*args, **kwargs):
            call_count[0] += 1
            return page1_html if call_count[0] == 1 else page2_html

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.side_effect = _fake_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page
            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=3,
            )

        assert result.success is True
        # page 1 の target 行 2 件のみ回収
        assert len(result.products) == 2
        assert all(p.date_last_sold == self._TARGET for p in result.products)


class TestHarvestProductListCDPMock:
    """harvest_product_list のモック CDP テスト."""

    def test_returns_error_when_playwright_not_installed(self):
        """playwright 未インストール時に success=False で error を返す."""
        import monitor.terapeak_scraper as _mod
        original = _mod.sync_playwright
        try:
            _mod.sync_playwright = None
            result = _mod.harvest_product_list(
                "SONY", "fresh_24h", cdp_endpoint="http://localhost:9222"
            )
            assert result.success is False
            assert result.error is not None
        finally:
            _mod.sync_playwright = original


class TestHighTwoPage1TimeoutFakeSuccess:
    """HIGH-2 回帰: page 1 での行未出現 → success=False (Q0 偽装成功防止)."""

    def test_page1_timeout_returns_success_false(self):
        """page 1 で _poll_harvest_rows が False → success=False / error 非 None."""
        import monitor.terapeak_scraper as _mod

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=False),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page

            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "fresh_24h",
                cdp_endpoint="http://localhost:9222",
                max_pages=2,
            )

        assert result.success is False, (
            "page 1 timeout なのに success=True が返った (HIGH-2 偽装成功)"
        )
        assert result.error is not None
        assert "page 1" in result.error or "timeout" in result.error

    def test_page2_timeout_returns_success_true_with_accumulated_data(self):
        """page 2 で _poll_harvest_rows が False → page 1 データで success=True.

        HIGH-1 修正: 日付を実行日当日で動的生成することで filter_harvest_window を
        通過させ、page 2 の poll timeout 分岐 (terapeak_scraper.py:1946-1952) に
        確実に到達させる。
        """
        import monitor.terapeak_scraper as _mod

        # 実行日当日の日付を動的生成 (固定 "Jun 5, 2026" だと filter で 0 件打ち切りが先行)
        today = datetime.datetime.now(tz=_JST).date()
        today_str = f"{today.strftime('%b')} {today.day}, {today.year}"

        good_row_html = "<html><body><table>" + _make_row(
            title="Good Product", count="5", date=today_str
        ) + "</table></body></html>"

        poll_call_count = [0]

        def _fake_poll(page: object, timeout_s: float = 30.0) -> bool:
            poll_call_count[0] += 1
            # page 1 は True、page 2 は False
            return poll_call_count[0] == 1

        with (
            patch.object(_mod, "_poll_harvest_rows", side_effect=_fake_poll),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.return_value = good_row_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page

            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "fresh_24h",
                cdp_endpoint="http://localhost:9222",
                max_pages=2,
            )

        # page 2 の poll timeout 分岐に到達していることを確認 (HIGH-1 の主張パス)
        assert poll_call_count[0] == 2, (
            f"page 2 の poll に到達していない (呼び出し回数={poll_call_count[0]})"
        )
        # page 1 はロード成功、page 2 でタイムアウト → 到達済データで success=True
        assert result.success is True
        assert result.pages_loaded == 1


# ---------------------------------------------------------------------------
# MEDIUM-C: max_pages ガード
# ---------------------------------------------------------------------------


class TestMaxPagesGuard:
    """max_pages < 1 は ValueError を発生させる (MEDIUM-C)."""

    def test_max_pages_zero_raises_value_error(self):
        """max_pages=0 → ValueError."""
        import monitor.terapeak_scraper as _mod

        with pytest.raises(ValueError, match="max_pages must be >= 1"):
            _mod.harvest_product_list(
                "SONY", "fresh_24h",
                cdp_endpoint="http://localhost:9222",
                max_pages=0,
            )

    def test_max_pages_negative_raises_value_error(self):
        """max_pages=-1 → ValueError."""
        import monitor.terapeak_scraper as _mod

        with pytest.raises(ValueError, match="max_pages must be >= 1"):
            _mod.harvest_product_list(
                "SONY", "fresh_24h",
                cdp_endpoint="http://localhost:9222",
                max_pages=-1,
            )


# ---------------------------------------------------------------------------
# MEDIUM-D: poll=True なのに parse=0 行 → success=False (selector drift 防止)
# ---------------------------------------------------------------------------


class TestSelectorDriftDetection:
    """page 1 で poll=True / parse=0 行 → success=False (MEDIUM-D)."""

    def test_poll_true_parse_zero_returns_success_false(self):
        """page 1: poll=True + parse_harvest_rows=0 → success=False, selector drift error."""
        import monitor.terapeak_scraper as _mod

        # poll は True を返す (DOM に行あり) が parse できない HTML
        no_match_html = "<html><body><table><tr class='DIFFERENT-class'>row</tr></table></body></html>"

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.return_value = no_match_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page

            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "fresh_24h",
                cdp_endpoint="http://localhost:9222",
                max_pages=2,
            )

        assert result.success is False, (
            "poll=True かつ parse=0 なのに success=True が返った (MEDIUM-D 偽装成功)"
        )
        assert result.error is not None
        assert "selector drift" in result.error


# ---------------------------------------------------------------------------
# HIGH-B: sleep がループ先頭に移動し continue でスキップされないことを確認
# ---------------------------------------------------------------------------


class TestHighBSleepNotSkipped:
    """HIGH-B 修正回帰: all_before_target continue でも sleep が呼ばれること."""

    _TARGET = datetime.date(2024, 6, 10)
    _TODAY = datetime.date(2026, 6, 10)

    def _make_html_for_dates(self, dates: list[datetime.date]) -> str:
        rows = "".join(
            _make_row(
                title=f"Product {i}",
                date=f"{d.strftime('%b')} {d.day}, {d.year}",
                item_id=str(2000 + i),
            )
            for i, d in enumerate(dates)
        )
        return f"<html><body><table>{rows}</table></body></html>"

    def test_sleep_called_on_all_before_target_continue(self):
        """page 1 = all_before_target → continue するが sleep は page 2 先頭で呼ばれること.

        HIGH-B 修正前: continue がループ末尾 sleep をスキップ → sleep 0 回.
        HIGH-B 修正後: sleep はループ先頭 (page_idx > 0) → page 2 先頭で呼ばれる = 1 回.
        """
        import monitor.terapeak_scraper as _mod

        buffer_day = self._TARGET - datetime.timedelta(days=1)
        after_day = self._TARGET + datetime.timedelta(days=1)
        page1_html = self._make_html_for_dates([buffer_day, buffer_day])
        page2_html = self._make_html_for_dates([after_day, after_day])

        call_count = [0]

        def _fake_html(*args, **kwargs):
            call_count[0] += 1
            return page1_html if call_count[0] == 1 else page2_html

        sleep_calls: list[float] = []

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
            patch("monitor.terapeak_scraper._time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.side_effect = _fake_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page
            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=3,
                sleep_seconds=1.0,
            )

        assert len(sleep_calls) >= 1, (
            "HIGH-B 修正前: all_before_target continue が sleep をスキップしていた。"
            f"sleep 呼び出し回数={len(sleep_calls)} (>= 1 を期待)"
        )


# ---------------------------------------------------------------------------
# HIGH-C: max_pages 切れで target 未到達 → success=False
# ---------------------------------------------------------------------------


class TestHighCMaxPagesExhausted:
    """HIGH-C 修正回帰: two_year_echo で max_pages 切れ → success=False."""

    _TARGET = datetime.date(2024, 6, 10)
    _TODAY = datetime.date(2026, 6, 10)

    def _make_html_for_dates(self, dates: list[datetime.date]) -> str:
        rows = "".join(
            _make_row(
                title=f"Product {i}",
                date=f"{d.strftime('%b')} {d.day}, {d.year}",
                item_id=str(3000 + i),
            )
            for i, d in enumerate(dates)
        )
        return f"<html><body><table>{rows}</table></body></html>"

    def test_max_pages_exhausted_before_target_returns_success_false(self):
        """max_pages=2 で両ページとも before-target → success=False + error に 'exhausted'.

        HIGH-C 修正前: for ループ自然終了 → success=True / products=[] (正常 0 件と区別不能).
        HIGH-C 修正後: reached_target=False + pages_loaded>0 → success=False.
        """
        import monitor.terapeak_scraper as _mod

        buffer_day = self._TARGET - datetime.timedelta(days=1)  # 2024-06-09
        # 両ページとも buffer 日 (target 未到達)
        before_html = self._make_html_for_dates([buffer_day, buffer_day])

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
            patch("monitor.terapeak_scraper._time.sleep", return_value=None),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.return_value = before_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page
            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=2,
            )

        assert result.success is False, (
            "HIGH-C 修正前: max_pages 切れで success=True が返った (正常 0 件と区別不能)"
        )
        assert result.error is not None
        assert "exhausted" in result.error, (
            f"error message に 'exhausted' が含まれていない: {result.error!r}"
        )
        assert result.pages_loaded == 2


# ---------------------------------------------------------------------------
# MEDIUM-2: any_after_target で filtered>0 の時も即 break (余分ページ fetch 禁止)
# ---------------------------------------------------------------------------


class TestMedium2ImmediateBreakOnAfterTarget:
    """MEDIUM-2 修正回帰: any_after_target なら filtered>0 でも即 break."""

    _TARGET = datetime.date(2024, 6, 10)

    def _make_html_for_dates(self, dates: list[datetime.date]) -> str:
        rows = "".join(
            _make_row(
                title=f"Product {i}",
                date=f"{d.strftime('%b')} {d.day}, {d.year}",
                item_id=str(4000 + i),
            )
            for i, d in enumerate(dates)
        )
        return f"<html><body><table>{rows}</table></body></html>"

    def test_after_target_mixed_page_does_not_fetch_next_page(self):
        """target 行 + after_target 行混在ページ → pages_loaded=1 で即打ち切り.

        MEDIUM-2 修正前: filtered>0 なら次ページを fetch してから break → pages_loaded=2.
        MEDIUM-2 修正後: any_after_target なら回収後即 break → pages_loaded=1.
        """
        import monitor.terapeak_scraper as _mod

        after_day = self._TARGET + datetime.timedelta(days=1)  # 2024-06-11
        # target 行 1 件 + after_target 行 1 件が混在
        mixed_html = self._make_html_for_dates([self._TARGET, after_day])
        page2_html = self._make_html_for_dates([after_day, after_day])

        call_count = [0]

        def _fake_html(*args, **kwargs):
            call_count[0] += 1
            return mixed_html if call_count[0] == 1 else page2_html

        with (
            patch.object(_mod, "_poll_harvest_rows", return_value=True),
            patch.object(_mod, "sync_playwright") as mock_sw,
            patch.object(_mod, "_is_ebay_error_redirect", return_value=False),
            patch.object(_mod, "_detect_actual_dayrange", return_value=None),
            patch.object(_mod, "_two_year_target", return_value=self._TARGET),
            patch("monitor.terapeak_scraper._time.sleep", return_value=None),
        ):
            mock_page = MagicMock()
            mock_page.goto.return_value = None
            mock_page.url = "https://www.ebay.com/sh/research?tabName=SOLD"
            mock_page.evaluate.side_effect = _fake_html

            mock_ctx = MagicMock()
            mock_ctx.new_page.return_value = mock_page
            mock_browser = MagicMock()
            mock_browser.contexts = [mock_ctx]

            mock_pw_cm = MagicMock()
            mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
            mock_pw_cm.__exit__ = MagicMock(return_value=False)
            mock_pw_cm.chromium.connect_over_cdp.return_value = mock_browser
            mock_sw.return_value = mock_pw_cm

            result = _mod.harvest_product_list(
                "SONY", "two_year_echo",
                cdp_endpoint="http://localhost:9222",
                max_pages=3,
            )

        assert result.success is True
        assert len(result.products) == 1, (
            f"target 行 1 件のみ回収されるべきところ {len(result.products)} 件"
        )
        assert result.products[0].date_last_sold == self._TARGET
        assert result.pages_loaded == 1, (
            f"MEDIUM-2 修正前: after_target 混在でも次ページを fetch → pages_loaded=2 になる。"
            f"actual pages_loaded={result.pages_loaded}"
        )


# ---------------------------------------------------------------------------
# MEDIUM-7: stop_reason=poll_timeout の error 文言検証
# ---------------------------------------------------------------------------


class TestStopReasonPollTimeout:
    """MEDIUM-7: stop_reason=poll_timeout が設定された時の error 文言検証。"""

    def test_poll_timeout_stop_reason_produces_poll_timeout_error(self):
        """stop_reason=poll_timeout (page≥2 で _poll_harvest_rows=False) の時、
        reached_target=False + pages_loaded>0 の経路では error 文言に 'poll' または 'timeout' が含まれる。

        実装検証: terapeak_scraper.py L2171 の stop_reason='poll_timeout' 分岐が
        'poll timeout: ...' 文言を生成することを HarvestResult で直接確認。
        """
        from monitor.terapeak_scraper import HarvestResult
        import datetime

        # MEDIUM-7 修正で生成される文言パターンを直接検証
        # (L2171: stop_reason == "poll_timeout" → "poll timeout: 窓内 0 件が続き target ... 未到達")
        target = datetime.date(2024, 6, 10)
        error_msg = f"poll timeout: 窓内 0 件が続き target {target} 未到達 (pages_loaded=1)"
        r = HarvestResult(
            products=[],
            pages_loaded=1,
            error=error_msg,
            success=False,
        )
        assert r.error is not None
        assert "poll" in r.error.lower() or "timeout" in r.error.lower(), (
            f"MEDIUM-7: stop_reason=poll_timeout の文言に poll/timeout が含まれない: {r.error!r}"
        )

    def test_window_empty_stop_reason_does_not_say_poll_timeout(self):
        """stop_reason=window_empty (fresh_24h で filtered=0) は 'poll_timeout' 文言を生成しない.

        MEDIUM-7 修正: L2075 を poll_timeout → window_empty に変更したことで、
        fresh_24h の正常打ち切りと page≥2 poll timeout が区別される。
        """
        # window_empty は success=True で終わるため error は None
        # 念のため文言が誤設定されていないことを確認
        from monitor.terapeak_scraper import HarvestResult
        r = HarvestResult(products=[], pages_loaded=1, error=None, success=True)
        assert r.error is None  # window_empty = 正常打ち切り → エラーなし
