---
name: karpathy-principles
description: Andrej Karpathy 流 4 原則 (Think Before Coding / Simplicity First / Surgical Changes / Goal-Driven). 全プロジェクト常時適用、Boris 30 Tips の上位 meta レイヤ
type: rule
---

# Karpathy 4 原則 (常時適用)

出典: https://github.com/forrestchang/andrej-karpathy-skills

## K0. Think Before Coding

**核心**: "Don't assume. Don't hide confusion. Surface tradeoffs."

- 仮定を明示。複数解釈を user に提示。混乱を抱えたまま進まない
- 不明点は clarification を求める。「単純な代替」「要求自体への challenge」も提示する

**違反例**: 「Opus にやらせて」と言われ Gemini で完了報告 / 動画学習 / pythonw 環境を assume

## K1. Simplicity First

**核心**: "Minimum code that solves the problem. Nothing speculative."

- 要求外の機能・抽象化・configurability・defensive error handling を **足さない**
- 単発コードに抽象化禁止。3 回出てから共通化
- Self-check: シニアエンジニアが見て over-engineered か?

**違反例**: 「daily_relist 7 件」で「将来 14 件にするかも」とパラメータ化 / 関税 1 件追加にファクトリ

## K2. Surgical Changes

**核心**: "Touch only what you must. Clean up only your own mess."

- 関係ない code/comment/formatting を「ついでに直す」禁止
- 動いている code の opportunistic refactor 禁止。既存 dead code は flag のみ
- 自分の変更で生まれた unused のみ削除
- Test: 修正した全行が user 要求に直接 trace できるか?

**違反例**: hour ドリフト修正 ついでに inventory_check 改善 / Streamlit hot reload 破綻 (連続変更 scope 越え)

## K3. Goal-Driven Execution

**核心**: "Define success criteria. Loop until verified."

- 抽象タスク → measurable goal に変換。修正前に reproducible test
- 変更前後で outcome verify。pytest PASS のみで完了宣言禁止

**違反例**: 送料 20% 反映を eBay GetItem で verify せず pytest だけで完了 / 「画像合成キャッシュ」を curl ログで確認せず

## 適用優先度

1. **K3 Goal-Driven** = 最優先 (測定可能な成功基準なき完了報告を自分で禁止)
2. **K0 Think Before** = ambiguity を抱えたら停止
3. **K2 Surgical** = scope 超え変更を自分で禁止
4. **K1 Simplicity** = 既存「Simple Thing That Works」と整合

## Boris Tips との対応

| Karpathy | 対応 Boris Tips |
|----------|----------------|
| K0 | Tip 1 / Tip 15 / Tip 18 |
| K1 | "Simple Thing That Works" / Tip 4 |
| K2 | Tip 1 / Tip 27 |
| K3 | Tip 2 / Tip 24 / Tip 28 |
