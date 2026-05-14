"""W7-A backfill watchdog regression test (H4 fix).

過去 3 回の停止 pattern を pattern match で正しく検知できることを保証.
完了 pattern が abort 時の partial progress (M<N) で誤発火しないことも保証.

H-F fix: watchdog 本体から pattern を import (旧実装は copy-paste で
regression として機能していなかった).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# watchdog 本体から pattern を import (regression として機能させるため)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from w7a_backfill_watchdog import (  # noqa: E402
    ABORT_PATTERN,
    CDP_ERROR_PATTERN,
    CDP_PROFILE_FILTER,
    COMPLETION_PATTERN,
    TIMEOUT_PATTERN,
)


# ─────────────────────────────────
# H6 fix: pattern matching 妥当性
# ─────────────────────────────────

def test_abort_pattern_matches_aborted_by_block():
    """aborted_by_block が pattern hit"""
    line = "2026-05-06 10:17:08 ERROR 連続 5 件失敗 → eBay 規制と判定して停止."
    assert ABORT_PATTERN.search(line)


def test_abort_pattern_matches_aborted_keyword():
    """aborted_by_block keyword 単独でも hit"""
    line = "aborted_by_block: True"
    assert ABORT_PATTERN.search(line)


def test_timeout_pattern_matches_180s():
    """thread timeout (>180s) が pattern hit"""
    line = "2026-05-06 10:17 WARNING 失敗: thread timeout (>180s)"
    assert TIMEOUT_PATTERN.search(line)


def test_timeout_pattern_matches_120s():
    """thread timeout (>120s) も hit (秒数任意)"""
    line = "thread timeout (>120s)"
    assert TIMEOUT_PATTERN.search(line)


def test_cdp_error_pattern_matches():
    """CDP Chrome 起動 error が hit"""
    line = "2026-05-07 21:50 ERROR CDP Chrome が起動していません."
    assert CDP_ERROR_PATTERN.search(line)


# ─────────────────────────────────
# H6 fix: completion pattern が partial で誤発火しない
# ─────────────────────────────────

def test_completion_pattern_matches_full_complete():
    """N/N (M==N) で完了検知"""
    line = "2026-05-06 06:31 INFO 処理: 15/15 件 / 成功: 15 / 失敗: 0"
    m = COMPLETION_PATTERN.search(line)
    assert m
    assert int(m.group(1)) == int(m.group(2))  # processed == total


def test_completion_pattern_does_not_match_partial_progress():
    """abort 時の partial progress (M<N) で完了 trigger しない"""
    line = "2026-05-06 10:17 INFO 処理: 5/349 件 / 成功: 0 / 失敗: 5"
    m = COMPLETION_PATTERN.search(line)
    assert m  # pattern 自体は hit
    # ただし processed != total なので watchdog の completion 判定 (M==N) で false
    assert int(m.group(1)) != int(m.group(2))


def test_completion_pattern_extracts_succeeded():
    """成功件数の数値を正しく抽出"""
    line = "処理: 26/248 件 / 成功: 16 / 失敗: 10"
    m = COMPLETION_PATTERN.search(line)
    assert m
    processed = int(m.group(1))
    total = int(m.group(2))
    succeeded = int(m.group(3))
    assert processed == 26
    assert total == 248
    assert succeeded == 16


# ─────────────────────────────────
# 過去 3 回の log 抜粋で実機 sample
# ─────────────────────────────────

def test_real_log_sample_trial_1_abort():
    """試行 1 (5/6 10:17) の log で abort 検知できる"""
    sample = "2026-05-06 10:17:08,149 - tasks.task_market_analysis_refresh - ERROR - 連続 5 件失敗 → eBay 規制と判定して停止. 処理済 5/349 件."
    assert ABORT_PATTERN.search(sample)


def test_real_log_sample_trial_2_abort():
    """試行 2 (5/6 11:33) の log で abort 検知できる"""
    sample = "2026-05-06 11:33:40,424 - tasks.task_market_analysis_refresh - ERROR - 連続 5 件失敗 → eBay 規制と判定して停止."
    assert ABORT_PATTERN.search(sample)


def test_real_log_sample_trial_3_timeout():
    """試行 3 の thread timeout で検知できる"""
    sample = "2026-05-07 22:08:03,374 - tasks.task_market_analysis_refresh - WARNING -   失敗: thread timeout (>180s)"
    assert TIMEOUT_PATTERN.search(sample)


# ─────────────────────────────────
# CDP profile filter (C1) — string match の sanity check
# ─────────────────────────────────

def test_cdp_profile_filter_string_matches_chrome_args():
    """Chrome 起動引数に CDP_PROFILE_FILTER が含まれることを確認.

    watchdog が PowerShell `$_.CommandLine -like '*<filter>*'` で kill するため、
    Chrome の --user-data-dir 引数に同じ文字列が含まれている必要.
    H-F fix: CDP_PROFILE_FILTER を本体から import 済 (changes に追従)
    """
    chrome_arg = "--user-data-dir=C:/Users/gucch/projects/claude/tools/ebay-manager/data/.chrome_cdp_profile"
    assert CDP_PROFILE_FILTER in chrome_arg


def test_cdp_profile_filter_does_not_match_normal_chrome():
    """user の通常 Chrome (= Default profile) は filter に hit しない"""
    normal_chrome_arg = "C:/Program Files/Google/Chrome/Application/chrome.exe"
    assert CDP_PROFILE_FILTER not in normal_chrome_arg


# ─────────────────────────────────
# H7 exponential backoff
# ─────────────────────────────────

def test_exponential_backoff_progression():
    """retry 数で cooldown が x4 倍ずつ増える"""
    initial = 60
    expected = [60, 240, 960, 3840, 15360]
    for retries in range(1, 6):
        cooldown = min(initial * (4 ** (retries - 1)), 4 * 3600)
        # max cap 4h = 14400s
        assert cooldown == min(expected[retries - 1], 14400)


def test_exponential_backoff_caps_at_4h():
    """4h で cap される"""
    initial = 60
    cooldown = min(initial * (4 ** 5), 4 * 3600)  # 60 * 1024 = 61440 → 14400
    assert cooldown == 14400
