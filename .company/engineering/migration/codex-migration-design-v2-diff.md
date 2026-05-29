---
title: Codex 移行設計書 v2 (公式 docs ベース訂正)
author: Claude Opus 4.7
generated: 2026-05-27
sources:
  - https://developers.openai.com/codex/config-basic
  - https://developers.openai.com/codex/hooks
  - https://developers.openai.com/codex/skills
  - https://developers.openai.com/codex/subagents
  - https://developers.openai.com/codex/memories
  - https://developers.openai.com/codex/rules
  - https://developers.openai.com/codex/cli/reference
parent: codex-migration-design-claude.md (v1)
purpose: v1 で Codex 本人ヒアリング経由の情報を、公式 docs 直接 fetch で訂正
---

# 0. 訂正サマリ (5 件 の誤り訂正 + 3 件 の追加発見)

| # | v1 → v2 | 影響度 |
|---|---|---|
| **訂正 1** | `PermissionRequest` hook は **実在** (v1 で「完全消失」と誤記) | 🔴 重大 — ntfy.sh push の代替手段が確保された |
| **訂正 2** | `Stop` hook は **実在** | 🔴 重大 — `~/.claude/hooks.sh` の Stop 部分もそのまま移植可 |
| **訂正 3** | hook bash の `exit 2 + stderr` 方式は **Codex でも最小互換動作** | 🟡 中 — 既存 hook 11 個の **書換は不要** (現状のまま動く)、JSON 化は optional |
| **訂正 4** | Skills の公式 path は **`.agents/skills/`** (`.codex/skills/` ではない) | 🟡 中 — Subagent の `.codex/agents/` とは別 namespace |
| **訂正 5** | Codex Rules は **Starlark 実行権限制御**、`.claude/rules/` 相当ではない | 🟢 低 — 既に v1 で AGENTS.md 集約を本筋にしていたので影響軽微 |
| **発見 1** | `PreCompact` / `PostCompact` hook が Codex に**新規存在** | 🟢 移行後の +α 機能 |
| **発見 2** | `SubagentStart` / `SubagentStop` hook が Codex に**新規存在** | 🟢 移行後の +α 機能 |
| **発見 3** | Codex の `model` フィールドで Claude モデル指定の対応可否は **公式 docs に明記なし** | 🔴 重大 — Phase 0 で必ず実機検証 (Opus 4.7 が使えなければ code-reviewer / research-brain / code-architect / ebay-listing の品質低下確定) |

---

# 1. 訂正詳細

## 訂正 1-2: PermissionRequest / Stop hook は実在

### v1 の記述 (誤り)

```
1.1 PermissionRequest hook → 完全消失
1.2 Stop hook → 完全消失
1.3 agentPushNotifEnabled push 通知 → 代替必要 (Discord webhook 自前)
1.4 ntfy.sh push → 同上
```

### v2 (公式 docs ベース)

Codex は以下 **10 個の hook event** を全てサポート:

| カテゴリ | event | Claude Code 対応 |
|---|---|---|
| ターン単位 | `PreToolUse` | ✅ あり |
| ターン単位 | `PermissionRequest` | ✅ あり |
| ターン単位 | `PostToolUse` | ✅ あり |
| ターン単位 | **`PreCompact`** | ⭐ Claude Code に無し |
| ターン単位 | **`PostCompact`** | ⭐ Claude Code に無し |
| ターン単位 | `UserPromptSubmit` | ✅ あり |
| ターン単位 | **`SubagentStop`** | ⭐ Claude Code に無し |
| ターン単位 | `Stop` | ✅ あり |
| セッション単位 | `SessionStart` | ✅ あり |
| セッション単位 | **`SubagentStart`** | ⭐ Claude Code に無し |

つまり **PermissionRequest / Stop hook が実在するので、ntfy.sh push 機構 (~/.claude/hooks.sh) もそのまま移植可能**。

ただし `agentPushNotifEnabled` (Anthropic Claude mobile app への push) は Anthropic 専用機構なので消失確定。**Discord webhook 自前実装は不要**、ntfy.sh の topic 購読をそのまま使うか、Codex app の Automations に切替か、選択肢が増える。

