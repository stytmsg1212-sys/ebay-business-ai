---
feature: W9 個別新規出品
version: 1.1
status: confirmed (Q1-Q5 全決定済 2026-04-20)
created: 2026-04-20
updated: 2026-04-20
author: engineering (feature-dev:code-architect)
migration: v14, v15
recommended_model: claude-sonnet-4-6
estimated_duration: 3日 + バッファ 0.5日
---

# W9 個別新規出品機能 PRD兼アーキテクチャ設計書 v1.1

## 更新履歴

### v1.1 (2026-04-20) - Q1-Q5 全決定後の確定版
- **Q1 確定**: Payment Policy `359244671023` / Return Policy `359243687023` / Shipping は重量×在庫で自動選択 (settings.json 登録済)
- **Q2 確定**: ドラフト方式 = **ScheduledTime 21日後** (AddFixedPriceItem + ScheduledTime)
- **Q3 確定**: description テンプレート v3 完成 + 8段階ランク体系 (N/S/A/B/C/D/PO/As-Is) 適用
- **Q4 確定**: 対応プラットフォーム = ヤフオク / メルカリ / PayPayフリマ の3つのみ (他サイトは実運用で需要確認後に追加)
- **Q5 確定**: **入力を2項目に変更** — 仕入先URL (必須) + 参考eBay URL (任意・推奨)
  - 参考eBay URL 提供時: GetItem API で Category/Item Specifics Keys 自動取得 (コピー禁止項目あり)
  - なし時: Claude 3候補提示 + 手動ID入力フォールバック

### v1.0 (2026-04-20 初版)
- feature-dev:code-architect エージェントによる PRD ドラフト
- 未確定事項 Q1-Q5 を識別

---

## 0. Q1-Q5 確定事項 サマリー (v1.1 で追加)

### Q1: eBay Business Policies
- **Payment**: `359244671023` (Managed Payments)
- **Return**: `359243687023` (Return)
- **Shipping**: DDP 系ポリシーから **重量 (g) × 在庫有無** で自動選択 (20個のID→settings.json に mapping 登録済)
  - 在庫あり=1day出荷系 10ID / 在庫なし=7day出荷系 10ID

### Q2: ドラフト方式
- **ScheduledTime = now + 21日** で AddFixedPriceItem 実行
- Seller Hub → Listings → **Scheduled** タブに表示
- ユーザーが随時編集・「List now」で即公開・21日で自動公開
- 完全非公開ドラフト (Inventory API) は W9 対象外、将来のOAuth対応時に検討

### Q3: description テンプレート
- **v3 完成版** を `.company/ebay-knowledge/topics/listing-description-template.md` に永続化
- **プレースホルダ**: `{product_name}` `{rank}` `{rank_label}` `{quick_notes}` `{includes}` `{dimensions_weight}` `{long_description}` `{shipping_notes}`
- **8段階ランク体系**: N / S / A / B / C / D / PO / As-Is (`feedback_condition_rank_system.md` 参照)
- Claude がランクを自動推定 → eBay Condition ID も自動マップ
- **Payment / Shipping / Voltage Warning** は全商品常時表示
- **Rank Definition Table** は aside 冒頭 (Payment の前) に配置

### Q4: 対応プラットフォーム
- ヤフオク / メルカリ / PayPayフリマ の **3つのみ**
- ラクマ / HardOff / 駿河屋等は実運用で需要確認後に W9 拡張として追加検討

### Q5: 入力設計 (★重大変更)
- **入力を2項目に再定義**:
  1. 仕入先URL (必須) — ヤフオク/メルカリ/PayPay
  2. 参考eBay URL/ItemID (任意・推奨) — ライバルセラー or 自分の過去出品
- **参考eBay URL 提供時**: GetItem API で以下を自動取得
  - ✅ **コピー**: PrimaryCategory.CategoryID / Item Specifics の Key 構造
  - 🟡 **参考のみ**: Title キーワード構造 (SEO分析用) / ConditionID
  - ❌ **コピー禁止**: Description 本文 / Images / Price (VeRO・著作権リスク)
- **参考eBay URL なし時**: Claude 3候補 + 手動入力フォールバック

---

## 1. 目的と背景

### 目的
eBay Manager に「個別新規出品」機能を追加し、ユーザーが仕入先で発見した商品を
1件ずつ手動で eBay にドラフト出品できるようにする。

### 背景
現状のワークフローは「既存出品の在庫切れ→仕入先候補探索→SKU付け替え」という
補充フローのみ。ユーザーが新たに見つけた商品を出品するには eBay Seller Hub で
全項目を手入力する必要があり、英語タイトル生成・Item Specifics 記入・description 作成に
多大な時間がかかる。

