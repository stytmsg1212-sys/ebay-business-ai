# フェーズ3: 在庫切れ検知 + AI 商品検索 - 完成報告

**完成日**: 2026-04-11
**ステータス**: ✅ 基本機能実装完了
**前フェーズ**: フェーズ2 完成（eBay同期 + 在庫チェック）

---

## 🔄 重要な方針転換

**当初の計画**: 在庫切れ商品の候補仕入先を複数条件でスコアリング

**ユーザーの実需**:
> 「本仕組みの目的は...在庫切れになった商品と同等の商品をピックアップしておいてほしいです。
> スクリプトではなくAIのあなた自身がネットサーフィンして見つけてくるイメージです」

**新しい実装方針**:
1. **検知 (Detection)** - 在庫状態の変化を検知
2. **抽出 (Extraction)** - 商品情報を詳細に抽出
3. **AI検索 (Web Surfing)** - Claude が実際にウェブを検索して同等商品を発見（別エージェント）

---

## 実装内容

### 1️⃣ **在庫状態変化検知** ✅
**ファイル**: `tasks/task_inventory_alert.py` (200+行)

#### 検知対象（2つのパターン）

```
在庫有 → 在庫無 (became_out_of_stock)
在庫有 → ページなし (became_page_not_found)
```

#### 主要関数

**`detect_inventory_status_changes(inventory_data: dict) -> Dict[str, List]`**
- 前回結果との比較で状態変化を検知
- 2つのパターンを別々に抽出
- 返却形式:
  ```json
  {
    "became_out_of_stock": [...],
    "became_page_not_found": [...]
  }
  ```

**`extract_product_details(item: dict) -> dict`**
- SKU, 仕入先, URL, 状態変化, 検知時刻を抽出
- AI検索エージェント用の情報を整形
- 返却形式:
  ```json
  {
    "sku": "ebayme_32400850054",
    "source": "メルカリ",
    "url": "https://jp.mercari.com/item/m32400850054",
    "status_change": "在庫有 → 在庫無",
    "changed_at": "2026-04-11T10:00:00",
    "product_name": "レアアイテム",
    "product_description": "..."
  }
  ```

**`run_inventory_alert(config) -> dict`**
- 在庫チェック結果を読み込み
- 状態変化を検知
- 商品詳細を抽出
- 返却形式:
  ```json
  {
    "success": true,
    "alert_count": 2,
    "alerts": [
      { "sku": "...", "source": "...", "url": "...", ... }
    ]
  }
  ```

---

### 2️⃣ **テスト実装** ✅
**ファイル**: `test_phase3.py` (更新)

#### テスト内容

1. **状態変化検知テスト** ✅
   - 「在庫有 → 在庫無」「在庫有 → ページなし」の検知確認
   - 仕入先別の統計

2. **Discord通知テスト** ✅
   - 検知結果を Discord 埋め込みで投稿
   - 「AI がネットサーフィンで同等商品を検索中」のステータス表示

3. **サンプルデータテスト** ✅
   - `test_inventory_alert_manual.py` で検知ロジック検証
   - 2件の状態変化を正確に検知、詳細抽出確認

#### テスト実行結果

```
✅ 在庫有 → 在庫無: 1件
   - ebayme_32400850054: メルカリ
✅ 在庫有 → ページなし: 1件
   - ebayyh_g1225005638: Yahoo Auctions

✅ 商品詳細抽出:
   SKU: ebayme_32400850054
   仕入先: メルカリ
   URL: https://jp.mercari.com/item/m32400850054
   状態変化: 在庫有 → 在庫無
   検知時刻: 2026-04-11T10:00:00
   商品名: レアアイテム A
```

---

## 🔄 フェーズ3 データフロー

```
【定時実行時刻: 5:00, 11:00, 17:00, 22:00】
    ↓
【task_inventory_check.py 実行（フェーズ2）】
├─ Selenium で仕入先チェック (348件)
├─ 在庫有/無/ページなし判定
├─ 前回結果と比較
└─ inventory_check_results.json 更新
    ↓
【task_inventory_alert.py 実行（フェーズ3新）】
├─ 「在庫有 → 在庫無」を検知
├─ 「在庫有 → ページなし」を検知
├─ 商品情報を詳細抽出
└─ alerts リスト生成
    ↓
【AI検索エージェント（次ステップ）】
├─ 検知された各商品について
├─ Mercari, Yahoo Auctions, Rakuma 等で同等商品を検索
├─ 価格・状態・仕様の類似度を判定
└─ 上位 3～5件の候補を提案
    ↓
【Discord 通知】
└─ 検知商品 + 同等商品の候補を投稿
```

---

## 📊 フェーズ3 コード統計

