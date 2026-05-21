# Discord 通知 設定 (常時参照)

eBay Manager の scheduler / health check / 注文アラート / 予算アラート / customs_automation 等の自動通知は **`eBay Manager`** という Discord webhook 経由で送信される。

出典: 2026-05-14 W126 Phase 3 verification で「scheduler は送信成功 (HTTP 204) しているが user は別アカウント login で気付けていなかった」事故 → R-11 制定 (`feedback_discord_visual_verify_required.md`)。

## webhook 詳細

| 項目 | 値 |
|---|---|
| webhook 名 | `eBay Manager` |
| Server ID (guild_id) | `1492273038782238881` |
| Channel ID | `1492273557277774005` |
| Channel 名 (人間可読) | `#bot通知` 付近 (user の主 Discord server 内、正確名は要再確認) |
| webhook URL 保存先 | `tools/ebay-manager/config/schedule_config.json` の `discord.webhook_url` |

## user 視認確認の前提 (R-11 / 2026-05-14 事故から)

- **webhook 登録 server に参加しているアカウントで Discord login する** ことが必須
- 別アカウントで login していると HTTP 204 が返っても user の眼球に届かない
- アカウント違い検知: Discord ⚙️ Settings → 詳細設定 → **開発者モード ON** → Server / Channel を右クリック「ID をコピー」で本 doc の値と照合

## 送信されるイベント (主要)

| イベント | cron / trigger | 内容 |
|---|---|---|
| 予算アラート | 06:00 / 12:00 / 19:00 | Anthropic API cost 残高 |
| 定時実行ヘルスチェック | 04:00 / 12:00 / 16:00 / 19:00 | missed task 検出 |
| HV 注文 / DDP / 在庫減少 alert | 30 分毎 W7-A | order_alert_check |
| customs_automation | event 駆動 | FedEx 通関書類 draft 生成 |
| W122 morning_discovery | 07:00 | 新商品発掘候補 3 件 |
| W184 新規競合 alert | event 駆動 | ライバル増加 alert |
| watchdog 復活通知 | scheduler down 検出時 | scheduler restart |

## 復旧手順 (届かない時)

1. webhook info を Discord API で照会 (Cloudflare bot detection 回避のため User-Agent: `DiscordBot` 必須):
   ```python
   import urllib.request, json
   url = '<webhook_url from schedule_config.json>'
   req = urllib.request.Request(url, method='GET', headers={'User-Agent': 'DiscordBot (https://github.com, 1.0)'})
   info = json.loads(urllib.request.urlopen(req, timeout=8).read())
   # info['channel_id'] / info['guild_id'] / info['name'] を取得
   ```
2. 返ってきた channel_id / guild_id を Discord 上で Developer Mode で照合
3. 別アカウント login していたら正しい account で login し直し
4. test message 送信 (`tools/ebay-manager/scripts/test_discord_w126.py` 参考) + user 実視認確認

## 関連

- `feedback_discord_visual_verify_required.md` (R-11): 通知系 verify は user 実視認まで
- W128 (`system_improvements.json` id 212): Discord メタ health check + Email 二重通知 (ROADMAP、未着手)
- `notifiers/discord_notifier.py`: DiscordNotifier クラス本体
