# EC 直接 URL 無在庫設計 + 楽天/Amazon 在庫判定調査

作成日: 2026-05-28  
対象: `C:\Users\gucch\projects\claude\tools\ebay-manager`  
調査者: Codex/GPT-5.5 実機調査

## 結論

推奨は **SKU 種別を増やさず、`source_url` を直接編集可能にし、`source_url_manual=1` で同期上書きを防ぐ** 設計です。SKU は従来通り `stock*` / `ebay*` の在庫種別判定と、規則性のある無在庫 URL 生成だけに使います。Amazon/楽天など URL 規則が弱い EC 商品は、SKU は任意の `ebayAM_...` / `ebayRT_...` でもよいが、監視 URL の真実源は手動 `source_url` とします。

楽天の在庫判定は raw HTML の schema.org microdata が最も安定しています。

- 在庫あり: `data\tmp\ec_direct_url_probe\rakuten_in_raw.html:1166`  
  `<meta itemprop="availability" content="http://schema.org/InStock">`
- 売り切れ: `data\tmp\ec_direct_url_probe\rakuten_oos_raw.html:528`  
  `<meta itemprop="availability" content="http://schema.org/OutOfStock">`

現 DB の楽天 `site_configs` は `in_stock_text1='かごに追加'`, `sold_out_text='売り切れ'` です。raw HTML では不十分で、Playwright 後も売り切れページに disabled の「かごに追加」が残るため、次へ置換するのが安全です。

- `in_stock_text1 = itemprop="availability" content="http://schema.org/InStock"`
- `sold_out_text = itemprop="availability" content="http://schema.org/OutOfStock"`
- `in_stock_text2` は空のまま

Amazon `https://www.amazon.co.jp/dp/B0D2HDZFL1` は今回の実機では httpx/curl/Playwright とも 200 で、raw HTML に `submit.add-to-cart` と `カートに入れる` が入ります。ただし Amazon はページ構造と bot 判定が変動しやすく、既存コードにも「同 IP 連続 GET は 503/CAPTCHA リスク」と明記されています。判定は `カートに入れる` だけではヘッダーや関連商品欄も拾うため、`id="add-to-cart-button" name="submit.add-to-cart"` を優先し、`Robot Check` / `captcha` / 503 は `unknown` として再試行対象にするべきです。

## 実機取得結果

取得ファイル:

- `data\tmp\ec_direct_url_probe\rakuten_in_raw.html`
- `data\tmp\ec_direct_url_probe\rakuten_oos_raw.html`
- `data\tmp\ec_direct_url_probe\rakuten_in_pw.html`
- `data\tmp\ec_direct_url_probe\rakuten_oos_pw.html`
- `data\tmp\ec_direct_url_probe\amazon_B0D2HDZFL1_raw.html`
- `data\tmp\ec_direct_url_probe\amazon_pw.html`

楽天 raw HTML:

- `rakuten_in_raw.html`: HTTP 200, 163,369 chars, `InStock` 1 件
- `rakuten_oos_raw.html`: HTTP 200, 116,436 chars, `OutOfStock` 1 件
- `かごに追加`, `買い物かご`, `売り切れ`, `在庫切れ`, `SOLD OUT`, `ご注文できない` は raw 判定の主シグナルにしない

楽天 Playwright 描画後:

- 在庫あり: `data\tmp\ec_direct_url_probe\rakuten_in_pw.html:1100` に `availability ... InStock`
- 売り切れ: `data\tmp\ec_direct_url_probe\rakuten_oos_pw.html:405` に `availability ... OutOfStock`
- 売り切れ表示: `data\tmp\ec_direct_url_probe\rakuten_oos_pw.html:663` に `この商品は売り切れです`
- 売り切れでも disabled の「かごに追加」が残る: `data\tmp\ec_direct_url_probe\rakuten_oos_pw.html:667`

Amazon raw HTML:

- `amazon_B0D2HDZFL1_raw.html`: HTTP 200, 約 1.9M chars
- 5 回連続 httpx 実測: 全回 `status=200`, `captcha=False`, `cart=True`, `addbtn=True`
- Add to cart 主ボタン: `data\tmp\ec_direct_url_probe\amazon_B0D2HDZFL1_raw.html:3253`  
  `id="add-to-cart-button" name="submit.add-to-cart" title="カートに入れる"`
