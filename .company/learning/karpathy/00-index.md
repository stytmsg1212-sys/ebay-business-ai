---
title: Karpathy X Archive (W123-W125 学習基盤)
collected: 2026-05-14
source: Grok x_search via search-x skill
scope: Karpathy 本人投稿 + 重要 reply / 引用 (LLM Wiki / Obsidian / Claude Code / Codex 周辺)
related: [[W123 Obsidian]] [[W124 Codex]] [[W125 Codex reviewer]]
---

# Karpathy X Archive — Index

W123 (Obsidian 連携) / W124 (OpenAI Codex 登録) / W125 (Codex code reviewer 化) 設計の論拠として、Andrej Karpathy (@karpathy) と関連投稿を Grok 経由で収集した。

## 収集スコープ

- **P1 (指定 post)**: [LLM Knowledge Bases manifesto](https://x.com/karpathy/status/2039805659525644595) (2026-04-02) + 主要 reply 5 件
- **P2 (直近 6 ヶ月)**: 6 テーマで Karpathy 周辺投稿 ~50 件
- **P3 (全期間)**: user 指示によりキュレーション不要 (_raw/ に raw 保管のみ)

## ファイル構成

| ファイル | 内容 | W との関連 |
|---|---|---|
| [01-karpathy-llm-wiki-manifesto.md](01-karpathy-llm-wiki-manifesto.md) | Karpathy 本人の LLM Wiki 提唱投稿 (一次資料) | W123 設計の核 |
| [02-llm-wiki-implementations.md](02-llm-wiki-implementations.md) | Tolaria / LLM Wiki repo / gbrain など実装事例 | W123 参考実装 |
| [03-obsidian-ai-integration.md](03-obsidian-ai-integration.md) | Obsidian + Claude Code 統合 (obsidian-cli / kepano / 各種 tutorial) | W123 実装パターン |
| [04-coding-agents-ecosystem.md](04-coding-agents-ecosystem.md) | Claude / Codex / Cursor / Copilot の最新動向 | W124 / W125 直結 |
| [05-claude-md-patterns-japan.md](05-claude-md-patterns-japan.md) | 日本コミュニティの CLAUDE.md 運用知見 | 既存 memory 運用への影響 |

raw 出力は [`_raw/`](_raw/) 配下 (12 ファイル、`P1_*.md` / `P2_*.md` / `P3_*.md`)。

## 最重要 takeaway

1. **我々の memory アーキテクチャは既に Karpathy 流 LLM Wiki**: `CLAUDE.md` (schema) + `memory/*.md` (wiki layer) + 業務 source (raw) の 3 層構造を既に運用している。Obsidian は wiki layer の **編集 UI** を追加するだけで、設計思想は変わらない
2. **Obsidian + Claude Code は既に標準的な組み合わせ**: kepano (Obsidian co-creator) も AI agent 連携を進めており、`obsidian-cli` / `obsidian-markdown` SDK が出ている。Claude Code から Obsidian vault に直接読み書きする tutorial が複数存在
3. **Codex は Copilot 経由で既に Claude と共存可能**: GitHub Changelog (2026-02) で Claude + Codex の両方が Copilot Business/Pro で利用可能と発表。multi-agent (Claude が Codex を呼ぶ) パターンも登場
4. **CLAUDE.md は日本コミュニティで「育てる」運用パターンが定着**: 案件中の問題発生 → CLAUDE.md / SKILL に「原因と対策」を記録、というループが共有されている
5. **vibe coding → agentic engineering へのパラダイム移行中**: Karpathy が AI Ascent 2026 で「思考は agent に外注できるが、理解は外注できない」と語った。我々の memory + review 体制 (W125) はこの方向と一致

## 次の action

- [ ] W123 Obsidian 連携設計に [03-obsidian-ai-integration.md](03-obsidian-ai-integration.md) の `obsidian-cli` を採用するか検討
- [ ] W124 Codex 登録は [04-coding-agents-ecosystem.md](04-coding-agents-ecosystem.md) の Copilot 経由か、独立 CLI かを user と協議
- [ ] W125 Codex code reviewer 化は Karpathy "Lint" 操作 (gist 参照) を参考に設計

## staleness 警告

- Karpathy 投稿は **2026-04-02 〜 2026-05-13** が中心。本 archive は **2026-05-14 取得** で fresh
- Obsidian-cli / Tolaria 等の tool は提唱 1-2 週間内のため、本番採用前に GitHub の最新状況再確認 (W123 着手時)