## 訂正 3: hook 応答方式は `exit 2 + stderr` で OK

### v1 の記述 (過剰書換要求)

```
hook bash 11 個 → `exit 2` を JSON `{"permissionDecision":"deny"}` に書換必須
```

### v2 (公式 docs ベース)

公式 docs に **3 つの応答方式**が明記:

```bash
# 方法 A (推奨): JSON 応答
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "理由"
  }
}

# 方法 B: 従来形式
{"decision": "block", "reason": "ブロック理由"}

# 方法 C (最小限): exit 2 + stderr ← Claude Code 互換
echo "ブロック理由" >&2
exit 2
```

→ **Claude Code 既存 hook 11 個 + ~/.claude/hooks.sh は応答書換不要、そのまま動く**。
→ Phase 1 工数の大幅削減 (1 日 → 0.5 日)。

ただし、Codex 固有の `additionalContext` を活用したい場合 (例: `userprompt-rule-router.sh` の rule snippet 注入) は JSON 形式に書換すると恩恵あり。最終的に下記が推奨:

| Hook | 推奨応答 |
|---|---|
| `check-secrets.sh` / `quality-gate.sh` / `db-preflight.sh` / `db-write-confirm.sh` / `claude-md-discipline.sh` | `exit 2 + stderr` (現状維持) |
| `userprompt-rule-router.sh` (rule snippet JIT 注入) | JSON `{"hookSpecificOutput":{"additionalContext":"..."}}` (現状の Claude Code 形式と同じ) |
| `session-start-recent.sh` / `session-start-load-incantation.sh` | JSON `{"hookSpecificOutput":{"additionalContext":"..."}}` (現状維持) |
| `~/.claude/hooks.sh` の PermissionRequest / Stop part | `exit 0` (現状維持、通知のみ) |

## 訂正 4: Skills の公式 path は `.agents/skills/`

### v1 の記述 (混在)

```
.codex/skills/ または .agents/skills/ (どちらか)
```

### v2 (公式 docs ベース)

公式 path は **`.agents/skills/<name>/SKILL.md`** に統一。階層:

| スコープ | path |
|---|---|
| `$CWD/.agents/skills` | 現在のディレクトリ固有 |
| `$REPO_ROOT/.agents/skills` | リポジトリ共有 |
| `$HOME/.agents/skills` | ユーザー個人 |
| `/etc/codex/skills` | システム |

→ Subagents (`.codex/agents/*.toml`) と Skills (`.agents/skills/*/SKILL.md`) は **別 namespace** に注意。

## 訂正 5: Codex Rules は別物

### v1 の記述 (誤解)

```
.claude/rules/*.md (constitution) → .codex/rules/*.md に移行
```

### v2 (公式 docs ベース)

Codex Rules は **Starlark 言語による実行権限制御**:

```starlark
# ~/.codex/rules/default.rules
prefix_rule(
    pattern = ["gh", "pr", "view"],
    decision = "prompt",
    justification = "PR 閲覧は承認後に許可",
)
```

これは Claude Code の `.claude/rules/*.md` (Karpathy 4 / Q0-Q7 等の常時 instructions) **とは全く別の機能**。Codex Rules はサンドボックス外コマンド実行の細粒度制御。

→ `.claude/rules/*.md` 8 個 (00-constitution / karpathy-principles / silent-skip-prevention / db-migration-rules / sku-rules / md-files-can-be-wrong / sqlite-timezone / cascade-update) の移行先は:
1. **AGENTS.md に集約** (project root / ~/.codex/AGENTS.md)
2. または **Skills (`.agents/skills/<rule>/SKILL.md`)** として description-triggered で再構築

Codex Rules は **新規利用** (例: `sqlite3 ... DROP` を物理 block する Starlark rule) として活用可。

---

# 2. 新発見 (v1 に無い活用機会)

## 発見 1-2: PreCompact / PostCompact / SubagentStart/Stop hook

Claude Code に無い 4 個の hook が Codex に存在。本 project への適用例:

