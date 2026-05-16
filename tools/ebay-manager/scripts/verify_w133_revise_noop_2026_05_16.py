"""W133 item2 (2026-05-16): ReviseInventoryStatus 本番冪等 no-op 検証 (one-shot).

対象: ebay_item_id=357443534474 (Ohuhu x Sanrio 80 Color, 有在庫 active, user 選択)

手順 (冪等 no-op):
  1. 修正済 inventory_sync._get_credentials() で creds 値タプル取得
  2. GetItem で現在の eBay Quantity Q (= 出品総数量) と QuantitySold を読取
  3. revise_inventory_quantity(item_id, Q, *creds) = **現在値と同じ Q** を書く
  4. Ack=Success(/Warning) を確認 (本番書き込み API 経路の実証)
  5. GetItem 再取得で Quantity が Q のまま不変であることを確認

安全性:
  - 数量を現在値に書くだけ → buyer 表示・available 在庫は不変 = Defect リスクゼロ
  - GetItem 失敗時は write を実行せず中断 (Q0: 不明を成功と偽装しない)
  - 認証値は出力しない (security.md)
"""
import sys
import xml.etree.ElementTree as ET

import httpx

sys.path.insert(0, r'C:/Users/gucch/projects/claude/tools/ebay-manager')

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import (
    API_VERSION,
    TRADING_API_URL,
    _build_get_item_xml,
    _resolve_active_token,
    revise_inventory_quantity,
)

TARGET = '357443534474'
NS = {'ns': 'urn:ebay:apis:eBLBaseComponents'}


def _get_item_snapshot(creds) -> dict:
    """GetItem で Title/Quantity/QuantitySold/ListingStatus を読む (読み取り専用)."""
    app_id, dev_id, cert_id, user_token = creds
    token = _resolve_active_token(user_token)
    xml_body = _build_get_item_xml(TARGET).replace('{USER_TOKEN}', token)
    headers = {
        'X-EBAY-API-SITEID': '0',
        'X-EBAY-API-COMPATIBILITY-LEVEL': API_VERSION,
        'X-EBAY-API-CALL-NAME': 'GetItem',
        'X-EBAY-API-APP-NAME': app_id,
        'X-EBAY-API-DEV-NAME': dev_id,
        'X-EBAY-API-CERT-NAME': cert_id,
        'Content-Type': 'text/xml',
    }
    resp = httpx.post(
        TRADING_API_URL, content=xml_body.encode('utf-8'),
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ack = root.findtext('ns:Ack', namespaces=NS)
    if ack not in ('Success', 'Warning'):
        errs = root.findall('.//ns:Errors/ns:LongMessage', namespaces=NS)
        msg = '; '.join(e.text for e in errs if e.text) or 'Unknown'
        raise RuntimeError(f'GetItem Ack={ack}: {msg}')
    item = root.find('.//ns:Item', namespaces=NS)
    if item is None:
        raise RuntimeError('GetItem: Item ノードが無い')
    sel = item.find('ns:SellingStatus', namespaces=NS)
    title = item.findtext('ns:Title', namespaces=NS) or ''
    return {
        'title': title.encode('ascii', 'replace').decode(),
        'quantity': int(item.findtext('ns:Quantity', namespaces=NS) or -1),
        'qty_sold': int(
            sel.findtext('ns:QuantitySold', namespaces=NS) or 0
        ) if sel is not None else 0,
        'status': (
            sel.findtext('ns:ListingStatus', namespaces=NS)
            if sel is not None else None
        ),
    }


def main() -> int:
    creds = _get_credentials()
    if not creds:
        print('RESULT: FAIL (creds 解決不可 = _get_credentials None)')
        return 1
    if creds[0] == 'app_id':
        print('RESULT: FAIL (creds がキー文字列 = dict→tuple バグ再発)')
        return 1

    print(f'=== STEP 2: GetItem (before) item={TARGET} ===')
    try:
        before = _get_item_snapshot(creds)
    except Exception as e:  # noqa: BLE001  失敗時は write せず中断 (Q0)
        print(f'RESULT: FAIL (GetItem before 失敗、write 未実行で中断): {e}')
        return 1
    print(f'  title   : {before["title"][:55]}')
    print(f'  status  : {before["status"]}')
    print(f'  Quantity: {before["quantity"]}  QuantitySold: {before["qty_sold"]}')

    q = before['quantity']
    if q < 0:
        print('RESULT: FAIL (Quantity 読取不能 = -1)、write 未実行')
        return 1

    print(f'=== STEP 3: ReviseInventoryStatus (本番) Quantity {q} → {q} '
          f'(冪等 no-op) ===')
    app_id, dev_id, cert_id, user_token = creds
    api = revise_inventory_quantity(
        TARGET, q, app_id, dev_id, cert_id, user_token
    )
    print(f'  api success={api.get("success")}  message={api.get("message")}')
    if not api.get('success'):
        print('RESULT: FAIL (ReviseInventoryStatus が success=False)')
        return 1

    print('=== STEP 5: GetItem (after) で Quantity 不変確認 ===')
    try:
        after = _get_item_snapshot(creds)
    except Exception as e:  # noqa: BLE001
        print(f'RESULT: INCONCLUSIVE (revise は success だが after GetItem 失敗): {e}')
        return 3
    print(f'  Quantity: {after["quantity"]}  QuantitySold: {after["qty_sold"]}')

    unchanged = (
        after['quantity'] == before['quantity']
        and after['qty_sold'] == before['qty_sold']
    )
    if unchanged:
        print(f'RESULT: PASS (ReviseInventoryStatus 本番書込成功 + '
              f'Quantity {q} 不変 = 冪等 no-op 実証、buyer 影響ゼロ)')
        return 0
    print(f'RESULT: UNEXPECTED (Quantity が変化: '
          f'{before["quantity"]}→{after["quantity"]})、要調査')
    return 2


if __name__ == '__main__':
    sys.exit(main())
