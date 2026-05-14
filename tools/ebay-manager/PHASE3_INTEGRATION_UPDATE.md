# Phase 3 統合アップデート：最大仕入価格と実行ガイドの追加

**更新日**: 2026-04-11
**更新者**: Claude
**ステータス**: ✅ 完了

---

## 概要

Phase 3 に**財務的制約**と**Claude実行ガイド**を統合しました。
これにより、Claude が同等商品を検索する際に、単に「同じ型番」という基準だけでなく、
「eBay販売で利益が出る価格」という財務的制約を考慮した判定が可能になります。

---

## 追加・更新されたファイル

### 1. task_calculate_max_cost.py（新規作成）

**役割**: eBay販売価格から仕入先最大価格を逆算

```python
def calculate_max_cost_price(
    ebay_item: Dict,
    settings: Dict,
    target_profit_jpy: int = 3000,
    cost_adjustment_jpy: int = 0
) -> Dict:
```

**ロジック:**
1. eBay出品価格（USD）を取得
2. 商品の重量・寸法から送料を計算（複数サービスから最安を選択）
3. eBay手数料・税金を計算
4. 目標利益（¥3,000）を確保する逆算：
   ```
   max_cost_price = (sale_price_jpy - total_costs - target_profit)
   ```
5. 結果を返す

**統合先**: task_product_search.py の prepare_equivalence_check_tasks()

---

### 2. task_product_search.py（更新）

**変更点:**
- task_calculate_max_cost.py をインポート
- prepare_equivalence_check_tasks() に settings パラメータ追加
- 各タスクに max_cost_price_jpy フィールド追加
- run_product_search() で calculator.load_settings() を呼び出し

**新しいフロー:**
```
在庫切れ検知
   ↓
task_product_search で：
  1. 型番を抽出
  2. 検索クエリを生成
  3. 最大仕入価格を計算 ← NEW
   ↓
equivalence_check_tasks.json に保存：
  - model_number
  - search_query
  - max_cost_price_jpy ← NEW
   ↓
Claude が検索・判定
```

**equivalence_check_tasks.json の新しい構造:**
```json
{
  "tasks": [
    {
      "sku": "ebayme_32400850054",
      "model_number": "R8340A",
      "search_query": "R8340A site:mercari.com 販売中",
      "max_cost_price_jpy": 15000,
      "ebay_original": {
        "title": "ADVANTEST R8340A ...",
        "condition": "傷や汚れあり",
        "includes": "ボディのみ",
        "warranty": "ノークレーム",
        "price_jpy": 30000
      },
      "status": "awaiting_candidates"
    }
  ]
}
```

---

### 3. task_process_search_results.py（新規作成）

**役割**: Claude が見つけた検索結果を構造化して保存

**主要関数:**
```python
def save_search_results(
    sku: str,
    source: str,
    model_number: str,
    candidates: List[Dict],
    max_cost_price_jpy: float = 0
) -> Dict:
```

**機能:**
- 候補をスコア順（降順）にソート
- financially_viable フラグを設定（price_jpy <= max_cost_price_jpy）
- Top 3 候補を product_search_results.json に保存
- 推奨・非推奨を自動判定

**出力形式:**
```json
{
  "tasks": [
    {
      "sku": "ebayme_32400850054",
      "model_number": "R8340A",
      "search_date": "2026-04-11T10:30:00",
      "candidates": [
        {
          "rank": 1,
          "url": "https://jp.mercari.com/item/m87654321",
          "title": "ADVANTEST R8340A ...",
          "condition": "傷や汚れあり",
          "price_jpy": 12000,
          "score": 0.95,
          "reason": "型番同じ、状態同等、価格も予算内。最適な候補。",
          "financially_viable": true
        }
      ],
      "summary": {
        "top_viable_candidate": {...},
        "recommendation": "採用推奨"
      }
    }
  ]
}
```

---

### 4. PHASE3_CLAUDE_EXECUTION_GUIDE.md（新規作成）

**役割**: Claude が同等商品検索を実行するための詳細ガイド

**目次:**
- 📋 入力：equivalence_check_tasks.json の構造
- 🔍 実行手順（5ステップ）
- 📊 同等性判定基準（スコア計算方法）
- 💾 結果保存フォーマット
- 🎯 実行例（ADVANTEST R8340A の検索例）
- ⚠️ 重要な注意事項
- 🛠️ トラブルシューティング

**判定基準:**
```
スコア 0.9～1.0: 採用推奨（financially_viable = true）
スコア 0.7～0.9: 要確認
スコア 0.3～0.7: 参考情報
スコア 0.0～0.3: 不採用
```

---

### 5. test_phase3_integration.py（更新）

**変更点:**
- max_cost_price_jpy を表示するように更新
- 検索タスクファイルの確認時に型番と最大仕入価格を表示

---

## 実装流程の全体像

```
【定時実行】(5:00, 11:00, 17:00, 22:00)
   ↓
【タスク1】inventory_check (Selenium)
   在庫状態を確認 → inventory_check_results.json
   ↓
【タスク2】task_inventory_alert (Python)
   状態変化を検知 → alerts リスト生成
   ↓
【タスク3】task_product_search + task_calculate_max_cost (Python)
   ├─ 型番を抽出
   ├─ 検索クエリを生成
   ├─ calculator.py で最大仕入価格を逆算
   └─ equivalence_check_tasks.json に保存（max_cost_price_jpy付き）
   ↓
【Discord通知】
   「在庫切れ × N件、検索準備中」を投稿
   ↓
【タスク4】Claude による Web 検索（手動）← 📍 ここ
   PHASE3_CLAUDE_EXECUTION_GUIDE.md に従って：
   ├─ WebSearch で同じ型番を検索
   ├─ WebFetch で詳細確認
   ├─ max_cost_price_jpy を考慮して判定
   └─ スコア計算
   ↓
【タスク5】task_process_search_results で結果保存
   product_search_results.json に出力
   ↓
【Discord通知】
   「同等商品見つかりました」を投稿
```

