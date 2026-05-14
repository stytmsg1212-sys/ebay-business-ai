---
title: Karpathy LLM Wiki Manifesto (一次資料)
date: 2026-04-02
author: Andrej Karpathy (@karpathy)
url: https://x.com/karpathy/status/2039805659525644595
gist: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
engagement: 20M+ views
collected: 2026-05-14
tags: [llm-wiki, knowledge-base, markdown, obsidian, foundation]
related: [[00-index]] [[02-llm-wiki-implementations]] [[03-obsidian-ai-integration]]
---

# Karpathy LLM Wiki Manifesto

W123-W125 設計の **一次論拠**。Andrej Karpathy 本人による LLM Knowledge Base アーキテクチャ提唱。

## 原文要旨 (X 投稿 + gist 統合)

> **LLM Knowledge Bases**
>
> Something I'm finding very useful recently: using LLMs to build personal knowledge bases for various topics of research interest. In this way, a large fraction of my recent token throughput is going less into manipulating code, and more into manipulating knowledge (stored as markdown and images). The latest LLMs are quite good at it.

### Karpathy の運用 (本人記述)

**Data ingest**:
- `raw/` ディレクトリに source document (記事 / 論文 / repo / dataset / 画像) を index
- LLM が incremental に "compile" して **wiki** = `.md` ファイル群を生成
- wiki には raw データの要約 + backlink + 概念分類 + 記事生成 を含む

**3 層アーキテクチャ** (gist より):
1. **Raw sources** (immutable): 元データ (PDF / web 記事 / GitHub / 画像)
2. **Wiki layer** (LLM-maintained): markdown で構造化された知識
3. **Schema** (configuration): `CLAUDE.md` 的な構造定義・workflow 指針

**3 つの操作**:
- **Ingest**: 新 source → wiki を 10-15 ページ同時更新
- **Query**: raw を読まず wiki だけ参照、citation 付きで合成回答
- **Lint**: 定期 health check (矛盾検出 / stale claim / orphan page)

### なぜ機能するか (Karpathy + コメント陣の補強)

- Vannevar Bush (1945) Memex 構想の現代化。**従来 wiki は maintenance burden で頓挫した** が、LLM が cross-reference / consistency 検査を肩代わりすることで、人間は curation / analysis に集中できる
- RAG (raw 都度検索) → **compilation (事前統合)** へのパラダイム移行
- 「compounding knowledge base」: 読むほどに価値が累積する構造

## 主要 reply スレッド

### @lexfridman (Lex Fridman)
> Same, I have a similar setup. A mix of Obsidian, Cursor (for md), and vibe-coded web terminals... [podcast-specific extensions, dynamic HTML, mini-knowledge-bases for runs]

**意義**: 著名 podcaster も同様の構成 (Obsidian + Cursor + 自作ツール) を運用。Karpathy の提案が孤立した思想でなく、実践的合意点になりつつある。

### @kepano (Obsidian co-creator)
> I like this approach because it mitigates the contamination risks of agent-generated content... the agents need a playground too! [quoting own post on Obsidian vaults]

**意義**: Obsidian 作者本人が AI 連携を肯定。**agent 用 vault と人間用 vault を分離** すべきという contamination リスク警告は重要。

### @himanshustwts (図解版)
> Here is the full architecture of the LLM Knowledge Base system covering every stage from ingest to future explorations. [diagram]

### @omarsar0 (elvis)
> I have also been obsessed with building LLM knowledge bases. Here is one example... LLMs are excellent at curating and searching... [video demo + semantic diagram]

### @Sumanth_077 (Tolaria 紹介)
> Karpathy's LLM wiki idea just became a real Mac app! Tolaria... git-backed markdown KB, offline, AI-friendly. (2026-05-13, 投稿 1 ヶ月で実装化)

### @ToninoPalmisano (LLM Wiki v0.2.1)
> Published LLM Wiki v0.2.1. A Markdown-first workflow... Inspired by @karpathy llm-wiki.md idea file. (2026-04-24, GitHub repo)

## 我々の現状との対応

| Karpathy 用語 | 我々の現状 (eBay 物販プロジェクト) | 状態 |
|---|---|---|
| Raw sources | `tools/ebay-manager/*.py`, eBay API 応答, 動画 source, FedEx 通関書類 | ✅ あり |
| Wiki layer | `memory/*.md`, `.company/ebay-knowledge/`, `.company/secretary/notes/` | ✅ 既に運用 |
| Schema | `CLAUDE.md` (project root + subdir), `.claude/rules/*.md` | ✅ 既に運用 |
| Ingest 操作 | session-close skill (新セッション内容 → session_*.md 化) | ✅ 自動化済 |
| Query 操作 | SessionStart hook + MEMORY.md auto-load | ✅ 自動化済 |
| Lint 操作 | R-9 (SUPERSEDED 自動検出) のみ部分実装 | 🟡 **W125 で本格化** |

**結論**: 我々は Karpathy 構想の **約 80% を既に実装している**。W123-W125 で残り (UI = Obsidian / 第二 reviewer = Codex / Lint 強化) を補完する位置付け。

## W123-W125 への示唆

1. **W123 (Obsidian)**: Wiki layer の編集 UI 追加。`memory/` をそのまま Obsidian vault として開けば最小工数で導入可能。kepano 警告に従い「agent 書込 vault」と「人間編集 vault」を分けるか検討
2. **W124 (Codex)**: Karpathy gist には Codex 言及なし。Codex は別ルートで追加するが、wiki への Ingest / Query 経路は共通化すべき
3. **W125 (Codex reviewer)**: Karpathy が定義した **Lint 操作** (矛盾検出 / stale 検出 / orphan 検出) を体系化する好機。既存の R-9 を拡張
