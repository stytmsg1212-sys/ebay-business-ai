#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eBay 個別新規出品モジュール (W9 Phase 4)

Phase 3 で揃えた GeneratedListing + RankClassification + ShippingPolicyID
+ 画像 URL から AddFixedPriceItem / VerifyAddFixedPriceItem の XML を組み立て、
既存の `monitor.ebay_client._call_trading_api` で送信する。

設計方針:
  - Description は CDATA で包み、HTML をそのまま eBay に届ける。
    理由: v4 テンプレには <style>, SVG, 二重引用符が頻出し、xml escape すると
    >50KB の不要 escape で可読性とトークン効率が劣化する。CDATA により
    ブラウザでも Trading API でも同じ HTML が評価される。
  - ScheduleTime = now + 21日 (UTC, ISO 8601, ミリ秒3桁末尾 Z)。
    settings.json の w9_draft_mode.scheduled_days_offset を優先して参照し、
    不在時は 21 日固定。Seller Hub → Listings → Scheduled タブに並ぶ。
  - WarningLevel=High で eBay から警告もフル返却させ、UI で 1:1 表示する。
  - SKU / ItemSpecifics 値は xml.sax.saxutils.escape で XML escape。
  - PictureURL は eBay 上限 24 枚に clip。processed_image_urls (W10 加工後) が
    あればそれを優先、なければ selected_image_urls、それも無ければ
    ScrapedProduct.image_urls の先頭 24 枚。
  - SellerProfiles は Payment/Return/Shipping 3つ全て必須。IDは
    settings.json の ebay_business_policies ブロックから読む。
    Shipping Policy ID は Phase 3 shipping_policy_selector が決定済の前提で
    引数で受け取る。
  - 全例外を catch して dict return。呼出側 UI を壊さない。
  - 実 API 呼出しは全てテストで mock されることを前提に、本モジュールは
    副作用を _call_trading_api 1箇所に閉じ込める。

正源:
  .company/engineering/docs/2026-04-20-W9-individual-listing-PRD.md セクション 7/8/9/14
  .company/ebay-knowledge/topics/listing-description-template.md v4
