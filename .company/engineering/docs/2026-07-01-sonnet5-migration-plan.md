# Sonnet 4.6 → Sonnet 5 移行 + effort 導入 計画 (Codex + Fugu レビュー済)

作成: 2026-07-01 / 状態: **user 承認待ち(実装未着手)** / model=Opus 4.8 窓口統合

## 0. 前提(公式 docs 2026-07 確定)
- ID `claude-sonnet-5`。価格 **導入 $2/$10 per MTok(〜2026-08-31)→ 通常 $3/$15**(=Sonnet 4.6 同額)。
- effort `low/medium/high/xhigh/max`、既定 `high`。指定 `output_config={"effort":"..."}`。
- adaptive thinking 既定オン。`thinking:{type:"disabled"}` で無効化。手動 `budget_tokens` は 400。
- **公式: Sonnet 5 medium ≈ Sonnet 4.6 high / high ≈ 4.6 max**。全 effort 帯で 4.6 を上回る。
- Haiku 4.5 は effort **非対応**。

## 1. 最終 model+effort 割り振り(Codex+Fugu 合意)
| 用途 | model | effort | 備考 |
|---|---|---|---|
| 仕入先評価(money-direct, `claude_evaluator`) | claude-sonnet-5 | **high** | 単段。誤buy損失≫モデル差額で品質マージン合理的。escalation二段化はK1 over-eng=別W |
| 出品文生成(`listing_generator`) | claude-sonnet-5 | **medium** | medium≈4.6 high で同品質、現行(effort未指定=既定high)よりコスト最適化 |
| supplier sweep(batch, `supplier_batch_evaluator`) | claude-sonnet-5(`CLAUDE_MODEL`継承) | high | L62変更で自動追従。L293 batch request に effort注入 + **Batches API×output_config 互換の実submit smoke必須** |
| bulk(rank/属性/keyword/要約 等) | Haiku 4.5 **据置** | — | effort非対応・最安・bulk |
| research heavy(`research_brain`/`opus_video_enricher`/`news_deep_dive`) | Opus 4.8 **現状維持** | effort付けない | xhighは新規挙動変更=今回scope外、別途検証 |

## 2. 価格運用(両者強く推奨=案A)
- **`_PRICING` に最初から `$3/$15` を入れる**(導入価格 $2/$10 を入れて9/1手動更新する案①は前回 api_logger 漏れの再発予備軍=却下)。
- 7-8月は実コスト~50%過大計上だが、単価~$0.008×低ボリュームで絶対額誤差。**過小報告ゼロ**最優先=安全側。予算forecastも$3/$15で。
- 精度が要るなら案B(date-window registry で両期間エンコード=9/1手動不要)も可だが dataclass+連続性validationが重い。要件「漏れ厳禁」なら案A。
- **sonnet-4-6 行は削除せず残す**(rollback容易性 + 過去ログ原価計算保持)。

## 3. 変更必須チェックリスト(漏れ厳禁・優先度順)
### HIGH(課金/利益/挙動に直結)
1. `monitor/api_logger.py:26-30` `_PRICING` に `"claude-sonnet-5": {input:3.0, output:15.0, cache_read:0.30, cache_write:3.75}` 追加。**最優先=$0事故の本丸**。batchはL57で×0.5自動。
2. `monitor/claude_evaluator.py:62` `CLAUDE_MODEL` → `claude-sonnet-5`(評価+batch両方が切替)。
3. `monitor/claude_evaluator.py:496` `messages.create` に `output_config={"effort":"high"}` 追加。
4. `monitor/listing_generator.py:84` `CLAUDE_MODEL` → `claude-sonnet-5`。
5. `monitor/listing_generator.py:723` `messages.create` に `output_config={"effort":"medium"}` 追加。
6. `monitor/supplier_batch_evaluator.py:293` batch request params に effort 注入 + **実 submit smoke**(unit不可、Batches API互換は実機のみ検出)。
7. **機械検証 pytest 新規追加**(再発の物理block): 全本番モデル定数(`claude_evaluator.CLAUDE_MODEL`/`listing_generator.CLAUDE_MODEL`)+ `_PRICING`/`_TIER1_INPUT_TOKENS_PER_MIN` 全キーについて `_estimate_cost_usd(id,1,1) > 0` を assert、かつ「本番id が `_PRICING` に存在」を assert。

