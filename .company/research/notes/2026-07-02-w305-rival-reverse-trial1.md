# W305 ライバルセラー丸ごと分析→国内仕入先逆引き 手動試験 (2026-07-02)

## 目的
手法「ライバルセラーの出品を丸ごと分析 → 国内仕入先を逆引き」の精度と工数を手動試験で実測し、仕組み化 (自動化) の GO/NO GO 判断材料を得る。**read-only厳守** (DB書込・実購入・実出品は一切なし)。

---

## 1. 選定セラー

### `mono_honpo_japan`

- **選定理由**: `listing_rival_discoveries` テーブルを `competitor_seller` でグルーピングし発見件数を実測した結果、**77件で最多** (2位 `labbid.japan` 28件、3位 `7daysmart` 27件、4位 `store_sakaguchi` 25件)。
- **日本人セラー判定根拠**: `new_competitor_alerts.is_japan_seller` フラグで `mono_honpo_japan` は `1` (Japan)。出品タイトルにも "from Japan" "Made in Japan" 等の記載が多数、産業計測器 (KEYENCE / HIOKI / ADVANTEST 等)・国内専業ブランド品を中心とした構成で、日本国内在庫からの発送を行う日本人セラーと判断。
- クエリ根拠:
  ```sql
  SELECT competitor_seller, COUNT(*) FROM listing_rival_discoveries
  GROUP BY competitor_seller ORDER BY COUNT(*) DESC;
  -- mono_honpo_japan: 77
  ```

---

## 2. 5品の選定と結果

対象は `mono_honpo_japan` の発見履歴からタイトル・eBay価格(USD)が揃っているもの5品 (DB実データ)。