W9 は以下の自動化でこの課題を解決する:
- 仕入先 URL から商品情報を自動スクレイプ
- Claude Sonnet が SEO 英語タイトル・description 生成
- Trading API AddFixedPriceItem でドラフト作成（公開は Seller Hub で最終校正後）

---

## 2. スコープ

### 含む (In Scope)
- 仕入先 URL（ヤフオク/メルカリ/PayPayフリマ）のスクレイプ
- Claude Sonnet による英語タイトル・description・Item Specifics 生成
- description テンプレート管理 UI（登録・編集・削除）
- eBay Trading API AddFixedPriceItem でドラフト作成
- 生成結果のプレビュー UI（eBay Manager 内）
- DB への draft 保存（再編集・再投稿対応）
- 「画像加工未実施」警告表示 + W10 連動差込点の用意

### 含まない (Out of Scope)
- 画像の加工・リサイズ・透かし除去（W10 に委譲）
- 即時公開（ドラフト作成のみ。公開は Seller Hub 操作）
- 一括出品（1件ずつ手動が前提）
- eBay Inventory API / Business Policies の自動取得（設定画面に手入力フィールドを用意）
- 利益計算との自動統合（計算タブは別途手動利用）

---

## 3. ユースケース

### UC1: 新規カテゴリ商品を発見→即ドラフト
ユーザーがヤフオクでレアな測定器を落札予定。eBay Manager の「個別出品」タブで出品先 URL を貼り付け、自動スクレイプ→Claude 生成→プレビュー確認→ドラフト保存を5分以内で完了する。

### UC2: description テンプレートをカテゴリ別に使い分け
「家電・AV機器用」と「産業機器用」の2テンプレートを登録。出品時に select box でテンプレートを選択すると、プレースホルダが自動置換される。

### UC3: 生成結果を手修正してから保存
Claude が生成した英語タイトルが長すぎる場合、UI 内のテキストエリアを直接編集して80字以内に短縮してから「ドラフト保存」を実行する。

### UC4: 仕入先 404 エラーの対応
出品先 URL が削除済みの場合、スクレイプエラーを即座に表示する。ユーザーは URL を修正して再試行するか、画像 URL・タイトルを手動入力して続行できる。

### UC5: 過去ドラフトの再利用
保存済みドラフトの一覧から過去の生成結果を呼び出し、価格を変更して再度 AddFixedPriceItem を実行する。

---

## 4. 機能要件

### MUST（必須）

| ID | 要件 |
|----|------|
| M1 | 仕入先 URL 入力欄（ヤフオク/メルカリ/PayPayフリマに対応） |
| M2 | Playwright sync でページスクレイプ（title/price/condition/images） |
| M3 | スクレイプ失敗時の手動入力フォールバック（タイトル/状態/画像URL） |
| M4 | description テンプレート登録・編集・削除 UI |
| M5 | Claude Sonnet による英語タイトル生成（80字以内、SEO最適化） |
| M6 | Claude Sonnet による description 生成（テンプレートプレースホルダ置換） |
| M7 | Claude Sonnet によるカテゴリ推定（eBay category ID）と最小 Item Specifics |
| M8 | 生成結果のプレビューと手修正（UI 上でテキスト編集可） |
| M9 | AddFixedPriceItem でドラフト作成（quantity=0、非公開相当） |
| M10 | ドラフト作成結果（ItemID）を listing_drafts テーブルに保存 |
| M11 | 「画像加工未実施」警告バナー表示（W10 連動まで常時表示） |
| M12 | SKU 手動入力欄（ドラフトと紐付け） |
| **M13** | **参考eBay URL/ItemID 入力欄**（任意だが推奨）— ライバル or 自分の過去出品 |
| **M14** | **GetItem API 呼出し**で Category / Item Specifics Keys / ConditionID を参考listingから取得 |
| **M15** | **Item Specifics の Key 構造のみコピー、値は Claude が仕入先情報から埋める**。Description/Image/Price はコピー禁止 |
| **M16** | **ScheduledTime = now + 21日** で AddFixedPriceItem を呼び、Seller Hub の Scheduled タブに置く (Q2確定) |
| **M17** | **8段階ランク (N/S/A/B/C/D/PO/As-Is) 自動推定** + eBay Condition ID 連動マップ (Q3確定) |
| **M18** | **重量 × 在庫有無から Shipping Policy ID を自動選択** (Q1確定、settings.json mapping 使用) |

### SHOULD（推奨）

