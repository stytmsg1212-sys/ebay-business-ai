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

                results[item_id] = {
                    "watch_count": watch_count,
                    "view_count": hit_count,
                    "sales_count_30d": quantity_sold,
                }

                if (idx + 1) % 50 == 0:
                    logger.info(f"GetItem progress: {idx + 1}/{len(item_ids)}")

                success_count += 1

        except Exception as e:
            logger.debug(f"GetItem {item_id} exception: {e}")
            error_count += 1

    logger.info(f"GetItem completed: {success_count} success, {error_count} errors")
    return results


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


def _build_get_orders_xml(num_days: int = 7, page_number: int = 1) -> str:
    """GetOrders XML.
    Args:
        num_days: 過去 N 日 (max 90, eBay API 制限)
        page_number: pagination
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken>
  </RequesterCredentials>
  <NumberOfDays>{num_days}</NumberOfDays>
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
    body = _build_get_orders_xml(num_days=num_days, page_number=page_number).replace(
        "{USER_TOKEN}", user_token
    )
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
