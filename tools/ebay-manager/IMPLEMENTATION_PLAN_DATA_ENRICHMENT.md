# eBay出品データ自動抽出：実装計画

**目的**: sku_conversion_results.json に weight, dimensions, price, condition などのデータを自動追加

**優先度**: 🔴 高（本番運用の前提条件）

---

## 📋 実装スケープ

### 対象データ
```
348件の仕入先商品（sourced）から以下を抽出:
  ✅ weight_g          - 商品重量（グラム）
  ✅ length_cm         - 商品サイズ（長さ）
  ✅ width_cm          - 商品サイズ（幅）
  ✅ height_cm         - 商品サイズ（高さ）
  ✅ item_price_usd    - eBay販売価格（USD）
  ✅ condition         - 商品状態（新品/中古/傷あり等）
  ✅ includes          - 付属品（本体のみ/完備等）
  ✅ warranty          - 保証（ノークレーム/保証あり等）
```

### 実装方式の比較

| 方式 | 難度 | 速度 | 精度 | 保守性 |
|------|------|------|------|--------|
| **eBay API** | 中 | 速 | 高 | 高 |
| **Selenium** | 低 | 遅 | 中 | 低 |
| **BeautifulSoup** | 低 | 速 | 低 | 低 |

**推奨: eBay API** （既にプロジェクトで使用中だから）

---

## 🔧 実装詳細

### 方式1: eBay API を使用（推奨）

#### 必要なもの
```
- eBay API credentials (既に設定済みと想定)
- ebay-python-sdk または requests ライブラリ
- ebay_id から出品情報を取得するエンドポイント
```

#### 実装ステップ
```
1. ebay_id から GetItem API を呼び出し
2. 返された XML/JSON をパース
3. 以下を抽出:
   - Weight → weight_g
   - Dimensions → length_cm, width_cm, height_cm
   - StartPrice → item_price_usd
   - Condition → condition
   - Description → includes, warranty を判定
4. sku_conversion_results.json に書き込み
5. ログ記録
```

#### 実装ファイル
```
新規作成:
  - tasks/task_enrich_ebay_data.py (250-300行)

修正:
  - daily_scheduler.py (統合)
  - config/schedule_config.json (新タスク追加)
```

---

### 方式2: 代替案：Selenium でスクレイピング

既にプロジェクトで Selenium が使用されているので、
eBay出品ページをスクレイピングすることも可能です。

```python
# ページ読み込み
driver.get(f"https://www.ebay.com/itm/{ebay_id}")

# 情報抽出
weight = driver.find_element(...).text  # Weight
price = driver.find_element(...).text    # Price
condition = driver.find_element(...).text # Item condition
```

欠点: 遅い（1件あたり2-3秒）、ブロックのリスク

---

## 📊 実装フロー

```
【方式1: eBay API】

task_enrich_ebay_data.py
├─ load_sku_conversion_results()
├─ for each sourced item:
│  ├─ ebay_id から GetItem API 呼び出し
│  ├─ XML をパース
│  ├─ weight_g, dimensions, price, condition 抽出
│  ├─ description から includes, warranty 判定
│  └─ sku_conversion_results.json に書き込み
├─ エラー時は logging
└─ 結果をサマリー表示

実行タイミング:
  - 初回: manual `python tasks/task_enrich_ebay_data.py`
  - 定期: daily_scheduler に追加（週1回等）
  - 追加時: 新商品追加のたびに実行
```

---

## 🛠️ 実装計画

### フェーズ1: 準備（30分）
```
☐ eBay API credentials を確認
☐ API 呼び出しテスト
☐ エラーハンドリング戦略を検討
```

### フェーズ2: 実装（1-2時間）
```
☐ task_enrich_ebay_data.py を作成
☐ GetItem API 呼び出しロジック実装
☐ データパース＆抽出ロジック実装
☐ ログ出力実装
☐ ユニットテスト作成
```

### フェーズ3: テスト（30分）
```
☐ 小規模テスト（5件）で動作確認
☐ エラーハンドリング確認
☐ 結果の品質確認
```

### フェーズ4: 本運用（1時間）
```
☐ 全348件を処理
☐ ログをチェック
☐ sku_conversion_results.json を検証
☐ calculator.py で正常動作を確認
```

---

## 📝 コード構造（案）

### task_enrich_ebay_data.py

