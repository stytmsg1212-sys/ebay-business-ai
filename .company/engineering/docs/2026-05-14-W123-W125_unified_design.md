# W123-W125 統合設計書 + Astro-Han Karpathy LLM Wiki 採用

**Date**: 2026-05-14
**Author**: Claude Code (Opus 4.7), reviewed by code-architect (Opus 4.7)
**Status**: DRAFT — review pending
**Scope**: W123 (Obsidian 連携) / W124 (OpenAI Codex 登録) / W125 (Codex non-source reviewer 化) + Astro-Han skill 採用

---

## 1. Goals & Non-Goals

### Goals

1. **memory ファイル群を Obsidian で編集・閲覧可能化** (W123)
2. **OpenAI Codex CLI を Claude Code から呼出可能化** (W124)
3. **ソースコード以外のすべて (memory / KB / 設計書 / 学習素材) を Codex で自動 review** (W125)
4. **Karpathy LLM Wiki 規約の selective 採用** で memory architecture の長期 quality 向上
5. **複数 AI ツール (Claude / Codex / ChatGPT / 将来) が同一 knowledge base を参照** できる共通基盤化

### Non-Goals

- Cursor / Aider / Gemini CLI 等の追加 AI tool 統合 (将来別 W)
- 既存 memory ファイル群の **構造的マイグレーション** (K2 違反、frontmatter 拡張のみ)
- Q0-Q6 ルールや eBay 業務固有規約の変更 (我々の固有強み維持)
- MCP server 化 (over-engineered、Bash 直叩きで開始、3 回出てから判断)
- Codex App (cloud) 採用 (CLI 単独で開始、月 2 で再評価)
- Pro $100/$200 tier 即採用 (Plus $20 から実測ベース昇格判断)

---

## 2. Background

### 2.1 Karpathy LLM Wiki Paradigm (2026-04-02)

Karpathy が X / GitHub gist で提唱した knowledge base 構造:

- **3 層**: Raw sources (immutable) / Wiki layer (LLM 維持 markdown) / Schema (CLAUDE.md 的)
- **3 操作**: Ingest / Query / Lint
- **本質**: RAG (query 時 retrieve) → Compilation (ingest 時 synthesize) へのパラダイム移行

### 2.2 我々の現状 = Karpathy 流の ~80% 既実装

| Karpathy 用語 | 我々の現状 | 状態 |
|---|---|---|
| Raw sources | `tools/ebay-manager/*.py` / eBay API 応答 / 動画 / FedEx 通関書類 | ✅ |
| Wiki layer | `memory/*.md` / `.company/ebay-knowledge/` / `.company/secretary/notes/` | ✅ |
| Schema | `CLAUDE.md` / `.claude/rules/*.md` | ✅ |
| Ingest 操作 | session-close skill | ✅ |
| Query 操作 | SessionStart hook + MEMORY.md auto-load | ✅ |
| Lint 操作 | R-9 (SUPERSEDED 自動検出) のみ部分実装 | 🟡 **W125 で本格化** |

### 2.3 不足している Astro-Han 規約 (Gap Analysis)

| Astro-Han 規約 | 我々の現状 | 採用価値 |
|---|---|---|
| raw/ vs wiki/ 明示分離 | 混在 | ⭐⭐⭐⭐ |
| Sources / Raw / Updated frontmatter | name / description のみ | ⭐⭐⭐⭐ |
| cascade update (波及 scan) | 受動的 R-9 のみ | ⭐⭐⭐⭐⭐ |
| **矛盾アノテーション規約 (両論併記)** | 後勝ち式 supersession | ⭐⭐⭐⭐⭐ |
| append-only log.md | 粒度の粗い session_*.md | ⭐⭐⭐ |
| **lint deterministic checks (9 種)** | R-9 のみ | ⭐⭐⭐⭐⭐ |
| archive page 概念 | なし | ⭐⭐ (採用しない) |
| citation 規約 | `[[name]]` のみ | ⭐⭐ (採用しない) |

