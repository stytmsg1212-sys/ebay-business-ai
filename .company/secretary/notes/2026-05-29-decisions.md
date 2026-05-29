# 意思決定ログ 2026-05-29

## [2026-05-29] モデル選定: Sonnet 据え置き継続 + 新 Sonnet 版で更新

**決定**: 現状 Sonnet 4.6 を使う全箇所 (supplier 評価 `candidate_evaluate`/`_batch` + `listing_generate`) を Opus 4.8 化せず **Sonnet 据え置き**。新 Sonnet バージョンがリリースされたら Sonnet を更新する。

**背景・根拠**:
- W86 A/B test (Opus 4.7 vs Sonnet 4.6、2026-05-01) で品質差が小さかった (user 体感)。
- 全 Sonnet→Opus 4.8 のコスト試算 (実 DB 直近 30 日):
  - Sonnet 現状 $51.35/月 → Opus 同トークン $85.58/月 = **+$34.23 (+67%)**
  - 新トークナイザ +35% 想定で最大 ~$115.5/月 = **+$64 (+125%)**
  - 差分の 95% は supplier 評価由来 (= 据え置き決定済)
- 品質差が小さいのにコスト +$34〜64/月 は見合わない。

**実装場所**: `tools/ebay-manager/monitor/claude_evaluator.py:62` `CLAUDE_MODEL = "claude-sonnet-4-6"` (変更なし)。

**記録先 memory**: `feedback_model_selection_policy.md` (確定方針 section) / `feedback_opus_price_watch.md` (新 Sonnet version watch)。

## [2026-05-29] 料金単価誤りの遡及補正は保留 (Q2 判断待ち)

`api_call_log` の Opus 4.7 履歴 3604 行が旧 $15/$75 誤単価で記録 → 3 倍過大 (stored $99.48 → 実 ~$33.16)。本番 cost カラムへの UPDATE は Q2 (6 step + user 承認) のため自動実行せず保留。`api_logger.py:_PRICING` の forward 修正は完了済 (今後の記録は正単価)。
