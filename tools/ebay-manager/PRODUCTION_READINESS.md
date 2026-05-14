# Phase 3 本番運用準備チェックリスト

**準備日**: 2026-04-12
**ステータス**: 🔄 準備中

---

## ✅ 技術的準備

### ファイル・コンポーネント
- [x] daily_scheduler.py - メインスケジューラー
- [x] task_inventory_alert.py - 在庫切れ検知 (220行)
- [x] task_product_search.py - 検索タスク準備 (380行)
- [x] task_calculate_max_cost.py - 仕入価格逆算 (250行)
- [x] task_process_search_results.py - 結果処理 (200行)
- [x] 設定ファイル (schedule_config.json)
- [x] Discord Webhook 設定

### データファイル
- [x] sku_conversion_results.json (166KB, 348件)
- [x] inventory_check_results.json (94KB)
- [x] equivalence_check_tasks.json (1KB)
- [x] product_search_results.json (1.5KB)

### テスト
- [x] ユニットテスト (task_inventory_alert.py)
- [x] 統合テスト (test_phase3_integration.py)
- [x] End-to-end テスト (Web検索 + 判定)

---

## 📋 運用前チェック

### データ品質

#### [必須] sku_conversion_results.json の充実度
```
現在: 348件の仕入先商品
確認事項:
  - [ ] weight_g が設定されているか
  - [ ] length_cm, width_cm, height_cm が設定されているか
  - [ ] item_price_usd が設定されているか
  - [ ] condition（状態）が正確か
  - [ ] includes（付属品）が正確か
  - [ ] warranty（保証）が正確か
```

**チェック方法:**
```bash
python -c "
import json
with open('data/sku_conversion_results.json') as f:
    data = json.load(f)
sourced = data.get('sourced', [])
missing = []
for item in sourced[:5]:
    if not item.get('weight_g') or not item.get('item_price_usd'):
        missing.append(item.get('sku'))
print(f'Missing data: {missing}')
"
```

#### [推奨] inventory_check_results.json の定期更新
```
現在: 348件の在庫チェック結果
確認事項:
  - [ ] Selenium チェックが毎回成功しているか
  - [ ] 検出結果が正確か
  - [ ] 実行時間は許容範囲か（< 10分）
```

---

## 🔔 通知システム

### Discord 通知の確認

#### 1️⃣ 在庫切れ検知通知
```
タイミング: inventory_alert 実行時
内容:
  - 在庫切れ検知件数
  - 検索対象商品リスト
  - 進捗状況
```

- [ ] Discord Webhook URL が正しく設定されている
- [ ] テスト投稿が成功している
- [ ] 通知フォーマットが見やすいか確認

**テスト方法:**
```bash
python test_phase3_integration.py
# Discord に通知が届くか確認
```

#### 2️⃣ 検索結果通知（実装予定）
```
タイミング: product_search_results.json 生成時
内容:
  - 見つかった候補数
  - Top 3 候補
  - 推奨/非推奨の判定
```

---

## ⏰ スケジュール

### 定時実行設定

```
毎日以下の時刻に実行:
  - 05:00 (早朝)
  - 11:00 (昼)
  - 17:00 (夕方)
  - 22:00 (夜間)

実行順序:
  1. task_inventory_check (Selenium, 5-10分)
  2. task_inventory_alert (Python, < 1分)
  3. task_product_search (Python, < 1分)
  4. Discord 通知 (< 1分)
```

**確認:**
- [ ] APScheduler が正しく設定されている
- [ ] cron expression が正しい
- [ ] ログファイルが生成されている

---

## 📊 監視・運用

### 1. ログ確認（毎日）

```bash
# 最新のログを確認
tail -f logs/scheduler.log

# エラーを検索
grep ERROR logs/scheduler.log
```

**確認項目:**
- [ ] エラーが発生していないか
- [ ] 全タスクが完了しているか
- [ ] 実行時間が許容範囲内か

### 2. データファイル確認（毎週）

```bash
# ファイルサイズと更新日時を確認
ls -lah data/*.json

# 内容が正常か確認
python -c "import json; json.load(open('data/product_search_results.json'))"
```

### 3. 在庫切れ対応（発生時）

```
在庫切れが検知されたら:
  1. equivalence_check_tasks.json を確認
  2. PHASE3_CLAUDE_EXECUTION_GUIDE.md に従って検索
  3. 結果を product_search_results.json に保存
  4. 採用/非採用を判定
  5. 必要に応じて仕入れ実施
```

---

## 🚨 トラブルシューティング

### 問題1: 「在庫切れが検知されない」
```
原因の順序で確認:
1. inventory_check_results.json が更新されているか
   → Selenium チェックが失敗していないか確認
2. 在庫確認結果が正確か
   → 手動で仕入先ページを確認
3. task_inventory_alert.py が正しく動作しているか
   → テストログを確認
```

### 問題2: 「最大仕入価格が計算されない」
```
原因の順序で確認:
1. sku_conversion_results.json に weight_g と item_price_usd があるか
   → なければデータを補完
2. calculator.py が正常に動作しているか
   → 単独テストを実行
3. settings ファイルが存在するか
   → exchange_rate, tax_rate 等を確認
```

### 問題3: 「Web検索で候補が見つからない」
```
原因の順序で確認:
1. 検索クエリが正確か
   → search_query を確認
2. プラットフォームが正しいか
   → site: 指定を確認
3. 実際に該当商品が存在するか
   → 手動で仕入先ページを確認
```

---

## 📈 本番運用での改善ポイント

### Phase 3 実装後の学習機会
```
実際の運用で以下をモニター:
1. 検知精度（假陰性/偽陽性）
2. 検索成功率
3. 実行時間
4. 候補スコアの精度
```

### 将来の拡張
```
以下が実装可能になったら対応:
- [ ] Playwright で動的コンテンツ取得（Mercari 対応）
- [ ] メルカリ/ラクマ API の統合
- [ ] 価格時系列データの追跡
- [ ] 自動再検索機能
- [ ] 画像認識による同等性判定
```

---

## 🎯 最終チェック

### Go/No-Go 判定

本番運用開始前に以下を確認：

- [ ] 全テストが PASS している
- [ ] Discord 通知が正常に機能している
- [ ] ログが記録されている
- [ ] データファイルが正常に生成されている
- [ ] スケジュール設定が正しい
- [ ] エラーハンドリングが適切
- [ ] ドキュメントが整備されている

### 本番運用開始条件

✅ **全チェック項目が完了したら、以下の状態で本番運用開始:**

```
本番環境:
- daily_scheduler.py が常時実行中
- ログが毎日記録される
- Discord に通知が配信される
- 在庫切れが自動検知される
- 同等商品の検索タスクが自動準備される
```

---

## 📞 サポート連絡先

本番運用中に問題が発生した場合：

1. ログファイルを確認 → `logs/scheduler.log`
2. PHASE3_CLAUDE_EXECUTION_GUIDE.md を参照
3. トラブルシューティングセクションを確認
4. 必要に応じて Claude に相談

---

**Next Step**: チェックリスト項目を埋めて、本番運用開始の判定を待つ

