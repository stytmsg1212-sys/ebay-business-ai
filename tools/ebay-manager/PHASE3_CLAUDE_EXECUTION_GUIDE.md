# Phase 3 Claude 実行ガイド：同等商品検索と同等性判定

## 概要

このガイドは、`equivalence_check_tasks.json` に記録された在庫切れ商品について、
Claude が**同じ型番の商品を検索し、同等性を判定する**ための手順です。

---

## 入力：equivalence_check_tasks.json

```json
{
  "tasks": [
    {
      "sku": "ebayme_32400850054",
      "ebay_original": {
        "title": "ADVANTEST R8340A Ultra High Resistance Meter",
        "condition": "傷や汚れあり",
        "includes": "ボディのみ",
        "warranty": "ノークレーム",
        "price_jpy": 30000,
        "url": "https://...",
        "source": "メルカリ"
      },
      "model_number": "R8340A",
      "source": "メルカリ",
      "search_query": "R8340A site:mercari.com",
      "max_cost_price_jpy": 15000,
      "status": "awaiting_candidates"
    }
  ]
}
```

### 各フィールドの意味

| フィールド | 説明 |
|-----------|------|
| `sku` | 商品のSKU |
| `ebay_original` | eBayで出品中の商品の詳細情報（これが基準） |
| `model_number` | 検索対象の型番 |
| `source` | 仕入先プラットフォーム（メルカリ、Yahoo Auctions等） |
| `search_query` | 推奨される検索クエリ |
| `max_cost_price_jpy` | 財務的に可能な最大仕入価格（これ以下なら採用可能） |

---

## 実行手順

### ステップ1：タスクファイルを読み込み

```
data/equivalence_check_tasks.json を確認する
```

各タスクについて、以下の判定を行う：

- ✅ **モデル番号は何か** → `model_number`
- ✅ **仕入先はどこか** → `source` / `search_query`
- ✅ **元の商品の条件は** → `ebay_original` の condition, includes, warranty
- ✅ **最大仕入価格はいくら** → `max_cost_price_jpy`

### ステップ2：WebSearch で同じ型番を検索

**重要：同じ型番の商品を見つけることが目標**

```python
# 例1: メルカリで検索
query = "R8340A site:mercari.com 販売中"
# → Mercari で「R8340A」と出品中の商品を検索

# 例2: Yahoo Auctions で検索
query = "R8340A site:auctions.yahoo.co.jp"
# → Yahoo Auctions で「R8340A」を検索
```

**検索のコツ：**
- 仕入先ごとに異なる検索シンタックスを使用
- プラットフォーム名を明示（site:mercari.com など）
- 「販売中」「出品中」を含めて生きている商品を優先

### ステップ3：候補をWebFetch で詳細確認

各候補について以下を確認：

- 🔍 **商品の状態** → 「新品」「美品」「中古」「傷あり」など
- 🔍 **何が含まれるか** → 「本体のみ」「付属品完備」など
- 🔍 **保証** → 「ノークレーム」「保証あり」など
- 🔍 **価格** → ¥いくらか（仕入価格）
- 🔍 **販売状況** → 「販売中」「売り切れ」など

### ステップ4：同等性判定

**判定基準：**

```
元の商品（ebay_original）と候補を比較

1. 型番が同じ？ ✓（必須）
2. 商品の状態は同等以上か？
   - 元：「傷や汚れあり」 → 候補も「傷や汚れあり」または「中古」でOK
   - 元：「中古」 → 候補も「中古」または「美品」でOK
3. 付属品はそろっているか？
   - 元：「ボディのみ」 → 候補も「ボディのみ」でOK（本体さえあればOK）
   - 元：「付属品完備」 → 候補も「付属品完備」である必要がある
4. 保証は？
   - 元：「ノークレーム」 → 候補も「ノークレーム」でOK
5. 価格は予算内？
   - 候補の仕入価格 ≤ max_cost_price_jpy ？

すべての条件を満たす → スコア 0.9～1.0（採用可能）
条件をほぼ満たす → スコア 0.7～0.9（要確認）
条件を部分的に満たす → スコア 0.3～0.7（参考情報）
条件を満たさない → スコア 0.0～0.3（不採用）
```

### ステップ5：結果を保存

`product_search_results.json` に以下のフォーマットで保存：

