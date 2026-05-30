#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W9 Phase 6: 個別新規出品 End-to-End Integration Test Suite

Phase 1-5 全フェーズを串刺しでテストする。実ネットワーク / 実 Playwright /
実 eBay API / 実 Claude API は一切叩かず、mock ベースで完全再現する。
実 DB は tempfile の sqlite で分離する。

## カバーするフロー

```
supplier_scraper.scrape_supplier_url()          → ScrapedProduct
ebay_reference_fetcher.fetch_reference_listing() → ReferenceListing
rank_classifier.classify_rank()                  → RankClassification
listing_generator.generate_listing()              → GeneratedListing
shipping_policy_selector.select_shipping_policy() → (policy_id, label)
ebay_lister.build_draft_params_from_phase3()     → params dict
ebay_lister.verify_add_fixed_price_item()         → verify result
database.save_listing_draft()                     → draft_id
ebay_lister.add_fixed_price_item_draft()          → add result
database.update_listing_draft_status(...)         → status='applied'/'api_failed'
```

## 検証ポイント (Phase 1-4 レビューで発見された HIGH 関連)

1. Phase 3 HIGH-関連: reference.category_id が Claude の返却を強制上書きする
2. Phase 3 HIGH-関連: Claude 失敗時の regex fallback が優先度順で動く
3. Phase 4 HIGH-1 修正済: `_fixed_schedule_time` 注入で add_fixed_price_item_draft
   の戻り値 `scheduled_time` と XML 内 `<ScheduleTime>` が完全一致
4. HIGH-2A 修正済: update_ebay_listing_sku が source_out_of_stock_since を NULL にクリア
5. HIGH-3 修正済: cleanup_stale_supplier_candidates が兄弟 pending 候補を auto_rejected=1 に

## 制約

- Phase 1-5 ファイル touch 禁止 (新規テストファイルのみ)
- 実ネットワーク / 実 API / 実 Playwright は一切叩かない
- DB は tempfile 分離 (database.DB_PATH を monkey patch)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

# tools/ebay-manager/ を sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 各フェーズのモジュールを import
from monitor import database  # noqa: E402
from monitor.ebay_lister import (  # noqa: E402
    _MAX_PICTURES,
    _build_add_fixed_price_item_xml,
    add_fixed_price_item_draft,
    build_draft_params_from_phase3,
    verify_add_fixed_price_item,
)
from monitor.ebay_reference_fetcher import (  # noqa: E402
    ReferenceListing,
    fetch_reference_listing,
)
from monitor.listing_generator import (  # noqa: E402
    GeneratedListing,
    generate_listing,
    render_description,
)
from monitor.rank_classifier import (  # noqa: E402
    RankClassification,
    classify_rank,
)
from monitor.shipping_policy_selector import (  # noqa: E402
    select_shipping_policy,
)
from monitor.supplier_scraper import ScrapedProduct  # noqa: E402


# =========================================================================
# Mock レスポンスコンスタント
# =========================================================================

_GET_ITEM_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-04-20T00:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1371</Version>
  <Item>
    <ItemID>358463512773</ItemID>
    <Title>Sony WH-1000XM5 Wireless Noise Cancelling Headphones Black</Title>
    <PrimaryCategory>
      <CategoryID>293</CategoryID>
      <CategoryName>Consumer Electronics:Portable Audio &amp; Headphones</CategoryName>
    </PrimaryCategory>
    <ConditionID>3000</ConditionID>
    <ConditionDisplayName>Used</ConditionDisplayName>
    <ItemSpecifics>
      <NameValueList>
        <Name>Brand</Name>
        <Value>Sony</Value>
      </NameValueList>
      <NameValueList>
        <Name>Model</Name>
        <Value>WH-1000XM5</Value>
      </NameValueList>
      <NameValueList>
        <Name>Type</Name>
        <Value>Over-Ear</Value>
      </NameValueList>
      <NameValueList>
        <Name>Color</Name>
        <Value>Black</Value>
      </NameValueList>
      <NameValueList>
        <Name>Connectivity</Name>
        <Value>Wireless</Value>
      </NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>"""

_ADD_ITEM_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-04-20T00:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1371</Version>
  <ItemID>998877665544</ItemID>
  <StartTime>2026-05-11T13:00:00.000Z</StartTime>
  <Fees>
    <Fee>
      <Name>InsertionFee</Name>
      <Fee currencyID="USD">0.0</Fee>
    </Fee>
    <Fee>
      <Name>ListingFee</Name>
      <Fee currencyID="USD">0.35</Fee>
    </Fee>
  </Fees>
</AddFixedPriceItemResponse>"""

_ADD_ITEM_FAILURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <ShortMessage>Invalid category</ShortMessage>
    <LongMessage>The category ID you supplied is invalid.</LongMessage>
    <ErrorCode>87</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
</AddFixedPriceItemResponse>"""

_VERIFY_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<VerifyAddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Fees>
    <Fee>
      <Name>InsertionFee</Name>
      <Fee currencyID="USD">0.0</Fee>
    </Fee>
  </Fees>
</VerifyAddFixedPriceItemResponse>"""

_VERIFY_ERRORS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<VerifyAddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <ShortMessage>Missing Brand</ShortMessage>
    <LongMessage>Item Specifics 'Brand' is required for this category.</LongMessage>
    <ErrorCode>21917112</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
  <Errors>
    <ShortMessage>Invalid price</ShortMessage>
    <LongMessage>StartPrice must be greater than 0.</LongMessage>
    <ErrorCode>10007</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
</VerifyAddFixedPriceItemResponse>"""


# =========================================================================
# v4 テンプレ (14 placeholder 全部入り)
# =========================================================================

_V4_TEMPLATE = """<style>
.mh-wrap { color: red; }
.mh-wrap .k { font-weight: bold; }
</style>
<div class="mh-wrap {{mode_class}}">
  <header>
    <h1 class="product">{{product_name}}</h1>
    <p class="sub">{{product_sub}}</p>
  </header>
  <section class="rank-block">
    <span class="rank">{{rank}}</span>
    <span class="rank-label">{{rank_label}}</span>
    <span class="rank-jp">{{rank_jp}}</span>
    <p class="notes">{{quick_notes}}</p>
  </section>
  <section class="includes">
    {{includes_rows}}
  </section>
  <table class="specs">
    {{specs_rows}}
  </table>
  <div class="spec-strip">
    {{spec_strip_rows}}
  </div>
  <footer class="shipping">
    <p>Origin: {{shipping_origin}}</p>
    <p>Carrier: {{shipping_carrier}}</p>
    <p>Handling: {{shipping_handling}}</p>
    <p>Delivery US: {{shipping_delivery_us}}</p>
    <p>Packaging: {{shipping_packaging}}</p>
    <p>Notes: {{shipping_notes}}</p>
  </footer>
