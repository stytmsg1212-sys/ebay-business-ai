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

    W207 (2026-06-01): `DISCORD_KEYWORD_WEBHOOK_URL` (任意) も同じ dict の
    `config['discord']['keyword_webhook_url']` に注入する. キーワード新着監視 (W148)
    の通知を専用チャンネルに分離するため. env 未設定なら key 自体を作らず (= caller 側
    で fallback to DISCORD_WEBHOOK_URL).
    """
    env_wh = (os.environ.get('DISCORD_WEBHOOK_URL') or '').strip()
    env_kw = (os.environ.get('DISCORD_KEYWORD_WEBHOOK_URL') or '').strip()
    env_rv = (os.environ.get('DISCORD_RIVAL_WEBHOOK_URL') or '').strip()
    if not env_wh and not env_kw and not env_rv:
        return config
    disc = config.setdefault('discord', {})
    if env_wh and not (disc.get('webhook_url') or '').strip():
        disc['webhook_url'] = env_wh
    # W207: 専用キーワード webhook (env 設定時のみ). 既存値があれば尊重 (idempotent).
    if env_kw and not (disc.get('keyword_webhook_url') or '').strip():
        disc['keyword_webhook_url'] = env_kw
    # W153 (2026-06-08): 専用ライバル検出 webhook (env 設定時のみ). 未設定なら key を
    # 作らず caller 側で fallback to DISCORD_WEBHOOK_URL.
    if env_rv and not (disc.get('rival_webhook_url') or '').strip():
        disc['rival_webhook_url'] = env_rv
    return config


# 依頼ボード#22 (2026-06-14): 通知カテゴリ別 Discord チャンネル ルーティング。
# 「あらゆる投稿が #notifications に集約」を解消し、種別ごとに専用チャンネルへ振り分ける。
# 各カテゴリは専用 env webhook を持ち、未設定なら DISCORD_WEBHOOK_URL (既定 ch) に fallback。
# → user は分けたいチャンネルだけ作成して env を設定すればよい (残りは自動で既定 ch)。
WEBHOOK_CATEGORY_ENV = {
    'inventory':       'DISCORD_INVENTORY_WEBHOOK_URL',       # 在庫アラート / OOS / 状態不明
    'order':           'DISCORD_ORDER_WEBHOOK_URL',            # 売れた / 注文 / payout
    'rival':           'DISCORD_RIVAL_WEBHOOK_URL',            # ライバルセラー検知 / 価格
    'keyword':         'DISCORD_KEYWORD_WEBHOOK_URL',          # キーワード新着監視
    'research':        'DISCORD_RESEARCH_WEBHOOK_URL',         # harvest / sourcing / 朝brief / 発掘
    'pricing':         'DISCORD_PRICING_WEBHOOK_URL',          # 価格改定 / 燃料サーチャージ
    'system':          'DISCORD_SYSTEM_WEBHOOK_URL',           # ヘルス / エラー / 予算 / lint / 日次レポート
    # W293 (2026-06-29): eBaymag セッション切れ / 復活など user 対応が必要な通知
    'action_required': 'DISCORD_ACTION_REQUIRED_WEBHOOK_URL',
}


def resolve_webhook(category: str = 'default') -> str:
    """通知カテゴリ → webhook URL を解決。

    専用 env (WEBHOOK_CATEGORY_ENV) が設定されていればそれを、未設定なら
    DISCORD_WEBHOOK_URL (既定 ch) に fallback する。category 不明時も既定にfallback。
    → user が一部チャンネルだけ作成しても残りは既定 ch に届き silent drop しない (Q0)。
    """
    env_name = WEBHOOK_CATEGORY_ENV.get(category)
    if env_name:
        url = (os.environ.get(env_name) or '').strip()
        if url:
            return url
    return (os.environ.get('DISCORD_WEBHOOK_URL') or '').strip()


def notifier_for(category: str = 'default') -> 'DiscordNotifier':
    """カテゴリ別 DiscordNotifier を返す (依頼ボード#22)。

    resolve_webhook の結果を bypass_env=True で直接使う = env DISCORD_WEBHOOK_URL に
    よる上書きを避け、カテゴリ専用 webhook を確実に使用する。専用 env 未設定時は
    resolve_webhook が DISCORD_WEBHOOK_URL を返すため従来と同一の既定 ch に届く。
    """
    return DiscordNotifier(resolve_webhook(category), bypass_env=True)


class DiscordNotifier:
    """Discord Webhook 通知クラス"""

    def __init__(self, webhook_url: str, *, bypass_env: bool = False):
        """
        Args:
            webhook_url: Discord Webhook URL (後方互換 fallback only)
            bypass_env: True の時、env DISCORD_WEBHOOK_URL の上書きを無効化し
                webhook_url を直接使用する (W207 2026-06-01)。キーワード新着監視が
                専用チャンネル webhook (DISCORD_KEYWORD_WEBHOOK_URL 由来) へ送る用途。
                既定 False = 従来通り env DISCORD_WEBHOOK_URL を最優先 (他通知は不変)。

        2026-05-25 改訂: .env DISCORD_WEBHOOK_URL を最優先.
        schedule_config.json から渡される webhook_url は legacy fallback で,
        .env が無い時のみ使用. 本来は schedule_config から URL を完全撤去.
        """
        if bypass_env and webhook_url:
            # W207: 呼出側が明示的に渡した webhook (専用チャンネル等) を env で
            # 握り潰さない。これが無いと専用チャンネル分離が env 上書きで無効化される。
            self.webhook_url = webhook_url
            self.timeout = 10
            return
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
        """タスク実行状況をフォーマット (W244: data-driven 化).

        旧実装はハードコード 11 キー dict のため、廃止済み task
        ('research'/'news' 等) の幽霊行が毎回「スキップ」表示され、
        新規 task は永遠にレポートに載らなかった (18 task 中 9 のみ表示)。
        実際に当該 batch で走った results をそのまま列挙し、表示名は
        TASK_SCHEDULE_BY_KEY (定時タスクの単一情報源) から引く。
        skip された task は results に入らない = ここには出ない (skip 可視化は
        task_execution_log + ヘルスチェック + MonoDeck 定時実行タブが担当)。
        """
        if not results:
            return '(この batch で実行されたタスクなし)'

        try:
            from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
        except ImportError:
            TASK_SCHEDULE_BY_KEY = {}

        status_lines = []
        for task_key, result in results.items():
            sched = TASK_SCHEDULE_BY_KEY.get(task_key)
            task_name = sched['display'] if sched else task_key
            if isinstance(result, dict) and result.get('success') is False:
                status = '❌ エラー'
            elif result:
                status = '✅ 完了'
            else:
                status = '❓ 実行結果不明'

            status_lines.append(f"{task_name}: {status}")

        return '\n'.join(status_lines)

    def _next_execution_time(self) -> str:
        """次回実行時刻を算出 (W244: 実スケジュール読込化).

        旧実装は [5, 11, 17, 22] ハードコードで、実際の batch 時刻
        (02:30/11/15/18/22) と乖離した「次回 17:00」等を毎回表示していた。
        schedule_config.json の execution_schedule (times + minutes) を読む。
        読めない場合は現行スケジュールの固定値に fallback。
        """
        times = [2, 11, 15, 18, 22]
        minutes = {2: 30}
        try:
            from pathlib import Path
            import json as _json
            _cfg_path = (Path(__file__).resolve().parent.parent
                         / 'config' / 'schedule_config.json')
            with open(_cfg_path, 'r', encoding='utf-8') as f:
                _sched = _json.load(f).get('execution_schedule', {})
            if _sched.get('times'):
                times = sorted(int(t) for t in _sched['times'])
                minutes = {int(k): int(v)
                           for k, v in (_sched.get('minutes') or {}).items()}
        except Exception as e:
            logger.warning(f"execution_schedule 読込失敗、固定値に fallback: {e}")

        now = datetime.now()
        for hour in times:
            minute = minutes.get(hour, 0)
            if (now.hour, now.minute) < (hour, minute):
                return f"今日 {hour:02d}:{minute:02d}"

        first = times[0]
        return f"明日 {first:02d}:{minutes.get(first, 0):02d}"

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