```json
{
  "tasks": [
    {
      "sku": "ebayme_32400850054",
      "source": "メルカリ",
      "model_number": "R8340A",
      "search_date": "2026-04-11T10:30:00",
      "candidates": [
        {
          "rank": 1,
          "url": "https://jp.mercari.com/item/m87654321",
          "title": "ADVANTEST R8340A ユーザマニュアル無し",
          "condition": "傷や汚れあり",
          "includes": "ボディのみ",
          "warranty": "ノークレーム",
          "price_jpy": 12000,
          "score": 0.95,
          "reason": "型番同じ、状態同等、価格も予算内（¥12,000 < ¥15,000）。最適な候補。",
          "financially_viable": true
        },
        {
          "rank": 2,
          "url": "https://jp.mercari.com/item/m12345678",
          "title": "ADVANTEST R8340A",
          "condition": "中古",
          "includes": "ボディのみ",
          "warranty": "ノークレーム",
          "price_jpy": 18000,
          "score": 0.85,
          "reason": "型番同じ、状態は中古（元は傷あり）だが問題ない。ただし価格が予算超過（¥18,000 > ¥15,000）。",
          "financially_viable": false
        },
        {
          "rank": 3,
          "url": "https://jp.mercari.com/item/m99999999",
          "title": "ADVANTEST R8340A Complete Set",
          "condition": "美品",
          "includes": "付属品完備",
          "warranty": "保証あり（30日間）",
          "price_jpy": 25000,
          "score": 0.75,
          "reason": "型番同じ。状態・付属品は上質だが価格が大きく超過（¥25,000 >> ¥15,000）。参考情報。",
          "financially_viable": false
        }
      ],
      "summary": {
        "search_date": "2026-04-11",
        "total_candidates_found": 12,
        "candidates_evaluated": 3,
        "top_viable_candidate": {
          "rank": 1,
          "url": "https://jp.mercari.com/item/m87654321",
          "price_jpy": 12000,
          "score": 0.95
        },
        "recommendation": "採用推奨（Rank 1 の候補を仕入れることで在庫補充可能）"
      }
    }
  ]
}
```

---

## 実行例

### 例：ADVANTEST R8340A をメルカリで検索

1. **タスク内容確認**
   - SKU: `ebayme_32400850054`
   - 元の商品: 傷あり、ボディのみ、ノークレーム、¥30,000で販売
   - 最大仕入価格: ¥15,000

2. **検索クエリ実行**
   ```
   "R8340A site:mercari.com 販売中" を WebSearch で検索
   ```

3. **候補をWebFetch で詳細確認**
   - 複数の「R8340A」の出品を見つける
   - 各出品ページから：価格、商品説明、画像を確認
   - 状態（傷あり、中古、美品など）を判定

4. **スコア計算**
   - 見つかった候補と元の商品を比較
   - 同じ型番で、状態が同等以上、予算内 → スコア0.9～1.0
   - 型番は同じだが状態が良すぎて過剰 → スコア0.7～0.8
   - 型番は同じだが価格超過 → スコア0.5～0.7

5. **保存**
   - Top 3 候補を `product_search_results.json` に保存
   - 「採用推奨」のコメント付き

---

## 重要な注意事項

### 🎯 常に「同じ型番」を探すこと

- ❌ **ダメな例**: 「ADVANTEST R8340A」の代わりに「ADVANTEST R8340」を探す
- ✅ **良い例**: 「ADVANTEST R8340A」と完全に同じ型番を探す

### 💰 max_cost_price_jpy を厳守すること

- `financially_viable` フィールドで判定する
- `price_jpy <= max_cost_price_jpy` の候補のみ「採用推奨」にする
- 価格超過の候補は「参考情報」としてランク下げ

### 🌐 プラットフォーム固有の検索方法

| プラットフォーム | 検索シンタックス例 | 備考 |
|------------------|-------------------|------|
| Mercari | `型番 site:mercari.com` | 販売中に限定 |
| Yahoo Auctions | `型番 site:auctions.yahoo.co.jp` | 出品中に限定 |
| Rakuma | `型番 site:rakuma.rakuten.co.jp` | 販売中に限定 |
| PayPay フリマ | `型番 site:paypayfleamarket.yahoo.co.jp` | 販売中に限定 |

### ⚠️ 販売状況を確認すること

- 販売中/出品中の商品のみ採用候補にする
- 売り切れ、入札待機中は除外

---

## 次のステップ

1. `equivalence_check_tasks.json` を確認 ✓
2. 各タスクについてWebSearchで検索
3. WebFetchで詳細確認
4. 同等性を判定してスコア計算
5. `product_search_results.json` に保存
6. Discord に結果を投稿

---

## トラブルシューティング

### Q: 同じ型番の商品が見つからない

**A:**
- 検索クエリをシンプルに（型番だけ）してみる
- サイト指定を外してみる（site:mercari.com を削除）
- 「販売中」の条件を外してみる（売り切れも含める）
- スコアを下げて「参考情報」として保存

### Q: 複数の同等商品が見つかった場合

**A:**
- すべてをスコア順にソート
- Top 3 を保存
- スコア 0.9以上の候補が複数ある場合は、最新の出品を優先

### Q: 価格が大きく異なる候補が見つかった

**A:**
- 検索結果が間違っている可能性がある（別の型番）
- 型番が正しいか再確認
- eBay 出品価格（¥30,000）との乖離が大きい場合は、別の型番の可能性

---

**Status**: Phase 3 Claude 実行ガイド完成 ✓
**Next**: Claude による実際の検索実行