| ID | 要件 |
|----|------|
| S1 | スクレイプ結果の画像URL一覧表示（最大10枚、チェックボックスで選択） |
| S2 | 利益計算タブへの入力値引継ぎリンク（仕入価格→計算タブ自動入力） |
| S3 | 過去ドラフト一覧（最新20件、再編集・再送信ボタン付き） |
| S4 | eBay カテゴリ候補3件をラジオボタンで選択 |
| S5 | AddFixedPriceItem 送信前の VerifyAddFixedPriceItem dry-run |

### COULD（あれば良い）

| ID | 要件 |
|----|------|
| C1 | 型番テキストからの仕入先検索自動起動（supplier_candidate_search 連携） |
| C2 | eBay 既存 ItemID 指定で既存 listing を雛型読込（GetItem → 上書き draft） |
| C3 | description HTML プレビュー（iframe による実際の表示確認） |

---

## 5. 非機能要件

### パフォーマンス
- スクレイプ + Claude 生成の合計: 30秒以内を目標（タイムアウト上限60秒）
- スクレイプは threading.Thread でバックグラウンド実行し UI をブロックしない
- st.status を使ったプログレス表示（スクレイプ中/生成中/ドラフト作成中の3ステップ）

### エラーハンドリング
- 仕入先 URL 404: エラーメッセージ + 手動入力フォールバック UI を即時表示
- Claude API 失敗: エラー表示 + 「手動でタイトル/descriptionを入力して続行」オプション
- AddFixedPriceItem 失敗: API エラーメッセージを表示。ドラフトは DB に status='api_failed' で保存
- Playwright タイムアウト: 15秒でキャンセル + httpx フォールバック（既存 scrapers.py パターンを踏襲）

### レート制限
- AddFixedPriceItem: eBay Trading API の1日あたり制限（通常5000コール）。手動1件ずつなので問題なし
- Claude API: 仕入先候補評価と同じ claude_evaluator.py の _get_client() を共用。コスト追跡は api_call_log テーブル（既存）に operation='individual_listing' で記録
- Playwright: sync_playwright を使用（既存 mercari_search.py/yahoo_search.py/paypay_search.py と同パターン）。スレッドから呼ぶ場合は subprocess 経由（既存制約に準拠）

### セキュリティ
- eBay 認証情報は既存の credentials.py / .env パターンを踏襲。ハードコード禁止
- description HTML はサニタイズ（xml.sax.saxutils.escape）してから API に送信
- 仕入先画像URLは src 属性に直接埋め込み（script 注入不可）

---

## 6. データ設計

### マイグレーション番号
現在 v13 まで確認済み。本機能は v14（description_templates）と v15（listing_drafts）を追加する。

### v14: description_templates テーブル

```sql
CREATE TABLE IF NOT EXISTS description_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,               -- ユーザー表示名（例: "家電・AV機器用"）
    body TEXT NOT NULL,                       -- テンプレート本文（HTML可）
    -- プレースホルダ: {product_name} {condition} {includes} {dimensions} {warranty} {shipping_notes}
    is_default INTEGER DEFAULT 0,            -- 1=デフォルト選択
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_desc_templates_default
    ON description_templates (is_default DESC);
```

### v15: listing_drafts テーブル (v1.1 で列追加)