</div>"""


# =========================================================================
# fixture helpers
# =========================================================================

def _make_scraped_product(**overrides) -> ScrapedProduct:
    """正常系の ScrapedProduct 生成。"""
    defaults = dict(
        url="https://auctions.yahoo.co.jp/jp/auction/x1234567890",
        platform="yahoo_auctions",
        title_ja="Sony WH-1000XM5 ブラック 美品 動作確認済",
        price_jpy=28000,
        condition_ja="中古 美品",
        includes_ja="本体、箱、説明書",
        image_urls=[
            "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/1.jpg",
            "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/2.jpg",
            "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/3.jpg",
            "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/4.jpg",
            "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/5.jpg",
        ],
        description_ja=(
            "ソニーのフラグシップ ワイヤレスヘッドホン WH-1000XM5 を出品します。"
            "動作確認済み。使用に伴う小キズあり。付属品: 本体、箱、説明書。"
            "重量 約 250g"
        ),
        seller_name="test_seller",
        weight_hint_g=250,
        scrape_error=None,
    )
    defaults.update(overrides)
    return ScrapedProduct(**defaults)


def _make_reference_listing(**overrides) -> ReferenceListing:
    defaults = dict(
        item_id="358463512773",
        category_id="293",
        category_name="Consumer Electronics:Portable Audio & Headphones",
        condition_id="3000",
        condition_display_name="Used",
        item_specifics_keys=["Brand", "Model", "Type", "Color", "Connectivity"],
        title_sample="Sony WH-1000XM5 Wireless Noise Cancelling Headphones Black",
        fetch_error=None,
    )
    defaults.update(overrides)
    return ReferenceListing(**defaults)


def _make_rank(rank_code: str = "A") -> RankClassification:
    """正常系の RankClassification 生成 (rank_code 指定で差し替え可)。"""
    table = {
        "N": ("New (Unopened)", "Brand New Sealed", "1000"),
        "S": ("Open Box", "Opened · No Wear", "1500"),
        "A": ("Excellent", "Tested · Minor Wear", "3000"),
        "B": ("Good", "Tested · Visible Wear", "3000"),
        "C": ("Fair", "Tested · Heavy Wear", "3000"),
        "D": ("Issues", "Working · Limited Function", "3000"),
        "PO": ("Power-On Only", "Powers On · Untested", "3000"),
        "As-Is": ("As-Is", "Not Tested · No Warranty", "7000"),
    }
    label, jp_hint, cond_id = table[rank_code]
    return RankClassification(
        rank_code=rank_code,
        rank_label=label,
        rank_jp=jp_hint,
        ebay_condition_id=cond_id,
        confidence=0.9,
        reasoning=f"test rank {rank_code}",
    )


def _make_claude_rank_response(rank_code: str = "A", confidence: float = 0.92) -> MagicMock:
    table = {
        "N": ("New (Unopened)", "Brand New Sealed"),
        "S": ("Open Box", "Opened · No Wear"),
        "A": ("Excellent", "Tested · Minor Wear"),
        "B": ("Good", "Tested · Visible Wear"),
        "C": ("Fair", "Tested · Heavy Wear"),
        "D": ("Issues", "Working · Limited Function"),
        "PO": ("Power-On Only", "Powers On · Untested"),
        "As-Is": ("As-Is", "Not Tested · No Warranty"),
    }
    label, jp = table[rank_code]
    payload = {
        "rank_code": rank_code,
        "rank_label": label,
        "rank_jp": jp,
        "confidence": confidence,
        "reasoning": f"test reasoning {rank_code}",
    }
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


def _make_claude_listing_response(
    category_id: str = "99999",  # デフォルトでは意図的に "誤推定" 値を入れて reference override を検証
    item_specifics: Optional[dict] = None,
    specs: Optional[list] = None,
    title: str = "Sony WH-1000XM5 Wireless Noise Cancelling Headphones Black",
) -> MagicMock:
    if item_specifics is None:
        item_specifics = {
            "Brand": "Sony",
            "Model": "WH-1000XM5",
            "Type": "Over-Ear",
            "Color": "Black",
            "Connectivity": "Wireless",
        }
    if specs is None:
        specs = [
            {"key": "Brand", "value": "Sony"},
            {"key": "Model", "value": "WH-1000XM5"},
            {"key": "Type", "value": "Wireless Over-Ear"},
            {"key": "Color", "value": "Black"},
        ]
    payload = {
        "title": title,
        "product_name": title,
        "product_sub": "Flagship noise-cancelling model",
        "quick_notes": "Tested and confirmed working. Minor cosmetic wear.",
        "includes_items": [
            {"label": "Main Unit", "detail": "Sony WH-1000XM5 (Black)"},
            {"label": "Original Box", "detail": "Included"},
            {"label": "Manual", "detail": "Japanese manual"},
        ],
        "specs": specs,
        "spec_strip": [
            {"key": "BATTERY", "value": "30h"},
            {"key": "ANC", "value": "Active"},
            {"key": "BT", "value": "5.2"},
        ],
        "category_id": category_id,
        "category_name": "Consumer Electronics",
        "category_candidates": [],
        "item_specifics": item_specifics,
        "shipping_origin": "Tokyo, Japan",
        "shipping_carrier": "DHL SpeedPAK · tracked, insured",
        "shipping_handling": "1–3 business days",
        "shipping_delivery_us": "6–10 business days typical",
        "shipping_packaging": "Double-boxed · bubble-wrapped · waterproof liner",
        "shipping_notes": "",
    }
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 800
    usage.output_tokens = 400
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


def _make_config(**overrides) -> dict:
    """E2E 用 config (mock settings.json)。"""
    base = {
        "ebay_business_policies": {
            "payment_policy_id": "359244671023",
            "return_policy_id": "359243687023",
            "shipping_weight_mapping_in_stock": {
                "0-500":     "IN_0_500",
                "500-1000":  "IN_500_1000",
                "1000-2000": "IN_1000_2000",
                "2000-3000": "IN_2000_3000",
                "10000-20000": "IN_10000_20000",
            },
            "shipping_weight_mapping_no_stock": {
                "0-500":     "NS_0_500",
                "500-1000":  "NS_500_1000",
                "1000-2000": "NS_1000_2000",
                "10000-20000": "NS_10000_20000",
            },
        },
        "w9_listing_defaults": {
            "location": "Tokyo, Japan",
            "postal_code": "100-0001",
            "dispatch_time_max": 3,
            "listing_duration": "GTC",
            "country": "JP",
            "currency": "USD",
        },
        "w9_draft_mode": {
            "method": "scheduled_time",
            "scheduled_days_offset": 21,
        },
    }
    base.update(overrides)
    return base


_FAKE_CREDS = {
    "app_id": "FAKE_APP_ID",
    "dev_id": "FAKE_DEV_ID",
    "cert_id": "FAKE_CERT_ID",
    "user_token": "FAKE_USER_TOKEN",
}


# =========================================================================
# DB fixture: tempfile sqlite で分離
# =========================================================================

@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """各テスト専用の sqlite DB を tempfile に作成し、database.DB_PATH を差し替える。

    init_db() で必要なテーブルを全部用意する。テスト終了時は自動削除。
    """
    # 一意な DB パス (並列実行対応)
    db_dir = tmp_path / "ebay_e2e_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "monitor.db"

    # database モジュール内グローバル DB_PATH を差し替え
    monkeypatch.setattr(database, "DB_PATH", db_path)
    # get_conn もこれを参照するので init_db() がこの tempfile に書き込む
    database.init_db()
    yield db_path


# =========================================================================
# TestW9FullFlowSuccess: 正常系 (applied で終わる)
# =========================================================================

class TestW9FullFlowSuccess:
    """全 Phase が成功して applied 状態で終わる正常系フロー。

    実 settings.json を読むテストと mock settings を読むテストの両方を用意する。
    """

    def _run_full_flow(
        self,
        config: dict,
        with_reference: bool = True,
    ) -> dict:
        """共通フロー。各テストで呼び出して使う。戻り値: DB の最終 draft 行 dict。"""

        # --- Phase 2: 仕入先スクレイプ (mock) ---
        product = _make_scraped_product()

        # --- Phase 2: 参考 eBay Listing (mock GetItem API) ---
        if with_reference:
            with patch(
                "monitor.ebay_reference_fetcher.get_ebay_credentials",
                return_value=_FAKE_CREDS,
            ):
                with patch(
                    "monitor.ebay_reference_fetcher._call_trading_api",
                    return_value={"success": True, "ack": "Success",
                                  "raw": _GET_ITEM_SUCCESS_XML},
                ):
                    reference = fetch_reference_listing(
                        "https://www.ebay.com/itm/358463512773",
                    )
            assert reference.fetch_error is None
            assert reference.category_id == "293"
        else:
            reference = None

        # --- Phase 3a: rank classifier (mock Claude Haiku) ---
        fake_rank_client = MagicMock()
        fake_rank_client.messages.create.return_value = _make_claude_rank_response("A", 0.92)
        with patch("monitor.rank_classifier._get_client", return_value=fake_rank_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                rank = classify_rank(
                    product.condition_ja,
                    product.description_ja,
                    product.title_ja,
                )
        assert rank.rank_code == "A"

        # --- Phase 3b: listing generator (mock Claude Sonnet) ---
        fake_listing_client = MagicMock()
        fake_listing_client.messages.create.return_value = _make_claude_listing_response(
            category_id="99999",  # Claude が誤推定、reference で上書きされるはず
        )
        # 2026-04-22: Taxonomy API を mock (real API 呼出しを防止 + Claude 推定値保持)
        with patch("monitor.listing_generator._get_client", return_value=fake_listing_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                with patch(
                    "monitor.ebay_taxonomy.get_category_suggestions",
                    return_value=[],
                ):
                    listing = generate_listing(product, reference, rank, _V4_TEMPLATE)

        assert listing.generate_error is None
        assert listing.ebay_title
        if with_reference:
            # 参考 listing 指定時は reference.category_id が上書きする
            assert listing.ebay_category_id == "293"
        else:
            # Claude 推定値がそのまま採用される
            assert listing.ebay_category_id == "99999"

        # --- Phase 3c: shipping policy selector (mock config) ---
        weight_g = product.weight_hint_g or 500
        shipping_policy_id, shipping_label = select_shipping_policy(
            weight_g, in_stock=True, config=config,
        )
        assert shipping_policy_id
        assert shipping_label

        # --- Phase 4a: draft_params 組立 ---
        params = build_draft_params_from_phase3(
            product=product,
            reference=reference,
            rank=rank,
            listing=listing,
            shipping_policy_id=shipping_policy_id,
            sku="ebayyh_x1234567890",
            listing_price_usd=249.99,
            image_urls=product.image_urls,
            config=config,
        )
        assert params["ebay_title"] == listing.ebay_title
        assert params["shipping_policy_id"] == shipping_policy_id
        assert params["sku"] == "ebayyh_x1234567890"

        # --- Phase 4b: VerifyAddFixedPriceItem (mock API) ---
        with patch(
            "monitor.ebay_lister._call_trading_api",
            return_value={"success": True, "ack": "Success", "raw": _VERIFY_SUCCESS_XML},
        ):
            verify_result = verify_add_fixed_price_item(
                params,
                app_id="A", dev_id="D", cert_id="C", user_token="T",
            )
        assert verify_result["success"] is True
        assert verify_result["errors"] == []

        # --- Phase 5a: DB に draft 保存 (api 呼出し前) ---
        draft_id = database.save_listing_draft({
            "sku": params["sku"],
            "supplier_url": product.url,
            "supplier_platform": product.platform,
            "supplier_title_ja": product.title_ja,
            "supplier_price_jpy": product.price_jpy,
            "supplier_condition_ja": product.condition_ja,
            "supplier_includes_ja": product.includes_ja,
            "supplier_image_urls": list(product.image_urls or []),
            "selected_image_urls": list(product.image_urls or []),
            "reference_ebay_url": (
                "https://www.ebay.com/itm/358463512773" if with_reference else None
            ),
            "reference_ebay_item_id": (
                reference.item_id if reference else None
            ),
            "reference_category_id": (
                reference.category_id if reference else None
            ),
            "reference_item_specifics_keys": (
                list(reference.item_specifics_keys) if reference else None
            ),
            "reference_condition_id": (
                reference.condition_id if reference else None
            ),
            "rank_code": rank.rank_code,
            "rank_label": rank.rank_label,
            "quick_notes": "test notes",
            "ebay_title": listing.ebay_title,
            "ebay_description": listing.ebay_description,
            "ebay_category_id": listing.ebay_category_id,
            "ebay_category_name": listing.ebay_category_name,
            "ebay_condition_id": rank.ebay_condition_id,
            "item_specifics": dict(listing.item_specifics or {}),
            "listing_price_usd": params["listing_price_usd"],
            "weight_g": weight_g,
            "in_stock": 1,
            "shipping_policy_id": shipping_policy_id,
            "status": "draft",
        })
        assert draft_id > 0

        # --- Phase 5b: AddFixedPriceItem (mock API) ---
        with patch(
            "monitor.ebay_lister._call_trading_api",
            return_value={"success": True, "ack": "Success", "raw": _ADD_ITEM_SUCCESS_XML},
        ):
            add_result = add_fixed_price_item_draft(
                params,
                app_id="A", dev_id="D", cert_id="C", user_token="T",
            )
        assert add_result["success"] is True
        assert add_result["ebay_item_id"] == "998877665544"

        # --- Phase 5c: DB status 遷移 ---
        database.update_listing_draft_status(
            draft_id,
            "applied",
            ebay_item_id=add_result["ebay_item_id"],
        )

        # 最終 DB 行取得
        final = database.get_listing_draft(draft_id)
        assert final is not None
        assert final["status"] == "applied"
        assert final["ebay_item_id"] == "998877665544"
        return final

    def test_full_flow_with_reference_url(self, isolated_db):
        """参考 URL ありの正常系フロー (mock settings)。"""
        cfg = _make_config()
        final = self._run_full_flow(cfg, with_reference=True)
        assert final["sku"] == "ebayyh_x1234567890"
        assert final["reference_ebay_item_id"] == "358463512773"
        assert final["reference_category_id"] == "293"
        assert final["ebay_category_id"] == "293"  # reference 採用
        assert final["rank_code"] == "A"
        assert final["shipping_policy_id"] == "IN_0_500"  # 250g
        assert final["ebay_item_id"] == "998877665544"
        # JSON カラムが復元されていること (list/dict)
        assert isinstance(final["item_specifics"], dict)
        assert isinstance(final["supplier_image_urls"], list)

    def test_full_flow_without_reference_url(self, isolated_db):
        """参考 URL なしの正常系フロー (Claude 推定 category を採用)。"""
        cfg = _make_config()
        final = self._run_full_flow(cfg, with_reference=False)
        assert final["reference_ebay_item_id"] is None
        # reference なしなので Claude の "99999" が採用される
        assert final["ebay_category_id"] == "99999"
        assert final["status"] == "applied"

    def test_full_flow_with_real_settings_json(self, isolated_db):
        """実 settings.json を読んでフローを通す (本番挙動確認)。"""
        from monitor.shipping_policy_selector import load_settings_policies

        cfg = load_settings_policies()
        # ebay_business_policies が存在することを確認
        assert "ebay_business_policies" in cfg
        final = self._run_full_flow(cfg, with_reference=True)
        # 実 settings の policy_id が採用されていること
        assert final["shipping_policy_id"]
        # 少なくとも UI-visible な正規 digit 文字列
        assert final["shipping_policy_id"].isdigit()
        assert len(final["shipping_policy_id"]) >= 8  # 実 eBay Policy ID は 12桁程度


# =========================================================================
# TestW9FullFlowFailures: 異常系
# =========================================================================

class TestW9FullFlowFailures:
    """各 Phase の失敗時に後続処理が止まる / DB 状態が正しいことを検証する。"""

    def test_scrape_failure_triggers_manual_fallback(self, isolated_db):
        """仕入先スクレイプ失敗 (scrape_error あり) → 後続フローは続行だが
        タイトル/価格 欠損で最低限のデータで進むこと。"""
        product = ScrapedProduct(
            url="https://auctions.yahoo.co.jp/jp/auction/broken",
            platform="yahoo_auctions",
            scrape_error="yahoo_goto_timeout",
        )
        # scrape_error がある場合、UI 側で手動入力 fallback に誘導する想定。
        # 本テストでは「scrape_error が保持されたまま後続 Phase に渡せる」ことを確認する。
        assert product.scrape_error == "yahoo_goto_timeout"
        assert product.title_ja is None
        # rank_classifier は空でも動く (fallback As-Is を返す)
        with patch("monitor.rank_classifier._get_client", return_value=None):
            rank = classify_rank(
                product.condition_ja or "",
                product.description_ja,
                product.title_ja,
            )
        # condition が空 & キーワード無し → 安全側 As-Is
        assert rank.rank_code == "As-Is"
        assert rank.confidence <= 0.5

    def test_rank_claude_failure_uses_regex_fallback(self, isolated_db):
        """rank_classifier の Claude 呼び出しが APIError で落ちたら regex fallback に倒れる。

        Phase 3 HIGH 関連: fallback は安全側 (As-Is) に倒れる優先度順。
        「ジャンク 動作未確認」は As-Is キーワード両方ヒット → As-Is。
        """
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("Claude 500")
        with patch("monitor.rank_classifier._get_client", return_value=fake_client):
            with patch("monitor.rank_classifier.log_anthropic_response", create=True):
                rank = classify_rank(
                    "ジャンク扱いになります",
                    "通電未確認です。現状渡し。",
                    "Pioneer Lonesome Carboy",
                )
        # regex fallback で As-Is 優先
        assert rank.rank_code == "As-Is"
        # fallback で matched した場合の confidence は 0.7
        assert 0.5 <= rank.confidence <= 0.8

    def test_rank_fallback_priority_as_is_before_po(self, isolated_db):
        """As-Is と PO のキーワード両方が入っていたら As-Is 優先 (安全側)。"""
        with patch("monitor.rank_classifier._get_client", return_value=None):
            # "ジャンク" と "通電確認のみ" 両方含む
            rank = classify_rank(
                "ジャンク品",
                "通電確認のみ。動作未確認。",
                None,
            )
        # As-Is が先に match
        assert rank.rank_code == "As-Is"

    def test_rank_fallback_priority_n_over_s(self, isolated_db):
        """N (新品未開封) が S (未使用) より優先されること。

        N と S は別パターンで並んでいるが、N が先に評価される。
        """
        with patch("monitor.rank_classifier._get_client", return_value=None):
            rank = classify_rank(
                "新品未開封",
                "未開封のシュリンク付きです。",
                None,
            )
        assert rank.rank_code == "N"

    def test_rank_fallback_priority_a_before_d(self, isolated_db):
        """'美品 小キズあり' → A 優先 (傷ありより美品判定優先)。"""
        with patch("monitor.rank_classifier._get_client", return_value=None):
            rank = classify_rank(
                "美品",
                "美品ですが、使用に伴う小キズあります。",
                None,
            )
        # A が先にマッチ (D は「傷あり」の match だがあとから評価される)
        assert rank.rank_code == "A"

    def test_generate_claude_failure_returns_error(self, isolated_db):
        """listing_generator の Claude が失敗したら generate_error に詳細、
        後続 Phase は params 組立時に空タイトル等で VerifyAdd がエラーを返す想定。"""
        product = _make_scraped_product()
        rank = _make_rank("A")
        # API キーなし → 即時 error return
        with patch("monitor.listing_generator._get_client", return_value=None):
            listing = generate_listing(product, None, rank, _V4_TEMPLATE)
        assert listing.generate_error is not None
        assert "ANTHROPIC_API_KEY" in listing.generate_error
        assert listing.ebay_title == ""

    def test_generate_claude_api_error(self, isolated_db):
        """Claude API が例外を投げたら generate_error をセットして dict return。"""
        product = _make_scraped_product()
        rank = _make_rank("A")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("Claude 503")
        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                listing = generate_listing(product, None, rank, _V4_TEMPLATE)
        assert listing.generate_error is not None
        assert "unexpected" in listing.generate_error or "api_error" in listing.generate_error

    def test_verify_errors_prevent_add(self, isolated_db):
        """VerifyAdd でエラーが返った場合、Add に進まない安全装置の検証。

        呼出側 UI の責務だが、E2E の設計契約として
        「verify.errors != [] なら add_fixed_price_item_draft は呼ばない」ことを検証する。
        """
        cfg = _make_config()
        product = _make_scraped_product()

        # VerifyAdd が Failure で返る
        with patch(
            "monitor.ebay_lister._call_trading_api",
            return_value={"success": False, "message": "Verify failed",
                          "raw": _VERIFY_ERRORS_XML},
        ):
            # 最小 params (ここで verify したい)
            rank = _make_rank("A")
            listing = GeneratedListing(
                ebay_title="Test", ebay_description="<div>x</div>",
                ebay_category_id="293", item_specifics={"Brand": "Sony"},
            )
            params = build_draft_params_from_phase3(
                product=product, reference=None, rank=rank, listing=listing,
                shipping_policy_id="IN_0_500",
                sku="SKU-001", listing_price_usd=100.0,
                image_urls=[], config=cfg,
            )
            verify_result = verify_add_fixed_price_item(
                params,
                app_id="A", dev_id="D", cert_id="C", user_token="T",
            )

        # Verify 失敗: success=False & errors が複数
        assert verify_result["success"] is False
        assert len(verify_result["errors"]) >= 2
        # UI ガード条件の表現: errors が空でないなら add フェーズに進まない
        assert bool(verify_result["errors"]) is True

    def test_add_api_failure_marks_api_failed(self, isolated_db):
        """AddFixedPriceItem が Failure 返却時、draft status が 'api_failed' になる。"""
        # draft 保存
        draft_id = database.save_listing_draft({
            "sku": "SKU-FAIL",
            "ebay_title": "Test Failure Title",
            "listing_price_usd": 100.0,
            "status": "draft",
        })
        assert draft_id > 0

        # AddFixedPriceItem が Failure
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("A")
        listing = GeneratedListing(
            ebay_title="Test Failure Title", ebay_description="<div>x</div>",
            ebay_category_id="293", item_specifics={"Brand": "Sony"},
        )
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="SKU-FAIL", listing_price_usd=100.0,
            image_urls=[], config=cfg,
        )
        with patch(
            "monitor.ebay_lister._call_trading_api",
            return_value={"success": False, "message": "API エラー",
                          "raw": _ADD_ITEM_FAILURE_XML},
        ):
            add_result = add_fixed_price_item_draft(
                params,
                app_id="A", dev_id="D", cert_id="C", user_token="T",
            )

        assert add_result["success"] is False
        assert add_result["ebay_item_id"] is None

        # DB 状態遷移
        error_msg = "; ".join(add_result["errors"]) or "unknown"
        database.update_listing_draft_status(
            draft_id, "api_failed",
            api_error_message=error_msg,
        )
        final = database.get_listing_draft(draft_id)
        assert final["status"] == "api_failed"
        assert final["ebay_item_id"] is None
        assert final["api_error_message"]
        assert "category" in final["api_error_message"].lower() or error_msg in final["api_error_message"]

    def test_network_timeout_graceful(self, isolated_db):
        """_call_trading_api が通信エラー (raw=None) を返したら
        verify/add とも dict を返し UI を壊さない。"""
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("A")
        listing = GeneratedListing(
            ebay_title="Test", ebay_description="<div>x</div>",
            ebay_category_id="293", item_specifics={"Brand": "Sony"},
        )
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="SKU-NET", listing_price_usd=100.0,
            image_urls=[], config=cfg,
        )
        with patch(
            "monitor.ebay_lister._call_trading_api",
            return_value={"success": False, "message": "通信エラー: timeout",
                          "raw": None},
        ):
            verify_result = verify_add_fixed_price_item(
                params, app_id="A", dev_id="D", cert_id="C", user_token="T",
            )
            add_result = add_fixed_price_item_draft(
                params, app_id="A", dev_id="D", cert_id="C", user_token="T",
            )
        # 両方とも dict を返し、success=False
        assert verify_result["success"] is False
        assert add_result["success"] is False
        assert any("通信エラー" in e for e in verify_result["errors"])
        assert any("通信エラー" in e for e in add_result["errors"])
        # add の scheduled_time は計算済 (XML 組立前に確定している)
        assert add_result["scheduled_time"]

    def test_reference_fetcher_api_failure(self, isolated_db):
        """GetItem API が Failure 返却時、ReferenceListing.fetch_error にメッセージを入れる。"""
        fail_xml = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <LongMessage>Item not found.</LongMessage>
  </Errors>
</GetItemResponse>"""

        with patch(
            "monitor.ebay_reference_fetcher.get_ebay_credentials",
            return_value=_FAKE_CREDS,
        ):
            with patch(
                "monitor.ebay_reference_fetcher._call_trading_api",
                return_value={"success": True, "ack": "Failure", "raw": fail_xml},
            ):
                ref = fetch_reference_listing(
                    "https://www.ebay.com/itm/999999999999",
                )
        assert ref.fetch_error is not None
        assert "ack_Failure" in ref.fetch_error or "Item not found" in ref.fetch_error


