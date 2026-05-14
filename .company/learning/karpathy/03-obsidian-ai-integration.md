---
title: Obsidian + AI 統合パターン
collected: 2026-05-14
source: Grok x_search (P2_obsidian_md)
tags: [obsidian, claude-code, integration, vault, ai-agent]
related: [[00-index]] [[01-karpathy-llm-wiki-manifesto]] [[02-llm-wiki-implementations]]
---

# Obsidian + AI 統合パターン

W123 (Obsidian 連携) 設計の実装オプション集。

## Obsidian 公式の AI agent 化 (2026 春の大変革)

### Obsidian creator (@kepano) の新リリース

- **@RodmanAi (2026-05-01)** / **@cyrilXBT (2026-04-30)** が報告:
  > THE CREATOR OF OBSIDIAN JUST TURNED YOUR NOTE VAULT INTO AN AI AGENT.

- **新コンポーネント**:
  - `obsidian-markdown` — wikilinks / embeds / callouts / properties をネイティブ理解する SDK
  - `obsidian-bases` — smart database 生成
  - `obsidian-cli` — Claude (および他 AI) が vault を直接操作する CLI

- **インストール**: `npx skills add [link]` (詳細 URL は重要なので W123 着手時に WebSearch で確認)

**示唆**: 我々が **「Obsidian を AI と統合する仕組み」を自前で設計する必要はもうない**。公式 SDK を採用するのが最短経路。

### kepano 直接コメント (2026-04-02、Karpathy thread 内)

> More and more people are using Obsidian as a local wiki to read things your agents are researching and writing. It works best with a separate Obsidian vault that you can fill it with content, e.g. via Obsidian Web Clipper.

**重要**:
- 推奨: **agent 用 vault と人間用 vault を分離**
- 理由: contamination 防止 (agent が書いた未検証情報が人間ノートに混じるのを防ぐ)

## 実装 tutorial (時系列)

### @polydao "Claude + Obsidian は私の most-used setup" (2026-04-08)
- **URL**: https://x.com/polydao/status/2041952312898547900
- **3 step**:
  1. Claude が vault に read/write 直接アクセス
  2. note 検索 / file 作成 / 連携
  3. Obsidian = "second brain" 化

### @EXM7777 "Karpathy 流 prompt 公開" (2026-05-05)
- **URL**: https://x.com/EXM7777/status/2051724113266590075
- **Karpathy が使う prompt** (再現):
  ```
  dissect this raw note into atomic Obsidian markdown files...
  each file = one concept...
  use [[wikilinks]] between any concept that references another...
  output as separate code blocks with filenames
  ```
- **特徴**: no RAG, no vectors, $0
- **手順**: Claude に raw note を貼り → 上記 prompt → Obsidian CLI で vault 同期

### @cyrilXBT "AI second brain in 15 minutes" (2026-04-27)
- **URL**: https://x.com/cyrilXBT/status/2048682120328184263
- **手順**:
  1. Obsidian インストール
  2. 新 vault 作成
  3. `.md` ファイルを drop
  4. **Claude Code を Andrej Karpathy's prompt で vault に接続**

## 我々の現状ファイルとの相性確認

memory ファイル群の構文を Obsidian で開けるか:

| 我々の機能 | Obsidian 標準 | 互換性 |
|---|---|---|
| `[[name]]` link | wikilinks | ✅ 完全 |
| frontmatter (`name:` / `description:`) | properties | ✅ 完全 |
| MEMORY.md index | hub note | ✅ パターン一致 |
| `.md` ファイル群 | markdown notes | ✅ 完全 |
| 日付ベースの session_*.md | daily notes (Daily Notes plugin) | ✅ |
| `.company/secretary/notes/YYYY-MM-DD-*.md` | folder structure | ✅ |

**結論**: 既存の `memory/` と `.company/` を **そのまま Obsidian vault として開ける**。マイグレーションなしで W123 着手可能。

## kepano 警告に基づく vault 分離案

```
.company/learning/karpathy/  ← human-curated (read-only for Claude agents)
.company/learning/_agent_writes/  ← agent が自由に書く実験 vault
memory/                          ← Claude Code の auto-memory (現状)
.company/secretary/notes/        ← 日次決定・学び (claude + 人間両方が書く)
```

**判断保留点**: 我々の場合 user (=人間) が直接編集する頻度はまだ低い。1 vault で start → kepano 警告に該当する事故が起きたら分離、で十分かも。

## W123 設計の選択肢 (再掲)

| 案 | 構成 | 工数 | 推奨度 |
|---|---|---|---|
| **A1** | OneDrive 配下の既存 dir を Obsidian vault として開く (zero infra) | ~30 分 | ⭐⭐⭐⭐ |
| **A2** | A1 + `obsidian-cli` 追加で Claude Code 統合 | ~2h | ⭐⭐⭐⭐⭐ |
| **B** | Obsidian Sync ($10/月) で別 PC 同期 | ~30 分 + 月額 | ⭐⭐⭐ |
| **C** | git ベース同期 (Obsidian Git plugin) で GitHub と連動 | ~3h | ⭐⭐⭐⭐ (W124 と連動) |
| **D** | 自前 web UI (over-engineering) | 数日 | ⭐ |

**仮の最有力**: **A2 + C** (obsidian-cli で Claude Code 統合 + Git plugin で GitHub 同期)。これだと W124 (Codex) も同じ vault を見られる。