```sql
CREATE TABLE IF NOT EXISTS listing_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT,                                -- 手動入力 SKU（任意）
    supplier_url TEXT,                       -- 仕入先URL（スクレイプ元）
    supplier_platform TEXT,                  -- 'yahoo_auctions'/'mercari'/'paypay'/NULL
    supplier_title_ja TEXT,                  -- スクレイプしたタイトル（日本語）
    supplier_price_jpy INTEGER,              -- スクレイプした価格（円）
    supplier_condition_ja TEXT,              -- スクレイプしたコンディション（日本語）
    supplier_includes_ja TEXT,               -- 付属品テキスト（日本語）
    supplier_image_urls TEXT,                -- JSON: スクレイプした画像URL群
    selected_image_urls TEXT,                -- JSON: ユーザーが選択した画像URL群
    -- ★ v1.1 Q5 追加: 参考eBay URL 関連
    reference_ebay_url TEXT,                 -- 任意: ライバル or 自分の過去出品URL
    reference_ebay_item_id TEXT,             -- URL から抽出したItemID
    reference_category_id TEXT,              -- GetItem で取得した CategoryID (採用元)
    reference_item_specifics_keys TEXT,      -- JSON: ["Brand","Model","Type",...] 参考ListingのItemSpecificsキー一覧
    reference_condition_id TEXT,             -- 参考ListingのConditionID（参考までに）
    -- 8段階ランク体系 (v1.1 Q3)
    rank_code TEXT,                          -- 'N'/'S'/'A'/'B'/'C'/'D'/'PO'/'As-Is'
    rank_label TEXT,                         -- 'Like New' / 'Excellent' 等 (英語ラベル)
    quick_notes TEXT,                        -- "Tested working..." 等の個別メモ
    -- eBay出品データ
    ebay_title TEXT,                         -- Claude生成の英語タイトル
    ebay_description TEXT,                   -- Claude生成のdescription HTML (テンプレv3にプレースホルダ埋込)
    ebay_category_id TEXT,                   -- 最終決定CategoryID (参考URLあれば同値、なければClaude推定)
    ebay_category_name TEXT,
    ebay_condition_id TEXT,                  -- rank_code から自動マップ
    item_specifics TEXT,                     -- JSON: {"Brand":"Sony","Model":"WH-1000XM5",...}
    listing_price_usd REAL,
    -- ShippingPolicy 自動選択 (v1.1 Q1)
    weight_g INTEGER,                        -- 出品物の重量 (ShippingPolicy選択用)
    in_stock INTEGER DEFAULT 1,              -- 0/1: 在庫あり=1 (1day出荷Policy) / 在庫なし=0 (7day出荷Policy)
    shipping_policy_id TEXT,                 -- 自動決定した ShippingPolicy ID
    template_id INTEGER,
    -- AddFixedPriceItem 結果
    scheduled_time TIMESTAMP,                -- AddFixedPriceItem に渡した予定時刻 (= created_at + 21日)
    ebay_item_id TEXT,                       -- AddFixedPriceItem で返された ItemID
    status TEXT DEFAULT 'draft',             -- 'draft'/'submitted'/'api_failed'/'published'
    api_error_message TEXT,
    processed_image_urls TEXT,               -- W10 加工後画像URL (NULL=未加工)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES description_templates(id)
);
CREATE INDEX IF NOT EXISTS idx_listing_drafts_status
    ON listing_drafts (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listing_drafts_sku
    ON listing_drafts (sku);
CREATE INDEX IF NOT EXISTS idx_listing_drafts_ebay_item
    ON listing_drafts (ebay_item_id);
```

### init_db への追記位置
`monitor/database.py` の `init_db()` 末尾（v13 ブロックの直後）に v14, v15 を追記する。

---

## 7. モジュール分割

### 新規作成ファイル (v1.1 更新)

| ファイルパス | 役割 |
|---|---|
| `tools/ebay-manager/monitor/supplier_scraper.py` | 仕入先URL→商品情報スクレイプ。ヤフオク/メルカリ/PayPayフリマを platform 判定して dispatch。MercariHit/YahooHit/PayPayHit を共通 ScrapedProduct dataclass に変換する |
| **`tools/ebay-manager/monitor/ebay_reference_fetcher.py`** (新) | **参考eBay URL/ItemID から GetItem API で Category/Item Specifics Keys/ConditionID を取得。URL パーサ + GetItem 呼出し + レスポンス parse** |
| `tools/ebay-manager/monitor/listing_generator.py` | Claude Sonnet を呼び出して英語タイトル/description/Item Specifics を生成。**参考listingの Item Specifics Keys があればそれを骨格に値を埋める**。3層キャッシュ設計（STABLE+DYNAMIC+reference）|
| **`tools/ebay-manager/monitor/shipping_policy_selector.py`** (新) | **重量×在庫有無から settings.json の ebay_business_policies mapping を引いて ShippingPolicy ID を選択** |
| **`tools/ebay-manager/monitor/rank_classifier.py`** (新) | **仕入先日本語記述 + Claude で 8段階ランク自動推定。feedback_condition_rank_system.md のルール実装** |
| `tools/ebay-manager/monitor/ebay_lister.py` | AddFixedPriceItem / VerifyAddFixedPriceItem の XML 構築と Trading API 呼出し。**ScheduledTime=now+21日を常に付与**。_call_trading_api() を ebay_client.py からインポートして再利用 |
| `tools/ebay-manager/tabs/tab_individual_listing.py` | 「個別出品」タブの Streamlit UI ロジック。**仕入先URL + 参考eBay URL の2入力**に対応 |
| `tools/ebay-manager/tabs/tab_description_templates.py` | descriptionテンプレート設定サブタブの UI ロジック。**v3テンプレを初期シードとしてDBに投入** |
| `tools/ebay-manager/tabs/__init__.py` | tabs パッケージ init |

### 修正ファイル

