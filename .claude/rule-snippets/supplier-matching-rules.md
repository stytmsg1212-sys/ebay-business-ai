---
name: supplier-matching-rules
description: 仕入先候補の置き換え/別出品判定ルール (match_score < 60 除外 + 別 SKU 機会 + ジャンク表記判別)
type: rule
---

# 仕入先候補マッチングルール (常時適用)

出典: 2026-04-19 supplier 候補機能 W7 設計時。eBay 出品中商品との「置き換え可能な完全同一物」評価ルール。

## 除外条件 (match_score < 60)

以下に **1 つでも該当** したら仕入先として **不採用**:

- 色違い
- 容量・サイズ違い
- 付属品の有無が違う
- 新品 / 中古 / ジャンク等の状態違い

**Why**: 仕入先の置き換えは顧客から見えないため、SKU の実体変更は許容不可。同一商品の純粋な仕入元差し替えのみが対象。

## 「除外 ≠ 破棄」: 別 SKU 出品機会として拾う

除外と判定されても、以下は **追加 SKU の出品機会** として `alt_listing_possible=1` で記録:

| 除外理由 | 別 SKU 機会 |
|---|---|
| 付属品欠落 | 箱なし別 SKU として出品 |
| 色違い・容量違い | 追加 SKU として拡販 |
| 動作未確認ジャンク | テスト後に出品 (下記参照) |

DB: `alt_listing_possible=1` + `alt_listing_note` に具体提案を格納。

## 「ジャンク」表記の 2 種類判別

仕入先の「ジャンク」「動作未確認」表記は 2 種類:

### (A) 動作確認が面倒で「ジャンク扱い」している実質動作品

**Sign** (以下のいずれか):
- 「動作確認していない」「通電のみ確認」「ノークレームノーリターン」
- **具体的故障記述なし**

**判定**: `junk_likely_untested=true` で記録 → **テスト後再出品の機会** として評価。

### (B) 具体的故障 (液晶割れ / 電源入らず等)

**判定**: `junk_likely_untested=false`、`match_score < 40` で除外。

## ブランド別特例

- **PIONEER Lonesome Carboy 等年代物 AV**: ジャンク表記は (B) 扱い、即 As-Is 判定
- **KEYENCE センサー単体等**: 基板単体ならジャンクでも B/C テスト前提で検討可
- 詳細: `feedback_condition_by_brand.md` (eBay 出品コンディションランクと連動)

## 実装連動

- 判定ロジック: `monitor/claude_evaluator.py::STABLE_PROMPT_TEMPLATE`
- DB schema: `supplier_candidates` テーブル (migration v5 で追加) の `alt_listing_possible` / `alt_listing_note` / `junk_likely_untested` カラム
- **現状 (2026-05-29〜)**: `supplier_evaluate` は **Sonnet 4.6** で運用 (`claude_evaluator.py::CLAUDE_MODEL = "claude-sonnet-4-6"` が真実)。2026-05-29 Opus 4.8 移行時も user 判断で Sonnet 据え置き (コスト対効果)。
- **過去 (〜2026-05-05)**: W25 で「Opus escalation = supplier_evaluate Opus 全置換」を検討したが、2026-05-02 W94 + 2026-05-05 Sonnet 切替で解消済。`feedback_w25_supplier_opus_review_pending.md` は ⛔ SUPERSEDED (historical only)。

## 未確定項目 (次回ヒアリング候補)

- 並行輸入品・海外版の扱い (現状は `alt_listing_note` にその旨記載のみ)
- match_score 閾値 60 の見直し (W25 ヒアリング時に併せて、`feedback_supplier_threshold_hearing.md` 連動)
