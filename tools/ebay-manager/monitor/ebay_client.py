"""
eBay Trading API クライアント
アクティブ出品を取得してSKUから仕入元情報を抽出する
"""
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import logging

import httpx

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _safe_int(val, default: int = 0) -> int:
    """安全に int 変換。空文字列/None/非数値は default を返す"""
    try:
        if val is None:
            return default
        s = str(val).strip()
        if not s:
            return default
        return int(s)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    """安全に float 変換。空文字列/None/非数値は default を返す"""
    try:
        if val is None:
            return default
        s = str(val).strip()
        if not s:
            return default
        return float(s)
    except (ValueError, TypeError):
        return default

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
API_VERSION = "967"


def _build_get_selling_xml(page_number: int = 1, entries_per_page: int = 200) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <ActiveList>
    <Include>true</Include>
    <IncludeNotes>false</IncludeNotes>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page_number}</PageNumber>
    </Pagination>
    <Sort>TimeLeft</Sort>
  </ActiveList>
  <DetailLevel>ReturnAll</DetailLevel>
</GetMyeBaySellingRequest>"""


def _resolve_active_token(user_token: str) -> str:
    """Trading API 呼出し直前に OAuth auto-refresh を適用.

    2026-04-21 に実装した REST API 用の get_valid_access_token を Trading API にも統合.
    Access token が期限切れ間近なら自動 refresh して返す. refresh 失敗時は引数そのまま.
    """
    try:
        from monitor.ebay_oauth_refresh import get_valid_access_token
        fresh = get_valid_access_token()
        if fresh:
            return fresh
    except Exception as e:  # noqa: BLE001
        logger.warning(f"OAuth auto-refresh skipped (fallback to env token): {e}")
    return user_token


def get_active_listings(
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> list[dict]:
    """
    アクティブ出品一覧を取得。
    Returns: [{item_id, title, sku, quantity}]
    """
    user_token = _resolve_active_token(user_token)
    all_items = []
    page = 1

    while True:
        xml_body = _build_get_selling_xml(page).replace("{USER_TOKEN}", user_token)

        headers = {
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
            "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
            "X-EBAY-API-APP-NAME": app_id,
            "X-EBAY-API-DEV-NAME": dev_id,
            "X-EBAY-API-CERT-NAME": cert_id,
            "Content-Type": "text/xml",
        }

        try:
            resp = httpx.post(TRADING_API_URL, content=xml_body.encode("utf-8"), headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"eBay API 通信エラー: {e}")

        root = ET.fromstring(resp.text)
        ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

        # デバッグ: 最初のページの最初のアイテムXMLを保存（EBAY_DEBUG=1 のときのみ）
        if page == 1 and os.environ.get("EBAY_DEBUG") == "1":
            try:
                items_in_page = root.findall(".//ns:ActiveList//ns:Item", namespaces=ns)
                if items_in_page:
                    sample_path = BASE_DIR / "_sample_item_response.xml"
                    sample_item_xml = ET.tostring(items_in_page[0], encoding='unicode')
                    sample_path.write_text(sample_item_xml, encoding="utf-8")
                    logger.info(f"Sample item XML saved to {sample_path}")
            except Exception as dbg_err:
                logger.debug(f"Sample XML dump failed: {dbg_err}")

        # エラーチェック
        ack = root.findtext("ns:Ack", namespaces=ns)
        if ack not in ("Success", "Warning"):
            errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
            msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
            raise RuntimeError(f"eBay API エラー: {msg}")

        # アイテム解析
        active_list = root.find(".//ns:ActiveList", namespaces=ns)
        if active_list is None:
            break

        items = active_list.findall(".//ns:Item", namespaces=ns)
        for idx, item in enumerate(items):
            item_id = item.findtext("ns:ItemID", namespaces=ns) or ""
            title = item.findtext("ns:Title", namespaces=ns) or ""
            sku = item.findtext("ns:SKU", namespaces=ns) or ""
            qty = item.findtext("ns:QuantityAvailable", namespaces=ns) or "0"

            # End→Relist 選定用: TimeLeft（残り時間）と StartTime（出品開始）
            time_left_text = item.findtext("ns:TimeLeft", namespaces=ns) or ""
            start_time_text = item.findtext(".//ns:ListingDetails/ns:StartTime", namespaces=ns) or ""

            # メトリクス抽出（WatchCount, HitCount）
            watch_count_text = item.findtext("ns:WatchCount", namespaces=ns) or "0"
            hit_count_text = item.findtext("ns:HitCount", namespaces=ns) or "0"

            # 販売数抽出（QuantitySold が利用可能か確認）
            # eBay API では QuantitySold が直接返される場合と、
            # Quantity - QuantityAvailable で計算する場合がある
            quantity_sold_text = item.findtext("ns:QuantitySold", namespaces=ns) or "0"

            # 価格抽出（CurrentPrice または BuyItNowPrice）
            price_text = item.findtext("ns:CurrentPrice", namespaces=ns) or "0"
            if price_text == "0":
                price_text = item.findtext("ns:BuyItNowPrice", namespaces=ns) or "0"

            # 通貨抽出 (CurrentPrice の currencyID 属性)。
            # eBaymag 各国版判別の唯一の安価な signal: USD=US本体 / CAD・GBP・EUR・AUD=国際版。
            # GetMyeBaySelling は <Site> を返さない (実機確認 2026-06-07) ため通貨で判別する。
            cur_el = item.find("ns:CurrentPrice", namespaces=ns)
            if cur_el is None:
                cur_el = item.find(".//ns:SellingStatus/ns:CurrentPrice", namespaces=ns)
            currency = (cur_el.get("currencyID") if cur_el is not None else "") or ""

            # USA向け送料抽出
            shipping_cost = 0.0
            shipping_details = item.find(".//ns:ShippingDetails", namespaces=ns)

            if shipping_details is not None:
                # 方法1: ShippingServiceOptions > ShippingServiceCost から取得
                # (実際のXML構造: ShippingServiceOptions の直下に ShippingServiceCost)
                cost_text = shipping_details.findtext("ns:ShippingServiceOptions/ns:ShippingServiceCost", namespaces=ns)
                if cost_text:
                    try:
                        shipping_cost = float(cost_text)
                    except ValueError:
                        shipping_cost = 0.0

                # 方法2: ShippingServiceOption (単数) を試す（別の構造の場合）
                if shipping_cost == 0.0:
                    service_options = shipping_details.findall(".//ns:ShippingServiceOption", namespaces=ns)
                    if service_options:
                        # 最初のオプションから抽出
                        cost_text = service_options[0].findtext("ns:ShippingServiceCost", namespaces=ns) or "0"
                        try:
                            shipping_cost = float(cost_text)
                        except ValueError:
                            shipping_cost = 0.0

                # 方法3: InternationalShippingServiceOption を試す
                if shipping_cost == 0.0:
                    intl_options = shipping_details.findall(".//ns:InternationalShippingServiceOption", namespaces=ns)
                    for intl_option in intl_options:
                        ships_to = intl_option.findtext("ns:ShipsTo", namespaces=ns) or ""
                        if "US" in ships_to.upper():
                            cost_text = intl_option.findtext("ns:ShippingServiceCost", namespaces=ns) or "0"
                            try:
                                shipping_cost = float(cost_text)
                                break
                            except ValueError:
                                pass

                # デバッグ: 最初の3件で抽出結果を log（print は scheduler ログで文字化けリスクあり）
                if idx < 3 and shipping_cost > 0:
                    logger.debug(f"[{item_id}] Extracted shipping cost: ${shipping_cost:.2f}")

            all_items.append({
                "item_id": item_id,
                "title": title,
                "sku": sku,
                "quantity": _safe_int(qty),
                "watch_count": _safe_int(watch_count_text),
                "view_count": _safe_int(hit_count_text),
                "sales_count_30d": _safe_int(quantity_sold_text),
                "current_price": _safe_float(price_text),
                "currency": currency,
                "shipping_cost": shipping_cost,
                "time_left_seconds": parse_time_left_to_seconds(time_left_text),
                "start_time": start_time_text,
            })

        # ページネーション
        total_pages_text = root.findtext(
            ".//ns:ActiveList/ns:PaginationResult/ns:TotalNumberOfPages",
            namespaces=ns,
        )
        if not total_pages_text or page >= int(total_pages_text):
            break
        page += 1

    return all_items


def _build_get_item_xml(item_id: str) -> str:
    """
    GetItem リクエストXML生成（1ItemIDごと）
    WatchCount, HitCount, QuantitySold を取得
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeWatchCount>true</IncludeWatchCount>
  <IncludeSelector>Details,ItemSpecifics</IncludeSelector>