| ファイルパス | 修正内容 |
|---|---|
| `tools/ebay-manager/monitor/database.py` | init_db() 末尾に v14/v15 マイグレーションブロックを追記。get_description_templates(), save_description_template(), delete_description_template(), get_listing_drafts(), save_listing_draft(), update_listing_draft_status() 関数を追加 |
| `tools/ebay-manager/app.py` | st.tabs() のリストに "個別出品" タブを追加（tab_listing）。from tabs.tab_individual_listing import render_tab as render_listing_tab を追加。with tab_listing: render_listing_tab(s) を追記 |

---

## 8. 処理フロー (v1.1 更新)

### 8.1 ドラフト作成処理 (2入力対応版)

```
User → UI → supplier_scraper → [ebay_reference_fetcher] → rank_classifier → listing_generator → shipping_policy_selector → ebay_lister → DB

1. 【必須】仕入先URL入力 → scrape(url) [threading]
   Playwright GET → title/price/condition/images/weight → ScrapedProduct

2. 【任意】参考eBay URL/ItemID入力 → fetch_reference(itemid) [threading, 並行]
   ItemID抽出 → GetItem API → extract(CategoryID, ItemSpecificsKeys, ConditionID)
   → ReferenceListing (None可)

3. rank_classifier(scraped_product)
   → rank_code (N/S/A/B/C/D/PO/As-Is) + rank_label + eBay Condition ID

4. listing_generator(scraped_product, reference_listing, rank)
   Claude Sonnet:
   - if reference_listing: Item Specifics keys を骨格にして値を埋める
   - else: Category は Claude 3候補提示 (ユーザー選択 or 手動ID入力)
   - Title (英語, SEO, 80字以内) 生成
   - Description テンプレ v3 にプレースホルダ埋込
   → GeneratedListing

5. User プレビュー + 手修正 (UI で編集可)

6. shipping_policy_selector(weight_g, in_stock)
   → settings.json の ebay_business_policies mapping 参照
   → shipping_policy_id

7. 「ドラフト保存」 → ebay_lister.add_fixed_price_item_draft()
   - ScheduledTime = now + 21日 (Q2確定)
   - PaymentPolicyID = 359244671023 (Q1)
   - ReturnPolicyID = 359243687023 (Q1)
   - ShippingPolicyID = 自動選択 (手順6)
   - VerifyAdd (dry-run) → AddFixedPriceItem → ItemID

8. save_draft(ItemID, all_data) → status='submitted'
   → 完了表示: "Scheduled to go live on YYYY-MM-DD HH:MM"
```

### 8.2 並行処理可能箇所
- スクレイプ（Playwright）は `threading.Thread(daemon=True)` でバックグラウンド実行し、完了後 `st.rerun()` で結果を反映する（video_learning タブの既存パターンを踏襲: app.py L2621-2625）
- Claude 生成も同様に threading.Thread で実行する
- AddFixedPriceItem 前の VerifyAddFixedPriceItem は直列（順序依存）

### 8.3 エラー処理フロー

```
仕入先 404/タイムアウト
  → st.error("URLが無効か削除されています")
  → "手動入力モード" ボタンを表示
  → フォールバック: タイトル/状態/画像URLを手動入力して続行

Claude API 失敗
  → st.error(f"生成失敗: {error_message}")
  → "手動入力で続行" ボタンを表示
  → 空のテキストエリアを開いてユーザー手入力

AddFixedPriceItem 失敗
  → st.error(f"eBay APIエラー: {api_error}")
  → draft を status='api_failed' で DB 保存（内容は失わない）
  → "再試行" ボタンを表示（保存済みdraftから再送信）
```

---

## 9. 画面設計

### 9.1 「個別出品」タブ Wireframe

```
========================================================
 個別出品
 eBayに1件ずつ手動で出品する。AddFixedPriceItemでドラフト作成。
========================================================

[サブタブ] 新規出品 | 保存済みドラフト | テンプレート設定
--------------------------------------------------------
■ 新規出品

 仕入先 URL
 [_______________________________________________]  [スクレイプ]

 --- スクレイプ結果 (st.status で展開) ---
  タイトル（日本語）: ソニー WH-1000XM5 ブラック 美品
  価格:               32,000 円
  コンディション:     中古（美品）
  付属品:             箱あり、説明書あり
  画像:  [img1] [img2] [img3] ...  (チェックで選択)

 --- 出品設定 ---
 SKU（任意）: [__________]
 出品価格（USD）: [_______]
 description テンプレート: [家電・AV機器用 v]

 [生成] ←ボタン

 !!! 画像加工未実施: 仕入先の画像URLをそのまま使用します。
     W10 実装後に画像加工が可能になります。

 --- 生成結果プレビュー ---
 eBay タイトル（英語）:
 [Sony WH-1000XM5 Wireless Headphones Black Excellent___]  (80/80)

 カテゴリ:  [Consumer Electronics > Headphones (3) v]
 コンディション: [Used (3000) v]

 Item Specifics:
  Brand: Sony | Type: Wireless | Color: Black

 Description プレビュー:
 [------ HTML テキストエリア (300px) ------]

 [ドラフト保存してeBayに登録]  [クリア]
========================================================
```