"""
from __future__ import annotations

import logging
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape as _xml_escape

# pythonw gotcha ガード (sys.stdout が None もありうる pythonw.exe 環境)
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

from monitor.api_logger import log_api_call
from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
from monitor.ebay_client import _call_trading_api, _is_forbidden_specific_name

# Phase 3 の dataclass を import (型ヒント・helper 用途。UI 側は build_draft_params_from_phase3 を使う)
try:
    from monitor.listing_generator import GeneratedListing
except ImportError:  # pragma: no cover — 循環回避
    GeneratedListing = None  # type: ignore[assignment,misc]
try:
    from monitor.rank_classifier import RankClassification
except ImportError:  # pragma: no cover
    RankClassification = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# eBay Trading API 上限 (PictureURL は最大 24 枚)
_MAX_PICTURES: int = 24

# eBay namespace (レスポンス parse 用)
_NS: str = 'urn:ebay:apis:eBLBaseComponents'

# ScheduleTime オフセット既定値 (PRD Q2 確定)
_DEFAULT_SCHEDULED_DAYS_OFFSET: int = 21

# Location / PostalCode / Country / Currency 既定 (settings.json 上書き可)
_DEFAULT_COUNTRY: str = 'JP'
_DEFAULT_CURRENCY: str = 'USD'
_DEFAULT_LOCATION: str = 'Tokyo, Japan'
_DEFAULT_POSTAL_CODE: str = '100-0001'
_DEFAULT_DISPATCH_TIME_MAX: int = 3  # 営業日。handling time
_DEFAULT_LISTING_DURATION: str = 'GTC'  # Good Till Cancelled (FixedPriceItem 推奨)


# =========================================================================
# 時刻ヘルパ
# =========================================================================

def _build_schedule_time(days_offset: int = _DEFAULT_SCHEDULED_DAYS_OFFSET) -> str:
    """now + days_offset 日の UTC を eBay Trading API ScheduleTime 形式で返す。

    形式: `2026-05-11T13:00:00.000Z` (ISO 8601, ミリ秒3桁, 末尾 Z)。

    eBay 仕様: ScheduleTime は UTC で、少なくとも現在時刻より 5分以上未来、
    最大 21日以内。本関数は 21日 (既定) を採用し、境界ギリギリで弾かれる
    リスクを避けるため秒以下は切り捨てる (マイクロ秒 0 固定)。
    """
    if not isinstance(days_offset, int) or days_offset < 1:
        days_offset = _DEFAULT_SCHEDULED_DAYS_OFFSET
    # eBay 上限は 21日、クリップする
    if days_offset > 21:
        days_offset = 21

    target = datetime.now(timezone.utc) + timedelta(days=days_offset)
    # 秒精度で確定、ミリ秒部分は '.000' 固定 (eBay が受け付ける形式)
    target = target.replace(microsecond=0)
    return target.strftime('%Y-%m-%dT%H:%M:%S.000Z')


# =========================================================================
# draft_params ビルダ (Phase 5 UI から呼ぶ想定)
# =========================================================================

def build_draft_params_from_phase3(
    product: Any,
    reference: Any,
    rank: Any,
    listing: Any,
    shipping_policy_id: str,
    sku: str,
    listing_price_usd: float,
    image_urls: list[str],
    config: Optional[dict] = None,
    primary_market: Optional[str] = None,
    hs_code: Optional[str] = None,
) -> dict:
    """Phase 3 で揃った dataclass 群 + UI 入力を draft_params dict に整形する。

    Phase 5 UI はこの関数を呼んで dict を生成し、
    verify_add_fixed_price_item / add_fixed_price_item_draft に渡す。

    Args:
        product: monitor.supplier_scraper.ScrapedProduct (UI 入力の元データ、今は使わないが将来拡張用に受ける)
        reference: monitor.ebay_reference_fetcher.ReferenceListing or None
        rank: monitor.rank_classifier.RankClassification
        listing: monitor.listing_generator.GeneratedListing
        shipping_policy_id: Phase 3 shipping_policy_selector が決定済の ID
        sku: ユーザー入力 SKU (任意だが実質必須。空なら空文字)
        listing_price_usd: UI で確認済の出品価格
        image_urls: 画像 URL リスト。processed_image_urls > selected_image_urls の優先順で
                    呼出側で解決済を渡すこと。
        config: settings.json の dict (ebay_business_policies / w9_* 読取り用)

    Returns:
        draft_params dict。全フィールドの欠損は呼出側 UI で検証する。
    """
    cfg = config or {}
    biz = cfg.get('ebay_business_policies') or {}
    defaults = cfg.get('w9_listing_defaults') or {}
    draft_mode = cfg.get('w9_draft_mode') or {}

    # listing / rank / reference から安全に値を引く
    ebay_title = _safe_attr(listing, 'ebay_title', '')
    ebay_description = _safe_attr(listing, 'ebay_description', '')
    ebay_category_id = _safe_attr(listing, 'ebay_category_id', None)
    ebay_category_name = _safe_attr(listing, 'ebay_category_name', None)
    item_specifics = _safe_attr(listing, 'item_specifics', {}) or {}
    rank_code = _safe_attr(rank, 'rank_code', 'As-Is')
    rank_label = _safe_attr(rank, 'rank_label', '')
    # 2026-05-01 fix: 旧 `quick_notes or reasoning` フォールバックは
    # `RankClassification.quick_notes` が dataclass 定義に無く常に空 →
    # 必ず `reasoning` (日本語判定理由) にフォールバックして出品文に日本語混入。
    # 解決: reasoning フォールバックを削除、quick_notes 単独参照のみ。
    quick_notes = _safe_attr(rank, 'quick_notes', '')
    ebay_condition_id = _safe_attr(rank, 'ebay_condition_id', '3000')

    reference_category_id = _safe_attr(reference, 'category_id', None) if reference else None
    # 参考URLがある場合そちらの CategoryID を優先 (PRD M14 / M15 に準拠)
    if reference_category_id and not ebay_category_id:
        ebay_category_id = reference_category_id

    # 2026-05-01 fix: `or` だと scheduled_days_offset=0 (Active 即時公開) が消える.
    # `is None` check で 0 を明示的に尊重する.
    _raw_days = draft_mode.get('scheduled_days_offset')
    scheduled_days = int(_raw_days) if _raw_days is not None else _DEFAULT_SCHEDULED_DAYS_OFFSET

    # 2026-05-01 fix: ConditionDescription を自然な英文に変更.
    # - 旧 `' | '.join` は生硬 → '. '.join + 末尾 '.' で文章化
    # - rank_jp は rank_label と意味重複 → 削除
    # - As-Is は CLAUDE.md L243-247 規定形式 (`As-Is — <reason>` / 65 字以内) に準拠
    #   (rank_code == rank_label == 'As-Is' で `Rank As-Is — As-Is` 冗長化を防ぐ)
    # - As-Is で quick_notes 不在 = silent fall-through 防止のため明示的 placeholder
    condition_description_parts: list[str] = []
    if rank_code == 'As-Is':
        # CLAUDE.md L243-247: As-Is は `As-Is — <reason>` 形式必須.
        # quick_notes が reason を兼ねる. 不在時は placeholder で defect リスク警告.
        if quick_notes:
            reason_text = str(quick_notes).rstrip(' .')
        else:
            reason_text = 'Reason not provided'
            logger.warning(
                "ConditionDescription: As-Is rank without quick_notes; "
                "using placeholder. eBay buyer 紛争で defect リスクあり, "
                "user は明示的 reason を設定すべき."
            )
        condition_description_parts.append(f'As-Is — {reason_text}')
    else:
        if rank_code and rank_label:
            condition_description_parts.append(f'Rank {rank_code} — {rank_label}')
        elif rank_label:
            condition_description_parts.append(rank_label)
        elif rank_code:
            condition_description_parts.append(f'Rank {rank_code}')
        if quick_notes:
            condition_description_parts.append(str(quick_notes).rstrip(' .'))
    # CLAUDE.md L246: As-Is は 65 字以内必須. 他 rank は eBay 全体上限 1000.
    _max_len = 65 if rank_code == 'As-Is' else 1000
    condition_description = (
        ('. '.join(condition_description_parts) + '.')[:_max_len]
        if condition_description_parts else ''
    )

    # 2026-04-21 追加: Package weight/dimensions を product (ScrapedProduct) / listing から引く
    weight_g = (
        _safe_attr(product, 'weight_hint_g', None)
        or _safe_attr(listing, 'weight_g', None)
    )
    length_mm = _safe_attr(product, 'length_mm', None) or _safe_attr(listing, 'length_mm', None)
    width_mm = _safe_attr(product, 'width_mm', None) or _safe_attr(listing, 'width_mm', None)
    depth_mm = _safe_attr(product, 'depth_mm', None) or _safe_attr(listing, 'depth_mm', None)

    # 2026-05-01 W84 候補 D: 4 区分 primary_market 別の listing 価格 + 米国向け送料 override.
    # `reference_shipping_tariff_logic.md` v1.0 § 4.2 / § 5.3 のマトリクス準拠.
    # 関税額は post-tariff 期暫定として `price * 0.20` 近似値 (W89 で strict 化予定).
    # 旧版 (~2026-04-30): 全 listing 共通 20% override.
    # 2026-05-02 W84 fix: primary_market / hs_code を明示引数 > listing 内 > None
    #   の優先順で取得 (UI 経路で listing オブジェクトに primary_market が無いケース対応).
    price_val = float(listing_price_usd) if listing_price_usd is not None else 0.0
    shipping_ratio = float(defaults.get('shipping_cost_ratio_of_price') or 0.20)
    if primary_market is None:
        primary_market = _safe_attr(listing, 'primary_market', None)
    if hs_code is None:
        hs_code = _safe_attr(listing, 'hs_code', None)
    adjusted_price_usd, us_shipping_override = _compute_shipping_override_for_market(
        price_val, primary_market, tariff_ratio=shipping_ratio, hs_code=hs_code,
    )
    shipping_cost_usd_override = us_shipping_override
    shipping_additional_cost_usd = us_shipping_override

    return {
        'sku': sku or '',
        'ebay_title': ebay_title,
        'ebay_description': ebay_description,
        'ebay_category_id': ebay_category_id or '',
        'ebay_category_name': ebay_category_name or '',
        'ebay_condition_id': ebay_condition_id or '3000',
        'rank_code': rank_code,
        'condition_description': condition_description,
        'item_specifics': dict(item_specifics) if isinstance(item_specifics, dict) else {},
        # W84: US_only 区分のみ関税包含 (adjusted_price_usd != price_val)、他は同値
        'listing_price_usd': adjusted_price_usd,
        'primary_market': (primary_market or 'unknown'),
        'image_urls': list(image_urls or [])[:_MAX_PICTURES],
        'payment_policy_id': str(biz.get('payment_policy_id') or ''),
        'return_policy_id': str(biz.get('return_policy_id') or ''),
        'shipping_policy_id': str(shipping_policy_id or ''),
        # 送料 20% override
        'shipping_cost_usd_override': shipping_cost_usd_override,
        'shipping_additional_cost_usd': shipping_additional_cost_usd,
        'shipping_service_name': defaults.get('shipping_service_override') or 'Other',
        # Package weight/dimensions
        'weight_g': int(weight_g) if weight_g else None,
        'length_mm': int(length_mm) if length_mm else None,
        'width_mm': int(width_mm) if width_mm else None,
        'depth_mm': int(depth_mm) if depth_mm else None,
        'country': defaults.get('country') or _DEFAULT_COUNTRY,
        'currency': defaults.get('currency') or _DEFAULT_CURRENCY,
        'location': defaults.get('location') or _DEFAULT_LOCATION,
        'postal_code': defaults.get('postal_code') or _DEFAULT_POSTAL_CODE,
        'dispatch_time_max': int(defaults.get('dispatch_time_max') or _DEFAULT_DISPATCH_TIME_MAX),
        'listing_duration': defaults.get('listing_duration') or _DEFAULT_LISTING_DURATION,
        'scheduled_days_offset': int(scheduled_days),
    }


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """dataclass / dict どちらでも安全に値を引く。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    # dataclass or 普通のオブジェクト
    return getattr(obj, name, default)


