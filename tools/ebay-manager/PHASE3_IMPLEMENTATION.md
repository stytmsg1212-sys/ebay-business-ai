# Phase 3 実装概要 - eBay API連携

**完了日**: 2026-04-05
**Status**: ✅ 基盤実装完了 / 機能テスト待ち
**前フェーズ**: Phase 2テスト 77/90 (85.6%) 完了

## 実装概要

Phase 2で構築した仕入元在庫監視システムをベースに、eBay Trading APIとの連携機能を追加。
以下の3つのメイン機能を実装：

1. **eBay出品取得** - Trading API から現在のアクティブ出品一覧を取得
2. **SKUマッピング** - eBay出品と監視アイテムをSKUで自動紐付け
3. **在庫ステータス連携** - 仕入元の在庫状態をeBay側に記録・同期

---

## 実装内容

### 1. Database拡張 (monitor/database.py)

#### 新テーブル: `ebay_listings`
```sql
CREATE TABLE ebay_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_item_id TEXT NOT NULL UNIQUE,    -- eBay出品ID
    sku TEXT NOT NULL,                     -- SKU（monitored_itemsと連動）
    title TEXT,                            -- 出品タイトル
    current_price REAL,                    -- 現在の出品価格（USD）
    quantity_ebay INTEGER,                 -- eBay上の数量
    last_synced_at TIMESTAMP,              -- 最後の同期時刻
    source_status TEXT,                    -- 仕入元在庫ステータス
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 新関数（8個）
```python
# 出品の登録・更新
upsert_ebay_listing(ebay_item_id, sku, title, current_price, quantity_ebay)
  → int: listing ID

# 出品データ取得
get_ebay_listings() → list[dict]
get_ebay_listing_by_sku(sku) → dict or None

# ステータス更新
update_ebay_listing_status(ebay_item_id, source_status)
update_ebay_listing_quantity(ebay_item_id, quantity)

# 削除
delete_ebay_listing(ebay_item_id)
```

### 2. 新モジュール: ebay_sync.py

#### 主要関数

**`sync_listings_from_ebay(app_id, dev_id, cert_id, user_token)`**
- eBay Trading APIからアクティブ出品を全件取得
- SKU付き出品をDBに同期（INSERT or UPDATE）
- 仕入元在庫ステータスをマッチング
- Returns: `{synced, matched, errors, messages}`

**`match_source_status_to_ebay()`**
- eBay出品と監視アイテムをSKUで照合
- 仕入元の在庫ステータス (available/unavailable/not_found/unknown) を eBay側に記録
- Returns: マッチ件数

**`get_sync_report()`**
- 現在の同期状態をレポート
- Returns: `{total_ebay, with_source, status_breakdown}`

#### エラーハンドリング
- API認証情報チェック（必須フィールド確認）
- API通信エラー → メッセージで返却
- 個別出品エラー → スキップ + エラーカウント

### 3. runner.py 統合

新関数追加：
```python
def run_ebay_sync(settings: dict) -> dict:
    """設定から認証情報を取得してeBay同期を実行"""
```

用途:
- 定期同期スケジュール実装時に使用
- 監視ループから独立（API呼び出し回数制限対策）

### 4. app.py UI拡張

#### 新タブ: 🔗 eBay連携

**上部コントロール**
- 「🔄 eBay出品取得・同期」 ボタン
  - eBay APIから全出品を取得
  - DB同期
  - 仕入元との紐付け
  - 結果表示

- 「📊 レポート表示」 ボタン
  - 同期状態の JSON レポート表示

**eBay出品一覧テーブル**
- Item ID - eBay出品ID
- SKU - ローカルSKU
- Title - 出品タイトル
- Price - 販売価格（USD）
- eBay Qty - eBay上の数量
- Source Status - 仕入元在庫ステータス
- Last Sync - 最終同期時刻

統計情報: 合計件数 / ソース紐付件数

#### 既存: eBay設定 (設定タブ)

API認証情報入力フォーム（既に実装済み）:
- App ID (Client ID)
- Dev ID
- Cert ID (Client Secret)
- User Token

---

## 使用方法

### 1. eBay API認証情報の設定

Streamlitの **⚙️ 設定** タブから：

1. 「eBay API認証情報」セクションに入力
   - App ID: eBay Developer Program から取得
   - Dev ID: 同上
   - Cert ID: 同上
   - User Token: eBay APIで生成（有効期限18ヶ月）

2. 「💾 設定を保存」 をクリック

### 2. eBay出品の同期

**🔗 eBay連携** タブから：

1. 「🔄 eBay出品取得・同期」 をクリック
   - APIから全アクティブ出品を取得
   - SKU付き出品をDB登録
   - 仕入元在庫ステータスと自動マッチング
   - 結果表示（何件同期、何件マッチしたか）

2. eBay出品一覧を確認
   - SKUが表示される = 仕入元と紐付き
   - Source Status が表示される = 仕入元在庫状態が反映済み

### 3. 定期同期（オプション）

**現在**: 手動ボタンクリック

**今後の拡張**: runner.py に統合して定期実行可能
```python
# 毎日1回実行のような場合
interval_seconds = 86400  # 24h
sync_result = run_ebay_sync(settings)
```

---

## 技術仕様

### API連携方式

**eBay Trading API (XML-RPC)**
- エンドポイント: `https://api.ebay.com/ws/api.dll`
- メソッド: `GetMyeBaySelling`
- 認証: XML-RPC headers + User Token
- 対応: アクティブ出品のみ（落札済み・終了品除外）