### 9.2 「テンプレート設定」サブタブ Wireframe

```
========================================================
 description テンプレート設定
========================================================

 登録済みテンプレート一覧
 ---
 [家電・AV機器用]  [デフォルト]  [編集] [削除]
 [産業機器用]                    [編集] [削除]
 ---

 テンプレート追加/編集
  名前: [__________________]
  本文:
  [<p>{product_name}</p>                              ]
  [<ul>                                               ]
  [  <li>Condition: {condition}</li>                  ]
  [  <li>Includes: {includes}</li>                    ]
  [  <li>Dimensions: {dimensions}</li>                ]
  [  <li>Warranty: {warranty}</li>                    ]
  [  <li>Shipping: {shipping_notes}</li>              ]
  [</ul>                                              ]
  (テキストエリア 200px)

  利用可能なプレースホルダ:
  {product_name} {condition} {includes} {dimensions} {warranty} {shipping_notes}

  [x] デフォルトに設定

  [保存]  [キャンセル]
========================================================
```

---

## 10. 実装順序（TODO分解）

### Phase 1: DB・基盤（0.5日）
- [ ] monitor/database.py: v14 description_templates マイグレーション追加 | 優先度: 高
- [ ] monitor/database.py: v15 listing_drafts マイグレーション追加 | 優先度: 高
- [ ] monitor/database.py: get/save/delete_description_template(), get/save/update_listing_draft() 関数追加 | 優先度: 高
- [ ] tests/test_w9_db.py: DB CRUD 単体テスト（Pytest, インメモリ DB） | 優先度: 高

### Phase 2: スクレイパ統合（0.5日）
- [ ] monitor/supplier_scraper.py: ScrapedProduct dataclass 定義 | 優先度: 高
- [ ] monitor/supplier_scraper.py: platform 判定関数 _detect_platform(url) | 優先度: 高
- [ ] monitor/supplier_scraper.py: scrape_supplier_url(url) → ScrapedProduct | 優先度: 高
  - ヤフオク: yahoo_search.py の YahooHit と同じ Playwright 構造を単一URL版に拡張
  - メルカリ: mercari_search.py の MercariHit 同様、itemページ直接アクセス版
  - PayPayフリマ: paypay_search.py の PayPayHit 同様
  - フォールバック: httpx + scrapers.py の _check_with_httpx() パターン
- [ ] tests/test_supplier_scraper.py: 各プラットフォームのモックテスト | 優先度: 通常

### Phase 3: Claude 生成モジュール（0.5日）
- [ ] monitor/listing_generator.py: GeneratedListing dataclass 定義 | 優先度: 高
- [ ] monitor/listing_generator.py: STABLE_PROMPT（出品指示/SEO基準/日本語→英語変換ルール）| 優先度: 高
- [ ] monitor/listing_generator.py: generate_listing(product, template_body, sku) → GeneratedListing | 優先度: 高
  - STABLE キャッシュ: 出品指示部分（ebay_title SEOルール/description構成/specifics抽出ルール）
  - DYNAMIC: 商品データ（タイトル/状態/付属品/価格）
  - api_call_log への記録（operation='individual_listing'）
- [ ] tests/test_listing_generator.py: Claude API をモックした単体テスト | 優先度: 通常

### Phase 4: eBay API 連携（0.5日）
- [ ] monitor/ebay_lister.py: _build_add_fixed_price_item_xml() | 優先度: 高
- [ ] monitor/ebay_lister.py: verify_add_fixed_price_item() | 優先度: 高
- [ ] monitor/ebay_lister.py: add_fixed_price_item_draft() → dict {item_id, fees} | 優先度: 高
  - _call_trading_api() を ebay_client.py からインポートして XML 送信部分を再利用
  - quantity=0 で Seller Hub 非公開ドラフト相当
- [ ] tests/test_ebay_lister.py: XML 構造の単体テスト（API mock） | 優先度: 通常

