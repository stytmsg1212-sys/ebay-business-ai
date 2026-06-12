"""依頼ボード#14 (2026-06-12): PayPay フリマ売り切れ検知もれの回帰テスト.

事故: item 358602209000 (KEYENCE KV-XLE02, sku=ebayPF_r1226199554) の仕入先が
5/7 に売り切れたのに、定時在庫監視は source_status='不明' のまま 1 か月以上検知漏れ。

真因: _check_with_httpx は site_configs シグナル (『関連商品をアプリで探す』
『購入手続きへ』= JS 描画後にしか出ない) だけで判定し、raw HTML に server-side で
必ず入る確実シグナル (購入日時 / "SoldOut" JSON-LD) は W182 候補ゲート
(_check_paypay_availability) にしか配線されていなかった。

修正: _detect_paypay_signals に判定を一元化し、_check_with_httpx (定時監視) と
_check_paypay_availability (W182 ゲート) の両方から使う。

実機検証 (Q1) は本番 URL https://paypayfleamarket.yahoo.co.jp/item/r1226199554 で
別途実施 (raw HTML: JSON-LD availability="SoldOut" + 別 JSON-LD に schema.org/InStock
が混在 = InStock を在庫シグナルにすると誤判定する実例)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.scrapers import (  # noqa: E402
    _check_with_httpx,
    _detect_paypay_signals,
)

PAYPAY_URL = "https://paypayfleamarket.yahoo.co.jp/item/r1226199554"

# 実ページ構造の最小再現: 売切ページは本体 JSON-LD が "SoldOut"、
# かつ別 JSON-LD に schema.org/InStock が残る (2026-06-12 実機確認)
SOLDOUT_HTML = (
    '<html><script type="application/ld+json">'
    '{"@type":"Product","offers":{"@type":"Offer","availability":"SoldOut",'
    '"price":31000,"priceCurrency":"JPY"}}</script>'
    '<script type="application/ld+json">'
    '{"availabilityStarts":"2026-05-07T22:45:28+09:00",'
    '"availability":"http://schema.org/InStock"}</script></html>'
)
AVAILABLE_HTML = "<html><body><button>購入手続きへ</button></body></html>"
PURCHASED_HTML = "<html><body>購入日時: 2026年5月7日</body></html>"
NO_PAGE_HTML = "<html><body>この商品は存在しません</body></html>"
NO_SIGNAL_HTML = "<html><body>loading...</body></html>"

# site_configs id=4 (Paypayフリマ) 相当のシグナル (JS 描画後にしか出ない弱シグナル)
SITE_IN_STOCK = ["購入手続きへ", ""]
SITE_SOLD_OUT = ["関連商品をアプリで探す"]
SITE_NO_PAGE = ["この商品は存在しません"]


# ---- _detect_paypay_signals 単体 ----

@pytest.mark.parametrize(
    "html, expected_status, expected_signal",
    [
        (SOLDOUT_HTML, "unavailable", "SoldOut JSON-LD"),
        (PURCHASED_HTML, "unavailable", "購入日時 in HTML"),
        ("<html>関連商品をアプリで探す</html>", "unavailable", "related items text"),
        (AVAILABLE_HTML, "available", "購入手続きへ"),
        (NO_PAGE_HTML, "not_found", "no_page_text"),
        (NO_SIGNAL_HTML, None, "no signal matched"),
    ],
)
def test_detect_paypay_signals(html, expected_status, expected_signal):
    status, signal = _detect_paypay_signals(html)
    assert status == expected_status
    assert signal == expected_signal


def test_soldout_json_ld_beats_instock_decoy():
    """売切ページの schema.org/InStock 残存 (実機確認済) に騙されないこと."""
    status, _ = _detect_paypay_signals(SOLDOUT_HTML)
    assert status == "unavailable"
    assert "InStock" in SOLDOUT_HTML  # decoy が実在する前提の確認


def test_sold_signal_beats_available_signal():
    """MED-2 (code-review 2026-06-12): sold シグナル優先順序の pin.

    売切ページに『購入手続きへ』がテンプレ残存しても unavailable に倒れること
    (将来シグナル並べ替えで false-available 化したら本テストが検知する)。
    """
    html = SOLDOUT_HTML.replace("</html>", "<button>購入手続きへ</button></html>")
    assert _detect_paypay_signals(html)[0] == "unavailable"


# ---- _check_with_httpx への配線 (定時在庫監視経路 = 本事故の回帰) ----

def _mock_response(html: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


def test_httpx_path_detects_paypay_soldout():
    """回帰本体: 売切 raw HTML で 'unavailable' 確定 (旧実装は None → 不明 stuck)."""
    with patch("monitor.scrapers.httpx.get", return_value=_mock_response(SOLDOUT_HTML)):
        result = _check_with_httpx(
            PAYPAY_URL, SITE_IN_STOCK, SITE_SOLD_OUT, SITE_NO_PAGE
        )
    assert result == "unavailable"


def test_httpx_path_detects_paypay_available():
    """LOW-1 (code-review 2026-06-12): site_configs シグナルを空にして呼び、
    汎用判定ではなく PayPay 専用分岐が available を確定したことを証明する."""
    with patch("monitor.scrapers.httpx.get", return_value=_mock_response(AVAILABLE_HTML)):
        result = _check_with_httpx(PAYPAY_URL, [], [], [])
    assert result == "available"


def test_httpx_path_paypay_no_signal_falls_back():
    """シグナル無しは None (Playwright fallback 維持、誤確定を作らない / Q0)."""
    with patch("monitor.scrapers.httpx.get", return_value=_mock_response(NO_SIGNAL_HTML)):
        result = _check_with_httpx(
            PAYPAY_URL, SITE_IN_STOCK, SITE_SOLD_OUT, SITE_NO_PAGE
        )
    assert result is None


def test_httpx_path_non_paypay_url_unaffected():
    """非 PayPay URL は PayPay 判定を通らない (K2: 既存サイトの挙動不変)."""
    html = '<html>"SoldOut"</html>'  # PayPay シグナルだが他サイトでは無意味
    with patch("monitor.scrapers.httpx.get", return_value=_mock_response(html)):
        result = _check_with_httpx(
            "https://example.com/item/123", ["カートに入れる"], ["売り切れ"], ["404"]
        )
    assert result is None  # 旧来どおり判定不能 → fallback


# ---- W182 ゲート (_check_paypay_availability) の互換維持 ----

@pytest.mark.parametrize(
    "html, expected_status, expected_signal",
    [
        (SOLDOUT_HTML, "unavailable", "SoldOut JSON-LD"),
        (AVAILABLE_HTML, "available", "購入手続きへ"),
        (NO_PAGE_HTML, "not_found", "no_page_text"),
        (NO_SIGNAL_HTML, "unknown", "no signal matched"),
    ],
)
def test_w182_gate_signal_compat(html, expected_status, expected_signal):
    """共有ヘルパー化後も check_candidate_availability の status/signal が不変."""
    from monitor.scrapers import check_candidate_availability

    with patch("monitor.scrapers.httpx.get", return_value=_mock_response(html)):
        result = check_candidate_availability(PAYPAY_URL)
    assert result["status"] == expected_status
    assert result["signal"] == expected_signal
    assert result["checked_at"]


def test_w182_gate_http_404():
    with patch(
        "monitor.scrapers.httpx.get", return_value=_mock_response("", status_code=404)
    ):
        from monitor.scrapers import check_candidate_availability

        result = check_candidate_availability(PAYPAY_URL)
    assert result["status"] == "not_found"
    assert result["signal"] == "HTTP 404"
