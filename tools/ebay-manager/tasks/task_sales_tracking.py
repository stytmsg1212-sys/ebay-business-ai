#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 売上トラッキング
Gmail の eBay 売上通知メールから販売データを自動抽出し、
monitor.db の sales_history テーブルに記録する。

eBay の売上通知メールのパターン:
  - Subject: "You made a sale!" / "Item sold"
  - From: ebay@ebay.com
  - 本文に: Item title, Item number, Sale price, Buyer location
"""

import sys
import re
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / 'monitor'))


def parse_ebay_sold_email(subject: str, body: str, date: str) -> Optional[Dict]:
    """
    eBay 売上通知メールから販売情報を抽出

    Returns:
        {
            'ebay_item_id': str,
            'title': str,
            'sold_price_usd': float,
            'buyer_country': str,
            'sold_at': str,
        }
    """
    # "You made a sale" 系のメールか確認
    if not any(kw in subject.lower() for kw in ['sold', 'sale', 'payment received']):
        return None

    result = {
        'ebay_item_id': '',
        'title': '',
        'sold_price_usd': 0.0,
        'buyer_country': '',
        'sold_at': date,
    }

    # Item ID の抽出パターン
    item_id_match = re.search(r'(?:Item|item)\s*(?:number|#|ID)?[:\s]*(\d{10,15})', body)
    if item_id_match:
        result['ebay_item_id'] = item_id_match.group(1)

    # Item ID が URL 内にある場合
    if not result['ebay_item_id']:
        url_match = re.search(r'ebay\.com/itm/(\d{10,15})', body)
        if url_match:
            result['ebay_item_id'] = url_match.group(1)

    # 価格の抽出 (USD)
    price_patterns = [
        r'(?:Sale price|Sold for|Total|Price)[:\s]*\$?([\d,]+\.?\d*)',
        r'\$([\d,]+\.?\d*)\s*(?:USD)?',
    ]
    for pattern in price_patterns:
        price_match = re.search(pattern, body, re.IGNORECASE)
        if price_match:
            price_str = price_match.group(1).replace(',', '')
            try:
                result['sold_price_usd'] = float(price_str)
                break
            except ValueError:
                pass

    # タイトル抽出
    title_match = re.search(r'(?:Item|Product)[:\s]*(.*?)(?:\n|$)', body)
    if title_match:
        result['title'] = title_match.group(1).strip()[:100]

    # Buyer 国の抽出
    country_match = re.search(r'(?:Ship to|Buyer location|Location)[:\s]*(.*?)(?:\n|$)', body, re.IGNORECASE)
    if country_match:
        result['buyer_country'] = country_match.group(1).strip()[:50]

    # 最低限 item_id か price があれば有効
    if result['ebay_item_id'] or result['sold_price_usd'] > 0:
        return result

    return None


def extract_sales_from_emails(config) -> List[Dict]:
    """
    Gmail から売上メールを取得して解析

    Returns:
        [{ebay_item_id, title, sold_price_usd, buyer_country, sold_at}]
    """
    try:
        from tasks.task_email_pickup import get_gmail_service
    except ImportError:
        logger.warning("Gmail サービスのインポートに失敗")
        return []

    try:
        service = get_gmail_service(config)
        if not service:
            logger.warning("Gmail サービスの初期化に失敗")
            return []

        # 直近2日間の売上メールを検索
        query = 'from:ebay.com subject:(sold OR "made a sale" OR "payment received") newer_than:2d'

        results = service.users().messages().list(
            userId='me', q=query, maxResults=20
        ).execute()

        messages = results.get('messages', [])
        if not messages:
            logger.info("売上メールが見つかりません")
            return []

        sales = []
        for msg_info in messages:
            try:
                msg = service.users().messages().get(
                    userId='me', id=msg_info['id'], format='full'
                ).execute()

                # ヘッダーから Subject と Date を取得
                headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                subject = headers.get('Subject', '')
                date = headers.get('Date', '')

                # 本文を取得
                body = _extract_email_body(msg.get('payload', {}))

                # 解析
                sale = parse_ebay_sold_email(subject, body, date)
                if sale:
                    sales.append(sale)

            except Exception as e:
                logger.warning(f"メール解析エラー: {e}")
                continue

        logger.info(f"売上メール: {len(sales)}件検出")
        return sales

    except Exception as e:
        logger.error(f"Gmail 売上取得エラー: {e}")
        return []


def _extract_email_body(payload: Dict) -> str:
    """メールの本文をプレーンテキストで取得"""
    import base64

    body = ""

    if payload.get('body', {}).get('data'):
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
    elif payload.get('parts'):
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
                break
            elif part.get('parts'):
                body = _extract_email_body(part)
                if body:
                    break

    return body


def save_sales_to_db(sales: List[Dict]) -> Dict:
    """
    売上データをDBに保存

    Returns:
        {'saved': int, 'skipped': int, 'errors': int}
    """
    try:
        from monitor.database import add_sale, init_db
        init_db()
    except ImportError:
        logger.error("database モジュールのインポートに失敗")
        return {'saved': 0, 'skipped': 0, 'errors': 0}

    saved = 0
    skipped = 0
    errors = 0

    for sale in sales:
        ebay_item_id = sale.get('ebay_item_id', '')
        if not ebay_item_id:
            skipped += 1
            continue

        try:
            # SKU を ebay_listings から取得
            from monitor.database import get_conn
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT sku, title FROM ebay_listings WHERE ebay_item_id=?",
                    (ebay_item_id,)
                ).fetchone()

            sku = row['sku'] if row else ''
            title = sale.get('title', '') or (row['title'] if row else '')

            # 重複チェック（同日同アイテムの売上は1つだけ）
            with get_conn() as conn:
                existing = conn.execute(
                    """SELECT id FROM sales_history
                       WHERE ebay_item_id=? AND date(sold_at)=date(?)""",
                    (ebay_item_id, sale.get('sold_at', '')),
                ).fetchone()

            if existing:
                skipped += 1
                continue

            add_sale(
                ebay_item_id=ebay_item_id,
                sku=sku,
                title=title,
                sold_price_usd=sale.get('sold_price_usd', 0),
                sold_at=sale.get('sold_at', ''),
                buyer_country=sale.get('buyer_country', ''),
            )
            saved += 1
            logger.info(f"売上記録: {ebay_item_id} ${sale.get('sold_price_usd', 0):.2f}")

        except Exception as e:
            logger.warning(f"売上保存エラー ({ebay_item_id}): {e}")
            errors += 1

    return {'saved': saved, 'skipped': skipped, 'errors': errors}


def generate_sales_report() -> Dict:
    """売上レポートを生成"""
    try:
        from monitor.database import get_sales_summary, get_sales_by_date, get_top_selling_items, init_db
        init_db()
    except ImportError:
        return {}

    return {
        'summary_30d': get_sales_summary(30),
        'summary_7d': get_sales_summary(7),
        'daily_trend': get_sales_by_date(30),
        'top_items': get_top_selling_items(10),
    }


def save_report_to_finance(report: Dict):
    """売上レポートを finance 部署に保存"""
    from company_router import get_company_root, _append_to_file, _today, _now_time

    company_root = get_company_root()
    if not company_root:
        return

    finance_file = company_root / "finance" / "expenses" / f"{_today()}-sales-report.md"

    summary_30d = report.get('summary_30d', {})
    summary_7d = report.get('summary_7d', {})

    content = f"# 売上レポート - {_today()} {_now_time()}\n\n"
    content += "## 売上サマリー\n\n"
    content += "| 期間 | 販売数 | 売上(USD) | 平均単価 | 利益(JPY) |\n"
    content += "|------|--------|-----------|----------|----------|\n"
    content += f"| 7日間 | {summary_7d.get('count', 0)} | ${summary_7d.get('revenue_usd', 0):,.2f} | ${summary_7d.get('avg_price', 0):,.2f} | ¥{summary_7d.get('total_profit_jpy', 0):,.0f} |\n"
    content += f"| 30日間 | {summary_30d.get('count', 0)} | ${summary_30d.get('revenue_usd', 0):,.2f} | ${summary_30d.get('avg_price', 0):,.2f} | ¥{summary_30d.get('total_profit_jpy', 0):,.0f} |\n"
    content += "\n"

    # TOP商品
    top_items = report.get('top_items', [])
    if top_items:
        content += "## 売上TOP商品\n\n"
        content += "| 商品名 | 販売数 | 売上合計 | 平均単価 |\n"
        content += "|--------|--------|---------|----------|\n"
        for item in top_items[:10]:
            title = (item.get('title') or '')[:40]
            content += f"| {title} | {item.get('sold_count', 0)} | ${item.get('total_revenue', 0):,.2f} | ${item.get('avg_price', 0):,.2f} |\n"
        content += "\n"

    _append_to_file(finance_file, content)
    logger.info(f"[Router] 売上レポート → finance/")


def run_sales_tracking(config) -> Dict:
    """
    売上トラッキングタスク

    1. Gmail から売上メールを取得
    2. 販売データを解析
    3. monitor.db の sales_history に記録
    4. 売上レポートを生成
    5. finance 部署に保存

    Returns:
        {'success': bool, 'sales_count': int, 'report': dict, 'message': str}
    """
    logger.info("【開始】売上トラッキングタスク")

    try:
        # Step 1-2: Gmail から売上データを抽出
        sales = extract_sales_from_emails(config)

        # Step 3: DBに保存
        save_result = save_sales_to_db(sales)
        logger.info(f"売上保存: 新規{save_result['saved']}, スキップ{save_result['skipped']}, エラー{save_result['errors']}")

        # Step 4: レポート生成
        report = generate_sales_report()

        # Step 5: finance に保存
        if report:
            save_report_to_finance(report)

        return {
            'success': True,
            'sales_count': save_result['saved'],
            'skipped': save_result['skipped'],
            'report': report,
            'message': f"売上トラッキング完了: {save_result['saved']}件記録"
        }

    except Exception as e:
        logger.error(f"売上トラッキングエラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'sales_count': 0,
            'report': {},
            'error': str(e)
        }