| 新 hook | 適用案 |
|---|---|
| `PreCompact` | 会話自動圧縮前に session_*.md へ強制 dump (現状の session-close skill より自動的) |
| `PostCompact` | 圧縮後 context size を実測し、threshold 超なら警告 (現 `clear-discipline.sh` の強化) |
| `SubagentStart` | code-reviewer / research-brain 起動時刻を `task_execution_log` に記録 (Q4 verify 強化) |
| `SubagentStop` | subagent 完了時の HIGH=0 ループ進捗を自動 audit |

これらは Phase 1 で **新規実装** すれば、移行後 Claude Code 時代より監視粒度が向上。

## 発見 3: Codex `model` フィールドで Claude モデル指定の可否は公式 docs に明記なし

これが **Phase 0 の最重要検証項目**:

```toml
# .codex/agents/code-reviewer.toml
[agent]
name = "code-reviewer"
description = "..."
developer_instructions = "..."
model = "claude-opus-4-7"  # ← これが動くか? 公式 docs に明記なし
```

検証手順 (Phase 0 必須):
1. `.codex/agents/test-claude-model.toml` を `model = "claude-opus-4-7"` で作成
2. `codex` 起動して「test-claude-model を使って」と指示
3. 結果:
   - ✅ 動く → 移行 GO、Opus 4.7 を引き続き harness の subagent model に
   - ❌ エラー → `model = "gpt-5.5"` に変更、業務判断力低下を Phase 0 並走で検証
   - ⚠️ 無視されて gpt-5.5 で動く → 同上 (silent fallback の可能性、log 確認)

→ **移行 GO/NO-GO の最重要 gate**。

---

# 3. v1 写像表 更新 (v2 反映)

## 3.1 hook (応答 schema 書換不要 = v1 から工数削減)

| Claude Code | Codex (v2) | 変更内容 |
|---|---|---|
| `.claude/hooks/check-secrets.sh` | `.codex/hooks/check-secrets.sh` | path 変更のみ、bash 本体 + `exit 2` そのまま |
| `.claude/hooks/quality-gate.sh` | 同上 | 同上 |
| `.claude/hooks/post-edit-audit.sh` | 同上 | 同上 |
| `.claude/hooks/claude-md-discipline.sh` → **`agents-md-discipline.sh`** | 同上 | rename + bash 内の CLAUDE.md → AGENTS.md grep に書換 |
| `.claude/hooks/clear-discipline.sh` | 同上 | bash 本体そのまま、ただし **`PreCompact` hook に切替** がより自然 (v2 新機能) |
| `.claude/hooks/db-write-confirm.sh` | 同上 | 同上 (現状維持) |
| `.claude/hooks/db-preflight.sh` | 同上 | 同上 (現状維持) |
| `.claude/hooks/session-start-recent.sh` | 同上 | hashes path 算出を `$HOME/.codex/projects/...` に切替 |
| `.claude/hooks/session-start-load-incantation.sh` | 同上 | 同上、`_NEXT_SESSION.md` location も更新 |
| `.claude/hooks/userprompt-rule-router.sh` | 同上 | bash 本体 + JSON 出力そのまま (現状 Claude Code でも JSON 出力なので Codex でも動作) |
| `~/.claude/hooks.sh` (PermissionRequest / Stop / ntfy push) | `~/.codex/hooks.sh` | path 変更のみ、bash 本体 + ntfy 部分そのまま (PermissionRequest / Stop hook は実在) |

**v1 から削減**: hook 書換工数 1 日 → 0.5 日 (path 変更と CLAUDE.md → AGENTS.md grep の rename だけ)

## 3.2 完全消失リスト 修正版