---

## 利益計算ロジックの詳細

### 逆算の仕組み

1. **通常の利益計算:**
   ```
   profit = eBay_sale_price_jpy - total_costs - purchase_price_jpy
   ```

2. **逆算：最大仕入価格**
   ```
   max_purchase_price = eBay_sale_price_jpy - total_costs - target_profit

   例：
   - eBay販売価格: $49.99 ≈ ¥5,000（為替レート100で計算）
   - 送料・手数料: ¥2,000
   - 税金・その他: ¥500
   - 合計コスト: ¥2,500
   - 目標利益: ¥3,000

   → max_purchase_price = ¥5,000 - ¥2,500 - ¥3,000 = -¥500
   → 購買不可（利益が出ない）

   例2（高い販売価格）：
   - eBay販売価格: $500 ≈ ¥50,000
   - 送料・手数料: ¥5,000
   - 税金・その他: ¥1,000
   - 合計コスト: ¥6,000
   - 目標利益: ¥3,000

   → max_purchase_price = ¥50,000 - ¥6,000 - ¥3,000 = ¥41,000
   → ¥41,000 以下なら購買可能
   ```

### calculator.py との統合

task_calculate_max_cost.py は以下を利用：
- `CalcInput`: 商品スペック入力（重量、寸法、価格）
- `calculate()`: 複数の送料サービスから最適解を選択
- `load_settings()`: 為替レート、税率、手数料などの設定

```python
calc_input = CalcInput(
    purchase_yen=0.0,  # 仕入価格をゼロで計算
    item_price_usd=49.99,  # eBay販売価格
    weight_g=500,
    length_cm=10,
    width_cm=10,
    height_cm=10
)

calc_result = calculate(calc_input, settings)
# calc_result.ebay_cost_subtotal から総コストを取得
# → reverse_calculate で max_cost_price を算出
```

---

## テスト実行結果

```bash
$ python test_phase3_integration.py

【ステップ2】同等商品検索タスクを準備
----------------------------------------------------------------------
✅ 検索タスク準備完了
   準備件数: 2件

【ステップ4】準備された検索タスクの確認
----------------------------------------------------------------------
✅ 検索タスクファイル: data/equivalence_check_tasks.json
   タスク数: 2件

📋 SKU: ebayme_32400850054
   仕入先: メルカリ
   型番: R8340A
   検索クエリ: R8340A site:mercari.com
   最大仕入価格: ¥15,000

✅ フェーズ3 統合テスト完了！
```

---

## 次のステップ

### 短期（今すぐ）
1. **Claude による手動実行**
   - `data/equivalence_check_tasks.json` を確認
   - PHASE3_CLAUDE_EXECUTION_GUIDE.md に従って実行
   - 検索結果を product_search_results.json に保存

2. **結果確認**
   - product_search_results.json が正しく生成されたか確認
   - Discord に結果が投稿されたか確認

### 中期（フェーズ4以降）
1. **自動化拡張**
   - Claude API を統計して完全自動化（スクリプト内から Claude を呼び出し）
   - 定時実行時に自動的に検索・判定を実施

2. **その他の機能**
   - 複数候補の自動比較
   - 再検索の自動トリガー（候補が見つからない場合）
   - 価格推移の記録

---

## 技術的な考慮事項

### Why Calculator Integration?
- 単一の「仕入価格」では不十分
- 商品によって送料が大きく異なる
- 複数の送料サービスから最適なものを選ぶ必要がある
- eBay手数料、税金、関税を統一的に計算

### Why max_cost_price_jpy?
- Claude の判定をスコアリング可能に（financially_viable フラグ）
- 「同等」という曖昧な基準に「金銭的に実現可能」という客観的基準を追加
- 自動フィルタリング可能に

### Why Structured Guide?
- Claude が Web 検索する際の曖昧さを排除
- 判定基準の客観化（スコア 0.0～1.0）
- 結果の再現性・監査可能性を向上

---

## 既知の制限事項

1. **sku_conversion_results.json のデータ不足**
   - 一部商品に weight_g, length_cm, width_cm, height_cm が未設定
   - 対策：実際の運用時にデータを充実させる

2. **動的コンテンツの取得**
   - Mercari、Yahoo Auctions の検索結果が JavaScript で生成される
   - 対策：今後 Playwright 統合時に解決（技術的制約ログ参照）

3. **複数プラットフォームの統一 API**
   - 各プラットフォームで検索方法が異なる
   - 対策：PHASE3_CLAUDE_EXECUTION_GUIDE.md で各プラットフォームの方法を記載

---

## 実装の統計

| 項目 | 詳細 |
|------|------|
| 新規ファイル | 3個（task_calculate_max_cost.py, task_process_search_results.py, PHASE3_CLAUDE_EXECUTION_GUIDE.md） |
| 更新ファイル | 2個（task_product_search.py, test_phase3_integration.py） |
| 追加コード行数 | 700+ |
| テスト実行 | ✅ 成功 |

---

**Status**: Phase 3 統合完了 ✅
**Next**: Claude による手動検索実行 → 結果保存 → Discord 投稿
