---
title: Coding Agents Ecosystem (Claude / Codex / Cursor / Copilot)
collected: 2026-05-14
source: Grok x_search (P2_coding_tools)
tags: [claude-code, codex, cursor, copilot, multi-agent]
related: [[00-index]] [[05-claude-md-patterns-japan]]
---

# Coding Agents Ecosystem 最新動向

W124 (Codex 登録) / W125 (Codex code reviewer 化) 設計の論拠。

## 主要 milestone (時系列)

### 2026-02-26: Codex が GitHub Copilot に正式登場
- **@GHchangelog**: 
  > **Claude by Anthropic and OpenAI Codex are now available as coding agents for Copilot Business and Copilot Pro customers.**
- **URL**: https://github.blog/changelog/2026-02-26-claude-and-codex-now-available-for-copilot-business-pro-users/
- **意義**: **Claude と Codex が同じ Copilot ホスト下で共存可能**。我々の W124 / W125 は GitHub アカウント + Copilot Pro で実現可能ルート。

### 2026-05-11: Claude Code "agent view"
- **@claudeai**: 
  > New in Claude Code: agent view. One list of all your sessions, available today as a research preview.

**示唆**: Claude Code 自体が multi-session 横断管理を導入中。Codex を併用する際の見通しが立てやすくなる。

### 2026-05-13: Sam Altman, Codex 推し
- **@sama**: 
  > codex is the best AI coding product and we want to make it easy to try. for the next 30 days, we are giving companies that want to try switching over two months of free codex usage.

**示唆**: **2 ヶ月無料 trial** がある (2026-06-13 まで)。W124 着手時に user 適用可能か確認推奨。

### 2026-05-13: Boris (Claude Code 創設者) vibe coding live session
- **@0xMovez 報告**:
  > Creator of Claude Code just did a 30-minute Claude vibe-coding live session... 100% of Boris's code is written by Claude. It's the best vibe-coding masterclass...

**示唆**: 既存 memory `learning_L3_claude_code_best_practices.md` の Boris Tips に最新内容を追補可能。次セッションで動画 fetch 候補。

## Multi-agent パターンの登場

### Claude が Codex / Cursor を呼ぶ "agent coordination"
- **@_itsjustshubh (2026-05-09)** ×2 投稿:
  > Claude can spin up Codex or Cursor agents mid-session and delegate tasks to them. coding agents talking to each other from one terminal
  > Claude texts Codex or Cursor, delegates tasks, gets results back. one terminal, multiple agents

**示唆**: W125 の方向性 (Codex を code reviewer として Claude Code 経由で起動) は既に実装例が出ている。

### "Complete AI agency for Claude Code" (61 specialized agents)
- **@NirDiamantAI (2026-03-11)**:
  > There's a public repo that's basically a complete AI agency for Claude Code: **61 specialized agents** across engineering, design, marketing, product, testing, and more... Also works with Cursor, Windsurf, Aider, and Gemini CLI.

**示唆**: 我々の `.claude/agents/` (現状 ~20 個) を更に展開する余地がある。ただし K1 (Simplicity First) を守るため、必要になったら追加が原則。

### 商用ツール: Production context for agents
- **@star_yutish (2026-05-13)**:
  > World's first tool that gives AI coding agents full production context... Connect your production stack once, and now Claude Code, Cursor, Codex, or any AI coding agent can: inspect logs / analyze production databases

**示唆**: 我々の場合、`logs/scheduler.log` / `monitor.db` への agent 読込は既に手動でやっている。ツール化価値あり (ROADMAP 候補)。

### マルチ agent 監視: @Saboo_Shubham_ (2025-08)
> Run and monitor Claude Code, Cursor, GitHub Copilot, and other AI Agents from literally anywhere. Get real-time visibility into what your agents are doing...

## 性能比較 (@natolambert, ML researcher 2025-07-23)

> The gaps between Claude Code over Cursor Agents over Github Copilot for basic scripting, while using the same underlying model, is bonkers. **Copilot barely works. Cursor is okay but frustrating (and slower). Claude Code usually just works fast.**

**示唆**: 我々が Claude Code を main にしているのは正解。Codex はあくまで **second opinion** / **code reviewer** 用途で導入し、main は Claude Code 継続が現実的。

## W124 / W125 の設計示唆

### W124 (Codex 登録)
- **入口**: GitHub Copilot Pro ($10/月) or OpenAI API 直接 or 単独 codex CLI
- **比較ポイント**:
  - Copilot 経由 → Claude と共通インターフェース、GitHub 統合容易
  - 単独 CLI → カスタマイズしやすいが連携実装が必要
- **trial 機会**: 2026-06-13 まで 2 ヶ月無料 (sama 発表)

### W125 (Codex code reviewer 化)
- **既存事例**: agent coordination (Claude が Codex を呼ぶ) は既に複数実装
- **核心**: Codex に何を review させるか
  - 案 a: Claude が書いたコード全般 (code-reviewer agent の Codex 版)
  - 案 b: Karpathy "Lint" 操作 (memory wiki の矛盾 / stale 検出)
  - 案 c: 両方
- **推奨**: 当面 **a** から開始、運用後に **b** を追加。一気に両方やると K1 違反

## 質問残し (W124 着手前に user 協議)

1. **GitHub アカウント** は user 既存ですか? W124 で同時に GitHub 導入する場合の前提
2. **Copilot Pro 月額 $10** は cost 投入可能ですか? 単独 CLI なら別 cost 構造
3. **Codex に review させる対象** はソースコードのみ? memory も含む?