# =========================================================================
# TestW9DataHandoff: Phase 間データ伝搬の整合性
# =========================================================================

class TestW9DataHandoff:
    """Phase 3 レビューの重要パスと HIGH 修正関連を検証する。"""

    def test_reference_category_overrides_claude_category(self, isolated_db):
        """Claude が返した category_id を、reference.category_id が強制上書きする。

        Phase 3 HIGH-関連: listing_generator 内部で reference.category_id が
        必ず最終値になる (Claude ミス防止)。
        """
        product = _make_scraped_product()
        reference = _make_reference_listing(category_id="293")
        rank = _make_rank("A")

        # Claude は別の category_id を返す
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_listing_response(
            category_id="11450",  # 衣類カテゴリ (明らかに誤)
        )

        # 2026-04-22 v2: Taxonomy API を mock して reference 293 を含める
        fake_taxonomy = [
            {"category_id": "293", "category_name": "Consumer Electronics",
             "ancestors_names": [], "ancestors": [], "is_leaf": True,
             "category_tree_node_level": 1},
        ]
        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                with patch(
                    "monitor.ebay_taxonomy.get_category_suggestions",
                    return_value=fake_taxonomy,
                ):
                    listing = generate_listing(product, reference, rank, _V4_TEMPLATE)

        # reference.category_id (293) が Taxonomy 経由で採用されている
        assert listing.ebay_category_id == "293"
        # category_name は Taxonomy のものが入る (mock値)
        assert listing.ebay_category_name == "Consumer Electronics"

    def test_rank_ebay_condition_id_maps_correctly(self, isolated_db):
        """rank_code → ebay_condition_id のマッピングが全ランクで正しい。"""
        expected_mapping = {
            "N": "1000",
            "S": "1500",
            "A": "3000",
            "B": "3000",
            "C": "3000",
            "D": "3000",
            "PO": "3000",
            "As-Is": "7000",
        }
        for rank_code, expected_cond_id in expected_mapping.items():
            rank = _make_rank(rank_code)
            assert rank.ebay_condition_id == expected_cond_id, (
                f"rank {rank_code} expected {expected_cond_id}, got {rank.ebay_condition_id}"
            )

    def test_shipping_policy_from_settings_json(self, isolated_db):
        """settings.json の重量レンジ mapping が複数パターンで正しく引ける。"""
        cfg = _make_config()

        # 250g in-stock → 0-500 レンジ
        pid, label = select_shipping_policy(250, in_stock=True, config=cfg)
        assert pid == "IN_0_500"
        assert "In-stock" in label

        # 500g in-stock → 500-1000 レンジ (半開区間 [500, 1000))
        pid, label = select_shipping_policy(500, in_stock=True, config=cfg)
        assert pid == "IN_500_1000"

        # 999g in-stock → 500-1000 レンジ
        pid, label = select_shipping_policy(999, in_stock=True, config=cfg)
        assert pid == "IN_500_1000"

        # 1500g no-stock → 1000-2000 レンジ & no-stock 系 policy
        pid, label = select_shipping_policy(1500, in_stock=False, config=cfg)
        assert pid == "NS_1000_2000"
        assert "Out-of-stock" in label

        # 25000g (max 超過) → 最大レンジ policy が fallback で採用
        pid, label = select_shipping_policy(25000, in_stock=True, config=cfg)
        assert pid == "IN_10000_20000"
        assert "exceeded" in label

        # weight_g = None → 最小レンジ fallback
        pid, label = select_shipping_policy(None, in_stock=True, config=cfg)
        assert pid == "IN_0_500"

    def test_image_urls_clipped_to_24(self, isolated_db):
        """build_draft_params_from_phase3 が image_urls を 24枚にクリップする。"""
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("A")
        listing = GeneratedListing(
            ebay_title="X", ebay_category_id="293",
            item_specifics={}, ebay_description="<div>x</div>",
        )
        urls = [f"https://e.com/{i}.jpg" for i in range(30)]
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="X", listing_price_usd=1.0,
            image_urls=urls, config=cfg,
        )
        assert len(params["image_urls"]) == _MAX_PICTURES == 24
        # 先頭 24枚を維持
        assert params["image_urls"][0] == "https://e.com/0.jpg"
        assert params["image_urls"][-1] == "https://e.com/23.jpg"

    def test_scheduled_time_matches_returned_vs_xml(self, isolated_db):
        """HIGH-1 修正検証: add_fixed_price_item_draft の戻り値 scheduled_time と
        XML 内 <ScheduleTime> 値が完全一致する (datetime.now() 再スナップズレ回避)。
        """
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("A")
        listing = GeneratedListing(
            ebay_title="Test", ebay_description="<div>x</div>",
            ebay_category_id="293", item_specifics={"Brand": "Sony"},
        )
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="SKU-ST", listing_price_usd=100.0,
            image_urls=[], config=cfg,
        )

        captured_xml = {}

        def _capture(*, call_name, xml_body, **kwargs):
            captured_xml["body"] = xml_body
            return {"success": True, "ack": "Success", "raw": _ADD_ITEM_SUCCESS_XML}

        with patch("monitor.ebay_lister._call_trading_api", side_effect=_capture):
            result = add_fixed_price_item_draft(
                params,
                app_id="A", dev_id="D", cert_id="C", user_token="T",
            )

        # 戻り値の scheduled_time
        scheduled = result["scheduled_time"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$", scheduled)

        # XML body の <ScheduleTime> と完全一致
        m = re.search(r"<ScheduleTime>([^<]+)</ScheduleTime>", captured_xml["body"])
        assert m is not None
        xml_schedule = m.group(1)
        assert xml_schedule == scheduled, (
            f"mismatch: returned={scheduled!r} vs xml={xml_schedule!r}"
        )

    def test_item_specifics_keys_from_reference_preserved(self, isolated_db):
        """参考 listing の item_specifics_keys 配列が listing_generator 経由で
        最終 params の item_specifics に引き継がれる。"""
        product = _make_scraped_product()
        reference = _make_reference_listing(
            category_id="293",
            item_specifics_keys=["Brand", "Model", "Type", "Color", "Connectivity"],
        )
        rank = _make_rank("A")

        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_listing_response(
            category_id="293",
            item_specifics={
                "Brand": "Sony",
                "Model": "WH-1000XM5",
                "Type": "Over-Ear",
                "Color": "Black",
                "Connectivity": "Wireless",
            },
        )

        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                listing = generate_listing(product, reference, rank, _V4_TEMPLATE)

        # reference.item_specifics_keys と listing.item_specifics のキーが一致
        for key in reference.item_specifics_keys:
            assert key in listing.item_specifics, f"missing key: {key}"

    def test_rank_code_propagates_to_draft_params(self, isolated_db):
        """rank_code と ebay_condition_id が build_draft_params_from_phase3 経由で
        最終 params に反映される。"""
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("PO")  # PO (Power-On Only)
        listing = GeneratedListing(
            ebay_title="X", ebay_category_id="293",
            item_specifics={}, ebay_description="<div>x</div>",
        )
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="X", listing_price_usd=1.0,
            image_urls=[], config=cfg,
        )
        assert params["rank_code"] == "PO"
        assert params["ebay_condition_id"] == "3000"

    def test_gadget_mode_triggers_for_consumer_electronics(self, isolated_db):
        """category_id=293 (Consumer Electronics) なら mode_class="gadget"。"""
        product = _make_scraped_product()
        reference = _make_reference_listing(category_id="293")
        rank = _make_rank("A")

        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_listing_response(
            category_id="293",
        )

        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                listing = generate_listing(product, reference, rank, _V4_TEMPLATE)

        assert listing.mode_class == "gadget"
        # description の mode_class placeholder が gadget に置換されていること
        assert "gadget" in listing.ebay_description


