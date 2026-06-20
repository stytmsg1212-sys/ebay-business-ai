"""W#33 — _keyword_watch_match 単体テスト."""
from __future__ import annotations

import pytest

from tabs._keyword_watch_match import (
    LegacyEntry,
    ListingEntry,
    _normalize,
    _tokenize,
    _score,
    match_legacy_to_listings,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _leg(keyword: str, legacy_id: int = 1, site: str = "yahoo_auctions",
         url: str = "https://example.com") -> LegacyEntry:
    return LegacyEntry(
        legacy_id=legacy_id,
        site=site,
        search_url=url,
        keyword=keyword,
        price_min_jpy=None,
        price_max_jpy=None,
    )


def _lst(title: str, eid: str = "111222333444") -> ListingEntry:
    return ListingEntry(ebay_item_id=eid, title=title)


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_fullwidth_to_halfwidth(self):
        assert _normalize("ＡＢＣＤ") == "abcd"

    def test_lowercase(self):
        assert _normalize("Hello World") == "hello world"

    def test_symbols_replaced(self):
        result = _normalize("A&B (C)")
        assert "&" not in result
        assert "(" not in result

    def test_whitespace_collapsed(self):
        assert _normalize("a   b") == "a b"


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_splits_on_space(self):
        assert "audio" in _tokenize("Audio Technica")
        assert "technica" in _tokenize("Audio Technica")

    def test_single_char_excluded(self):
        tokens = _tokenize("a b c abc")
        assert "a" not in tokens
        assert "abc" in tokens

    def test_empty(self):
        assert _tokenize("") == []


# ---------------------------------------------------------------------------
# _score
# ---------------------------------------------------------------------------

class TestScore:
    def test_full_match(self):
        score, matched = _score(["sony", "wh1000xm5"], ["sony", "wh1000xm5", "headphones"])
        assert score >= 1.0
        assert len(matched) == 2

    def test_partial_match(self):
        # "wh1000" is substring of "wh1000xm5"
        score, matched = _score(["wh1000"], ["sony", "wh1000xm5"])
        assert score > 0
        assert "wh1000" in matched

    def test_no_match(self):
        score, matched = _score(["camera"], ["headphones", "earbuds"])
        assert score == 0.0
        assert matched == []

    def test_empty_legacy(self):
        score, _ = _score([], ["sony", "headphones"])
        assert score == 0.0

    def test_score_capped_at_1(self):
        # bonus might push over 1 — should be capped
        tokens = ["a1", "b2", "c3", "d4", "e5"]
        score, _ = _score(tokens, tokens)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# match_legacy_to_listings
# ---------------------------------------------------------------------------

class TestMatchLegacyToListings:
    def test_best_match_selected(self):
        legacy = [_leg("Astell Kern SR35")]
        listings = [
            _lst("Some Random Product", eid="111"),
            _lst("Astell&Kern A&norma SR35 Portable Music Player", eid="222"),
        ]
        results = match_legacy_to_listings(legacy, listings)
        assert len(results) == 1
        assert results[0].listing.ebay_item_id == "222"
        assert results[0].score > 0.5

    def test_empty_listings(self):
        legacy = [_leg("Sony WH-1000XM5")]
        results = match_legacy_to_listings(legacy, [])
        assert results == []

    def test_empty_legacy(self):
        results = match_legacy_to_listings([], [_lst("Sony headphones")])
        assert results == []

    def test_score_threshold_filters(self):
        legacy = [_leg("totally unrelated keyword xyz")]
        listings = [_lst("Yamaha Piano", eid="333")]
        results = match_legacy_to_listings(legacy, listings, score_threshold=0.5)
        assert results == []

    def test_threshold_zero_includes_low_score(self):
        legacy = [_leg("audio xyz")]
        listings = [_lst("Audio Technica headphones", eid="444")]
        results = match_legacy_to_listings(legacy, listings, score_threshold=0.0)
        # "audio" matches, "xyz" doesn't → score > 0
        assert len(results) == 1

    def test_sorted_by_score_desc(self):
        legacy = [
            _leg("Sony WH-1000XM5 headphones", legacy_id=1),
            _leg("KEYENCE Sensor", legacy_id=2),
        ]
        listings = [
            _lst("Sony WH-1000XM5 Wireless Headphone Japan", eid="eid1"),
            _lst("KEYENCE LR-ZB250CB Sensor", eid="eid2"),
        ]
        results = match_legacy_to_listings(legacy, listings)
        assert len(results) == 2
        # Both should be reasonably matched; just verify sort
        assert results[0].score >= results[1].score

    def test_keyword_with_symbols_normalized(self):
        # Keyword "Astell&Kern A&norma" should normalize & to space
        legacy = [_leg("Astell&Kern A&norma SR35")]
        listings = [_lst("Astell Kern SR35 Player", eid="555")]
        results = match_legacy_to_listings(legacy, listings)
        assert results[0].score > 0.0

    def test_returns_one_result_per_legacy(self):
        # Each legacy should have exactly 1 best match
        legacy = [_leg("Sony", legacy_id=1), _leg("Canon", legacy_id=2)]
        listings = [
            _lst("Sony WH-1000XM5", eid="e1"),
            _lst("Canon EOS R5 Camera", eid="e2"),
        ]
        results = match_legacy_to_listings(legacy, listings)
        assert len(results) == 2

    def test_ebay_item_id_used_not_title(self):
        """マッチング結果の識別子は ebay_item_id であることを確認."""
        legacy = [_leg("Sony headphones")]
        listings = [_lst("Sony WH-1000XM5 Wireless Headphone", eid="999888777666")]
        results = match_legacy_to_listings(legacy, listings)
        assert results[0].listing.ebay_item_id == "999888777666"