- `availability_feature_div` は空に近い: `data\tmp\ec_direct_url_probe\amazon_B0D2HDZFL1_raw.html:5019`
- `OutOfStock` は動的 widget 設定文字列にも出るため単独判定不可: `data\tmp\ec_direct_url_probe\amazon_B0D2HDZFL1_raw.html:5136`

## 現行コードの根拠

SKU と URL:

- `sku_mapping_manager.py:25-27` は楽天 URL から `ebayRT_`、Amazon URL から `ebayAM_` を逆算するが、楽天は shop slug しか取れず商品 URL 全体を再生成できない。
- `sku_mapping_manager.py:85-108` は `ebayRT_` と `ebayAM_` の `common_url` / pattern を持つ。
- `monitor\database.py:2530-2548` は `find_site_config_by_sku()` / `build_source_url()` が SKU prefix から URL を作る。
- `monitor\database.py:2553-2588` の `upsert_item()` は毎回 `source_url = _build_source_url_from_sku(sku) or build_source_url(sku)` を作り、既存 `monitored_items.source_url` を上書きする。
- `monitor\database.py:2674-2713` の `upsert_ebay_listing()` は SKU 変更時に `new_source_url = _build_source_url_from_sku(sku)` を作り、`ebay_listings.source_url` を更新する。
- `monitor\database.py:4107-4166` の `_sync_monitored_items_sku()` も eBay listing 側 URL または SKU 生成 URL を `monitored_items` へ同期する。

監視:

- `tasks\task_inventory_check.py:453-469` は `monitored_items` を真実源にして `prepare_batch_items()` に渡す。
- `monitor\scrapers.py:326-348` の `prepare_batch_items()` は `sku.startswith(prefix)` で `site_configs` を決めるため、直接 URL だけでは監視 batch から落ちる。
- `monitor\scrapers.py:22-44` はまず httpx で raw HTML を見る。
- `monitor\scrapers.py:55-82` は no_page / sold_out / in_stock の文字列包含で判定する。
- `monitor\scrapers.py:228-246` は httpx 不明時に Playwright headless、さらに headed Chrome へ fallback する。

DB:

- 現 `PRAGMA user_version = 54`。
- 次 migration は **v55** とする。
- `monitor\database.py:367-378` に `site_configs`。
- `monitor\database.py:380-391` に `monitored_items.source_url` / `site_config_id`。
- `monitor\database.py:414-420` 以降に `ebay_listings`、`ebay_item_id` は UNIQUE。
- `monitor\database.py:1928-1938` に SKU は listing 識別キーではなく、listing 識別は `ebay_item_id` という既存コメントがある。

現 DB の対象 `site_configs`:

- 楽天市場: id=6, `url_keyword='item.rakuten'`, `convert_url='ebayRT_'`, `in_stock_text1='かごに追加'`, `sold_out_text='売り切れ'`, `common_url='https://x.gd/'`
- Amazon: id=9, `url_keyword='www.amazon.co.jp'`, `convert_url='ebayAM_'`, `in_stock_text1='カートに入れる'`, `in_stock_text2='今すぐ買う'`, `sold_out_text='現在在庫切れ'`

## A. SKU 種別

### Codex 推奨案

第 3 SKU 種別は作らない。Amazon/楽天の直接 URL 無在庫も `stock*` でなければ無在庫として扱い、監視 URL は `source_url_manual=1` の `source_url` を使う。

理由:

- `.claude/rules/sku-rules.md` の要点「SKU 用途は在庫種別判定と SKU→URL 変換の 2 つだけ」に合う。
- `url_...` のような第 3 prefix を作ると、`sku.startswith("ebay")` 前提の既存処理と supplier search 条件に波及する。
- listing 識別は `ebay_item_id` で、直接 URL は listing の属性にすべき。

運用:

- 有在庫: `stock*`
- 既存規則型無在庫: `ebayme_...`, `ebayPF_...`, `ebayyh_...` など。URL は SKU から生成。
- 直接 URL 無在庫: SKU は任意。ただし既存画面/集計との互換性を考え、Amazon は `ebayAM_任意`, 楽天は `ebayRT_任意` を推奨。URL は手動。

### Claude レビュー観点案

`source_url_manual=1` に加えて `source_kind` を持つ案もある。

