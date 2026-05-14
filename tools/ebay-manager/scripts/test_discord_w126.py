"""W126 Phase 3 verification: Discord webhook 動作確認 (one-shot)."""
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

sys.path.insert(0, r'C:/Users/gucch/projects/claude/tools/ebay-manager')
from notifiers.discord_notifier import DiscordNotifier

cfg_path = r'C:/Users/gucch/projects/claude/tools/ebay-manager/config/schedule_config.json'
with open(cfg_path, encoding='utf-8') as f:
    cfg = json.load(f)

url = (cfg.get('discord') or {}).get('webhook_url')
if not url:
    print('FAIL: no webhook_url in schedule_config.json')
    sys.exit(1)

print(f'webhook_url length: {len(url)}')

msg = (
    '[W126 verify] Phase 3 / 5 fail signal #5 test '
    'from new-path scheduler (PID 46588). '
    'このメッセージが Discord に届けば verification 完了。'
    'new path = C:/Users/gucch/projects/claude'
)

notifier = DiscordNotifier(url)
result = notifier.send_message(msg)
print(f'send_message returned: {result!r}')