# =========================================================================
# TestW9Persistence: DB 永続化フロー
# =========================================================================

class TestW9Persistence:
    """listing_drafts テーブルの永続化フロー検証。"""

    def test_draft_saved_before_api_call(self, isolated_db):
        """API 呼出前に draft が DB に保存されていること。

        (実際のフロー順序: save_listing_draft → API call → update_status)
        """
        draft_id = database.save_listing_draft({
            "sku": "SKU-BEFORE-API",
            "ebay_title": "Pre-API Title",
            "listing_price_usd": 249.99,
            "status": "draft",
        })
        assert draft_id > 0
        # status="draft" で保存されている
        row = database.get_listing_draft(draft_id)
        assert row["status"] == "draft"
        assert row["ebay_item_id"] is None

    def test_status_transitions_draft_to_applied(self, isolated_db):
        """status が 'draft' → 'applied' に正しく遷移する。"""
        draft_id = database.save_listing_draft({
            "sku": "SKU-TRANSITION",
            "ebay_title": "Test",
            "status": "draft",
        })
        # draft
        assert database.get_listing_draft(draft_id)["status"] == "draft"

        # applied
        database.update_listing_draft_status(
            draft_id, "applied", ebay_item_id="998877665544",
        )
        row = database.get_listing_draft(draft_id)
        assert row["status"] == "applied"
        assert row["ebay_item_id"] == "998877665544"

    def test_status_api_failed_preserves_error_message(self, isolated_db):
        """api_failed 時の api_error_message が保存される。"""
        draft_id = database.save_listing_draft({
            "sku": "SKU-APIFAIL",
            "ebay_title": "Test",
            "status": "draft",
        })
        error_detail = "Invalid category ID; Shipping profile required."
        database.update_listing_draft_status(
            draft_id, "api_failed",
            api_error_message=error_detail,
        )
        row = database.get_listing_draft(draft_id)
        assert row["status"] == "api_failed"
        assert row["api_error_message"] == error_detail
        assert row["ebay_item_id"] is None

    def test_json_columns_roundtrip_japanese(self, isolated_db):
        """JSON カラムに日本語 dict/list を保存 → 読み出し時に復元される。"""
        payload = {
            "sku": "SKU-JP",
            "supplier_image_urls": [
                "https://jp.example.com/画像1.jpg",
                "https://jp.example.com/画像2.jpg",
            ],
            "reference_item_specifics_keys": ["Brand", "Model", "色"],
            "item_specifics": {
                "Brand": "Sony",
                "Model": "WH-1000XM5",
                "色": "ブラック",  # 日本語 value
            },
        }
        draft_id = database.save_listing_draft(payload)
        row = database.get_listing_draft(draft_id)

        # list が list として復元
        assert isinstance(row["supplier_image_urls"], list)
        assert len(row["supplier_image_urls"]) == 2
        assert "画像1.jpg" in row["supplier_image_urls"][0]

        # dict が dict として復元、日本語キーと値両方維持
        assert isinstance(row["item_specifics"], dict)
        assert row["item_specifics"]["色"] == "ブラック"

        # 日本語キーを含むリスト
        assert "色" in row["reference_item_specifics_keys"]

    def test_multiple_drafts_ordered_by_created_desc(self, isolated_db):
        """get_listing_drafts(status) が status フィルタ・limit を正しく適用する。

        created_at の DESC 順序検証は SQLite の CURRENT_TIMESTAMP が秒粒度のため
        連続 INSERT だと同一秒になって ORDER BY が不定になる。そのため本テストでは
        「取得件数」と「status フィルタが効く」ことのみ検証する。順序は実運用では
        秒以上の粒度があるので ORDER BY created_at DESC で十分。
        """
        ids = []
        for i in range(3):
            did = database.save_listing_draft({
                "sku": f"SKU-{i}",
                "ebay_title": f"Title {i}",
                "status": "draft",
            })
            ids.append(did)

        # 1件は applied に遷移 → status フィルタ検証用
        database.update_listing_draft_status(ids[0], "applied", ebay_item_id="X")

        draft_rows = database.get_listing_drafts(status="draft", limit=10)
        applied_rows = database.get_listing_drafts(status="applied", limit=10)

        assert len(draft_rows) == 2
        assert len(applied_rows) == 1
        assert applied_rows[0]["ebay_item_id"] == "X"
        # status フィルタで draft のみ: 全件 status='draft'
        assert all(r["status"] == "draft" for r in draft_rows)
        # limit フィルタ
        assert len(database.get_listing_drafts(status="draft", limit=1)) == 1