</GetItemRequest>"""


def get_item_details_batch(
    item_ids: list[str],
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """
    GetItem APIで複数アイテムの詳細メトリクスを取得（バッチ処理）
    Returns: {item_id: {watch_count, view_count, sales_count_30d}}

    注: GetItem は1ItemIDごとに1リクエスト必要だが、バッチ処理で効率化
    """
    if not item_ids:
        return {}

    # 2026-04-24: OAuth access token を auto-refresh 後に使用
    user_token = _resolve_active_token(user_token)

    results = {}
    success_count = 0
    error_count = 0

    for idx, item_id in enumerate(item_ids):
        try:
            xml_body = _build_get_item_xml(item_id).replace("{USER_TOKEN}", user_token)

            headers = {
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
                "X-EBAY-API-CALL-NAME": "GetItem",
                "X-EBAY-API-APP-NAME": app_id,
                "X-EBAY-API-DEV-NAME": dev_id,
                "X-EBAY-API-CERT-NAME": cert_id,
                "Content-Type": "text/xml",
            }

            resp = httpx.post(TRADING_API_URL, content=xml_body.encode("utf-8"), headers=headers, timeout=30)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

            # エラーチェック
            ack = root.findtext("ns:Ack", namespaces=ns)
            if ack not in ("Success", "Warning"):
                errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
                msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
                logger.debug(f"GetItem {item_id} error: {msg}")
                error_count += 1
                continue

            # アイテム解析
            item = root.find(".//ns:Item", namespaces=ns)
            if item is not None:
                watch_count = _safe_int(item.findtext("ns:WatchCount", namespaces=ns))
                hit_count = _safe_int(item.findtext("ns:HitCount", namespaces=ns))
                # QuantitySold は SellingStatus の子要素
                selling_status = item.find("ns:SellingStatus", namespaces=ns)
                quantity_sold = _safe_int(selling_status.findtext("ns:QuantitySold", namespaces=ns)) if selling_status is not None else 0
                # W222 (2026-06-05): bulk sync で category_id も埋める (daily 02:30 で全件
                # GetItem 済 = 追加 API 無し)。enrich_listings_with_metrics の
                # listing.update() で listing dict にマージ → upsert_ebay_listing が保存。
                # これが無いと bulk path の category_id は常に None で永久 backfill されない。
                category_id = _safe_int(
                    item.findtext("ns:PrimaryCategory/ns:CategoryID", namespaces=ns)
                )

                results[item_id] = {
                    "watch_count": watch_count,
                    "view_count": hit_count,
                    "sales_count_30d": quantity_sold,
                    "category_id": category_id,
                }

                if (idx + 1) % 50 == 0:
                    logger.info(f"GetItem progress: {idx + 1}/{len(item_ids)}")

                success_count += 1

        except Exception as e:
            logger.debug(f"GetItem {item_id} exception: {e}")
            error_count += 1

    logger.info(f"GetItem completed: {success_count} success, {error_count} errors")
    return results


def get_single_listing(
    item_id: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> Optional[dict]:
    """W176-followup (2026-05-27): 単一 ItemID で GetItem を 1 回呼び、
    get_active_listings と同じ field set (item_id, title, sku, quantity,
    current_price, shipping_cost, watch_count, view_count, sales_count_30d)
    を返す。Not found / API error / parse 失敗時は None.

    用途: eBay連携タブの「1 件のみ同期」ボタン (~3 秒で完了)。
    パーサは get_active_listings の Item 要素解析と同 schema (eBay GetItem と
    GetMyeBaySelling の Item は共通スキーマ)。
    """
    if not item_id:
        return None

    user_token = _resolve_active_token(user_token)
    xml_body = _build_get_item_xml(item_id).replace("{USER_TOKEN}", user_token)
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
        resp = httpx.post(TRADING_API_URL, content=xml_body.encode("utf-8"),
                          headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"GetItem {item_id} HTTP error: {e}")
        return None

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"GetItem {item_id} XML parse error: {e}")
        return None

    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        logger.warning(f"GetItem {item_id} API error: {msg}")
        return None

    item = root.find(".//ns:Item", namespaces=ns)
    if item is None:
        return None

    title = item.findtext("ns:Title", namespaces=ns) or ""
    sku = item.findtext("ns:SKU", namespaces=ns) or ""
    qty_text = item.findtext("ns:QuantityAvailable", namespaces=ns) or "0"

    # 価格: GetItem schema は SellingStatus.CurrentPrice が正
    # (get_active_listings は GetMyeBaySelling 経由で Item 直下 CurrentPrice に
    #  flatten される別 schema、本関数とは API が違うため位置が異なる)。
    # 最終 fallback は BuyItNowPrice (Fixed-Price 出品の主要 field)。
    selling_status = item.find("ns:SellingStatus", namespaces=ns)
    price_text = "0"
    cur_el = None
    if selling_status is not None:
        cur_el = selling_status.find("ns:CurrentPrice", namespaces=ns)
        price_text = selling_status.findtext("ns:CurrentPrice", namespaces=ns) or "0"
    if price_text == "0":
        price_text = item.findtext("ns:BuyItNowPrice", namespaces=ns) or "0"

    # 通貨 (eBaymag 各国版判別、get_active_listings と同 signal)。
    currency = (cur_el.get("currencyID") if cur_el is not None else "") or ""

    # 送料: get_active_listings と同じ 3 段階 fallback
    shipping_cost = 0.0
    shipping_details = item.find(".//ns:ShippingDetails", namespaces=ns)
    if shipping_details is not None:
        cost_text = shipping_details.findtext(
            "ns:ShippingServiceOptions/ns:ShippingServiceCost", namespaces=ns
        )
        if cost_text:
            try:
                shipping_cost = float(cost_text)
            except ValueError:
                shipping_cost = 0.0
        if shipping_cost == 0.0:
            service_options = shipping_details.findall(
                ".//ns:ShippingServiceOption", namespaces=ns
            )
            if service_options:
                cost_text = service_options[0].findtext(
                    "ns:ShippingServiceCost", namespaces=ns
                ) or "0"
                try:
                    shipping_cost = float(cost_text)
                except ValueError:
                    shipping_cost = 0.0
        if shipping_cost == 0.0:
            for intl in shipping_details.findall(
                ".//ns:InternationalShippingServiceOption", namespaces=ns
            ):
                ships_to = intl.findtext("ns:ShipsTo", namespaces=ns) or ""
                if "US" in ships_to.upper():
                    cost_text = intl.findtext(
                        "ns:ShippingServiceCost", namespaces=ns
                    ) or "0"
                    try:
                        shipping_cost = float(cost_text)
                        break
                    except ValueError:
                        pass

    watch_count = _safe_int(item.findtext("ns:WatchCount", namespaces=ns))
    view_count = _safe_int(item.findtext("ns:HitCount", namespaces=ns))
    sales_count = _safe_int(
        selling_status.findtext("ns:QuantitySold", namespaces=ns)
    ) if selling_status is not None else 0

    # W222/C-fix (2026-06-05): 商品説明 (description) と eBay カテゴリ ID を抽出。
    # DetailLevel=ReturnAll なので両方とも応答に含まれる。
    # - description: 商品管理の「📥 eBayから現在の説明を取得」用 (listing_description が空の解消)
    # - category_id: カテゴリ別 FVF 計算用 (W222)。PrimaryCategory が leaf カテゴリ
    description = item.findtext("ns:Description", namespaces=ns) or ""
    category_id = _safe_int(
        item.findtext("ns:PrimaryCategory/ns:CategoryID", namespaces=ns)
    )

    return {
        "item_id": item_id,
        "title": title,
        "sku": sku,
        "quantity": _safe_int(qty_text),
        "current_price": _safe_float(price_text),
        "currency": currency,
        "shipping_cost": shipping_cost,
        "watch_count": watch_count,
        "view_count": view_count,
        "sales_count_30d": sales_count,
        "description": description,
        "category_id": category_id,
    }


def enrich_listings_with_metrics(
    listings: list[dict],
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> list[dict]:
    """
    GetMyeBaySelling の結果に ItemLookup で取得したメトリクスを統合
    (watch_count, view_count, sales_count_30d を更新)
    """
    if not listings:
        return listings

    # item_id リストを抽出
    item_ids = [item["item_id"] for item in listings if item.get("item_id")]

    logger.info(f"Fetching detailed metrics for {len(item_ids)} items...")

    # ItemLookup でメトリクスを取得
    metrics_map = get_item_details_batch(item_ids, app_id, dev_id, cert_id, user_token)

    # listings にメトリクスをマージ
    enriched = []
    for listing in listings:
        item_id = listing.get("item_id")
        if item_id and item_id in metrics_map:
            # ItemLookup から取得したメトリクスで上書き
            listing.update(metrics_map[item_id])
        enriched.append(listing)

    return enriched


def _build_revise_inventory_xml(item_id: str, quantity: int) -> str:
    """ReviseInventoryStatus リクエストXML生成"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <InventoryStatus>
    <ItemID>{item_id}</ItemID>
    <Quantity>{quantity}</Quantity>
  </InventoryStatus>
</ReviseInventoryStatusRequest>"""


def revise_inventory_quantity(
    item_id: str,
    quantity: int,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """
    eBay Trading API: ReviseInventoryStatus で在庫数を変更
    Returns: {'success': bool, 'message': str}
    """
    # 2026-04-24: OAuth access token を auto-refresh
    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_inventory_xml(item_id, quantity).replace("{USER_TOKEN}", user_token)

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseInventoryStatus",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }

    try:
        resp = httpx.post(TRADING_API_URL, content=xml_body.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return {'success': False, 'message': f"通信エラー: {e}"}

    # F5 (Codex 2026-05-16): HTTP 200 でも HTML/不正 body が返ると ET.fromstring
    # が ParseError を送出し関数外へ伝播 (inventory_sync が qty_sync_error を
    # 残せず UI/タスクがクラッシュ). graceful に success:False で返す.
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {'success': False, 'message': f"XML parse error: {e}"}
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {'success': True, 'message': f"ItemID {item_id} の在庫を {quantity} に変更しました"}
    else:
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        return {'success': False, 'message': f"API エラー: {msg}"}


def _build_get_user_preferences_xml() -> str:
    """GetUserPreferences (ShowOutOfStockControlPreference のみ要求) リクエスト XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserPreferencesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <ShowOutOfStockControlPreference>true</ShowOutOfStockControlPreference>
  <Version>{API_VERSION}</Version>
