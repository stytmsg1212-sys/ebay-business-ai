"""
燃料サーチャージ 週次手動更新リマインダー（通知専用 / notify-only）

2026-05-31 user 選択で「通知専用化」に転換。旧実装は FedEx/DHL の **小売公式ページ**を
scrape し、差分 <5pt なら settings.json に自動反映していた。しかし利益計算 (calculator.py)
が price するのは **CPaSS 便**であり、CPaSS の燃油サーチャージ表は小売とは別 (login 必須
PDF)。「小売値 ≠ CPaSS値」のまま自動上書きすると、正しい CPaSS 値を誤った小売値で潰す
money-direct なリスクがあった (実際 2026-04-19 以降 抽出は失敗し続け、毎週 health check の
失敗タスクにも計上されていた)。

本タスクは settings.json を **一切書き換えない**。毎週月曜にユーザーへ「今週の CPaSS 値を
確認し MonoDeck 全体設定で更新」するリマインダーを Discord 送信するだけ。

`fuel_surcharge_last_updated` は「最後に **手動** 更新した日時」を表す。設定経路:
- MonoDeck →「全体設定」タブの保存 (app.py、fuel 値変更時に自動記録)
- fuel_surcharge_manager.apply_surcharge_update (CPaSS PDF 取込ヘルパ)
本タスクはこの値を **読むだけ** で、経過日数からリマインダー文面を組み立てる。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows CP932対策: 親プロセスが utf8_console 未import でも安全に自己適用
_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
try:
    import utf8_console  # noqa: F401
except Exception:
    pass


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = BASE_DIR / "settings.json"

# CPaSS 燃油サーチャージは login 必須 PDF（手動取得）。リマインダー内の参照ヒント。
CPASS_SOURCE_HINT = "CPaSS 管理画面の燃油サーチャージ表（FedEx / DHL、login 必須 PDF）"

# 手動更新からこの日数を超えたら警告レベルを上げる（app.py UPDATE_WARNING_DAYS=30 と整合）
STALE_WARNING_DAYS = 30
# 2 週を超えて未更新なら「反映漏れ確認」を促す中間警告
STALE_NOTICE_DAYS = 14


def _notify(webhook_url: str, message: str) -> tuple[bool, bool]:
    """リマインダーを Discord 送信。戻り値 (has_webhook, sent)。

    依頼ボード#22 (2026-06-20): notifier_for("pricing") 経由でカテゴリ別チャンネルに振り分け。
    DISCORD_PRICING_WEBHOOK_URL 未設定時は DISCORD_WEBHOOK_URL (既定 ch) に自動 fallback。
    通知専用 task では Discord 送信が唯一の deliverable なので、webhook 未設定 / 送信失敗は
    logger.error で痕跡を残す（Q0 / R-11）。
    R-11: HTTP 2xx は endpoint 受信であって user 到達 signal ではない点に注意。
    """
    from notifiers.discord_notifier import notifier_for
    notifier = notifier_for("pricing")
    if not notifier.webhook_url:
        logger.error(
            "燃料サーチャージ リマインダー: webhook 未設定 = user に届かない. "
            ".env DISCORD_WEBHOOK_URL を確認 (Q0 silent skip 防止)"
        )
        return False, False
    sent = notifier.send_message(message)
    if not sent:
        logger.error("燃料サーチャージ リマインダー: Discord 送信失敗 (R-11 user 不達)")
    return True, sent


def _build_reminder(cur_fedex: float, cur_dhl: float, last_str: str,
                    days_ago: Optional[int]) -> str:
    """週次リマインダー文面を組み立てる。"""
    days_label = f"{days_ago} 日経過" if days_ago is not None else "不明"
    parts: list[str] = [
        ":fuelpump: **燃料サーチャージ 週次更新リマインダー**",
        "",
        "今週の CPaSS 燃油サーチャージ（FedEx / DHL）を確認し、変わっていれば更新してください。",
        "",
        f"**現在の設定値**: FedEx {cur_fedex}% / DHL {cur_dhl}%",
        f"**最終手動更新**: {last_str}（{days_label}）",
        "",
        "**更新手順**:",
        f"1. {CPASS_SOURCE_HINT} で今週適用の値を確認",
        "2. MonoDeck →「全体設定」タブ →「燃料サーチャージ FedEx（%）」「燃料サーチャージ DHL（%）」"
        "に入力 → 保存",
        "   （保存すると最終更新日時が自動記録され、本リマインダーの経過日数がリセットされます）",
    ]
    if days_ago is not None and days_ago > STALE_WARNING_DAYS:
        parts += [
            "",
            f":rotating_light: **{STALE_WARNING_DAYS}日以上 手動更新されていません。"
            "利益計算が古い値で稼働中、即時更新を推奨。**",
        ]
    elif days_ago is not None and days_ago > STALE_NOTICE_DAYS:
        parts += [
            "",
            ":warning: **2週間以上 更新されていません。今週分の反映漏れがないか確認してください。**",
        ]
    return "\n".join(parts)


def run_fuel_surcharge_check(config: dict) -> dict:
    """通知専用: settings.json は読むだけ。毎週月曜に CPaSS 手動更新リマインダーを送る。

    旧実装の小売 scrape→settings 自動反映は撤去（小売値 ≠ CPaSS値 の誤上書き防止）。

    Returns:
        {
            'success': bool,            # 送信試行が成功 / webhook 未設定なら True（実行成功）
            'fedex_rate': float,        # 現在 settings 値（参考、取得値ではない）
            'dhl_rate': float,
            'days_since_update': int or None,
            'reminder_sent': bool,      # 実際に Discord 送信できたか
            'changed': False,           # 後方互換: 本タスクは settings を変更しない
        }
    """
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        settings = json.load(f)

    cur_fedex = float(settings.get('fuel_surcharge_fedex', 0))
    cur_dhl = float(settings.get('fuel_surcharge_dhl', 0))

    last_str = settings.get('fuel_surcharge_last_updated', '不明')
    days_ago: Optional[int] = None
    try:
        last_dt = datetime.fromisoformat(last_str)
        days_ago = (datetime.now() - last_dt).days
    except (ValueError, TypeError):
        days_ago = None

    webhook = (config.get('discord', {}) or {}).get('webhook_url') or settings.get('discord_webhook_url', '')

    message = _build_reminder(cur_fedex, cur_dhl, last_str, days_ago)
    has_webhook, sent = _notify(webhook, message)
    logger.info(
        f"燃料サーチャージ リマインダー: last_updated={last_str}, days_ago={days_ago}, "
        f"has_webhook={has_webhook}, sent={sent}"
    )

    return {
        # webhook 未設定（test/dev）は「送るものがない」= 実行成功扱い。
        # webhook 有で送信失敗（本番 Discord 障害）は success=False で health に拾わせる。
        'success': True if not has_webhook else sent,
        'fedex_rate': cur_fedex,
        'dhl_rate': cur_dhl,
        'days_since_update': days_ago,
        'reminder_sent': sent,
        'changed': False,
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    config_path = BASE_DIR / 'config' / 'schedule_config.json'
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)
    result = run_fuel_surcharge_check(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['success'] else 1)