### SKUマッチング

**アルゴリズム**:
1. eBay APIから取得した出品のSKUフィールドを抽出
2. monitored_items テーブルのSKUと完全一致検索
3. マッチした場合: ebay_listings.sku = monitored_items.sku

**条件**:
- eBay側でSKUが設定されている必要がある
- SKUは一意（重複は不可）
- ローカルDB内に当該SKUの監視アイテムが存在すること

### 在庫ステータス同期

**方向**: Source → eBay （一方向）

**ステータス値**:
- `available` - 仕入元で在庫有
- `unavailable` - 仕入元で在庫無
- `not_found` - 仕入元ページなし
- `unknown` - 判定不能

**更新タイミング**:
1. eBay出品取得時に毎回マッチング
2. 仕入元の監視チェック後、自動反映

### エラーハンドリング

**認証エラー**:
- メッセージ: "eBay credentials not configured"
- 対処: 設定タブで認証情報を確認

**API通信エラー**:
- メッセージ: "eBay API error: [詳細]"
- 対処: eBay APIステータス確認、再試行

**個別出品エラー**:
- 該当出品をスキップ、次へ継続
- エラーカウント増加
- ログに詳細記録

---

## 今後の拡張予定

### Phase 3.1: 価格監視
- eBay出品の価格変動を記録
- 競合商品の価格監視機能

### Phase 3.2: 数量同期
- Source → eBay への数量更新
- 在庫0時にeBay出品を一時停止
- バッチ更新API統合

### Phase 3.3: 定期同期スケジュール
- runner.py に eBay同期ループ統合
- 独立したスケジュール設定
- API呼び出し回数制限管理

### Phase 3.4: オーダー連携
- eBay注文を監視DB に取り込み
- 配送管理との連携

---

## ファイル変更サマリ

### 新規作成
- `PHASE3_PLAN.md` - 実装計画書
- `PHASE3_IMPLEMENTATION.md` - このファイル
- `monitor/ebay_sync.py` - eBay同期モジュール

### 既存ファイル修正
- `monitor/database.py`
  - `ebay_listings` テーブル追加
  - 8個のヘルパー関数追加

- `monitor/runner.py`
  - `run_ebay_sync()` 関数追加
  - imports 更新

- `app.py`
  - 新タブ追加: `tab_ebay`
  - UI: eBay連携タブ実装
  - imports 更新

### 変更なし
- `monitor/ebay_client.py` - 既存実装を活用
- `settings.json` - 既に認証情報フィールド存在

---

## テストチェックリスト

### 単体テスト
- [ ] `sync_listings_from_ebay()` 関数テスト（モック API）
- [ ] `match_source_status_to_ebay()` マッチング精度
- [ ] `get_sync_report()` レポート生成

### 統合テスト
- [ ] eBay API認証テスト（実際のキー使用）
- [ ] 出品取得テスト（複数ページネーション）
- [ ] SKUマッチング精度テスト
- [ ] 在庫ステータス反映確認

### UI テスト
- [ ] 設定タブ：認証情報保存
- [ ] eBay連携タブ：同期ボタン動作
- [ ] 出品一覧：データ表示確認
- [ ] エラーハンドリング：各種エラー表示

### 負荷テスト
- [ ] 大量出品時の処理時間（1000件以上）
- [ ] API制限内での運用確認

---

## トラブルシューティング

### 「eBay API認証情報が不足しています」
- **原因**: 設定タブでAPI認証情報が未入力
- **対処**: 設定タブで4つの認証情報をすべて入力して保存

### 「eBay API error: Connection timeout」
- **原因**: eBay APIサーバーが応答していない
- **対処**: 数分待機後に再試行

### 「eBay出品が登録されていません」
- **原因**: eBay側にSKUが設定されていない出品のみ
- **対処**: eBay出品管理画面でSKUを設定してから同期

### マッチ件数が少ない
- **原因**: ローカルDB内に対応するSKUがない
- **対処**:
  1. eBay出品のSKUを確認
  2. 監視リストにそのSKUを追加
  3. 再度同期実行

---

## 技術情報

### テーブルリレーション

```
monitored_items (Phase 2)
├─ id (PK)
├─ sku (UNIQUE)
├─ ebay_item_id (外部参照可)
└─ last_status

ebay_listings (Phase 3 新規)
├─ id (PK)
├─ ebay_item_id (UNIQUE)
├─ sku (FK → monitored_items.sku)
├─ current_price
├─ quantity_ebay
├─ source_status
└─ last_synced_at
```

### 依存関係
```
ebay_sync.py
├─ ebay_client.py (get_active_listings, filter_items_with_sku)
├─ database.py (upsert_ebay_listing, match関数)
└─ logging

runner.py
├─ ebay_sync.py (sync_listings_from_ebay, get_sync_report)

app.py
├─ ebay_sync.py
├─ database.py (get_ebay_listings, update_ebay_listing_*)
```

---

## ログレベル

実装時のログ記録:

```
INFO: "Fetching active listings from eBay..."
INFO: "Got {n} total listings, {m} with SKU"
INFO: "Matched {x} eBay listings to source items"

DEBUG: "{sku} -> {status}"

WARNING: "Failed to sync listing {item_id}: {error}"
WARNING: "Failed to match source status: {error}"

ERROR: "eBay API error: {error}"
```

---

**Implementation Date**: 2026-04-05
**Phase Status**: Phase 2完了 (77/90) → Phase 3開始
**Next Phase**: Phase 3実装継続（テスト・検証・機能追加）