</GetUserPreferencesRequest>"""


def get_out_of_stock_control_enabled(
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> Optional[bool]:
    """eBay Trading API GetUserPreferences で Out-of-Stock Control の ON/OFF を取得.

    W133 (2026-05-16): 在庫0 の listing を ReviseInventoryStatus で数量0 に
    する前に **必ず** OOS Control が ON か機械検証するための読み取り専用関数.

    OOS Control が ON のとき eBay は数量0 listing を「販売停止 (hidden)」に
    するだけで listing 自体は残る (検索順位 / watcher 保持). OFF だと数量0 で
    listing が **自動 End** され Defect / 再出品コスト発生.

    Returns:
        True  : OOS Control 有効 (数量0 revise を安全に実行可)
        False : OOS Control 無効 (数量0 revise すると listing が落ちる → 抑止すべき)
        None  : 通信 / 認証 / parse 失敗 = **不明** (安全側に倒し抑止判断に使う)
                Q0 silent skip 防止: 不明を True と誤魔化さず None で返す.
    """
    user_token = _resolve_active_token(user_token)
    xml_body = _build_get_user_preferences_xml().replace("{USER_TOKEN}", user_token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetUserPreferences",
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
        logger.warning(f"GetUserPreferences 通信エラー: {e}")
        return None

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"GetUserPreferences XML parse 失敗: {e}")
        return None
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        logger.warning(f"GetUserPreferences API エラー: {msg}")
        return None

    raw = root.findtext(
        ".//ns:OutOfStockControlPreference", namespaces=ns
    )
    if raw is None:
        logger.warning("GetUserPreferences に OutOfStockControlPreference が無い")
        return None
    return raw.strip().lower() == "true"


def _build_revise_item_sku_xml(item_id: str, new_sku: str) -> str:
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <SKU>{escape(new_sku)}</SKU>
  </Item>
</ReviseItemRequest>"""