### 2.4 我々が持っていて Astro-Han にない 8 項目 (維持)

Q0-Q6 業務規律 / eBay 規制 KB / SessionStart hook / subagent ecosystem / Quality Gate hook / `/feature-dev` flow / MEMORY.md auto-load / 多層 nesting — **これらは我々の固有強み**、Astro-Han 採用時も変更しない。

---

## 3. Architecture Decisions

### 3.1 W123 Obsidian 連携

#### Decision A: vault path は **OneDrive 外**

```
C:\Users\gucch\obsidian-vault\          (vault root, OneDrive 外)
├── memory\          ← junction → C:\Users\gucch\.claude\projects\...\memory\
├── company\         ← junction → C:\Users\gucch\OneDrive\work\claude\.company\
└── .obsidian\       (Obsidian config、vault 固有)
```

**Rationale**:
- OneDrive ↔ Obsidian Git plugin の **conflict copy 量産事故** を物理排除 (Obsidian community 実例多数)
- `.git/FETCH_HEAD` / `community-plugins.json` の競合回避

**Risk & Mitigation**:
- Junction が壊れた場合 → vault が空になる: **junction 作成スクリプトを `scripts/setup_obsidian_vault.ps1` に保存**

#### Decision B: 統合 tool = **obsidian-skills (kepano, MIT, 2026-02 初出)**

- Agent Skills 標準準拠の Obsidian skill
- Obsidian 1.12+ 必須 (本番運用前に Obsidian 版確認)
- Claude Code から `npx skills add kepano/obsidian-skills` で install

**MCP server alternatives は保留**:
- `iansinnott/obsidian-claude-code-mcp` 等あるが、初期段階では over-engineered (K1)
- orphan 検出など vault graph 操作が必要になってから追加

#### Decision C: Git 同期は **W124 完了後**

OneDrive 外の vault に Obsidian Git plugin を入れて GitHub と sync。これは **W124 で GitHub アカウント設定後** に着手。

### 3.2 W124 OpenAI Codex 登録

#### Decision A: Codex CLI 単独 (Copilot 経由 NG、Codex App は保留)

```powershell
npm i -g @openai/codex
codex  # 初回 ChatGPT login
```

**Rationale**:
- Copilot は autocomplete 強化が本領、独立 agent 用途不向き ("They're not competitors" 評価)
- Codex App (cloud) は parallel background batch 向き、現状の対話的 review 用途に過剰
- Codex CLI は Claude Code と同型 UX (terminal + MCP + 画像入力)

#### Decision B: 料金 = **ChatGPT Plus $20/月 から start**

**Rationale**:
- 25x promo (5/31 まで) は **Pro $200 tier 限定**、Plus は対象外 (前回 review で訂正)
- Plus $20 は固定費、API key 直叩きより予測可能
- 1 週間 dogfood で token 実測 → Pro $100 昇格 or API key 移行を判断

**Cost cap**:
- `codex exec --max-tokens N` で物理上限
- 5h window hit 時 Discord 警告

#### Decision C: 連携方式 = **Bash `codex exec --json` 直叩き**

```bash
codex exec --sandbox read-only --json --max-tokens 50000 \
  "review C:/.../memory/foo.md against lint checks list"
```

**Rationale (前回 review より)**:
- Codex MCP server (`codex mcp-server`) は対話 loop 向き、我々の用途 (依頼 → 結果) ではない
- Bash 直叩きが K1 surgical
- MCP は 3 回目以降の use case 出てから (subagent 多段対話 等)

### 3.3 W125 Codex Non-Source Reviewer 化

#### Decision A: review 対象 = **ソースコード以外のすべて**

| 対象 | 担当 |
|---|---|
| `*.py` / `*.ts` / `*.js` 等 | Claude Code の `code-reviewer` agent (現状維持) |
| `memory/*.md` / `.company/*.md` / `*.md` 設計書 / `CLAUDE.md` / `.claude/rules/*` / 学習素材 | **Codex (W125 新規)** |

