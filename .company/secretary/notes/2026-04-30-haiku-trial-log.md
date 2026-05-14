# Haiku Explorer trial 観測ログ (2026-04-30 〜 2026-05-07)

## trial 概要

`Agent` tool の `subagent_type=Explore` (read-only) を **Haiku 4.5** で呼び出した時の観測ログ。判断指針 / 失敗判定基準 / ふりかえり項目は `feedback_haiku_explorer_trial_2026_04_30.md` 参照。

- **trial 期間**: 2026-04-30 〜 2026-05-07 (7 日間)
- **記録方法**: Explore Haiku 呼出毎に 1 行追記 (date / call site / 結果妥当性 ○/△/✕)
- **失敗判定**: 誤検出 ✕ 3 件以上 / 7 日間 OR user 体感低下 1 件で **即中止 + Sonnet 復帰**

---

## 観測ログ (追記式)

### 形式

```
| YYYY-MM-DD HH:MM | Glob/Read/Grep | call site (簡潔に 1 行) | ○/△/✕ | comment |
```

凡例:
- ○ = 妥当 (期待通りの結果取得、Sonnet と同等品質)
- △ = 微妙 (結果は得たが浅い / 1-2 round 余計、許容範囲)
- ✕ = 誤検出 (重要漏れ / 結果誤解釈、即 Sonnet 復帰必要)

### ログ本体

| 日時 | tool 内訳 | call site | 判定 | comment |
|---|---|---|---|---|
| (まだ呼出なし) | | | | trial 開始日 (2026-04-30)、初回呼出時に追記 |

---

## 7 日間集計 (2026-05-07 ふりかえり時に記入)

| 項目 | 計測値 | 判定 |
|---|---|---|
| Haiku 呼出回数 | (count) | target 70%+ |
| Sonnet 維持回数 | (count) | target 30%- |
| 誤検出件数 (✕) | (count) | 0-2 OK / 3+ 中止 |
| user 体感 | 1-5 | 低下なら中止 |
| API コスト delta | (推定 USD) | Haiku 1/4 単価 |

**採用判定**: ⬜ フル採用 / ⬜ 一部採用 (Glob/Read 単純のみ) / ⬜ 中止

---

## 判定根拠 (ふりかえり時記入)

(7 日間の傾向、Sonnet 維持判断の妥当性、誤検出の典型パターン、コスト削減効果、user 主観評価 等)

---

## 関連 memory

- `feedback_haiku_explorer_trial_2026_04_30.md` (trial 仕様 / 判定基準 / 採用 ROADMAP)
- `feedback_model_selection_policy.md` (W42、本 trial の根拠)
- `~/.claude/CLAUDE.md` (W2-D10-S1 ツールルーティング規約、Explore agent 推奨)