# Section 232 該当 HS prefix (memory `reference_section_232_kb.md` v1.0 / 2026-04-06 改訂)
# tariff_ratio=0.20 近似値運用時に 50%/25% 該当品で赤字リスクのため早期警告対象.
_SECTION_232_HS_PREFIXES = (
    # Annex I-A (50%、Chapter 72-74/76 純金属、重量閾値なし)
    "72",   # 鉄鋼素材 (鋼板/棒/線材/インゴット/合金鋼) ← code-reviewer HIGH-1 fix で追加
    "73",   # 鉄鋼製品 (ストーブ/鍋/フライパン/保温ジャー)
    "74",   # 銅製品
    "76",   # アルミ製品
    # Annex I-B (25%、Chapter 84-87 派生品、metal weight ≥15%)
    "8516", # 電気炊飯器/オーブン
    "8418", # 冷蔵・冷凍
    "8501", # 特定モーター
    "8504", # 変圧器
    "8415", # エアコン
    "8517", # 通信機器
    "8544", # 電線
    "8708", # 自動車部品
    "8716", # トレーラー
    # Annex III (15% transitional, ~2027-12-31)
    "8421", # 液体ろ過
    "8424", # スプレー
    "8428", # コンベア/産業ロボット
)


def _is_section_232_hs(hs_code: Optional[str]) -> bool:
    """HS code が Section 232 該当 prefix にマッチするか判定."""
    if not hs_code:
        return False
    hs = (hs_code or "").replace(".", "").strip()
    return any(hs.startswith(p) for p in _SECTION_232_HS_PREFIXES)


def _compute_shipping_override_for_market(
    price_val: float,
    primary_market: Optional[str],
    tariff_ratio: float = 0.20,
    hs_code: Optional[str] = None,
) -> tuple[float, Optional[float]]:
    """W84 候補 D: 4 区分 primary_market 別の listing 価格 + 米国向け送料 override.

    `reference_shipping_tariff_logic.md` v1.0 § 4.2 / § 5.3 マトリクスに準拠.
    関税額は post-tariff 期暫定として `price * tariff_ratio` 近似値を使用
    (DDP 関税計算 strict 化は別 W89 で実装予定).

    注: mixed_global の正式仕様 (memory v1.0 § 4.2):
        各国送料 = (各国実送料 - US 実送料) + DDP 関税 (米国向けのみ)
    現状は各国実送料データ未整備のため、US 実送料=0 暫定値で運用 (DDP 関税のみを
    US 送料 override として出力). 「実送料差分」は別 W (実送料サブシステム後)
    で正式対応.

    各国実送料データが現状システムにないため、他国 override は BP fallback
    (= override 行なし) で運用.

    HIGH-3 fix (2026-05-02): hs_code 引数で Section 232 該当判定を行い、US_only
    区分かつ該当 HS の場合は warning log を出力 (W89 strict 化前の安全装置).

    Args:
        price_val: 商品の販売価格 (USD).
        primary_market: 'US_only' | 'mixed_global' | 'global_only' | 'unknown' | None
                         (None / 未知値は unknown と同等の default 動作).
        tariff_ratio: 関税近似率 (default 0.20 = 20%).
        hs_code: HS code (optional). Section 232 該当時に warning log 出力.

    Returns:
        (adjusted_price_usd, us_shipping_override_usd)
        - adjusted_price_usd: 出品時の <StartPrice>. US_only のみ関税包含.
        - us_shipping_override_usd: 米国向け送料 override. None で β fix を bypass.
    """
    market = (primary_market or "unknown").strip().lower()
    if price_val is None or price_val <= 0:
        return (price_val or 0.0, None)
    tariff_approx = round(price_val * float(tariff_ratio), 2)

    # HIGH-3: Section 232 該当 HS は近似値 20% では赤字リスク (I-A 50% / I-B 25%)
    # 2026-05-02 code-reviewer HIGH-2 fix: mixed_global / unknown も US 送料欄に同じ
    # tariff_approx が乗るので警告対象。global_only は user が自腹覚悟で送料 $0 を
    # 選ぶ前提なので警告不要 (赤字判断 = user 承認済).
    if _is_section_232_hs(hs_code) and market in ("us_only", "mixed_global", "unknown", ""):
        logger.warning(
            "Section 232 該当の可能性 (HS=%s, market=%s, price=$%.2f). "
            "tariff_ratio=%.0f%% 近似は I-A (50%%) / I-B (25%%) 該当品で赤字リスク. "
            "W89 DDP strict 計算 + user 承認が必要. 詳細: reference_section_232_kb.md",
            hs_code, market or "unknown", price_val, tariff_ratio * 100,
        )

    if market == "us_only":
        # 商品価格に DDP 関税を包含 → US 表示送料 $0 Free
        return (round(price_val + tariff_approx, 2), 0.0)
    if market == "global_only":
        # 商品代のみ + 米国は自腹リスク許容で Free Shipping
        return (price_val, 0.0)
    # mixed_global / unknown: 商品代のみ + US 送料欄に DDP 関税近似値
    # (memory v1.0 § 4.2 の差分式 with US 実送料=0 暫定. 実送料サブシステム後に正式対応)
    return (price_val, tariff_approx)


# =========================================================================
# XML ビルダ
# =========================================================================

