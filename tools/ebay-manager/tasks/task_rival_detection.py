#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: ライバルセラー検出
eBay Finding API で自分の主力商品と同じキーワードの出品を検索し、
日本発セラーの中から新規参入者を検出する。

定時実行で使える手段（Claude不要）:
1. monitor.db から自分の主力商品のタイトル/キーワードを取得
2. eBay Finding API で同キーワードの他セラー出品を検索
3. 既知セラーリストと比較して新規を検出
4. 結果をファイルに保存 + Discord通知
"""

import sys
import re
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List

import httpx

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / 'data' / 'monitor.db'
KNOWN_SELLERS_FILE = BASE_DIR / 'data' / 'known_rival_sellers.json'
FINDING_API_URL = "https://svcs.ebay.com/services/search/FindingService/v1"


def get_top_selling_keywords(limit: int = 10) -> List[str]:
    """
    monitor.db から売上/ウォッチ数上位商品のキーワードを抽出
    ランクA以上、またはwatch_count上位の商品タイトルから検索キーワードを生成
    """
    if not DB_PATH.exists():
        logger.warning(f"データベースが見つかりません: {DB_PATH}")
        return []

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # ランクが高い or watch_count が多い商品のタイトルを取得
        cursor.execute("""
            SELECT title, watch_count, rank
            FROM ebay_listings
            WHERE rank IN ('S', 'A', 'B')
              AND COALESCE(is_ended, 0) = 0
            ORDER BY watch_count DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            # ランクがなければwatch_count順
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT title, watch_count, rank
                FROM ebay_listings
                WHERE COALESCE(is_ended, 0) = 0
                ORDER BY watch_count DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            conn.close()

        keywords = []
        for title, watch, rank in rows:
            # タイトルから主要キーワードを抽出（記号・絵文字除去、最初の3-4語）
            clean = title.replace('☆', '').replace('★', '').replace('✅', '').strip()
            words = clean.split()[:4]
            kw = ' '.join(words)
            if kw and len(kw) > 5:
                keywords.append(kw)

        logger.info(f"主力商品キーワード: {len(keywords)}件抽出")
        return keywords

    except Exception as e:
        logger.error(f"キーワード抽出エラー: {e}")
        return []


def search_competing_sellers_via_browse_api(keywords: str, config: Dict) -> List[Dict]:
    """
    eBay Browse API で競合セラーを検索
    日本発セラーの出品を取得し、セラー情報を抽出
    """
    from monitor.credentials import get_ebay_credentials
    _creds = get_ebay_credentials(config)
    app_id = _creds.get('app_id', '')
    cert_id = _creds.get('cert_id', '')

    if not app_id or not cert_id:
        logger.warning("eBay API credentials が未設定")
        return []

    try:
        from tasks.ebay_browse_api import BrowseAPIClient
        client = BrowseAPIClient(app_id, cert_id)
        items = client.search_items(keywords, limit=50, item_location_country="JP")
    except Exception as e:
        logger.warning(f"Browse API エラー ({keywords}): {e}")
        return []

    sellers = []
    for item in items:
        seller_name = item.get('seller', '')
        if seller_name:
            sellers.append({
                'seller': seller_name,
                'feedback_score': item.get('feedback_score', 0),
                'item_title': item.get('title', ''),
                'item_id': item.get('item_id', ''),
                'price_usd': item.get('price_usd', 0),
                'search_keyword': keywords,
            })

    logger.info(f"Browse API: '{keywords[:40]}' → {len(sellers)}セラー検出")
    return sellers


def load_known_sellers() -> set:
    """既知のライバルセラーリストを読み込む"""
    if not KNOWN_SELLERS_FILE.exists():
        return set()

    try:
        with open(KNOWN_SELLERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('sellers', []))
    except Exception:
        return set()


def save_known_sellers(sellers: set):
    """ライバルセラーリストを保存"""
    KNOWN_SELLERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        'sellers': sorted(list(sellers)),
        'updated_at': datetime.now().isoformat(),
        'count': len(sellers),
    }
    with open(KNOWN_SELLERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_my_seller_id(config: Dict) -> str:
    """自分のセラーIDを取得（configまたはDBから）"""
    seller_id = config.get('ebay', {}).get('seller_id', '')
    if seller_id:
        return seller_id

    # DBから推測（最も多く出品しているセラー = 自分）
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ebay_listings")
            count = cursor.fetchone()[0]
            conn.close()
            if count > 0:
                return '__self__'  # 自分のリスティングなのでフィルタ不要
        except Exception:
            pass

    return ''


def save_detection_results(new_sellers: List[Dict], all_sellers_found: int):
    """検出結果をファイルに保存"""
    output_dir = BASE_DIR / 'data' / 'rival_detection'
    output_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    output_file = output_dir / f"{today}-rivals.json"

    result = {
        'date': today,
        'timestamp': datetime.now().isoformat(),
        'new_sellers': new_sellers,
        'new_count': len(new_sellers),
        'total_sellers_scanned': all_sellers_found,
    }

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"検出結果を保存: {output_file}")


