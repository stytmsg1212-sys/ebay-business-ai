"""メルカリショップ SKU 判別ロジックの unit test.

対象:
- sku_mapping_manager.is_mercari_shops_item_id
- sku_mapping_manager.generate_url (ebayme_ shops / normal 分岐)
- monitor/scrapers.prepare_batch_items (config 選択の正誤)
- monitor/database.find_site_config_by_url (longest-keyword-match, shops/通常 分離)
- regression: `ebayme_<数字>` と `ebayme_m<数字>` は両方とも通常メルカリ

判別ルール (URL 形式ベース):
  fullmatch(r'm?\\d+', item_id) 成立 → 通常メルカリ (.../item/m<...>)
  それ以外の非空文字列                → メルカリショップ (.../shops/product/<英数字>)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sku_mapping_manager import is_mercari_shops_item_id, generate_url


# ---------------------------------------------------------------------------
# is_mercari_shops_item_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item_id,expected", [
    # 全数字 = 通常メルカリ → False
    ("123456", False),
    ("44731528581", False),
    ("0", False),
    # m<digits> = 通常メルカリ (URL 側 m を SKU に含めた表記) → False
    # 実 DB 実在値 (2026-07 monitored_items):
    ("m95434266490", False),
    ("m81786287162", False),
    ("m32400850054", False),
    # 英字を含む (m<非数字> / 大文字英字 / 記号) = ショップ → True
    ("2JNdrTRFyn3rAX2enYZwkZ", True),
    ("abc", True),
    ("1a2b3c", True),
    ("A", True),
    ("mABC123", True),  # m の後が非数字含む = shops
    # 空文字列 → False
    ("", False),
])
def test_is_mercari_shops_item_id(item_id: str, expected: bool) -> None:
    assert is_mercari_shops_item_id(item_id) is expected


# ---------------------------------------------------------------------------
# generate_url
# ---------------------------------------------------------------------------

def test_generate_url_ebayme_digit_returns_normal_mercari() -> None:
    """ebayme_ + 数字 → 通常メルカリ URL"""
    url = generate_url("ebayme_", "123456")
    assert url == "https://jp.mercari.com/item/m123456", f"got {url}"


def test_generate_url_ebayme_mform_returns_normal_mercari() -> None:
    """ebayme_ + m<数字> は通常メルカリ URL (SKU に m を含めた歴史的表記)。

    誤って shops URL を返すと 404 スクレイプ → 在庫判定失敗。
    さらに URL は **単一 m** (`.../item/m<数字>`) でなければならない。
    二重 m (`.../item/mm<数字>`) は 404 となり sold-out 誤判定を招く
    (Codex HIGH 2026-07-02)。実 DB `m95434266490`。
    """
    url = generate_url("ebayme_", "m95434266490")
    assert url == "https://jp.mercari.com/item/m95434266490", (
        f"二重 m or shops 誤ルーティング: {url}"
    )


def test_generate_url_ebayme_bare_digit_unchanged() -> None:
    """既存 107 件形式 `ebayme_<数字>` (m 無し) は従来どおり `.../item/m<数字>`。

    HIGH/二重m修正で regression が起きていないことの証明。
    """
    url = generate_url("ebayme_", "95434266490")
    assert url == "https://jp.mercari.com/item/m95434266490", f"got {url}"


def test_generate_url_ebayme_alpha_returns_shops_url() -> None:
    """ebayme_ + 英字含む → メルカリショップ URL"""
    url = generate_url("ebayme_", "2JNdrTRFyn3rAX2enYZwkZ")
    assert url == "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ", f"got {url}"


def test_generate_url_ebayMS_unaffected() -> None:
    """ebayMS_ は従来どおりショップ URL"""
    url = generate_url("ebayMS_", "someShopId")
    assert url == "https://jp.mercari.com/shops/product/someShopId", f"got {url}"


def test_generate_url_ebayyh_unaffected() -> None:
    """ヤフオク prefix は変更なし"""
    url = generate_url("ebayyh_", "x1137149904")
    assert url is not None
    assert "x1137149904" in url


# ---------------------------------------------------------------------------
# prepare_batch_items — config 選択の正誤
# ---------------------------------------------------------------------------

MERCARI_CFG = {
    "convert_url": "ebayme_",
    "site_name": "メルカリ",
    "in_stock_text1": "購入手続きへ",
    "in_stock_text2": "",
    "sold_out_text": "売り切れました",
    "no_page_text": "このページは",
    "url_keyword": "mercari.com/item/",
}

SHOPS_CFG = {
    "convert_url": "ebayMS_",
    "site_name": "メルカリショップ",
    "in_stock_text1": "購入手続きへ",
    "in_stock_text2": "",
    "sold_out_text": "売り切れ",
    "no_page_text": "このページは",
    "url_keyword": "mercari.com/shops/",
}


def _make_configs_by_prefix():
    return {
        "ebayme_": MERCARI_CFG,
        "ebayMS_": SHOPS_CFG,
    }


def test_prepare_batch_items_digit_uses_normal_mercari_config() -> None:
    """ebayme_数字 → 通常メルカリ設定 (sold_out='売り切れました')"""
    from monitor.scrapers import prepare_batch_items

    items = [
        {
            "id": 1,
            "sku": "ebayme_44731528581",
            "source_url": "https://jp.mercari.com/item/m44731528581",
        }
    ]
    batch = prepare_batch_items(items, _make_configs_by_prefix())
    assert len(batch) == 1
    assert batch[0]["sold_out"] == ["売り切れました"], f"got {batch[0]['sold_out']}"


def test_prepare_batch_items_alpha_uses_shops_config() -> None:
    """ebayme_英数字 → メルカリショップ設定 (sold_out='売り切れ')"""
    from monitor.scrapers import prepare_batch_items

    items = [
        {
            "id": 2,
            "sku": "ebayme_2JNdrTRFyn3rAX2enYZwkZ",
            "source_url": "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ",
        }
    ]
    batch = prepare_batch_items(items, _make_configs_by_prefix())
    assert len(batch) == 1
    assert batch[0]["sold_out"] == ["売り切れ"], f"got {batch[0]['sold_out']}"


def test_prepare_batch_items_alpha_no_shops_cfg_drops_to_no_config() -> None:
    """ebayme_英数字 で ebayMS_ 未登録 → dropped_no_config に落とす (Q0 silent skip 防止)"""
    from monitor.scrapers import prepare_batch_items

    items = [
        {
            "id": 3,
            "sku": "ebayme_2JNdrTRFyn3rAX2enYZwkZ",
            "source_url": "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ",
        }
    ]
    # ebayMS_ を意図的に除いた configs
    cfgs = {"ebayme_": MERCARI_CFG}
    batch = prepare_batch_items(items, cfgs)
    assert len(batch) == 0  # dropped (ebayMS_ 設定なし)


# ---------------------------------------------------------------------------
# regression: 既存 ebayme_<数字> が通常メルカリ設定のまま
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("digit_sku", [
    "ebayme_44731528581",
    "ebayme_12345678901",
    "ebayme_99999999999",
    "ebayme_1",
    "ebayme_0",
    # m<digits> 形式も通常メルカリ (実 DB 存在): 前 assistant 版で shops 誤判定 →
    # ここで機械的に regression 捕捉する。
    "ebayme_m95434266490",
    "ebayme_m81786287162",
    "ebayme_m32400850054",
])
def test_regression_digit_ebayme_normal_config(digit_sku: str) -> None:
    """ebayme_<数字> / ebayme_m<数字> は通常メルカリ設定を使う (regression)"""
    from monitor.scrapers import prepare_batch_items

    item_id = digit_sku[len("ebayme_"):]
    items = [
        {
            "id": 99,
            "sku": digit_sku,
            "source_url": f"https://jp.mercari.com/item/m{item_id}",
        }
    ]
    batch = prepare_batch_items(items, _make_configs_by_prefix())
    assert len(batch) == 1, f"{digit_sku} が batch から落ちた"
    assert batch[0]["sold_out"] == ["売り切れました"], (
        f"{digit_sku}: 通常メルカリ設定が使われていない"
    )


# ---------------------------------------------------------------------------
# _build_source_url_from_sku (database module): m-form 経路確認
# ---------------------------------------------------------------------------

def test_build_source_url_from_sku_m_form_normal_mercari() -> None:
    """`ebayme_m95434266490` の URL 生成が shops 経路に流れず、**単一 m** 化される。

    二重 m 防止 (Codex HIGH 2026-07-02)。
    """
    from monitor.database import _build_source_url_from_sku
    url = _build_source_url_from_sku("ebayme_m95434266490")
    assert url == "https://jp.mercari.com/item/m95434266490", (
        f"二重 m or shops 誤ルーティング: {url}"
    )


def test_build_source_url_from_sku_bare_digit_unchanged() -> None:
    """既存 107 件形式 `ebayme_<数字>` は従来どおり `.../item/m<数字>` (regression)."""
    from monitor.database import _build_source_url_from_sku
    url = _build_source_url_from_sku("ebayme_95434266490")
    assert url == "https://jp.mercari.com/item/m95434266490", f"got {url}"


def test_build_source_url_from_sku_alpha_shops() -> None:
    """`ebayme_2JN...` の URL 生成が shops 経路に流れること。"""
    from monitor.database import _build_source_url_from_sku
    url = _build_source_url_from_sku("ebayme_2JNdrTRFyn3rAX2enYZwkZ")
    assert url == "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ", (
        f"got {url}"
    )


# ---------------------------------------------------------------------------
# find_site_config_by_url — longest-keyword-match (money-direct)
# ---------------------------------------------------------------------------

def test_find_site_config_by_url_shops_wins_over_mercari() -> None:
    """shops URL は shops 設定 (sold_out='売り切れ') を返す。

    現行 DB は url_keyword 'mercari' (id=1) が 'jp.mercari.com/shops' (id=2)
    より先に登録されている。DB 順の先頭一致だと通常メルカリ設定が横取りし、
    shops 側売切見逃し → 売切候補採用リスク。longest-match で防ぐ。
    """
    from monitor.database import find_site_config_by_url, init_db
    init_db()
    cfg = find_site_config_by_url(
        "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ"
    )
    assert cfg is not None, "shops URL が site_config 未解決"
    assert cfg.get("convert_url") == "ebayMS_", (
        f"shops URL がメルカリショップ設定を返さない: {cfg.get('convert_url')} "
        f"(site_name={cfg.get('site_name')})"
    )
    assert cfg.get("sold_out_text") == "売り切れ", (
        f"sold_out_text が shops 用でない: {cfg.get('sold_out_text')}"
    )


def test_find_site_config_by_url_regular_mercari_still_matches() -> None:
    """通常メルカリ URL は従来どおりメルカリ設定 (sold_out='売り切れました')。"""
    from monitor.database import find_site_config_by_url, init_db
    init_db()
    cfg = find_site_config_by_url("https://jp.mercari.com/item/m44731528581")
    assert cfg is not None
    assert cfg.get("convert_url") == "ebayme_", (
        f"通常メルカリ URL がメルカリ設定を返さない: {cfg.get('convert_url')}"
    )
    assert cfg.get("sold_out_text") == "売り切れました"


# ---------------------------------------------------------------------------
# _check_via_site_configs — longest-match で shops URL がメルカリショップ設定を使う
# (money-direct: W182 仕入先候補在庫 gate)
# ---------------------------------------------------------------------------

def test_check_via_site_configs_shops_url_picks_shops_config(monkeypatch) -> None:
    """`_check_via_site_configs(shops URL)` がメルカリショップ設定 (sold_out='売り切れ')
    を選ぶこと。DB 順先頭一致では通常メルカリ設定 (`mercari`, sold_out='売り切れました')
    が横取りしていた (Codex MEDIUM 2026-07-02)。
    """
    from monitor.database import init_db
    import monitor.scrapers as scrapers_mod
    init_db()

    captured = {}

    def _fake_httpx(url, in_stock, sold_out, no_page):
        captured["url"] = url
        captured["in_stock"] = in_stock
        captured["sold_out"] = sold_out
        captured["no_page"] = no_page
        return "unavailable"

    monkeypatch.setattr(scrapers_mod, "_check_with_httpx", _fake_httpx)
    result = scrapers_mod._check_via_site_configs(
        "https://jp.mercari.com/shops/product/2JNdrTRFyn3rAX2enYZwkZ",
        timeout_sec=5,
        checked_at="2026-07-02T00:00:00",
    )
    assert result["signal"] == "site_config: メルカリショップ", (
        f"shops URL がメルカリショップ設定を選ばない: signal={result['signal']}"
    )
    assert captured["sold_out"] == ["売り切れ"], (
        f"shops sold_out ('売り切れ') が使われていない: got {captured['sold_out']}"
    )


def test_check_via_site_configs_regular_mercari_still_normal(monkeypatch) -> None:
    """通常メルカリ URL は従来どおりメルカリ設定 (sold_out='売り切れました')."""
    from monitor.database import init_db
    import monitor.scrapers as scrapers_mod
    init_db()

    captured = {}

    def _fake_httpx(url, in_stock, sold_out, no_page):
        captured["sold_out"] = sold_out
        return "available"

    monkeypatch.setattr(scrapers_mod, "_check_with_httpx", _fake_httpx)
    result = scrapers_mod._check_via_site_configs(
        "https://jp.mercari.com/item/m44731528581",
        timeout_sec=5,
        checked_at="2026-07-02T00:00:00",
    )
    assert result["signal"] == "site_config: メルカリ", (
        f"通常メルカリ URL がメルカリ設定を選ばない: signal={result['signal']}"
    )
    assert captured["sold_out"] == ["売り切れました"]
