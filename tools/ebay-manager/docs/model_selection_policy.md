# モデル選定ポリシー (eBay Manager)

**最終更新**: 2026-04-19  
**適用範囲**: tasks/ + monitor/ 配下の全 Claude/Gemini API 呼出

## 方針

各用途に応じた最小コスト・最大精度のモデルを標準化する。決定基準:
- 高度な判断・意思決定が必要 → **Sonnet**（または Opus）
- 構造化された分類/要約/翻訳 → **Haiku**
- 視覚情報（動画/複数画像）が必要 → **Gemini 2.5 Flash**（無料枠活用）
- 超重要な商品評価の決定打 → **Opus** (月数件)

## 用途別標準

| 用途 | 推奨モデル | 理由 | 実装箇所 |
|---|---|---|---|
| メール日本語要約・分類 | claude-haiku-4-5 | 構造化出力、低コスト | `monitor/claude_summarizer.py` |
| ニュース要約・影響判定 | claude-haiku-4-5 | 同上 | `monitor/claude_summarizer.py` |
| 商品 weight 推定 | claude-haiku-4-5 | テキストから数値推定、軽量 | `tasks/task_estimate_weights_claude.py` |
| **仕入先候補評価** (match_score) | **claude-sonnet-4-6** | 画像比較+判断、精度要求高い | `monitor/claude_evaluator.py` |
| 動画から構造化知識抽出 | **gemini-2.5-flash** | 動画ネイティブ対応、無料枠 | `monitor/gemini_video_learner.py` |
| 高品質な動画解析が必要な時 | gemini-2.5-pro (env切替) | 精度優先、数倍コスト | 同上 (`GEMINI_MODEL=gemini-2.5-pro`) |

## 切替ルール

### Haiku → Sonnet への格上げ条件
- 出力 JSON のパース失敗率が 3% を超える
- 判定の誤りがビジネス影響を生むケース（仕入判定など）
- タスク数月 < 100 かつ単価重要度高い

### Sonnet → Opus への格上げ条件
- Sonnet で判定信頼度 (confidence) が低いと自己申告するケースが 10% 以上
- 特定の重要決定だけ Opus にフォールバック（例: 利益 $500 超の候補評価）

### Gemini Flash → Pro への切替
- 無料枠を超えた場合、`.env` に `GEMINI_MODEL=gemini-2.5-pro` を設定
- 精度が必要な動画（コンサル内容の核心など）だけ選択的に Pro

## コスト目安 ($/1M tokens, 2026-04)

| モデル | Input | Output | Cache Read | Cache Write |
|---|---|---|---|---|
| claude-opus-4-7 | $15.00 | $75.00 | $1.50 | $18.75 |
| claude-sonnet-4-6 | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-haiku-4-5 | $1.00 | $5.00 | $0.10 | $1.25 |
| gemini-2.5-pro | $1.25 | $10.00 | — | — |
| gemini-2.5-flash | $0.30 | $2.50 | — | — |

## 監視

各 API 呼出は `monitor.api_logger.log_anthropic_response()` / `log_gemini_response()` で
`api_call_log` テーブルに記録。eBay Manager「エージェント監視」タブで:
- 日別コスト推移
- モデル別内訳
- エラー一覧
- operation 別稼働率

が確認可能。月次コスト目安: **$5 未満 / 月**（現状の呼出ボリューム基準）。

## レビュー頻度

- 月初に モデル使用状況を確認、異常な増減があれば本ポリシーを更新
- Anthropic/Google の新モデル公開時は本ドキュメントを更新し、切替を検討