**Rationale**:
- Karpathy "Lint" 操作の自動化 (Lint = wiki maintenance, ≠ code review)
- Claude の echo chamber 化防止 (同 lineage が同 lineage を review しない)

#### Decision B: 連携方式 (3 段階)

| 方式 | 用途 | 着手フェーズ |
|---|---|---|
| **(i) subagent** `.claude/agents/codex-reviewer.md` | 定型 review (memory / KB) | Week 3 |
| **(ii) slash command** `.claude/skills/codex-review/SKILL.md` | user 任意 fire | Week 3 |
| **(iii) cron 定期** scheduler 経由 | 日次 lint | Month 2 (token 実測後) |
| (iv) hook 自動 fire | 過剰、当面なし | (採用しない) |

#### Decision C: review 指示 = **Astro-Han lint checks リスト準拠**

```
Auto-fix:
- index 整合性 (MEMORY.md ↔ 実ファイル)
- internal link 検証 ([[name]] が実在)
- raw 参照検証 (source link 切れ)
- See Also cross-references 補完

Report-only:
- factual contradictions (両論併記なし)
- outdated / superseded claims (Updated > N ヶ月)
- missing conflict annotations
- orphan pages (どこからも link されない)
- stale archive (raw 更新後 wiki 未追随)
```

#### Decision D: 2-stage loop (Codex output を Claude が再評価)

```
1. Codex が review → report 生成
2. Claude が report を読む
3. Claude が「重要度 + 修正提案」を user に提示
4. user 承認後、Claude が修正実行
```

**Rationale**: Codex も hallucinate する、single-shot 採用は危険

### 3.4 Astro-Han Skill 選択的採用

#### Adopted (5 項目)

| Astro-Han 規約 | 採用方法 |
|---|---|
| **矛盾アノテーション** | `.claude/rules/contradiction-annotation.md` 新設、feedback / reference 系で両論併記 |
| **cascade update** | `.claude/rules/cascade-update.md` 新設、規約変更時の必須 step として組込 |
| **lint deterministic checks** | W125 codex-reviewer agent の指示に Astro-Han 9 種 checks を埋込 |
| **raw vs wiki frontmatter** | `layer: raw \| wiki` を frontmatter に追加 (新規 / 編集時のみ、一括変更しない = K2) |
| **frontmatter 拡張** | Sources / Raw / Updated を追加、6ヶ月以上未 update を stale candidate に |

#### Skill install: **Astro-Han の lint command を流用、構造は採用しない**

```bash
npx skills add Astro-Han/karpathy-llm-wiki
# Skill は install するが、raw/ + wiki/ ディレクトリ強制構造は採用せず、
# 既存の memory/ + .company/learning/ にそのまま lint コマンドを向ける
```

#### Not Adopted (3 項目)

| Astro-Han 規約 | 採用しない理由 |
|---|---|
| archive page 概念 | session_*.md がその役割、追加すると K1 違反 |
| wiki/ one-level nesting only | 我々の `.company/learning/karpathy/` 多階層が業務適合 |
| 「LLM writes, human reads」 | user が eBay ドメイン知識を直接書く混合運用が業務適合 |

---

## 4. Implementation Roadmap

### Week 1 (5/14-5/21): 基礎整備 + W123

| Phase | 内容 | 工数 | 成功基準 (K3) |
|---|---|---|---|
| **A** | `.claude/rules/` に新 3 rule (contradiction / cascade / wiki-frontmatter) + CLAUDE.md @import 追加 | 2h | rule ファイル 3 個作成 + CLAUDE.md 整合 |
| **B** | Astro-Han skill install + `.company/learning/karpathy/` で lint 試行 (既存ファイルに非破壊で実行) | 1h | lint 出力件数 / 既存ファイル変更 0 |
| **C** | Obsidian vault 構築 (OneDrive 外 path + junction) | 1h | OneDrive conflict copy = 0 件 / 1 週間 |
| **D** | `kepano/obsidian-skills` install + Claude Code から vault 操作試行 | 1h | obsidian-cli 経由で memory/ への read/write 確認 |

