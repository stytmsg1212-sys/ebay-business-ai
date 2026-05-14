# リサーチ

## 役割
商品リサーチ、競合分析、市場トレンド調査、仕入れ先調査を行い、結果をまとめる。

## ルール
- 調査ファイルは `topics/topic-name.md`
- ステータス: planning → in-progress → completed
- 情報源は必ずURLまたは出典を記載
- 調査結果には必ず「結論」と「ネクストアクション」を含める
- 商品リサーチには必ず: 需要・競合数・価格帯・利益見込みを記載
- 調査完了時は秘書のTODOに報告を追記

## フォルダ構成
- `topics/` - 調査トピック（1トピック1ファイル）
- `notes/YYYY-MM-DD-rival-report.md` - 競合レポート（日次）
- `notes/YYYY-MM-DD-new-product-research.md` - 新商品リサーチ（日次）

## 現在の自動化基盤
- **eBay Manager 連携**:
  - `tasks/task_rival_check.py` - 既存出品の競合スキャン
  - `tasks/task_supplier_candidate_search.py` - OOS発生時に仕入先候補を自動探索 (Mercari/ヤフオク/PayPayフリマ)
  - `monitor/gemini_video_learner.py` - 動画資料をGemini 2.5 Flashで構造化知識化
- **Playwright MCP** で認証済みブラウザ経由の精密スクレイピングが可能
- **Claude API** で match_score(0-100) 判定（Phase 1: ユーザー accept/reject 履歴を Few-shot 注入）

## プラットフォーム特性（重要）
- **Mercari**: カジュアル層中心
- **ヤフオク**: 業務寄り (KEYENCE等産業機器はヤフオクのみHit正常)
- **PayPayフリマ**: カジュアル寄り
→ 商品ジャンルでプラットフォーム選別を明示する

## 関税時代区分 (2025-10 デミニミス撤廃)
- `pre_tariff` / `transition` / `post_tariff` の3区分
- 送料・関税・価格知識は**日付必須考慮**
- 古い記事・動画を参照する際は必ず記載日付を確認

## 商品別動作確認ポリシー（feedback_condition_by_brand）
- 年代物AV/可動部多: 動作確認必須、ジャンク即不採用（例: PIONEER Lonesome Carboy KP-717G 等）
- 電子基板単体(KEYENCE FL-001等): ジャンクでも採用検討可
- これらは Claude 評価プロンプト＋過去判断履歴で自動反映される

## アウトプット先
- 新機能提案は `secretary/inbox/YYYY-MM-DD.md` に投入
- 商品選定結果は `daily-operations/listings/` に SKU別に保存
- 仕入れ可否判断は eBay Manager の supplier_candidates テーブルに自動永続化

## リサーチ実践応用 (動画学習由来)

詳細は `.claude/rules/karpathy-principles.md` (K0-K3) および以下 memory 参照:
- `learning_L1_hayattiq.md` (Context Pack 3 層 / Grok)
- `learning_L3_claude_code_best_practices.md` (Discover WF)
- `learning_L4_nobel_824.md` (継続学習)

以下は research dept 固有応用例。

### Context Pack 3 層構造 (リサーチ出力 template)

`topics/商品名.md` は 3 層構造で統一:
- **一次**: 公式ソース (メーカー / eBay sold / Mercari 成約)
- **反論**: ネガ情報 (返品理由 / 類似品敗因 / VeRO 警告)
- **最新**: 発売日 / 改訂日付 (関税時代区分)

`## 一次ソース / ## 反論 / ## 最新日付` の見出しテンプレで統一。

### Grok X 検索 (商品トレンド)

X/Twitter の商品話題は Grok 経由 (Claude 直接困難)。トレンド検知時 `notes/YYYY-MM-DD-trend-alerts.md` 投入。

### 過去調査参照 (重複避け)

新商品リサーチ時はまず関連過去 `topics/*` を Claude に読ませる。「このジャンルの過去調査から学んだポイント 3 つ」で土台確認 → 深掘り。

### 継続学習パターン

- 仕入れ判断 accept/reject (`supplier_candidates`) を定期レビュー → match_score 閾値見直し (`feedback_supplier_threshold_hearing.md`)
- プラットフォーム特性更新 (`reference_platform_user_profile.md`)

### 並行実行 / think hard

- 複数商品同時リサーチ = `general-purpose` x5 並行
- 動画資料は `gemini_video_learner` で構造化
- think hard 投入: VeRO リスク / 関税時代区分境界 / 新興プラットフォーム特性
