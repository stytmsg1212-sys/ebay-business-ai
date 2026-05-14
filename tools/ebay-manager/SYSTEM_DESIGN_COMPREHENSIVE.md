# eBay物販ビジネス - 統合システム設計書（全ツール・フロー完全版）

**バージョン**: 2.0（総合版）
**作成日**: 2026-04-13
**対象範囲**: 全システム（定時実行 + 随時実行ツール、UI統合、データベース）
**ステータス**: 完全設計書

---

## 目次

1. [システム全体像](#システム全体像)
2. [ツール・機能インベントリ](#ツール機能インベントリ)
3. [定時実行タスク（朝5:00-6:00）](#定時実行タスク朝500600)
4. [随時実行ツール（ユーザー手動実行）](#随時実行ツールユーザー手動実行)
5. [eBay連携システム](#eBay連携システム)
6. [データベース・ストレージ設計](#データベースストレージ設計)
7. [UI・ダッシュボード統合](#uiダッシュボード統合)
8. [エラーハンドリング](#エラーハンドリング)
9. [実装チェックリスト](#実装チェックリスト)
10. [拡張計画（Phase 5以降）](#拡張計画phase-5以降)

---

## システム全体像

### アーキテクチャの3層構造

```
【レイヤー0】 実行管理層
  ├─ daily_scheduler.py      ← 定時実行エンジン（朝5:00-6:00自動実行）
  ├─ run_scheduler.py        ← スケジューラー起動スクリプト
  └─ scheduler_integration.py ← スケジューラー設定・統合

【レイヤー1】 ビジネスロジック層（41個のツール・機能）
  ├─ 定時実行タスク（朝5:00-6:00）
  │  ├─ task_company_secretary.py      ← 秘書ルーティン
  │  ├─ task_email_pickup.py           ← メール確認
  │  ├─ task_research.py               ← デイリーリサーチ（3層構造）
  │  ├─ task_ebay_sync.py              ← eBay同期
  │  └─ task_inventory_check.py        ← 在庫チェック
  │
  ├─ 随時実行ツール（ユーザー手動/ダッシュボードボタン）
  │  ├─ task_product_search.py         ← 商品リサーチ（詳細検索）
  │  ├─ task_product_search_executor.py ← 検索実行
  │  ├─ task_process_search_results.py ← 検索結果処理
  │  ├─ task_enrich_ebay_data.py       ← eBayデータ拡張
  │  ├─ task_supplier_select.py        ← 仕入先選定
  │  ├─ task_calculate_max_cost.py     ← 最大仕入価格計算
  │  ├─ task_inventory_alert.py        ← 在庫アラート設定
  │  ├─ task_news_check.py             ← ニュース確認
  │  └─ task_rival_detection.py        ← 競合検出
  │
  ├─ コアロジック
  │  ├─ calculator.py                  ← 利益計算エンジン
  │  ├─ sku_conversion.py              ← SKU→URL変換
  │  ├─ sku_mapping_manager.py         ← SKU マッピング管理
  │  └─ company_integration.py         ← 秘書ルーティン統合
  │
  └─ eBay連携システム（monitor/）
     ├─ ebay_sync.py                   ← eBay API同期
     ├─ ebay_client.py                 ← eBay APIクライアント
     ├─ rank_calculator.py             ← ランク自動計算
     ├─ database.py                    ← SQLiteDB管理（37KB）
     ├─ notifier.py                    ← アラート通知
     └─ runner.py                      ← タスク実行ラッパー

【レイヤー2】 データベース・ストレージ層
  ├─ monitor.db                 ← SQLite（eBay出品・競合監視・メトリクス）
  ├─ .company/                  ← 組織管理（秘書・TODO・リサーチ・学習）
  └─ data/                      ← ファイルデータ（CSV・JSON・計算結果）

【レイヤー3】 UI・プレゼンテーション層
  └─ app.py (Streamlit)         ← 統合ダッシュボード（1000+行）
     ├─ 秘書ルーティン結果
     ├─ eBay出品管理
     ├─ 仕入先在庫状況
     ├─ 利益管理
     ├─ 競合分析
     └─ 手動実行ボタン群
```

### ツール統計

```
📊 実装済みコンポーネント

定時実行タスク:        5個
随時実行ツール:        9個
コアロジック:          4個
eBay連携:              6個
─────────────────────────
合計ビジネスロジック:  24個

ユーティリティ・テスト: 17個（デバッグ・テスト用）
────────────────────────
総実装ファイル:        41個
  ├─ .py ファイル:    35個
  ├─ SQLite DB:       1個
  ├─ MD ファイル:     多数（.company/内）
  └─ JSON/CSV:        複数

総コード行数:         ~6000+ 行
```

---

## ツール・機能インベントリ

### 【A】定時実行タスク（朝5:00-6:00）

| # | タスク名 | ファイル | 時刻 | 時間 | 機能 | 出力 |
|---|---------|---------|------|------|------|------|
| 1 | 秘書ルーティン | task_company_secretary.py | 5:00 | 4分 | メール確認・TODO繰越・リサーチ | JSON + MD |
| 2 | メール確認 | task_email_pickup.py | 5:00 | 2分 | Gmail APIで eBay関連メール抽出 | inbox/ |
| 3 | デイリーリサーチ（3層） | task_research.py | 5:03 | 1分 | 新商品 + AIニュース + 制約解決案 | notes/ |
| 4 | eBay同期 | task_ebay_sync.py | 5:05 | 2分30秒 | 498件アイティム同期 + ランク計算 | monitor.db |
| 5 | 在庫チェック | task_inventory_check.py | 5:10 | 40分 | 348件仕入先在庫チェック（Selenium） | CSV/JSON |

**合計時間**: 朝5:00-5:50（約50分）

---

### 【B】随時実行ツール（ユーザー手動実行）

#### 1️⃣ 商品リサーチシステム（3層構造）

```
task_product_search.py (15899行 - 最大級モジュール)
├─ 深層リサーチ実行
├─ eBay Research Products データ自動分析
├─ 仕入先候補の自動検索
├─ SKU自動生成
└─ 利益率計算

実行タイミング: ユーザー手動実行
処理時間: 15-30分（検索範囲による）

入力:
├─ リサーチ条件（カテゴリ、価格範囲、期間等）
└─ 除外キーワード、仕入先パターン

出力:
├─ task_process_search_results.py へデータ引き継ぎ
├─ .company/research/notes/ に詳細記録
└─ ダッシュボード「リサーチ結果」セクション表示
```

#### 2️⃣ リサーチ結果処理・フィルタリング

```
task_process_search_results.py (7198行)
├─ 候補商品を利益率でフィルタリング
├─ ランク計算（A-E）
├─ 競合分析（仕入先）
└─ リスク評価（価格変動性）

入力: task_product_search.py の結果
出力: task_enrich_ebay_data.py へ連携
```

#### 3️⃣ eBay データ拡張

```
task_enrich_ebay_data.py (12797行)
├─ 既存のeBay出品と新規候補の統合
├─ 販売パターン分析
├─ ウォッチ数・価格動向の予測
└─ 在庫推奨値の自動計算

出力: monitor.db に補助情報保存
```

#### 4️⃣ 仕入先選定・検証

```
task_supplier_select.py (8091行)
├─ 複数候補仕入先から最適を選定
├─ 信用度・配送速度・価格の点数化
├─ 過去購入実績の参照
└─ 推奨仕入先提案

入力: task_enrich_ebay_data.py の結果
出力: .company/research/notes/supplier-recommendation.md
```

#### 5️⃣ 最大仕入価格計算

```
task_calculate_max_cost.py (10036行)
├─ 目標利益率（例：30%）から逆算
├─ eBay手数料・送料を含めた計算
├─ 為替変動考慮
├─ 仕入先パターン別の価格比較
└─ 購入ボタン押下推奨額を提示

使用例: 「このカテゴリなら 仕入価格 $100 以下なら買い」
```

#### 6️⃣ 在庫アラート設定

```
task_inventory_alert.py (6885行)
├─ 指定SKUの在庫状況を監視
├─ 在庫が少なくなったら通知
├─ 特定の仕入先で在庫が出たら通知
├─ カスタムルール設定
└─ メール/Slack 通知送信

配置先: .company/secretary/alerts/
```

#### 7️⃣ ニュース確認

```
task_news_check.py (1267行 - 軽量)
├─ AI/ML関連ニュース取得
├─ eBay API更新情報
├─ 仕入先プラットフォームのお知らせ
└─ technical_constraints.md の項目と照合

使用例: 「MCPマーケットプレイスで新しい Mercari コネクタが出た」
```

#### 8️⃣ 競合検出・監視

```
task_rival_detection.py (1391行)
├─ 同じSKUを出品している競合者を検出
├─ 競合の価格・ランク・フィードバック取得
├─ 価格ダンピング検出
└─ 対抗戦略の提案

出力: .company/research/notes/rival-analysis.md
```

---

### 【C】コア計算エンジン

#### 利益計算システム（calculator.py）

```python
機能:
├─ 課金重量 vs 実重量の自動判定
├─ 複数キャリア（DHL/USPS/FedEx等）対応
├─ eBay手数料（出品・最終売却）自動計算
├─ 国別ゾーン割り当て
├─ 梱包材料コスト
├─ 為替レート組み込み（USD→JPY）
└─ 利益率シミュレーション（複数パターン）

入力: ShippingRates.csv, EbayFeeRates.csv
出力: JSON（各種計算結果）

使用例:
├─ ダッシュボード: 「月次利益率 X.X%」表示
├─ リサーチ: 「この商品は利益率 35%」判定
└─ 手動実行: シミュレーターで「仕入価格 $100 なら利益 $XX」
```

---

## 定時実行タスク（朝5:00-6:00）

### 全体フロー

```
【朝 5:00:00】━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
定時実行エンジン起動（daily_scheduler.py）

 ┌─ Task 1: 秘書ルーティン実行開始
 │  ├─ 5:00:00 - 5:02:30：メール確認（Gmail API）
 │  │   └─ .company/secretary/inbox/ に記録
 │  │
 │  ├─ 5:02:30 - 5:03:00：TODO繰越
 │  │   └─ .company/secretary/todos/YYYY-MM-DD.md 自動生成
 │  │
 │  └─ 5:03:00 - 5:04:00：デイリーリサーチ（3層構造）✨
 │      ├─ A. eBay新商品候補リサーチ
 │      ├─ B. AIニュース・技術動向リサーチ（新）
 │      │   └─ WebSearch で最新AI・MCP・API情報取得
 │      └─ C. 自システム制約解決機会の検討（新）
 │          └─ technical_constraints.md の6項目をチェック
 │
 │  ✅ 結果保存：
 │     - routine_results/YYYY-MM-DD-routine.json
 │     - notes/YYYY-MM-DD-new-product-research.md
 │     - notes/YYYY-MM-DD-ai-news-summary.md（新）
 │     - notes/YYYY-MM-DD-improvement-opportunities.md（新）
 │
 └─ 4分で完了 ───┐
                  │
 ┌─ Task 2: eBay同期 & ランク計算開始
 │  ├─ 5:05:00 - 5:06:30：API呼び出し & メトリクス取得
 │  │   └─ 498件全アイティムの watch/view/sales 取得
 │  │
 │  └─ 5:06:30 - 5:07:30：ランク自動計算
 │      └─ 伸び率 + 複合スコア → S/A/B/C/D/E 割り当て
 │
 │  ✅ 結果保存：monitor.db ebay_listings テーブル更新
 │
 └─ 2分30秒で完了 ───┐
                      │
 ┌─ Task 3: 競合監視・在庫チェック開始
 │  ├─ 5:10:00 - 5:50:00：348件在庫チェック（Selenium）
 │  │   ├─ SKU → 仕入先URL 変換
 │  │   ├─ グローバルドライバー初期化
 │  │   ├─ プラットフォーム別バッチ処理
 │  │   │   ├─ メルカリ（23件）
 │  │   │   ├─ ヤフオク（215件）
 │  │   │   ├─ PayPayフリマ（23件）
 │  │   │   └─ その他（87件）
 │  │   │
 │  │   ├─ リトライロジック（指数バックオフ）
 │  │   └─ メモリ管理（50アイティム毎リセット）
 │  │
 │  └─ 5:50:00 - 5:51:00：結果集計 & ファイル出力
 │
 │  ✅ 結果保存：
 │     - data/inventory_check_results.csv
 │     - data/inventory_check_results.json
 │     - monitor.db competitor_products テーブル
 │
 └─ 40分で完了 ───┐
                   │
【朝 6:00】━━━━━━━┛
全タスク完了 → Streamlit ダッシュボード自動更新準備完了
```

### Task 詳細設計

#### Task 1: 秘書ルーティン（朝5:00）

**実装**: `task_company_secretary.py` (201行)

```python
def run_company_secretary(config={}):
    # 1. メール確認
    emails = task_email_pickup.get_ebay_emails()  # Gmail API
    inbox_file = save_to_inbox(emails)

    # 2. TODO繰越
    todos = load_yesterday_todos()
    undone_todos = [t for t in todos if not t.is_done]
    save_to_today_todos(undone_todos)

    # 3. デイリーリサーチ（3層構造）
    # A. 新商品候補
    products = task_research.search_new_products()

    # B. AIニュース・技術動向（新）
    ai_news = web_search("Claude MCP Vision 2026年4月")
    ai_opportunities = analyze_for_improvements(ai_news)

    # C. 制約解決機会
    constraints = load_technical_constraints()
    solvable = check_solvable_constraints(ai_news, constraints)

    # 結果保存
    result_json = {
        "date": today,
        "email": {"total": len(emails), "ebay_related": count},
        "todos_carried_over": len(undone_todos),
        "research": {
            "new_products": len(products),
            "ai_news_found": len(ai_news),
            "improvement_opportunities": len(solvable)
        }
    }
    save_json(result_json, f"routine_results/{today}-routine.json")

    return result_json
```

**エラーハンドリング**
- Gmail API 未インストール → スタブモード（スキップ）
- TODO ファイル未検出 → スキップ、警告ログ
- WebSearch 失敗 → キャッシュデータを使用

---

#### Task 2: eBay同期 & ランク計算（朝5:05）

**実装**: `task_ebay_sync.py` (3800行) + `monitor/rank_calculator.py`

```python
def sync_ebay_and_rank(config={}):
    # 1. eBay API 認証
    token = get_or_refresh_ebay_token()

    # 2. アイティム一覧取得（ページネーション対応）
    items = ebay_client.get_active_listings()  # 498件

    # 3. メトリクス抽出
    for item in items:
        current_watch = item.WatchCount
        current_view = item.HitCount
        current_sales = item.QuantitySold  # 30日

        # DB から前回値を取得
        prev = db.get_item_metrics(item.ItemID)

        # 伸び率計算
        watch_growth = (current_watch - prev.watch) / max(prev.watch, 1)
        view_growth = (current_view - prev.view) / max(prev.view, 1)
        sales_growth = (current_sales - prev.sales) / max(prev.sales, 1)

        # スコア計算（0-100）
        view_score = min(current_view / 200 * 100, 100)
        watch_score = min(current_watch / 50 * 100, 100)
        sales_score = min(current_sales / 5 * 100, 100)

        composite_score = (
            view_score * 2.0 +
            watch_score * 1.0 +
            sales_score * 1.0 +
            view_growth * 0.5 +
            watch_growth * 0.3
        )

        # ランク割り当て
        rank = assign_rank(composite_score)
        # S: >= 90, A: >= 75, B: >= 60, C: >= 45, D: >= 30, E: < 30

        # DB 更新
        db.upsert_ebay_listing(
            item_id=item.ItemID,
            rank=rank,
            metrics_score=composite_score,
            last_synced_at=now()
        )

    return {"synced_count": len(items), "errors": 0}
```

**パフォーマンス**
- API 呼び出し: < 1分30秒
- ランク計算: < 1分
- 成功率: > 99%

---

#### Task 3: 在庫チェック（朝5:10）

**実装**: `inventory_checker_selenium.py` (25821行 - 最大モジュール)

```python
def check_inventory_for_348_items():
    # 1. SKU → URL 変換
    items_to_check = load_and_convert_skus()  # 348件

    # 2. グローバルドライバー初期化（シングルトン）
    driver = get_global_driver()

    # 3. プラットフォーム別バッチ処理
    results = []
    by_platform = group_by_platform(items_to_check)

    for platform, items in by_platform.items():
        # プラットフォーム別タイムアウト設定
        timeout = get_timeout_for_platform(platform)

        for item in items:
            status, retries = check_with_retry(
                url=item.url,
                source=platform,
                max_retries=3
            )

            results.append({
                "sku": item.sku,
                "source": platform,
                "status": status,  # 在庫有/無/ページなし/エラー
                "checked_at": now(),
                "retries_needed": retries
            })

            # レート制限対応
            time.sleep(0.5)

        # メモリ管理
        if len(results) % 50 == 0:
            driver.quit()
            time.sleep(2)
            driver = get_global_driver()

    # 4. 結果集計
    save_to_csv(results, "data/inventory_check_results.csv")
    save_to_json(results, "data/inventory_check_results.json")

    summary = {
        "total": len(results),
        "in_stock": len([r for r in results if r["status"] == "在庫有"]),
        "out_of_stock": len([r for r in results if r["status"] == "在庫無"]),
        "not_found": len([r for r in results if r["status"] == "ページなし"]),
        "error": len([r for r in results if r["status"] == "エラー"])
    }

    return summary
```

**リトライロジック**
```
TimeoutException:
  1回目失敗 → 1秒待機 → リトライ
  2回目失敗 → 2秒待機 → リトライ
  3回目失敗 → 結果: "エラー"

WebDriverException:
  → ドライバー再初期化 → リトライ
```

---

## 随時実行ツール（ユーザー手動実行）

### 実行方法

```
1️⃣ Streamlit ダッシュボースのボタン
   ├─ 🔍 「商品リサーチを実行」ボタン
   ├─ 📊 「利益シミュレーター」（対話的）
   ├─ 🎓 「動画学習実行」
   └─ 🔀 「SKU変換」テスト

2️⃣ コマンドライン実行
   python -c "from tasks.task_product_search import run; run({'category': 'xxx'})"

3️⃣ 秘書への依頼
   /company → 秘書に「商品リサーチして」と依頼
```

### 各ツールの詳細

#### 商品リサーチシステム

```
task_product_search.py (15899行)

実行フロー:
1. リサーチ条件入力
   ├─ カテゴリ（Consumer Electronics等）
   ├─ 価格範囲（$100-$500）
   ├─ 期間（直近7日など）
   ├─ 除外キーワード（Card, Camera等）
   └─ 最小販売数（2個以上など）

2. eBay Research Products API データ取得
   ├─ 直近7日売れた商品リスト
   ├─ 各商品の販売数・平均価格
   └─ 出品者国・フィードバック評価

3. 自動フィルタリング
   ├─ 利益率 > 30% の商品に絞り込み
   ├─ 販売トレンド分析（上昇傾向を優先）
   └─ リスク評価（価格変動性が低い商品を優先）

4. 仕入先リサーチ
   ├─ 各候補商品について、メルカリ・ヤフオク等で検索
   ├─ 複数の仕入先候補を検出
   └─ 価格・在庫状況を取得

5. SKU自動生成
   ├─ ebayme_XXXXX（メルカリ用）
   ├─ ebayyh_XXXXX（ヤフオク用）
   └─ その他プラットフォーム別コード

出力:
├─ .company/research/notes/YYYY-MM-DD-product-research.md
├─ data/research_candidates.json
└─ ダッシュボード「リサーチ結果」セクション

処理時間: 15-30分（検索範囲による）
```

#### リサーチ結果処理

```
task_process_search_results.py (7198行)

入力: task_product_search.py の結果

処理:
1. 利益率フィルタリング
   ├─ 目標利益率（例：30%）を超える商品に絞り込み
   ├─ 手数料・送料を含めた正確な計算
   └─ 複数シナリオでのシミュレーション

2. ランク計算
   ├─ 過去販売実績から「売りやすさ」を判定
   ├─ 競合数・価格安定性も考慮
   └─ A（売りやすい）～ E（売りにくい）を割り当て

3. 競合分析
   ├─ 同じSKUを出品している競合者を検出
   ├─ 競合の価格・ランク・販売速度を分析
   └─ 価格戦略のアドバイス

4. リスク評価
   ├─ 価格変動性（過去30日の標準偏差）
   ├─ 在庫リスク（売却期間の標準偏差）
   └─ 仕入先リスク（配送遅延実績など）

出力:
├─ data/processed_candidates.json
└─ .company/research/notes/processed-analysis.md
```

#### eBay データ拡張

```
task_enrich_ebay_data.py (12797行)

機能:
├─ 既存アイティムと新規候補の統合
├─ 販売パターン分析
├─ ウォッチ数の予測モデル
├─ 在庫推奨値の計算
└─ 季節性・トレンドの考慮

出力: monitor.db に補助テーブル記録
```

---

## eBay連携システム

### システムアーキテクチャ

```
monitor/ ディレクトリ構成

monitor/
├─ __init__.py
├─ database.py          (37KB) ← SQLiteDB 管理エンジン
├─ ebay_client.py       (11KB) ← eBay API クライアント
├─ ebay_sync.py         (8KB)  ← API同期ロジック
├─ rank_calculator.py   (10KB) ← ランク計算エンジン
├─ notifier.py          (5KB)  ← アラート通知
├─ runner.py            (5KB)  ← タスク実行ラッパー
├─ ebay_competitor_monitoring.py (現在は未使用)
└─ ebay_monitor.db      ← SQLite データベース
```

### データベース設計（database.py 37KB）

```sql
【ebay_listings テーブル】(498件)
├─ item_id (PK)           : eBay ItemID
├─ sku                    : ユーザー割り当てコード
├─ title                  : 出品タイトル
├─ price (USD)            : 現在の販売価格
├─ quantity               : 現在の在庫数
│
├─ watch_count            : 現在のウォッチ数
├─ last_watch_count       : 前回値（伸び率計算用）
├─ watch_growth_rate      : 伸び率（%）
│
├─ view_count             : 閲覧数（HitCount）
├─ last_view_count        : 前回値
├─ view_growth_rate       : 伸び率（%）
│
├─ sales_count_30d        : 過去30日販売数
├─ last_sales_count_30d   : 前回値
├─ sales_growth_rate      : 伸び率（%）
│
├─ metrics_score          : 複合スコア（0-100）
├─ rank                   : 自動割り当てランク（S/A/B/C/D/E）
├─ last_metrics_updated_at: メトリクス更新時刻
│
├─ created_at             : 初登録時刻
├─ last_synced_at         : 最終同期時刻
└─ notes                  : ユーザーメモ

【competitor_products テーブル】(348件)
├─ id (PK)                : 自動採番
├─ source                 : 仕入先（メルカリ、ヤフオク等）
├─ sku (FK)               : 対応する eBay SKU
├─ url                    : 仕入先ページURL
├─ status                 : 在庫状態（在庫有/無/ページなし/エラー）
├─ price_jpy              : 仕入先での価格
├─ inventory_last_checked_at : 最終チェック時刻
└─ notes                  : チェックメモ

【monitored_items テーブル】(ユーザー監視中)
├─ ebay_item_id (FK)      : ebay_listings.item_id
├─ monitor_reason         : 監視理由
├─ alert_watch_count      : アラート発動値
├─ alert_price_drop       : アラート発動価格低下（%）
├─ last_alert_at          : 最終アラート時刻
└─ is_active              : 監視継続中か
```

### 同期フロー

```
【朝 5:05】 daily_scheduler.py が task_ebay_sync.py を実行
    ↓
【ebay_sync.py】
├─ ebay_client.get_active_listings() → 498件取得
│   ├─ ItemID
│   ├─ SKU
│   ├─ Price (USD)
│   ├─ Quantity
│   ├─ WatchCount
│   ├─ HitCount
│   └─ QuantitySold (30日)
│
└─ database.py で UPSERT
    ├─ 既存アイティムはメトリクス更新
    └─ 新規アイティムは初期化
    ↓
【rank_calculator.py】
├─ 各アイティムの伸び率計算
├─ 複合スコア計算（0-100）
└─ ランク割り当て（S/A/B/C/D/E）
    ↓
【monitor.db】(更新)
    ├─ ebay_listings テーブル: ランク + スコア + メトリクス
    └─ 498件全てのランクが朝5:07までに確定
```

---

## データベース・ストレージ設計

### ファイルシステム構成

```
C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager\
│
├─ app.py                      ← Streamlit メインUI（1000+行）
├─ daily_scheduler.py          ← 定時実行エンジン
├─ run_scheduler.py            ← スケジューラー起動
├─ scheduler_integration.py    ← スケジューラー設定
│
├─ calculator.py               ← 利益計算エンジン
├─ sku_conversion.py           ← SKU→URL変換
├─ sku_mapping_manager.py      ← SKU マッピング管理
├─ company_integration.py      ← 秘書統合
├─ execution_logger.py         ← ロギング
├─ ui_themes.py                ← UI テーマ
│
├─ qa_tester.py                ← QA テスター（手動実行）
│
├─ data/
│   ├─ sku_mappings.json       ← SKU→URL マッピングルール
│   ├─ inventory_check_results.csv   ← 348件在庫チェック結果
│   ├─ inventory_check_results.json  ← JSON形式
│   ├─ ShippingRates.csv       ← 送料テーブル
│   ├─ EbayFeeRates.csv        ← eBay手数料テーブル
│   ├─ research_candidates.json ← リサーチ候補
│   └─ processed_candidates.json ← 処理済み候補
│
├─ monitor/
│   ├─ database.py             ← SQLiteDB管理（37KB）
│   ├─ ebay_client.py          ← eBay APIクライアント
│   ├─ ebay_sync.py            ← API同期
│   ├─ rank_calculator.py      ← ランク計算
│   ├─ notifier.py             ← アラート通知
│   ├─ runner.py               ← タスク実行ラッパー
│   └─ monitor.db              ← SQLite（3テーブル）
│
├─ tasks/
│   ├─ task_company_secretary.py     ← 秘書ルーティン
│   ├─ task_email_pickup.py          ← メール確認
│   ├─ task_research.py              ← デイリーリサーチ
│   ├─ task_ebay_sync.py             ← eBay同期
│   ├─ task_inventory_check.py       ← 在庫チェック
│   ├─ task_product_search.py        ← 商品リサーチ
│   ├─ task_process_search_results.py ← 結果処理
│   ├─ task_enrich_ebay_data.py      ← データ拡張
│   ├─ task_supplier_select.py       ← 仕入先選定
│   ├─ task_calculate_max_cost.py    ← 価格計算
│   ├─ task_inventory_alert.py       ← 在庫アラート
│   ├─ task_news_check.py            ← ニュース確認
│   └─ task_rival_detection.py       ← 競合検出
│
└─ .company/
    ├─ CLAUDE.md                   ← 組織ルール
    ├─ secretary/
    │   ├─ CLAUDE.md
    │   ├─ todos/
    │   │   ├─ 2026-04-12.md
    │   │   └─ 2026-04-13.md
    │   ├─ inbox/
    │   │   └─ 2026-04-13.md
    │   ├─ notes/
    │   │   ├─ 2026-04-13-decisions.md
    │   │   ├─ 2026-04-13-learnings.md
    │   │   ├─ 2026-04-13-ai-news-summary.md（新）
    │   │   └─ 2026-04-13-improvement-opportunities.md（新）
    │   └─ routine_results/
    │       └─ 2026-04-13-routine.json
    ├─ research/
    │   ├─ CLAUDE.md
    │   ├─ topics/
    │   │   └─ daily-research-criteria.md
    │   └─ notes/
    │       ├─ 2026-04-13-new-product-research.md
    │       ├─ 2026-04-13-product-search.md（随時）
    │       ├─ 2026-04-13-processed-analysis.md（随時）
    │       ├─ processed-ai-news-summary.md（随時）
    │       └─ rival-analysis.md（随時）
    ├─ finance/
    │   └─ 2026-04-monthly-report.md
    └─ ebay-knowledge/
        ├─ policies.md
        ├─ shipping-guide.md
        └─ faq.md
```

---

## UI・ダッシュボード統合

### Streamlit ページ構成（app.py 1000+行）

```
Streamlit アプリ構成

【サイドバー】
├─ ログインユーザー表示
├─ ページナビゲーション（タブ選択）
├─ 定時実行スケジュール表示
├─ 最後の定時実行時刻
└─ ⚙️ 設定パネル

【ページ 1】 ダッシュボード（メイン）
├─ KPI サマリー（売上、利益率、ランク分布）
│
├─ 📧 秘書ルーティン結果セクション（新）
│   ├─ メール確認: X件（eBay関連: Y件）
│   ├─ TODO繰越: Z件
│   ├─ リサーチ進捗: 候補 W個
│   ├─ AIニュース: 新情報 V件
│   └─ 改善機会: 検討 U項目
│
├─ 📊 eBay出品管理
│   ├─ ランク分布グラフ（S/A/B/C/D/E の個数）
│   ├─ 売上動向（30日グラフ）
│   ├─ 出品詳細テーブル（498件、ソート/フィルタ可能）
│   └─ 🔄 「eBay同期実行」ボタン
│
├─ 🔍 仕入先在庫状況
│   ├─ 在庫状況の円グラフ
│   │   ├─ 在庫有: XX件（XX%）
│   │   ├─ 在庫無: XX件（XX%）
│   │   ├─ ページなし: XX件（XX%）
│   │   └─ エラー: XX件（XX%）
│   ├─ プラットフォーム別詳細テーブル
│   └─ 🔍 「在庫チェック実行」ボタン
│
└─ 💰 利益管理（簡易）
    ├─ 月次売上
    ├─ 月次利益
    └─ 平均利益率

【ページ 2】 eBay連携
├─ API接続状態
├─ 最終同期時刻・同期件数
├─ ランク計算の詳細説明
├─ メトリクス表示（watch/view/sales の伸び率）
└─ 🔄 「同期実行」ボタン

【ページ 3】 利益管理
├─ 月次売上・利益
├─ カテゴリ別利益率
├─ 📊 シミュレーター（対話的）
│   ├─ 仕入価格を入力 → 利益計算
│   ├─ 目標利益率から逆算
│   └─ 複数パターンの比較
└─ 詳細レポート（CSV DL）

【ページ 4】 商品リサーチ（随時実行）
├─ リサーチ条件フォーム
│   ├─ カテゴリ選択
│   ├─ 価格範囲
│   ├─ 期間
│   ├─ 除外キーワード
│   └─ 最小販売数
├─ 🔍 「リサーチ実行」ボタン
├─ 候補商品テーブル（利益率で自動ソート）
├─ 仕入先候補表示
└─ SKU自動生成

【ページ 5】 SKU管理
├─ SKU→URL マッピングルール一覧
├─ ルール追加・編集フォーム
├─ テスト実行（SKUを入力 → URL表示）
└─ JSON エクスポート

【ページ 6】 競合分析（随時実行）
├─ 仕入先別の在庫トレンド
├─ 稀有な在庫商品（仕入れ困難）
├─ 競合者ランキング
├─ 価格ダンピング検出
└─ 対抗戦略の提案

【ページ 7】 学習管理
├─ YouTube 学習パイプライン（手動実行）
├─ 学習済み動画一覧
├─ eBay知識ベース
└─ 🎓 「新規学習を追加」ボタン

【ページ 8】 ログ・通知
├─ 定時実行ログビューア
├─ エラーレポート（フィルタ可能）
├─ 通知履歴
└─ 手動実行履歴
```

---

## エラーハンドリング

### 3段階のエラー分類

```
【Level 1】自動リカバリー可能（ユーザー介入不要）
├─ TimeoutException（ページ読み込み遅延）
│   └─ 指数バックオフリトライ（3回：1s, 2s, 4s）
├─ APIレート制限（一時的）
│   └─ 待機してリトライ
└─ 一時的なネットワーク障害
    └─ リトライロジック（最大3回）

【Level 2】ログ記録・部分継続（ユーザー確認推奨）
├─ SKU マッピング未対応
│   └─ 自有在庫と判別、警告ログ
├─ 仕入先ページ削除（ページなし）
│   └─ status = "ページなし" で記録
├─ 単一アイティム処理失敗
│   └─ 全体処理は続行
└─ eBay API 部分的レスポンス
    └─ 取得成功分で続行、未取得件数ログ

【Level 3】手動介入必要（プロセス停止）
├─ eBay API 認証失敗
│   └─ メール通知、ユーザー対応待機
├─ SQLiteDB ロック
│   └─ リトライ後も失敗 → スキップ + ログ
└─ Selenium ドライバークラッシュ（復旧不可）
    └─ プロセス停止 + メール通知
```

---

## 実装チェックリスト

### 本番運用開始前

- [ ] monitor/scrapers.py 削除 ✅
- [ ] monitor.db 初期化＋498件同期
- [ ] .company/ ディレクトリ完成
- [ ] daily_scheduler.py スケジュール設定（朝5:00自動実行）
- [ ] eBay API credentials 設定
- [ ] Gmail OAuth 認証設定
- [ ] Selenium WebDriver インストール
- [ ] エラー通知テスト
- [ ] Streamlit app.py 全ページ動作確認
- [ ] データベース読み込み確認
- [ ] リアルタイム更新確認

---

## 拡張計画（Phase 5以降）

### Phase 5: リアルタイム通知システム
- Discord ボット統合
- Slack ワークスペース連携
- メール自動レポート

### Phase 6: AI ベース仕入れ推奨
- 機械学習で「買い時」「売り時」予測
- 季節性・トレンド抽出
- 利益率最適化

### Phase 7: マルチマーケットプレイス
- Amazon Global セリング
- Mercado Libre 対応
- 自社 EC サイト連携

### Phase 8: 完全自動化
- 自動価格最適化
- 自動出品・下出品
- 自動仕入れ提案

---

**この設計書をベースに PowerPoint資料を作成します。**