```python
#!/usr/bin/env python3

"""
eBay出品データを自動抽出して sku_conversion_results.json に追加
"""

import ebay  # eBay SDK
import json
import logging
from pathlib import Path

def get_ebay_item_details(ebay_id: str, api_context) -> Dict:
    """
    eBay GetItem API で出品情報を取得

    Returns:
        {
            'weight_g': float,
            'length_cm': float,
            'width_cm': float,
            'height_cm': float,
            'item_price_usd': float,
            'condition': str,
            'includes': str,
            'warranty': str
        }
    """
    # eBay API 呼び出し
    request = ebay.shopping.GetItemRequest()
    request.ItemID = ebay_id

    response = api_context.execute(request)

    # パース＆抽出
    item = response.Item
    weight_g = item.ShippingInfo.WeightMajor * 453.592  # lbs to grams

    # ... 他のフィールドも抽出

    return {
        'weight_g': weight_g,
        'length_cm': ...,
        'width_cm': ...,
        'height_cm': ...,
        'item_price_usd': float(item.CurrentPrice.value),
        'condition': item.Condition.DisplayName,
        'includes': parse_includes(item.Description),
        'warranty': parse_warranty(item.Description),
    }

def enrich_sku_conversion_results():
    """
    sku_conversion_results.json を読み込んで、
    各 sourced item にデータを追加
    """
    # ファイル読み込み
    with open('data/sku_conversion_results.json') as f:
        data = json.load(f)

    sourced = data['sourced']

    # 各商品について情報を取得
    for idx, item in enumerate(sourced):
        ebay_id = item['ebay_id']

        try:
            # eBay API で情報取得
            details = get_ebay_item_details(ebay_id, api_context)

            # マージ
            item.update(details)

            logging.info(f"✓ {idx+1}/{len(sourced)}: {ebay_id}")

        except Exception as e:
            logging.warning(f"✗ {idx+1}/{len(sourced)}: {ebay_id} - {e}")

    # ファイル保存
    with open('data/sku_conversion_results.json', 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logging.info(f"完了: {len(sourced)}件処理")

if __name__ == '__main__':
    enrich_sku_conversion_results()
```

---

## ⚠️ 注意事項

### API Rate Limiting
```
eBay API は rate limiting がある
  - 標準: 5000 calls/day
  - 348件 = OK（1日以内）

対策:
  - バッチ処理で間隔を空ける
  - 失敗時のリトライ機構
  - ログで監視
```

### エラーハンドリング
```
考えられるエラー:
  1. API 接続エラー → スキップして継続
  2. パースエラー → デフォルト値を設定
  3. 出品終了済み → データなし をマーク
  4. API キー無効 → 例外発生して中断
```

### パフォーマンス
```
348件の処理時間:
  - API 呼び出し: ~0.5秒/件 = 174秒（3分）
  - データ抽出: ~0.1秒/件 = 35秒
  - ファイル保存: ~1秒

合計: 約 4-5分
```

---

## 🧪 テスト計画

### ユニットテスト
```python
def test_parse_condition():
    assert parse_condition("Brand New") == "新品"
    assert parse_condition("Like New") == "ほぼ新品"
    assert parse_condition("Used") == "中古"

def test_parse_includes():
    assert parse_includes("...本体のみ...") == "本体のみ"
    assert parse_includes("...付属品完備...") == "付属品完備"
```

### 統合テスト
```bash
# 5件でテスト
python tasks/task_enrich_ebay_data.py --limit 5

# 結果確認
python -c "
import json
with open('data/sku_conversion_results.json') as f:
    data = json.load(f)
item = data['sourced'][0]
print(f'weight_g: {item.get(\"weight_g\")}')
print(f'item_price_usd: {item.get(\"item_price_usd\")}')
"
```

---

## 📅 スケジュール

| フェーズ | 作業 | 時間 | 予定 |
|---------|------|------|------|
| 1 | 準備 | 30分 | 本日中 |
| 2 | 実装 | 1-2h | 本日中 |
| 3 | テスト | 30分 | 本日中 |
| 4 | 本運用 | 1h | 本日中 |

**予定完了: 本日夜 ✅**

---

## 📦 成果物

実装後:

```
新規ファイル:
  ✅ tasks/task_enrich_ebay_data.py (300行)
  ✅ test_enrich_data.py (100行)

修正ファイル:
  ✅ daily_scheduler.py (統合)
  ✅ schedule_config.json (設定追加)
  ✅ sku_conversion_results.json (データ充実)

ドキュメント:
  ✅ 実装ドキュメント
  ✅ 運用手順

結果:
  ✅ 348件のデータが100%充実
  ✅ calculator.py で max_cost_price 計算可能に
  ✅ Phase 3 が完全に動作可能に
```

---

**Next Action**: task_enrich_ebay_data.py の実装開始