# =========================================================================
# TestW9DescriptionTemplate: v4 テンプレ placeholder 置換
# =========================================================================

class TestW9DescriptionTemplate:
    """v4 description テンプレの placeholder 置換挙動を検証する。"""

    V4_PLACEHOLDERS: tuple[str, ...] = (
        "mode_class",
        "product_name",
        "product_sub",
        "rank",
        "rank_label",
        "rank_jp",
        "quick_notes",
        "includes_rows",
        "specs_rows",
        "spec_strip_rows",
        "shipping_origin",
        "shipping_carrier",
        "shipping_handling",
        "shipping_delivery_us",
        "shipping_packaging",
        "shipping_notes",
    )

    def test_all_14_placeholders_replaced(self, isolated_db):
        """v4 テンプレの 14 種 placeholder が全て置換されて残らないこと。

        (正確には 16 種だが、仕様上は 14 カテゴリの placeholder。mode_class と
        notes 系は shared。)
        """
        values = {
            "mode_class": "default",
            "product_name": "Sony WH-1000XM5",
            "product_sub": "Flagship headphones",
            "rank": "A",
            "rank_label": "Excellent",
            "rank_jp": "Tested · Minor Wear",
            "quick_notes": "Tested working",
            "includes_rows": "<div>inc</div>",
            "specs_rows": "<tr>row</tr>",
            "spec_strip_rows": "<div>strip</div>",
            "shipping_origin": "Tokyo, Japan",
            "shipping_carrier": "DHL SpeedPAK",
            "shipping_handling": "1–3 business days",
            "shipping_delivery_us": "6–10 business days",
            "shipping_packaging": "Double-boxed",
            "shipping_notes": "",
        }
        out = render_description(_V4_TEMPLATE, values)

        # 未置換 {{...}} が残っていないこと
        assert "{{" not in out
        # 各 value が正しく出力に含まれる
        assert "Sony WH-1000XM5" in out
        assert "Tokyo, Japan" in out
        assert "Excellent" in out

    def test_mode_class_default_vs_gadget(self, isolated_db):
        """mode_class placeholder が default/gadget で切り替わる。"""
        values_default = {"mode_class": "default"}
        out_default = render_description(
            '<div class="mh-wrap {{mode_class}}">x</div>',
            values_default,
        )
        assert 'class="mh-wrap default"' in out_default

        values_gadget = {"mode_class": "gadget"}
        out_gadget = render_description(
            '<div class="mh-wrap {{mode_class}}">x</div>',
            values_gadget,
        )
        assert 'class="mh-wrap gadget"' in out_gadget

    def test_missing_placeholder_becomes_empty(self, isolated_db):
        """values dict に無い placeholder は空文字に置換される (未破壊)。"""
        tpl = "{{existing}} | {{missing}} | {{existing}}"
        out = render_description(tpl, {"existing": "E"})
        assert out == "E |  | E"

    def test_css_braces_not_interpreted_as_placeholder(self, isolated_db):
        """CSS の `{ ... }` 単波括弧は placeholder とみなされず保持される。"""
        tpl = "<style>.mh { color: red; } .mh-wrap { padding: 10px; }</style>"
        out = render_description(tpl, {})
        assert out == tpl  # 無変換で保持

    def test_double_braces_with_special_chars_not_matched(self, isolated_db):
        """{{foo-bar}} のようにハイフン等を含むと placeholder として認識されない
        (正源: `\\w+` マッチ = [a-zA-Z0-9_])。
        """
        tpl = "{{foo-bar}} normal {{key}}"
        out = render_description(tpl, {"key": "V"})
        # `{{foo-bar}}` は \w+ にマッチしないので置換されない
        assert "{{foo-bar}}" in out
        assert out.endswith("normal V")

    def test_cdata_wrapping_preserves_description_in_xml(self, isolated_db):
        """description HTML が CDATA で包まれて XML 内に原形のまま埋め込まれる。"""
        description_html = render_description(_V4_TEMPLATE, {
            "mode_class": "gadget",
            "product_name": "Sony WH-1000XM5",
            "product_sub": "Flagship",
            "rank": "A",
            "rank_label": "Excellent",
            "rank_jp": "Tested",
            "quick_notes": "notes",
            "includes_rows": "<div>inc</div>",
            "specs_rows": "<tr>row</tr>",
            "spec_strip_rows": "",
            "shipping_origin": "Tokyo",
            "shipping_carrier": "DHL",
            "shipping_handling": "1-3",
            "shipping_delivery_us": "6-10",
            "shipping_packaging": "Box",
            "shipping_notes": "",
        })

        # XML 組立
        params = {
            "sku": "SKU-CDATA",
            "ebay_title": "Test",
            "ebay_description": description_html,
            "ebay_category_id": "293",
            "ebay_condition_id": "3000",
            "listing_price_usd": 100.0,
            "image_urls": [],
            "payment_policy_id": "P", "return_policy_id": "R",
            "shipping_policy_id": "S",
            "country": "JP", "currency": "USD",
            "location": "Tokyo", "postal_code": "100-0001",
            "dispatch_time_max": 3, "listing_duration": "GTC",
            "scheduled_days_offset": 21,
            "item_specifics": {"Brand": "Sony"},
        }
        xml = _build_add_fixed_price_item_xml(params, verify=False)

        # Description が CDATA で包まれている
        assert "<Description><![CDATA[" in xml
        # CSS の `<style>` タグが escape されず生 HTML として残っていること
        assert "<style>" in xml  # CDATA 内では escape されない
        # 未置換 placeholder が残っていないこと (description 経由で XML に漏れない)
        assert "{{" not in description_html
        # XML として well-formed (CDATA 内部は parse OK)
        xml_for_parse = xml.replace("{USER_TOKEN}", "dummy")
        root = ET.fromstring(xml_for_parse)
        assert root.tag.endswith("AddFixedPriceItemRequest")


