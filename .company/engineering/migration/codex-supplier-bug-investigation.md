# eBay 物販 AI ツール 仕入先候補 sold_out 混入バグ調査

調査日時: 2026-05-28 JST

対象 DB: `C:\Users\gucch\projects\claude\tools\ebay-manager\data\monitor.db`

## 結論

root cause は複数です。

1. (c) 判定 layer 不在: 仕入先候補の発見登録フローに、候補 URL 個別ページの在庫確認が入っていない。
2. (a) scraper / search 誤判定: `monitor/paypay_search.py` は `/search/{keyword}?status=selling&sort=new` の検索結果リンクを在庫ありとして返すが、実 fetch で sold out の PayPay 個別ページが現在も検索結果に返る。
3. (a) PayPay sold_out 設定不足: `site_configs` と `monitor/database.py` の PayPay `sold_out_text` は `関連商品をアプリで探す` だけで、実 HTML に存在する `SoldOut` と `購入日時` を検出しない。
4. (b) 既存判定結果の未利用: `monitor/scrapers.py` の在庫判定関数は存在するが、`task_supplier_candidate_search.py` と `task_supplier_sweep.py` の候補登録前に呼ばれていない。
5. (d) 事後 cleanup の対象不足: `scripts/check_supplier_candidates_oos_2026_05_20.py` は `status='pending'` のみを対象にし、`accepted` 候補を除外する。

id=1362 は「発見時点で既に売切れだった URL を、検索結果ヒットとして取得し、個別ページ在庫確認なしで `supplier_candidates` に登録した」バグです。

## DB 実データ

`supplier_candidates` に発見時 availability を保存する列はありません。

| id | platform | candidate_url | created_at | status | discovered_via | parent quantity_ebay | match_score | profitable |
|---:|---|---|---|---|---|---:|---:|---:|
| 467 | yahoo_auctions | https://auctions.yahoo.co.jp/jp/auction/j1227612451 | 2026-04-24 06:10:34 | accepted | pattern_2_batch | 0 | 85 | 1 |
| 619 | yahoo_auctions | https://auctions.yahoo.co.jp/jp/auction/j1221361668 | 2026-05-01 06:27:56 | accepted | pattern_1_continuing_oos | 0 | 75 | 1 |
| 700 | yahoo_auctions | https://auctions.yahoo.co.jp/jp/auction/j1221361668 | 2026-05-02 05:54:56 | accepted | pattern_1_continuing_oos | 0 | 78 | 1 |
| 1354 | paypay_furima | https://paypayfleamarket.yahoo.co.jp/item/z572785762 | 2026-05-27 18:42:00 | pending | pattern_2_batch_w94 | 0 | 72 | 1 |
| 1360 | paypay_furima | https://paypayfleamarket.yahoo.co.jp/item/l1211525040 | 2026-05-27 18:42:00 | pending | pattern_2_batch_w94 | 0 | 82 | 1 |
| 1362 | paypay_furima | https://paypayfleamarket.yahoo.co.jp/item/z478735304 | 2026-05-27 18:42:00 | pending | pattern_2_batch_w94 | 0 | 72 | 1 |

## 発見ロジック

`tasks/task_supplier_sweep.py:68-86` は親 `ebay_listings` から長期在庫切れ listing を抽出する。候補 URL の availability は見ていない。

`tasks/task_supplier_sweep.py:249-289` は `search_candidates_on_platform()` の検索ヒットを `BatchItem` にする。個別候補 URL の在庫確認はない。

`tasks/task_supplier_sweep.py:325-379` は Batch 評価結果を match score、alt 判定、利益判定で filter し、`add_supplier_candidate()` に渡す。availability check はない。

`tasks/task_supplier_candidate_search.py:288-302` は各 platform の検索ヒットを Claude 評価へ送る。個別 URL の在庫確認はない。

`tasks/task_supplier_candidate_search.py:312-370` は match score、alt 判定、利益判定だけで `add_supplier_candidate()` を呼ぶ。

`monitor/database.py:3935-3972` の `add_supplier_candidate()` は `INSERT OR IGNORE INTO supplier_candidates` を行うだけで、候補 URL の在庫状態を検査しない。

## PayPay 判定設定

DB の `site_configs` 実値:

```text
Paypayフリマ:
  in_stock_text1 = 購入手続きへ
  sold_out_text  = 関連商品をアプリで探す
  no_page_text   = この商品は存在しません
```

`monitor/database.py:45-53` の DEFAULT_SITE_CONFIGS も同じ設定です。

`monitor/scrapers.py:55-83` は `sold_out_texts` が content にあれば `unavailable` を返す。関数自体は sold_out 判定能力を持つが、現行 PayPay 設定は実 HTML の `SoldOut` と `購入日時` を検出しない。

## 実 fetch 結果

実行方法:

```text
httpx: User-Agent Chrome 120, Accept-Language ja-JP, follow_redirects=True
curl.exe: -L -s --compressed -A Chrome 120 -H Accept-Language: ja-JP
```

