# eBay物販ビジネス システム - 定時実行方式設計書

**バージョン**: 1.0
**作成日**: 2026-04-13
**対象範囲**: 全体の定時実行・スケジューリング・ワークフロー統合
**ステータス**: 設計書（実装済みコンポーネントの統合）

---

## 目次

1. [全体概要](#全体概要)
2. [システムアーキテクチャ](#システムアーキテクチャ)
3. [定時実行スケジュール](#定時実行スケジュール)
4. [各実行タスクの詳細設計](#各実行タスクの詳細設計)
5. [データベース・ストレージ設計](#データベースストレージ設計)
6. [エラーハンドリング・リカバリー](#エラーハンドリングリカバリー)
7. [UI・ダッシュボード統合](#uiダッシュボード統合)
8. [拡張計画（Phase 5以降）](#拡張計画phase-5以降)

---

## 全体概要

### システムミッション

```
eBay出品商品の【仕入先在庫】から【販売・利益管理】までを
完全自動化し、ユーザーは朝ダッシュボード確認するだけで
全ての定期タスクが完了している状態を実現する
```

### 3層アーキテクチャ

```
【レイヤー1】定時実行エンジン
  └─ daily_scheduler.py（Cron/タスク実行管理）

【レイヤー2】ビジネスロジック（19ファイル・38機能）
  ├─ 秘書ルーティン（メール・TODO・リサーチ）
  ├─ eBay連携（同期・ランク計算）
  ├─ 競合監視（8サイト在庫チェック）
  ├─ 利益計算（送料・手数料・利益率）
  └─ 学習パイプライン（YouTube動画→知識化）

【レイヤー3】データベース・ファイルシステム
  ├─ monitor.db（eBay出品・競合監視データ）
  ├─ .company/（組織・TODO・メモ・学習）
  └─ data/（CSV・JSON・計算結果）
```

### 実行責任者

```
定時実行エンジン: daily_scheduler.py（Python スケジューラ）
  ↓
各実行タスク: task_*.py（タスク固有のロジック）
  ↓
エラー通知: notifier.py（メール・ログ）
  ↓
UI表示: app.py（Streamlit ダッシュボード）
```

---

## システムアーキテクチャ

### 全体データフロー図

```
【朝 5:00 定時実行開始】
    ↓
[定時実行エンジン: daily_scheduler.py]
    ↓
┌──────────────────────────────────────────────────────────────┐
│  秘書ルーティン (朝 5:00)                                       │
├──────────────────────────────────────────────────────────────┤
│ 1. Gmail API で eBay関連メール取得                              │
│    └─ 件数・内容 → .company/secretary/inbox/                 │
│                                                                │
│ 2. 前日未完了 TODO を本日ファイルへ繰越                          │
│    └─ .company/secretary/todos/YYYY-MM-DD.md                │
│                                                                │
│ 3. デイリーリサーチ実行                                         │
│    └─ 基準に基づいて新商品候補リサーチ実施                      │
│    └─ .company/research/notes/YYYY-MM-DD-*.md                │
│                                                                │
│ 結果 → .company/secretary/routine_results/YYYY-MM-DD.json   │
└──────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────┐
│  eBay同期タスク (朝 5:05 - 予定)                                │
├──────────────────────────────────────────────────────────────┤
│ 1. eBay API から全出品（498件）を取得                          │
│    ├─ item_id, sku, price, qty, watch_count など              │
│    └─ monitor.db の ebay_listings テーブルに同期               │
│                                                                │
│ 2. 自動ランク計算（A～E, または S～E）                         │
│    ├─ watch_count, view_count, sales_count_30d                │
│    ├─ 伸び率計算（成長スコア）                                  │
│    └─ rank カラムを自動更新                                     │
│                                                                │
│ 結果 → monitor.db （メモリに最新ランク保持）                    │
└──────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────┐
│  競合監視タスク (朝 5:10 - 予定)                                │
├──────────────────────────────────────────────────────────────┤
│ 1. 348件の仕入先商品在庫を自動チェック                          │
│    ├─ Selenium グローバルドライバー（接続1回）                 │
│    ├─ プラットフォーム別グループ化（バッチ処理）                │
│    ├─ 8サイト対応（メルカリ、ヤフオク、PayPayフリマなど）      │
│    └─ 3層リトライロジック（TimeoutException対応）              │
│                                                                │
│ 2. 在庫状態を分類                                              │
│    ├─ 在庫有（仕入可能）                                       │
│    ├─ 在庫無（販売完了・売切れ）                                │
│    ├─ ページなし（削除済み商品）                                │
│    └─ エラー（技術的な問題）                                   │
│                                                                │
│ 結果 → data/inventory_check_results.csv                        │
│ + monitor.db の competitor_products テーブルに同期              │
└──────────────────────────────────────────────────────────────┘
    ↓
【朝 起床後】
    ↓
[Streamlit ダッシュボード（app.py）]
    ↓
┌──────────────────────────────────────────────────────────────┐
│  統合ダッシュボード表示                                         │
├──────────────────────────────────────────────────────────────┤
│ 📧 秘書ルーティン結果                                           │
│   ├─ メール件数                                              │
│   ├─ TODO繰越数                                              │
│   └─ リサーチ進捗                                            │
│                                                                │
│ 📊 eBay出品状況（498件）                                       │
│   ├─ ランク分布（A-E or S-A-B-C-D-E）                        │
│   ├─ 売上・ウォッチ数動向                                      │
│   └─ 価格トレンド                                            │
│                                                                │
│ 🔍 仕入先在庫状況（348件）                                     │
│   ├─ 在庫有: XX件 (XX%)                                       │
│   ├─ 在庫無: XX件 (XX%)                                       │
│   ├─ ページなし: XX件 (XX%)                                   │
│   └─ エラー: XX件 (XX%)                                       │
│                                                                │
│ 💰 利益管理                                                   │
│   ├─ 総売上（月次）                                           │
│   ├─ 総利益率                                                │
│   └─ カテゴリ別利益                                          │
└──────────────────────────────────────────────────────────────┘
```

### コンポーネント依存図

```
daily_scheduler.py （頂点：定時実行エンジン）
├── task_company_secretary.py
│   ├── task_email_pickup.py （Gmail API）
│   ├── task_research.py （基準ベース検索）
│   └── .company/secretary/ （TODO・メモ）
│
├── run_ebay_sync.py
│   ├── ebay_sync.py （API同期）
│   ├── ebay_client.py （APIクライアント）
│   ├── rank_calculator.py （ランク計算）
│   └── monitor.db （データ永続化）
│
├── run_inventory_check.py
│   ├── inventory_checker_selenium.py （在庫チェック）
│   ├── sku_mapping_manager.py （SKU→URL変換）
│   └── data/inventory_check_results.csv （結果保存）
│
└── app.py （UI層）
    ├── company_integration.py （秘書ルーティン結果読み込み）
    ├── monitor.db （eBay・競合データ表示）
    └── calculator.py （利益計算・表示）
```

---

## 定時実行スケジュール

### 日次スケジュール（毎日）

```
【朝 5:00:00】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
秘書ルーティン実行開始

  ├─ 5:00:00 - 5:02:30：メール確認（Gmail API）
  │   └─ eBay関連メール取得 → inbox へ記録
  │
  ├─ 5:02:30 - 5:03:00：TODO繰越
  │   └─ 前日未完了タスク → 本日ファイルに自動生成
  │
  └─ 5:03:00 - 5:04:00：デイリーリサーチ
      └─ 基準に基づいて新商品候補リサーチ実施

  ✅ 結果保存：routine_results/YYYY-MM-DD-routine.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【朝 5:05:00】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
eBay同期実行開始

  ├─ 5:05:00 - 5:06:30：API呼び出し & データ取得
  │   └─ 498件のアイティム取得（page_number, sku, price, qty等）
  │
  └─ 5:06:30 - 5:07:30：自動ランク計算 & DB更新
      └─ watch/view/sales の伸び率 → ランク S/A/B/C/D/E 割り当て

  ✅ 結果保存：monitor.db の ebay_listings テーブル

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【朝 5:10:00】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
競合監視（仕入先在庫チェック）実行開始

  ├─ 5:10:00 - 5:50:00：348件在庫チェック（約40分）
  │   ├─ SKU → 仕入先URL 変換
  │   ├─ Selenium グローバルドライバー初期化
  │   ├─ プラットフォーム別バッチ処理
  │   │   ├─ メルカリ（23件）
  │   │   ├─ ヤフオク（215件）
  │   │   ├─ PayPayフリマ（23件）
  │   │   ├─ ラクマ（XX件）
  │   │   ├─ 楽天市場（XX件）
  │   │   ├─ Amazon（3件）
  │   │   └─ その他（XX件）
  │   │
  │   └─ リトライロジック
  │       ├─ TimeoutException → 指数バックオフ（1s, 2s, 4s）
  │       ├─ WebDriverException → ドライバー再初期化
  │       └─ 最大 3回まで自動リトライ
  │
  └─ 5:50:00 - 5:51:00：結果集計 & ファイル出力
      └─ CSV + JSON 出力

  ✅ 結果保存：
     - data/inventory_check_results.csv
     - data/inventory_check_results.json
     - monitor.db の competitor_products テーブル

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【朝 6:00】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
全タスク完了 → ダッシュボード更新準備完了

  ✅ ユーザー起床時には全ての定時タスクが完了した状態
```

### 週次スケジュール（毎週月曜朝 5:51）

```
【朝 5:51】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
週次リサーチレポート生成

  ├─ 過去7日間のランク変動分析
  ├─ 新規商品リサーチサマリー（7件の候補）
  ├─ 仕入先在庫トレンド分析
  │   ├─ 在庫有の増減傾向
  │   ├─ 急に在庫がなくなった商品（仕入れ困難化）
  │   └─ 新規出現商品（新しい仕入先）
  │
  └─ リポート出力：.company/research/notes/YYYY-MM-DD-weekly-report.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 月次スケジュール（毎月1日朝 5:51）

```
【朝 5:51】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
月次決算・分析レポート生成

  ├─ 売上集計（前月分）
  ├─ 利益率分析（カテゴリ別・ランク別）
  ├─ 仕入原価 vs 販売利益 分析
  ├─ ランク分布変化（月初 vs 月末）
  ├─ 競合監視結果のサマリー
  │   ├─ 仕入先平均在庫率
  │   ├─ 在庫が稀有な商品（仕入れレアー品）
  │   └─ 在庫が安定している仕入先
  │
  └─ 月次レポート出力：.company/finance/YYYY-MM-monthly-report.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 手動実行タスク

```
【ユーザー操作】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Streamlit ダッシュボードから手動実行可能なタスク

1. 「🔄 今すぐ同期」ボタン
   └─ eBay API 即時同期 + ランク計算

2. 「🔍 在庫チェック実行」ボタン
   └─ 348件の仕入先在庫即時チェック（~40分）

3. 「📊 利益計算シミュレーター」
   └─ 仕入価格 → 販売価格・手数料・利益 の対話的計算

4. 「🎓 動画学習実行」
   └─ YouTube URL → Whisper文字起こし → 知識化

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 各実行タスクの詳細設計

### Task 1: 秘書ルーティン（朝 5:00）

**ファイル**: `tasks/task_company_secretary.py`
**実行時間**: 4分（5:00 - 5:04）
**頻度**: 毎日

**処理フロー**

```
run_company_secretary() 関数
├─ 1. メール確認（task_email_pickup.py）
│   ├─ Gmail API で過去24時間のメール取得
│   ├─ eBay関連メール（件名・送信者フィルタ）を抽出
│   └─ .company/secretary/inbox/YYYY-MM-DD.md に記録
│
├─ 2. TODO繰越（.company/secretary/todos/）
│   ├─ 昨日のファイルから「- [ ]」の行を抽出
│   ├─ 本日ファイルに「## 繰越タスク」セクション作成
│   └─ 自動生成メッセージ「前日からの繰越: X件」
│
└─ 3. デイリーリサーチ（task_research.py）
    ├─ .company/research/topics/daily-research-criteria.md を読み込み
    ├─ 基準に基づいて新商品候補をリサーチ（スタブ or API）
    └─ .company/research/notes/YYYY-MM-DD-new-product-research.md に記録

結果JSON: .company/secretary/routine_results/YYYY-MM-DD-routine.json
{
  "date": "2026-04-13",
  "email": {
    "total": 5,
    "ebay_related": 3,
    "subjects": ["Order Notification", ...]
  },
  "todos_carried_over": 2,
  "research": {
    "candidates_found": 5,
    "high_potential": ["SKU-001", ...]
  },
  "executed_at": "2026-04-13T05:04:00Z"
}
```

**エラーハンドリング**

| エラー | 原因 | リカバリー |
|--------|------|-----------|
| Gmail API未インストール | credentials.json 未配置 | スタブモード：メール取得スキップ |
| TODO ファイルなし | 秘書室未初期化 | TODO繰越スキップ、警告ログ |
| リサーチ基準ファイル未検出 | .company/research/topics/ 未作成 | デフォルト基準を使用 |

**出力成果物**

- `routine_results/YYYY-MM-DD-routine.json` - 結果メタデータ
- `inbox/YYYY-MM-DD.md` - メール記録
- `todos/YYYY-MM-DD.md` - 本日の TODO（繰越含む）
- `notes/YYYY-MM-DD-new-product-research.md` - リサーチ結果

---

### Task 2: eBay同期 & ランク計算（朝 5:05）

**ファイル**: `ebay_sync.py`, `rank_calculator.py`
**実行時間**: 2分30秒（5:05 - 5:07:30）
**頻度**: 毎日（またはオンデマンド）

**処理フロー**

```
ebay_sync.sync_listings_from_ebay() 関数
├─ 1. eBay API 認証
│   └─ credentials から access_token 再取得（有効期限確認）
│
├─ 2. アイティム一覧取得
│   ├─ GetMyEBaySellingList API 呼び出し
│   ├─ ページネーション処理（複数ページ対応）
│   └─ 各アイティムから抽出:
│       ├─ ItemID
│       ├─ SKU
│       ├─ Title
│       ├─ Price
│       ├─ Quantity
│       ├─ WatchCount
│       ├─ HitCount
│       ├─ QuantitySold（過去30日）
│       └─ LastListingRevision（更新時刻）
│
├─ 3. データベース同期（monitor.db）
│   ├─ ebay_listings テーブルに UPSERT
│   ├─ SKU で既存データとマッチング
│   └─ メトリクス前回値を自動保存
│
└─ 4. 自動ランク計算（rank_calculator.py）
    ├─ 各アイティムの伸び率計算:
    │   ├─ watch_growth_rate = (current_watch - last_watch) / max(last_watch, 1)
    │   ├─ view_growth_rate = (current_view - last_view) / max(last_view, 1)
    │   └─ sales_growth_rate = (current_sales - last_sales) / max(last_sales, 1)
    │
    ├─ メトリクススコア計算:
    │   ├─ view_score = min(current_view / 200 * 100, 100)
    │   ├─ watch_score = min(current_watch / 50 * 100, 100)
    │   ├─ sales_score = min(current_sales / 5 * 100, 100)
    │   └─ composite = view_score×2.0 + watch_score×1.0 + sales_score×1.0
    │                + view_growth×0.5 + watch_growth×0.3
    │
    └─ ランク割り当て:
        ├─ S: score >= 90
        ├─ A: 75 <= score < 90
        ├─ B: 60 <= score < 75
        ├─ C: 45 <= score < 60
        ├─ D: 30 <= score < 45
        └─ E: score < 30

結果DB: monitor.db の ebay_listings テーブル
```

**パフォーマンス目標**

| 項目 | 目標値 |
|------|--------|
| API呼び出し時間 | < 1分30秒 |
| ランク計算時間 | < 1分 |
| エラー率 | < 0.1% |
| 同期成功率 | > 99% |

**エラーハンドリング**

| エラー | 原因 | リカバリー |
|--------|------|-----------|
| API 認証失敗 | トークン期限切れ | refresh_token で再取得 |
| API レート制限 | 呼び出し頻度超過 | 待機してリトライ（最大3回） |
| ネットワークタイムアウト | eBay API不応答 | エクスポーネンシャルバックオフ |
| 部分的なデータ欠落 | 一部アイティム取得失敗 | 取得成功分で続行、ログに記録 |

**出力成果物**

- `monitor.db` - ebay_listings テーブル更新（498件のランク情報）
- ログ：同期件数、エラー数、平均スコア

---

### Task 3: 競合監視・在庫チェック（朝 5:10）

**ファイル**: `inventory_checker_selenium.py`, `sku_mapping_manager.py`
**実行時間**: 40分（5:10 - 5:50）
**頻度**: 毎日（またはオンデマンド）

**処理フロー**

```
run_inventory_check() 関数
├─ 1. SKU → 仕入先URL 変換
│   ├─ data/sku_mappings.json を読み込み
│   ├─ 498件eBay出品 × SKU マッピングルール
│   ├─ 348件の実仕入先商品 URL を生成
│   │   ├─ ebayme_18924610012 → https://jp.mercari.com/item/m18924610012
│   │   ├─ ebayyh_XXXXX → https://page.auctions.yahoo.co.jp/auction/XXXXX
│   │   └─ ... その他プラットフォーム
│   │
│   └─ 109件は自有在庫（URL不要）、41件は未分類
│
├─ 2. グローバルドライバー初期化（Selenium）
│   ├─ Chrome Options 設定
│   │   ├─ --guest モード（プロファイル不要）
│   │   ├─ --disable-blink-features=AutomationControlled
│   │   └─ 最小化オプション
│   │
│   └─ WebDriver インスタンス作成（シングルトンパターン）
│
├─ 3. プラットフォーム別バッチ処理（メモリ効率化）
│   ├─ メルカリ（23件） → 同一プラットフォーム連続処理
│   │   ├─ JavaScriptレンダリング待機（3秒）
│   │   ├─ テキスト抽出 → "購入手続きへ" or "売り切れました" 判定
│   │   └─ レート制限（0.5秒間隔）
│   │
│   ├─ ヤフオク（215件）
│   │   ├─ タイムアウト：60秒（複雑な JS が多い）
│   │   ├─ テキスト判定：["入札", "入札する", "今すぐ落札", "もうすぐ終了"] など
│   │   └─ 記述エラーのリトライ対応
│   │
│   ├─ PayPayフリマ（23件）
│   │   ├─ タイムアウト：30秒
│   │   └─ "購入手続きへ" → in_stock
│   │
│   └─ その他プラットフォーム（87件）
│       ├─ ラクマ、楽天市場、Amazon
│       └─ 各プラットフォーム固有の検出ルール適用
│
├─ 4. 在庫状態判定（検出ルールベース）
│   ├─ 在庫有: HTML に in_stock キーワード存在
│   ├─ 在庫無: HTML に out_of_stock キーワード存在
│   ├─ ページなし: 404 or not_found キーワード存在
│   └─ エラー: タイムアウト or WebDriver例外
│
├─ 5. リトライロジック（信頼性向上）
│   ├─ TimeoutException 発生時
│   │   ├─ 1回目失敗 → 1秒待機 → リトライ
│   │   ├─ 2回目失敗 → 2秒待機 → リトライ
│   │   └─ 3回目失敗 → 結果: "エラー"
│   │
│   ├─ WebDriverException 発生時
│   │   └─ ドライバーをリセット → 再初期化
│   │
│   └─ メモリ管理
│       ├─ 50アイティムごと（idx % 50 == 1）
│       └─ ドライバー再起動（メモリリーク対策）
│
└─ 6. 結果集計 & ファイル出力
    ├─ CSV: data/inventory_check_results.csv
    ├─ JSON: data/inventory_check_results.json
    └─ ログ：実行時間、成功率、エラー件数

結果例（CSV 形式）:
SKU,Source,URL,Status,Details,Checked_At
ebayme_18924610012,メルカリ,https://...,在庫有,購入手続きへ,2026-04-13T05:35:42Z
ebayyh_XXXXX,Yahoo Auctions,https://...,在庫無,このオークションは終了,2026-04-13T05:40:15Z
ebayPF_YYYYY,PayPayフリマ,https://...,ページなし,ページが見つかりません,2026-04-13T05:45:20Z
```

**パフォーマンス目標**

| 項目 | 目標値 |
|------|--------|
| 処理時間（348件） | 35-45分 |
| 成功率 | > 86% |
| 平均 1件あたり | 6-8秒 |
| メモリピーク | < 512MB |

**エラーハンドリング**

| エラー | 原因 | リカバリー |
|--------|------|-----------|
| TimeoutException | ページ読み込み遅延 | 指数バックオフリトライ |
| ConnectionRefusedError | Selenium ドライバークラッシュ | ドライバー再起動 |
| StaleElementReferenceException | JS 実行中に DOM変更 | リトライ（ページ再読み込み） |
| メモリ不足 | Selenium メモリリーク | 50アイティムごとにリセット |
| プロキシブロック | レート制限 | 0.5秒インターバル + バックオフ |

**出力成果物**

- `data/inventory_check_results.csv` - 348件の在庫チェック結果
- `data/inventory_check_results.json` - JSON形式の詳細結果
- ログ：進捗表示、エラーレポート

---

### Task 4: ダッシュボード更新（朝 6:00）

**ファイル**: `app.py`, `company_integration.py`
**実行時間**: 自動（リアルタイム）
**頻度**: 毎日（定時タスク完了後は自動更新）

**表示内容**

```
【秘書ルーティン結果】
├─ 📧 メール確認: X件（eBay関連: Y件）
├─ ✅ TODO繰越: Z件
└─ 🔍 リサーチ進捗: 候補 W個

【eBay出品状況】
├─ 総出品数: 498件
├─ ランク分布:
│   ├─ S ランク: A件
│   ├─ A ランク: B件
│   ├─ B ランク: C件
│   ├─ C ランク: D件
│   ├─ D ランク: E件
│   └─ E ランク: F件
│
└─ 売上動向（30日）: ●●●▲▲... グラフ表示

【仕入先在庫状況】
├─ 在庫有: XX件 (XX%)
├─ 在庫無: XX件 (XX%)
├─ ページなし: XX件 (XX%)
└─ エラー: XX件 (XX%)

【利益管理】
├─ 月次売上: ¥XXX,XXX
├─ 月次利益: ¥XX,XXX
├─ 平均利益率: X.X%
└─ カテゴリ別TOP5

【アクション】
├─ 🔄 eBay同期（手動）
├─ 🔍 在庫チェック実行（手動）
├─ 📊 利益シミュレーター（手動）
└─ 🎓 動画学習実行（手動）
```

**技術的実装**

```python
# app.py の秘書ルーティン統合
def get_company_routine_results():
    routine_file = Path(".company/secretary/routine_results")
    latest_file = max(routine_file.glob("*.json"))
    return json.load(latest_file)

# 朝起床時には定時タスク結果を自動読み込み
if st.checkbox("秘書ルーティン結果を表示"):
    result = get_company_routine_results()
    st.json(result)
```

---

## データベース・ストレージ設計

### monitor.db（eBay監視用 SQLite）

**テーブル設計**

```sql
【ebay_listings テーブル】
├─ item_id (PK): eBay ItemID（一意）
├─ sku (FK): ユーザーが割り当てた SKU
├─ title: 出品タイトル
├─ price: 現在の販売価格（USD）
├─ quantity: 現在の在庫数
│
├─ watch_count: 現在のウォッチ数
├─ last_watch_count: 前回のウォッチ数（伸び率計算用）
├─ watch_growth_rate: 前回比 ウォッチ伸び率（%）
│
├─ view_count: 現在の閲覧数（HitCount）
├─ last_view_count: 前回の閲覧数
├─ view_growth_rate: 前回比 閲覧伸び率（%）
│
├─ sales_count_30d: 過去30日の販売数
├─ last_sales_count_30d: 前回の販売数
├─ sales_growth_rate: 前回比 販売伸び率（%）
│
├─ metrics_score: 複合スコア（0-100）
├─ rank: 自動割り当てランク（S/A/B/C/D/E）
├─ last_metrics_updated_at: メトリクス更新時刻
│
├─ created_at: 初登録時刻
├─ last_synced_at: 最終同期時刻
└─ notes: ユーザーメモ

【competitor_products テーブル】
├─ id (PK): 自動採番
├─ source: 仕入先プラットフォーム（メルカリ、ヤフオクなど）
├─ sku: 対応する eBay SKU
├─ url: 仕入先の商品ページ URL
├─ status: 在庫状態（在庫有/在庫無/ページなし/エラー）
├─ price_jpy: 仕入先での価格（JPY）
├─ inventory_last_checked_at: 最終チェック時刻
└─ notes: チェック結果メモ

【monitored_items テーブル】（ユーザーが特に監視中の商品）
├─ ebay_item_id (FK): ebay_listings.item_id
├─ monitor_reason: 監視理由（価格追跡、ランク改善待機など）
├─ alert_watch_count: アラート発動ウォッチ数
├─ alert_price_drop: アラート発動価格低下（%）
├─ last_alert_at: 最終アラート時刻
└─ is_active: 監視継続中か
```

### ファイルシステム

```
C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager\
├─ data/
│   ├─ sku_mappings.json          ← SKU→URL マッピングルール（JSON）
│   ├─ inventory_check_results.csv ← 348件の在庫チェック結果
│   ├─ inventory_check_results.json ← JSON形式の詳細結果
│   ├─ ShippingRates.csv          ← 送料テーブル
│   ├─ EbayFeeRates.csv           ← eBay手数料テーブル
│   └─ monitor.db                 ← SQLite（eBay・競合監視）
│
└─ .company/
   ├─ CLAUDE.md                   ← 組織ルール
   │
   ├─ secretary/
   │   ├─ CLAUDE.md               ← 秘書の振る舞い
   │   ├─ todos/
   │   │   ├─ 2026-04-12.md       ← 前日のTODO
   │   │   ├─ 2026-04-13.md       ← 本日のTODO（繰越済み）
   │   │   └─ ...
   │   │
   │   ├─ inbox/
   │   │   ├─ 2026-04-13.md       ← 本日のメール記録
   │   │   └─ ...
   │   │
   │   ├─ notes/
   │   │   ├─ 2026-04-13-decisions.md    ← 意思決定ログ
   │   │   ├─ 2026-04-13-learnings.md    ← 学び・ノウハウ
   │   │   └─ ...
   │   │
   │   └─ routine_results/
   │       ├─ 2026-04-13-routine.json    ← 秘書ルーティン結果（JSON）
   │       └─ ...
   │
   ├─ research/
   │   ├─ CLAUDE.md               ← リサーチ部門ルール
   │   ├─ topics/
   │   │   └─ daily-research-criteria.md ← リサーチ基準
   │   │
   │   └─ notes/
   │       ├─ 2026-04-13-new-product-research.md
   │       └─ ...
   │
   ├─ finance/
   │   └─ 2026-04-monthly-report.md  ← 月次決算レポート
   │
   └─ ebay-knowledge/
       ├─ policies.md             ← eBay出品ポリシー
       ├─ shipping-guide.md       ← 送料ガイド
       └─ faq.md                  ← トラブル対応 FAQ
```

---

## エラーハンドリング・リカバリー

### エラー分類とリカバリー戦略

```
【Level 1】自動リカバリー可能（ユーザー介入不要）
├─ TimeoutException（ページ読み込み遅延）
│   └─ → 指数バックオフリトライ（3回まで）
│
├─ APIレート制限（一時的）
│   └─ → 待機してリトライ（backoff: 1s, 2s, 4s, 8s）
│
└─ 一時的なネットワーク障害
    └─ → リトライロジック（最大3回）

【Level 2】ログ記録・部分継続（ユーザー確認推奨）
├─ SKU マッピングエラー（対応ルール不在）
│   └─ → 自有在庫と判別、警告ログ
│
├─ 仕入先ページ削除（ページなし）
│   └─ → status = "ページなし" で記録、次回スキップ
│
├─ 単一アイティム処理失敗（エラーハンドリング）
│   └─ → 結果: "エラー"、全体処理は続行
│
└─ eBay API 部分的なレスポンス
    └─ → 取得成功分で続行、ログに未取得件数記録

【Level 3】 手動介入必要（プロセス停止）
├─ eBay API 認証失敗（credentials 無効）
│   └─ → エラーログ + メール通知 → ユーザー対応待機
│
├─ データベースロック（更新中アクセス）
│   └─ → リトライ後も失敗 → スキップ＋ログ
│
└─ Selenium ドライバークラッシュ（復旧不可）
    └─ → プロセス停止＋エラーメール＋ダッシュボードに赤フラグ
```

### エラーログ・通知

**ログファイル構成**

```
logs/
├─ daily_scheduler.log              ← メインスケジューラー
├─ task_company_secretary.log       ← 秘書ルーティン
├─ task_ebay_sync.log               ← eBay同期
├─ task_inventory_check.log         ← 在庫チェック
└─ errors.log                        ← 全エラー統合

【ログ形式】
[2026-04-13 05:35:42] [INFO] Inventory check started for 348 items
[2026-04-13 05:36:15] [WARN] TimeoutException for ebayme_12345 - Retry 1/3
[2026-04-13 05:36:20] [WARN] TimeoutException for ebayme_12345 - Retry 2/3
[2026-04-13 05:36:25] [ERROR] Max retries exceeded for ebayme_12345
[2026-04-13 05:50:00] [INFO] Inventory check completed: 298/348 success (85.6%)
```

**エラー通知フロー**

```
エラー発生
├─ Level 1 （自動リカバリー）
│   └─ ログ記録のみ
│
├─ Level 2 （部分継続）
│   ├─ ログ記録
│   ├─ ダッシュボード警告マーク表示
│   └─ .company/secretary/inbox に通知ファイル生成
│
└─ Level 3 （プロセス停止）
    ├─ ログ記録
    ├─ 🚨 ダッシュボード赤フラグ
    ├─ メール送信（notifier.py）
    ├─ .company/secretary/inbox に警告ファイル生成
    └─ ユーザーへ Slack/Discord 通知（将来）
```

---

## UI・ダッシュボード統合

### Streamlit アプリ構成

```
Streamlit app.py
├─ 【サイドバー】
│   ├─ ログインユーザー表示
│   ├─ ページナビゲーション
│   ├─ 定時実行スケジュール表示
│   └─ ⚙️ 設定パネル
│
├─ 【ダッシュボードページ】
│   ├─ KPI サマリー（売上、利益率、ランク分布）
│   ├─ 秘書ルーティン結果セクション
│   │   ├─ 📧 メール確認結果
│   │   ├─ ✅ TODO繰越数
│   │   └─ 🔍 リサーチ進捗
│   │
│   ├─ 📊 eBay出品管理
│   │   ├─ ランク分布グラフ
│   │   ├─ 売上動向（30日）
│   │   ├─ 出品詳細テーブル（498件）
│   │   └─ 🔄 「今すぐ同期」ボタン
│   │
│   └─ 🔍 仕入先在庫状況
│       ├─ 在庫状況の円グラフ
│       ├─ プラットフォーム別の詳細テーブル
│       ├─ 最終チェック時刻
│       └─ 🔍 「今すぐチェック」ボタン
│
├─ 【eBay連携ページ】
│   ├─ API接続状態
│   ├─ 最終同期時刻・件数
│   ├─ ランク計算の詳細
│   └─ 🔄 「同期実行」ボタン
│
├─ 【利益管理ページ】
│   ├─ 月次売上・利益
│   ├─ カテゴリ別利益率
│   ├─ シミュレーター（仕入価格 → 利益計算）
│   └─ 詳細レポート（CSV DL）
│
├─ 【競合分析ページ】
│   ├─ 仕入先別の在庫トレンド
│   ├─ 稀有な在庫商品
│   └─ リスク分析（仕入れ困難度）
│
├─ 【学習管理ページ】
│   ├─ YouTube 学習パイプライン
│   ├─ 学習済み動画一覧
│   ├─ 知識ベース（eBay知識）
│   └─ 🎓 「新規学習を追加」ボタン
│
├─ 【SKU変換管理ページ】
│   ├─ SKU → URL マッピングルール一覧
│   ├─ ルール追加・編集フォーム
│   ├─ テスト実行
│   └─ JSON エクスポート
│
└─ 【ログ・通知ページ】
    ├─ 定時実行ログビューア
    ├─ エラーレポート
    └─ 通知履歴
```

### リアルタイム表示例

```python
# Streamlit コンポーネント
import streamlit as st
from pathlib import Path
import json

st.title("eBay 物販ビジネス ダッシュボード")

# 秘書ルーティン結果
routine_file = Path(".company/secretary/routine_results") / f"{date.today()}-routine.json"
if routine_file.exists():
    result = json.load(open(routine_file))

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("📧 メール件数", result['email']['total'])
    with col2:
        st.metric("✅ TODO繰越", result['todos_carried_over'])
    with col3:
        st.metric("🔍 リサーチ候補", result['research']['candidates_found'])

# eBay出品情報
ebay_data = load_from_db("monitor.db", "ebay_listings")
st.write(f"📊 eBay出品数: {len(ebay_data)}件")

rank_counts = ebay_data.groupby('rank').size()
st.bar_chart(rank_counts)

# 仕入先在庫情報
inventory_data = pd.read_csv("data/inventory_check_results.csv")
status_counts = inventory_data['Status'].value_counts()
st.pie_chart(status_counts)

# 手動実行ボタン
if st.button("🔄 eBay同期を実行"):
    run_ebay_sync()
    st.success("同期完了！")

if st.button("🔍 在庫チェックを実行（~40分）"):
    run_inventory_check()
    st.success("在庫チェック完了！")
```

---

## 拡張計画（Phase 5以降）

### Phase 5: リアルタイム通知システム

**目標**: エラーや重要な変化をリアルタイムで通知

**実装内容**

```
├─ Discord ボット統合
│   ├─ 定時実行結果の自動投稿
│   └─ エラー時の即時通知
│
├─ Slack ワークスペース統合
│   ├─ #ebay-alerts チャネル
│   ├─ 在庫稀有化アラート
│   └─ ランク昇格・降格通知
│
└─ メール通知（既存拡張）
    ├─ 週次レポート
    ├─ エラー通知
    └─ 利益率低下アラート
```

### Phase 6: AI ベースの仕入れ推奨システム

**目標**: 機械学習で「買い時」「売り時」を予測

**実装内容**

```
├─ 過去の販売データから学習
│   ├─ ランク昇格のパターン分析
│   ├─ 季節性・トレンド抽出
│   └─ 利益率最適化
│
├─ 仕入先の在庫パターン分析
│   ├─ 在庫が稀有になる周期
│   ├─ 価格変動の予測
│   └─ 仕入れリスク評価
│
└─ リコメンデーション
    ├─ 「今週中に仕入れ推奨」リスト
    ├─ 「この仕入先は要注意」アラート
    └─ 「新規仕入候補」提案
```

### Phase 7: マルチマーケットプレイス対応

**目標**: eBay 以外の販売チャネルにも対応

**実装内容**

```
├─ Amazon Global セリング
├─ Mercado Libre（南米）
├─ Shopify ストア連携
└─ 独自越境 ECプラットフォーム

※各プラットフォームで同じビジネスロジック
  (利益計算、競合監視、ランク管理) を再利用
```

### Phase 8: 高度な自動化

**目標**: ユーザー判断を最小化した自動運用

**実装内容**

```
├─ 自動価格最適化
│   ├─ 競合価格に基づく自動値下げ
│   ├─ ランクに基づく段階的値上げ
│   └─ 季節価格調整
│
├─ 自動出品・下出品
│   ├─ 高ランク商品の自動再出品
│   ├─ 売上低迷商品の自動下出品
│   └─ 季節商品の時期的出品
│
└─ 自動仕入れ提案
    ├─ 利益率 > 30% の商品は即仕入れ推奨
    ├─ ランク S の関連商品自動提案
    └─ 「今仕入れるべき」スコアリング
```

---

## 実装チェックリスト

### 本番運用開始前の確認

- [ ] **データベース準備**
  - [ ] `monitor.db` の初期化（テーブル作成）
  - [ ] 既存の498件eBay出品を同期
  - [ ] 競合監視テーブル初期化

- [ ] **ファイルシステム準備**
  - [ ] `.company/secretary/` ディレクトリ完成
  - [ ] `.company/research/topics/daily-research-criteria.md` 作成
  - [ ] `data/sku_mappings.json` に全10プラットフォーム対応

- [ ] **認証設定**
  - [ ] eBay API credentials（token, refresh_token）
  - [ ] Google OAuth credentials.json（Gmail API）
  - [ ] Selenium WebDriver のダウンロード

- [ ] **定時実行設定**
  - [ ] Windows Task Scheduler または cron設定
  - [ ] 朝 5:00 の自動実行確認
  - [ ] ログ出力フォルダ作成（logs/）

- [ ] **エラーハンドリング**
  - [ ] メール通知テスト（notifier.py）
  - [ ] ダッシュボードのエラー表示確認
  - [ ] リトライロジック動作確認

- [ ] **UI・ダッシュボード**
  - [ ] Streamlit app.py 全ページ動作確認
  - [ ] データベース読み込み確認
  - [ ] リアルタイム更新確認

---

## まとめ

本設計書は、eBay物販ビジネスの **全体の定時実行アーキテクチャ** を統一した文書です。

### 実現される状態

```
【朝 5:00 - 6:00】
  ✅ 秘書ルーティン（メール・TODO・リサーチ）
  ✅ eBay同期（498件のランク自動計算）
  ✅ 競合監視（348件の仕入先在庫チェック）
      ↓
【朝 6:00】
  ✅ ダッシュボード準備完了
  ✅ ユーザーが起床すると全ての定期タスクが完了している状態
```

### 次のステップ

1. **この設計書の承認** ← ここ
2. 実装チェックリストの完実施
3. Phase 5-8 の詳細設計（リアルタイム通知→AI推奨）

**ご質問・修正点がありましたら、お知らせください。**