**Week 1 終了時に user dogfood 開始** → 1 週間運用して問題なければ Week 2 へ

### Week 2 (5/22-5/28): W124 Codex CLI

| Phase | 内容 | 工数 | 成功基準 |
|---|---|---|---|
| **A** | user 操作: ChatGPT Plus 加入 (既存なら skip) | user 5 分 | アカウント active |
| **B** | `npm i -g @openai/codex` + 認証 + 動作確認 | 30 分 | `codex "list files"` が Claude Code Bash から成功 |
| **C** | 1 ファイルに対する `codex exec --sandbox read-only --json` 試行 | 30 分 | review 出力が JSON 形式で取得可能 |
| **D** | token 実測 (1 日分、対話的に複数回 review 実行) | 1 日 | token/日 / 5h window hit 回数の実測値 |

**Week 2 終了時の判断**:
- Plus $20 で十分か / Pro $100 昇格すべきか / API key 移行か

### Week 3 (5/29-6/4): W125 Codex Reviewer Wiring

| Phase | 内容 | 工数 | 成功基準 |
|---|---|---|---|
| **A** | `.claude/agents/codex-reviewer.md` 作成 (subagent、Astro-Han 9 checks 埋込) | 1.5h | subagent から Codex 呼出可能 |
| **B** | `.claude/skills/codex-review/SKILL.md` 作成 (slash command) | 1h | `/codex-review <path>` で fire 可能 |
| **C** | 2-stage loop (Codex output → Claude 再評価) 検証 | 1h | review 出力の重要度判定 + user 提示が動作 |
| **D** | Discord 通知連動 (重要 finding のみ) | 30 分 | webhook 動作確認 |

### Month 2 (6/5 以降): 拡大判断

- **cron 化判断**: token 実測値 (Week 2) + lint 出力品質 (Week 3) を見て、scheduler に `daily_codex_review` task 追加するか判断
- **memory/ 全体への lint 拡大**: Week 3 で `.company/learning/` だけだったのを memory 全域に
- **MCP server / Codex App 再評価**: 3 回以上のユースケースが見えたら検討
- **GitHub 同期**: Obsidian Git plugin で memory / vault を GitHub private repo 同期 (user 別途 GitHub アカウント設定)

---

## 5. File-Level Change Inventory

### 新規作成

