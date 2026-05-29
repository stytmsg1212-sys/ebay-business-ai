# Claude Code → Codex CLI 移行設計 (Codex 視点)

作成日: 2026-05-27
対象 root: `C:\Users\gucch\projects\claude`
scope: harness 層のみ。`tools/ebay-manager/*.py` の Anthropic SDK 直叩きは除外。

実機確認:
- `codex --version` = `codex-cli 0.130.0`
- `~/.codex/config.toml` = project trust 設定のみ
- `codex features list`: `hooks=true`, `plugins=true`, `multi_agent=true`, `memories=false experimental`, `child_agents_md=false`, `plugin_hooks=false`
- 公式確認: OpenAI Codex docs 0.134 系 (`AGENTS.md`, hooks, skills) と local `codex --help` / `codex exec --help` / `codex mcp --help`
- 参照: https://developers.openai.com/codex/guides/agents-md / https://developers.openai.com/codex/hooks / https://developers.openai.com/codex/skills / https://github.com/openai/codex

実機で読んだ対象:
- project: `CLAUDE.md` 73 行、`.claude/settings.json`、`.claude/settings.local.json`、`tools/ebay-manager/CLAUDE.md` 170 行
- rules 8 file: `00-constitution.md` 37、`karpathy-principles.md` 50、`silent-skip-prevention.md` 68、`db-migration-rules.md` 67、`sku-rules.md` 61、`md-files-can-be-wrong.md` 41、`sqlite-timezone.md` 62、`cascade-update.md` 62
- rule-snippets 5 file: `contradiction-annotation.md` 53、`discord-notification.md` 36、`llm-wiki-compilation.md` 60、`supplier-matching-rules.md` 39、`wiki-frontmatter.md` 127
- agents 5 file: `code-architect.md` 69、`code-reviewer.md` 65、`codex-reviewer.md` 84、`ebay-listing.md` 136、`research-brain.md` 117
- hooks 10 file: `check-secrets.sh` 15、`claude-md-discipline.sh` 108、`clear-discipline.sh` 165、`db-preflight.sh` 64、`db-write-confirm.sh` 60、`post-edit-audit.sh` 45、`quality-gate.sh` 99、`session-start-load-incantation.sh` 294、`session-start-recent.sh` 74、`userprompt-rule-router.sh` 118
- commands/skills: `.claude/commands/listing.md` 3、`.claude/skills/codex-review/SKILL.md` 60
- user global: `~/.claude/CLAUDE.md` 33、`~/.claude/settings.json`、`~/.claude/hooks.sh`、`add_s.md` 51、`mono.md` 27、`find-skills` 95、`session-close` 137、`session-resume` 55、`ebay-manager-qa.md` 104
- memory: `MEMORY.md` 72、`MEMORY_session.md` 43、`MEMORY_feedback.md` 38。後者 2 file は存在確認済み。

## 1. 完全に移行できないもの

| Claude Code 側 | 観測内容 | Codex CLI 0.130/0.134 での結論 |
|---|---|---|
| `CLAUDE.md` 自動読込 | root/global/subdir の 3 層 | 不可。Codex は `AGENTS.md` が規約 file。 |
| `@tools/ebay-manager/CLAUDE.md` import | root 末尾で launch load 強制 | 不可。Codex `AGENTS.md` に同等 `@import` 前提を置かない。 |
| Claude permissions DSL | `Bash(...)`, `Read(...)`, `WebFetch(domain:...)`, `Skill(...)`, `mcp__...` | 不可。Codex は sandbox/approval/config/profile/MCP へ再設計。 |
| `defaultMode=bypassPermissions` | project settings | 同名不可。`--dangerously-bypass-approvals-and-sandbox` 等へ user 判断で置換。 |
| `skipDangerousModePermissionPrompt` | global settings | 同名不可。 |
| Claude hook event 全互換 | `SessionStart`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, global `PermissionRequest`, `Stop` | 不可。Codex hooks は存在するが event 名・stdin JSON・出力契約は別。 |
| `hookSpecificOutput.additionalContext` / `systemMessage` | session/memory/router が context 注入 | 同型不可。`AGENTS.md`、skills、wrapper prompt へ変換。 |
| `.claude/commands/*.md` slash command | `listing`, `add_s`, `mono` | 不可。Codex skill または PowerShell wrapper 化。 |
| `.claude/agents/*.md` 自動 subagent | 5 project agents + 1 global agent | 0.130 実機で `child_agents_md=false`。そのまま不可。 |
| Claude model 固定 | `claude-opus-4-7`, `claude-opus-4-6` | 不可。OpenAI model/profile 方針へ。 |
| Claude plugins | `company@cc-company`, official plugins | Codex plugins とは別物。機能単位で再選定。 |
| Claude remote-control loop | `claude --remote-control --name ClaudeAutoLoop` | 互換不可。Codex `remote-control` は experimental で別物。 |
| Claude upgrade script | `winget upgrade Anthropic.ClaudeCode` | 不可。Codex は `codex update` / npm update 系。 |
| Claude memory auto-load | `~/.claude/projects/.../memory` + `_NEXT_SESSION.md` + SessionStart | 不可。0.130 実機で `memories=false experimental`。 |
| Claude transcript monitor | `transcript_path` JSONL を `clear-discipline.sh` が読む | 不可。Codex session 保存形式に合わせて新規実装。 |
| Claude env | `$CLAUDE_PROJECT_DIR`, `CLAUDE_TOOL_INPUT` | 不可。Codex env/payload に書換。 |
| PermissionRequest/Stop 通知 | `~/.claude/hooks.sh` beep/ntfy | 同型不可。wrapper/watchdog として別実装。 |
| `settings.local.json` 全量 | 巨大 allow list、旧 path、WebFetch domain、Gmail/GSheets 等 | 機械移植不可。必要最小を MCP/config へ再選定。 |

