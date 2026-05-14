#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 価格最適化
ランクデータ + 競合価格 + 売上実績に基づいて価格調整の提案を生成

ルール:
  - ランク D/E（低需要）→ 値下げ候補リスト
  - 競合の方が安い → アラート通知
  - ランク S/A（高需要）で競合少ない → 値上げ候補
  - 長期間売れていない → 段階的値下げ提案
"""

import sys
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / 'data' / 'monitor.db'


def get_listings_with_metrics() -> List[Dict]:
    """eBay出品のメトリクス付きリストを取得"""
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 2026-05-01 W76 T2: is_ended=1 listing は対 buyer に出ない = 価格最適化対象外.
    # 旧 query は ended 含めて分析していて compute 浪費 (E rank で 98 件 leak).
    cursor.execute("""
        SELECT ebay_item_id, sku, title, current_price, rank,
               watch_count, view_count, sales_count_30d,
               metrics_score, source_status,
               competitor_min_price, competitor_count,
               total_sold_count, last_sold_at,
               last_synced_at
        FROM ebay_listings
        WHERE current_price > 0
          AND COALESCE(is_ended, 0) = 0
        ORDER BY rank, metrics_score DESC
    """)

    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def analyze_price_opportunities(listings: List[Dict]) -> Dict:
    """
    価格最適化の機会を分析

    Returns:
        {
            'price_decrease_candidates': [...],  # 値下げ候補
            'price_increase_candidates': [...],  # 値上げ候補
            'competitor_undercut': [...],         # 競合に負けている
            'stale_listings': [...],              # 長期間売れていない
        }
    """
    price_decrease = []
    price_increase = []
    competitor_undercut = []
    stale_listings = []

    now = datetime.now()

    for item in listings:
        rank = item.get('rank', 'C')
        price = item.get('current_price', 0)
        watch = item.get('watch_count', 0)
        views = item.get('view_count', 0)
        sales = item.get('sales_count_30d', 0)
        comp_min = item.get('competitor_min_price')
        comp_count = item.get('competitor_count', 0)
        total_sold = item.get('total_sold_count', 0)
        last_sold = item.get('last_sold_at')

        suggestion = {
            'ebay_item_id': item['ebay_item_id'],
            'sku': item.get('sku', ''),
            'title': (item.get('title') or '')[:50],
            'current_price': price,
            'rank': rank,
            'watch_count': watch,
            'sales_30d': sales,
        }

        # ルール1: ランク D/E で需要低い → 値下げ候補
        if rank in ('D', 'E') and price > 20:
            discount = 0.10 if rank == 'D' else 0.15
            suggested = round(price * (1 - discount), 2)
            suggestion.update({
                'suggested_price': suggested,
                'change_pct': -discount * 100,
                'reason': f'ランク{rank}（低需要）。{discount*100:.0f}%値下げを推奨',
            })
            price_decrease.append(suggestion.copy())

        # ルール2: 競合の方が安い
        if comp_min and comp_min > 0 and price > comp_min * 1.05:
            diff_pct = (price - comp_min) / price * 100
            suggested = round(comp_min * 0.99, 2)  # 競合より1%安く
            suggestion.update({
                'suggested_price': suggested,
                'change_pct': -diff_pct,
                'competitor_price': comp_min,
                'reason': f'競合(${comp_min:.2f})より{diff_pct:.1f}%高い',
            })
            competitor_undercut.append(suggestion.copy())

        # ルール3: ランク S/A で競合少ない → 値上げ候補
        if rank in ('S', 'A') and comp_count <= 2 and watch > 5:
            increase = 0.05 if rank == 'A' else 0.10
            suggested = round(price * (1 + increase), 2)
            suggestion.update({
                'suggested_price': suggested,
                'change_pct': increase * 100,
                'reason': f'ランク{rank}（高需要）+ 競合{comp_count}件。{increase*100:.0f}%値上げ余地あり',
            })
            price_increase.append(suggestion.copy())

        # ルール4: views > 50 だが sales = 0 → 価格が高すぎる可能性
        if views > 50 and sales == 0 and watch < 3 and price > 30:
            suggested = round(price * 0.90, 2)
            suggestion.update({
                'suggested_price': suggested,
                'change_pct': -10,
                'reason': f'閲覧{views}回だが売上0。価格が高すぎる可能性',
            })
            stale_listings.append(suggestion.copy())

    # 各カテゴリをインパクト順にソート
    price_decrease.sort(key=lambda x: x.get('current_price', 0), reverse=True)
    price_increase.sort(key=lambda x: x.get('watch_count', 0), reverse=True)
    competitor_undercut.sort(key=lambda x: abs(x.get('change_pct', 0)), reverse=True)
    stale_listings.sort(key=lambda x: x.get('watch_count', 0) - x.get('sales_30d', 0))

    return {
        'price_decrease_candidates': price_decrease[:20],
        'price_increase_candidates': price_increase[:10],
        'competitor_undercut': competitor_undercut[:15],
        'stale_listings': stale_listings[:15],
    }


def save_suggestions_to_db(opportunities: Dict):
    """価格提案をDBに保存"""
    if not DB_PATH.exists():
        return

    conn = sqlite3.connect(str(DB_PATH))

    # 全カテゴリの提案をDBに書き込む
    all_suggestions = []
    for category in ['price_decrease_candidates', 'price_increase_candidates',
                     'competitor_undercut', 'stale_listings']:
        all_suggestions.extend(opportunities.get(category, []))

    for item in all_suggestions:
        ebay_id = item.get('ebay_item_id', '')
        suggested = item.get('suggested_price', 0)
        reason = item.get('reason', '')

        if ebay_id and suggested > 0:
            try:
                conn.execute(
                    """UPDATE ebay_listings SET price_suggestion=?, price_suggestion_reason=?
                       WHERE ebay_item_id=?""",
                    (suggested, reason, ebay_id),
                )
            except Exception:
                pass

    conn.commit()
    conn.close()
    logger.info(f"価格提案を {len(all_suggestions)} 件 DB に保存")


def save_report_to_company(opportunities: Dict):
    """価格最適化レポートを組織に保存"""
    try:
        from company_router import get_company_root, _append_to_file, _today, _now_time
    except ImportError:
        return

    company_root = get_company_root()
    if not company_root:
        return

    # secretary/inbox に概要
    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"

    decrease = opportunities.get('price_decrease_candidates', [])
    increase = opportunities.get('price_increase_candidates', [])
    undercut = opportunities.get('competitor_undercut', [])
    stale = opportunities.get('stale_listings', [])

    total = len(decrease) + len(increase) + len(undercut) + len(stale)

    content = f"\n## 価格最適化レポート ({_now_time()})\n\n"
    content += f"- 値下げ候補: {len(decrease)}件\n"
    content += f"- 値上げ候補: {len(increase)}件\n"
    content += f"- 競合負け: {len(undercut)}件\n"
    content += f"- 閲覧多・売上0: {len(stale)}件\n\n"

    if undercut:
        content += "**競合に負けている商品（要対応）:**\n\n"
        for item in undercut[:5]:
            content += f"- {item['title']} | 現在${item['current_price']:.2f} → 提案${item.get('suggested_price', 0):.2f} ({item.get('reason', '')})\n"
        content += "\n"

    if increase:
        content += "**値上げ余地のある商品:**\n\n"
        for item in increase[:5]:
            content += f"- {item['title']} | 現在${item['current_price']:.2f} → 提案${item.get('suggested_price', 0):.2f} ({item.get('reason', '')})\n"
        content += "\n"

    _append_to_file(inbox_file, content)
    logger.info(f"[Router] 価格最適化({total}件) → secretary/inbox/")


def run_price_optimization(config) -> Dict:
    """
    価格最適化タスク

    1. ebay_listings からメトリクス付きリストを取得
    2. 価格最適化の機会を分析
    3. 提案をDBに保存
    4. レポートを組織に配信

    Returns:
        {'success': bool, 'opportunities': dict, 'message': str}
    """
    logger.info("【開始】価格最適化タスク")

    try:
        listings = get_listings_with_metrics()
        if not listings:
            return {
                'success': True,
                'opportunities': {},
                'message': '出品データがありません'
            }

        opportunities = analyze_price_opportunities(listings)

        # DBに保存
        save_suggestions_to_db(opportunities)

        # 組織に配信
        save_report_to_company(opportunities)

        total = sum(len(v) for v in opportunities.values())
        logger.info(f"価格最適化完了: {total}件の提案")

        return {
            'success': True,
            'total_suggestions': total,
            'decrease_count': len(opportunities['price_decrease_candidates']),
            'increase_count': len(opportunities['price_increase_candidates']),
            'undercut_count': len(opportunities['competitor_undercut']),
            'stale_count': len(opportunities['stale_listings']),
            'opportunities': opportunities,
            'message': f'価格最適化完了: {total}件の提案'
        }

    except Exception as e:
        logger.error(f"価格最適化エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'opportunities': {},
            'error': str(e)
        }