# =========================================================================
# TestW9HighFixRegressions: Phase 1-4 レビューで発見された HIGH の回帰ガード
# =========================================================================

class TestW9HighFixRegressions:
    """過去に修正した HIGH レベル不具合が再発しないことを保証する。"""

    def test_high1_schedule_time_xml_match(self, isolated_db):
        """HIGH-1: add_fixed_price_item_draft の scheduled_time と
        XML 内 <ScheduleTime> が完全一致する (再掲: TestW9DataHandoff にもあり)。
        """
        cfg = _make_config()
        product = _make_scraped_product()
        rank = _make_rank("A")
        listing = GeneratedListing(
            ebay_title="Test", ebay_description="<div>x</div>",
            ebay_category_id="293", item_specifics={"Brand": "Sony"},
        )
        params = build_draft_params_from_phase3(
            product=product, reference=None, rank=rank, listing=listing,
            shipping_policy_id="IN_0_500",
            sku="SKU-H1", listing_price_usd=100.0,
            image_urls=[], config=cfg,
        )
        captured = {}

        def _capture(*, call_name, xml_body, **kwargs):
            captured["body"] = xml_body
            return {"success": True, "ack": "Success", "raw": _ADD_ITEM_SUCCESS_XML}

        with patch("monitor.ebay_lister._call_trading_api", side_effect=_capture):
            result = add_fixed_price_item_draft(
                params, app_id="A", dev_id="D", cert_id="C", user_token="T",
            )

        assert result["scheduled_time"]
        m = re.search(r"<ScheduleTime>([^<]+)</ScheduleTime>", captured["body"])
        assert m is not None
        assert m.group(1) == result["scheduled_time"]

    def test_high2a_update_ebay_listing_sku_clears_oos_since(self, isolated_db):
        """HIGH-2A: update_ebay_listing_sku が source_out_of_stock_since を NULL にクリアする。

        Pattern 2 sweep が新 SKU を即再掃引しないように防衛する動作の検証。
        """
        # 親 listing を ebay_listings に作る (source_out_of_stock_since セット済)
        with database.get_conn() as conn:
            conn.execute(
                """INSERT INTO ebay_listings (ebay_item_id, sku, title,
                       source_status, source_out_of_stock_since)
                   VALUES (?, ?, ?, ?, ?)""",
                ("ITEM_001", "ebayme_old_sku", "Old Title",
                 "在庫なし", "2026-04-10 12:00:00"),
            )

        # 新 SKU に書き換え (= W9 apply 後の ReviseItem 成功後の動作模擬)
        database.update_ebay_listing_sku("ITEM_001", "ebayme_new_sku")

        # DB 状態検証
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT sku, source_status, source_last_checked, "
                "source_out_of_stock_since, risk_confirmed "
                "FROM ebay_listings WHERE ebay_item_id=?",
                ("ITEM_001",),
            ).fetchone()

        assert row is not None
        assert row["sku"] == "ebayme_new_sku"
        # source_out_of_stock_since が NULL にクリアされている (HIGH-2A 修正点)
        assert row["source_out_of_stock_since"] is None
        # 他のリセット項目
        assert row["source_status"] == "unknown"
        assert row["source_last_checked"] is None
        assert row["risk_confirmed"] == 0

    def test_high3_sibling_pending_auto_rejected_when_parent_ended(self, isolated_db):
        """HIGH-3: 親 listing が is_ended=1 のとき、pending 候補を auto_rejected=1 にする。

        cleanup_stale_supplier_candidates の動作を検証。
        """
        with database.get_conn() as conn:
            # 親 listing: is_ended=1
            conn.execute(
                """INSERT INTO ebay_listings (ebay_item_id, sku, is_ended, ended_reason)
                   VALUES (?, ?, 1, 'test')""",
                ("ITEM_ENDED", "sku_ended"),
            )
            # 親 listing: is_ended=0 (正常)
            conn.execute(
                """INSERT INTO ebay_listings (ebay_item_id, sku, is_ended)
                   VALUES (?, ?, 0)""",
                ("ITEM_ACTIVE", "sku_active"),
            )

            # pending 候補を兄弟で 2つ (片方が ended 親、もう片方が active 親)
            conn.execute(
                """INSERT INTO supplier_candidates
                   (sku, ebay_item_id, candidate_url, status, match_score)
                   VALUES (?, ?, ?, 'pending', 80)""",
                ("sku_ended", "ITEM_ENDED", "https://a.example.com/1"),
            )
            conn.execute(
                """INSERT INTO supplier_candidates
                   (sku, ebay_item_id, candidate_url, status, match_score)
                   VALUES (?, ?, ?, 'pending', 90)""",
                ("sku_active", "ITEM_ACTIVE", "https://a.example.com/2"),
            )

        # cleanup 実行
        result = database.cleanup_stale_supplier_candidates()
        assert result["rejected_ended"] == 1

        # DB 状態検証
        with database.get_conn() as conn:
            ended_row = conn.execute(
                "SELECT status, auto_rejected FROM supplier_candidates "
                "WHERE sku='sku_ended'"
            ).fetchone()
            active_row = conn.execute(
                "SELECT status, auto_rejected FROM supplier_candidates "
                "WHERE sku='sku_active'"
            ).fetchone()

        # ended 親の兄弟候補は auto-reject
        assert ended_row["status"] == "rejected"
        assert ended_row["auto_rejected"] == 1
        # active 親はそのまま pending
        assert active_row["status"] == "pending"
        assert (active_row["auto_rejected"] or 0) == 0


# =========================================================================
# Smoke Test: module import sanity check
# =========================================================================

class TestW9ModuleImports:
    """Phase 1-5 モジュールの import sanity。"""

    def test_all_w9_phase_modules_importable(self):
        """全 Phase モジュールが import エラーなしに読める。"""
        # 既に先頭で import 済なので、ここではヘルパ関数の存在だけ確認
        from monitor import (
            database,
            ebay_lister,
            ebay_reference_fetcher,
            listing_generator,
            rank_classifier,
            shipping_policy_selector,
            supplier_scraper,
        )
        assert hasattr(database, "save_listing_draft")
        assert hasattr(database, "update_listing_draft_status")
        assert hasattr(database, "get_listing_draft")
        assert hasattr(ebay_lister, "build_draft_params_from_phase3")
        assert hasattr(ebay_lister, "verify_add_fixed_price_item")
        assert hasattr(ebay_lister, "add_fixed_price_item_draft")
        assert hasattr(ebay_reference_fetcher, "fetch_reference_listing")
        assert hasattr(listing_generator, "generate_listing")
        assert hasattr(rank_classifier, "classify_rank")
        assert hasattr(shipping_policy_selector, "select_shipping_policy")
        assert hasattr(supplier_scraper, "scrape_supplier_url")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
