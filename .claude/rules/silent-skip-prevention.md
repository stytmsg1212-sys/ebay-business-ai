---
name: silent-skip-prevention
description: Q0 絶対禁止 - サイレントスキップ / 偽装成功 / 逃避修正。2026-04-25 daily_relist 5 日間欠落事故 (35 件 SEO ブースト機会損失) からの恒久ルール
type: rule
---

# Q0 絶対禁止 — サイレントスキップ / 偽装成功 / 逃避修正 (常時適用)

出典: 2026-04-25 `daily_relist` が 5 日間 (4/21〜25) サイレントスキップ → 35 件 SEO ブースト機会損失。**ログにも UI にも痕跡なし、5 日間気付けず**。

## 3 つの禁止パターン

### 1. サイレントスキップ (silent skip)

処理が skip されたのに **どこにも記録されない** 状態を作らない。

- ❌ `if not condition: return` で何も log せず脱出
- ❌ `try/except: pass` で例外を握りつぶし
- ❌ `should_task_run` 系で False を返す時に skip 理由が DB / log に残らない
- ✅ 必ず `log_task_skip` / `logger.warning` / Discord 通知のいずれかで痕跡を残す

### 2. 偽装成功 (fake success)

処理が失敗したのに `success: True` / `status='completed'` と返さない。

- ❌ 例外パスで `result['success'] = True` を書き込む
- ❌ try/except で空の結果を「成功」として返す
- ❌ retry 全失敗なのに最後だけ「completed」と log する
- ✅ 失敗は `success: False` + error_message + Discord 通知

### 3. 逃避修正 (avoidance refactor)

困難なバグを **回避 (skip / disable / 例外吸収)** で「修正完了」に見せかけない。

- ❌ テストが落ちるから skip マーク
- ❌ 機能が動かないから feature flag OFF で「対応済」報告
- ❌ 例外頻発から try/except でラップして「修正完了」報告
- ❌ scheduled task の skip 条件を緩めて「実行された」ように見せる
- ✅ **必ず根本原因を調査・修正**。やむを得ず無効化する場合は **明示的に user 許可を求める**

## コード書く時の自問 (3 項目)

1. このタスクが skip された時、user / 開発者は **どこで知ることができるか?** (`scheduler.log` grep だけは不十分、Discord or DB log or UI 表示が必須)
2. 失敗時に `success: True` を返す経路はないか?
3. 修正案が「失敗ケースを回避する」なら、それは **本当に修正か、逃避か?**

## 既存防御層 (2026-04-25 実装済、4 層)

1. **`task_execution_log` v20** — 全タスクの started/completed/failed/skip_* を必須記録
2. **`health_alert_log` v20** + `claim_alert_dedupe` — 期待タスクが本日未完了なら Discord 即通知 (04/12/16/19/23 時)
3. **MonoDeck「定時実行」タブ** — 欠落 / 失敗 / 実行中を可視化
4. **`should_task_run` を try/except でラップ** — 例外時も `skip_other` で必ず記録
5. **`_batch_ctx["hour"] = scheduled_hour`** — hour ドリフト時もタスクが正しく実行される構造的予防

## 新規 scheduled task の必須要件

1. **`task_key` 必須**: `daily_scheduler.execute_daily_tasks` で `run_task(..., task_key='xxx')` 形式
2. **`TASK_SCHEDULE` 登録**: `monitor/task_execution_log.TASK_SCHEDULE` に `(key, display, hours, weekdays, owner)` で登録
3. **`scheduled_hour` 引き渡し**: `setup_scheduler` の `add_job(args=[config, hour])` 第 2 引数。`datetime.now().hour` 直接参照禁止
4. **`max_instances=1` 維持**: BackgroundScheduler から外すと parallel batch で `_batch_ctx` race condition

## 完了報告

**誘導表現禁止** (例: 「W22 対象が 30 件に増加」だけで使用モデル不明示 = 暗黙の偽装成功)。詳細は CLAUDE.md Q5 (完了報告 4 行テンプレ) / Q6 (モデル選定)。

## 違反検出 (運用時)

- `task_execution_log` で「started しか無い (finished が無い)」レコードを定期 audit
- Discord 通知が一定期間来ていない = 健康チェック自体が動いていない可能性
- 重要 task (daily_relist / inventory_check 等) の **最終成功時刻** を MonoDeck で常時可視化

## hook 強制 (実装状況)

- **物理 BLOCK** (PreToolUse `quality-gate.sh`): `except: pass` / `except Exception: pass` のみ
- **警告検出** (PostToolUse): 例外パスで `success: True` 返却は **block されない**、人間 review 必須

## 違反時の自己ペナルティ

assistant が将来このルールに違反したら:
1. **即座に正直に user 報告** (隠さない)
2. `feedback_silent_skip_prevention.md` の事例として追記
3. 再発防止のための memory / hook / test を **その場で作る**