def _build_add_fixed_price_item_xml(draft_params: dict, verify: bool = False) -> str:
    """AddFixedPriceItem / VerifyAddFixedPriceItem の XML 本文を組み立てる。

    Args:
        draft_params: build_draft_params_from_phase3() が返す dict
        verify: True の場合、ルート要素を VerifyAddFixedPriceItemRequest に切替

    Returns:
        XML 文字列。USER_TOKEN は `{USER_TOKEN}` プレースホルダのまま残す
        (_call_trading_api が置換する)。

    設計選択:
      - Description は <![CDATA[...]]> で包む。v4 HTML テンプレには <style> や
        SVG のインラインコードが含まれ、xml escape すると巨大な &lt; の列に
        変換されて eBay 側でレンダリング不能になるリスクがある。CDATA により
        "生 HTML" のまま届けられる。CDATA 終端 ]]> が HTML 内に出現した場合は
        _escape_cdata_body で分割して回避する。
      - Title / ItemSpecifics 値 / Location / PostalCode は通常 escape。
      - ItemSpecifics は NameValueList を繰り返し、Name ごとに 1つの Value。
      - PictureURL は最大 _MAX_PICTURES (24) 枚に切る。0枚の場合は
        PictureDetails ブロック自体を出力しない (eBay が Warning を返す)。
    """
    root_tag = 'VerifyAddFixedPriceItemRequest' if verify else 'AddFixedPriceItemRequest'

    sku = _xml_escape(str(draft_params.get('sku') or ''))
    title = _xml_escape(str(draft_params.get('ebay_title') or ''))
    description_html = str(draft_params.get('ebay_description') or '')
    category_id = _xml_escape(str(draft_params.get('ebay_category_id') or ''))
    condition_id = _xml_escape(str(draft_params.get('ebay_condition_id') or '3000'))
    price_usd = float(draft_params.get('listing_price_usd') or 0.0)
    currency = _xml_escape(str(draft_params.get('currency') or _DEFAULT_CURRENCY))
    country = _xml_escape(str(draft_params.get('country') or _DEFAULT_COUNTRY))
    location = _xml_escape(str(draft_params.get('location') or _DEFAULT_LOCATION))
    postal = _xml_escape(str(draft_params.get('postal_code') or _DEFAULT_POSTAL_CODE))
    dispatch_max = int(draft_params.get('dispatch_time_max') or _DEFAULT_DISPATCH_TIME_MAX)
    duration = _xml_escape(str(draft_params.get('listing_duration') or _DEFAULT_LISTING_DURATION))

    payment_pid = _xml_escape(str(draft_params.get('payment_policy_id') or ''))
    return_pid = _xml_escape(str(draft_params.get('return_policy_id') or ''))
    shipping_pid = _xml_escape(str(draft_params.get('shipping_policy_id') or ''))

    # ScheduleTime: 呼出側が `_fixed_schedule_time` を注入していればそれを優先。
    # これで add_fixed_price_item_draft の戻り値 `scheduled_time` と XML 内の値が一致する
    # (datetime.now() の再スナップによる秒単位ズレを防止する)。
    # 2026-05-01: 空文字 ('') も Active 即時公開 sentinel として尊重 (XML から要素自体省略).
    fixed_st = draft_params.get('_fixed_schedule_time')
    if isinstance(fixed_st, str):
        schedule_time = fixed_st
    else:
        _raw_off = draft_params.get('scheduled_days_offset')
        days_offset = int(_raw_off) if _raw_off is not None else _DEFAULT_SCHEDULED_DAYS_OFFSET
        if days_offset <= 0:
            schedule_time = ''  # Active 即時公開
        else:
            schedule_time = _build_schedule_time(days_offset)

    # ItemSpecifics
    specifics = draft_params.get('item_specifics') or {}
    specifics_xml = _build_item_specifics_xml(specifics)

    # PictureURL (最大 24 枚)
    image_urls = list(draft_params.get('image_urls') or [])[:_MAX_PICTURES]
    pictures_xml = _build_pictures_xml(image_urls)

    # Description CDATA
    description_cdata = _wrap_cdata(description_html)

    # SKU element は空なら省略 (eBay は空 SKU を嫌う場合がある)
    sku_line = f'    <SKU>{sku}</SKU>\n' if sku else ''

    # 2026-05-01: schedule_time が空なら ScheduleTime 要素自体を省略 (eBay は不在で即 Active 公開).
    schedule_time_line = (
        f'    <ScheduleTime>{schedule_time}</ScheduleTime>\n' if schedule_time else ''
    )

    # 2026-04-21 追加: ConditionDescription (ランク日本語+quick_notes を eBay のフリーフォーム説明欄へ)
    cond_desc_raw = draft_params.get('condition_description') or ''
    condition_description_line = (
        f'    <ConditionDescription>{_xml_escape(str(cond_desc_raw)[:1000])}</ConditionDescription>\n'
        if cond_desc_raw else ''
    )

    # 2026-04-21 追加: ShippingPackageDetails (weight_g + dimensions) — eBay 計算送料+発送猶予に影響
    shipping_pkg_xml = _build_shipping_package_details_xml(
        weight_g=draft_params.get('weight_g'),
        length_mm=draft_params.get('length_mm'),
        width_mm=draft_params.get('width_mm'),
        depth_mm=draft_params.get('depth_mm'),
    )

    # 2026-05-01 (β fix): eBay 公式 BP override 機構 ShippingServiceCostOverrideList を採用.
    # SellerShippingProfile (BP) はそのまま参照しつつ cost のみ listing 単位で上書き.
    # 4/26 の ShippingDetails 直接指定 (silently ignored) → 5/1 first SellerShippingProfile omit
    # (非標準) → 5/1 second 公式正攻法 ShippingServiceCostOverrideList に修正.
    # 詳細: https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingServiceCostOverrideListType.html
    shipping_cost_override_xml = _build_shipping_service_cost_override_list_xml(
        cost=draft_params.get('shipping_cost_usd_override'),
        additional=draft_params.get('shipping_additional_cost_usd'),
    )

    # Category はカテゴリ ID が空だと eBay 必須エラーになる。呼出側で検証する前提で
    # 空でも出力する (VerifyAdd で検出させる)。
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<{root_tag} xmlns="urn:ebay:apis:eBLBaseComponents">\n'
        '  <RequesterCredentials>\n'
        '    <eBayAuthToken>{USER_TOKEN}</eBayAuthToken>\n'
        '  </RequesterCredentials>\n'
        '  <WarningLevel>High</WarningLevel>\n'
        '  <Item>\n'
        f'    <Title>{title}</Title>\n'
        f'    <Description>{description_cdata}</Description>\n'
        '    <PrimaryCategory>\n'
        f'      <CategoryID>{category_id}</CategoryID>\n'
        '    </PrimaryCategory>\n'
        f'    <ConditionID>{condition_id}</ConditionID>\n'
        f'{sku_line}'
        f'    <StartPrice currencyID="{currency}">{price_usd:.2f}</StartPrice>\n'
        '    <Quantity>1</Quantity>\n'
        f'    <ListingDuration>{duration}</ListingDuration>\n'
        '    <ListingType>FixedPriceItem</ListingType>\n'
        f'    <Country>{country}</Country>\n'
        f'    <Currency>{currency}</Currency>\n'
        f'    <Location>{location}</Location>\n'
        f'    <PostalCode>{postal}</PostalCode>\n'
        f'    <DispatchTimeMax>{dispatch_max}</DispatchTimeMax>\n'
        f'{schedule_time_line}'
        '    <SellerProfiles>\n'
        '      <SellerPaymentProfile>\n'
        f'        <PaymentProfileID>{payment_pid}</PaymentProfileID>\n'
        '      </SellerPaymentProfile>\n'
        '      <SellerReturnProfile>\n'
        f'        <ReturnProfileID>{return_pid}</ReturnProfileID>\n'
        '      </SellerReturnProfile>\n'
        '      <SellerShippingProfile>\n'
        f'        <ShippingProfileID>{shipping_pid}</ShippingProfileID>\n'
        '      </SellerShippingProfile>\n'
        '    </SellerProfiles>\n'
        f'{condition_description_line}'
        f'{shipping_pkg_xml}'
        f'{shipping_cost_override_xml}'
        f'{specifics_xml}'
        f'{pictures_xml}'
        '  </Item>\n'
        f'</{root_tag}>\n'
    )


