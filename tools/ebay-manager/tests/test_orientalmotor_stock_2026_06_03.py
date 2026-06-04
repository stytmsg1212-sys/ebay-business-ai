"""オリエンタルモーター WEB ショップ専用在庫判定のテスト (2026-06-03、option B)。

受注生産型で在庫切れ状態がほぼ無く、「出荷日X日（数量: N～M）」シグナルでのみ
注文可否を判定する。「在庫なし」「404」「生産終了」テキストは在庫ありページにも
常駐する罠なので、それらに釣られないことを担保 (money-direct: 誤判定でオーバーセル)。

code-review 2026-06-03 の HIGH 3 件:
- HIGH-1: signal absent を即 unavailable 確定すると部分取得/anti-bot で false-OOS →
  本文体裁 sanity check で取得異常は None (fallback) に逃がす。
- HIGH-2: 実 HTTP デコード経路 (resp.text) で日本語 signal が available になる回帰。
- HIGH-3: 出荷日 と （数量 の間に HTML タグが挟まっても検出 (tag_tolerant 正規表現)。

出典: 2026-06-03 Playwright 実機調査 (在庫あり21件で signal present / 404スタブで absent)。
"""
from unittest.mock import patch

import httpx

from monitor.scrapers import _detect_orientalmotor_status, _check_with_httpx

_OM_URL = "https://www.orientalmotor-shop.jp/products/BMUD200-A/"
# 本文体裁 sanity check (orientalmotor 含む + len>2000) を満たす padding。
_PAGE = "orientalmotor product page " * 100  # > 2000 chars, contains 'orientalmotor'


def test_in_stock_signal_returns_available():
    """「出荷日X日（数量: N～M）」present -> available."""
    html = _PAGE + "<span>出荷日5日（数量: 1～5）</span><a>カートに追加</a>"
    assert _detect_orientalmotor_status(_OM_URL, html) == "available"


def test_full_page_no_signal_returns_unavailable():
    """正常取得できた商品ページで signal absent -> unavailable (削除/生産終了/受注停止)."""
    html = _PAGE + "<div>この製品は受注を終了しました</div>"
    assert _detect_orientalmotor_status(_OM_URL, html) == "unavailable"


def test_truncated_html_returns_none_not_oos():
    """HIGH-1: 部分取得/空応答 (本文不十分) は unavailable 確定せず None (fallback)."""
    assert _detect_orientalmotor_status(_OM_URL, "<html></html>") is None
    assert _detect_orientalmotor_status(_OM_URL, "") is None


def test_trap_zaiko_nashi_with_signal_still_available():
    """罠: 在庫ありでも HTML に「在庫なし」が常駐。signal あれば available を維持."""
    html = _PAGE + '<script>var t="在庫なし";</script><span>出荷日5日（数量: 1～5）</span>'
    assert _detect_orientalmotor_status(_OM_URL, html) == "available"


def test_trap_404_text_without_signal_is_unavailable():
    """罠: 「404」「生産終了」テキストは全ページ常駐。signal 無ければ unavailable (本文OK時)."""
    html = _PAGE + '<script>errorCode:"404"</script><div>生産終了の表記もテンプレに常駐</div>'
    assert _detect_orientalmotor_status(_OM_URL, html) == "unavailable"


def test_tag_split_signal_still_detected():
    """HIGH-3: 出荷日 と （数量 の間に HTML タグが挟まっても検出 (tag_tolerant)."""
    html = _PAGE + "<span>出荷日5日</span><span>（数量: 1～5）</span>"
    assert _detect_orientalmotor_status(_OM_URL, html) == "available"


def test_non_orientalmotor_url_returns_none():
    """orientalmotor 以外の URL は None (Playwright/汎用 fallback へ委譲)."""
    assert _detect_orientalmotor_status(
        "https://item.rakuten.co.jp/foo/bar/", _PAGE + "出荷日5日（数量: 1）") is None


def test_signal_variants():
    """出荷日の日数/数量レンジが変わっても検出 (出荷日N日（数量: …）)."""
    for h in [
        "出荷日10日（数量: 1～10）",
        "出荷日3日（数量: 1～2）",
        "出荷日 5日 （数量: 1～5）",
    ]:
        assert _detect_orientalmotor_status(_OM_URL, _PAGE + h) == "available", h


def test_httpx_decodes_japanese_signal():
    """HIGH-2: 実 HTTP 経路 (resp.text) で UTF-8 日本語 signal が available。
    Shift_JIS 文字化けで全件 false-OOS になる回帰を防ぐ。"""
    raw = (_PAGE + "出荷日5日（数量: 1～5）").encode("utf-8")
    resp = httpx.Response(200, content=raw,
                          headers={"Content-Type": "text/html; charset=UTF-8"})
    with patch("httpx.get", return_value=resp):
        assert _check_with_httpx(_OM_URL, [], [], []) == "available"
