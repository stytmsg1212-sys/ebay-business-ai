---
title: W125 Codex Reviewer wiring 設計検討 (P3 = 5/29 着手用)
date: 2026-05-15
author: Claude Code (Opus 4.7)
audience: 5/29 W125 着手 session の assistant + user
sources:
  - .company/engineering/docs/2026-05-14-W123-W125_unified_design.md
  - .company/engineering/docs/2026-05-15-github-obsidian-integration-benefits.md
  - feedback_codex_review_usage.md
layer: wiki
updated: 2026-05-15
---

# W125 Codex Reviewer wiring 設計検討

## 1. ゴール

5/29 着手時の **「何を作るか」「どう実装するか」「コスト見積」** を本日 (5/15) 時点で確定しておく. 着手時の議論時間を最小化.

## 2. 構成 (3 経路で fire 可能化)

| 経路 | 起動方法 | 対象 | 頻度 |
|---|---|---|---|
| (i) **slash command** | `/codex-review <path>` を user / assistant が任意発火 | 任意 file | on-demand |
| (ii) **subagent** | `code-reviewer` agent と同型、 `codex-reviewer` を main agent が delegate | 任意 file | on-demand (assistant 判断) |
| (iii) **cron 定期** | scheduler に `daily_codex_lint` task 追加 | 直近 N 日に編集された memory + 業務 KB | 1 日 1 回 (03:00) |

(iv) hook 自動 fire (PostToolUse 等) は当面採用しない (過剰、Plus 枠消費過多).

## 3. ファイル構成

### 新規作成

| Path | 役割 |
|---|---|
| `.claude/agents/codex-reviewer.md` | subagent definition (path 指定で Codex exec を呼ぶ wrapper) |
| `.claude/skills/codex-review/SKILL.md` | slash command 定義 (`/codex-review <path>`) |
| `tools/ebay-manager/tasks/task_daily_codex_lint.py` | cron task (scheduler から呼ばれる) |
| `tools/ebay-manager/monitor/codex_lint_runner.py` | Codex exec 起動 + 結果保存 + Discord 通知の core module |

### 既存編集

| Path | 変更内容 |
|---|---|
| `tools/ebay-manager/daily_scheduler.py` | `daily_codex_lint` task の cron 登録 (03:00) |
| `tools/ebay-manager/monitor/task_execution_log.py` | TASK_SCHEDULE に `('codex_lint', '...', [3], None, 'main')` 追加 |
| `feedback_codex_review_usage.md` | (i)(ii)(iii) の使い分け runbook 追記 |

## 4. cron 設計 (G5)

### 走らせる対象

「**直近 7 日に編集 (frontmatter `updated:` で判定) された memory + .company/ebay-knowledge + 設計書**」.

- 全 file lint は token 過剰、frequency drift → drift 検出に十分.
- 7 日 window で平均 5-15 file/日 と推定.

### lint 内容 (Astro-Han 9 種)

1. index 整合性 (MEMORY.md ↔ 実 file)
2. internal `[[wikilink]]` 検証 (リンク先実在)
3. external URL 切れ (`sources:` の URL に curl)
4. See Also cross-references 補完候補
5. factual contradictions (両論併記なし) — cross-file
6. outdated / superseded claims (`updated:` > 6 ヶ月)
7. missing contradiction annotations (新旧並存箇所)
8. orphan pages (どこからも `[[wikilink]]` されない)
9. stale archive (raw 更新後 wiki 未追随)

### 出力

```
data/codex_lint_log/<YYYY-MM-DD>-lint.jsonl
```

各行 1 finding (file, line, severity, message, suggested_fix).

### Discord 通知 (R-11 適用)

HIGH severity が **3 件以上** で Discord 通知:
- 通知本文に top 3 findings + lint log path
- 通知頻度 cap: 1 日 1 回 (連投防止)

## 5. cascade 検出 (G6)

G5 lint の subset として実装. 規約変更を含む commit を git log で検出 → 同 topic の他 file を grep → 未追従があれば lint 出力に追加.

### 検出 trigger keyword (例)