## 2. 書換が必要だが移植可能なもの

| Claude 側 | 行数 | Codex 側 | 変換 |
|---|---:|---|---|
| `CLAUDE.md` | 73 | `AGENTS.md` | Q0-Q7、Karpathy、eBay 核心、company 運用を Codex 用語で統合。 |
| `~/.claude/CLAUDE.md` | 33 | `~/.codex/AGENTS.md` or project `AGENTS.md` | tool 優先、探索 routing、並列、権限方針を Codex shell/apply_patch/MCP 方針へ。 |
| `tools/ebay-manager/CLAUDE.md` | 170 | `tools/ebay-manager/AGENTS.md` | eBay 固有規約を保持。`@import` 前提は削除。 |
| `00-constitution.md` | 37 | `AGENTS.md` | Q0-Q7 と金銭直結 rule を常時規約化。 |
| `karpathy-principles.md` | 50 | `AGENTS.md` / skill | K0-K3 は保持。Claude model 依存だけ置換。 |
| `silent-skip-prevention.md` | 68 | `AGENTS.md` + hooks | silent skip/fake success/avoidance refactor 禁止を保持。 |
| `db-migration-rules.md` | 67 | `AGENTS.md` + DB guard | 冪等性、backup、24h retrospective を保持。 |
| `sku-rules.md` | 61 | `AGENTS.md` | SKU 用途 2 つだけ、listing 識別は `ebay_item_id`。 |
| `md-files-can-be-wrong.md` | 41 | `AGENTS.md` | 実コード > .md を保持。 |
| `sqlite-timezone.md` | 62 | `AGENTS.md` | UTC/JST 混在ルールを保持。 |
| `cascade-update.md` | 62 | `AGENTS.md` + skill | 関連 file grep、同 session 更新、両論併記を Codex workflow へ。 |
| rule-snippets 5 file | 36-127 | `.codex/skills/*/SKILL.md` | router 注入ではなく on-demand skill 化。 |
| `code-architect.md` | 69 | skill | design output と探索手順を移植。frontmatter model/tools は削除。 |
| `code-reviewer.md` | 65 | skill + `codex review` | HIGH=0 ループと review 観点を移植。 |
| `codex-reviewer.md` | 84 | `docs-lint` skill | Claude から Codex を呼ぶ役割は反転。文書 lint として再定義。 |
| `ebay-listing.md` | 136 | skill | eBay listing 生成 skill として移植。 |
| `research-brain.md` | 117 | skill | Research brain として移植。 |
| `ebay-manager-qa.md` | 104 | skill | Playwright/Streamlit QA 手順として移植。 |
| `codex-review/SKILL.md` | 60 | 廃止 or `docs-lint` | 移行後は「Codex 外部 reviewer」ではなく自己 review。 |
| `find-skills/SKILL.md` | 95 | 原則不要 | Codex 実機に system `skill-installer` あり。 |
| `session-close/SKILL.md` | 137 | skill | `_NEXT_SESSION.md` 生成は保持。auto-load 前提を削除。 |
| `session-resume/SKILL.md` | 55 | skill | manual resume と staleness check として保持。 |
| `listing.md` | 3 | `ebay-listing` skill/wrapper | `/listing` を skill trigger へ。 |
| `add_s.md` | 51 | `scripts/codex-add-roadmap.ps1` | `$ARGUMENTS` を CLI 引数へ。 |
| `mono.md` | 27 | `scripts/start-monodeck.ps1` | `run_in_background=true` を `Start-Process` へ。port 8501 health check は保持。 |
| `.claude/settings.json` | - | `~/.codex/config.toml` + hooks | permissions と hooks を分解移植。 |
| `~/.claude/settings.json` | - | `~/.codex/config.toml` | allow/deny/plugins/effort を Codex profile/guard へ。 |
| `~/.claude/hooks.sh` | - | `scripts/codex-notify.ps1` | beep/ntfy は可能。event は別設計。 |
| `start-claude-loop.ps1` | - | `start-codex-loop.ps1` | lock/heartbeat/kill switch/crash guard は流用。起動コマンドは再判断。 |
| `claude-loop.ps1` | - | archive | 旧版。移植するなら一本化。 |
| `upgrade-claude.ps1` | - | `upgrade-codex.ps1` | loop stop/update/restart 構造だけ流用。 |

