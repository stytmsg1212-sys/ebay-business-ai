# 週次経営レポート 試作1号 (W306)

- 対象期間: 直近7日間 = **2026-06-25 〜 2026-07-02** (実行時点 UTC 基準の相対7日)
- 前週比の「前週」: 2026-06-18 〜 2026-06-25
- データ源: `tools/ebay-manager/data/monitor.db` (SELECT のみ、read-only)
- 作成日: 2026-07-02
- ⚠️ 本レポートは **売上テーブルの原価・手数料・利益カラムが全件ゼロ** のため、粗利は算出不能（後述）。売上高・件数・在庫は実データ。

---

## 1. 今週の数字

| 指標 | 今週 (6/25-7/2) | 前週 (6/18-6/25) | 前週比 |
|---|---|---|---|
| 売上件数 | **8 件** | 10 件 | −2 件 (−20%) |
| 売上高 (USD) | **$1,717.14** | $2,002.79 | −$285.65 (−14.3%) |
| 粗利 (推定) | **データ源なし** | データ源なし | — |

### 粗利が出せない理由（正直な報告）
売上テーブル `sales_history` の全 157 件で、原価 `source_cost_jpy`・利益 `profit_jpy`・eBay手数料 `ebay_fee_usd` が **すべて 0**（未記録）でした。つまり DB 上に「いくらで仕入れて、いくら手数料を払ったか」の実績が入っていないため、**売上高しか確定値として出せません**。粗利を推測で埋めることはしていません。

参考として、在庫マスタ側 (`ebay_listings`) には一部商品に仕入値 `purchase_yen` と損益分岐価格 `lp_breakeven_usd` が入っていますが、これは「売れた商品」ではなく「出品中の商品」に紐づくため、今週の売上と突き合わせた粗利計算には使えませんでした。

→ **改善提案**: 販売時に原価・手数料を `sales_history` に記録する仕組みが入れば、来週から粗利・利益率を出せます。

---

## 2. 動きのあった商品

### 今週売れた商品 トップ5（売値順）
| 順位 | 商品 | 売値 | 買い手国 |
|---|---|---|---|
| 1 | HIOKI DT4282 デジタルマルチメーター | $339.32 | 米国 |
| 2 | PLOTTER 5012 A5 6リング レザーバインダー | $291.30 | ノルウェー |
| 3 | Google Pixel Tablet 充電スピーカードック | $282.15 | 豪州 |
| 4 | Pioneer DVL-919 DVD/LD レーザーディスクプレーヤー | $254.00 | 米国 |
| 5 | Sony ICD-TX660 ボイスレコーダー 16GB | $179.30 | アイルランド |

（残り3件: CRYPTON 初音ミク V4X $146.90 / Baccarat CRYSTA タンブラーペア $134.18 / maxell カセットプレーヤー $89.99）
傾向: 計測器・国産オーディオ・日本サブカル系がバランスよく売れ、買い手は米国中心に欧州・豪州へ分散。

### 値下げが多かった商品（`price_change_log` 直近7日、JOIN は `ebay_item_id`）
⚠️ **重要な運用上の発見**: 今週の価格改定ログ 303 件のうち、**実際に価格が下がったのは 6 件のみ**。残り **297 件は「価格計算失敗（rule=None）」でエラー**、値上げは 0 件でした。

- 実際に下がった6件はすべて `competitor - 0.01`（競合の1セント下）の微調整。うち Baccarat CRYSTA タンブラーペア (item 356420645893) が4回連続で $134.32 → $133.58 と小刻みに値下げされ、**今週売れました**（自動値下げが刺さった好例）。
- 失敗297件は同一商品群（Mitutoyo カウンター / Razer THX Onyx DAC / SICK 安全スキャナ / YOKOGAWA DMM / KEYENCE 各種 等、各27回リトライ）が、競合価格は取得できているのに「rule=None（適用ルール未設定）」で新価格を計算できず、6時間バッチのたびに空振りしています。**= 自動値付けエンジンが一部商品で機能していない**。

---

## 3. 死に筋在庫（出品90日以上・未販売）

