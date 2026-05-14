# USER_MANUAL.md — user 実施手順 集約

本ファイルは **user (人間) が手で実行する手順** をまとめた reference。assistant が自律的にやる作業は対象外 (それらは CLAUDE.md / .claude/rules/ 配下)。新しい手順が発生したら本ファイルに追記する運用 (assistant が手順を新規定義する時は同時に本ファイルを update)。

最終更新: 2026-05-02 (W94 Phase 7 切替時に scheduler restart 手順を追加し manual を新設)

---

## 目次

1. [scheduler 操作](#1-scheduler-操作)
2. [Phase 7 (W94 batch shadow run) 監視・緊急停止](#2-phase-7-w94-batch-shadow-run-監視緊急停止)
3. [task kill switch (一時停止 / 再開)](#3-task-kill-switch-一時停止--再開)
4. [メンテナンス手順](#4-メンテナンス手順)
5. [session 運用 (Claude Code 側)](#5-session-運用-claude-code-側)
6. [出品 / 業務操作](#6-出品--業務操作)
7. [トラブル時の対処](#7-トラブル時の対処)
8. [slash command 早見表](#8-slash-command-早見表)

---

## 1. scheduler 操作

### 1-1. 状態確認

PowerShell:

```powershell
# 現在の scheduler PID 確認 (python.exe で daily_scheduler.py を実行している process)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { "{0,7} | {1}" -f $_.ProcessId, $_.CommandLine } | Where-Object { $_ -match 'daily_scheduler' }

# scheduler.log の最新 mtime / tail
Get-Item C:\Users\gucch\projects\claude\tools\ebay-manager\logs\scheduler.log | Select-Object LastWriteTime
Get-Content C:\Users\gucch\projects\claude\tools\ebay-manager\logs\scheduler.log -Tail 20 -Encoding UTF8
```

`order_alert_check` が **30 分毎** に走るので、scheduler.log は常に **35 分以内に更新されている** はず。35 分超えたら scheduler stuck or dead 推定。

### 1-2. 手動再起動 (config 変更を反映させる時)

`schedule_config.json` を編集しても、scheduler は **起動時に config を snapshot して保持** しているため即時反映されない (例外: `customs_automation.send_enabled` のみ per-call read で hot-reload OK)。flag 切替時は再起動必須。

```powershell
# (1) 現在 PID を取得
$old = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'daily_scheduler' }).ProcessId

# (2) 停止
Stop-Process -Id $old -Force

# (3) 起動 (UTF-8 環境変数を設定して文字化け防止)
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
Start-Process -FilePath python -ArgumentList 'daily_scheduler.py' `
    -WorkingDirectory 'C:\Users\gucch\projects\claude\tools\ebay-manager' `
    -WindowStyle Hidden -PassThru

# (4) 5 秒後に scheduler.log に "スケジューラーが起動しました" が出ているか確認
Start-Sleep -Seconds 5
Get-Content C:\Users\gucch\projects\claude\tools\ebay-manager\logs\scheduler.log -Tail 5 -Encoding UTF8
```

assistant が PID を把握している時は assistant に依頼してもよい (今回 W94 Phase 7 切替時は assistant が実施)。

### 1-3. watchdog (自動再起動の仕組み)

- ファイル: `tools/ebay-manager/scripts/scheduler_watchdog.ps1`
- 起動: Windows Task Scheduler が **5 分間隔** で実行
- 健康判定: `scheduler.log` の mtime が **35 分超** 経過していたら scheduler down と判定 → `python daily_scheduler.py` を起動 + Discord 通知

確認:

```powershell
# watchdog の最新 log
Get-Content C:\Users\gucch\projects\claude\tools\ebay-manager\logs\watchdog.log -Tail 10 -Encoding UTF8

# 想定される行: "OK log_age=Ns" (健全) / "ALERT scheduler_down attempting_restart" (再起動発動)
```

watchdog 自体が動いているかは `Task Scheduler (taskschd.msc)` で確認 (タスク名検索: `*scheduler*` or `*watchdog*`)。

---

## 2. Phase 7 (W94 batch shadow run) 監視・緊急停止

### 2-1. 進行状況の監視 (毎日 1 回 / 12:00 JST 推奨)

毎朝 02:30 JST の supplier_sweep batch 後に以下を確認:

```python
# C:/Users/gucch/projects/claude/tools/ebay-manager/data/monitor.db
import sqlite3
db = r'C:/Users/gucch/projects/claude/tools/ebay-manager/data/monitor.db'
conn = sqlite3.connect(db); cur = conn.cursor()

# DLQ 累計 (★ 5 件超で緊急停止)
print('DLQ:', cur.execute('SELECT COUNT(*) FROM supplier_eval_pending').fetchone())

# 直近 24h batch 経路の cache hit / cost
cur.execute("""SELECT operation, COUNT(*) calls,
    SUM(input_tokens) in_t, SUM(cache_read_tokens) cache_r,
    ROUND(100.0*SUM(cache_read_tokens)/NULLIF(SUM(input_tokens),0),1) cache_pct,
    ROUND(SUM(cost_usd),2) cost
  FROM api_call_log
  WHERE called_at >= datetime('now','-24 hours')
    AND operation IN ('candidate_evaluate','candidate_evaluate_batch')
  GROUP BY operation""")
for r in cur.fetchall(): print(r)

# supplier_sweep の最新ステータス
cur.execute("""SELECT started_at, status, success, substr(message,1,150)
  FROM task_execution_log
  WHERE task_key='supplier_sweep'
  ORDER BY started_at DESC LIMIT 3""")
for r in cur.fetchall(): print(r)
```

### 2-2. 緊急停止 (DLQ 累計 5 件超 or 異常)

```powershell
# (1) flag を false に戻す
notepad C:\Users\gucch\projects\claude\tools\ebay-manager\config\schedule_config.json
# tasks_enabled.supplier_sweep.use_batch_api を true → false に書換、保存

# (2) scheduler 再起動 (1-2 と同じ手順)
$old = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'daily_scheduler' }).ProcessId
Stop-Process -Id $old -Force
$env:PYTHONUNBUFFERED='1'; $env:PYTHONIOENCODING='utf-8'
Start-Process python daily_scheduler.py -WorkingDirectory 'C:\Users\gucch\projects\claude\tools\ebay-manager' -WindowStyle Hidden -PassThru

# (3) 即時に assistant に「Phase 7 緊急停止しました、原因調査ヒアリング希望」と報告
```

### 2-3. Phase 7 完走後 (3 日経過、2026-05-05 夜以降)

assistant に「Phase 7 結果評価して Phase 8 着手判断ください」と依頼。assistant は G6-G10 で評価し、本番切替 (= flag 維持で運用継続) or 中止 (= flag false に戻す) を提案する。

---

## 3. task kill switch (一時停止 / 再開)

scheduler が走らせている **どの task でも個別に停止・再開できる**。

### 3-1. task を一時停止する

```powershell
notepad C:\Users\gucch\projects\claude\tools\ebay-manager\config\schedule_config.json
# tasks_enabled.<task_name>.enabled を true → false に書換
# scheduler 再起動 (1-2 参照) で反映
```

主な kill 対象 task (例):

| task_key | 役割 | 停止すべき場面 |
|---|---|---|
| `supplier_sweep` | 朝 02:30 仕入先候補スイープ | API 不調時 / Phase 7 緊急停止 |
| `daily_relist` | 毎朝 7 件の relist SEO ブースト | eBay 側に異常 / VeRO 通報 |
| `inventory_check` | Selenium で仕入先在庫チェック | Mercari/Yahoo 仕様変更時 |
| `supplier_select` | 長期 OOS の仕入先選出 | supplier_sweep 修理中 |
| `email_pickup` | Gmail からの売上通知ピックアップ | Gmail OAuth 期限切れ |
| `customs_check` | 通関要求メール検知 → ドラフト | FedEx/DHL 仕様変更時 |
| `video_learning_queue` | 動画学習 Gemini 投入 | 無料枠切れ時 (CronCreate 自動再開ある場合不要) |

### 3-2. customs_automation の hot-reload kill switch (例外、再起動不要)

`config.customs_automation.send_enabled = false` だけ **scheduler 再起動なしで反映** される (`customs_gmail_sender.py` が per-call で config 読み直し)。緊急で通関ドラフトの送信を止めたい時に便利。

```powershell
notepad C:\Users\gucch\projects\claude\tools\ebay-manager\config\schedule_config.json
# customs_automation.send_enabled を true → false に書換、保存
# 再起動不要、次の send 試行から即停止
```

---

## 4. メンテナンス手順

### 4-1. Gmail OAuth 再認可

`token.json` の refresh が長期で失敗した場合 / scope 追加した時に必要。

```powershell
cd C:\Users\gucch\projects\claude\tools\ebay-manager
python -m scripts.gmail_reauth
# → ブラウザが開いて Google アカウント認可 → token.json が更新される
```

2026-04-22 以降 OAuth 本番公開済 = refresh_token 無期限化されている。週次再認可は不要。

### 4-2. eBay OAuth (User Token / App Token)

User Token は `.env` の `EBAY_USER_TOKEN` に設定済 = 18 か月有効。期限近づいたら eBay Developer Portal で再生成 → `.env` 更新。

### 4-3. Photoroom API key 入力

W10 (画像加工) 機能のセットアップ時に 1 回。MonoDeck の「設定」タブ → Photoroom API key 欄に入力 → secure_store 経由で保存。

### 4-4. Discord webhook 確認

`schedule_config.json` の `discord.webhook_url` に設定済。差し替え時は同フィールドを編集。

### 4-5. DB バックアップ確認

`tools/ebay-manager/data/backups/` 配下に自動 backup が作られる (`schedule_config.json:database.backup_enabled=true`)。週次で「ファイルが増えているか」を一目見る程度で良い。

### 4-6. PC sleep 防止 (silent skip 予防)

2026-04-30 の事故 (PC sleep で scheduler 24h 消失) 以降:

- Windows 設定 → 「電源とスリープ」 → **画面オフ・スリープともに「なし」**
- バッテリー駆動時も同様 (ノート PC の場合)

これで scheduler が消失するリスクは去ったが、念のため watchdog も併存。

---

## 5. session 運用 (Claude Code 側)

### 5-1. /clear / /compact の判断

| 状況 | 行うこと |
|---|---|
| 1 機能 (W 番号) クローズ後、話題切替 | `/clear` |
| 同一機能の長大セッションで過去ログ詳細不要 | `/compact` (事前に session_*.md 総括 必須) |
| MEMORY.md staleness 警告が出ている | 一次情報照合 → 必要なら `/clear` |

### 5-2. /session-close

セッション終了時に呼ぶ:

```
/session-close
または「ここで一旦 Claude Code を閉じます」「セッション終了」「/close」「/bye」
```

assistant が自動で session_*.md (永続記録) + MEMORY.md update + _NEXT_SESSION.md (次回 SessionStart hook 自動 inject) を作成。**user 操作 0**。

### 5-3. /session-resume (manual fallback)

通常は SessionStart hook が `_NEXT_SESSION.md` を自動 inject するため不要。ただし以下の時のみ手動発動:

- hook 動作不良で「auto-load されてない」状態
- `[STALE WARNING]` prefix 観測時
- 詳細 review が欲しい時

### 5-4. /add_s (新機能 ROADMAP 登録)

口頭で新機能アイデアを言ったら assistant は自動で `data/system_improvements.json` に W 番号付きで登録するはずだが、明示的に呼ぶ場合:

```
/add_s 在庫切れ商品の自動非表示
```

### 5-5. /feature-dev (新機能 / 不確実な変更)

新機能 / 外部 API 連携 / スクレイプ構造追随 / 見積不確実な変更は **必ず** `/feature-dev` 発動 (Q3)。Phase 3 Clarify 省略禁止。

### 5-6. /fewer-permission-prompts

permission prompt が頻発する時に呼ぶ。直近 transcript を分析して allowlist を `.claude/settings.json` に追加。新 MCP 導入後にも提案あり。

### 5-7. /loop / /schedule

- `/loop 5m /foo` — `/foo` を 5 分毎に実行
- `/schedule` — cron スケジュールで remote agent 起動 (例: 「毎週月曜に PR triage」)

---

## 6. 出品 / 業務操作

### 6-1. /mono (MonoDeck 起動)

MonoDeck (eBay Manager の Streamlit UI) を起動:

```
/mono
```

既に起動していれば URL を返すだけ。停止する時は MonoDeck の「停止」ボタン or 起動した PowerShell window を Ctrl+C。

### 6-2. /listing (出品文作成)

商品名と状態を渡すと assistant が eBay 出品文 (Title / Item Specifics / HTML description) を英語で生成:

```
/listing
商品名: Audio-Technica ATH-CKS330NC
状態: 新品同様 (S)
```

または日本語商品名 + 状態の自由文でも triggered。

### 6-3. 通関ドラフトの送信

assistant が `.company/daily-operations/fedex-drafts/YYYY-MM-DD-TRK_xxx.md` にドラフト保存 → user が MonoDeck UI 「通関対応」タブで内容確認 → 「送信」ボタン押下で Gmail API 経由で送信。

緊急停止: `customs_automation.send_enabled = false` (本ファイル 3-2 参照、scheduler 再起動不要)。

### 6-4. eBay GetItem 実反映確認 (Q1 DoD 一部)

出品 / 価格 / 送料 / 文言変更後は **必ず eBay GetItem API で実反映確認**。MonoDeck の「listing 詳細」タブで「再取得」ボタン → 直近 GetItem 結果を表示。

### 6-5. 個別出品

MonoDeck の「個別出品」タブから商品 1 件ずつ出品。Claude が rank / weight / condition description を自動推定。User は最終 review して「出品」ボタン押下。

---

## 7. トラブル時の対処

### 7-1. scheduler が動いていない疑い

```powershell
# 1. PID 存在確認
(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'daily_scheduler' }).ProcessId
# 何も返らない = scheduler 死亡

# 2. scheduler.log mtime 確認
Get-Item C:\Users\gucch\projects\claude\tools\ebay-manager\logs\scheduler.log | Select-Object LastWriteTime
# 35 分以上前 = scheduler stuck

# 3. 手動再起動 (本ファイル 1-2)
```

### 7-2. watchdog が暴走している疑い

```powershell
Get-Content C:\Users\gucch\projects\claude\tools\ebay-manager\logs\watchdog.log -Tail 30 -Encoding UTF8
# "RESTART_OK" が連続 = scheduler が起動しても 35 分以内に止まっている = scheduler 側 bug
# "concurrent_run_skip" 連発 = lock file が残ったまま = .watchdog_lock を手動削除
```

`.watchdog_lock` 残存対処:

```powershell
Remove-Item C:\Users\gucch\projects\claude\tools\ebay-manager\.watchdog_lock -ErrorAction SilentlyContinue
```

### 7-3. Discord 通知が来ていない

- `schedule_config.json:discord.enabled` が `true` か確認
- `discord.webhook_url` が有効か確認 (Discord 側で webhook 削除されていないか)
- scheduler.log で `notifiers.discord_notifier - WARNING` を grep

### 7-4. DB に異常な数値 (急減 / 急増) が出た時

直接 UPDATE / DELETE は **絶対しない** (Q2 / `db-migration-rules.md`)。assistant に状況を伝えて one-shot script を作ってもらう。`tasks_enabled.<task>.enabled = false` で関連 task を一時停止して状況再現を保全。

### 7-5. メールが届きすぎる / 届かない

`tasks_enabled.email_pickup.enabled` を `false` で即停止。原因調査後再開。

### 7-6. eBay 出品が VeRO 通報された

該当 listing を即取り下げ → `data/vero_brands.json` にブランド追加 → assistant に「VeRO 通報あり」を共有。

---

## 8. slash command 早見表

skill 系 (assistant が紹介してくれるもの含む):

| command | 用途 |
|---|---|
| `/mono` | MonoDeck (Streamlit UI) 起動 |
| `/listing` | eBay 出品文生成 |
| `/add_s <機能>` | ROADMAP に新機能登録 |
| `/feature-dev` | 新機能設計の体系プロセス起動 (Q3 必須) |
| `/session-close` (`/close`, `/bye`) | セッション終了 + 永続化 |
| `/session-resume` | 復帰 (manual fallback、通常不要) |
| `/clear` | 文脈クリア (機能切替時) |
| `/compact` | 文脈圧縮 (長大セッション中) |
| `/loop <interval> <cmd>` | recurring task |
| `/schedule` | cron で remote agent |
| `/fewer-permission-prompts` | permission allowlist 自動追加 |
| `/company` | 仮想組織 (秘書 + 各部署) ルーティン |
| `/help` | Claude Code ヘルプ |

assistant が「これも使えますか?」と提案するもの: `/ultrareview` (PR multi-agent review、有料) / `/security-review` / `/init` 等。

---

## 9. 本 manual の運用

- **更新タイミング**: 新しい user 手順が発生したら、assistant は実装と同時に本ファイルに追記する (例: 新 kill switch 追加 / 新メンテ procedure 追加)
- **スコープ**: user (人間) が手で実行する手順のみ。assistant 自律作業 / コード規約は CLAUDE.md / `.claude/rules/` 配下を参照
- **言語**: 日本語 (user の作業言語に合わせる)
- **重複 OK**: CLAUDE.md / memory に同じ手順があっても、ここから引くのが user 視点で最短。重複 = 故障耐性

---

## 付録: 関連ファイル早見

| ファイル | 役割 |
|---|---|
| `CLAUDE.md` (project root) | プロジェクト規約・絶対ルール (assistant 向け) |
| `tools/ebay-manager/CLAUDE.md` | eBay 規制業務 rules (subdir auto-load) |
| `tools/ebay-manager/config/schedule_config.json` | scheduler 設定 (本 manual の操作対象) |
| `tools/ebay-manager/logs/scheduler.log` | scheduler 動作ログ |
| `tools/ebay-manager/logs/watchdog.log` | watchdog 動作ログ |
| `tools/ebay-manager/data/monitor.db` | 業務 DB (SQLite) |
| `.claude/rules/*.md` | 横断 rule (Karpathy / DB migration / silent-skip / SKU / sqlite-timezone 等) |
| `~/.claude/projects/.../memory/` | session 履歴 / feedback / project memory |
