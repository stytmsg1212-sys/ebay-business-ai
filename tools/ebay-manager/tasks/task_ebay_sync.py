#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: eBay連携・同期 - monitor/ebay_sync.py を統合実行
498件のeBay出品を同期し、ランク（A～E）を再計算
"""

import sys
import logging
from pathlib import Path

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

# monitor モジュールをインポート
import_path = Path(__file__).parent.parent / 'monitor'
sys.path.insert(0, str(import_path.parent))

from monitor.ebay_sync import (
    sync_listings_from_ebay,
    auto_rank_all_listings_in_db,
    get_sync_report
)
from monitor.credentials import get_ebay_credentials, ebay_credentials_ok


def run_ebay_sync(config):
    """
    eBay API 同期を実行 - 498件の出品情報を最新化、ランク再計算

    Args:
        config: 設定辞書（eBay API認証情報を含む）

    Returns:
        {'success': bool, 'synced_count': int, 'details': dict}
    """

    logger.info("【開始】eBay連携同期タスク")

    try:
        # eBay API 認証情報を取得（.env 優先、schedule_config.json fallback）
        creds = get_ebay_credentials(config)
        if not ebay_credentials_ok(creds):
            logger.warning("eBay API 認証情報が設定されていません (.env または config)")
            return {
                'success': False,
                'synced_count': 0,
                'details': {},
                'message': 'eBay credentials not configured'
            }
        app_id = creds['app_id']
        dev_id = creds['dev_id']
        cert_id = creds['cert_id']
        user_token = creds['user_token']

        # Step 1: eBay同期実行
        logger.info("ステップ1: eBayからアクティブ出品を取得中...")
        sync_result = sync_listings_from_ebay(
            app_id=app_id,
            dev_id=dev_id,
            cert_id=cert_id,
            user_token=user_token
        )

        synced_count = sync_result.get('synced', 0)
        matched_count = sync_result.get('matched', 0)
        ended_count = sync_result.get('ended', 0)
        reactivated_count = sync_result.get('reactivated', 0)
        retirement_skipped = sync_result.get('retirement_skipped', False)
        sync_errors = sync_result.get('errors', 0)
        sync_messages = sync_result.get('messages', [])

        logger.info(
            f"ステップ1 完了: {synced_count}件同期, {matched_count}件マッチング, "
            f"{ended_count}件退役, {reactivated_count}件復活"
            + (" (退役検出スキップ)" if retirement_skipped else "")
        )

        # Step 2: ランク自動計算
        logger.info("ステップ2: ランク（A～E）を自動計算中...")
        rank_result = auto_rank_all_listings_in_db()

        rank_assigned = rank_result.get('rank_assigned', 0)
        rank_errors = rank_result.get('errors', 0)
        rank_distribution = rank_result.get('distribution', {})

        logger.info(f"ステップ2 完了: {rank_assigned}件のランク計算完了")

        # Step 3: 同期レポート取得
        logger.info("ステップ3: 同期レポート取得中...")
        report = get_sync_report()

        logger.info("eBay連携同期完了")

        # 結果をまとめる
        return {
            'success': True,
            'synced_count': synced_count,
            'details': {
                'sync': {
                    'synced': synced_count,
                    'matched': matched_count,
                    'ended': ended_count,
                    'reactivated': reactivated_count,
                    'retirement_skipped': retirement_skipped,
                    'errors': sync_errors,
                    'messages': sync_messages
                },
                'rank': {
                    'assigned': rank_assigned,
                    'errors': rank_errors,
                    'distribution': rank_distribution
                },
                'report': report
            },
            'message': f'eBay同期完了: {synced_count}件同期, ランク{rank_assigned}件計算'
        }

    except Exception as e:
        logger.error(f"eBay同期エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'synced_count': 0,
            'details': {},
            'error': str(e)
        }