判定根拠: 出品中 (`is_ended`≠1 かつ `quantity_ebay`>0) の 240 件のうち、**eBay 出品開始日 `start_time` から90日以上経過**し、かつ **90日以内に販売実績がない**（`last_sold_at` が NULL または90日超）ものを「死に筋」と定義。該当 **135 件**（うち有在庫 stock系 59 件）。
床価格 = `lp_breakeven_usd`（DB に記録済みの損益分岐USD価格。仕入値・送料・手数料込みで「これ以下だと赤字」の水準）。

### 有在庫（自社仕入=資金が寝ている）死に筋 トップ10（出品日数順）
| 商品 | 出品日数 | 現価格 | 床価格 | ウォッチ | 状態 |
|---|---|---|---|---|---|
| LightPix Labs FlashQ Q20II ストロボ | 567日 | $146.00 | $109.10 | 6 | 値下げ余地あり |
| Audio-Technica ATH-CKS330NC (BK) | 565日 | $72.22 | $61.84 | 7 | 値下げ余地あり |
| Audio-Technica ATH-CKS330NC (BG) | 565日 | $72.22 | $61.84 | 2 | 値下げ余地あり |
| 坂本龍一 展 公式カタログ | 485日 | $114.00 | $92.30 | 16 | 値下げ余地あり |
| Eagle Racing モーターダイノ MD1-V2 | 455日 | $169.80 | $173.57 | 31 | **既に赤字ライン割れ** |
| SONY ICD-ST25 ボイスレコーダー | 455日 | $79.62 | $60.68 | 11 | 値下げ余地あり |
| Vocaloid4 鏡音リン・レン V4X English | 452日 | $150.34 | $141.56 | 44 | 高ウォッチ・薄利 |
| The Art of God of War Ascension 画集 | 449日 | $268.00 | $206.73 | 20 | 値下げ余地あり |
| Baccarat Aria タンブラー 2025 | 449日 | $89.00 | $74.37 | 2 | 値下げ余地あり |
| YOKOGAWA CL220 ミニクランプテスター | 447日 | $145.00 | $76.55 | 1 | 値下げ余地大 |

### 現価格が床価格を割っている（売れても赤字）商品
| 商品 | 現価格 | 床価格 | ウォッチ | 出品日数 | 種別 |
|---|---|---|---|---|---|
| Fluke 393 FC クランプメーター | $429.00 | $440.78 | 46 | 488日 | 無在庫 |
| Eagle Racing モーターダイノ MD1-V2 | $169.80 | $173.57 | 31 | 455日 | 有在庫 |

→ この2件は今の価格で売れると赤字。値上げ（Fluke は46ウォッチと需要あり）か撤退の判断が必要。

---

## 4. 今週やるべき3つ

### ① 自動値付けエンジンの「rule=None」故障を直す（最優先・機会損失）
`price_change_log` で今週 **297回**、競合価格は取得できているのに「適用ルール未設定」で値付けが空振り。対象は Mitutoyo / Razer THX Onyx DAC ($398) / SICK 安全スキャナ ($450) / YOKOGAWA DMM ($350) / KEYENCE 製品群など、いずれも高単価。これらは**競合が動いても価格が一切追随できていない**状態で、売れる機会を毎日6時間ごとに逃している可能性が高い。→ 値付けルールの割り当て漏れを調査。

### ② 高ウォッチ・売れ残りを狙い撃ちで値下げ（在庫回転）
需要シグナル（ウォッチ数）が高いのに1年前後売れていない商品を、床価格までの余地の範囲で値下げ:
- **Ohuhu × Sanrio 80色マーカーセット**（有在庫・324日・ウォッチ69）現$120 / 床$99.38 → **床上限まで約21%の余地**。ウォッチ最多。$110前後まで下げて反応を見る価値大。
- **Le Creuset ミニ ブルーベリー ココット**（有在庫・381日・ウォッチ68）現$174.21。床価格未登録のため仕入値を確認のうえ値下げ検討。
- **Vocaloid4 鏡音リン・レン**（有在庫・452日・ウォッチ44）現$150.34 / 床$141.56 → 余地薄く、値下げより出品テコ入れ（写真・タイトル）向き。