### Phase 5: UI 実装（1日）
- [ ] tabs/__init__.py 作成 | 優先度: 高
- [ ] tabs/tab_description_templates.py: テンプレート CRUD UI | 優先度: 高
- [ ] tabs/tab_individual_listing.py: メイン出品フォーム UI | 優先度: 高
  - スクレイプ→生成→プレビュー→ドラフト保存の st.status 連鎖フロー
  - threading.Thread でスクレイプと生成をバックグラウンド実行
  - 保存済みドラフト一覧サブタブ
- [ ] app.py: "個別出品" タブ追加（st.tabs に追記、with tab_listing: render_listing_tab(s)）| 優先度: 高

### Phase 6: E2E テスト・調整（0.5日）
- [ ] 手動 E2E: ヤフオク実URL → スクレイプ → Claude 生成 → VerifyAddFixedPriceItem dry-run | 優先度: 高
- [ ] code-reviewer 実行 → HIGH 0件確認 | 優先度: 高
- [ ] W10 連動差込点のコメント確認（processed_image_urls カラム・upload_images() 関数stub） | 優先度: 通常

---

## 11. テスト計画

### 単体テスト（Pytest）

| テストファイル | カバー範囲 |
|---|---|
| tests/test_w9_db.py | description_templates CRUD / listing_drafts CRUD / status 遷移 |
| tests/test_supplier_scraper.py | platform 判定 / ScrapedProduct dataclass / httpx フォールバック |
| tests/test_listing_generator.py | GeneratedListing 構造 / テンプレートプレースホルダ置換 / Claude API mock |
| tests/test_ebay_lister.py | AddFixedPriceItem XML 構造 / VerifyAdd dry-run / エラーレスポンス処理 |

### E2E テスト（手動）

| シナリオ | 合格条件 |
|---|---|
| ヤフオク実URL → ドラフト作成 | ItemID 取得 + listing_drafts に status='submitted' で保存 |
| メルカリ実URL → ドラフト作成 | 同上 |
| 存在しない URL → フォールバック | エラーメッセージ + 手動入力フォーム表示 |
| Claude API オフライン → フォールバック | 生成スキップ + 手動入力で ドラフト保存可能 |
| テンプレート登録・編集・削除 | DB 反映確認 / デフォルト切替 |
| 保存済みドラフト再送信 | 新 ItemID 取得 + 旧 draft status='submitted'に更新 |

---

## 12. W10 連動ポイント

W10（画像加工機能）との将来の統合は以下の差込点を用意する。

### DB: listing_drafts.processed_image_urls カラム
NULL の間は supplier_image_urls（仕入先のまま）を使用。W10 が加工を完了したら processed_image_urls に上書きし、ReviseItem で画像差替えを実行する。

### ebay_lister.py: 引数 image_urls の切り替え
```python
def add_fixed_price_item_draft(
    ...,
    image_urls: list[str],  # processed_image_urls があれば優先、なければ selected_image_urls
) -> dict: ...
```

### UI: 警告バナーの制御ロジック
```python
# tab_individual_listing.py（将来対応）
if draft.get("processed_image_urls"):
    st.success("W10 加工済み画像を使用します")
else:
    st.warning("画像加工未実施: 仕入先の画像URLをそのまま使用します。W10実装後に画像加工が可能になります。")
```

### tasks/task_w10_image_processing.py（W10 実装時に追加）
1. listing_drafts から processed_image_urls=NULL かつ status='submitted' の drafts を取得
2. 画像を加工してホスティング（または eBay Picture Manager にアップロード）
3. processed_image_urls を更新
4. ebay_lister.revise_item_images(item_id, processed_image_urls) を呼び出し

---

## 13. リスクと対策

### R1: 仕入先サイトの DOM 変更（高頻度リスク）
**リスク**: ヤフオク/メルカリ/PayPayフリマのセレクタが変わるとスクレイプが壊れる。
**対策**: supplier_scraper.py に「スクレイプ失敗→手動入力フォールバック」を必須フローとして実装する。セレクタを定数にまとめ、変更時の修正コストを最小化。監視: スクレイプ失敗率が高い場合は api_call_log に記録してダッシュボードに警告表示。

### R2: eBay AddFixedPriceItem の必須項目漏れ（中頻度リスク）
**リスク**: eBay は出品に必須の Category/ConditionID/ShippingDetails/PaymentMethods 等が欠けると API エラーを返す。Claude が推定したカテゴリが不正確な場合に起きやすい。
**対策**: VerifyAddFixedPriceItem（dry-run）を先行実行し、エラー内容を UI に表示する。ShippingDetails/ReturnPolicy/SellerProfiles は settings.json に手入力させる（schedule_config.json の ebay.business_policy_id 等）。XML ビルダー関数で「必須フィールドのデフォルト値」を埋め込む。