| id | URL | HTTP | 実 fetch signal | 現行 `monitor.scrapers` httpx 判定 |
|---:|---|---:|---|---|
| 1360 | PayPay l1211525040 | 200 | `SoldOut`; `InStock` も ld+json に混在 | unknown |
| 1354 | PayPay z572785762 | 200 | `購入日時：2026年3月14日 07:35`; `SoldOut`; `InStock` も ld+json に混在 | unknown |
| 1362 | PayPay z478735304 | 200 | `購入日時：2025年12月13日 00:53`; `出品日時：2025年8月27日 14:06`; `SoldOut`; `InStock` も ld+json に混在 | unknown |
| 700 | Yahoo j1221361668 | 200 | `入札する`; `https://schema.org/InStock` | available |
| 619 | Yahoo j1221361668 | 200 | `入札する`; `https://schema.org/InStock` | available |
| 467 | Yahoo j1227612451 | 404 | `このオークションは終了しています`; `終了日時：2026年4月27日（月）10時18分` | not_found |

curl.exe の signal 集計:

```text
https://paypayfleamarket.yahoo.co.jp/item/l1211525040    signals=出品日時,SoldOut,InStock
https://paypayfleamarket.yahoo.co.jp/item/z572785762     signals=購入日時,出品日時,SoldOut,InStock
https://paypayfleamarket.yahoo.co.jp/item/z478735304     signals=購入日時,出品日時,SoldOut,InStock
https://auctions.yahoo.co.jp/jp/auction/j1221361668      signals=InStock,入札する
https://auctions.yahoo.co.jp/jp/auction/j1227612451      signals=このオークションは終了
```

PayPay の `InStock` は ld+json の古い Product schema に残っている一方、同じ HTML 内の別 script に `offers.availability:"SoldOut"` が入っている。`http://schema.org/InStock` 単独では available と判定できない。

追加再現:

```text
python -c "from monitor.paypay_search import search_paypay; print(search_paypay('BIG 大昭和精機 EWN20-36CKB1', max_results=10))"
```

この検索は 2026-05-28 時点で、売切れ確定の `z478735304` と `l1211525040` を検索結果として返した。`monitor/paypay_search.py:91-94` の `status=selling` は sold out を除外できていない。

## 判定結果の扱い

発見時には `monitor/scrapers.py` の判定結果が作られていません。登録前 availability 判定 layer がありません。

`scripts/check_supplier_candidates_oos_2026_05_20.py:183-188` は `WHERE status = 'pending'` のみを取得し、accepted は対象外です。accepted 候補は事後 cleanup でも残ります。

## 即時対応 SQL

UI の「復活候補」から 6 件を消す目的で、6 件すべてを手動 reject する SQL:

```sql
UPDATE supplier_candidates
SET status = 'rejected',
    auto_rejected = 1,
    user_action_at = CURRENT_TIMESTAMP
WHERE id IN (1360, 1354, 1362, 700, 619, 467)
  AND status IN ('pending', 'accepted');
```

実 fetch の sold_out / not_found 根拠だけで reject する最小 SQL:

```sql
UPDATE supplier_candidates
SET status = 'rejected',
    auto_rejected = 1,
    user_action_at = CURRENT_TIMESTAMP
WHERE id IN (1360, 1354, 1362, 467)
  AND status IN ('pending', 'accepted');
```

id=700 と id=619 の `j1221361668` は 2026-05-28 の実 fetch で `available` です。6 件一括 reject は「親 eBay listing が quantity_ebay=0 の復活候補表示から除外する」運用判断として実行する SQL です。

## 恒久対応

1. `tasks/task_supplier_candidate_search.py:288-302` で `evaluate_candidate_with_claude()` 前に個別 URL 在庫 gate を追加する。
2. `tasks/task_supplier_sweep.py:256-289` で `BatchItem` 作成前に同じ gate を追加する。
3. `tasks/task_supplier_sweep.py:325-379` の batch persist 前にも二重防御を入れる。
4. `monitor/scrapers.py` に PayPay 専用判定を追加する。優先順位は `availability:"SoldOut"`、`購入日時：`、`購入手続きへ`。`http://schema.org/InStock` 単独は使わない。
5. `monitor/paypay_search.py` は検索結果取得後、各 `/item/` URL を個別 fetch して sold out を除外する。
6. `supplier_candidates` に `availability_status`, `availability_checked_at`, `availability_signal` を追加し、`unknown` と `unavailable` は UI 既定表示から除外する。
7. `scripts/check_supplier_candidates_oos_2026_05_20.py:183-188` を configurable にし、必要時は `status IN ('pending', 'accepted')` を対象にする。

暫定 SQL:

```sql
UPDATE site_configs
SET sold_out_text = 'SoldOut'
WHERE convert_url = 'ebayPF_';
```

ただし `購入日時` と `SoldOut` の複数 signal を扱うには単一 `sold_out_text` 列では不足します。恒久対応はコード側で PayPay 専用判定を追加する。

## 最終判定

id=1362 の root cause は、(c) 判定 layer 不在 + (a) PayPay 検索結果が sold out を返す + (a) PayPay sold_out 設定不足です。

id=1354 と id=1360 も同じ PayPay 経路で sold out が登録されています。

id=467 は現在 not_found / オークション終了です。

id=619 と id=700 は同一 URL `j1221361668` で、現在 fetch では available です。

DONE
