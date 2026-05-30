#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord Notifier - Discord Webhook 通知システム
定期実行タスク の結果を Discord チャネルに投稿

2026-05-25 (Codex Phase B review Q4): webhook URL の .env 移行.
旧: schedule_config.json に bare 保存 → git commit で公開リスク
新: .env DISCORD_WEBHOOK_URL 優先, schedule_config は後方互換 fallback
"""

import os
import sys
import json
import requests
import logging
from datetime import datetime
from typing import Optional, Dict, List

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    pass  # dotenv 未インストール時は os.environ 直接参照 (起動側で読み込まれていれば OK)

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)


def inject_webhook_into_config(config: dict) -> dict:
    """`.env` の DISCORD_WEBHOOK_URL を config['discord']['webhook_url'] に in-memory 注入.

    2026-05-25 の .env 移行 (commit 8473103) で schedule_config.json から webhook_url を
    撤去した結果、config['discord']['webhook_url'] が空になり、DiscordNotifier を
    construct する前に `if not config[...]['webhook_url']: return` で early return する
    各タスクの通知ガードが silent skip していた (Q0). 各エントリポイント
    (daily_scheduler / run_task / subprocess task の _load_config) でこれを呼んで空ガードを
    通す. config はディスクに書き戻さないため webhook 再露出なし. 既存値があれば尊重
    (idempotent). 連鎖できるよう同 dict を返す.

    本 module の import 時に module-level load_dotenv が走り .env を os.environ へ展開済の
    ため、呼び側で load_dotenv は不要.
    """
    env_wh = (os.environ.get('DISCORD_WEBHOOK_URL') or '').strip()
    if not env_wh:
        return config
    disc = config.setdefault('discord', {})
    if not (disc.get('webhook_url') or '').strip():
        disc['webhook_url'] = env_wh
    return config


class DiscordNotifier:
    """Discord Webhook 通知クラス"""

    def __init__(self, webhook_url: str):
        """
        Args:
            webhook_url: Discord Webhook URL (後方互換 fallback only)

        2026-05-25 改訂: .env DISCORD_WEBHOOK_URL を最優先.
        schedule_config.json から渡される webhook_url は legacy fallback で,
        .env が無い時のみ使用. 本来は schedule_config から URL を完全撤去.
        """
        env_url = (os.environ.get('DISCORD_WEBHOOK_URL') or '').strip()
        if env_url:
            self.webhook_url = env_url
        else:
            if webhook_url and 'discord.com/api/webhooks' in webhook_url:
                # legacy schedule_config 由来. 警告 log で .env 移行を促す.
                logger.warning(
                    "Discord webhook_url が schedule_config 由来. .env DISCORD_WEBHOOK_URL に "
                    "移行してください (security risk: git commit で公開)"
                )
            self.webhook_url = webhook_url or ''
        self.timeout = 10

    def send_message(self, message: str, embed: Optional[Dict] = None) -> bool:
        """
        シンプルなメッセージを送信

        Args:
            message: メッセージテキスト
            embed: 埋め込みオブジェクト（オプション）

        Returns:
            成功: True, 失敗: False
        """
        try:
            payload = {
                'content': message,
            }
            if embed:
                payload['embeds'] = [embed]

            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            logger.info("Discord メッセージ送信成功")
            return True

        except Exception as e:
            logger.error(f"Discord メッセージ送信失敗: {e}")
            return False

    def send_daily_report(self, results: Dict) -> bool:
        """
        日々の全体実行結果をレポート

        Args:
            results: タスク実行結果の辞書

        Returns:
            成功: True, 失敗: False
        """
        try:
            embed = self._create_daily_report_embed(results)
            return self.send_message("📊 日々の定時実行レポート", embed)

        except Exception as e:
            logger.error(f"日々レポート送信失敗: {e}")
            return False

    def _create_daily_report_embed(self, results: Dict) -> Dict:
        """日々レポート埋め込みを生成"""

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        task_status = self._format_task_status(results)

        embed = {
            'title': '📊 eBay Manager 日々の実行レポート',
            'color': 3447003,  # Blue
            'timestamp': now,
            'fields': [
                {
                    'name': '実行時刻',
                    'value': now,
                    'inline': False
                },
                {
                    'name': 'タスク実行状況',
                    'value': task_status,
                    'inline': False
                },
                {
                    'name': '次回実行予定',
                    'value': self._next_execution_time(),
                    'inline': False
                }
            ]
        }

        return embed

    def _format_task_status(self, results: Dict) -> str:
        """タスク実行状況をフォーマット"""

        status_lines = []

        task_names = {
            'company_secretary': '🗂️ 秘書ルーティン',
            'ebay_sync': '🔄 eBay同期',
            'inventory_check': '📦 在庫チェック',
            'inventory_alert': '⚠️ 在庫切れ通知',
            'supplier_select': '🏪 仕入先候補選出',
            'email': '📧 メール取得',
            'research': '🔍 新商品リサーチ',
            'rival_detection': '👥 ライバルセラー検出',
            'data_sync': '🗄️ データストア統合',
            'price_optimization': '💰 価格最適化',
            'news': '📰 AIニュース',
        }

        for task_key, task_name in task_names.items():
            result = results.get(task_key)
            if result is None:
                status = '⏭️ スキップ'
            elif isinstance(result, dict) and result.get('success') is False:
                status = f'❌ エラー'
            elif result:
                status = '✅ 完了'
            else:
                status = '❓ 実行結果不明'

            status_lines.append(f"{task_name}: {status}")

        return '\n'.join(status_lines)

    def _next_execution_time(self) -> str:
        """次回実行時刻を算出"""

        now = datetime.now()
        execution_times = [5, 11, 17, 22]

        # 今日のこれからの実行時刻
        for hour in execution_times:
            if now.hour < hour:
                return f"今日 {hour:02d}:00"

        # 明日の最初の実行時刻
        return f"明日 05:00"

    def send_inventory_alert(self, out_of_stock_items: List[Dict],
                            supplier_candidates: List[Dict]) -> bool:
        """
        在庫切れアラート送信

        Args:
            out_of_stock_items: 在庫切れになった商品リスト
            supplier_candidates: 仕入先候補リスト

        Returns:
            成功: True, 失敗: False
        """
        try:
            embed = self._create_inventory_alert_embed(
                out_of_stock_items, supplier_candidates
            )
            return self.send_message("⚠️ 在庫切れアラート", embed)

        except Exception as e:
            logger.error(f"在庫切れアラート送信失敗: {e}")
            return False

    def _create_inventory_alert_embed(self, out_of_stock_items: List[Dict],
                                      supplier_candidates: List[Dict]) -> Dict:
        """在庫切れアラート埋め込みを生成"""

        embed = {
            'title': '⚠️ 在庫切れになった商品',
            'color': 15158332,  # Red
            'fields': []
        }

        if out_of_stock_items:
            items_text = '\n'.join([
                f"• {item.get('title', 'N/A')}"
                for item in out_of_stock_items[:10]  # 最初の10件
            ])

            embed['fields'].append({
                'name': '対象商品',
                'value': items_text if items_text else 'なし',
                'inline': False
            })

        if supplier_candidates:
            candidates_text = '\n'.join([
                f"• {c.get('name', 'N/A')} (スコア: {c.get('score', 0):.2f})"
                for c in supplier_candidates[:5]  # Top 5
            ])

            embed['fields'].append({
                'name': '推奨仕入先',
                'value': candidates_text if candidates_text else 'なし',
                'inline': False
            })

        return embed

    def send_news_summary(self, news_items: List[Dict]) -> bool:
        """
        ニュースサマリー送信

        Args:
            news_items: ニュース記事リスト

        Returns:
            成功: True, 失敗: False
        """
        try:
            embed = self._create_news_embed(news_items)
            return self.send_message("📰 Claude/AI ニュース速報", embed)

        except Exception as e:
            logger.error(f"ニュース送信失敗: {e}")
            return False

    def _create_news_embed(self, news_items: List[Dict]) -> Dict:
        """ニュース埋め込みを生成"""

        embed = {
            'title': '📰 Claude/AI ニュース（本日）',
            'color': 9807270,  # Orange
            'fields': []
        }

        for news in news_items[:5]:  # 最初の5件
            title = news.get('title', 'N/A')
            summary = news.get('summary', '')[:200]
            impact = news.get('impact_on_tool', '')[:100]

            field_value = f"{summary}\n\n**このツールへの影響**: {impact}"

            embed['fields'].append({
                'name': title,
                'value': field_value,
                'inline': False
            })

        return embed


def test_connection(webhook_url: str) -> bool:
    """Webhook 接続テスト"""
    try:
        notifier = DiscordNotifier(webhook_url)
        return notifier.send_message(
            "✅ eBay Manager ディスコード接続テスト成功！"
        )
    except Exception as e:
        logger.error(f"接続テスト失敗: {e}")
        return False


if __name__ == '__main__':
    # テスト用
    import sys

    if len(sys.argv) > 1:
        webhook_url = sys.argv[1]
        if test_connection(webhook_url):
            print("✅ Discord 接続テスト成功")
        else:
            print("❌ Discord 接続テスト失敗")
    else:
        print("使用方法: python discord_notifier.py <WEBHOOK_URL>")