hook 個別:
- `check-secrets.sh` 15 行: file path block として移植可。
- `quality-gate.sh` 99 行: Python anti-pattern block として移植可。
- `claude-md-discipline.sh` 108 行: `AGENTS.md` discipline へ改名。
- `db-preflight.sh` 64 行: `.db` 0 bytes guard は保持。
- `db-write-confirm.sh` 60 行: destructive SQL + recent backup guard は保持。
- `post-edit-audit.sh` 45 行: post-edit warning として保持。
- `clear-discipline.sh` 165 行: Claude transcript 依存が強い。Codex session log 用に新規。
- `session-start-recent.sh` 74 行: wrapper/skill で recent activity 表示。
- `session-start-load-incantation.sh` 294 行: `_NEXT_SESSION.md` 検査は保持、auto context injection は不可。
- `userprompt-rule-router.sh` 118 行: keyword router は skill trigger へ置換。

コマンド変換:
- `claude --version` → `codex --version`
- `claude --remote-control --name ClaudeAutoLoop` → 未確定。候補は `codex` interactive / `codex remote-control` experimental / `codex exec`
- `claude mcp:*` → `codex mcp list/get/add/remove/login/logout`
- `winget upgrade Anthropic.ClaudeCode` → `codex update` or npm update
- `/listing` → `ebay-listing` skill or `scripts/codex-listing.ps1`
- `/mono` → `scripts/start-monodeck.ps1`
- `/add_s <text>` → `scripts/codex-add-roadmap.ps1 <text>`

## 3. 設計思想の変換が必要なもの

### MEMORY システム

現状:
- `MEMORY.md` は tier-1 auto-load 前提。
- `MEMORY_session.md` / `MEMORY_feedback.md` は tier-2 on-demand index。
- `session-close` が session memory、index、`_NEXT_SESSION.md` を作る。
- `session-start-load-incantation.sh` が staleness、MD5、scheduler.log、silent_skip_ongoing、heartbeat を検査し context 注入。

Codex 方針:
- 0.130 実機で `memories=false experimental` のため auto-load を前提にしない。
- `AGENTS.md` には常時規約だけ置き、履歴 memory は `session-resume` skill が明示的に読む。
- memory 実体は project-local `.company/engineering/memory/` または `.codex/memory/` へ寄せる判断が必要。
- `_NEXT_SESSION.md` は廃止せず、manual resume の first-read source にする。

### `@import`

現状:
- root `CLAUDE.md` が `@tools/ebay-manager/CLAUDE.md` を使う。
- hook が `@import` 漏れを検出。

Codex 方針:
- `@import` 依存を廃止。
- root `AGENTS.md` に eBay 核心だけ直接入れる。
- 詳細は `tools/ebay-manager/AGENTS.md` として、作業 path に応じて読む。

### Skills / Agents

現状:
- Claude skills は自動 trigger と slash command 的利用が混在。
- Claude agents は `model/tools` frontmatter 依存。

Codex 方針:
- skill は「必要時に読む手順書」として移植。
- agents は当面 skill 化。`child_agents_md` が false の間、agent md 自動発見は blocker にしない。
- `codex-reviewer` は移行後に役割が変わるため `docs-lint` へ改名候補。

## 4. Phase 分割

### Phase 0 = 並走検証

- Claude Code は止めない。
- Codex 0.130 の実機制約を `features list` と help で固定。
- Codex read-only で `AGENTS.md` prototype の理解を検証。
- 完了条件: Claude を壊さず、Codex が Q0/Q2/SKU/SQLite/cascade を読める。

### Phase 1 = harness 機能移植

- root `AGENTS.md` 作成。
- `tools/ebay-manager/AGENTS.md` 作成。
- `.codex/skills/` に agents/skills を移植。
- `mono` / `add_s` を PowerShell wrapper 化。
- secret/DB/quality hooks を Codex payload または wrapper guard として再実装。
- 完了条件: Codex だけで通常作業の規約、MonoDeck 起動、ROADMAP 追加、review guard が回る。

