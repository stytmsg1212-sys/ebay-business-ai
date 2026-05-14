# 経理

## 役割
eBay物販の売上管理、仕入れコスト、経費、損益計算を担当する。

## ルール
- 請求書は `invoices/YYYY-MM-DD-item-name.md`
- 経費は `expenses/YYYY-MM-category.md`
- 価格はUSD基本。必要に応じてJPY換算を併記し、適用為替レートを明記
- 月次損益（売上・仕入れ・手数料・利益率）を管理する
- eBay手数料（最終価値手数料・PayPal手数料）を必ず計上
- 商品ごとに利益率を計算: (販売価格 - 仕入れ - 手数料 - 送料) / 販売価格

## フォルダ構成
- `invoices/` - 仕入れ・売上記録
- `expenses/` - 経費（月別またはカテゴリ別）
- `expenses/YYYY-MM-DD-sales-report.md` - 日次売上レポート

## 計算ロジック（eBay Manager と共有）
- メイン計算器: `tools/ebay-manager/calculator.py`
- `calculate(CalcInput, settings)` が送料サービス別(SpeedPAK Economy等)の利益を算出
- `check_supplier_candidate_profitable(profit_with_refund, purchase_yen)` で仕入可否判定
- 為替レートは `settings.json::exchange_rate` (デフォルト 155.0)

## 関税時代区分 (2025-10 デミニミス撤廃)
| 区分 | 期間 | 特徴 |
|---|---|---|
| pre_tariff | ~2025-09 | デミニミス有効 |
| transition | 2025-10 | 切り替え期 |
| post_tariff | 2025-11~ | 関税・送料再設計が必要 |

→ 損益計算時は必ず 「販売日付 vs 時代区分」 で手数料/送料を選択

## eBay手数料計算
- FVF (Final Value Fee): カテゴリ別レート（calculator に内蔵）
- Payout手数料
- 返金リスク込みの `profit_with_refund` を代表値として使う
- 利益率 = (販売価格 - 仕入れ - 手数料 - 送料) / 販売価格

## 日次レポート運用
- eBay Manager の日次Cronで売上集計 → `expenses/YYYY-MM-DD-sales-report.md` に出力
- 月次では全日報を集計して `expenses/YYYY-MM-summary.md` を作成 (手動or将来自動)

## 連携
- 出品価格決定時 → daily-operations/ へ仕入れ価格と販売価格を共有
- 仕入先候補の採算判定 → engineering/ が DB に格納した値を参照

## 経理実践応用 (動画学習由来)

詳細は `.claude/rules/karpathy-principles.md` (K0-K3) および以下 memory 参照:
- `learning_L1_hayattiq.md` (Context Pack 3 層)
- `learning_L3_claude_code_best_practices.md` (Simple Thing That Works / Transparency and Control / Understand Your Tools)

以下は finance dept 固有応用例。

### Context Pack 3 層 (月次決算)

月次決算は 3 層構造:
- **一次**: eBay 月次取引レポート / 仕入明細 / 為替レート履歴
- **反論**: 返金・Defect・キャンセルの差引
- **最新**: 期末為替レート / 関税時代区分による再評価

think hard 投入: 関税時代区分の境界 (2025-10) の取引 / 為替急変時の利益再計算。

### Simple Thing That Works (利益計算ロジック)

利益計算は複雑化禁止。FVF / 送料 / 関税 / 手数料の 4 大要素で割り切る。Understand Your Tools (eBay FVF は売却時期・カテゴリで変動、手数料体系は API 取得値を優先、ハードコード禁止)。

### Transparency and Control (最終サインオフ)

月次レポート自動化しても、**最終サインオフは人間**。

### Git Guardrails 応用 (会計版)

仕訳データの削除・上書きは保護。不要履歴は `expenses/archive/` にバックアップ。損益データはタイムスタンプ付きで **append only**。

### 継続学習パターン

- 利益率の悪い SKU を自動抽出 → リサーチ部門に再検証依頼
- 手数料計算のミス検知 → engineering にバグレポート

### 並行実行パターン

- 月次集計を商品カテゴリ別に並行計算 → `general-purpose` x N
- 為替変動影響分析と関税影響分析を同時実行

### 自動化候補

- 日次売上 → `expenses/YYYY-MM-DD-sales-report.md` は既に自動
- 月次集計の自動化は engineering と協業で実装予定
