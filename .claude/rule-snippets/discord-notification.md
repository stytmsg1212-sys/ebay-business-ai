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
| webhook URL 保存先 | `.env` の `DISCORD_WEBHOOK_URL` (2026-05-25 移行、`schedule_config.json` 旧記録は legacy fallback) |

### W207 専用キーワードチャンネル (2026-06-01〜)

キーワード新着監視 (W148 task) の通知だけを分離する用に、**専用 webhook** が `.env` に追加されている。

| 項目 | 値 |
|---|---|
| env 変数名 | `DISCORD_KEYWORD_WEBHOOK_URL` |
| 用途 | W148 keyword crawl task の **全 Discord 送信** (hit 通知 / DOM rot / orphan / resend pass DB error) |
| 注入先 | `config['discord']['keyword_webhook_url']` (`inject_webhook_into_config` で自動注入) |
| fallback | env / config 未設定なら `DISCORD_WEBHOOK_URL` (既定 webhook) に自動フォールバック (Q0: 通知先消失を防ぐ) |
| 他通知 | ヘルスチェック / 予算アラート / 在庫切れ等は引き続き `DISCORD_WEBHOOK_URL` (既定 webhook) のまま |

### W153 専用ライバル検出チャンネル (2026-06-08〜)

商品別ライバル検出 (W153 task_rival_detection) の通知を分離する用に、**専用 webhook** が `.env` に追加されている。背景: 既定 #bot通知 チャンネルに埋もれて user が新規ライバル検出に気付けなかった (user 報告 2026-06-08)。

| 項目 | 値 |
|---|---|
| env 変数名 | `DISCORD_RIVAL_WEBHOOK_URL` |
| Discord channel 名 | `eBay Rival` (既定と同一 guild=`1492273038782238881`) |
| 用途 | W153 の **新規ライバル集約通知 / errors alert / truncation 警告** (`task_rival_detection.py` の3送信箇所) |
| 注入先 | `config['discord']['rival_webhook_url']` (`inject_webhook_into_config` で自動注入) |
| 解決 helper | `task_rival_detection._resolve_rival_webhook(config)` = `rival_webhook_url or webhook_url` (専用優先 / 既定 fallback) |
| 送信 | `DiscordNotifier(webhook, bypass_env=True)` で env 既定上書きを回避 (W207 と同一) |
| fallback | env / config 未設定なら `DISCORD_WEBHOOK_URL` (既定 webhook) に自動フォールバック (Q0: 通知先消失防止) |
| ⚠️ 既知の穴 | UI「今すぐ検索」(`run_rival_per_listing_detection_one` 直呼び) は通知を送らない設計 (操作中は画面に inline 表示)。自動通知は 02:30 batch (`run_rival_detection`) のみ |

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
