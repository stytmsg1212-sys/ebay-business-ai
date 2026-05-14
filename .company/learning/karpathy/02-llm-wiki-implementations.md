---
title: LLM Wiki 実装事例
collected: 2026-05-14
source: Grok x_search (P1 + P2_wiki_rag)
tags: [llm-wiki, implementation, obsidian, rag, knowledge-base]
related: [[00-index]] [[01-karpathy-llm-wiki-manifesto]] [[03-obsidian-ai-integration]]
---

# LLM Wiki 実装事例

Karpathy manifesto (2026-04-02) から **6 週間以内** に登場した実装と派生概念。

## 直接的な Karpathy LLM Wiki 実装

### Tolaria (Mac app, 2026-05-13)
- **投稿者**: @Sumanth_077
- **URL**: https://x.com/Sumanth_077/status/2054566469036613811
- **特徴**:
  - git-backed markdown KB (各変更が git commit)
  - **offline first** (ローカル動作、クラウド依存なし)
  - AI-friendly (LLM が読み書き前提の構造)
- **示唆**: 我々の `.company/learning/` ディレクトリ + git 管理化で同等機能を実現可能

### LLM Wiki v0.2.1 (open-source)
- **投稿者**: @ToninoPalmisano (2026-04-24)
- **URL**: https://x.com/ToninoPalmisano/status/2047810522301493666
- **特徴**: Karpathy の `llm-wiki.md` idea file を直接参照、Markdown-first workflow
- **示唆**: GitHub repo を W123 着手時に確認 (採用 or 参考実装)

### gbrain by Garry Tan (YC CEO)
- **投稿者**: @garrytan (2026-04-18)
- **URL**: https://x.com/garrytan/status/2045447417290727616
- **GitHub**: github.com/garrytan/gbrain
- **特徴**:
  - Karpathy markdown を **system-of-record** として採用
  - 上に Graph RAG + Vector Search + retrieval を載せた
  - OpenClaw / Hermes Agent 統合
  - **free, open source**
- **示唆**: 単純な markdown を超えて、構造化検索を後付けする方向性

### LLM Wiki by @RodmanAi (2026-04-27)
- **URL**: https://x.com/RodmanAi/status/2048665408165859594
- **メッセージ**:
  > Not retrieval → Not RAG → Not search. It evolves your knowledge.
- **特徴**: 「compounding knowledge base」を明示的に標榜

## 派生概念

### Context Engineering (RAG の次)

- **@helloiamleonie**: "Is context engineering the new RAG?" (2026-04-09) — 6 components: Query Augmentation / Retrieval / Memory / Agents / Tools / Prompting
- **@femke_plantinga**: "Context Engineering is the most underrated skill in AI development" (2025-12-25)
- **@ingliguori**: "RAG is already becoming the 'old way' 🤯 The future of AI memory is not retrieval. It's compilation... The new model? **LLM Wiki**" (2026-05-10)

**論点**:
- RAG = query 時に chunks を retrieve
- LLM Wiki = ingest 時に LLM が一度 compile、query 時は wiki だけ参照
- **コスト構造が逆転**: RAG = query 時に高負荷、Wiki = ingest 時に高負荷、定常運用は安い

### Garry Tan "Thin Harness, Fat Skills" 哲学

- **@toto_pm 経由**: Y Combinator CEO Garry Tan のエッセイ
- **核心**: AI agent で 10-100x 生産性を出すには、**薄い harness + 厚い skill** が必要
  - Thin harness = 過剰設定なしの軽い実行環境
  - Fat skills = 累積した手順・知識・パターン
- **我々への対応**: `.claude/agents/*.md` + `.claude/skills/` で既に実践、Karpathy 流 LLM Wiki と統合する余地あり

## 我々の選択肢 (W123 設計時)

| 案 | 内容 | コスト | リスク |
|---|---|---|---|
| **A** | `memory/` をそのまま Obsidian vault として開く (zero infra) | 最小 | kepano 警告 (agent vs 人間 vault 分離未対応) |
| **B** | LLM Wiki v0.2.1 / Tolaria を試験導入 | 中 | 立ち上がり時期で API/仕様 unstable リスク |
| **C** | gbrain 風に既存 memory + Graph RAG 追加 | 高 | over-engineering、現状の MEMORY.md インデックスで足りる可能性高 |
| **D** | Karpathy 流 wiki を **ゼロから自前構築** | 最高 | NIH。先行事例の学習価値捨てる |

**仮の推奨**: A → 不足が見えたら B 検討。C/D は 1-3 ヶ月運用後に再判断。
