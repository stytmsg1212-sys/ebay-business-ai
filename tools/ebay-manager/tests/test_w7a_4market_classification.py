"""W7-A 候補 C: 4 区分 primary_market 判定の境界条件 + 全区分網羅 test.

詳細仕様: reference_shipping_tariff_logic.md v2.0 § 4.

判定式 (W110(2) 2026-05-09 改訂後):
    - sample < MIN_SAMPLE_SIZE (=3)             → unknown
    - sample >= 3 かつ us_ratio >= US_ONLY_THRESHOLD (=0.70):
        - sample < MIN_SAMPLE_SIZE_US_ONLY (=5) → mixed_global (DDP 安全側格下げ)
        - sample >= 5                            → US_only
    - sample >= 3 かつ us_ratio <= GLOBAL_ONLY_THRESHOLD (=0.30) → global_only
    - sample >= 3 かつ 中間                       → mixed_global
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.terapeak_scraper import (  # noqa: E402
    _judge_primary_market,
    GLOBAL_ONLY_THRESHOLD,
    MIN_SAMPLE_SIZE,
    MIN_SAMPLE_SIZE_US_ONLY,
    US_ONLY_THRESHOLD,
)


def test_constants_match_reference_v2_0():
    """定数値が reference_shipping_tariff_logic.md v2.0 § 4.1 と一致."""
    assert US_ONLY_THRESHOLD == 0.70
    assert GLOBAL_ONLY_THRESHOLD == 0.30
    assert MIN_SAMPLE_SIZE == 3
    assert MIN_SAMPLE_SIZE_US_ONLY == 5


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


def test_min_sample_boundary_exactly_5_is_judged():
    """sample 5 (= MIN_SAMPLE_SIZE_US_ONLY ぴったり) で全判定可能."""
    # 5/0 = 100% US → US_only (sample 5 達成で確定)
    market, _ = _judge_primary_market(5, 0)
    assert market == "US_only"
    # 0/5 = 0% US → global_only
    market, _ = _judge_primary_market(0, 5)
    assert market == "global_only"


# ── W110(2) 新仕様: 2 段サンプル閾値 (2026-05-09) ───────────────────────────

def test_w110_us_only_demoted_to_mixed_when_sample_3_full_us():
    """W110(2): sample 3 で US 3/3 (100%) は mixed_global に格下げ (DDP 安全側).

    sample 3/3 完全 US でも偶然性が高く、DDP 関税 buffer 内蔵で
    商品価格が他国客から見て高く見える機会損失リスクを回避するため.
    """
    market, reason = _judge_primary_market(3, 0)
    assert market == "mixed_global"
    assert "sample<5" in reason or "DDP 安全側" in reason


def test_w110_us_only_demoted_to_mixed_when_sample_4_full_us():
    """W110(2): sample 4 で US 4/0 (100%) も mixed_global に格下げ."""
    market, reason = _judge_primary_market(4, 0)
    assert market == "mixed_global"
    assert "sample<5" in reason or "DDP 安全側" in reason


def test_w110_us_only_confirmed_at_sample_5_full_us():
    """W110(2): sample 5 で US 5/0 (100%) は US_only 確定."""
    market, _ = _judge_primary_market(5, 0)
    assert market == "US_only"


def test_w110_global_only_judged_at_sample_3_full_non_us():
    """W110(2): sample 3 で US 0/3 (0%) は global_only 判定可能 (US_only 以外は 3 件閾値)."""
    market, _ = _judge_primary_market(0, 3)
    assert market == "global_only"


def test_w110_unknown_below_sample_3():
    """W110(2): sample 2 (= MIN-1) で unknown 判定."""
    market, reason = _judge_primary_market(1, 1)
    assert market == "unknown"
    assert "sample 2 < 3" in reason


def test_w110_mixed_global_at_sample_3_middle_ratio():
    """W110(2): sample 3 で US 1/2 (33%) → middle range で mixed_global."""
    # 1/3 = 0.333... > GLOBAL_ONLY_THRESHOLD (0.30) かつ < US_ONLY_THRESHOLD (0.70)
    market, _ = _judge_primary_market(1, 2)
    assert market == "mixed_global"