### ③ 赤字ライン割れ2品の是正
- **Fluke 393 FC クランプメーター**（無在庫・ウォッチ46）現$429 < 床$440.78。需要は十分あるので **値上げ$450前後** で黒字化を狙う（値下げではなく値上げ）。
- **Eagle Racing モーターダイノ**（有在庫・455日・ウォッチ31）現$169.80 < 床$173.57。有在庫で資金が寝ており、455日売れず。**赤字覚悟の在庫処分値下げ**か**撤退**の二択を判断。

---

## 5. 監査情報（使用テーブルと算定式）

### 使用テーブル
- `sales_history` (売上実績: `sold_price_usd`, `sold_at`[UTC], `title`, `ebay_item_id`, `buyer_country`, `profit_jpy`/`source_cost_jpy`/`ebay_fee_usd`=全件0)
- `ebay_listings` (出品マスタ: `current_price`, `purchase_yen`, `lp_breakeven_usd`, `start_time`[UTC], `last_sold_at`, `watch_count`, `quantity_ebay`, `is_ended`, `sku`, `title`, `ebay_item_id`)
- `price_change_log` (価格改定履歴: `old_price_usd`, `new_price_usd`, `success`, `rule_applied`, `error_message`, `triggered_by`, `changed_at`[UTC], `ebay_item_id`)

### タイムゾーン確認（推測せず実測）
- `sales_history.sold_at` = UTC（ISO `...Z` 形式）→ `datetime('now','-7 days')` で直接比較可
- `price_change_log.changed_at` = UTC
- `ebay_listings.start_time` = UTC（ISO `...Z`）
- （`last_synced_at` は JST naive だが本レポートでは未使用）

### 主要 SQL と実行結果（検算用）

**今週の売上件数・売上高:**
```sql
SELECT COUNT(*), ROUND(SUM(sold_price_usd),2)
FROM sales_history
WHERE sold_at >= datetime('now','-7 days');
-- 結果: 8 件, $1717.14
```
**前週:**
```sql
SELECT COUNT(*), ROUND(SUM(sold_price_usd),2)
FROM sales_history
WHERE sold_at >= datetime('now','-14 days') AND sold_at < datetime('now','-7 days');
-- 結果: 10 件, $2002.79
```
**粗利カラムのゼロ確認（全157件）:**
```sql
SELECT COUNT(*),
  SUM(CASE WHEN profit_jpy!=0 THEN 1 ELSE 0 END),
  SUM(CASE WHEN source_cost_jpy!=0 THEN 1 ELSE 0 END)
FROM sales_history;
-- 結果: 157, 0, 0  (原価・利益とも全件未記録)
```
**値下げ集計（JOIN は ebay_item_id、SKU不使用）:**
```sql
SELECT p.ebay_item_id, COUNT(*), SUM(p.success),
  (SELECT title FROM ebay_listings e WHERE e.ebay_item_id = p.ebay_item_id)
FROM price_change_log p
WHERE p.changed_at >= datetime('now','-7 days')
GROUP BY p.ebay_item_id ORDER BY COUNT(*) DESC;
-- 成功値下げ=6, 失敗(price calc failed)=297, 値上げ=0
```
**死に筋在庫（出品90日以上・未販売、床価格比較）:**
```sql
SELECT ebay_item_id, title, current_price, lp_breakeven_usd, watch_count,
  CAST(julianday('now')-julianday(start_time) AS INT) AS days_listed
FROM ebay_listings
WHERE COALESCE(is_ended,0)=0 AND quantity_ebay>0
  AND CAST(julianday('now')-julianday(start_time) AS INT) >= 90
  AND (last_sold_at IS NULL OR julianday('now')-julianday(last_sold_at) >= 90)
ORDER BY days_listed DESC;
-- 該当 135 件 (うち有在庫 stock系 59 件、床価格割れ 2 件)
```

### 「データ源なし」とした項目
1. **今週の粗利 / 利益率** — `sales_history` の原価・利益・手数料カラムが全件0のため算出不能。
2. **床価格が出せない一部商品**（Le Creuset, Motherhouse Moomin, PLOTTER の一部など）— `lp_breakeven_usd` / `purchase_yen` 未登録。値下げ判断には仕入値の追加登録が必要。
