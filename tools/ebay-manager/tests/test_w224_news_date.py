#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W224 (2026-06-05): ニュース投稿日 (published_at) parse + 鮮度表示ヘルパーのテスト.

news_items.published_at は RSS 由来でフォーマット混在 (ISO 8601 Z付き / RFC822)。
両形式を datetime(UTC) に parse し、JST 絶対 + 相対 + 経過日数で表示することを検証。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tabs.tab_dashboard import _parse_news_published, _fmt_news_freshness  # noqa: E402

_NOW = datetime(2026, 6, 5, 8, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_z():
    dt = _parse_news_published("2026-06-04T16:15:12Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 6, 4, 16, 15, 12, tzinfo=timezone.utc)


def test_parse_rfc822():
    dt = _parse_news_published("Fri, 17 Apr 2026 00:00:00 +0000")
    assert dt == datetime(2026, 4, 17, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_with_offset():
    dt = _parse_news_published("2026-06-04T16:15:12+09:00")
    # JST 16:15 = UTC 07:15
    assert dt == datetime(2026, 6, 4, 7, 15, 12, tzinfo=timezone.utc)


def test_parse_empty_and_garbage_return_none():
    assert _parse_news_published("") is None
    assert _parse_news_published(None) is None
    assert _parse_news_published("not a date") is None


def test_fmt_jst_conversion():
    dt = datetime(2026, 6, 4, 16, 15, 12, tzinfo=timezone.utc)  # = JST 6/5 01:15
    jst_abs, rel, age = _fmt_news_freshness(dt, _NOW)
    assert jst_abs == "6/5 01:15"
    assert "時間前" in rel


def test_fmt_relative_minutes():
    dt = datetime(2026, 6, 5, 7, 30, 0, tzinfo=timezone.utc)  # 30 分前
    _, rel, age = _fmt_news_freshness(dt, _NOW)
    assert rel == "30分前"
    assert age < 0.05


def test_fmt_relative_hours():
    dt = datetime(2026, 6, 5, 3, 0, 0, tzinfo=timezone.utc)  # 5 時間前
    _, rel, _age = _fmt_news_freshness(dt, _NOW)
    assert rel == "5時間前"


def test_fmt_relative_days():
    dt = datetime(2026, 6, 3, 8, 0, 0, tzinfo=timezone.utc)  # 2 日前
    _, rel, age = _fmt_news_freshness(dt, _NOW)
    assert rel == "2日前"
    assert 1.9 < age < 2.1


def test_fmt_old_shows_date_not_relative():
    dt = datetime(2026, 4, 17, 0, 0, 0, tzinfo=timezone.utc)  # >7 日前
    _, rel, age = _fmt_news_freshness(dt, _NOW)
    assert rel == "4/17", "7日超は M/D 表示"
    assert age > 7


def test_parse_weird_input_never_raises():
    """外部(RSS)由来の汚染/極端文字列でも例外を投げず None (NEWS render 保護)。"""
    for s in ("9999-99-99T00:00:00Z", "Mon, 99 XXX 9999 99:99:99 +9999",
              "\x00bad", "0001-01-01T00:00:00+99:00", "12345", "T", "Z"):
        assert _parse_news_published(s) is None, s


def test_fmt_future_clamped_to_zero():
    dt = datetime(2026, 6, 5, 9, 0, 0, tzinfo=timezone.utc)  # now より未来
    _, rel, age = _fmt_news_freshness(dt, _NOW)
    assert age == 0
    assert rel == "0分前"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
