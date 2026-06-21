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

### 4. 負の能力主張による逃避 handoff (negative-capability handoff) ★2026-06-21 制定

**単一手段の失敗を「タスク不能」に昇格させ、未完了タスクを user に転嫁しない。**

出典: 2026-06-21 eBaymag「送料無料」チェック解除で、`page.evaluate` の JS 合成クリック (isTrusted=false、React controlled component が無視) **1 手段だけ**試して「CDP では外せない=user 手動が必要」と確定・作業停止 → 実際は Playwright **ネイティブ実入力** (`locator.uncheck()`) で普通に動いた。2026-06-09「API 無し=UI 操作不能」と完全同型の再発。**method failure ≠ goal failure** を取り違えた。これは「困難を回避で逃げる」= 本ルール 3 の handoff 版。

#### 禁止出力語ゲート (機械的トリガー)

以下の語を**未完了タスクについて**出力する直前は、必ず後述の **Failure Evidence Block を emit** すること (出さずに handoff = Q0 違反):

> 「できない / 不可 / 無理 / 自動操作できない / user 手動が必要 / 手動でお願いします / Playwright/CDP/ブラウザでは〜できない / blocked / cannot / impossible / take over / manual intervention」

#### Failure Evidence Block (3 必須)

1. **候補手段の列挙**: その goal を達成しうる手段を全部挙げる (例: ブラウザ操作なら native locator method / role-based locator / CDP input / page.evaluate の優先順)
2. **最強の未試行手段を実際にテストした証拠**: 「試した手段名 + 実行結果ログ」。**1 手段の失敗だけで確定しない**
3. **真に不能な根拠**: なぜ全候補が不能か (検証済の事実。推測で「無理そう」は不可)

#### 除外 (Failure Evidence Block 不要 = 即 handoff してよい)

- credentials / permission の明確な欠落
- user が既に停止を指示済
- 法的 / 倫理的に不許可
- 必要な外部システムが**直接検証済で**利用不能
- **確定済の user-only 作業** (eBay/銀行ログイン壁、2FA、user 専権の承認ボタン等。2026-06-09 のログイン壁判断は正当だった)

#### Codex 相談則 (user 2026-06-21 指示)

上記ゲートに該当し (= 未完了タスクを能力不足で handoff しようとし)、かつ除外に当たらない時は、**自分一人の判断で止めず Codex に相談してから結論する**。`codex-reviewer` agent (model:opus override) に「root cause + 候補手段 + なぜ不能か」を渡し、外部視点で詰め残しを潰す。terminal handoff 直前のみ発火 (通常の debug/retry/事実確認では呼ばない = Codex usage 枠を浪費しない)。

技術詳細 (ブラウザ UI のネイティブ実入力 vs 合成クリック) は on-demand snippet `.claude/rule-snippets/browser-ui-native-input.md` 参照。

## コード書く時の自問 (3 項目)

1. このタスクが skip された時、user / 開発者は **どこで知ることができるか?** (`scheduler.log` grep だけは不十分、Discord or DB log or UI 表示が必須)
2. 失敗時に `success: True` を返す経路はないか?
3. 修正案が「失敗ケースを回避する」なら、それは **本当に修正か、逃避か?**

## 既存防御層 (2026-04-25 実装済、4 層)

1. **`task_execution_log` v20** — 全タスクの started/completed/failed/skip_* を必須記録
2. **`health_alert_log` v20** + `claim_alert_dedupe` — 期待タスクが本日未完了なら Discord 即通知 (04/12/16/19/23 時)
3. **MonoDeck「定時実行」タブ** — 欠落 / 失敗 / 実行中を可視化
4. **`should_task_run` を try/except でラップ** — 例外時も `skip_other` で必ず記録
5. **`_batch_ctx` thread-local 化** — hour ドリフト + thread 跨ぎ clobber の構造的予防

### ⚠️ 5 の改訂 (2026-05-18 silent skip 再発の根治、md-files-can-be-wrong 自規約適用)

**過去の見解 (〜2026-05-18)**: `_batch_ctx["hour"] = scheduled_hour` を batch 開始時に
1 回 set し、`_run_isolated_task` が save/restore すれば hour ドリフトを構造的に防げる。

**現状の見解 (2026-05-18〜)**: 上記は **同一 thread でしか正しくない**。APScheduler は
各 job を別 worker thread で並行実行する。`_batch_ctx` が **module 共有 global dict**
だと、長時間 02:30 batch (ebay_sync+inventory_check+supplier_sweep で ~03:21) の
最中に 03:00 `daily_codex_lint` 等 isolated task が**別 thread**で hour を 3 に上書きし、
まだ走行中の 02:30 batch が daily_relist 評価時に clobbered hour=3 を読み
`batch_hour=3 not in execution_times=[2]` で silent skip する。save/restore は
thread 跨ぎで非 composable のため無効。

**契機**: 2026-05-15 `daily_codex_lint` (03:00 cron) 追加 → 5/16 から daily_relist /
enrich_listings_physical / estimate_weights_claude / cleanup_old_relisted /
research_morning_brief が**毎日 silent skip** (DB task_execution_log で実証: 5/15
まで `batch_hour=2 completed`、5/16-17 全て `skip_time batch_hour=3`)。2026-04-25 /
05-05 事故の第 3 次再発。

**根治**: `daily_scheduler._ThreadLocalBatchCtx` (内部 `threading.local()`、dict 互換)
で各 job の batch context を完全分離。`should_task_run` は `execute_daily_tasks`
内からのみ呼ばれ、同 thread 冒頭で hour set 済 = 並行 isolated task の影響を受けない。
**教訓**: 「単一プロセス・単一 batch 直列実行が前提」というコメントを書いた共有 mutable
global は、並行 scheduler 下で必ず thread 安全性を検証する (max_instances=1 は同一
job の再入のみ防ぎ、別 job 間の global 共有は防がない)。

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