def _save_to_db(new_sellers: List[Dict]):
    """検出結果を monitor.db の new_competitor_alerts テーブルにも保存"""
    if not DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        for seller in new_sellers:
            seller_name = seller.get('seller', '')
            for item in seller.get('competing_items', []):
                # 真の eBay legacy item id を優先 (W98 リンク・送料取得が機能するため必須).
                # 取得不可時のみ合成 ID で fallback (旧仕様、UI 側で除外フィルタ).
                _item_id = item.get('legacy_item_id') or (
                    f"synthetic_{seller_name}_{item.get('keyword', '')[:20]}"
                )
                try:
                    cursor.execute("""
                        INSERT OR IGNORE INTO new_competitor_alerts
                        (our_item_id, keyword, found_item_id, found_seller,
                         found_location, found_price, is_japan_seller, found_at)
                        VALUES (?, ?, ?, ?, 'Japan', ?, 1, ?)
                    """, (
                        '',  # our_item_id は不明（キーワード検索のため）
                        item.get('keyword', ''),
                        _item_id,
                        seller_name,
                        item.get('price_usd', 0),
                        datetime.now().isoformat(),
                    ))
                except sqlite3.IntegrityError:
                    pass  # 重複は無視

        conn.commit()
        conn.close()
        logger.info(f"DB に {len(new_sellers)} セラーのアラートを保存")
    except Exception as e:
        logger.warning(f"DB保存エラー（継続）: {e}")


def run_rival_detection(config):
    """
    新規ライバルセラーを検出

    1. 自分の主力商品のキーワードを取得
    2. eBay Finding API で同キーワードの日本発セラーを検索
    3. 既知セラーリストと比較し、新規を検出
    4. 既知セラーリストを更新

    Args:
        config: 設定辞書

    Returns:
        {'success': bool, 'new_sellers_count': int, 'sellers': list}
    """

    logger.info("【開始】ライバルセラー検出タスク")

    try:
        # Step 1: 主力商品キーワード取得
        keywords_list = get_top_selling_keywords(limit=5)
        if not keywords_list:
            logger.warning("主力商品キーワードが取得できません")
            return {
                'success': False,
                'new_sellers_count': 0,
                'sellers': [],
                'message': 'No top selling keywords found'
            }

        # Step 2: 既知セラーリスト読み込み
        known_sellers = load_known_sellers()
        my_seller_id = get_my_seller_id(config)
        logger.info(f"既知ライバルセラー: {len(known_sellers)}件")

        # Step 3: 各キーワードで競合検索
        all_found_sellers = {}  # seller_name -> info

        for keywords in keywords_list:
            logger.info(f"競合検索: '{keywords[:50]}'")
            sellers = search_competing_sellers_via_browse_api(keywords, config)

            for s in sellers:
                seller_name = s['seller']
                # 自分自身を除外
                if seller_name == my_seller_id:
                    continue

                if seller_name not in all_found_sellers:
                    all_found_sellers[seller_name] = {
                        'seller': seller_name,
                        'feedback_score': s['feedback_score'],
                        'competing_items': [],
                        'first_seen': datetime.now().isoformat(),
                    }

                # Browse API itemId は "v1|285999999001|0" 形式. 末尾の数値部のみ抽出.
                _raw_iid = s.get('item_id', '')
                _legacy_iid = ''
                if _raw_iid:
                    _parts = _raw_iid.split('|')
                    if len(_parts) >= 2:
                        _legacy_iid = _parts[1]
                    else:
                        _legacy_iid = _raw_iid
                all_found_sellers[seller_name]['competing_items'].append({
                    'title': s['item_title'],
                    'price_usd': s['price_usd'],
                    'keyword': s['search_keyword'],
                    'legacy_item_id': _legacy_iid,
                })

        # Step 4: 新規セラーを検出
        new_sellers = []
        for seller_name, info in all_found_sellers.items():
            if seller_name not in known_sellers:
                info['is_new'] = True
                info['competing_count'] = len(info['competing_items'])
                new_sellers.append(info)

        # feedback_score 順にソート（強い競合を上に）
        new_sellers.sort(key=lambda x: x['feedback_score'], reverse=True)

        # Step 5: 既知セラーリストを更新
        updated_sellers = known_sellers | set(all_found_sellers.keys())
        save_known_sellers(updated_sellers)

        # Step 6: 結果保存（ファイル + DB）
        save_detection_results(new_sellers, len(all_found_sellers))
        _save_to_db(new_sellers)

        logger.info(f"ライバル検出完了: 新規{len(new_sellers)}件 / 全{len(all_found_sellers)}件スキャン")

        return {
            'success': True,
            'new_sellers_count': len(new_sellers),
            'total_scanned': len(all_found_sellers),
            'sellers': new_sellers[:20],  # 上位20件
            'message': f'新規ライバル{len(new_sellers)}件検出（{len(all_found_sellers)}件スキャン）'
        }

    except Exception as e:
        logger.error(f"ライバル検出エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'new_sellers_count': 0,
            'sellers': [],
            'error': str(e)
        }