def _build_shipping_package_details_xml(
    weight_g: Optional[int], length_mm: Optional[int],
    width_mm: Optional[int], depth_mm: Optional[int],
) -> str:
    """ShippingPackageDetails XML を組み立てる (eBay は MajorWeight=lbs + MinorWeight=oz、寸法は inches 推奨)。
    全て None/0 なら空文字を返す。片方だけでも情報があれば部分出力。"""
    lines: list[str] = []
    # 重量 (g → oz 換算、eBay で MeasurementSystem=English で統一)
    try:
        w_g = int(weight_g or 0)
    except (TypeError, ValueError):
        w_g = 0
    if w_g > 0:
        total_oz = w_g / 28.3495
        major_lbs = int(total_oz // 16)
        minor_oz = round(total_oz - major_lbs * 16, 1)
        lines.append('      <MeasurementUnit>English</MeasurementUnit>')
        lines.append(f'      <WeightMajor unit="lbs" measurementSystem="English">{major_lbs}</WeightMajor>')
        lines.append(f'      <WeightMinor unit="oz" measurementSystem="English">{minor_oz}</WeightMinor>')
    # 寸法 (mm → inches)
    def _mm_to_in(v: Optional[int]) -> Optional[float]:
        try:
            return round(float(v or 0) / 25.4, 1) if v else None
        except (TypeError, ValueError):
            return None
    l_in = _mm_to_in(length_mm)
    w_in = _mm_to_in(width_mm)
    d_in = _mm_to_in(depth_mm)
    if l_in and l_in > 0:
        lines.append(f'      <PackageLength unit="inches" measurementSystem="English">{l_in}</PackageLength>')
    if w_in and w_in > 0:
        lines.append(f'      <PackageWidth unit="inches" measurementSystem="English">{w_in}</PackageWidth>')
    if d_in and d_in > 0:
        lines.append(f'      <PackageDepth unit="inches" measurementSystem="English">{d_in}</PackageDepth>')
    if not lines:
        return ''
    return '    <ShippingPackageDetails>\n' + '\n'.join(lines) + '\n    </ShippingPackageDetails>\n'


def _build_shipping_service_cost_override_list_xml(
    cost: Optional[float], additional: Optional[float],
    intl_cost: Optional[float] = None, intl_additional: Optional[float] = None,
    priority: int = 1,
) -> str:
    """eBay 公式 Business Policy cost override 機構 (ShippingServiceCostOverrideList).

    SellerShippingProfile (BP) はそのまま参照しつつ shipping の cost のみを
    listing 単位で上書きする. BP 内の sortOrderId と ShippingServicePriority が
    一致する必要あり (default 1).

    全引数 None なら空文字 (BP の cost 完全踏襲).

    Args:
        cost / additional: Domestic (米国向け) override. None で BP fallback.
        intl_cost / intl_additional: International (他国向け) override. None で BP fallback.

    2026-05-01 修正経緯:
      - 4/21 実装: <ShippingDetails> 直接指定 → BP と同居で silently ignored
      - 4/26 fix: <ShippingType>Flat</ShippingType> 追加 → necessary だが not sufficient
      - 5/1 first: SellerShippingProfile 完全 omit → 動作するが eBay 非標準
      - 5/1 second: ShippingServiceCostOverrideList = eBay 公式正攻法 (Domestic 1 entry)
      - 5/1 W84: International override 引数追加 (4 区分 primary_market 別 XML 切替)

    Reference:
      https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingServiceCostOverrideListType.html
    """
    has_domestic = cost is not None or additional is not None
    has_intl = intl_cost is not None or intl_additional is not None
    if not has_domestic and not has_intl:
        return ''

    def _f(v: Optional[float]) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    entries: list[str] = []
    if has_domestic:
        c, a = _f(cost), _f(additional)
        entries.append(
            '      <ShippingServiceCostOverride>\n'
            '        <ShippingServiceType>Domestic</ShippingServiceType>\n'
            f'        <ShippingServicePriority>{int(priority)}</ShippingServicePriority>\n'
            f'        <ShippingServiceCost currencyID="USD">{c:.2f}</ShippingServiceCost>\n'
            f'        <ShippingServiceAdditionalCost currencyID="USD">{a:.2f}</ShippingServiceAdditionalCost>\n'
            '      </ShippingServiceCostOverride>\n'
        )
    if has_intl:
        ic, ia = _f(intl_cost), _f(intl_additional)
        entries.append(
            '      <ShippingServiceCostOverride>\n'
            '        <ShippingServiceType>International</ShippingServiceType>\n'
            f'        <ShippingServicePriority>{int(priority)}</ShippingServicePriority>\n'
            f'        <ShippingServiceCost currencyID="USD">{ic:.2f}</ShippingServiceCost>\n'
            f'        <ShippingServiceAdditionalCost currencyID="USD">{ia:.2f}</ShippingServiceAdditionalCost>\n'
            '      </ShippingServiceCostOverride>\n'
        )
    return (
        '    <ShippingServiceCostOverrideList>\n'
        + ''.join(entries)
        + '    </ShippingServiceCostOverrideList>\n'
    )


# 2026-04-22 追加: eBay Item Specifics の制約 (ユーザー実例から判明)
# - 各 Value は 65 文字上限 (例: "Seller Notes" 78字で VerifyAdd 失敗)
# - "Unknown" / "N/A" / "-" / "Not specified" 等の placeholder は eBay が required フィールドの
#   missing 扱いするため、空文字相当として除外する
_ITEM_SPECIFIC_VALUE_MAX_LEN = 65
_ITEM_SPECIFIC_PLACEHOLDER_VALUES = frozenset({
    'unknown', 'n/a', 'na', '-', 'none', 'not specified', 'not applicable',
    'tbd', 'tba', '不明', '未定',
})


def _sanitize_item_specific_value(value: str) -> Optional[str]:
    """Item Specific の値を eBay 制約に合わせて正規化する。

    - 前後空白除去、65字で truncate
    - placeholder 語 ("Unknown" 等) は None 返却 (XML で除外させる)

    Returns:
        サニタイズ済みの値。placeholder なら None。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in _ITEM_SPECIFIC_PLACEHOLDER_VALUES:
        return None
    if len(s) > _ITEM_SPECIFIC_VALUE_MAX_LEN:
        s = s[:_ITEM_SPECIFIC_VALUE_MAX_LEN].rstrip()
    return s


def _build_item_specifics_xml(specifics: dict) -> str:
    """ItemSpecifics ブロックを組み立てる。空なら空文字を返す。

    各値は 65 字で truncate し、placeholder 値 (Unknown 等) は除外する。

    #44 (2026-07-04) 原産国混入チェーン封鎖 (3点封鎖の1): 参考 listing の
    ItemSpecifics Keys がノーフィルタで抽出され (ebay_reference_fetcher.py)、
    listing_generator.py のプロンプトが Keys 完全一致を強制した結果、
    Country of Origin / Country/Region of Manufacture / Manufacturer が
    AddItem XML にそのまま送出されるバグがあった (tools/ebay-manager/CLAUDE.md
    「Country of Origin / Manufacturer の layer 分離」違反、関税リスク)。
    G2 の `monitor.ebay_client._is_forbidden_specific_name` (revise_item_specifics
    と同一の禁止 Name 集合) を共有 import して AddItem 経路でも同じフィルタを
    適用する (多層防御、md-files-can-be-wrong: CLAUDE.md に「空送出」と記載が
    あったが実装が存在しなかった)。除外は Q0 (silent skip 禁止) のため
    logger.warning で痕跡を残す。
    """
    if not specifics or not isinstance(specifics, dict):
        return ''
    lines = ['    <ItemSpecifics>']
    emitted_count = 0
    for name, value in specifics.items():
        if not name:
            continue
        if _is_forbidden_specific_name(name):
            logger.warning(
                "_build_item_specifics_xml: 禁止 Name '%s' を除外 "
                "(原産国/Manufacturer 系、CLAUDE.md 規約)", name,
            )
            continue
        # Value が list の場合は複数 <Value> に展開 (eBay 仕様)
        if isinstance(value, (list, tuple)):
            sanitized_values: list[str] = []
            for v in value:
                s = _sanitize_item_specific_value(v)
                if s:
                    sanitized_values.append(s)
            if not sanitized_values:
                continue
            lines.append('      <NameValueList>')
            lines.append(f'        <Name>{_xml_escape(str(name))}</Name>')
            for v in sanitized_values:
                lines.append(f'        <Value>{_xml_escape(v)}</Value>')
            lines.append('      </NameValueList>')
            emitted_count += 1
        else:
            s = _sanitize_item_specific_value(value)
            if not s:
                continue
            lines.append('      <NameValueList>')
            lines.append(f'        <Name>{_xml_escape(str(name))}</Name>')
            lines.append(f'        <Value>{_xml_escape(s)}</Value>')
            lines.append('      </NameValueList>')
            emitted_count += 1
    lines.append('    </ItemSpecifics>')
    if emitted_count == 0:
        # 全部 placeholder で filter されたケースは ItemSpecifics ブロック自体を省略
        return ''
    return '\n'.join(lines) + '\n'


def _build_pictures_xml(image_urls: Optional[list[str]]) -> str:
    """PictureDetails ブロックを組み立てる。

    URL は escape のみで CDATA では包まない (URL 内 & は escape 必須)。
    空リスト / None なら PictureDetails 自体を出力しない。
    """
    if not image_urls:
        return ''
    urls = [u for u in image_urls if u and isinstance(u, str)][:_MAX_PICTURES]
    if not urls:
        return ''
    lines = ['    <PictureDetails>']
    for u in urls:
        lines.append(f'      <PictureURL>{_xml_escape(u)}</PictureURL>')
    lines.append('    </PictureDetails>')
    return '\n'.join(lines) + '\n'


def _wrap_cdata(html: str) -> str:
    """HTML 本文を CDATA セクションで包む。

    HTML 内に `]]>` が出現していた場合は `]]]]><![CDATA[>` で分割して
    CDATA 終端の誤検知を回避する (XML 1.0 仕様に従った CDATA escape テクニック)。
    """
    if html is None:
        return '<![CDATA[]]>'
    safe = str(html).replace(']]>', ']]]]><![CDATA[>')
    return f'<![CDATA[{safe}]]>'


# =========================================================================
# レスポンスパーサ
# =========================================================================

def _parse_add_item_response(response_xml: str) -> dict:
    """AddFixedPriceItem / VerifyAddFixedPriceItem のレスポンス XML を parse する。

    Returns:
        dict:
          - success: bool (Ack=Success/Warning なら True)
          - ack: str ('Success'/'Warning'/'Failure'/'PartialFailure'/None)
          - ebay_item_id: str | None
          - fees: list[dict] ({'name','fee','currency'})
          - errors: list[str] (SeverityCode=Error の LongMessage)
          - warnings: list[str] (SeverityCode=Warning の LongMessage)
          - parse_error: str | None (XML parse 自体が失敗した場合のみ)
    """
    result: dict = {
        'success': False,
        'ack': None,
        'ebay_item_id': None,
        'fees': [],
        'errors': [],
        'warnings': [],
        'parse_error': None,
    }

    if not response_xml:
        result['parse_error'] = 'empty_response_xml'
        return result

    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError as e:
        result['parse_error'] = f'xml_parse_error: {e}'
        return result

    ns = {'ns': _NS}

    # Ack
    ack_el = root.find('ns:Ack', namespaces=ns)
    ack = ack_el.text if ack_el is not None else None
    result['ack'] = ack
    result['success'] = ack in ('Success', 'Warning')

    # ItemID (AddFixedPriceItem のみ。Verify は通常返さないが念のため探す)
    item_el = root.find('ns:ItemID', namespaces=ns)
    if item_el is not None and item_el.text:
        result['ebay_item_id'] = item_el.text.strip()

    # Fees
    for fee_el in root.findall('.//ns:Fees/ns:Fee', namespaces=ns):
        name_el = fee_el.find('ns:Name', namespaces=ns)
        fee_val_el = fee_el.find('ns:Fee', namespaces=ns)
        fee_dict: dict = {
            'name': name_el.text if name_el is not None and name_el.text else '',
            'fee': fee_val_el.text if fee_val_el is not None and fee_val_el.text else '',
            'currency': '',
        }
        if fee_val_el is not None:
            cur = fee_val_el.get('currencyID')
            if cur:
                fee_dict['currency'] = cur
        result['fees'].append(fee_dict)

    # Errors / Warnings
    for err_el in root.findall('.//ns:Errors', namespaces=ns):
        sev_el = err_el.find('ns:SeverityCode', namespaces=ns)
        sev = sev_el.text if sev_el is not None else None
        long_msg_el = err_el.find('ns:LongMessage', namespaces=ns)
        short_msg_el = err_el.find('ns:ShortMessage', namespaces=ns)
        msg = ''
        if long_msg_el is not None and long_msg_el.text:
            msg = long_msg_el.text.strip()
        elif short_msg_el is not None and short_msg_el.text:
            msg = short_msg_el.text.strip()
        if not msg:
            continue
        if sev == 'Warning':
            result['warnings'].append(msg)
        else:
            # Error / 不明 severity は error 扱い (安全側)
            result['errors'].append(msg)

    return result


# =========================================================================
# 公開 API: Verify / Add
# =========================================================================

def _is_hard_expired_error(parsed: dict) -> bool:
    """eBay レスポンスの errors に "hard expired" が含まれるか判定 (W29).

    "Auth token is hard expired" / "User needs to generate a new token" 等の
    トークン失効系エラーをパターン検出する.
    """
    errors = parsed.get('errors') or []
    haystack = ' '.join(str(e) for e in errors).lower()
    return (
        'hard expired' in haystack
        or 'token is invalid' in haystack
        or 'user needs to generate a new token' in haystack
    )


def verify_add_fixed_price_item(
    draft_params: dict,
    app_id: str = '',
    dev_id: str = '',
    cert_id: str = '',
    user_token: str = '',
    config: Optional[dict] = None,
) -> dict:
    """VerifyAddFixedPriceItem (dry-run) を実行する。

    認証情報は引数で明示指定するのが基本だが、どれかが空文字の場合
    `get_ebay_credentials(config)` でフォールバック解決する。

    2026-04-26 W29 追加: eBay が "Auth token is hard expired" を返した場合、
    `refresh_access_token(force=True)` で強制 refresh して **1 回だけリトライ**.
    これで Streamlit プロセスの os.environ が古い token を持っていても
    自動回復するようになる.

    Returns:
        dict:
          - success: bool
          - ack: str | None
          - fees: list[dict]
          - errors: list[str]
          - warnings: list[str]
          - raw_xml: str (デバッグ用。API 失敗時は空文字)
          - token_recovered: bool (リトライで成功した場合 True、初回成功なら False)
    """
    return _verify_add_with_retry(
        draft_params, app_id, dev_id, cert_id, user_token, config,
        retry_attempted=False,
    )


def _verify_add_with_retry(
    draft_params: dict,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
    config: Optional[dict],
    retry_attempted: bool,
) -> dict:
    """VerifyAdd 本体 + hard expired リトライロジック."""
    creds = _resolve_credentials(app_id, dev_id, cert_id, user_token, config)
    missing_error = _check_credentials(creds)
    if missing_error:
        return {
            'success': False,
            'ack': None,
            'fees': [],
            'errors': [missing_error],
            'warnings': [],
            'raw_xml': '',
            'token_recovered': False,
        }

    try:
        xml_body = _build_add_fixed_price_item_xml(draft_params, verify=True)
    except Exception as e:  # noqa: BLE001
        logger.exception('VerifyAdd XML build failed')
        return {
            'success': False,
            'ack': None,
            'fees': [],
            'errors': [f'xml_build_error: {e}'],
            'warnings': [],
            'raw_xml': '',
            'token_recovered': False,
        }

    start = time.time()
    try:
        api_result = _call_trading_api(
            call_name='VerifyAddFixedPriceItem',
            xml_body=xml_body,
            app_id=creds['app_id'],
            dev_id=creds['dev_id'],
            cert_id=creds['cert_id'],
            user_token=creds['user_token'],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception('VerifyAdd API call failed')
        _log_api('w9_verify_add_item', success=False, error=str(e), duration_ms=int((time.time() - start) * 1000))
        return {
            'success': False,
            'ack': None,
            'fees': [],
            'errors': [f'api_call_error: {e}'],
            'warnings': [],
            'raw_xml': '',
            'token_recovered': False,
        }
    duration_ms = int((time.time() - start) * 1000)

    raw_xml = api_result.get('raw') or ''
    # _call_trading_api が通信段階で失敗した場合 raw は None で message に "通信エラー"
    if not api_result.get('success') and not raw_xml:
        err_msg = api_result.get('message') or 'unknown_api_failure'
        _log_api('w9_verify_add_item', success=False, error=err_msg, duration_ms=duration_ms)
        return {
            'success': False,
            'ack': None,
            'fees': [],
            'errors': [err_msg],
            'warnings': [],
            'raw_xml': '',
            'token_recovered': False,
        }

    parsed = _parse_add_item_response(raw_xml)

    # W29: hard expired 検知 → force refresh + リトライ (一回限り)
    if not retry_attempted and _is_hard_expired_error(parsed):
        logger.warning(
            'VerifyAdd: "hard expired" detected, attempting force refresh + retry...'
        )
        try:
            from monitor.ebay_oauth_refresh import refresh_access_token
            refresh_result = refresh_access_token(config=config, force=True)
            if refresh_result.get('success'):
                logger.info('Force refresh succeeded, retrying VerifyAdd...')
                # リトライ. 引数は元の値 (空 + None) を維持 → _resolve_credentials
                # が再度 fresh token を読み出す.
                retry = _verify_add_with_retry(
                    draft_params, '', '', '', '', config,
                    retry_attempted=True,
                )
                retry['token_recovered'] = True
                return retry
            else:
                logger.error(
                    f'Force refresh failed: {refresh_result.get("errors")}'
                )
        except Exception as _re:  # noqa: BLE001
            logger.exception(f'Force refresh exception: {_re}')
    if parsed.get('parse_error'):
        _log_api('w9_verify_add_item', success=False, error=parsed['parse_error'], duration_ms=duration_ms)
        return {
            'success': False,
            'ack': parsed.get('ack'),
            'fees': parsed.get('fees', []),
            'errors': [parsed['parse_error']] + parsed.get('errors', []),
            'warnings': parsed.get('warnings', []),
            'raw_xml': raw_xml,
            'token_recovered': False,
        }

    _log_api(
        'w9_verify_add_item',
        success=bool(parsed['success']),
        error=None if parsed['success'] else '; '.join(parsed.get('errors') or []),
        duration_ms=duration_ms,
    )
    return {
        'success': bool(parsed['success']),
        'ack': parsed.get('ack'),
        'fees': parsed.get('fees', []),
        'errors': parsed.get('errors', []),
        'warnings': parsed.get('warnings', []),
        'raw_xml': raw_xml,
        'token_recovered': False,
    }


def add_fixed_price_item_draft(
    draft_params: dict,
    app_id: str = '',
    dev_id: str = '',
    cert_id: str = '',
    user_token: str = '',
    config: Optional[dict] = None,
) -> dict:
    """AddFixedPriceItem を本番実行する (ScheduleTime = now + 21日)。

    2026-04-26 W29 追加: hard expired 検知 → force refresh + 1 リトライ.

    Returns:
        dict:
          - success: bool
          - ebay_item_id: str | None
          - ack: str | None
          - fees: list[dict]
          - scheduled_time: str (UTC ISO 8601, 例 '2026-05-11T13:00:00.000Z')
          - errors: list[str]
          - warnings: list[str]
          - raw_xml: str (デバッグ用)
          - token_recovered: bool
    """
    return _add_with_retry(
        draft_params, app_id, dev_id, cert_id, user_token, config,
        retry_attempted=False,
    )


def _add_with_retry(
    draft_params: dict,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
    config: Optional[dict],
    retry_attempted: bool,
) -> dict:
    """AddFixedPriceItem 本体 + hard expired リトライロジック."""
    creds = _resolve_credentials(app_id, dev_id, cert_id, user_token, config)
    missing_error = _check_credentials(creds)
    if missing_error:
        return {
            'success': False,
            'ebay_item_id': None,
            'ack': None,
            'fees': [],
            'scheduled_time': '',
            'errors': [missing_error],
            'warnings': [],
            'raw_xml': '',
        }

    # ScheduleTime を事前計算 (XML に埋める値と戻り値の値を完全一致させる)。
    # XML ビルダが独自に _build_schedule_time を再呼出しすると datetime.now() の
    # 再スナップで秒単位ズレが発生するため、確定値を `_fixed_schedule_time` キー
    # に注入して XML ビルダ側で優先採用させる (2026-04-20 HIGH fix)。
    # 2026-05-01: scheduled_days_offset == 0 → Active 即時公開 ('' を sentinel として注入).
    _raw_off = draft_params.get('scheduled_days_offset')
    days_offset = int(_raw_off) if _raw_off is not None else _DEFAULT_SCHEDULED_DAYS_OFFSET
    if days_offset <= 0:
        scheduled_time = ''  # Active 即時公開
    else:
        scheduled_time = _build_schedule_time(days_offset)
    params_snap = dict(draft_params)
    params_snap['_fixed_schedule_time'] = scheduled_time

    try:
        xml_body = _build_add_fixed_price_item_xml(params_snap, verify=False)
    except Exception as e:  # noqa: BLE001
        logger.exception('AddFixedPriceItem XML build failed')
        return {
            'success': False,
            'ebay_item_id': None,
            'ack': None,
            'fees': [],
            'scheduled_time': scheduled_time,
            'errors': [f'xml_build_error: {e}'],
            'warnings': [],
            'raw_xml': '',
        }

    start = time.time()
    try:
        api_result = _call_trading_api(
            call_name='AddFixedPriceItem',
            xml_body=xml_body,
            app_id=creds['app_id'],
            dev_id=creds['dev_id'],
            cert_id=creds['cert_id'],
            user_token=creds['user_token'],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception('AddFixedPriceItem API call failed')
        _log_api('w9_add_item', success=False, error=str(e), duration_ms=int((time.time() - start) * 1000))
        return {
            'success': False,
            'ebay_item_id': None,
            'ack': None,
            'fees': [],
            'scheduled_time': scheduled_time,
            'errors': [f'api_call_error: {e}'],
            'warnings': [],
            'raw_xml': '',
        }
    duration_ms = int((time.time() - start) * 1000)

    raw_xml = api_result.get('raw') or ''
    if not api_result.get('success') and not raw_xml:
        err_msg = api_result.get('message') or 'unknown_api_failure'
        _log_api('w9_add_item', success=False, error=err_msg, duration_ms=duration_ms)
        return {
            'success': False,
            'ebay_item_id': None,
            'ack': None,
            'fees': [],
            'scheduled_time': scheduled_time,
            'errors': [err_msg],
            'warnings': [],
            'raw_xml': '',
        }

    parsed = _parse_add_item_response(raw_xml)

    # W29: hard expired 検知 → force refresh + リトライ (一回限り)
    if not retry_attempted and _is_hard_expired_error(parsed):
        logger.warning(
            'AddFixedPriceItem: "hard expired" detected, force refresh + retry...'
        )
        try:
            from monitor.ebay_oauth_refresh import refresh_access_token
            refresh_result = refresh_access_token(config=config, force=True)
            if refresh_result.get('success'):
                logger.info('Force refresh succeeded, retrying AddFixedPriceItem...')
                retry = _add_with_retry(
                    draft_params, '', '', '', '', config,
                    retry_attempted=True,
                )
                retry['token_recovered'] = True
                return retry
            else:
                logger.error(
                    f'Force refresh failed: {refresh_result.get("errors")}'
                )
        except Exception as _re:  # noqa: BLE001
            logger.exception(f'Force refresh exception: {_re}')

    if parsed.get('parse_error'):
        _log_api('w9_add_item', success=False, error=parsed['parse_error'], duration_ms=duration_ms)
        return {
            'success': False,
            'ebay_item_id': parsed.get('ebay_item_id'),
            'ack': parsed.get('ack'),
            'fees': parsed.get('fees', []),
            'scheduled_time': scheduled_time,
            'errors': [parsed['parse_error']] + parsed.get('errors', []),
            'warnings': parsed.get('warnings', []),
            'raw_xml': raw_xml,
            'token_recovered': False,
        }

    _log_api(
        'w9_add_item',
        success=bool(parsed['success']),
        error=None if parsed['success'] else '; '.join(parsed.get('errors') or []),
        duration_ms=duration_ms,
    )
    return {
        'success': bool(parsed['success']),
        'ebay_item_id': parsed.get('ebay_item_id'),
        'ack': parsed.get('ack'),
        'fees': parsed.get('fees', []),
        'scheduled_time': scheduled_time,
        'errors': parsed.get('errors', []),
        'warnings': parsed.get('warnings', []),
        'raw_xml': raw_xml,
        'token_recovered': False,
    }


# =========================================================================
# 内部ヘルパ
# =========================================================================

def _resolve_credentials(
    app_id: str, dev_id: str, cert_id: str, user_token: str,
    config: Optional[dict],
) -> dict:
    """引数が空の場合 env/config からフォールバック取得する。
    さらに user_token は ebay_oauth_refresh.get_valid_access_token() 経由で
    残り有効時間 < 10分なら自動 refresh する (2026-04-22 FIX)。
    従来: .env の stale な EBAY_USER_TOKEN をそのまま使って
          「Auth token is hard expired」エラーで VerifyAdd が落ちていた。"""
    if all([app_id, dev_id, cert_id, user_token]):
        # 全部明示指定でも、user_token が OAuth 形式 (v^1.1#) なら期限切れチェック
        try:
            from monitor.ebay_oauth_refresh import (
                get_valid_access_token, is_token_near_expiry,
            )
            if user_token.startswith('v^') and is_token_near_expiry():
                refreshed = get_valid_access_token(config=config)
                if refreshed:
                    user_token = refreshed
        except Exception as e:  # noqa: BLE001
            logger.warning(f'OAuth auto-refresh skipped: {e}')
        return {
            'app_id': app_id,
            'dev_id': dev_id,
            'cert_id': cert_id,
            'user_token': user_token,
        }
    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'get_ebay_credentials failed: {e}')
        creds = {'app_id': '', 'dev_id': '', 'cert_id': '', 'user_token': ''}
    # 2026-04-22 追加: .env から取得した user_token が OAuth 形式なら有効期限を確認
    # して必要なら refresh する。Trading API の「hard expired」エラーを防ぐ。
    fallback_token = creds.get('user_token') or ''
    if fallback_token.startswith('v^'):
        try:
            from monitor.ebay_oauth_refresh import get_valid_access_token
            refreshed = get_valid_access_token(config=config)
            if refreshed:
                fallback_token = refreshed
        except Exception as e:  # noqa: BLE001
            logger.warning(f'OAuth auto-refresh skipped: {e}')
    # 引数優先
    return {
        'app_id': app_id or creds.get('app_id') or '',
        'dev_id': dev_id or creds.get('dev_id') or '',
        'cert_id': cert_id or creds.get('cert_id') or '',
        'user_token': user_token or fallback_token,
    }


def _check_credentials(creds: dict) -> Optional[str]:
    """認証情報の欠損チェック。欠損ありならエラーメッセージ、OK なら None。"""
    if not ebay_credentials_ok(creds):
        missing = [k for k, v in creds.items() if not v]
        return f'ebay_credentials_missing: {missing}'
    return None


def _log_api(
    operation: str, success: bool, error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """api_call_log に eBay Trading API 呼出しを記録 (token 不明なので 0)。"""
    try:
        log_api_call(
            provider='ebay',
            model='trading_api',
            operation=operation,
            input_tokens=0,
            output_tokens=0,
            duration_ms=duration_ms,
            success=success,
            error_message=error,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f'_log_api failed (ignored): {e}')


if __name__ == '__main__':
    # 手動検証用: draft_params のダミーから XML を吐くだけ
    import json as _json
    logging.basicConfig(level=logging.INFO)

    demo = {
        'sku': 'W9-DEMO-001',
        'ebay_title': 'Sony WH-1000XM5 Wireless Headphones Black',
        'ebay_description': '<div>Demo HTML</div>',
        'ebay_category_id': '112529',
        'ebay_condition_id': '3000',
        'rank_code': 'A',
        'item_specifics': {'Brand': 'Sony', 'Model': 'WH-1000XM5'},
        'listing_price_usd': 249.99,
        'image_urls': ['https://example.com/1.jpg', 'https://example.com/2.jpg'],
        'payment_policy_id': '359244671023',
        'return_policy_id': '359243687023',
        'shipping_policy_id': '377279091023',
        'country': 'JP',
        'currency': 'USD',
        'location': 'Tokyo, Japan',
        'postal_code': '100-0001',
        'dispatch_time_max': 3,
        'listing_duration': 'GTC',
        'scheduled_days_offset': 21,
    }
    print(_build_add_fixed_price_item_xml(demo, verify=True))