| # | タイトル (eBay) | eBay価格(USD) | 仕入先候補 | 仕入価格(JPY) | 状態 | 粗利概算(円) | 利益率 |
|---|---|---|---|---|---|---|---|
| 1 | HILTI SMD 57 Screw Magazine Attachment for SD 5000-22 SMD57 NEW from Japan | $199.80 | [クニモトハモノ (工具通販)](https://kunihamonet.com/products/detail58037.html) — 型番2289475 完全一致 | ¥15,730 (税込・新品) | 新品 | ¥7,008 | 22.3% |
| 2 | Fluke 393 FC True RMS Clamp Meter 1500V CAT III, Compact IP54 Rated NEW from JP | $429.00 | [工具の楽市 (Yahoo!ショッピング)](https://store.shopping.yahoo.co.jp/kougurakuichi/5944399.html) — 型番393FC 完全一致 | ¥48,682 (税込送料無料・新品) | 新品 | ¥4,643 | 6.9% |
| 3 | DENSO DST-i Scan Tool Diagnostic Tester Main Unit DN-VIM-003 Used Tested Japan | $717.98 | ヤフオク落札相場 ([aucfan.com検索](https://aucfan.com/search1/q-DST.2dI/s-ya/)) — 型番DN-VIM-003 完全一致の直近6件平均。※現在出品中の個別URLは特定できず、相場データとして採用 | ¥62,556 (中古・直近落札平均) | 中古 | ¥29,333 | 26.0% |
| 4 | HIOKI 9783 Electric Carrying Case Hard Case for 8847 and MR8847 NEW from JAPAN | $1,300.00 | **候補なし** — 検索結果は型番9399等の別ケースと混同されており、9783完全一致で購入可能な国内候補を発見できず | — | — | 試算不可 | 試算不可 |
| 5 | KEYENCE KV-XLE02 Ethernet Unit 2 Port NEW from Japan | $360.00 | [Amazon.co.jp](https://www.amazon.co.jp/-/en/Keyence-Ethernet-KV-XLE02-Programmable-Controller/dp/B07SWJRFSZ) — 型番KV-XLE02 完全一致・新品 (¥38,333〜¥123,500まで出品者により価格差あり、最安値を採用) | ¥38,333 (新品) | 新品 | ¥5,784 | 10.2% |

### 粗利計算式 (全品共通、明記)
```
粗利(円) ≈ (eBay価格USD - eBay価格USD×0.15[手数料] - $25[送料概算]) × 157[円/$] - 仕入値JPY
```
利益率 = 粗利 ÷ (eBay価格USD × 157)。

### 利益床チェック (参考情報)
- #1 HILTI ($199.80, 300$以下帯): 利益床目安 2,000円/15% → 粗利¥7,008 (22.3%) で **クリア**
- #2 Fluke ($429.00, 300$超): 利益床目安なし (300$以下帯の目安は適用外) だが利益率6.9%は薄利
- #3 DENSO ($717.98): 高額帯、粗利¥29,333 (26.0%) で **良好**
- #5 KEYENCE ($360.00, 300$超): 粗利¥5,784 (10.2%) はやや薄い

---

## 3. 手法の精度所見

### 仕入先候補ヒット率
**5品中 4品で仕入先候補が見つかった (80%)**。1品 (HIOKI 9783 キャリングケース) は候補なし。

### 誤マッチ (型番不一致・別商品) リスクの所見
- HIOKI 9783 の探索過程で、検索結果が **型番9399の携帯用ケース** (別モデル) を混同して提示するケースが複数回発生した。ニッチな計測器付属品ほど「型番一致」の検証が必須で、これを怠ると誤った仕入価格で粗利を過大/過小評価するリスクが高い (本試験ではルール通り除外)。
- DENSO DST-i は「DN-VIM-003」という具体的サブ型番まで一致させないと、同シリーズ内で価格帯が全く異なる別モデル (DST-2 / DSTT 等、平均1万円台) を誤って仕入先候補にしてしまう risk があった。検索クエリに型番全体を含めることで回避。
- KEYENCE KV-XLE02 は同一型番でも出品者間で ¥38,333〜¥123,500 と3倍以上の価格差があり、**最安値を機械的に採用すると転売リスク品や実は別コンディション品を掴む可能性**がある点は留意が必要 (今回は最安値のみ記録、個別ページの状態未確認)。
- 全体として、産業機器・計測器カテゴリは「型番完全一致」検索が有効に機能する一方、消耗品・アクセサリ類 (ケース等) は型番の表記揺れが多く自動化時の誤マッチ率が上がりやすいと見立てる。

### 1品あたりの所要工数感
- WebSearch: 1品あたり平均 **2〜3回** (初回検索でヒットしない場合は型番を絞り込んだ再検索が必要)
- WebFetch (詳細ページ確認): 1品あたり平均 **1回程度** (価格・型番確認のため)
- 5品合計で WebSearch 12回・WebFetch 5回 (うち1回タイムアウト、1回404で再試行)を要した。
- 体感として、**型番が明確な工業製品・計測器は5分程度/品**で候補特定まで到達できるが、アクセサリ/ケース類や中古相場データ主体の品目 (DENSO等) は**10分以上**かかり、精度検証 (型番完全一致確認) に追加の手間がかかる。

---

## 4. 仕組み化する場合の推奨アーキテクチャ (簡潔)

1. **発見履歴 → セラー別集計バッチ**: `listing_rival_discoveries` を日次で `competitor_seller` 集計し、上位N名を自動リストアップ (`is_japan_seller` フラグでJP絞り込み)。
2. **仕入先候補検索エンジン**: 型番抽出 (タイトルから正規表現/LLM抽出) → WebSearch/WebFetch で候補URL・価格・状態を取得し、型番完全一致チェックをハードフィルタとして適用 (不一致は自動除外、無理な推定はしない)。
3. **粗利フィルタ + 人間レビューキュー**: 本試験の計算式で粗利/利益率を自動算出し、利益床未達・候補なし・型番不一致リスクありの品は自動除外、残りを `morning_discovery_candidates` 相当のレビューキューに投入して人間が最終判断する。

---

## 補足: 使用ツール
- DB確認: Bash経由 Python `sqlite3` で `sqlite_master` LIKE検索 → `PRAGMA table_info` → `SELECT ... GROUP BY competitor_seller` (全てSELECTのみ、read-only厳守)
- WebSearch: 12回 (各品2〜3回)
- WebFetch: 5回 (詳細ページの型番・価格確認)