- `source_kind='sku_derived' | 'manual_url' | 'stock'`
- 可読性は高いが、既存の `stock*` / `ebay*` 判定と二重管理になる。
- 今回の最小実装では不要。将来 SKU prefix 依存を完全に剥がす時に採用する。

## B. source_url 上書き保護

### migration v55

`monitor/database.py` の `init_db()` に user_version gate で追加する。DB migration rule に合わせ、`ALTER TABLE ADD COLUMN` は列ごとに `try/except sqlite3.OperationalError`、最後に `PRAGMA user_version = 55`。

追加列:

```sql
ALTER TABLE ebay_listings ADD COLUMN source_url_manual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE monitored_items ADD COLUMN source_url_manual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ebay_listings ADD COLUMN source_url_updated_at TIMESTAMP;
ALTER TABLE monitored_items ADD COLUMN source_url_updated_at TIMESTAMP;
```

推奨 helper:

- `find_site_config_by_url(url: str) -> Optional[dict]`
  - `site_configs.url_keyword in url` で判定。
  - SKU prefix に依存しない。
- `set_listing_source_url_manual(ebay_item_id: str, source_url: str, manual: bool = True) -> bool`
  - `ebay_listings` を `ebay_item_id` で更新。
  - `monitored_items` を同じ `ebay_item_id` で upsert/update。
  - `site_config_id` は `find_site_config_by_url()` の id。
  - 空 URL + manual off は「SKU 派生へ戻す」扱い。

既存関数修正:

- `monitor\database.py:2553-2588` `upsert_item()`
  - 既存行が `source_url_manual=1` なら `source_url` を再計算で上書きしない。
  - 新規行は `manual_source_url` 引数があればそれを採用し `source_url_manual=1`。
- `monitor\database.py:2674-2713` `upsert_ebay_listing()`
  - SKU 変更時も `source_url_manual=1` の listing では `source_url=COALESCE(?, source_url)` を実行しない。
  - `source_status='unknown'` と `source_last_checked=NULL` の reset は URL が変わった時だけに限定する。
- `monitor\database.py:4107-4166` `_sync_monitored_items_sku()`
  - `monitored_items.source_url_manual=1` の行は `source_url` を維持し、`sku` と `site_config_id` だけ必要に応じて更新。
  - `site_config_id` は manual URL なら URL から、SKU 派生なら SKU から。

## C. 商品管理 UI

対象: `tabs\tab_product_management.py`

読み込み:

- `tabs\tab_product_management.py:143-150` の SELECT に `el.source_url_manual` を追加。
- `tabs\tab_product_management.py:222-230` の `_fetch_monitored_items_for_listing()` に `source_url_manual` を追加。

配置:

- 各商品 expander の「仕入先/監視」付近、現 `source_url` 表示の近くに配置する。
- 表示は 1 行:
  - `st.text_input("仕入先 URL", value=p["source_url"] or "", key=f"pm_source_url_{eid}")`
  - `st.checkbox("直接 URL として固定", value=bool(p["source_url_manual"]), key=f"pm_source_url_manual_{eid}")`
  - `st.caption("固定 ON の URL は eBay 同期や SKU 変更で上書きされません。")`
- 保存ボタンは既存の商品保存導線に統合し、変更検知時だけ `set_listing_source_url_manual()` を呼ぶ。

入力 validation:

- 空 URL + 固定 ON は不可。
- `http://` / `https://` 以外は不可。
- `find_site_config_by_url()` で site 未判定なら warning を出し、保存は許可するが監視結果は `unknown` になり得ると表示。
- SKU が `stock*` かつ直接 URL ON は warning。保存自体は user 判断で許可するか、初期実装ではブロックが安全。

## D. 在庫監視への組込

`monitor\scrapers.py:326-348` の `prepare_batch_items()` を次の優先順位にする。

1. `source_url` が空なら除外。ただし除外件数と理由をログに残す。
2. `source_url_manual=1` または SKU prefix 不一致なら、`find_site_config_by_url(source_url)` で config を決める。
3. それでも config がなければ `unknown` 対象として batch に入れるか、少なくとも `task_inventory_check.py:470-479` の silent skip 記録に「site_config_missing_url」として出す。
4. SKU prefix config がある場合は従来通り。

`tasks\task_inventory_check.py:453-491` は batch 空を failure にしているため、直接 URL 商品が全件 prefix 不一致で落ちると task failure になる。新設計では `prepare_batch_items()` が URL 判定へ fallback するので回避できる。