def revise_item_sku(
    item_id: str,
    new_sku: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """
    eBay Trading API: ReviseItem で SKU を変更
    Returns: {'success': bool, 'message': str}
    """
    # 2026-04-24: OAuth access token を auto-refresh
    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_item_sku_xml(item_id, new_sku).replace("{USER_TOKEN}", user_token)

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseItem",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }

    try:
        resp = httpx.post(TRADING_API_URL, content=xml_body.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return {'success': False, 'message': f"通信エラー: {e}"}

    # F5 同型 (2026-05-17): HTTP 200 でも HTML/不正 body なら ET.fromstring が
    # ParseError を送出し関数外へ伝播 (商品管理 UI クラッシュ). graceful に
    # success:False で返す (revise_inventory_quantity と同じ防御).
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {'success': False, 'message': f"XML parse error: {e}"}
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {'success': True, 'message': f"ItemID {item_id} の SKU を {new_sku} に変更しました"}
    else:
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        return {'success': False, 'message': f"API エラー: {msg}"}


def _build_revise_item_pictures_xml(item_id: str, picture_urls: list[str]) -> str:
    """W115 H-1: ReviseItem の PictureDetails に PictureURL 配列を渡す XML を組立.

    eBay 制約: PictureURL は最大 12 件、HTTPS or EPS URL 必須、1 番目は Gallery (hero) 画像.
    """
    from xml.sax.saxutils import escape
    url_xml = "\n    ".join(
        f"<PictureURL>{escape(u)}</PictureURL>" for u in picture_urls
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <PictureDetails>
    {url_xml}
    </PictureDetails>
  </Item>
</ReviseItemRequest>"""


def revise_item_pictures(
    item_id: str,
    picture_urls: list[str],
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """W115 H-1: eBay Trading API ReviseItem で active listing の PictureDetails を更新.

    Args:
        item_id: eBay ItemID (12 桁)
        picture_urls: HTTPS or EPS URL の list (最大 12 件、1 番目が Gallery hero)

    Returns:
        {'success': bool, 'message': str, 'picture_urls': list[str]} — picture_urls は
        送信した URL リスト (caller が GetItem 検証経路で突合に使う、H-6 DoD 連動).

    eBay 制約 (caller 側で事前検証推奨):
      - URL は HTTPS or EPS (https://i.ebayimg.com/...) のみ受理. http:// は reject
      - 最大 12 件. 12 件超は eBay 側で 13 件目以降切捨 (silent drop、Q0 注意)
      - hero (1 番目) はまとめて変更可能だが、変更時 Listing Quality 監査対象
    """
    if not picture_urls:
        return {'success': False, 'message': 'picture_urls is empty', 'picture_urls': []}
    if len(picture_urls) > 12:
        # eBay 側 silent drop 防止のため caller に明示エラー (Q0 silent skip prevention).
        return {
            'success': False,
            'message': f'picture_urls exceeds eBay limit (12), got {len(picture_urls)}',
            'picture_urls': picture_urls,
        }
    for u in picture_urls:
        if not u.startswith('https://'):
            return {
                'success': False,
                'message': f'all picture_urls must start with https:// (got {u!r})',
                'picture_urls': picture_urls,
            }

    # OAuth access token auto-refresh (revise_item_sku と同パターン)
    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_item_pictures_xml(item_id, picture_urls).replace(
        "{USER_TOKEN}", user_token
    )

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseItem",
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
    except Exception as e:
        return {
            'success': False, 'message': f"通信エラー: {e}",
            'picture_urls': picture_urls,
        }

    root = ET.fromstring(resp.text)
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {
            'success': True,
            'message': f"ItemID {item_id} の写真 {len(picture_urls)} 件を更新しました",
            'picture_urls': picture_urls,
        }
    errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
    msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
    return {
        'success': False, 'message': f"API エラー: {msg}",
        'picture_urls': picture_urls,
    }


def _build_revise_item_description_xml(item_id: str, description_html: str) -> str:
    """ReviseItem の Description (HTML body) を更新する XML を組立.

    Description は CDATA で wrap (HTML 内 `<`, `>`, `&` が entity escape されると
    eBay 表示崩れ)。HTML 内に `]]>` が含まれる場合は CDATA premature close
    防止のため `]]]]><![CDATA[>` に置換 (XML 仕様準拠の安全策)。
    """
    from xml.sax.saxutils import escape
    # CDATA premature close 防止 (HTML 本文に `]]>` リテラルがあると section が
    # 閉じてしまい後続が parse 不能になる)。
    safe_html = (description_html or "").replace("]]>", "]]]]><![CDATA[>")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <Description><![CDATA[{safe_html}]]></Description>
  </Item>
</ReviseItemRequest>"""


def revise_item_description(
    item_id: str,
    description_html: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """W148-X (2026-05-20 user 緊急要望): eBay Trading API ReviseItem で
    active listing の Description (HTML body) を更新する。

    用途: 仕入先候補「採用」後、新仕入先 URL から description を再生成して
    既存 listing に反映する supplier_candidates 経路 (= 個別出品の
    description 生成相当を Revise 経路で動かす)。

    Args:
        item_id: eBay ItemID (12 桁)
        description_html: 新規 description HTML body (CDATA で wrap される)

    Returns:
        {'success': bool, 'message': str, 'description_len': int}

    eBay 制約 (caller 側で事前検証推奨):
      - Description は最大約 500,000 文字 (caller では generate_listing 由来の
        通常サイズなので超えない想定、超えた場合は API 側 error 返却)
      - 空 Description は reject (caller 側で空チェック)
      - HTML 内に `]]>` リテラルがあった場合は本関数内で safe 化 (CDATA escape)
    """
    if not (description_html or "").strip():
        return {
            'success': False,
            'message': 'description_html is empty',
            'description_len': 0,
        }

    # OAuth access token auto-refresh (revise_item_pictures と同パターン)
    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_item_description_xml(
        item_id, description_html,
    ).replace("{USER_TOKEN}", user_token)

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseItem",
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
    except Exception as e:
        return {
            'success': False, 'message': f"通信エラー: {e}",
            'description_len': len(description_html or ""),
        }

    # Codex 2026-05-20 HIGH 対応: eBay が HTTP 200 で invalid XML (HTML エラー
    # ページ等) を返す場合に ET.ParseError で crash → UI が apply_result を
    # set できず無音失敗化するのを防ぐ。revise_item_sku L555-558 と同 guard。
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {
            'success': False,
            'message': f"XML parse error: {e}",
            'description_len': len(description_html or ""),
        }
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}

    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {
            'success': True,
            'message': (
                f"ItemID {item_id} の description を更新しました "
                f"({len(description_html)} 文字)"
            ),
            'description_len': len(description_html),
        }
    errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
    msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
    return {
        'success': False, 'message': f"API エラー: {msg}",
        'description_len': len(description_html or ""),
    }


def _build_revise_item_condition_xml(
    item_id: str, condition_id: str, condition_description: Optional[str] = None
) -> str:
    """ReviseFixedPriceItem の ConditionID (+任意 ConditionDescription) 更新 XML.

    W220 (2026-06-04): 商品ランク変更を eBay の Condition に反映。ConditionID は
    eBay 標準値 (1000=New / 1500=New other / 3000=Used / 7000=For parts/As-Is)。
    ConditionDescription は used/as-is で買い手向けフリーフォーム説明 (≤1000 字)。
    指定時のみ送る (未指定なら既存 ConditionDescription を eBay 側で維持)。
    """
    from xml.sax.saxutils import escape
    cd = ""
    if condition_description and condition_description.strip():
        cd = (f"\n    <ConditionDescription>"
              f"{escape(condition_description.strip()[:1000])}"
              f"</ConditionDescription>")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <ConditionID>{escape(str(condition_id))}</ConditionID>{cd}
  </Item>
</ReviseFixedPriceItemRequest>"""


def revise_item_condition(
    item_id: str,
    condition_id: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
    condition_description: Optional[str] = None,
) -> dict:
    """W220 (2026-06-04): active fixed-price listing の ConditionID を更新.

    商品ランク (N/S/A/B/C/D/PO/As-Is) → eBay ConditionID への反映。caller が
    rank→condition_id を解決して渡す。ConditionID 値が category で不可
    (例 S=1500 制限) の場合は eBay が Ack=Failure を返すので caller が
    fallback (3000 等) を判断する (Q0: 本関数は成否を正直に返す、握り潰さない)。

    Returns: {'success': bool, 'message': str, 'condition_id': str}
    """
    cid = str(condition_id or "").strip()
    if not cid:
        return {'success': False, 'message': 'condition_id is empty',
                'condition_id': cid}
    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_item_condition_xml(
        item_id, cid, condition_description,
    ).replace("{USER_TOKEN}", user_token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
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
    except Exception as e:
        return {'success': False, 'message': f"通信エラー: {e}",
                'condition_id': cid}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {'success': False, 'message': f"XML parse error: {e}",
                'condition_id': cid}
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {'success': True,
                'message': f"ItemID {item_id} の ConditionID を {cid} に更新",
                'condition_id': cid}
    errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
    msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
    return {'success': False, 'message': f"API エラー: {msg}",
            'condition_id': cid}


def _build_revise_item_title_xml(item_id: str, new_title: str) -> str:
    """ReviseItem の Title を更新する XML を組立.

    eBay 制約: Title は最大 80 文字 (caller 側で validate 済を前提)。
    XML escape で `<`, `>`, `&` を entity 化する。
    """
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <Title>{escape(new_title)}</Title>
  </Item>
</ReviseItemRequest>"""


def revise_item_title(
    item_id: str,
    new_title: str,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """W31 (2026-06-20): active listing の Title を ReviseItem で更新。

    eBay 制約:
      - Title は **80 文字以内** (超過は本関数が reject、eBay 送出しない)
      - 空文字は reject
      - XML escape (saxutils.escape) 適用

    反映後 GetItem で実値 Title が一致するか verify (Ack 偽装成功防止 Q0)。

    Returns:
        {'success': bool, 'message': str, 'new_title': str}
    """
    title = (new_title or "").strip()
    if not title:
        return {'success': False, 'message': 'new_title is empty', 'new_title': title}
    if len(title) > 80:
        return {
            'success': False,
            'message': f"Title が 80 文字超 ({len(title)} 文字) — eBay 制約違反のため送出しません",
            'new_title': title,
        }

    user_token = _resolve_active_token(user_token)
    xml_body = _build_revise_item_title_xml(item_id, title).replace(
        "{USER_TOKEN}", user_token
    )
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "ReviseItem",
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
    except Exception as e:
        return {'success': False, 'message': f"通信エラー: {e}", 'new_title': title}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {'success': False, 'message': f"XML parse error: {e}", 'new_title': title}

    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        return {'success': False, 'message': f"API エラー: {msg}", 'new_title': title}

    # post-verify: GetItem で実値 Title が一致するか確認 (Ack でなく実値で判定 Q0)
    try:
        xml_get = _build_get_item_xml(item_id).replace("{USER_TOKEN}", user_token)
        hdr_get = {
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-APP-NAME": app_id,
            "X-EBAY-API-DEV-NAME": dev_id,
            "X-EBAY-API-CERT-NAME": cert_id,
            "Content-Type": "text/xml",
        }
        resp_get = httpx.post(
            TRADING_API_URL, content=xml_get.encode("utf-8"),
            headers=hdr_get, timeout=30,
        )
        resp_get.raise_for_status()
        root_get = ET.fromstring(resp_get.text)
        actual_title = (
            root_get.findtext(".//ns:Item/ns:Title", namespaces=ns) or ""
        ).strip()
        if actual_title == title:
            return {
                'success': True,
                'message': f"ItemID {item_id} の Title を更新しました ({len(title)} 文字)",
                'new_title': title,
            }
        else:
            return {
                'success': False,
                'message': (
                    f"Title 反映 verify 失敗 (送出={title!r}, 実値={actual_title!r}) "
                    f"— GetItem で不一致。eBay 管理画面を確認してください。"
                ),
                'new_title': title,
            }
    except Exception as e:
        # verify が通信失敗 → ReviseItem 自体は Ack=Success だったがサイレント成功扱いしない
        return {
            'success': False,
            'message': f"Revise Ack=Success だが verify GetItem 通信エラー: {e}",
            'new_title': title,
        }


def filter_items_with_sku(items: list[dict]) -> list[dict]:
    """SKUが設定されているアイテムのみ返す"""
    return [i for i in items if i.get("sku", "").strip()]


# =================================================================
# End→Relist (SEO ブースト) 機能: 2026-04-19 実装
# =================================================================

def parse_time_left_to_seconds(time_left_str: str) -> Optional[int]:
    """eBay の TimeLeft (ISO 8601 duration) を秒数に変換。

    例: 'PT1H2M26S' → 3746秒, 'P5DT2H' → 439200秒, 'PT0S' → 0
    失敗時 None。
    """
    if not time_left_str:
        return None
    import re as _re
    m = _re.match(
        r'^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$',
        time_left_str.strip(),
    )
    if not m:
        return None
    d = int(m.group(1) or 0)
    h = int(m.group(2) or 0)
    mi = int(m.group(3) or 0)
    s = int(m.group(4) or 0)
    return d * 86400 + h * 3600 + mi * 60 + s


def _build_end_item_xml(item_id: str, end_reason: str = "Incorrect") -> str:
    """EndItem リクエスト XML 生成。

    end_reason 候補: 'NotAvailable', 'Incorrect', 'LostOrBroken',
                     'OtherListingError', 'SellToHighBidder'
    SEO ブースト目的の自発終了なら 'Incorrect' が無難（"listing details are incorrect"）。
    """
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{escape(item_id)}</ItemID>
  <EndingReason>{escape(end_reason)}</EndingReason>
</EndItemRequest>"""


def end_item(
    item_id: str, app_id: str, dev_id: str, cert_id: str, user_token: str,
    end_reason: str = "Incorrect",
) -> dict:
    """eBay Trading API: EndItem で listing を終了。

    Returns: {success, message, end_time}
    """
    # 2026-04-24: OAuth access token を auto-refresh
    user_token = _resolve_active_token(user_token)
    xml = _build_end_item_xml(item_id, end_reason).replace("{USER_TOKEN}", user_token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "EndItem",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(TRADING_API_URL, content=xml.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return {"success": False, "message": f"通信エラー: {e}"}

    root = ET.fromstring(resp.text)
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        end_time = root.findtext("ns:EndTime", namespaces=ns) or ""
        return {"success": True, "message": f"ItemID {item_id} を終了しました", "end_time": end_time}
    errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
    msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
    return {"success": False, "message": f"API エラー: {msg}"}


def _build_relist_fixed_price_xml(item_id: str) -> str:
    """RelistFixedPriceItem リクエスト XML 生成。

    最小限の構造: ItemID のみを指定して「ほぼ同じ内容で再出品」。
    ShippingDetails / SellerProfiles / title / pictures など全て自動継承される。
    """
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<RelistFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
  </Item>
</RelistFixedPriceItemRequest>"""


def _build_verify_relist_xml(item_id: str) -> str:
    """VerifyRelistItem = dry-run。実listingは作成せず検証のみ。

    Note: VerifyRelistFixedPriceItem は廃止されているため一般版 VerifyRelistItem を使う。
    """
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<VerifyRelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
  </Item>
</VerifyRelistItemRequest>"""


def _call_trading_api(
    call_name: str, xml_body: str,
    app_id: str, dev_id: str, cert_id: str, user_token: str,
    timeout: int = 45,
) -> dict:
    """Trading API 共通ラッパ。

    2026-04-23: OAuth access token を呼出前に auto-refresh.
    Refresh token (18ヶ月) が有効な限り access token 期限切れを自動解消する.
    """
    user_token = _resolve_active_token(user_token)
    body = xml_body.replace("{USER_TOKEN}", user_token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(
            TRADING_API_URL, content=body.encode("utf-8"),
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"success": False, "message": f"通信エラー: {e}", "raw": None}

    # F5 同型 (2026-05-17 Codex finding 4): HTTP 200 でも eBay メンテ HTML /
    # gateway エラーページ等 非XML body が返ると ET.fromstring が ParseError を
    # 送出し、本ラッパを経由する全 Revise/Relist/End 系 (revise_fixed_price_with
    # _shipping 等) へ伝播し UI/scheduler がクラッシュ. graceful に success:False.
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {
            "success": False,
            "message": f"XML parse error: {e}",
            "raw": resp.text,
        }
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack in ("Success", "Warning"):
        return {
            "success": True,
            "ack": ack,
            "new_item_id": root.findtext("ns:ItemID", namespaces=ns) or "",
            "start_time": root.findtext("ns:StartTime", namespaces=ns) or "",
            "end_time": root.findtext("ns:EndTime", namespaces=ns) or "",
            "fees": [e.text for e in root.findall(".//ns:Fees/ns:Fee/ns:Fee", namespaces=ns)],
            "warnings": [e.text for e in root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)],
            "raw": resp.text,
        }
    errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
    msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
    return {"success": False, "message": f"API エラー: {msg}", "raw": resp.text}


def verify_relist_item(
    item_id: str, app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> dict:
    """VerifyRelistItem = dry-run。実listingは作らず、relistが可能か検証のみ。

    事前チェックで使う。実運用では必須ではないが、本番実行前の安全装置。
    """
    return _call_trading_api(
        "VerifyRelistItem", _build_verify_relist_xml(item_id),
        app_id, dev_id, cert_id, user_token,
    )


def _build_revise_fixed_price_xml(item_id: str, new_price_usd: float) -> str:
    """ReviseFixedPriceItem 価格変更 XML.

    既存出品の StartPrice (= 表示価格) を更新する. ItemID + 価格のみで
    他フィールド (送料 / shipping profile / pictures / item specifics) は
    自動継承される.

    new_price_usd は USD で 0.01 刻み. 0 以下は呼出側で reject 必須.
    """
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{escape(item_id)}</ItemID>
    <StartPrice currencyID="USD">{new_price_usd:.2f}</StartPrice>
  </Item>
  <Version>{API_VERSION}</Version>
</ReviseFixedPriceItemRequest>"""


def _build_revise_with_shipping_xml(
    item_id: str,
    new_price_usd: Optional[float],
    ship_cost_usd: Optional[float],
    ship_additional_usd: Optional[float],
    seller_profiles: Optional[dict] = None,
    ship_priority: int = 1,
    force_seller_profiles: bool = False,
) -> str:
    """ReviseFixedPriceItem で price + shipping (Buyer pays + each identical item) を更新.

    送料は `ShippingServiceCostOverrideList` で Business Policy の cost を上書き
    (BP 自体は維持). 商品管理タブの「DB + eBay 反映」ボタンから呼ばれる.

    W136 修正 (2026-05-17): BP 管理 listing では override が policy に bind する
    ため、ShippingServiceCostOverrideList と **同一 Revise request 内に
    SellerProfiles (SellerShippingProfile) を同梱**しないと eBay は Ack=Success
    を返しつつ override を無音で無視する (出品 Add 経路は SellerProfiles 同梱で
    効くが、旧 Revise 経路は欠落で無音失敗。audit-2026-05-01 8/9 不適用 +
    eBay 一次情報 + Codex 2 段検証で真因確定). seller_profiles が与えられ
    override がある時のみ SellerProfiles を出力.

    Args:
        item_id: eBay listing ID
        new_price_usd: 新しい商品価格. None で価格変更しない.
        ship_cost_usd: 新しい Domestic shipping cost (Buyer pays).
                       None で送料変更しない.
        ship_additional_usd: 新しい Domestic shipping additional cost (each identical item).
                             ship_cost_usd と同時に指定する想定. 単独 None なら 0 として送る.
        seller_profiles: {'payment_id','return_id','shipping_id'} dict.
                         None or shipping_id 不在なら SellerProfiles を出さない
                         (= 旧挙動、後方互換 D1). 送料 override を確実に効かせる
                         には呼出側が GetItem 由来の 3 ID を渡すこと.

    Reference:
        https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingServiceCostOverrideListType.html
    """
    from xml.sax.saxutils import escape
    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="utf-8"?>')
    parts.append('<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">')
    parts.append('  <RequesterCredentials>')
    parts.append('    <eBayAuthToken>{USER_TOKEN}</eBayAuthToken>')
    parts.append('  </RequesterCredentials>')
    parts.append('  <Item>')
    parts.append(f'    <ItemID>{escape(item_id)}</ItemID>')
    if new_price_usd is not None and new_price_usd > 0:
        parts.append(
            f'    <StartPrice currencyID="USD">{new_price_usd:.2f}</StartPrice>'
        )
    # W136: 送料 override がある時、BP 参照 (SellerProfiles) を同梱.
    # shipping_id が無ければ出さない (旧挙動維持 = 既存テスト不変, D1).
    # W142 HIGH-1 fix: combined-新BP で override 無し (custom 送料を持たない
    # listing の BP 差替) の場合、ship_cost_usd is None だと SellerProfiles
    # も出ず **新BPが無音欠落** する。force_seller_profiles=True (combined
    # 経路専用) で override 有無に関わらず SellerProfiles を出す。default
    # False = 既存挙動完全不変 (price-only revise は SellerProfiles 非同梱、
    # W136/W137 既存テスト機械的不変、D1 後方互換)。
    _sp = seller_profiles or {}
    if (ship_cost_usd is not None or force_seller_profiles) \
            and _sp.get("shipping_id"):
        parts.append('    <SellerProfiles>')
        if _sp.get("payment_id"):
            parts.append('      <SellerPaymentProfile>')
            parts.append(
                f'        <PaymentProfileID>{escape(str(_sp["payment_id"]))}'
                '</PaymentProfileID>'
            )
            parts.append('      </SellerPaymentProfile>')
        if _sp.get("return_id"):
            parts.append('      <SellerReturnProfile>')
            parts.append(
                f'        <ReturnProfileID>{escape(str(_sp["return_id"]))}'
                '</ReturnProfileID>'
            )
            parts.append('      </SellerReturnProfile>')
        parts.append('      <SellerShippingProfile>')
        parts.append(
            f'        <ShippingProfileID>{escape(str(_sp["shipping_id"]))}'
            '</ShippingProfileID>'
        )
        parts.append('      </SellerShippingProfile>')
        parts.append('    </SellerProfiles>')
    if ship_cost_usd is not None:
        ac = float(ship_additional_usd) if ship_additional_usd is not None else 0.0
        parts.append('    <ShippingServiceCostOverrideList>')
        parts.append('      <ShippingServiceCostOverride>')
        parts.append('        <ShippingServiceType>Domestic</ShippingServiceType>')
        # W142: 旧実装は priority=1 ハードコード。combined-新BP で新BP の
        # domestic service の sortOrder が 1 でないと eBay が Ack=Success を
        # 返しつつ override を黙殺する (W136 無音失敗 = DDP buffer 喪失 =
        # Section 232 数百ドル/件)。default=1 = 既存経路の XML 完全不変
        # (後方互換、W136/W137 既存テスト機械的回帰)。combined-新BP 経路
        # のみ呼出側が新BP由来の解決済 priority を渡す。
        parts.append(
            f'        <ShippingServicePriority>{int(ship_priority)}'
            '</ShippingServicePriority>'
        )
        parts.append(
            f'        <ShippingServiceCost currencyID="USD">{float(ship_cost_usd):.2f}</ShippingServiceCost>'
        )
        parts.append(
            f'        <ShippingServiceAdditionalCost currencyID="USD">{ac:.2f}</ShippingServiceAdditionalCost>'
        )
        parts.append('      </ShippingServiceCostOverride>')
        parts.append('    </ShippingServiceCostOverrideList>')
    parts.append('  </Item>')
    parts.append(f'  <Version>{API_VERSION}</Version>')
    parts.append('</ReviseFixedPriceItemRequest>')
    return '\n'.join(parts)


def revise_fixed_price_with_shipping(
    item_id: str,
    new_price_usd: Optional[float],
    ship_cost_usd: Optional[float],
    ship_additional_usd: Optional[float],
    app_id: str, dev_id: str, cert_id: str, user_token: str,
    seller_profiles: Optional[dict] = None,
    ship_priority: int = 1,
    force_seller_profiles: bool = False,
) -> dict:
    """ReviseFixedPriceItem で price + shipping を同時更新.

    商品管理タブ「📤 DB + eBay 反映」ボタンから呼出.
    成功時 ebay_listings.current_price / shipping_cost を呼出側で更新する責務分離.

    W136 (2026-05-17): 送料 override を BP 管理 listing で効かせるには
    seller_profiles ({'payment_id','return_id','shipping_id'}) を渡すこと.
    None なら SellerProfiles 非同梱 = 旧挙動 (後方互換 D1、ただし送料は
    無音失敗し得る). 呼出側 (_apply_to_ebay) は反映前 GetItem の 3 ID を渡す.

    W142 (2026-05-19): combined-新BP では ship_priority に新BP の domestic
    service sortOrder を渡す (eBay 公式: ShippingServicePriority は BP の
    matching service の sortOrder と一致させる)。default=1 = 既存呼出
    (W136/W137 経路) の XML 完全不変 (後方互換、機械的回帰で担保).

    Returns:
        {'success': bool, 'ack': str, 'message': str | None, ...}
    """
    # W142 HIGH-A fix: 旧ゲートは price/ship のみ判定し SellerProfiles 単独
    # 差替 (combined-新BP で override 無 listing = ship_cost None) を
    # 「変更対象がない」と無音棄却 → HIGH-1 で足した force_seller_profiles
    # が XML 構築前に殺され BP 変更が黙って消える + 誤誘導 message。
    # BP 差替意図 (force_seller_profiles ∧ shipping_id) も「変更対象」。
    # default False = 既存 (W136/W137) 経路は判定式が原型と完全同値
    # (price/ship のみ) = 後方互換不変。
    _sp_g = seller_profiles or {}
    _has_bp_swap = force_seller_profiles and bool(_sp_g.get("shipping_id"))
    if (new_price_usd is None or new_price_usd <= 0) \
            and ship_cost_usd is None and not _has_bp_swap:
        return {
            "success": False,
            "message": "価格・送料・BP どれも変更対象がない",
            "raw": None,
        }
    xml = _build_revise_with_shipping_xml(
        item_id, new_price_usd, ship_cost_usd, ship_additional_usd,
        seller_profiles=seller_profiles,
        ship_priority=ship_priority,
        force_seller_profiles=force_seller_profiles,
    )
    result = _call_trading_api(
        "ReviseFixedPriceItem", xml,
        app_id, dev_id, cert_id, user_token,
    )
    # Warning ack でも SeverityCode=Error が混じれば失敗扱い (revise_fixed_price_item と同じ).
    if result.get("success") and result.get("ack") == "Warning":
        raw_xml = result.get("raw") or ""
        if raw_xml:
            try:
                root = ET.fromstring(raw_xml)
                ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
                fatal_msgs = []
                for err in root.findall(".//ns:Errors", namespaces=ns):
                    sev = err.findtext("ns:SeverityCode", namespaces=ns)
                    if sev == "Error":
                        long_msg = err.findtext("ns:LongMessage", namespaces=ns) or ""
                        code = err.findtext("ns:ErrorCode", namespaces=ns) or "?"
                        fatal_msgs.append(f"[{code}] {long_msg}")
                if fatal_msgs:
                    return {
                        **result,
                        "success": False,
                        "message": (
                            "API Warning に重大エラー混入 (SeverityCode=Error): "
                            + "; ".join(fatal_msgs)
                        ),
                    }
            except ET.ParseError:
                pass
    return result


def _build_revise_bp_only_xml(item_id: str, seller_profiles: dict) -> str:
    """W138 (2026-05-17): shipping Business Policy のみ変更する Revise XML.

    `<Item><ItemID>` + `<SellerProfiles>` (Payment/Return/Shipping 3 ID) のみ。
    `<StartPrice>` / `<ShippingServiceCostOverrideList>` は **出力しない**
    (= BP を別 policy に差し替えるだけ。override は eBay 仕様で BP default に
    リセットされる前提、W138 案2)。

    重要 (Codex HIGH-2): SellerProfiles は **Payment/Return/Shipping の 3 ID**
    を同梱する (shipping のみは不可。不完全だと Ack=Fail or 意図せぬ
    payment/return policy 適用 = money/account risk)。W136
    `_build_revise_with_shipping_xml` の SellerProfiles 構造と同形 (gate は
    一切共有せず別関数 = W136 経路非改修, K2/D1)。
    """
    from xml.sax.saxutils import escape
    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="utf-8"?>')
    parts.append('<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">')
    parts.append('  <RequesterCredentials>')
    parts.append('    <eBayAuthToken>{USER_TOKEN}</eBayAuthToken>')
    parts.append('  </RequesterCredentials>')
    parts.append('  <Item>')
    parts.append(f'    <ItemID>{escape(item_id)}</ItemID>')
    parts.append('    <SellerProfiles>')
    if seller_profiles.get("payment_id"):
        parts.append('      <SellerPaymentProfile>')
        parts.append(
            f'        <PaymentProfileID>'
            f'{escape(str(seller_profiles["payment_id"]))}</PaymentProfileID>'
        )
        parts.append('      </SellerPaymentProfile>')
    if seller_profiles.get("return_id"):
        parts.append('      <SellerReturnProfile>')
        parts.append(
            f'        <ReturnProfileID>'
            f'{escape(str(seller_profiles["return_id"]))}</ReturnProfileID>'
        )
        parts.append('      </SellerReturnProfile>')
    parts.append('      <SellerShippingProfile>')
    parts.append(
        f'        <ShippingProfileID>'
        f'{escape(str(seller_profiles["shipping_id"]))}</ShippingProfileID>'
    )
    parts.append('      </SellerShippingProfile>')
    parts.append('    </SellerProfiles>')
    parts.append('  </Item>')
    parts.append(f'  <Version>{API_VERSION}</Version>')
    parts.append('</ReviseFixedPriceItemRequest>')
    return '\n'.join(parts)


def revise_shipping_profile(
    item_id: str,
    seller_profiles: dict,
    app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> dict:
    """W138: listing の shipping Business Policy のみを変更 (BP-only Revise).

    商品管理タブの BP selectbox 変更から呼ばれる専用経路。W136 の
    `revise_fixed_price_with_shipping` の早期 return gate (price/ship 両 None
    で API 呼ばず) を**持たない** (BP のみで実行する、HIGH-1 訂正)。

    Args:
        seller_profiles: {'payment_id','return_id','shipping_id'}.
            **3 ID 全て必須** (Codex HIGH 2026-05-17 + 設計 HIGH-2): eBay
            ReviseFixedPriceItem の SellerProfiles は Payment/Shipping/Return
            各 1 を揃えて指定する仕様の蓋然性が高く、不完全だと Ack=Fail or
            意図せぬ payment/return policy 適用 (money/account risk)。pre-
            snapshot で 1 つでも欠ければ API を呼ばず success:False
            (Q0: 不完全 SellerProfiles を送らない。両 eBay 解釈下で安全側)。

    Returns: {'success': bool, 'ack': str, 'message': str | None, 'raw': ...}
    """
    sp = seller_profiles or {}
    _missing = [k for k in ("payment_id", "return_id", "shipping_id")
                if not sp.get(k)]
    if _missing:
        return {
            "success": False,
            "message": (
                f"SellerProfiles 不完全 ({', '.join(_missing)} 欠落) のため "
                "BP 変更を抑止 (3 ID 全必須)。GetItem に payment/return/"
                "shipping profile が揃わない listing は BP 変更不可"
            ),
            "raw": None,
        }
    xml = _build_revise_bp_only_xml(item_id, seller_profiles)
    result = _call_trading_api(
        "ReviseFixedPriceItem", xml,
        app_id, dev_id, cert_id, user_token,
    )
    # Ack=Warning でも Errors 内 SeverityCode=Error は失敗扱いに降格
    # (revise_fixed_price_with_shipping / revise_fixed_price_item と挙動統一、
    #  失敗診断を message に出す。code-reviewer MEDIUM 2026-05-17)。
    if result.get("success") and result.get("ack") == "Warning":
        raw_xml = result.get("raw") or ""
        if raw_xml:
            try:
                root = ET.fromstring(raw_xml)
                ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
                fatal_msgs = []
                for err in root.findall(".//ns:Errors", namespaces=ns):
                    sev = err.findtext("ns:SeverityCode", namespaces=ns)
                    if sev == "Error":
                        long_msg = err.findtext(
                            "ns:LongMessage", namespaces=ns) or ""
                        code = err.findtext(
                            "ns:ErrorCode", namespaces=ns) or "?"
                        fatal_msgs.append(f"[{code}] {long_msg}")
                if fatal_msgs:
                    return {
                        **result,
                        "success": False,
                        "message": (
                            "API Warning に重大エラー混入 "
                            "(SeverityCode=Error): " + "; ".join(fatal_msgs)
                        ),
                    }
            except ET.ParseError:
                pass
    return result


def revise_fixed_price_item(
    item_id: str, new_price_usd: float,
    app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> dict:
    """ReviseFixedPriceItem で既存出品の価格を変更.

    W183 値下げ pipeline の最終 step. 呼出前に min_price floor / 当日値下げ回数
    チェックは trigger logic 側で実施する責務分離.

    H6 fix: ack='Warning' でも Errors 内に SeverityCode='Error' があれば実質的に
    reject されているケースがあるため、success=False に降格する.

    Returns:
        {
            'success': bool,
            'ack': 'Success'/'Warning'/None,
            'message': str (失敗時のみ),
            'fees': list[str],   # 成功時の fee 構造
            'warnings': list[str],
            'raw': str,
        }
    """
    if new_price_usd is None or new_price_usd <= 0:
        return {
            "success": False,
            "message": f"invalid new_price_usd: {new_price_usd}",
            "raw": None,
        }
    result = _call_trading_api(
        "ReviseFixedPriceItem",
        _build_revise_fixed_price_xml(item_id, new_price_usd),
        app_id, dev_id, cert_id, user_token,
    )
    # H6: Warning ack でも SeverityCode=Error が混じれば失敗扱い.
    if result.get("success") and result.get("ack") == "Warning":
        raw_xml = result.get("raw") or ""
        if raw_xml:
            try:
                root = ET.fromstring(raw_xml)
                ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
                fatal_msgs = []
                for err in root.findall(".//ns:Errors", namespaces=ns):
                    sev = err.findtext("ns:SeverityCode", namespaces=ns)
                    if sev == "Error":
                        long_msg = err.findtext("ns:LongMessage", namespaces=ns) or ""
                        code = err.findtext("ns:ErrorCode", namespaces=ns) or "?"
                        fatal_msgs.append(f"[{code}] {long_msg}")
                if fatal_msgs:
                    return {
                        **result,
                        "success": False,
                        "message": (
                            "API Warning に重大エラー混入 (SeverityCode=Error): "
                            + "; ".join(fatal_msgs)
                        ),
                    }
            except ET.ParseError:
                pass  # parse 不能は元の result をそのまま返す
    return result


def _build_get_orders_xml(
    num_days: int = 7,
    page_number: int = 1,
    create_time_from: "datetime | None" = None,
    create_time_to: "datetime | None" = None,
) -> str:
    """GetOrders XML.
    Args:
        num_days: 過去 N 日 (max 90, eBay API 制限). create_time_from/to 指定時は無視.
        page_number: pagination
        create_time_from / create_time_to: W149 (2026-05-22) 範囲指定 backfill 用.
            両方指定時 NumberOfDays でなく CreateTimeFrom/To を使う (90 日以内必須).
    """
    if create_time_from is not None and create_time_to is not None:
        # eBay GetOrders は ISO 8601 UTC, ms 含む形式 ('.000Z')
        time_filter = (
            f"  <CreateTimeFrom>{create_time_from.strftime('%Y-%m-%dT%H:%M:%S')}.000Z</CreateTimeFrom>\n"
            f"  <CreateTimeTo>{create_time_to.strftime('%Y-%m-%dT%H:%M:%S')}.000Z</CreateTimeTo>"
        )
    else:
        time_filter = f"  <NumberOfDays>{num_days}</NumberOfDays>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
{time_filter}
  <OrderRole>Seller</OrderRole>
  <OrderStatus>All</OrderStatus>
  <Pagination>
    <EntriesPerPage>100</EntriesPerPage>
    <PageNumber>{page_number}</PageNumber>
  </Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
  <Version>{API_VERSION}</Version>
</GetOrdersRequest>"""


def get_orders(
    app_id: str, dev_id: str, cert_id: str, user_token: str,
    *,
    num_days: int = 7,
    page_number: int = 1,
    timeout: int = 60,
    create_time_from: "datetime | None" = None,
    create_time_to: "datetime | None" = None,
) -> dict:
    """Trading API GetOrders.

    Returns:
        {
          success: bool,
          orders: [
            {order_id, paid_time, status, buyer_country, item_price_usd,
             shipping_usd, total_usd, sku, ebay_item_id, title, ...}
          ],
          total_count: int,
          page_count: int,
          has_more: bool,
          raw: str,
        }
    """
    user_token = _resolve_active_token(user_token)
    body = _build_get_orders_xml(
        num_days=num_days,
        page_number=page_number,
        create_time_from=create_time_from,
        create_time_to=create_time_to,
    ).replace("{USER_TOKEN}", user_token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetOrders",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(
            TRADING_API_URL, content=body.encode("utf-8"),
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        return {"success": False, "message": f"通信エラー: {e}", "orders": [], "raw": None}

    root = ET.fromstring(resp.text)
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        return {"success": False, "message": f"GetOrders エラー: {msg}",
                "orders": [], "raw": resp.text}

    orders = []
    for order_elem in root.findall(".//ns:OrderArray/ns:Order", namespaces=ns):
        order_id = order_elem.findtext("ns:OrderID", namespaces=ns) or ""
        order_status = order_elem.findtext("ns:OrderStatus", namespaces=ns) or ""
        paid_time = order_elem.findtext("ns:PaidTime", namespaces=ns) or ""
        shipped_time = order_elem.findtext("ns:ShippedTime", namespaces=ns) or ""
        total_text = order_elem.findtext("ns:Total", namespaces=ns) or "0"
        subtotal_text = order_elem.findtext("ns:Subtotal", namespaces=ns) or "0"

        ship_addr = order_elem.find("ns:ShippingAddress", namespaces=ns)
        buyer_country = ship_addr.findtext("ns:Country", namespaces=ns) if ship_addr is not None else ""
        buyer_country_name = (ship_addr.findtext("ns:CountryName", namespaces=ns)
                              if ship_addr is not None else "")

        ship_service = order_elem.find("ns:ShippingServiceSelected", namespaces=ns)
        shipping_text = "0"
        if ship_service is not None:
            shipping_text = ship_service.findtext("ns:ShippingServiceCost", namespaces=ns) or "0"

        # Transactions (1 注文に複数 line item の可能性)
        for txn_elem in order_elem.findall(".//ns:Transaction", namespaces=ns):
            item_elem = txn_elem.find("ns:Item", namespaces=ns)
            if item_elem is None:
                continue
            ebay_item_id = item_elem.findtext("ns:ItemID", namespaces=ns) or ""
            title = item_elem.findtext("ns:Title", namespaces=ns) or ""
            sku = item_elem.findtext("ns:SKU", namespaces=ns) or ""
            qty = _safe_int(txn_elem.findtext("ns:QuantityPurchased", namespaces=ns), 1)
            price_text = txn_elem.findtext("ns:TransactionPrice", namespaces=ns) or "0"

            orders.append({
                "order_id": order_id,
                "ebay_item_id": ebay_item_id,
                "sku": sku,
                "title": title,
                "qty": qty,
                "item_price_usd": _safe_float(price_text),
                "shipping_usd": _safe_float(shipping_text),
                "subtotal_usd": _safe_float(subtotal_text),
                "total_usd": _safe_float(total_text),
                "buyer_country": buyer_country,
                "buyer_country_name": buyer_country_name,
                "order_status": order_status,
                "paid_time": paid_time,
                "shipped_time": shipped_time,
            })

    total_count = _safe_int(
        root.findtext(".//ns:PaginationResult/ns:TotalNumberOfEntries", namespaces=ns)
    )
    page_count = _safe_int(
        root.findtext(".//ns:PaginationResult/ns:TotalNumberOfPages", namespaces=ns)
    )

    return {
        "success": True,
        "ack": ack,
        "orders": orders,
        "total_count": total_count,
        "page_count": page_count,
        "current_page": page_number,
        "has_more": page_number < page_count,
        "raw": resp.text,
    }


def relist_item(
    item_id: str, app_id: str, dev_id: str, cert_id: str, user_token: str,
) -> dict:
    """RelistFixedPriceItem で listing を再出品（Sell Similar 相当）。

    元の listing の ShippingDetails（ShippingServiceCost 含む）、title、images、
    SellerProfiles は全て自動継承される。新 ItemID を返す。

    Returns: {success, new_item_id, start_time, end_time, fees, warnings, message}
    """
    return _call_trading_api(
        "RelistFixedPriceItem", _build_relist_fixed_price_xml(item_id),
        app_id, dev_id, cert_id, user_token,
    )


# =================================================================
# W219 (2026-06-03): eBay Finances API
# 実手数料 (FVF / INTERNATIONAL_FEE / AD_FEE / REGULATORY_OPERATING_FEE 等)
# を取得し計算式の推定手数料と突合する分析専用 wrapper.
# OAuth scope: sell.finances (settings 既保有, ebay_oauth_refresh.py L52).
# read-only GET のみ. calculator/settings/task_order_alert は変更しない.
# =================================================================

# REST 用 base URL (Production). sandbox は別途 apiz.sandbox.ebay.com.
FINANCES_API_BASE = "https://apiz.ebay.com/sell/finances/v1"


def get_transactions(
    start_date: str,
    end_date: str,
    *,
    limit: int = 200,
    offset: int = 0,
    transaction_type: str | None = None,
    timeout: int = 60,
    access_token: str | None = None,
    page_cap: int = 50,
) -> dict:
    """eBay Finances API `/transaction` をページングしながら全件取得.

    用途: W219 段1-2 分析 (実手数料 vs calculator 推定の突合). read-only.
    `apiz.ebay.com/sell/finances/v1/transaction?filter=transactionDate:[<from>..<to>]`

    Args:
        start_date: ISO 8601 UTC. 例 "2025-12-05T00:00:00.000Z".
        end_date:   ISO 8601 UTC. 例 "2026-06-03T00:00:00.000Z".
        limit: 1 ページ件数 (eBay 仕様で 1..1000, 既定 200).
        offset: 初回 offset (通常 0). page_cap 到達時に途中状態を返す用に exposed.
        transaction_type: 'SALE' / 'REFUND' / 'NON_SALE_CHARGE' / 'SHIPPING_LABEL'.
            None なら全種別取得 (filter 未指定).
        timeout: HTTP timeout sec.
        access_token: 上書き用. None なら `get_valid_access_token()` で取得 (auto-refresh).
        page_cap: 暴走防止. 最大ページ数 (limit=200 なら 200*50=10000 件まで).

    Returns:
        {
            'success': bool,
            'transactions': list[dict],     # eBay 生 JSON object をそのまま蓄積
            'total': int | None,            # eBay 報告 total (推定値)
            'fetched': int,                 # 実取得件数
            'pages': int,                   # 実ページ数
            'truncated': bool,              # page_cap 到達で打ち切ったか
            'errors': list[str],            # 失敗時の詳細 (Q0: 偽装成功させない)
            'last_status': int | None,      # 最終 HTTP status code
        }

    Q0 方針:
        - 401/403 (token 失効・scope 未 consent) は errors に詳細記録し success=False.
          空配列を success=True で返さない.
        - rate limit (429) も同様に errors. 部分取得でも transactions に積んだ分は保持.
        - JSON parse 失敗・network エラーも success=False で raw 残す.

    eBay 公式 doc:
        https://developer.ebay.com/api-docs/sell/finances/resources/transaction/methods/getTransactions
        - filter syntax: `transactionDate:[2025-12-05T00:00:00.000Z..2026-06-03T00:00:00.000Z]`
        - transactionType filter は別 key. 複合は `filter=...&filter=...` で OR 動作.
        - 1 ページ最大 200 件 (内部上限 1000 だが安定実績 200).
    """
    errors: list[str] = []
    transactions: list[dict] = []
    last_status: Optional[int] = None
    reported_total: Optional[int] = None
    pages = 0
    truncated = False

    # OAuth token (auto-refresh 経由) を取得
    if access_token is None:
        try:
            from monitor.ebay_oauth_refresh import get_valid_access_token
            access_token = get_valid_access_token()
        except Exception as e:  # noqa: BLE001
            errors.append(f"oauth token 取得失敗: {e}")
            return {
                "success": False, "transactions": [], "total": None,
                "fetched": 0, "pages": 0, "truncated": False,
                "errors": errors, "last_status": None,
            }
    if not access_token:
        errors.append(
            "access_token が None (auto-refresh 失敗 or EBAY_USER_TOKEN 未設定)"
        )
        return {
            "success": False, "transactions": [], "total": None,
            "fetched": 0, "pages": 0, "truncated": False,
            "errors": errors, "last_status": None,
        }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # eBay の filter は **URL encoded** で渡す必要がある (`[`, `]`, `..`, `:` 等).
    # httpx は params= に dict を渡すと自動 encode するため利用.
    base_filter = f"transactionDate:[{start_date}..{end_date}]"
    cur_offset = int(offset)

    try:
        client = httpx.Client(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        errors.append(f"httpx.Client 初期化失敗: {e}")
        return {
            "success": False, "transactions": [], "total": None,
            "fetched": 0, "pages": 0, "truncated": False,
            "errors": errors, "last_status": None,
        }

    try:
        while pages < page_cap:
            params: dict = {
                "filter": base_filter,
                "limit": str(limit),
                "offset": str(cur_offset),
            }
            if transaction_type:
                # eBay は同名 key の繰り返しで複数 filter を受ける.
                # httpx は str を 1 個渡すなら下記、複数なら list. 今回は OR 同 key 想定.
                params["filter"] = (
                    f"{base_filter}&filter=transactionType:{{{transaction_type}}}"
                )
                # ↑ params 経由だと `&` も encode されるため URL を組み立て直す.
                # 簡潔さ優先で URL を明示組立に切替.
            try:
                if transaction_type:
                    # 2 個の filter を要求するため manual URL 組み立て
                    from urllib.parse import quote
                    url = (
                        f"{FINANCES_API_BASE}/transaction"
                        f"?filter={quote(base_filter)}"
                        f"&filter={quote(f'transactionType:{{{transaction_type}}}')}"
                        f"&limit={limit}&offset={cur_offset}"
                    )
                    resp = client.get(url, headers=headers)
                else:
                    resp = client.get(
                        f"{FINANCES_API_BASE}/transaction",
                        headers=headers,
                        params={
                            "filter": base_filter,
                            "limit": str(limit),
                            "offset": str(cur_offset),
                        },
                    )
            except httpx.HTTPError as e:
                errors.append(f"page offset={cur_offset} HTTP error: {e}")
                break
            last_status = resp.status_code
            if resp.status_code in (401, 403):
                # Q0: scope 未 consent / token 失効. 空成功にしない.
                try:
                    body_preview = resp.text[:500]
                except Exception:  # noqa: BLE001
                    body_preview = "<unreadable>"
                errors.append(
                    f"Finances API auth 失敗 status={resp.status_code} "
                    f"(scope=sell.finances consent 必要 / token 失効 の疑い): "
                    f"{body_preview}"
                )
                break
            if resp.status_code == 429:
                # rate limit. 部分取得を返す.
                try:
                    body_preview = resp.text[:300]
                except Exception:  # noqa: BLE001
                    body_preview = "<unreadable>"
                errors.append(
                    f"Finances API rate limit (429) offset={cur_offset}: "
                    f"{body_preview}"
                )
                break
            if resp.status_code != 200:
                try:
                    body_preview = resp.text[:500]
                except Exception:  # noqa: BLE001
                    body_preview = "<unreadable>"
                errors.append(
                    f"Finances API 異常 status={resp.status_code} "
                    f"offset={cur_offset}: {body_preview}"
                )
                break

            try:
                data = resp.json()
            except (ValueError, TypeError) as e:
                errors.append(
                    f"page offset={cur_offset} JSON parse 失敗: {e}; "
                    f"body[:300]={resp.text[:300]!r}"
                )
                break

            page_items = data.get("transactions") or []
            transactions.extend(page_items)
            pages += 1
            if reported_total is None:
                rt = data.get("total")
                if isinstance(rt, int):
                    reported_total = rt

            # ページ尽きたか判定
            if not page_items or len(page_items) < limit:
                break
            cur_offset += limit
        else:
            # while else: page_cap に達して break せず終了
            truncated = True
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    success = (not errors) and (last_status == 200 or last_status is None and pages == 0)
    # last_status が None のまま (1 度も叩けず) = errors にもう積んでいるので success=False になる.
    if last_status is None:
        success = False

    return {
        "success": success,
        "transactions": transactions,
        "total": reported_total,
        "fetched": len(transactions),
        "pages": pages,
        "truncated": truncated,
        "errors": errors,
        "last_status": last_status,
    }


def parse_sale_fees(txn: dict) -> dict:
    """1 つの SALE transaction から手数料明細を抽出 (parse helper).

    実観測スキーマ (2026-06-03, 本番 styt.msg1212 アカウント):
        {
          "transactionId": "06-14724-56167",
          "orderId":       "06-14724-56167",
          "transactionType": "SALE",
          "transactionDate": "2026-06-02T13:20:06.839Z",
          "amount":              {"value": "162.36", "currency": "USD"},
          "totalFeeBasisAmount": {"value": "195.0",  "currency": "USD"},
          "totalFeeAmount":      {"value": "32.64",  "currency": "USD"},
          "ebayCollectedTaxAmount": {"value": "19.5", "currency": "USD"},
          "orderLineItems": [
            {
              "lineItemId": "10082224798306",
              "feeBasisAmount": {"value": "214.5", "currency": "USD"},
              "marketplaceFees": [
                {"feeType": "FINAL_VALUE_FEE",
                 "amount": {"value": "29.96"}},
                {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER",
                 "amount": {"value": "0.44"}},
                {"feeType": "INTERNATIONAL_FEE",
                 "amount": {"value": "2.24"},
                 "feeMemo": "Charged because the delivery address is in ..."},
              ],
            }
          ]
        }

    ⚠️ **SALE には `itemId` フィールド無し** (lineItemId のみ). 本当の eBay
    legacy ItemID (12 桁) は SALE 単体からは取得不能で、`orderId` を
    `sales_history.ebay_order_id` に join するしかない. 旧 implementation の
    `_extract_legacy_item_id(li.get("itemId"))` は常に空文字列を返していた
    (実観測で確定、scripts/inspect_finances_schema_2026_06_03.py).

    ⚠️ **Promoted Listings fee (AD_FEE / PREMIUM_AD_FEES) は SALE に出ない**.
    NON_SALE_CHARGE transactionType で別 entry として課金される
    (`transactionMemo="Promoted Listings - Priority fee"`,
     `references[].referenceType="ITEM_ID"` で listing にひも付け).
    SALE のみ集約すると AD=$0 になる (本当の AD 料金は `parse_non_sale_charge`
    で取得).

    Returns:
        {
            'order_id': str,
            'transaction_id': str,
            'transaction_date': str,
            'amount_usd': float,            # transaction (買い手支払合計) 売上
            'total_fee_usd': float,         # eBay 公表合計手数料
            'line_items': [
                {
                    'item_id': str (eBay legacy 12 桁を抽出),
                    'line_item_id': str,
                    'fees_by_type': {feeType: float_usd},  # 合計値
                    'fee_total_usd': float,
                }, ...
            ],
            'fees_by_type': {feeType: float_usd},   # transaction 全体集約
            'fee_total_from_lines_usd': float,
        }

    Q0 (silent skip 防止): 想定 key 不在時も空 dict ではなく 0 で構造維持.
        ただし orderLineItems が完全に無い (非 SALE 等) は line_items=[] で返す.
    """
    def _money(d) -> float:
        if not isinstance(d, dict):
            return 0.0
        v = d.get("value")
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _extract_legacy_item_id(item_id_raw: str) -> str:
        """eBay legacy/RESTful itemId 形式 'v1|123456789012|0' から 12 桁を抽出.
        既に純数字なら返す. 不明形式は元文字列."""
        if not item_id_raw:
            return ""
        s = str(item_id_raw)
        if "|" in s:
            parts = s.split("|")
            for p in parts:
                if p.isdigit() and len(p) >= 9:
                    return p
        return s

    order_id = str(txn.get("orderId") or "")
    txn_id = str(txn.get("transactionId") or "")
    txn_date = str(txn.get("transactionDate") or "")
    amount_usd = _money(txn.get("amount"))
    total_fee_usd = _money(txn.get("totalFeeAmount"))

    line_results: list[dict] = []
    agg_by_type: dict[str, float] = {}
    fee_total_from_lines = 0.0

    for li in (txn.get("orderLineItems") or []):
        if not isinstance(li, dict):
            continue
        li_id = str(li.get("lineItemId") or "")
        # 実観測: SALE.orderLineItems[].itemId は存在しない. 念のため互換コードは
        # 残す (将来 schema 変更で itemId が増えた場合に拾える). 通常は "".
        item_id = _extract_legacy_item_id(li.get("itemId") or "")
        fees_by_type: dict[str, float] = {}
        li_fee_total = 0.0
        for fee in (li.get("marketplaceFees") or []):
            if not isinstance(fee, dict):
                continue
            ft = str(fee.get("feeType") or "UNKNOWN")
            fv = _money(fee.get("amount"))
            fees_by_type[ft] = fees_by_type.get(ft, 0.0) + fv
            agg_by_type[ft] = agg_by_type.get(ft, 0.0) + fv
            li_fee_total += fv
        fee_total_from_lines += li_fee_total
        line_results.append({
            "item_id": item_id,
            "line_item_id": li_id,
            "fees_by_type": fees_by_type,
            "fee_total_usd": li_fee_total,
        })

    return {
        "order_id": order_id,
        "transaction_id": txn_id,
        "transaction_date": txn_date,
        "amount_usd": amount_usd,
        "total_fee_usd": total_fee_usd,
        "line_items": line_results,
        "fees_by_type": agg_by_type,
        "fee_total_from_lines_usd": fee_total_from_lines,
    }


def parse_non_sale_charge(txn: dict) -> dict:
    """NON_SALE_CHARGE transaction を parse する (Promoted Listings fee 等).

    実観測 (2026-06-03):
        {
          "transactionId": "FEE-7561359420110_11",
          "transactionType": "NON_SALE_CHARGE",
          "amount": {"value": "2.0", "currency": "USD"},
          "bookingEntry": "DEBIT",
          "transactionDate": "2026-06-02T...",
          "transactionMemo": "Promoted Listings - Priority fee",
          "feeType": "PREMIUM_AD_FEES",
          "references": [{"referenceId": "358212419810",
                          "referenceType": "ITEM_ID"}]
        }

    SALE には現れない手数料 (Promoted, 月額 Store subscription, 各種 surcharge)
    を拾うため、SALE と別 parser を持つ. amount は DEBIT/CREDIT を区別し
    DEBIT を seller 負担 (= cost) として正値で返す.

    Returns:
        {
            'transaction_id': str,
            'transaction_date': str,
            'fee_type': str,            # 例 PREMIUM_AD_FEES
            'memo': str,                # 例 'Promoted Listings - Priority fee'
            'amount_usd_debit': float,  # DEBIT 時の seller 負担額 (正値)
            'amount_usd_credit': float, # CREDIT 時の seller 戻し額 (正値)
            'item_id': str,             # references[ITEM_ID] (eBay legacy 12桁)
            'order_id_ref': str,        # references[ORDER_ID] (あれば)
        }
    """
    def _money(d) -> float:
        if not isinstance(d, dict):
            return 0.0
        v = d.get("value")
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    booking = str(txn.get("bookingEntry") or "").upper()
    amt = _money(txn.get("amount"))
    debit = amt if booking == "DEBIT" else 0.0
    credit = amt if booking == "CREDIT" else 0.0

    item_id = ""
    order_id_ref = ""
    for ref in (txn.get("references") or []):
        if not isinstance(ref, dict):
            continue
        rt = str(ref.get("referenceType") or "").upper()
        rid = str(ref.get("referenceId") or "")
        if rt == "ITEM_ID" and not item_id:
            item_id = rid
        elif rt == "ORDER_ID" and not order_id_ref:
            order_id_ref = rid

    return {
        "transaction_id": str(txn.get("transactionId") or ""),
        "transaction_date": str(txn.get("transactionDate") or ""),
        "fee_type": str(txn.get("feeType") or ""),
        "memo": str(txn.get("transactionMemo") or ""),
        "amount_usd_debit": debit,
        "amount_usd_credit": credit,
        "item_id": item_id,
        "order_id_ref": order_id_ref,
    }
