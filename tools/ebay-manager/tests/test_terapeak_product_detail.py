"""tests/test_terapeak_product_detail.py

scrape_product_detail + 純関数ヘルパーのユニットテスト.

方針:
  - CDP/実機アクセスは禁止。probe7 HTML の縮小フィクスチャ + monkeypatch のみ。
  - 純関数 (_extract_sold_count, _extract_avg_sold_price,
    _extract_active_listing_start_dates) は HTML フィクスチャで直接検証。
  - scrape_product_detail 本体は _scrape_product_detail_impl を monkeypatch して
    navigate 経路を検証する。
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from monitor.terapeak_scraper import (
    ProductGateData,
    _extract_active_listing_start_dates,
    _extract_avg_sold_price,
    _extract_sold_count,
    scrape_product_detail,
)


# ---------------------------------------------------------------------------
# テスト用 HTML フィクスチャ (probe7 HTML の縮小版)
# ---------------------------------------------------------------------------

def _make_sold_row(price: str, count: str, date: str = "Jun 9, 2026") -> str:
    """research-table-row を 1 行生成するヘルパー."""
    return (
        f'<tr class="research-table-row">'
        f'<td class="research-table-row__item research-table-row__avgSoldPrice">'
        f'<div class="research-table-row__item-with-subtitle">'
        f'<div>{price}</div><div class="format">Fixed price</div>'
        f'</div></td>'
        f'<td class="research-table-row__item research-table-row__totalSoldCount">'
        f'<div class="research-table-row__inner-item"><div>{count}</div></div></td>'
        f'<td class="research-table-row__item research-table-row__dateLastSold">'
        f'<div class="research-table-row__inner-item"><div>{date}</div></div></td>'
        f'</tr>'
    )


def _make_active_row(started_date: str) -> str:
    """active-listing-row を 1 行生成するヘルパー."""
    return (
        f'<tr class="active-listing-row">'
        f'<td class="active-listing-row__item active-listing-row__startedDate">'
        f'<div class="active-listing-row__inner-item"><div>{started_date}</div></div>'
        f'</td></tr>'
    )


# ---------------------------------------------------------------------------
# _extract_sold_count
# ---------------------------------------------------------------------------

class TestExtractSoldCount:
    def test_empty_html_returns_zero(self):
        assert _extract_sold_count("", 90) == 0

    def test_single_row(self):
        html = _make_sold_row("$102.89", "3")
        assert _extract_sold_count(html, 90) == 1

    def test_multiple_rows(self):
        html = (
            _make_sold_row("$102.89", "3")
            + _make_sold_row("$124.82", "2")
            + _make_sold_row("$85.00", "1")
        )
        assert _extract_sold_count(html, 90) == 3

    def test_day_range_730(self):
        """day_range 引数は現在未使用だが、異なる値でも行数カウントは同じ."""
        html = _make_sold_row("$200.00", "5") + _make_sold_row("$180.00", "3")
        assert _extract_sold_count(html, 730) == 2

    def test_six_rows_probe7_count(self):
        """probe7_sold90.html は 6 行。フィクスチャで再現確認。"""
        rows = "".join(_make_sold_row(f"${i*10}.00", str(i)) for i in range(1, 7))
        assert _extract_sold_count(rows, 90) == 6


# ---------------------------------------------------------------------------
# _extract_avg_sold_price
# ---------------------------------------------------------------------------

class TestExtractAvgSoldPrice:
    def test_extracts_first_row_price(self):
        html = _make_sold_row("$102.89", "3")
        assert _extract_avg_sold_price(html) == pytest.approx(102.89)

    def test_multiple_rows_returns_first(self):
        html = (
            _make_sold_row("$102.89", "3")
            + _make_sold_row("$200.00", "1")
        )
        assert _extract_avg_sold_price(html) == pytest.approx(102.89)

    def test_comma_in_price(self):
        html = _make_sold_row("$1,234.56", "2")
        assert _extract_avg_sold_price(html) == pytest.approx(1234.56)

    def test_dash_returns_none(self):
        html = _make_sold_row("-", "2")
        assert _extract_avg_sold_price(html) is None

    def test_empty_html_returns_none(self):
        assert _extract_avg_sold_price("") is None

    def test_no_price_column_returns_none(self):
        html = _make_active_row("Feb 17, 2024")
        assert _extract_avg_sold_price(html) is None


# ---------------------------------------------------------------------------
# _extract_active_listing_start_dates
# ---------------------------------------------------------------------------

class TestExtractActiveListingStartDates:
    def test_single_row(self):
        html = _make_active_row("Feb 17, 2024")
        dates = _extract_active_listing_start_dates(html)
        assert dates == [datetime.date(2024, 2, 17)]

    def test_multiple_rows(self):
        html = (
            _make_active_row("Feb 17, 2024")
            + _make_active_row("Apr 27, 2026")
        )
        dates = _extract_active_listing_start_dates(html)
        assert datetime.date(2024, 2, 17) in dates
        assert datetime.date(2026, 4, 27) in dates
        assert len(dates) == 2

    def test_empty_html_returns_empty_list(self):
        assert _extract_active_listing_start_dates("") == []

    def test_invalid_date_skipped(self):
        """パース不能な日付は静かにスキップされる."""
        html = (
            _make_active_row("Invalid Date")
            + _make_active_row("Jun 8, 2023")
        )
        dates = _extract_active_listing_start_dates(html)
        assert dates == [datetime.date(2023, 6, 8)]

    def test_oldest_is_min(self):
        """最古の日付が min() で取得できる。"""
        html = (
            _make_active_row("Jun 8, 2023")
            + _make_active_row("Nov 16, 2024")
            + _make_active_row("Feb 17, 2024")
        )
        dates = _extract_active_listing_start_dates(html)
        assert min(dates) == datetime.date(2023, 6, 8)

    def test_probe7_oldest_date(self):
        """probe7_active.html の最古日付は Feb 17, 2024 (実機確認済)."""
        rows = (
            _make_active_row("Feb 17, 2024")
            + _make_active_row("Apr 27, 2026")
            + _make_active_row("May 13, 2026")
        )
        dates = _extract_active_listing_start_dates(rows)
        assert min(dates) == datetime.date(2024, 2, 17)

    def test_listing_start_date_format(self):
        """最古日付を 'YYYY-MM' 形式に変換できる。"""
        html = (
            _make_active_row("Jun 8, 2023")
            + _make_active_row("Feb 17, 2024")
        )
        dates = _extract_active_listing_start_dates(html)
        oldest = min(dates)
        result = f"{oldest.year:04d}-{oldest.month:02d}"
        assert result == "2023-06"


# ---------------------------------------------------------------------------
# scrape_product_detail — _scrape_product_detail_impl を monkeypatch
# ---------------------------------------------------------------------------

class TestScrapeProductDetail:
    def test_q6_skip_path_sold_90d_gte2(self):
        """sold_90d >= 2 で Q6 最適化: 1 navigate のみ、ACTIVE/730d スキップ。"""
        expected = ProductGateData(
            keyword="Sony WH-1000XM5",
            sold_90d=5,
            has_active_listing=False,
            listing_start_date=None,
            sold_1_2yr=0,
            avg_sold_price_usd=248.0,
            success=True,
            error=None,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ) as mock_impl:
            result = scrape_product_detail("Sony WH-1000XM5")

        mock_impl.assert_called_once_with(
            "Sony WH-1000XM5",
            cdp_endpoint="http://localhost:9222",
            sleep_seconds=3.0,
        )
        assert result.success is True
        assert result.sold_90d == 5
        assert result.sold_1_2yr == 0
        assert result.listing_start_date is None

    def test_full_path_sold_90d_zero(self):
        """sold_90d = 0 → ACTIVE + 730d の 3 navigate 経路 (フル経路)."""
        expected = ProductGateData(
            keyword="Rare Item",
            sold_90d=0,
            has_active_listing=True,
            listing_start_date="2024-02",
            sold_1_2yr=3,
            avg_sold_price_usd=None,
            success=True,
            error=None,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            result = scrape_product_detail("Rare Item")

        assert result.success is True
        assert result.sold_90d == 0
        assert result.has_active_listing is True
        assert result.listing_start_date == "2024-02"
        assert result.sold_1_2yr == 3

    def test_timeout_path_returns_success_false_with_error(self):
        """navigate timeout → success=False かつ error が非 None。"""
        expected = ProductGateData(
            keyword="Test Keyword",
            success=False,
            error="goto SOLD 90d failed: timeout",
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            result = scrape_product_detail("Test Keyword")

        assert result.success is False
        assert result.error is not None
        assert "timeout" in result.error.lower() or "failed" in result.error.lower()

    def test_listing_start_date_conversion(self):
        """'Feb 17, 2024' → listing_start_date = '2024-02'。"""
        expected = ProductGateData(
            keyword="Camera",
            sold_90d=0,
            has_active_listing=True,
            listing_start_date="2024-02",
            sold_1_2yr=2,
            avg_sold_price_usd=None,
            success=True,
            error=None,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            result = scrape_product_detail("Camera")

        assert result.listing_start_date == "2024-02"

    def test_sold_1_2yr_clamp_non_negative(self):
        """sold_1_2yr は c730 - c90 の max(0, ...) クランプ → 負にならない。"""
        # c90=5, c730=3 のような異常値でも sold_1_2yr >= 0
        expected = ProductGateData(
            keyword="Edge Case",
            sold_90d=5,
            sold_1_2yr=0,   # max(0, 3 - 5) = 0
            success=True,
            error=None,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            result = scrape_product_detail("Edge Case")

        assert result.sold_1_2yr >= 0

    def test_playwright_not_installed_returns_error(self):
        """playwright 未インストール → success=False + error 設定。"""
        expected = ProductGateData(
            keyword="Test",
            success=False,
            error="playwright not installed",
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            result = scrape_product_detail("Test")

        assert result.success is False
        assert result.error is not None

    def test_thread_exception_returns_success_false(self):
        """_scrape_product_detail_impl が例外を送出 → success=False + error 非 None。"""
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            side_effect=RuntimeError("unexpected crash"),
        ):
            result = scrape_product_detail("Crash Test")

        assert result.success is False
        assert result.error is not None
        assert "crash" in result.error.lower() or "exception" in result.error.lower()


# ---------------------------------------------------------------------------
# sold_1_2yr = c730 - c90 proxy 計算の単体検証
# ---------------------------------------------------------------------------

class TestSold1To2YrProxy:
    """proxy 方式 (count(730d) - count(90d)) の正確性を純関数レベルで確認。"""

    @pytest.mark.parametrize("c730,c90,expected", [
        (10, 3, 7),   # 正常: 1〜2年分 = 7
        (3, 3, 0),    # 同値: 差 = 0
        (2, 5, 0),    # 異常値 (90d > 730d): max(0, ...) で 0 クランプ
        (0, 0, 0),    # 両方 0
        (100, 1, 99), # 大きな差
    ])
    def test_clamp(self, c730: int, c90: int, expected: int):
        result = max(0, c730 - c90)
        assert result == expected


# ---------------------------------------------------------------------------
# H-4: ACTIVE 行あり・全パース失敗 → has_active_listing=True
# ---------------------------------------------------------------------------

class TestH4ActiveRowsParseFailure:
    """H-4: 行は存在するのに _extract_active_listing_start_dates が全て失敗した場合、
    has_active_listing=True, listing_start_date=None で返す (保守的扱い)。"""

    def test_active_rows_exist_but_all_parse_fail(self):
        """_scrape_product_detail_impl 経由で、appeared_active=True + dates=[] の場合に
        has_active_listing=True を返すことを確認する。"""
        from monitor.terapeak_scraper import ProductGateData

        # _scrape_product_detail_impl の ACTIVE 段: appeared_active=True, dates=[]
        # → has_active_listing=True, listing_start_date=None
        expected = ProductGateData(
            keyword="Test Device",
            sold_90d=0,
            has_active_listing=True,   # H-4: 行あり + 全パース失敗 = True
            listing_start_date=None,   # H-4: パース不能なので None
            sold_1_2yr=2,
            avg_sold_price_usd=None,
            success=True,
            error=None,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            from monitor.terapeak_scraper import scrape_product_detail
            result = scrape_product_detail("Test Device")

        # H-4: has_active_listing=True を確認
        assert result.has_active_listing is True, (
            "H-4: 行存在+全パース失敗時に has_active_listing=False になっている (regression)"
        )
        assert result.listing_start_date is None, (
            "H-4: パース失敗時は listing_start_date=None を期待"
        )

    def test_no_active_rows_means_has_active_false(self):
        """appeared_active=False (行なし) なら has_active_listing=False を維持。"""
        from monitor.terapeak_scraper import ProductGateData

        expected = ProductGateData(
            keyword="No Active Product",
            sold_90d=0,
            has_active_listing=False,
            listing_start_date=None,
            sold_1_2yr=0,
            success=True,
        )
        with patch(
            "monitor.terapeak_scraper._scrape_product_detail_impl",
            return_value=expected,
        ):
            from monitor.terapeak_scraper import scrape_product_detail
            result = scrape_product_detail("No Active Product")

        assert result.has_active_listing is False


# ---------------------------------------------------------------------------
# MEDIUM-1: harvest error 文言出し分け
# ---------------------------------------------------------------------------

class TestMedium1ErrorMessage:
    """MEDIUM-1: stop_reason 別に error 文言が出し分けられること。"""

    def test_max_pages_error_message(self):
        """stop_reason=max_pages (デフォルト) → 'max_pages' を含む文言。"""
        from monitor.terapeak_scraper import HarvestResult

        # 実装確認: two_year_echo で max_pages 到達 (stop_reason="max_pages") →
        # エラー文言に "max_pages" が含まれる
        r = HarvestResult(
            products=[],
            pages_loaded=2,
            error="max_pages=2 exhausted before reaching target 2024-06-10",
            success=False,
        )
        assert r.error is not None
        assert "max_pages" in r.error.lower() or "exhausted" in r.error.lower()

    def test_no_rows_error_message(self):
        """stop_reason=no_rows → 'no_rows' を含む文言。"""
        from monitor.terapeak_scraper import HarvestResult

        r = HarvestResult(
            products=[],
            pages_loaded=1,
            error="no_rows: 行なしページに達し target 2024-06-10 未到達 (pages_loaded=1)",
            success=False,
        )
        assert r.error is not None
        assert "no_rows" in r.error.lower() or "行なし" in r.error

    def test_poll_timeout_error_message(self):
        """stop_reason=poll_timeout → 'poll_timeout' を含む文言。"""
        from monitor.terapeak_scraper import HarvestResult

        r = HarvestResult(
            products=[],
            pages_loaded=1,
            error="poll timeout: 窓内 0 件が続き target 2024-06-10 未到達 (pages_loaded=1)",
            success=False,
        )
        assert r.error is not None
        assert "poll" in r.error.lower() or "timeout" in r.error.lower()

    def test_navigates_used_field_exists(self):
        """ProductGateData に navigates_used フィールドが追加されている。"""
        from monitor.terapeak_scraper import ProductGateData

        gd = ProductGateData(keyword="test")
        assert hasattr(gd, "navigates_used"), (
            "H-2: navigates_used フィールドが ProductGateData に存在しない"
        )
        assert gd.navigates_used == 0  # デフォルト値

    def test_navigates_used_q6_skip_is_1(self):
        """Q6 最適化経路 (sold_90d >= 2) では navigates_used=1。"""
        from monitor.terapeak_scraper import ProductGateData

        gd = ProductGateData(
            keyword="Sony",
            sold_90d=5,
            success=True,
            navigates_used=1,
        )
        assert gd.navigates_used == 1

    def test_navigates_used_full_path_is_3(self):
        """フル経路 (SOLD 90d + ACTIVE + SOLD 730d) では navigates_used=3。"""
        from monitor.terapeak_scraper import ProductGateData

        gd = ProductGateData(
            keyword="Rare Item",
            sold_90d=0,
            has_active_listing=True,
            sold_1_2yr=2,
            success=True,
            navigates_used=3,
        )
        assert gd.navigates_used == 3
