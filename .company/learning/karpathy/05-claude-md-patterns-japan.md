---
title: CLAUDE.md 運用パターン (日本コミュニティ知見)
collected: 2026-05-14
source: Grok x_search (P2_memory_prompt)
tags: [claude-md, memory, hooks, japan-community, agent-memory]
related: [[00-index]] [[04-coding-agents-ecosystem]]
---

# CLAUDE.md 運用パターン (日本コミュニティ知見)

P2 検索の副産物。日本コミュニティで CLAUDE.md / MEMORY.md の運用パターンが急速に共有されている。

## "育てる" ループ (一番シンプル)

### @TomoCS_TomoCS (2026-05-13)
> Claude Code の育て方、今のところこれが一番うまくいってる。
> ① 案件を進める
> ② 問題が起きる
> ③ 修正する
> ④ CLAUDE.md か SKILL ファイルに「原因と対策」を記録
> ⑤ ① に戻る
> このループで、Claude Code がどんどん自分仕様に。
> AI を使うというより、一緒に仕事してる感覚に近い。

**我々の対応**:
- ① 〜 ⑤ は **既に実装済** (feedback_*.md / session_*.md / Q0-Q6 ルール追加)
- 既存運用は典型例として共有可能なレベル

## Pre-tool hooks で読み忘れ防止

### @engineer__117 (2026-05-13)
> Claude Code の hooks、地味だけど超便利でした。
> 「提案前に必読ファイル (CLAUDE.md とか設計メモ) を自動で読ませる」設定にしておくと、毎回コンテキスト渡し忘れる事故が一気に減りますね😊
> PreToolUse で Read を差し込むだけ。
> 人間が頑張って思い出すより、hooks に任せた方が確実でした💪

**我々の対応**:
- `.claude/hooks/quality-gate.sh` (PreToolUse) で physical block 運用済
- ただし「必読ファイル auto-read」用の hook は **未実装**
- 候補: session 開始時に MEMORY.md 必読ファイル群 (⭐⭐⭐) を auto-read

## Agentmemory で context 圧縮 → トークン激減

### @9aKYDTkbwn63955 (2026-05-13)
> これうちでも速攻で試した。セッション切れるたびに CLAUDE.md を手動コピペしてたのが馬鹿みたいに思える。
> Agentmemory 入れたら過去 240 回のやり取りを勝手に圧縮して引き継ぎしてくれる。実際にトークン使用量が月 $190→$24 に激減した。
> もうセッション管理で消耗することがない。

**示唆**:
- **Agentmemory** という ext は要調査 (W123 着手時に WebSearch 確認)
- 我々は既に SessionStart hook + `_NEXT_SESSION.md` で zero-paste 引継ぎ運用済
- トークン削減効果は CLAUDE.md auto-load の代替案として参考になる

## Opus 4.7 の "言うこと聞かない" 警告

### @chrisbailey87 (2026-05-13)
> Unfortunately not so much. You can have the best plan possible and Opus 4.7 will ignore the plan, ignore your prompt and ignore the Claude.MD.

**示唆**:
- 我々が今まさに使っている Opus 4.7 への警告コメント
- 対策: Q0-Q6 ルールを **「絶対遵守」「品質事故」と明示** + hook で physical block (既に実装済)
- それでも plan 無視は起こりうる、定期的に self-check 必要

## @HinaseShin1 (Claude Code vs Claude.ai)
> Claude、.ai より Code の方が何故かウィットに富んだ回答してくんだよね。。。ふしぎ！

**示唆**: 同一モデル (Opus 4.7) でも CLI と Web で振る舞いが違う。CLAUDE.md + hooks 効果の可能性大。

## 我々の運用との比較

| パターン | 我々の現状 | 日本コミュニティ知見 | gap |
|---|---|---|---|
| 育てるループ | feedback_*.md 自動追記 | TomoCS の 5 step | 既に同等 |
| Pre-tool hooks | quality-gate.sh (BLOCK only) | engineer__117 (Read 注入) | **必読 auto-read 未実装** |
| context 圧縮 | SessionStart hook + _NEXT_SESSION.md | Agentmemory ext | 別アプローチ、要評価 |
| Opus 4.7 規律 | Q0-Q6 ルール + hook | 警告のみ | 既に先行 |

## ROADMAP 候補 (副産物)

このセッションで明らかになった候補:

1. **必読ファイル auto-read hook**: SessionStart hook で MEMORY.md の ⭐⭐⭐ ファイル群を auto-read 化
2. **Agentmemory ext 検証**: 我々の _NEXT_SESSION.md 方式と比較
3. **Boris vibe coding 動画追加学習**: 既存 `learning_L3_claude_code_best_practices.md` に追補

ただし K1 (Simplicity First) の観点で、**「困ってから」追加** が原則。現状すでに動いている仕組みを置き換える積極的理由は今のところなし。
