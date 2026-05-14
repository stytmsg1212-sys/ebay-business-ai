# Phase 3: eBay API連携 (eBay API Integration)

**Status**: 計画中 (Planning)
**Phase 2完了**: 77/90テスト (85.6%) - source inventory monitoring
**Phase 3目標**: eBay API連携による自動化

## 概要

Phase 2で実装した仕入元在庫監視システムを、eBay販売商品との連携に拡張。
以下の3つのメイン機能を実装：

1. **アクティブ出品取得** - eBay Trading API から現在の出品一覧を取得
2. **SKUマッピング** - eBay出品とローカル監視アイテムをSKUで紐付け
3. **在庫同期** - 仕入元の在庫状態をeBay出品に反映

## 実装ステップ

### Step 1: Database拡張 (データベース)
- [ ] `ebay_listings` テーブル作成
  - item_id, title, sku, current_price, quantity_ebay, last_synced_at
- [ ] `monitored_items` テーブル拡張
  - ebay_item_id フィールド追加（existing SKUの参照）

### Step 2: ebay_sync.py モジュール作成
- [ ] `get_active_listings_from_api()` - eBay Trading APIコール
- [ ] `match_listings_by_sku()` - SKUによる紐付けロジック
- [ ] `sync_inventory()` - 在庫同期（ロジック決定待ち）
  - Direction: source → eBay or bidirectional?
  - Behavior: automatic or manual approval?
- [ ] `update_ebay_listing()` - eBay出品更新（価格・数量）

### Step 3: runner.py 拡張
- [ ] eBay sync ステップを監視ループに統合
- [ ] Sync失敗時のリトライロジック
- [ ] Sync結果ログ記録

### Step 4: app.py UI拡張
- [ ] **eBay設定タブ**
  - API認証情報入力フォーム
  - テスト接続ボタン
  - 同期スケジュール設定

- [ ] **eBay連携タブ**
  - アクティブ出品一覧表示（quantity, price）
  - Source item との紐付け状態表示
  - 手動同期ボタン
  - Sync履歴ログ

### Step 5: テスト・検証
- [ ] API認証テスト
- [ ] 出品取得テスト（複数ページネーション）
- [ ] SKUマッピング精度テスト
- [ ] 在庫同期の一貫性テスト

## 設計上の決定事項（待ち）

### 1. 同期方向 (Sync Direction)
**Options:**
- A. Source → eBay のみ（一方向）
  - 利点：シンプル、仕入元が真実
  - 欠点：eBay直販の在庫反映できない
- B. eBay ← → Source （双方向）
  - 利点：完全同期
  - 欠点：競合時の優先度決定が複雑

**推奨**: A. Source → eBay（当初は一方向で安全に）

### 2. 同期タイミング (Sync Timing)
**Options:**
- A. 監視サイクルと同時
- B. 独立したスケジュール（例: 毎日1回）
- C. イベントベース（在庫変化時のみ）

**推奨**: B. 独立スケジュール（安定性のため）

### 3. 数量同期の詳細 (Quantity Sync Detail)
- eBay出品数 = min(source在庫数, 最大在庫数)？
- 複数仕入元の場合の集約ロジック？
- 在庫警告閾値（e.g., source 在庫0 → eBay quantity 0 にして出品を一時停止？）

**推奨**: eBay qty = source qty（シンプル）、在庫0で一時停止検討

## API設定チェックリスト

Required in settings.json (already present):
- `ebay_app_id` - Application ID
- `ebay_dev_id` - Developer ID
- `ebay_cert_id` - Signing Certificate ID
- `ebay_user_token` - Auth Token (expires after 18 months)

## ファイル構成予定

```
ebay-manager/
├── monitor/
│   ├── ebay_client.py      ← 既存（基本的なAPI実装）
│   ├── ebay_sync.py        ← 新規（同期ロジック）
│   ├── database.py         ← 拡張（ebay_listings テーブル）
│   ├── scrapers.py         ← 既存（source監視）
│   ├── runner.py           ← 拡張（sync統合）
│   ├── notifier.py         ← 既存
│   └── __init__.py
├── app.py                  ← 拡張（UI追加）
├── settings.json           ← 既存（eBay認証情報）
├── PHASE3_PLAN.md          ← このファイル
└── ...
```

## 次ステップ

1. 上記ステップを順序通り実装
2. 各ステップ完了後にテスト
3. 最終的に runner.py に統合
4. 本番環境での検証

---
**Started**: 2026-04-05
**Phase 2 Summary**: 33 sites × 3 patterns = 90 tests, 77 passing (85.6%)
