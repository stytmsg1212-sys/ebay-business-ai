# -*- coding: utf-8 -*-
"""W288/W290/W291 sourcing エンジン修正の回帰テスト (2026-06-27)。

出典: Phase 0 診断 (真因A: 英語タイトル直投げ / 真因B,D: 最安誤選択 / 真因C: Section232 未控除)。
code-reviewer HIGH-1 (永続テスト欠落) + Fugu M1 (substring 誤ヒット) 対応。
純関数中心で API 不要。compute_profit_true_for_research は settings.json + 送料 CSV を読む。
"""
from monitor.research_poc import (
    _best_hit,
    _query_overlap_score,
    compute_profit_true_for_research,
    FreemarketHit,
)
from monitor.research_section232 import estimate_section232


# =============================================================================
# W291: _query_overlap_score (token 集合交差・substring 誤ヒット根治)
# =============================================================================

def test_overlap_exact_token_match():
    # Brand+型番が全 token 一致
    assert _query_overlap_score("Roland HDP-88DLE", "Roland HDP-88DLE 内蔵HDD VS-880") == 3
    assert _query_overlap_score("SONY WM-DD9", "sony wm-dd9 本体 美品") == 3


def test_overlap_rejects_substring_false_match():
    # Fugu M1 実証ケース: 旧 substring 実装では誤ヒットしていたもの
    # "150" は純数字で除外 → "LH150" に誤ヒットしない (別 headshell)
    assert _query_overlap_score("AT 150", "Naga AT-LH150 cartridge") == 1   # at のみ
    # "zx7" != "zx707" (token 完全一致なので部分一致しない)
    assert _query_overlap_score("sony zx7", "SONY NW-ZX707 player") == 1     # sony のみ
    # "7" は len<2 で除外 → "700" に誤ヒットしない
    assert _query_overlap_score("7", "Model 700 amplifier") == 0


def test_overlap_empty_and_cjk():
    assert _query_overlap_score("", "anything") == 0
    assert _query_overlap_score("query", "") == 0
    # CJK 混じりから latin/数字 run を抽出 ("内蔵HDD" → "hdd")
    assert _query_overlap_score("HDD", "内蔵HDD ケース") == 1


# =============================================================================
# W291: _best_hit (最安固定 → match 優先 + 最安 tiebreak)
# =============================================================================

def test_best_hit_prefers_match_over_cheapest():
    # 真因B/D: 旧実装は最安(¥21 のゴミ)を掴んでいた
    hits = [
        FreemarketHit("mercari", "u_junk", "シール ステッカー おまけ", 21),
        FreemarketHit("yahoo", "u_correct", "SONY WM-DD9 本体 動作品", 8000),
    ]
    assert _best_hit(hits, "SONY WM-DD9").url == "u_correct"


def test_best_hit_tie_breaks_by_cheapest():
    hits = [
        FreemarketHit("m", "a", "SONY WM-DD9 本体", 12000),
        FreemarketHit("m", "b", "SONY WM-DD9 本体 美品", 9000),
    ]
    assert _best_hit(hits, "SONY WM-DD9").url == "b"


def test_best_hit_empty_query_backward_compat_cheapest():
    hits = [
        FreemarketHit("m", "a", "X", 500),
        FreemarketHit("m", "b", "Y", 300),
    ]
    assert _best_hit(hits, "").url == "b"    # query 空 → 最安 (後方互換)
    assert _best_hit(hits).url == "b"        # query 省略時も同じ


def test_best_hit_skips_invalid_price():
    hits = [
        FreemarketHit("m", "a", "SONY WM-DD9", None),
        FreemarketHit("m", "b", "SONY WM-DD9", 0),
        FreemarketHit("m", "c", "無関係 商品", 700),
    ]
    assert _best_hit(hits, "SONY WM-DD9").url == "c"   # 価格有効のみ対象
    assert _best_hit([], "q") is None


# =============================================================================
# W290: Section232 実関税が利益から控除される (money-direct)
# =============================================================================

def test_section232_estimator_fires_on_japanese():
    assert estimate_section232("南部鉄器 鉄瓶 急須")["annex"] == "I-A"
    assert estimate_section232("象印 炊飯器 5.5合")["annex"] == "I-B"
    # 英語タイトルのみは日本語辞書に当たらない (= best.title 和文併合が必要な理由)
    assert estimate_section232("HIOKI 3280-10F AC Clamp Meter")["flag"] is False


def test_section232_duty_reduces_profit():
    common = dict(terapeak_avg_price_usd=120.0, purchase_yen=3000, manual_weight_g=2000.0)
    p_none = compute_profit_true_for_research(**common, actual_duty_rate=None)
    p_ib = compute_profit_true_for_research(**common, actual_duty_rate=0.25)
    assert p_none[0] is not None and p_ib[0] is not None
    assert p_ib[0] < p_none[0]   # 実関税 25% 控除で利益が下がる (過大計上根治)


def test_actual_duty_rate_none_equals_omitted():
    # 後方互換: None は引数省略と数学的完全一致
    common = dict(terapeak_avg_price_usd=120.0, purchase_yen=3000, manual_weight_g=2000.0)
    assert (
        compute_profit_true_for_research(**common, actual_duty_rate=None)[0]
        == compute_profit_true_for_research(**common)[0]
    )