### Phase 2 = memory 哲学変換

- memory 置き場を決定。
- `MEMORY.md` を必読 index として短縮。
- `_NEXT_SESSION.md` を manual resume source にする。
- `session-close` / `session-resume` を Codex skill 化。
- 完了条件: 新 Codex session で明示 resume すれば前回状態を復元できる。

### Phase 3 = 自走機構

- `start-codex-loop.ps1` を作る。
- 既存の lock/heartbeat/kill switch/crash loop guard を流用。
- 起動方式を user 判断: interactive / experimental remote-control / exec queue。
- `upgrade-codex.ps1` を作る。
- 完了条件: logon 後に Codex loop が 1 instance だけ起動し、heartbeat と kill switch が効く。

### Phase 4 = Claude Code 完全廃止

- Claude Startup/Task Scheduler/auto loop を停止。
- `.claude/` と `~/.claude` harness を archive/read-only 化。
- company docs の Claude Code 前提を Codex CLI へ cascade update。
- 完了条件: Codex のみで開始、再開、終了、review、MonoDeck 運用ができる。

## 5. 要 user 判断ポイント

| 判断 | 選択肢 | tradeoff |
|---|---|---|
| Codex 権限 | `danger-full-access + approval never` / `workspace-write` | 前者は自走性が高いが破壊リスク大。後者は安全だが速度低下。 |
| memory 置き場 | `~/.codex/memories` / `.company/engineering/memory` / `.codex/memory` | home は個人向き、company は共有向き、`.codex` は用途明確。 |
| `_NEXT_SESSION.md` | 継続 / Codex memories 待ち / 廃止 | 継続が最小リスク。廃止は context loss 大。 |
| hooks 強制度 | hard block / warning first / manual | 移行初期は warning first が安全。 |
| agents 移植形 | skills / plugin / child_agents_md 待ち | skills が現実的。plugin は管理性、待ちは blocker。 |
| 自走 loop | interactive / remote-control experimental / exec queue | remote-control は近いが experimental。exec は堅牢だが対話性低い。 |
| `company@cc-company` | plugin 化 / docs 化 / 後回し | plugin 化は構造維持、docs 化は最速。 |
| `settings.local.json` | 必要分のみ / 全棚卸し / 移植しない | 必要分のみが現実的。全量は stale 持込リスク。 |
| Web 調査 | `--search` / MCP-browser / curl | `--search` が自然。curl は引用・安全管理が荒れやすい。 |
| Claude 廃止時点 | Phase 1 後 / Phase 2 後 / Phase 3 後 | Phase 3 後が完全移行として安全。 |

## 6. リスク

| リスク | 影響 | 対策 |
|---|---|---|
| `memories=false` | zero-paste resume 消失 | manual `session-resume` で読む。 |
| `child_agents_md=false` | Claude agent md が動かない | skill 化を先行。 |
| context injection 消失 | rule-snippets と `_NEXT_SESSION.md` が自動注入されない | 常時必須は `AGENTS.md` 昇格、残りは skill。 |
| PermissionRequest/Stop 不在 | beep/ntfy が落ちる | process/log watchdog に置換。 |
| permission 粒度低下 | secret/DB/eBay API 事故 | guard hooks/wrapper を Phase 1 で先に移植。 |
| `danger-full-access` | 破壊リスク | Claude deny list の重要項目を Codex guard 化。 |
| `@import` 廃止 | eBay 核心漏れ | root `AGENTS.md` に短縮版を直接統合。 |
| `settings.local.json` stale | 旧 path/不要 permission 持込 | 全量移植禁止。 |
| remote-control experimental | 自走機構が不安定 | interactive/exec fallback を用意。 |
| Claude plugins 消失 | review/hookify/playwright 等が消える | Codex native review/MCP/skills に分解。 |
| encoding | 一部出力で mojibake | 重要規約は UTF-8 で再保存し、移植時に再確認。 |
| 運用停止 | MonoDeck/ROADMAP/resume が一時欠落 | Phase 0/1 は Claude と並走。 |
| docs と実機差 | 0.134 docs の機能が 0.130 で使えない | 実機 `features list` を優先。false/experimental は blocker 扱い。 |

最小実装順:
1. root `AGENTS.md` + `tools/ebay-manager/AGENTS.md`
2. `session-resume` / `session-close` Codex skill
3. `mono` / `add_s` PowerShell wrapper
4. DB/secret/quality guard
5. agents の skill 化
6. Codex auto loop

DONE