```python
TRIGGER_KEYWORDS = [
    # 関税系
    "Section 232", "Annex I-A", "Annex I-B", "Annex III",
    "IEEPA", "de minimis", "DDP", "DDU",
    # SKU 系
    "stock", "ebayyh", "ebayme", "ebayPF",
    # 送料系
    "primary_market", "US_only", "mixed_global", "global_only",
    "sample size", "MIN_SAMPLE_SIZE",
    # eBay API 系
    "ShippingServiceCostOverride", "VerifyAdd", "ConditionID",
]
```

各 keyword に対し、`updated` が直近 24h の file に当該 keyword があれば、同 keyword を含む別 file をリストアップ → 「cascade 漏れ候補」として lint 出力に flag.

## 6. コスト見積

### cron 1 回あたり

- 対象 file 数: 10-15 (1 週間の編集)
- 1 file あたり Codex local message: 1
- 合計: **10-15 messages / 日**
- Plus 5 時間枠の Local Messages 上限 = 15-80
- 03:00 cron は他の時間帯と shared window でない (= 03:00 単独実行なら window 占有率低)

### 月間

- 15 messages/日 × 30 日 = 450 messages/月
- Plus 5h 枠を **1 日 1 回しか使わない** 範囲ならクレジット消費少
- 想定コスト: **Plus $20/月 ぴったり、超過なし**

### コスト超過時の安全装置

- 1 cron 実行で対象 file > 30 → 「あまりに多い」と判定して **半分は翌日に持ち越し** (= バッチ分割)
- 5h 枠 hit を `scheduler.log` で観測、3 日連続 hit なら Discord 警告 + cron 一時停止

## 7. 実装順序 (W125 着手日 = 5/29 想定、3 日分)

### Day 1 (5/29 = Phase A)

- `.claude/agents/codex-reviewer.md` 作成 (subagent)
- `tools/ebay-manager/monitor/codex_lint_runner.py` 作成 (core module)
- pytest: dry-run で 1 file lint 成功確認

### Day 2 (5/30 = Phase B + C)

- `.claude/skills/codex-review/SKILL.md` 作成 (slash command)
- `/codex-review <path>` 動作確認
- 2-stage loop (Codex output → Claude 再評価 → user 提示) の verify

### Day 3 (5/31 = Phase D)

- `tools/ebay-manager/tasks/task_daily_codex_lint.py` 作成 (cron)
- `daily_scheduler.py` に登録 + task_execution_log 設定
- 03:00 dry-run 確認 + Discord 通知 verify
- 1 週間 monitor 期間開始

## 8. 想定リスク

| リスク | 影響 | Mitigation |
|---|---|---|
| Codex hallucination | 誤 lint で memory 破壊 | 2-stage loop (5/15 実証済、1/11 件で hallucination 却下) |
| cron 中の Plus 枠 hit | 03:00 業務に支障 | バッチ分割 + Discord 警告 + 自動停止 |
| `updated:` frontmatter 欠落 file が lint 対象から漏れる | sweep 漏れ | 月 1 で「`updated:` 欠落 file」を別 sweep |
| cron が pythonw 起動失敗 (W126 で経験) | silent skip | Phase 3 で scheduler 起動 verify 必須、健康チェック cron で検知 |
| Discord 通知 spam | user 鈍化 | HIGH 3 件以上 + 1 日 1 回 cap |

## 9. 着手前のチェックリスト (5/29 朝)

- [ ] Plus アカウント active (月課金確認)
- [ ] `codex login status` = "Logged in using ChatGPT"
- [ ] `feedback_codex_review_usage.md` 最新の使い分け runbook 反映済
- [ ] scheduler 健康 (W126 Phase 3 monitor 完了済)
- [ ] 本設計書を Phase A 着手前に 5 分 read で再確認

## 10. 着手しない判断基準 (5/29 時点で no-go なら別 W に移行)

- ChatGPT Plus 課金体系が再変更 (4/2 のような大改訂) で Plus 課金不可能
- W126 Phase 3 verification 失敗 → scheduler 信頼性問題 (cron 追加できない)
- 5/22-5/28 で Codex hallucination 率が >20% に増えてた (= 信頼性低下)

## 関連

- [[2026-05-14-W123-W125_unified_design]] (親設計、W125 §4 Week 3 と整合)
- [[2026-05-15-github-obsidian-integration-benefits]] (上位コンテキスト)
- `feedback_codex_review_usage.md` (起動規約)