### MED
8. `tabs/tab_model_comparison.py:104,169` SQL の `'claude-sonnet-4-6'` 固定 → 移行後 silent 空表示回避(両者発見の棚卸し漏れ)。
9. `tests/test_api_logger_batch_cost.py` / `test_w223_step3_eval_ledger.py:52` / `test_supplier_card_html_2026_06_04.py:243` に sonnet-5 ケース**追加**(既存sonnet-4-6行を残せば破損しない=追加のみ)。
10. `tests/test_supplier_batch_evaluator.py:231` に effort kwarg アサート追加。
11. SDK: `requirements.txt:13 anthropic>=0.89.0` が `output_config` 対応か確認・検証版に pin。realtime+batch両方smoke。
12. cascade(cascade-update.md 必須): `docs/model_selection_policy.md:21,46` / `.claude/rule-snippets/supplier-matching-rules.md:60-61`(実装の権威記述)/ memory `feedback_model_selection_policy.md`(新Sonnet検知→更新 mandate)/ CLAUDE.md・constitution Q6。

### LOW
13. `tabs/_supplier_card_html.py:205` バッジ `elif "sonnet" in m` → 全Sonnetが"Sonnet 4.6"誤表示。sonnet-5ラベル対応。
14. `monitor/supplier_batch_evaluator.py:255` docstring `claude-opus-4-7` 誤記訂正(実体sonnet、移行前から stale)。
15. `docs/ai_agent_architecture.html:358,368` ノードラベル。
16. `scripts/run_supplier_ab_test_2026_05_01.py:151` 第2 `_PRICING` コピー(dated one-shot、再実行時のみ要)= mental flag。

### Optional hardening(別検討・$0クラス根絶)
- `api_logger` 未知IDで $0 黙殺 → swallowing try の**外**で sentinel(`cost_status='pricing_error'` 等)+ Discord alert。**raise は try内に置かない**(except握りつぶしで行消失=Q0悪化)。

## 4. money-direct 安全
### H2 calibration drift
- model だけ替えると同プロンプト+閾値60固定で採用/却下境界が静かにズレる。
- 移行前に**既存A/B資産**(`supplier_candidates.eval_model` 列 + `tab_model_comparison` + `eval_ledger`)で shadow/突合。50-70バンドの判定不一致 + 高額・閾値近傍・画像欠落/期限切れ・ジャンク曖昧・型番曖昧 を重点確認。
- 新規canaryインフラ(段階rollout)は solo-operator tool には over-engineering=作らない(K1)。定数flip + eval_model記録継続 + 直近実ケースで buy/no-buy 突合。

### A1 landed-cost 算術を LLM に委ねない(Fugu追補・HIGH)
- FX/国際送料/燃油・容積重量/eBay手数料/Promoted/DDP関税/Section 232派生品 は **deterministic 計算**で出し、Sonnet 5 にはマッチ/コンディション/リスク判定のみさせる。effort を上げても「モデルが関税率を捏造」は直らない=money-direct の本丸。
- 移行作業時、supplier eval プロンプトが関税/着地原価の**数値計算を LLM に委ねていないか要確認**。委ねていれば deterministic 経路(`reference_shipping_tariff_logic.md`/Section 232辞書が権威)へ通すよう是正。これは Sonnet 5 移行とは独立の品質問題だが、money-direct 移行時に同時点検すべき。

## 4.5 機械検証スクリプトの実装注意(Fugu追補)
- Fugu サンプルの import (`app.api_logger`/`app.rate_limits`/`app.llm_config.LLM_CALLS`) は**実モジュール構成と不一致**(実際は `monitor.api_logger`、rate limit は `claude_evaluator._TIER1_INPUT_TOKENS_PER_MIN`、中央 `LLM_CALLS` registry は存在しない=各モジュール定数)。そのまま流用不可 → #7 の「本番定数 + `_TIER1` keys + `_PRICING` keys を集めて cross-assert」版で実装。
- 中央 LLM registry 新設(raw モデル文字列の集約)は 2-site 移行を超える refactor=K1 で今回 scope 外、**別W番号 ROADMAP 候補**。

## 5. 検証(Q1 DoD)
pytest(機械検証#7含む)/ Batches API 実submit smoke / 実DB SELECT(`api_call_log` で sonnet-5 の cost_usd>0 確認)/ 移行後 supplier eval 1件 live 突合 / Streamlit再起動で比較タブ表示確認。

## 6. 委譲プラン(承認後)
- 実装: `generator`(money-direct含むため Sonnet ではなく品質確保) → 単一PRで #1-7,11,12。
- レビュー: `code-reviewer` HIGH=0 ループ + `codex-reviewer` 2段(money-direct必須)。
- main(窓口)が統合判断 + Q1検証指示 + cascade(#12)整合確認。
- rollback: モジュール定数2つの revert で容易(sonnet-4-6の_PRICING/_TIER1行は残す)。

## 7. レビュー痕跡
- Explore 全数棚卸し(73.5k tok) → Codex(92k tok, hallucination 0) + Fugu(fugu-ultra) 並行 → main統合。
- 訂正された暫定案: ①Opus xhigh=新規変更(scope外) ②tier-map=graceful(任意) ③テスト=追加のみで非破損 ④Fugu raise案=本構造で逆効果。
