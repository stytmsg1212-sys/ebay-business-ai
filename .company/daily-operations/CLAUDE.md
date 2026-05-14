# 日々業務

## 役割
eBay商品の出品、在庫管理、注文処理、カスタマーサポートを担当する。

## ルール
- 出品情報は `listings/SKU-name.md`（SKUで管理）
- 注文記録は `orders/YYYY-MM-DD-order-id.md`
- カスタマー対応は `customer-support/YYYY-MM-DD-case.md`
- カスタマー返信は24時間以内を目標
- Defect率を低く保つことを最優先
- 出品時に必ず記録: SKU・在庫数・仕入れ価格・販売価格・利益率

## KPI管理
- 月次売上、利益率、販売数量、Feedback評価を追跡

## フォルダ構成
- `listings/` - 出品商品情報（1SKU1ファイル）
- `orders/` - 注文処理記録
- `customer-support/` - カスタマー対応履歴
- `logs/YYYY-MM-DD.md` - 日次運用ログ

## eBay Manager 連携
- 在庫監視: `tasks/task_inventory_check.py` (05:00/17:00 定時 + OOS即時再スキャン)
- 仕入先候補URL: 在庫監視テーブルの 候補URL1/2/3 カラムに自動表示
- メール三分類: Claude Haiku による優先度判定 → ダッシュボード表示 (summary_ja/action_ja/buyer_message_ja)
- OOS検知→候補探索自動起動
- 日次 End→Relist SEO ブースト: 7件/日 自動化

## 出品運用フロー
1. リサーチで候補発見 → research/notes/ に記録
2. 仕入れ判断 → 仕入先候補に 採用/不採用 (今後UI実装予定、現状 CLI)
3. 出品: `ebay-listing/` で英語生成 → eBay Manager でドラフト作成
4. 在庫監視に自動登録 (SKUベース)
5. OOS検知 → 候補再探索 or 出品終了
6. 売却 → finance/ に記録

## カスタマー返信ルール
- 返信は 24時間以内を目標
- Defect 率を低く保つことを最優先
- Claude Haiku で buyer_message の和訳＋返信候補を自動生成 (未実装: W4 Claudeチャット機能)

## KPI管理
- 月次売上、利益率、販売数量、Feedback評価
- finance/expenses/YYYY-MM-DD-sales-report.md に日次反映

## 古物台帳
- 中古品仕入れのみ対象（新品仕入れは記録不要）

## 日々業務実践応用 (動画学習由来)

詳細は `.claude/rules/karpathy-principles.md` (K0-K3) および以下 memory 参照:
- `learning_L1_hayattiq.md` (Context Pack 3 層 / Grok)
- `learning_L4_nobel_824.md` (プラットフォーム別レビュアー / 並行実行)

以下は daily-operations dept 固有応用例。

### Context Pack 3 層 (バイヤー返信)

バイヤー問い合わせ返信時は 3 層参照:
- **一次**: 注文履歴・商品ページ・トラッキング情報
- **反論**: 過去の類似クレーム・否定的レビュー
- **最新**: 配送状況・関税時代区分

think hard 投入: 訴訟リスク / Defect 影響が懸念される返信。

### Transparency and Control (破壊的 UI 操作)

在庫大量更新・価格一括変更・大量出品停止・全件削除は必ずユーザー承認後に実行。事前確認ダイアログ必須。Simple Thing That Works (仮想ケース事前実装禁止)。

### Claude Haiku メール三分類

Claude Haiku でメール三分類 → 自動返信下書き、**送信は必ず人間承認**。

### メール種別別エージェント

メール種別ごとに専用エージェント化可能:
- Feedback Request / Order Inquiry / Return Request / Defect Escalation
- 各エージェントが適切なテンプレ + 商品コンテキストで下書き生成

### 並行実行パターン

- 複数バイヤー問い合わせ返信下書き → `general-purpose` x5 並行
- OOS 検知時の候補探索は既に自動並列化済 (`tasks/task_supplier_candidate_search` 呼び出し)

### KPI 自動レビュー

週次で Feedback 評価・Defect 率・返信所要時間を集計し、改善点を `logs/YYYY-Wxx-kpi-review.md`。
