"""W133 item3 (2026-05-16): 在庫0 Discord アラート 検証 (one-shot).

R-11 (feedback_discord_visual_verify_required): HTTP 204 は endpoint 受信
signal にすぎず user 到達 signal ではない. 本 script は user 実視認の前段として:

  STEP 1: webhook が discord-notification.md の正規 channel/guild に向くか機械検証
          (2026-05-14 事故 = 別アカウント login で 204 でも未到達、の正面対策)
  STEP 2: 本番 run_order_alert_check と **完全同一フォーマット**の在庫0 embed を
          実 task_order_alert._send_discord 経由で送信 (faithful path)

検証 test と明示マーカ付き (real listing が在庫0 化したわけではない旨)。
最終的な「Discord で見えた」確認は user 実視認 (R-11、本 script では不可)。
webhook URL は secret のため出力しない (security.md)。
"""
import json
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, r'C:/Users/gucch/projects/claude/tools/ebay-manager')

from tasks.task_order_alert import _send_discord

# discord-notification.md の正規値 (人間可読 doc の権威値と照合)
EXPECT_GUILD = '1492273038782238881'
EXPECT_CHANNEL = '1492273557277774005'
CFG_PATH = r'C:/Users/gucch/projects/claude/tools/ebay-manager/config/schedule_config.json'


def _check_webhook_integrity(webhook: str) -> bool:
    """Discord API GET で channel/guild を照合 (Cloudflare 回避で DiscordBot UA)."""
    req = urllib.request.Request(
        webhook, method='GET',
        headers={'User-Agent': 'DiscordBot (https://github.com, 1.0)'},
    )
    info = json.loads(urllib.request.urlopen(req, timeout=8).read())
    ch, gu, nm = info.get('channel_id'), info.get('guild_id'), info.get('name')
    print(f'  webhook name : {nm}')
    print(f'  channel_id   : {ch}  (expect {EXPECT_CHANNEL})  '
          f'{"OK" if ch == EXPECT_CHANNEL else "MISMATCH"}')
    print(f'  guild_id     : {gu}  (expect {EXPECT_GUILD})  '
          f'{"OK" if gu == EXPECT_GUILD else "MISMATCH"}')
    return ch == EXPECT_CHANNEL and gu == EXPECT_GUILD


def main() -> int:
    with open(CFG_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    webhook = (cfg.get('discord') or {}).get('webhook_url') or ''
    if not webhook:
        print('RESULT: FAIL (schedule_config.json に discord.webhook_url が無い)')
        return 1
    print(f'webhook 解決 OK (len={len(webhook)}, 値は secret のため非表示)')

    print('=== STEP 1: webhook 整合性 (R-11 正面対策) ===')
    try:
        integrity_ok = _check_webhook_integrity(webhook)
    except Exception as e:  # noqa: BLE001
        print(f'RESULT: FAIL (webhook info 取得失敗、送信せず中断): {e}')
        return 1
    if not integrity_ok:
        print('RESULT: FAIL (webhook が正規 channel/guild に向いていない '
              '= 送信しても user 主 server に届かない。送信せず中断)')
        return 2

    print('=== STEP 2: 本番同一フォーマットの在庫0 embed を実送信 ===')
    # task_order_alert.run_order_alert_check L478-487 と同一構造。
    # 3 状態 (✅反映済 / ⚠️抑止 / ❌失敗) を 1 通に同梱し本番表示を再現。
    # ※ [W133 item3 検証] マーカで test と明示 (real 在庫0 ではない)。
    fields = [
        {'name': 'Ohuhu x Sanrio 80 Color (3534)',
         'value': '在庫0 / ✅ eBay反映済', 'inline': False},
        {'name': 'Le Creuset Mini Cocotte (6749)',
         'value': '在庫0 / ⚠️ eBay反映抑止 (OOS未確認)', 'inline': False},
        {'name': 'Google Pixel Tablet Dock (7584)',
         'value': '在庫0 / ❌ eBay反映失敗: API エラー: sample', 'inline': False},
    ]
    embed = {
        'title': '[W133 item3 検証] [在庫] 有在庫 listing が在庫0 / eBay反映に注意',
        'description': (
            '【これは W133 item3 の検証送信です。実際に listing が在庫0 に '
            'なったわけではありません】\n'
            '3 件の有在庫 listing が在庫0 化、または eBay 数量反映が抑止/失敗 '
            'しました. 補充 or 出品停止を確認してください.'
        ),
        'color': 0xD84C38,
        'fields': fields,
        'timestamp': datetime.now().isoformat(),
    }
    sent = _send_discord(webhook, embed)
    print(f'  _send_discord returned: {sent} (True = HTTP 200/204)')
    if not sent:
        print('RESULT: FAIL (送信が False = HTTP 非 200/204)')
        return 1
    print('RESULT: SENT-OK (HTTP 204 + webhook 整合性 OK)。')
    print('  → R-11: 最終確認は user が Discord #bot通知 付近で本 embed を '
          '実視認すること (HTTP 204 ≠ 到達)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