| Path | 役割 |
|---|---|
| `.claude/rules/contradiction-annotation.md` | 両論併記規約 |
| `.claude/rules/cascade-update.md` | 波及 update 規約 |
| `.claude/rules/wiki-frontmatter.md` | Sources / Raw / Updated / layer 規約 |
| `.claude/agents/codex-reviewer.md` | Codex 経由 subagent |
| `.claude/skills/codex-review/SKILL.md` | slash command |
| `scripts/setup_obsidian_vault.ps1` | vault + junction 構築 PowerShell |
| `C:\Users\gucch\obsidian-vault\` | vault root (OneDrive 外) |

### 既存編集

| Path | 変更内容 |
|---|---|
| `CLAUDE.md` (project root) | 新 3 rule の `@import` 追加 (~5 行) |
| `MEMORY.md` | 新規 reference memory への link 追加 |
| `.company/learning/karpathy/00-index.md` | frontmatter に layer / sources / updated 追加 (実証 1 例) |

### 既存維持 (変更なし)

- `memory/feedback_*.md` / `session_*.md` / `project_*.md` (frontmatter 拡張は新規 / 編集時のみ)
- `tools/ebay-manager/*` 全体
- 既存 `.claude/agents/*` / `.claude/skills/*`
- 既存 `.claude/hooks/quality-gate.sh`

---

## 6. Success Criteria (K3 Goal-Driven)

| 項目 | 測定方法 | 合格基準 |
|---|---|---|
| Obsidian conflict copy | `Get-ChildItem -Recurse "* - *.md"` で count | 1 週間 = 0 件 |
| Codex token/日 | ChatGPT account dashboard | (実測ベース判断、< Plus limit 想定) |
| Codex 5h window hit | scheduler.log でカウント | 1 週間で < 3 回 |
| Codex review 出力品質 | user 主観評価 | 「価値ある finding」率 > 30% |
| memory file lint 違反 | Codex 自動検出 | 既存ファイルで baseline 確立 → 月次で減少傾向 |
| ROADMAP 進行 | ROADMAP UI で進捗 | W123 / W124 / W125 完了 (3 週以内) |

---

## 7. Risks & Mitigations

| Risk | 影響 | Mitigation |
|---|---|---|
| OneDrive ↔ Obsidian 衝突 | conflict copy 量産 | vault を OneDrive **外** に物理隔離 |
| Codex hallucination | 誤 review で memory 破壊 | 2-stage loop (Codex → Claude → user) |
| Codex token cost 暴走 | $20/月超過 | `--max-tokens` cap + 5h window 監視 |
| Astro-Han skill 仕様変更 | install 後の breaking change | npm version pin / 1ヶ月 stable 経過後 build cache |
| K2 違反 (frontmatter 一括変更) | 既存ファイル破壊 | 新規 / 編集時のみ追加、bulk 変更禁止 |
| Obsidian 1.12 が beta だった | vault 不安定 | Obsidian 安定版に下げて kepano skill 諦め |
| junction が壊れる | vault 空 | `setup_obsidian_vault.ps1` で再構築可能化 |
| Codex authentication 期限切れ | cron fire 失敗 | API key 経由運用に切替 (Month 2) |
| memory 編集中の競合 | Claude + user 同時編集 | 当面 single-user 想定、衝突は手動解決 |

---

## 8. Rollback Plan

各 phase で問題発生時:

### Week 1 rollback
- `.claude/rules/` 新 3 rule を削除、CLAUDE.md の @import を revert
- Obsidian vault は外部なので OneDrive 同期に影響なし、vault フォルダ削除のみ
- Astro-Han skill は uninstall (`npx skills remove ...`)

### Week 2 rollback
- Codex CLI は `npm uninstall -g @openai/codex`
- ChatGPT Plus は cancel (月次)

### Week 3 rollback
- `.claude/agents/codex-reviewer.md` / `.claude/skills/codex-review/` 削除
- cron 追加していなければ scheduler.log への影響なし

**全 phase 通じて**: 既存 memory / source code / `.company/` 業務データには **物理的に変更を加えない設計** のため、rollback で復元すべき業務データなし。

---

## 9. Open Questions / Decisions Pending

user 確認待ち:
1. **ChatGPT Plus サブスク**: 既存ですか? 新規加入が必要?
2. **Obsidian 既存 install**: 既に user PC に入っていますか?
3. **GitHub アカウント**: W123 Decision C (Git 同期) で必要、既存ですか?
4. **vault path**: `C:\Users\gucch\obsidian-vault\` で OK ですか? 別 path 希望?
5. **Codex 認証方式**: ChatGPT login (user 個人) と API key (cron 用無人) のどちらから? Week 2 で API key 必要になったら user 操作必要

設計内決定済 (確認不要):
- W123 obsidian-skills 採用
- W124 Codex CLI 単独採用 (Copilot NG)
- W125 Bash 直叩き (MCP NG)
- Astro-Han 5 項目 selective 採用
- 3 週間逐次プラン

---

## 10. Open Loops (Month 2+)

- cron 化判断 (W125 拡大)
- MCP server / Codex App 再評価
- GitHub 同期 (W123 Decision C)
- memory/ 全体への lint 拡大
- 旧 memory ファイルへの frontmatter 拡張遡及適用 (やる場合のみ別 W)
- Pro $100/$200 昇格 / API key 移行 判断

---

## 改訂履歴

- 2026-05-14 初版 (Claude Opus 4.7) — code-architect review 待ち