| # | 機能 | 状態 |
|---|---|---|
| ~~1.1~~ | ~~`PermissionRequest` hook~~ | ✅ 実在に訂正 |
| ~~1.2~~ | ~~`Stop` hook~~ | ✅ 実在に訂正 |
| **1.3** | `agentPushNotifEnabled` (Anthropic Claude mobile push) | 🔴 消失確定 (Claude mobile app 専用)。代替: ntfy.sh push (`~/.codex/hooks.sh` でそのまま稼働) + Codex app の Automations 検討 |
| **1.5** | Anthropic plugin marketplace (12 個 enabled) | 🔴 消失確定 (前回確認済) |
| **1.6** | `effortLevel: xhigh` | 🟡 Codex の `model_reasoning_effort = "high"` で類似機能 (v2 公式 docs 明記) |
| **1.7** | `bypassPermissions` / `skipDangerousModePermissionPrompt` | 🟡 Codex の `approval_policy = "never"` + `sandbox_mode = "workspace-write"` で再現 (`danger-full-access` は cli/reference 未記載 = 要実機確認) |
| **1.8** | Claude mobile app から `--remote-control` 介入 | 🔴 消失確定 (Anthropic app 専用) |
| **1.9** | `TaskCreate / TaskList / TaskGet / TaskUpdate / TodoWrite` harness tools | ⚠️ Codex CLI に同等あるか公式 docs 上 cli/reference に未記載 = 要実機検証 |
| **1.10** | `ScheduleWakeup / CronCreate / Monitor` harness tools | 🔴 cli/reference に schedule 系コマンド未記載 = 不在確定 (外部 cron 必須) |

→ 完全消失は **9 → 5 個に減少**。

---

# 4. v2 反映 Phase 工数

| Phase | v1 工数 | v2 工数 | 削減 |
|---|---|---|---|
| Phase 0 並走 | 1 週間 | 1 週間 | (変わらず) |
| Phase 1 harness 書換 | 2-3 日 | **1-1.5 日** | hook 書換不要分 |
| Phase 2 memory 哲学 | 1-2 日 | 1-2 日 | (変わらず) |
| Phase 3 自走 + 通知 | 1 日 | **0.5 日** | ntfy push 流用可 |
| Phase 4 廃止 | 0.5 日 | 0.5 日 | (変わらず) |
| **合計** (Phase 0 込み) | 6-7 日 + 1 週間並走 | **約 4-5 日 + 1 週間並走** | 約 2 日削減 |

---

# 5. 残る要 user 判断ポイント (v2)

| # | 項目 | tradeoff |
|---|---|---|
| 5.1 | **Subagent の `model` で Claude モデル指定可能か** | Phase 0 で実機検証必須。NO なら全 6 agent を GPT-5.5 化、業務判断力低下リスク |
| 5.2 | **MEMORY 集約戦略**: AGENTS.md に圧縮 vs 拡張 vs 移行中止 | v1 §5.2 と同じ、`project_doc_max_bytes` 値は公式 docs 未記載なので実機確認必要 |
| 5.3 | **`PreCompact` / `PostCompact` 新機能を Phase 1 で導入するか** | Phase 1 工数 +0.5 日、ただし将来の context 管理品質向上 |
| 5.4 | **mobile 介入**: ntfy push 維持 vs Codex app 採用 | ntfy はそのまま動く (新発見)、Codex app 別途検討で OK |

---

# 6. v2 推奨 Phase 0 並走スケジュール

公式 docs 取得済の今、Phase 0 を即着手可能:

1. Day 1: `.codex/agents/test-claude-model.toml` で Claude モデル動作確認 (発見 3)
2. Day 1: `.codex/hooks/test-pretooluse.sh` で `exit 2 + stderr` 互換確認 (訂正 3)
3. Day 2-3: W139-revisit Phase 1 設計を Codex で実施、Claude 並走で品質 diff
4. Day 4: 1 出品 task / 1 supplier 判定 task / code-reviewer HIGH=0 ループ を Codex で完走
5. Day 5: AGENTS.md 32KiB cap (実機値) を確認、現 MEMORY.md (~50KB) trim 案作成
6. Day 6-7: GO/NO-GO 判定 → Phase 1 着手 or 並走維持

---

# 7. 結論

公式 docs 直接 fetch により v1 の 5 件の誤りを訂正。**Codex 移行のハードルは v1 推定より低い** (hook 書換不要、PermissionRequest/Stop 実在)。最大の不確定要因は **subagent の `model = "claude-opus-4-7"` 動作可否** で、ここが NO なら業務判断力低下を覚悟して GPT-5.5 化 (Phase 0 並走で実証)。

ntfy.sh push の代替実装も不要になったため、移行コストは大幅削減 (6-7 日 → 4-5 日、Phase 0 並走 1 週間込み)。
