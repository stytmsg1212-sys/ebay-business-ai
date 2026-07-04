"""W137 (2026-05-17): eBay GetItem 実値スナップショット.

商品管理「eBay 反映」の (A) 反映前 変更検出 (form vs 実 eBay) と
(B) 反映後 verify (Ack でなく実値一致) で使う単一エントリポイント.
加えて W136 修正のため SellerProfiles の 3 profile ID
(Payment/Return/Shipping) を抽出する — Revise XML に SellerProfiles を
同梱しないと BP 管理 listing で ShippingServiceCostOverride が無音失敗する
(2026-05-17 Codex+一次情報+実コード非対称で真因確定) ため.

設計方針:
  - DB は信頼しない (DB↔eBay 乖離前提、実 eBay が真実源).
  - 取得/parse 失敗は **raise せず** ok=False + error を返す (Q0: UI を
    クラッシュさせず、呼出側が「不明なので revise しない/成功と偽らない").
  - listing 識別は ebay_item_id (SKU 不使用、sku-rules 準拠).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx

from monitor.ebay_client import (
    API_VERSION,
    TRADING_API_URL,
    _build_get_item_xml,
    _resolve_active_token,
)

logger = logging.getLogger(__name__)

_NS = {"n": "urn:ebay:apis:eBLBaseComponents"}


@dataclass(frozen=True)
class ListingSnapshot:
    item_id: str
    sku: Optional[str]
    start_price_usd: Optional[float]
    ship_cost_usd: Optional[float]
    ship_additional_usd: Optional[float]
    payment_profile_id: Optional[str]
    return_profile_id: Optional[str]
    shipping_profile_id: Optional[str]
    ack: str            # "Success" / "Warning" / "Fail" / "Error"
    ok: bool            # API 成立 (HTTP/parse/Ack すべて正常) か
    error: Optional[str]
    # --- W142 追加 (末尾、default 付き = 既存 positional/kwargs 構築不変) ---
    # combined-新BP revise の Phase4 verify 用。Ack=Success でも override が
    # 黙殺される W136 無音失敗 (= DDP buffer 喪失 = Section 232 数百ドル/件)
    # を post-state で検出する核心 signal。
    ship_override_present: bool = False        # Domestic override 要素の有無
    ship_override_priority: Optional[int] = None  # その <ShippingServicePriority>
    # --- W220 追加 (末尾、default 付き = 既存構築不変) ---
    # 商品ランク → eBay Condition 反映 (slice3) の pre/post verify 用。
    condition_id: Optional[str] = None         # Item/ConditionID (1000/1500/3000/7000)
    # --- #44 Wave2 (2026-07-04) 追加 (末尾、default 付き = 既存構築不変) ---
    # tasks/task_listing_content_audit.py (US 本体 DB↔eBay 整合性 日次突合) 用。
    title: Optional[str] = None                  # Item/Title
    condition_description: Optional[str] = None   # Item/ConditionDescription
    # ItemSpecifics: {Name: [Value, ...]}。_build_get_item_xml が #44 G2 で
    # <IncludeItemSpecifics>true</IncludeItemSpecifics> を既定送出するよう
    # 修正されたため常時取得できる (実 API probe で IncludeSelector=Details,
    # ItemSpecifics だけでは値が返らないと確定済、data/tmp/coo_scan.py 参照)。
    item_specifics: Optional[dict] = None
    picture_count: Optional[int] = None           # PictureDetails/PictureURL の件数


def _f(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return None


def fetch_listing_snapshot(
    item_id: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> ListingSnapshot:
    """GetItem 1 回で実 eBay の SKU/価格/送料/3 profile ID を取得.

    ok=False の時、呼出側は「実 eBay 不明」として変更検出/verify を中断する
    (Q0: 不明を「変更なし」「成功」と誤魔化さない).

    #44 Wave2 (2026-07-04) 追記: title/condition_description/item_specifics/
    picture_count も本関数で常時 parse する (`_build_get_item_xml` は #44 G2 で
    `<IncludeItemSpecifics>true</IncludeItemSpecifics>` を既定送出するよう
    修正済のため、ItemSpecifics も追加パラメータ無しで取得できる)。
    """
    base = ListingSnapshot(
        item_id=item_id, sku=None, start_price_usd=None,
        ship_cost_usd=None, ship_additional_usd=None,
        payment_profile_id=None, return_profile_id=None,
        shipping_profile_id=None, ack="Fail", ok=False, error=None,
    )

    token = _resolve_active_token(user_token)
    xml_body = _build_get_item_xml(item_id).replace("{USER_TOKEN}", token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(
            TRADING_API_URL, content=xml_body.encode("utf-8"),
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"[snapshot] {item_id} GetItem 通信エラー: {e}")
        return _replace(base, error=f"通信エラー: {e}")

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"[snapshot] {item_id} GetItem XML parse 失敗: {e}")
        return _replace(base, error=f"XML parse error: {e}")

    ack = root.findtext("n:Ack", namespaces=_NS) or "Fail"
    if ack not in ("Success", "Warning"):
        errs = root.findall(".//n:Errors/n:LongMessage", namespaces=_NS)
        msg = "; ".join(e.text for e in errs if e.text) or "Unknown error"
        logger.warning(f"[snapshot] {item_id} GetItem Ack={ack}: {msg}")
        return _replace(base, ack=ack, error=f"API エラー: {msg}")

    item = root.find(".//n:Item", namespaces=_NS)
    if item is None:
        return _replace(base, ack=ack, error="GetItem に Item ノードが無い")

    # 価格: Item/StartPrice を一次、無ければ SellingStatus/CurrentPrice
    start_price = _f(item.findtext("n:StartPrice", namespaces=_NS))
    if start_price is None:
        ss = item.find("n:SellingStatus", namespaces=_NS)
        if ss is not None:
            start_price = _f(ss.findtext("n:CurrentPrice", namespaces=_NS))

    # 送料 (W137 / Codex HIGH 2026-05-17 防御):
    #   BP listing で listing-level cost override が効いた場合、GetItem は
    #   override 値を Item/ShippingServiceCostOverrideList 側に返し、
    #   ShippingDetails/ShippingServiceOptions は BP default を返し得る
    #   (eBay 挙動が一次情報で未確定)。両コンテナを parse し、Domestic の
    #   **override が在ればそれを実効値として優先**、無ければ
    #   ShippingServiceOptions を fallback とする (どちらの挙動でも正しく
    #   verify / DB 同期する)。
    ship_cost = ship_add = None
    sd = item.find("n:ShippingDetails", namespaces=_NS)
    if sd is not None:
        so = sd.find("n:ShippingServiceOptions", namespaces=_NS)
        if so is not None:
            ship_cost = _f(so.findtext("n:ShippingServiceCost", namespaces=_NS))
            ship_add = _f(
                so.findtext("n:ShippingServiceAdditionalCost", namespaces=_NS)
            )
    ov_present = False           # W142: Domestic override 要素が在るか
    ov_priority: Optional[int] = None  # W142: その ShippingServicePriority
    ovl = item.find("n:ShippingServiceCostOverrideList", namespaces=_NS)
    if ovl is not None:
        for ov in ovl.findall(
            "n:ShippingServiceCostOverride", namespaces=_NS
        ):
            if (ov.findtext("n:ShippingServiceType", namespaces=_NS)
                    == "Domestic"):
                # W142: cost が空でも「Domestic override が listing に
                # bind した」事実自体が verify の核心 (Ack 偽装失敗検出)。
                ov_present = True
                _op = ov.findtext(
                    "n:ShippingServicePriority", namespaces=_NS)
                if _op is not None:
                    try:
                        ov_priority = int(str(_op).strip())
                    except (ValueError, TypeError):
                        ov_priority = None
                _oc = _f(ov.findtext(
                    "n:ShippingServiceCost", namespaces=_NS))
                if _oc is not None:
                    ship_cost = _oc          # 実効 = listing-level override
                    ship_add = _f(ov.findtext(
                        "n:ShippingServiceAdditionalCost", namespaces=_NS
                    ))
                break

    # SellerProfiles 3 ID (W136: Revise に同梱必須). 子要素順は不定なので
    # find で個別取得.
    sp = item.find("n:SellerProfiles", namespaces=_NS)
    pay_id = ret_id = ship_id = None
    if sp is not None:
        pp = sp.find("n:SellerPaymentProfile", namespaces=_NS)
        rp = sp.find("n:SellerReturnProfile", namespaces=_NS)
        shp = sp.find("n:SellerShippingProfile", namespaces=_NS)
        if pp is not None:
            pay_id = (pp.findtext("n:PaymentProfileID", namespaces=_NS)
                      or None)
        if rp is not None:
            ret_id = (rp.findtext("n:ReturnProfileID", namespaces=_NS)
                      or None)
        if shp is not None:
            ship_id = (shp.findtext("n:ShippingProfileID", namespaces=_NS)
                       or None)

    # --- #44 Wave2 (2026-07-04): 監査用フィールド (常に parse、無ければ None/0) ---
    title = item.findtext("n:Title", namespaces=_NS) or None
    condition_description = (
        item.findtext("n:ConditionDescription", namespaces=_NS) or None
    )
    item_specifics: dict[str, list] = {}
    isp = item.find("n:ItemSpecifics", namespaces=_NS)
    if isp is not None:
        for nvl in isp.findall("n:NameValueList", namespaces=_NS):
            name = nvl.findtext("n:Name", namespaces=_NS)
            if not name:
                continue
            values = [
                v.text for v in nvl.findall("n:Value", namespaces=_NS) if v.text
            ]
            item_specifics.setdefault(name, []).extend(values)
    picture_count = len(
        item.findall(".//n:PictureDetails/n:PictureURL", namespaces=_NS)
    )

    return ListingSnapshot(
        item_id=item_id,
        sku=item.findtext("n:SKU", namespaces=_NS),
        start_price_usd=start_price,
        ship_cost_usd=ship_cost,
        ship_additional_usd=ship_add,
        payment_profile_id=pay_id,
        return_profile_id=ret_id,
        shipping_profile_id=ship_id,
        ack=ack,
        ok=True,
        error=None,
        ship_override_present=ov_present,
        ship_override_priority=ov_priority,
        # W220: ConditionID (GetItem が標準で返す)。rank→Condition verify 用。
        condition_id=(item.findtext("n:ConditionID", namespaces=_NS) or None),
        title=title,
        condition_description=condition_description,
        item_specifics=item_specifics,
        picture_count=picture_count,
    )


def _replace(snap: ListingSnapshot, **kw) -> ListingSnapshot:
    """frozen dataclass の部分置換 (dataclasses.replace の薄ラッパ)."""
    from dataclasses import replace
    return replace(snap, **kw)
