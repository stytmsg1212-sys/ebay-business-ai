---
title: Claude Code → Codex CLI 全面移行 設計書 (Claude 視点)
author: Claude Opus 4.7
generated: 2026-05-27
scope: harness 層 (`.claude/` / `~/.claude/` / CLAUDE.md / MEMORY 系) のみ
out_of_scope: tools/ebay-manager/*.py 内の anthropic SDK 直叩き (layer 独立、無影響)
companion: codex-migration-design-codex.md (GPT-5.5 視点、Codex 本人が記述)
---

# 0. 結論サマリ

- **技術的に移行可能**。harness の核心 (subagents / hooks / skills / MCP) は Codex 0.130 で対応機構あり (前回ヒアリング済)
- **完全消失する機能**: PermissionRequest hook / Stop hook / Anthropic plugin marketplace (12 個) / `agentPushNotifEnabled` push 通知 / ntfy.sh 経由スマホ push / `learning-output-style` / `effortLevel: xhigh` / `Claude mobile app` からの remote-control 介入
- **設計再考が必要**: MEMORY システム (auto-load 50KB+ → AGENTS.md 32KiB cap への trim or 拡張) / `@import` 構文の階層連結への書換 / Opus 4.7 専門 agents 5 個 (model 指定が GPT-5.5 になり業務判断力が劣化する可能性)
- **総工数**: 6-7 日 (full scope)。並走 1 週間込みで **約 2-3 週間**
- **要 user 判断**: (a) Opus 4.7 業務判断力 vs GPT-5.5 の性能差をどう扱うか (b) スマホ通知方式 (Codex app vs Discord webhook 自前) (c) MEMORY 60+ file をどこまで AGENTS.md に集約するか

---

# 1. 完全消失する機能 (Codex 0.130 では再現不可)

| # | 機能 | 影響 | 代替案 |
|---|---|---|---|
| 1.1 | `PermissionRequest` hook | user 承認時の自動通知不可 | `Stop` hook も無いため、別 lifecycle event で自前 webhook |
| 1.2 | `Stop` hook | 完了時の自動通知不可 | 同上 |
| 1.3 | `agentPushNotifEnabled` (Claude mobile push) | 携帯への完了通知消失 | Discord webhook を自前実装 (eBay Manager bot 流用可) |
| 1.4 | `ntfy.sh` 経由スマホ push (`hooks.sh` PermissionRequest/Stop) | 同上 | 同上 |
| 1.5 | Anthropic plugin marketplace (12 個 enabled) | cc-company / hookify / learning-output-style / claude-code-setup / pyright-lsp / playwright / code-review 等 全消失 | (a) MCP に置換できるもの (playwright) / (b) skill に再実装 (code-review / cc-company / hookify) / (c) 諦める (learning-output-style: output style 機構が Codex に無い、custom instructions で近似) |
| 1.6 | `effortLevel: xhigh` | Anthropic 専用 | Codex の `reasoning_effort = "xhigh"` で類似機能あり (要実機検証) |
| 1.7 | `skipDangerousModePermissionPrompt: true` + `defaultMode: bypassPermissions` | Codex は approval_policy で粒度違う | `approval_policy = "never"` + `sandbox_mode = "danger-full-access"` で再現 |
| 1.8 | `claude --remote-control --name 'ClaudeAutoLoop'` の named session を **Claude mobile app から介入** | user が携帯から会話継続できる核心 | Codex CLI に `remote-control` あるが **Claude mobile からは介入不可** (Codex app になる) |
| 1.9 | `TaskCreate / TaskList / TaskGet / TaskUpdate / TodoWrite` harness tools | session 内の task tracker | Codex に同等 harness tool があるか要実機確認、無ければ skill で代替 |
| 1.10 | `ScheduleWakeup / CronCreate / Monitor` harness tools | 私の自走で重要 | Codex CLI に self-schedule なし、外部 cron (Windows Task Scheduler) で `codex exec` を叩く |

---

# 2. 書換が必要だが移植可能 (file-level 写像表)

## 2.1 settings / config

| Claude Code | Codex CLI | 変換内容 |
|---|---|---|
| `.claude/settings.json` (project) | `.codex/config.toml` (project) | JSON → TOML、hook 応答 schema 書換 (`exit 2` → JSON `{permissionDecision:"deny"}`) |
| `.claude/settings.local.json` | `.codex/config.local.toml` (公式に存在するか要確認) | 同上 |
| `~/.claude/settings.json` | `~/.codex/config.toml` | 同上 |
| `enabledPlugins: {...}` (12 個) | `[plugins]` セクション or skill 再実装 | 個別判断、§3.2 参照 |

## 2.2 agents (md → toml)

| Claude Code | Codex CLI | 注意 |
|---|---|---|
| `.claude/agents/code-reviewer.md` (model: claude-opus-4-7) | `.codex/agents/code-reviewer.toml` (model 指定削除 or GPT-5.5) | **Opus → GPT-5.5 で業務判断力劣化リスク** |
| `.claude/agents/code-architect.md` | `.codex/agents/code-architect.toml` | 同上 |
| `.claude/agents/research-brain.md` | `.codex/agents/research-brain.toml` | **Karpathy K3 が GPT-5.5 で機能するか実証要** |
| `.claude/agents/codex-reviewer.md` | `.codex/agents/codex-reviewer.toml` (循環参照: Codex から Codex CLI 呼ぶ → 廃止案) | 用途自体が「外部視点 2 段 review」だったので、Claude API 直叩き (`anthropic` SDK) で「Claude 視点 review」に再設計 |
| `.claude/agents/ebay-listing.md` | `.codex/agents/ebay-listing.toml` | 出品文生成、英語生成は GPT-5.5 でも可と判断 |
| `~/.claude/agents/ebay-manager-qa.md` | `~/.codex/agents/ebay-manager-qa.toml` | Playwright qa、MCP 経由なので問題なし |

## 2.3 hooks (bash 本体流用、応答 schema のみ書換)

| Claude Code | Codex CLI | 書換ポイント |
|---|---|---|
| `.claude/hooks/check-secrets.sh` | 同 path で OK | `exit 2` → JSON 出力に書換 |
| `.claude/hooks/quality-gate.sh` | 同上 | 同上 |
| `.claude/hooks/post-edit-audit.sh` | 同上 | 同上 |
| `.claude/hooks/claude-md-discipline.sh` → **`agents-md-discipline.sh` にリネーム** | 同上 | CLAUDE.md → AGENTS.md の check に書換 |
| `.claude/hooks/clear-discipline.sh` | 同上 | transcript path schema 要確認 |
| `.claude/hooks/db-write-confirm.sh` | 同上 | 同上 |
| `.claude/hooks/db-preflight.sh` | 同上 | 同上 |
| `.claude/hooks/session-start-recent.sh` | 同上 | json schema コピー可 |
| `.claude/hooks/session-start-load-incantation.sh` | 同上 | hash 算出ロジック (`$HOME/.claude/projects/...` → `$HOME/.codex/...`?) 要 path 再設計 |
| `.claude/hooks/userprompt-rule-router.sh` | 同上 | json schema コピー可 |
| `~/.claude/hooks.sh` | `~/.codex/hooks.sh` | PermissionRequest / Stop 部分削除 (= ntfy push 機能消失) |

## 2.4 commands

| Claude Code | Codex CLI |
|---|---|
| `.claude/commands/listing.md` (`/listing`) | `.codex/commands/listing.md` or `.agents/skills/listing/SKILL.md` |
| `~/.claude/commands/add_s.md` (`/add_s`) | `~/.codex/commands/add_s.md` or skill |
| `~/.claude/commands/mono.md` (`/mono`) | 同上 |

## 2.5 skills (frontmatter Anthropic 互換、本体ほぼコピー)

| Claude Code | Codex CLI |
|---|---|
| `.claude/skills/codex-review/SKILL.md` | `.agents/skills/codex-review/SKILL.md` |
| `~/.claude/skills/session-close/SKILL.md` | `~/.agents/skills/session-close/SKILL.md` (W59 zero-paste 核心) |
| `~/.claude/skills/session-resume/SKILL.md` | 同上 |
| `~/.claude/skills/find-skills/SKILL.md` | **不要** (Codex は `/skills` discovery が標準) |

## 2.6 scripts

| Claude Code | Codex CLI |
|---|---|
| `~/.claude/scripts/start-claude-loop.ps1` | `~/.codex/scripts/start-codex-loop.ps1` (新規) |
| `~/.claude/scripts/claude-loop.ps1` | `~/.codex/scripts/codex-loop.ps1` (新規、`claude --remote-control` → `codex remote-control` に書換) |
| `~/.claude/scripts/claude-loop-status.ps1` | 同上 |
| `~/.claude/scripts/claude-loop-stop.ps1` | 同上 |
| `~/.claude/scripts/upgrade-claude.ps1` | **削除** (`codex update` 公式コマンドで代替) |

## 2.7 CLAUDE.md → AGENTS.md (階層連結再設計)

| Claude Code | Codex CLI | 注意 |
|---|---|---|
| `CLAUDE.md` (project root) | `AGENTS.md` (project root) | `@import` 削除、subdir AGENTS.md は階層連結で自動 load |
| `tools/ebay-manager/CLAUDE.md` (subdir) | `tools/ebay-manager/AGENTS.md` | cwd 階層連結 (近階層後勝ち) で自動読込 |
| `~/.claude/CLAUDE.md` (user global) | `~/.codex/AGENTS.md` | 同上 |
| `.claude/rules/*.md` (8 個 always-load) | AGENTS.md に統合 or `.codex/rules/*.md` を AGENTS.md から手動引用 | `project_doc_max_bytes = 32768` (default) で 32KiB cap、現状 8 rule で 30KiB 超なので **拡張 (65536) 必須** |
| `.claude/rule-snippets/*.md` (5 個 on-demand) | `.codex/rule-snippets/*.md`、UserPromptSubmit hook で keyword JIT 注入 | 既存 router.sh が流用可 (schema 書換のみ) |

## 2.8 MEMORY システム

| Claude Code | Codex CLI | 注意 |
|---|---|---|
| `MEMORY.md` (auto-load) | AGENTS.md or `~/.codex/memories/` 配下 | Memories は **off by default、user 手動 enable 必要** |
| `MEMORY_session.md` / `MEMORY_feedback.md` / 他 tier-2 (5 file) | 該当機構なし、`docs/`-like 配下に置いて Read 経由 | 「常時 indexed」性質消失 |
| 個別 memory (`session_*.md` / `feedback_*.md` / `project_*.md` / `reference_*.md` 計 60+ ファイル) | 同上 | 物理的に file は残る、auto-load されないだけ |

---

# 3. 設計思想の変換が必要

## 3.1 MEMORY 哲学差

Claude Code は「session ごとに編集可能な long-term memory file を MEMORY.md / tier-2 として常時 auto-load」する設計。本 project は 60+ file で歴史を編集可能テキストとして資産化している。

Codex は AGENTS.md (確定的 instructions、32KiB) を主役にし、Memories (off by default) は副次的。よって:

- **選択肢 A (推奨)**: MEMORY.md の核心 (Q0-Q7 / R-1〜R-12 / Karpathy 4 / SKU 用途 / 通関 / Section 232 / 送料 4 区分) を AGENTS.md に集約 (~30KiB)。tier-2 (session/feedback/project/reference) は `.codex/memories/` or `docs/memory/` 配下に置き **on-demand Read** に切替。`description` 一覧 (今の MEMORY.md tier-1 と等価) のみ AGENTS.md に index として 1 行/file
- **選択肢 B**: `project_doc_max_bytes = 65536` (64KiB) に拡張、MEMORY.md tier-1 を AGENTS.md に丸ごと入れる。tier-2 は同上 on-demand Read
- **選択肢 C**: 諦めて Claude Code 並走維持 (移行しない)

## 3.2 enabledPlugins 12 個の取扱い

| Plugin | Codex で対応? | 推奨対応 |
|---|---|---|
| `cc-company` | 同等品なし | `.codex/skills/` に再実装 (秘書フロー + 部署フォルダ) |
| `code-review@claude-plugins-official` | Codex に `code-review` skill あり (`codex review` subcommand) | 公式機能で代替 |
| `hookify@claude-plugins-official` | Codex に hook 生成支援なし | 諦める or 手動 hook 編集 |
| `playwright@claude-plugins-official` | MCP で代替可 (`codex mcp add playwright`) | MCP 化 |
| `pyright-lsp@claude-plugins-official` | Codex に LSP 統合あり | 公式機能で代替 |
| `claude-code-setup@claude-plugins-official` | Claude Code 専用 | 削除 |
| `learning-output-style@claude-plugins-official` | Codex に output style なし | 諦める or custom instructions で近似 |

## 3.3 Opus 4.7 業務判断力 vs GPT-5.5

本 project の `.claude/rules/karpathy-principles.md` で:
- Opus 4.7: 全 K 原則本領発揮
- Sonnet 4.6: 複雑ゴールで迷子化 (K3 分割必要)
- Haiku 4.5: K3 ほぼ非機能

GPT-5.5 が Opus 4.7 と同等か、業務判断 (Section 232 / VeRO / 出品文 / Karpathy K3) で実機検証必要。**ここが移行最大の不確定要因**。

---

# 4. Phase 分割 (推奨)

## Phase 0: 並走検証 (1 週間)

- Codex CLI と Claude Code を **同じ task に並行投入**、品質と速度を比較
- 検証 task: (a) W139-revisit Phase 1 設計 (b) eBay 出品 1 件 (c) supplier 候補判定 1 件 (d) code-reviewer による HIGH=0 ループ
- GO/NO-GO 判定: 業務判断力が Sonnet 4.6 同等以下なら **移行中止**

## Phase 1: harness file 機械的書換 (2-3 日)

- settings.json (project + user + local) → config.toml
- agents 6 個 → toml 化
- hooks 11 個 → 応答 schema 書換 (bash 本体流用)
- commands 3 個 → skill 化
- AGENTS.md (project / user / subdir) 階層連結再構成
- 検証: 各 hook が Codex で発火することを実機確認 (BLOCK / additionalContext / SessionStart auto-load 全部)

## Phase 2: memory 哲学変換 (1-2 日)

- MEMORY.md → AGENTS.md 集約 (選択肢 A 推奨)
- tier-2 (`MEMORY_session.md` 等) を `.codex/memories/` or `docs/memory/` 配下に物理移動
- 60+ 個別 memory file は path 変更のみ
- 検証: 過去事故 (silent skip / SKU 主キー崩壊 / Section 232) が AGENTS.md で context として正しく機能するか実例 task で確認

## Phase 3: 自走機構 + 通知 (1 日)

- `codex-loop.ps1` を `codex remote-control` 経由で再実装
- Windows Task Scheduler に登録
- ntfy push を **Discord webhook 自前** で代替 (eBay Manager の `tools/ebay-manager/monitor/discord.py` 既存ヘルパ流用)
- `Claude mobile app` 経由介入は **諦め**、代替: Codex app をスマホ install + Automations 利用 (要 user 判断)

## Phase 4: Claude Code 完全廃止 (0.5 日)

- `~/.claude/` を `~/.claude.bak.YYYY-MM-DD/` に rename (即時 rollback 用に保持)
- `claude` CLI uninstall (`npm uninstall -g @anthropic-ai/claude-code`)
- Windows Task Scheduler の `eBay Manager Daily Scheduler` は無関係なので touchしない
- 検証: Codex CLI 単独で 1 週間運用、再起動 / kill switch / crash loop guard 全部動作

---

# 5. 要 user 判断ポイント (押し付けない)

| # | 判断項目 | tradeoff 併記 |
|---|---|---|
| 5.1 | **業務判断力**: Opus 4.7 → GPT-5.5 で同等保てるか? | Phase 0 並走で実証。NO-GO なら移行中止 |
| 5.2 | **MEMORY 集約**: 選択肢 A (集約) vs B (拡張) vs C (移行しない) | A: AGENTS.md ~30KiB に絞り込み、tier-2 on-demand。情報量低下のリスク。B: 64KiB に拡張、Codex の API cost 増。C: 移行価値消滅 |
| 5.3 | **スマホ通知**: Discord webhook 自前 vs Codex app | Discord: 既存実装流用、無料。Codex app: 公式機能だが Anthropic の Claude mobile app と別 ecosystem |
| 5.4 | **`codex-reviewer` 廃止判断** | 現状 Codex CLI で Claude 文書を lint。移行後は Codex 内で Codex CLI を呼ぶ循環。Claude API 直叩き (`anthropic` SDK) で「Claude 視点 review」に再設計するか、廃止するか |
| 5.5 | **learning-output-style**: 諦めるか、custom instructions で近似するか | 教育的説明モードが消える。学習目的次第 |
| 5.6 | **session-close skill の zero-paste 機構**: Codex SessionStart hook schema 実機確認後、互換確認 | Codex の hook が `additionalContext` field に json で content を渡せれば OK、互換性は実証必要 |

---

# 6. リスク (実行前に user に開示すべき)

| # | リスク | mitigation |
|---|---|---|
| 6.1 | **Phase 0 で GPT-5.5 < Sonnet 4.6 判定が出る** | 業務判断 task で実証、NO-GO で並走維持 |
| 6.2 | **Codex CLI 0.130 の hook 応答 schema が公式 docs と乖離** | Phase 1 で実機検証、blocker 出たら Codex 公式 issue 投げる |
| 6.3 | **移行中に scheduler.log が止まる** | tools/ebay-manager の APScheduler は harness と独立、影響なし (確認済) |
| 6.4 | **memory 60+ file の path 変更で wikilink (`[[name]]`) 切れ** | sed で一括書換、Codex 側で linkチェック skill 走らせる |
| 6.5 | **claude-loop 停止中に user が携帯から介入したい場面** | Codex app 同時インストール推奨。または PowerShell remote から介入 |
| 6.6 | **W139-revisit Phase 1 等の進行中 task が止まる** | Phase 0 並走中に進行可、Phase 1-3 で 3-4 日止める覚悟必要 |
| 6.7 | **codex-reviewer 経由の文書 lint 機構が止まる** | Phase 1 で codex-reviewer agent を再実装、または直接 `codex exec` で代替 |

---

# 7. 並行作業: Codex 視点の設計書

本書は Claude 視点。Codex 本人 (GPT-5.5) 視点の設計書を `codex-migration-design-codex.md` として並行生成中 (bg job `b7e08gmo1`)。

完了後、2 視点を diff し、特に以下を user に提示:
- 「Codex が『できる』と言うが Claude が『できない』と言う」項目 (情報の食い違い)
- 「Codex 視点の Phase 順序」vs「Claude 視点の Phase 順序」
- Codex が新規に発見した移行 blocker

---

# 8. 推奨アクション

1. **本書を user 確認** → Phase 0 GO 判定なら Phase 1 着手
2. **Codex の設計書 (companion file) 確認** → 差分を user 提示
3. Phase 0 並走検証スケジュール決定 (現在進行 task W139-revisit を題材に推奨)
4. 移行宣言 commit (rollback 用 tag を切る)
