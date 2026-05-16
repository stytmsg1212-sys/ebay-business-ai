"""W7-A 候補 C: 4 区分 primary_market 判定の境界条件 + 全区分網羅 test.

詳細仕様: reference_shipping_tariff_logic.md v2.1 § 4.

判定式 (v2.1 2026-05-15 訂正後 = 一律 sample>=3):
    - sample < MIN_SAMPLE_SIZE (=3)                              → unknown
    - sample >= 3 かつ us_ratio >= US_ONLY_THRESHOLD (=0.70)     → US_only
    - sample >= 3 かつ us_ratio <= GLOBAL_ONLY_THRESHOLD (=0.30) → global_only
    - sample >= 3 かつ 中間                                       → mixed_global
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.terapeak_scraper import (  # noqa: E402
    _judge_primary_market,
    GLOBAL_ONLY_THRESHOLD,
    MIN_SAMPLE_SIZE,
    US_ONLY_THRESHOLD,
)


def test_constants_match_reference_v2_1():
    """定数値が reference_shipping_tariff_logic.md v2.1 § 4.1 と一致."""
    assert US_ONLY_THRESHOLD == 0.70
    assert GLOBAL_ONLY_THRESHOLD == 0.30
    assert MIN_SAMPLE_SIZE == 3


def test_unknown_when_sample_below_min():
    """sample 2 (= MIN-1) で unknown 判定."""
    market, reason = _judge_primary_market(1, 1)
    assert market == "unknown"
    assert "sample 2 < 3" in reason


def test_unknown_when_sample_zero():
    """sample 0 で unknown 判定."""
    market, _ = _judge_primary_market(0, 0)
    assert market == "unknown"


def test_us_only_at_threshold():
    """US 比率 70% (境界、= US_ONLY_THRESHOLD) かつ sample=10 で US_only 判定."""
    market, reason = _judge_primary_market(7, 3)
    assert market == "US_only"
    assert "US 7/10" in reason
    assert ">= 70%" in reason


def test_us_only_above_threshold():
    """US 比率 80% で US_only 判定."""
    assert _judge_primary_market(8, 2)[0] == "US_only"


def test_us_only_at_100pct():
    """US 比率 100% (sample=10) で US_only 判定."""
    assert _judge_primary_market(10, 0)[0] == "US_only"


def test_global_only_at_threshold():
    """US 比率 30% (境界、= GLOBAL_ONLY_THRESHOLD) で global_only 判定."""
    market, reason = _judge_primary_market(3, 7)
    assert market == "global_only"
    assert "US 3/10" in reason
    assert "<= 30%" in reason


def test_global_only_below_threshold():
    """US 比率 20% で global_only 判定."""
    assert _judge_primary_market(2, 8)[0] == "global_only"


def test_global_only_at_0pct():
    """US 比率 0% で global_only 判定."""
    assert _judge_primary_market(0, 10)[0] == "global_only"


def test_mixed_global_at_50pct():
    """US 比率 50% で mixed_global 判定 (中間)."""
    market, reason = _judge_primary_market(5, 5)
    assert market == "mixed_global"
    assert "in middle range" in reason


def test_mixed_global_just_above_global_only():
    """US 比率 31% (= GLOBAL_ONLY_THRESHOLD + 1pp) で mixed_global 判定."""
    assert _judge_primary_market(31, 69)[0] == "mixed_global"


def test_mixed_global_just_below_us_only():
    """US 比率 69% (= US_ONLY_THRESHOLD - 1pp) で mixed_global 判定."""
    assert _judge_primary_market(69, 31)[0] == "mixed_global"


def test_real_data_simulation_stock01_18pct_is_global_only():
    """実 data 検証: stock:01 (US 2/11 = 18%) は global_only 判定."""
    market, _ = _judge_primary_market(2, 9)
    assert market == "global_only"


def test_real_data_simulation_keyence_75pct_is_us_only():
    """実 data 検証: ebayme_72681238000 (US 9/12 = 75%) は US_only 判定."""
    market, _ = _judge_primary_market(9, 3)
    assert market == "US_only"


def test_min_sample_boundary_at_sample_5():
    """sample 5 で全区分判定可能 (sample>=3 で OK なので当然成立)."""
    market, _ = _judge_primary_market(5, 0)
    assert market == "US_only"
    market, _ = _judge_primary_market(0, 5)
    assert market == "global_only"


# ── v2.1 訂正仕様: 一律 sample>=3 (2026-05-15) ───────────────────────────

def test_v21_us_only_confirmed_at_sample_3_full_us():
    """v2.1: sample 3 で US 3/3 (100%) は US_only 確定 (旧 v2.0 では mixed_global 格下げだった)."""
    market, reason = _judge_primary_market(3, 0)
    assert market == "US_only"
    assert "US 3/3" in reason
    assert ">= 70%" in reason


def test_v21_us_only_confirmed_at_sample_4_full_us():
    """v2.1: sample 4 で US 4/0 (100%) も US_only 確定."""
    market, _ = _judge_primary_market(4, 0)
    assert market == "US_only"


def test_v21_us_only_confirmed_at_sample_5_full_us():
    """v2.1: sample 5 で US 5/0 (100%) は US_only 確定."""
    market, _ = _judge_primary_market(5, 0)
    assert market == "US_only"


def test_v21_global_only_judged_at_sample_3_full_non_us():
    """v2.1: sample 3 で US 0/3 (0%) は global_only 判定."""
    market, _ = _judge_primary_market(0, 3)
    assert market == "global_only"


def test_v21_unknown_below_sample_3():
    """v2.1: sample 2 (= MIN-1) で unknown 判定."""
    market, reason = _judge_primary_market(1, 1)
    assert market == "unknown"
    assert "sample 2 < 3" in reason


def test_v21_mixed_global_at_sample_3_middle_ratio():
    """v2.1: sample 3 で US 1/2 (33%) → middle range で mixed_global."""
    market, _ = _judge_primary_market(1, 2)
    assert market == "mixed_global"