### R3: Claude が生成するタイトルの品質ばらつき（低頻度だが高影響）
**リスク**: 日本語タイトルの機種依存文字・商品カテゴリ固有語で英語変換精度が下がる。
**対策**: UI でユーザーが最終確認・編集してからドラフト保存する前提設計。80字カウンターで文字数超過を即時フィードバック。将来的に listing_drafts の accepted 事例を past_judgments として Few-shot 注入（W10 以降のフェーズ）。

### R4: PayPayフリマ CORS/JavaScript SPA でスクレイプ失敗（高頻度リスク）
**リスク**: PayPayフリマは React SPA で画像セレクタが難読化されており、Playwright でも取得できないことがある（paypay_search.py の既知制約）。
**対策**: scrape_supplier_url() は 画像取得失敗時に image_urls=[] で ScrapedProduct を返し、UI で「画像URLを手動入力」フォームを出す。

---

## 14. モデル選定推奨

### 推奨: Claude Sonnet 4.6（claude-sonnet-4-6）

**根拠**:

1. **タスクの複雑度**: eBay 英語タイトル生成・description 埋め込み・カテゴリ推定・Item Specifics 抽出は「有界なテキスト変換タスク」。Opus が必要とされる長大なマルチステップ推論ではない。

2. **既存パターン踏襲**: claude_evaluator.py が同様の「日本語商品情報 → 英語評価/判定」タスクで Sonnet を採用し、品質に問題が出たら Opus に切り替えるという方針を確立済み。

3. **コスト**: AddFixedPriceItem の前に毎回 Claude を呼ぶため、1出品あたりのトークン消費が固定費になる。Sonnet ならトークン単価が Opus の約1/5。

4. **プロンプトキャッシュの効果**: STABLE_PROMPT（出品SEO指示・descriptionルール）をキャッシュ対象にすれば、同一セッション内の連続出品で cache_read が効き、コスト 1/4 相当になる。

5. **品質閾値**: 生成結果はユーザーが UI で確認・編集してから送信するため、「完璧な1回生成」よりも「修正しやすい下書き生成」が求められる。Sonnet で十分。

**Opus を検討するケース**: Item Specifics の推定精度が低くクレームにつながる事態が複数回発生した場合、または eBay カテゴリが非常にニッチ（産業機器の細分類など）で Sonnet の推定精度が著しく低い場合のみ、CLAUDE_MODEL を "claude-opus-4-7" に切り替える。

---

## 15. 実装前にユーザー確認が必要な5項目

### Q1: eBay Business Policy ID の入手
AddFixedPriceItem XML に必須の Shipping / Return / Payment の Policy ID を教えてほしい。settings.json に追加する。

### Q2: 「ドラフト」の定義
eBay の「量=0」で出品して Seller Hub で数量追加し公開 という運用でよいか？ あるいは Save Draft として完全非公開にしたいか？（API のドラフト扱いは数量0か Scheduled Listing のどちらか選択が必要）

### Q3: description テンプレートのデフォルト文面
空のテンプレートからユーザーが作成する方式でよいか？ それともこちらで雛型を1つ用意（英語/日本語併記のサンプル）するか？

### Q4: 出品対象プラットフォームの優先度
ヤフオク/メルカリ/PayPayフリマ以外（ラクマ・HardOff等）も仕入先としてスクレイプ対象に入れるか？

### Q5: eBay カテゴリ推定の精度が低い場合の対応
手動でカテゴリIDを入力する検索 UI が必要か（eBay FindingAPI との連携）、またはドロップダウンに手打ちで良いか？

---

## 関連ファイル絶対パス

- `C:/Users/gucch/projects/claude/tools/ebay-manager/app.py` — タブ追加箇所（L87、L3301周辺）
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/database.py` — init_db() マイグレーション追記箇所（L820 v13ブロック直後）
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/ebay_client.py` — _call_trading_api() 再利用元（L577）
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/claude_evaluator.py` — listing_generator.py の設計参考（STABLE/DYNAMIC 3層キャッシュパターン、L67-L115）
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/mercari_search.py` — MercariHit dataclass、Playwright scrape パターン参考
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/yahoo_search.py` — YahooHit dataclass、fallback セレクタパターン参考
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/paypay_search.py` — PayPayHit dataclass、SPA対策パターン参考
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/scrapers.py` — httpx フォールバック実装参考（L24-L52）
- `C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/credentials.py` — get_ebay_credentials() 再利用