| 項目 | 内容 |
|------|------|
| task_inventory_alert.py | ✅ 200+行（検知 + 抽出） |
| test_phase3.py | ✅ 更新済み（新フロー対応） |
| test_inventory_alert_manual.py | ✅ 新規（サンプルテスト） |
| AI検索エージェント | 🔄 次ステップ |

---

## 🧪 実行方法

### 1. 在庫アラート検知テスト（全体）

```bash
python test_phase3.py
```

**期待される出力:**
```
✅ 状態変化検知 テスト完了
   検出件数: X件
✅ Discord に投稿完了！
```

### 2. サンプルデータでのロジック検証

```bash
python test_inventory_alert_manual.py
```

**期待される出力:**
```
✅ 在庫有 → 在庫無: 1件
✅ 在庫有 → ページなし: 1件
✅ テスト完了！
   検知合計: 2件
```

### 3. 定時実行での動作確認（スケジューラ統合後）

```bash
python daily_scheduler.py
```

**4回の定時実行時刻で自動実行:**
- 5:00, 11:00, 17:00, 22:00

---

## 🚀 次のステップ（フェーズ3-B: AI検索エージェント）

### 実装予定（1-2日）

**AI エージェント機能:**
1. **入力**: task_inventory_alert.py からの alerts リスト
2. **検索実行**:
   - Mercari で同等商品を検索
   - Yahoo Auctions で同等商品を検索
   - Rakuma, PayPayフリマ, Rakuten等 で検索
   - 各プラットフォーム × 複数の検索キーワード
3. **評価**:
   - 商品の状態（新品/中古/未使用）を確認
   - 付属品の有無を確認
   - 価格の近さを確認
   - 説明文の類似度を判定
4. **提案**:
   - Top 3～5 の同等商品候補を提案
   - 各候補の「同等性スコア」を計算
   - 理由を説明（「○○がマッチしている」）

**実装ファイル:**
- 新規作成: `tasks/task_product_search.py` — AI検索エージェント
- 修正予定: `daily_scheduler.py` — フェーズ3Bを統合
- 修正予定: `notifiers/discord_notifier.py` — 検索結果の表示形式を追加

**キーテクニック:**
- `WebSearch` ツール: 各プラットフォームで検索
- `WebFetch` ツール: 候補商品の詳細を取得
- Claude の判断力: 「この商品は同等か」を人間らしく判断

---

## 📈 全体進捗状況

```
フェーズ1（スケジューラ基盤）: ████████░ 100%
フェーズ2（eBay連携 + 在庫チェック）: ████████░ 100%
フェーズ3-A（在庫切れ検知）: ████████░ 100% ✅ NEW
フェーズ3-B（AI検索エージェント）: ░░░░░░░░░ 0%
フェーズ4（リサーチ + ニュース）: ░░░░░░░░░ 0%
フェーズ5（ダッシュボード）: ░░░░░░░░░ 0%
フェーズ6（テスト・調整）: ░░░░░░░░░ 0%
```

**全体進捗**: 🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ **42.9%** （3/7 フェーズ完成）

---

## 🔑 重要なポイント

### 検知と検索の分離

フェーズ3をA（検知）とB（AI検索）に分けることで:

✅ **検知部分** は確定的・再現可能
- 「状態が変わった」という事実は必ず検知
- 重複がない（アラート漏れなし）

✅ **検索部分** は試行錯誤可能
- AI に良い検索指示を与えられるか試す
- 検索キーワードを改善する
- 評価基準を調整する

### API制限対策

- task_inventory_alert.py は JSON ファイル読み取りのみ（API無制限）
- task_product_search.py は WebSearch / WebFetch を使用（API制限あり）
  → 検索対象を「アラート件数のみ」に限定可能

---

## 📝 トラブルシューティング

### ❌ 「検出件数: 0件」が表示される

**原因**: inventory_check_results.json に 'changes' キーがない
**解決方法**:
1. task_inventory_check.py で2回以上の実行を実施
2. 最初の実行から次の実行までの間に状態変化があると、changes キーが生成される
3. または test_inventory_alert_manual.py で検証

### ❌ Discord に投稿されない

**原因**: webhook_url が設定されていない
**解決方法**:
```bash
# config/schedule_config.json を確認
cat config/schedule_config.json | grep webhook
```

---

## 📚 関連ファイル一覧

| ファイル | 役割 |
|---------|------|
| `tasks/task_inventory_alert.py` | 状態変化検知 + 商品情報抽出 |
| `tasks/task_inventory_check.py` | 在庫チェック（フェーズ2） |
| `test_phase3.py` | フェーズ3統合テスト |
| `test_inventory_alert_manual.py` | サンプルデータテスト |
| `data/inventory_check_results.json` | 在庫チェック結果（入力） |
| `notifiers/discord_notifier.py` | Discord通知 |
| `daily_scheduler.py` | スケジューラ（定時実行） |

---

**Status**: フェーズ3-A 完成 ✅
**Next**: フェーズ3-B（AI検索エージェント実装）