`source_out_of_stock_since` 更新は既存の `source_status` 更新経路に合わせる。直接 URL でも URL 単位監視で問題ないが、`source_url` 共有 listing がある場合は `ebay_item_id` を主にして更新する。

## E. 楽天 signal 修正

DB 更新案:

```sql
UPDATE site_configs
SET in_stock_text1 = 'itemprop="availability" content="http://schema.org/InStock"',
    in_stock_text2 = '',
    sold_out_text = 'itemprop="availability" content="http://schema.org/OutOfStock"'
WHERE convert_url = 'ebayRT_'
  AND url_keyword = 'item.rakuten';
```

ただし本番 DB 直接 UPDATE は避け、migration v55 の中で冪等に実行する。

判定理由:

- raw HTML だけで在庫あり/売り切れを分離できる。
- Playwright 描画後も同じ microdata が残る。
- 「かごに追加」は売り切れページにも disabled button として残る。
- 「買い物かご」はヘッダー文言で両ページに出る。
- 「売り切れ」は raw HTML には出ず、JS 描画後にだけ出るケースがあるため fallback には使えるが主シグナルにはしない。

実装候補:

- 最小: `site_configs` の文字列を上記 meta 文字列へ変更する。
- 堅牢: `monitor\scrapers.py` に `detect_schema_org_availability(html)` を追加し、`InStock` / `OutOfStock` を文字列 config より先に判定する。楽天以外の EC でも利用できる。

## Amazon 判定方針

現 `site_configs` の `in_stock_text1='カートに入れる'` は広すぎる。今回のページでは nav shortcut、関連商品カード、主ボタンすべてに出る。

推奨:

- `in_stock_text1 = id="add-to-cart-button"`
- `in_stock_text2 = name="submit.add-to-cart"`
- `sold_out_text = 現在在庫切れ`
- scraper 側で Amazon 専用判定を追加:
  - `Robot Check`, `captcha`, `503` は `unknown`。
  - `id="add-to-cart-button"` かつ `name="submit.add-to-cart"` があれば `available`。
  - `現在在庫切れ` / `Currently unavailable` があれば `unavailable`。
  - `OutOfStock` 単独は Amazon の動的 widget 設定にも混ざるため使わない。

anti-bot:

- 今回の 5 回連続 httpx はすべて 200 で CAPTCHA なし。
- 既存 `tasks\task_inventory_check.py:665-667` にも Amazon 連続 GET の 503/CAPTCHA リスクが記載済み。
- 監視では Amazon を低頻度、jitter、unknown retry とし、CAPTCHA を在庫切れ扱いしない。

## 実装順

1. v55 migration を `monitor/database.py` に追加。
2. `find_site_config_by_url()` と `set_listing_source_url_manual()` を追加。
3. `upsert_item()`, `upsert_ebay_listing()`, `_sync_monitored_items_sku()` に manual URL 保護を入れる。
4. `prepare_batch_items()` を URL fallback 対応にする。
5. 楽天 `site_configs` を schema.org availability へ更新。
6. Amazon 専用判定を `monitor\scrapers.py` に入れる。
7. `tabs\tab_product_management.py` に直接 URL UI を追加。
8. tests:
   - `init_db()` 2 回実行で v55 列と既存データ保持。
   - manual URL が `upsert_item()` / `upsert_ebay_listing()` / SKU 変更で消えない。
   - 楽天 raw fixture の `InStock` / `OutOfStock` 判定。
   - Amazon fixture の add-to-cart 判定と CAPTCHA unknown 判定。
   - prefix 不一致 manual URL が `prepare_batch_items()` から落ちない。

## tradeoff

Codex 推奨案: `source_url_manual` flag

- 利点: 既存 SKU ルールと衝突しない。変更範囲が明確。同期上書きバグを直接止める。
- 欠点: SKU prefix 由来 config と URL 由来 config の二系統が残る。

Claude レビュー観点案: `source_kind` を追加

- 利点: 状態が読みやすく、将来 SKU prefix 依存を減らしやすい。
- 欠点: 既存 `stock*` / `ebay*` 判定と二重管理になり、移行時の不整合が増える。

採用判断: v55 では `source_url_manual` のみ採用。`source_kind` は次段階で SKU prefix 依存を整理する時に検討。

DONE
