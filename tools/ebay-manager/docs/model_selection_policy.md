# モデル選定ポリシー (eBay Manager)

**最終更新**: 2026-04-19 / 2026-05-29 (価格訂正: Opus は 4.5 以降ずっと $5/$25、過去記録の $15/$75 は誤値) / 2026-07-01 (Sonnet 4.6 → Sonnet 5 移行 + effort 導入)  
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
| **仕入先候補評価** (match_score) | **claude-sonnet-5** (effort=high) | 画像比較+判断、money-direct で精度要求高い。誤 buy 損失 ≫ モデル差額 | `monitor/claude_evaluator.py` |
| **出品文生成** | **claude-sonnet-5** (effort=medium) | マルチ制約 (SEO/カテゴリ/Item Specifics)。medium ≈ 旧 4.6 high で同品質・コスト最適 | `monitor/listing_generator.py` |
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

## コスト目安 ($/1M tokens, 2026-05-29 訂正済 / Anthropic 公式照合)

| モデル | Input | Output | Cache Read | Cache Write |
|---|---|---|---|---|
| claude-opus-4-8 (現行) | $5.00 | $25.00 | $0.50 | $6.25 |
| claude-opus-4-7 | $5.00 | $25.00 | $0.50 | $6.25 |
| claude-sonnet-5 (現行 Sonnet) | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-sonnet-4-6 (旧・過去ログ用) | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-haiku-4-5 | $1.00 | $5.00 | $0.10 | $1.25 |
| gemini-2.5-pro | $1.25 | $10.00 | — | — |
| gemini-2.5-flash | $0.30 | $2.50 | — | — |

> ℹ️ **Sonnet 5 移行 (2026-07-01)**: `_PRICING` には最初から **通常価格 $3/$15** を登録 (公式の導入価格 $2/$10 〜2026-08-31 は登録しない = 9/1 手動更新漏れ = $0 事故予備軍の回避、過小報告ゼロ優先の安全側)。7-8 月は実コスト ~50% 過大計上だが単価 ~$0.008 × 低ボリュームで絶対額誤差。effort は `output_config={"effort": "high"|"medium"}` で指定。公式: Sonnet 5 medium ≈ Sonnet 4.6 high、high ≈ 4.6 max。sonnet-4-6 の `_PRICING`/`_TIER1` 行は rollback 容易性 + 過去ログ原価計算保持のため削除せず温存。
>
> ⚠️ **訂正 (2026-05-29)**: 旧版の「Opus 4.7 = $15/$75」は誤り。Opus は **4.5 以降ずっと $5/$25** で 4.8 値下げは無かった (Anthropic 公式 pricing 照合)。`monitor/api_logger.py:_PRICING` の誤値により過去 `api_call_log` の Opus 4.7 cost_usd は 3 倍過大に記録 (3604 行 / stored $99.48 → 実 ~$33.16、遡及補正は別途 Q2 判断)。fast mode は Opus $10/$50・batch 50% 引き。Opus 4.7 以降は新トークナイザで同一テキスト最大 +35% token。

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
