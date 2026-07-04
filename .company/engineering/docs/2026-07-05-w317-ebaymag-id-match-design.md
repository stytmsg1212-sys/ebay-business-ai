# W317: eBaymag discover をタイトル一致 → GraphQL products の item id 照合へ (Phase 0)

出典: 2026-07-05 依頼ボード派生。eBaymag 反映キュー (`ebaymag_apply_queue`) の `discover_product_id`
がタイトル/検索語ベースの一致に依存しており、多言語タイトル (英語 eBay タイトル vs eBaymag 側の
検索インデックス) のズレで `awaiting_import` 滞留が発生している。実証根拠:
`scripts/_sample_ebay_reflection.py` L18-29 (GraphQL `products.listings[].publicationUrl` から
正規表現で eBay item id を抽出できることを既に実証済み)。

## 現状の課題

`monitor/ebaymag_driver.discover_product_id` (L709-) は eBaymag のアーカイブ検索 UI に
`query` (listing title or search_keyword) を投げて候補行を集め、各候補の panel を開いて
`itm` (eBay item id) を照合する。この経路は:

- 検索語 (英語タイトル) と eBaymag 側の商品名インデックスが一致しないと候補 0 件になる
  (`PLOTTER` 4 品 / `Fostex R100T` / `HP 34401A` 等、実際に awaiting_import で滞留した 7 件で確認)
- 候補ごとに panel を開く (`_read_panel`) ため 1 job あたり UI 往復が発生し重い
- 検索語がヒットしても itm 不一致で終わるケースがある (id 5: `panel=None` = 候補は出たが
  itm 抽出自体が失敗)

## 変更方針

`ebaymag_graphql.list_products` (新設) で全商品の `listings[].publicationUrl` から
eBay item id を一括抽出した **id → product_id map** を作り、`discover_product_id` を
「map 参照 (即時)」→ 見つからない場合のみ「既存のタイトル/検索語一致」に **降格フォールバック**する。

### 変更ファイル

- `monitor/ebaymag_graphql.py`: `list_products(page, first=200)` 新設。Relay pagination
  (`pageInfo.hasNextPage` + `after`) で全件走査し `{id, listings{site{id} publicationUrl}}` を返す。
- `monitor/ebaymag_driver.py`: `fetch_product_map()` 新設 (GraphQL 経由で id map 構築、
  `EbaymagResult.product_map: dict[str, str]` フィールド追加、`_run_isolated` 経由の relay 対応)。
- `tasks/task_ebaymag_apply_queue.py`: `_process_job` の discover 経路を
  「map lookup (即時 hit) → 未 hit ならタイトル/検索語探索」の順に変更。

### 安全弁 (不変)

mutation 前の itm 照合安全弁 (`ebaymag_driver._open_panel_and_check_itm` L338-358、
`expected_itm == eid` を panel 上で再確認してから mutation) は **そのまま維持**。
map lookup は discover (product_id の発見) のみを高速化するもので、mutation 直前の
権威チェックを迂回しない。

### DB 変更

なし。`ebaymag_products` (migration v75, `ebay_item_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
site_states_json, last_synced_at, updated_at`) を再利用する (`upsert_ebaymag_product` 既存関数)。

### map 構築規約

- **item_id 衝突 (同一 item_id が複数 product_id に紐付く) は除外 + `logger.warning`** (Q0 痕跡)。
  mutation 対象を誤確定しない (silent に片方を採用しない)。
- **US (siteId="0") listing を優先** — 衝突時のタイブレークにのみ使う (通常は衝突 0 件想定、
  Phase 0 実測でも 0 件)。
- **空 map を `ok=True` で返さない** (`EbaymagResult.ok` は GraphQL 呼び出し自体の成否、
  map が空でも呼び出し自体が成功していれば `ok=True` は正当 — 「map に対象 item_id が無い」は
  discover の呼び出し元がタイトルフォールバックへ降格する判断材料であり、GraphQL 呼び出しの
  失敗ではない。この区別を実装コメントに明記する)。

### DoD

- 現在 `awaiting_import` で滞留している 7 件 (id 5,6,8,9,10,13,36) のうち、eBaymag に
  実在する 6 件が discover 成功に転じること (7 件目 = eBay item id 358724549446 は
  eBaymag に未取込のため対象外、Phase 0 probe で確認済)
- 誤 mutation 0 件 (itm 照合安全弁は無傷)
- T3 money-direct (eBay 反映 mutation を伴う経路) のため code-reviewer HIGH=0 + Codex/Fugu
  2 段レビュー + live 検証 (依頼ボード実 job での消化確認) を実施してから本番投入

## Phase 0 実測結果 (2026-07-05、read-only probe)

`scripts/_probe_w317_product_map.py` (CDP Chrome 9222 の既存 ebaymag.com タブに attach、
mutation なし) で以下を確定した。

### Q1: pagination 型

`products` field は Relay 型 Connection (`ProductConnection`: `edges` / `nodes` /
`pageInfo{endCursor hasNextPage}` / `totalCount` / `totalWithProblemsCount`)。
`after: String` 変数で cursor pagination 可能、`first` は 200 件 request で正常応答
(200 件区切り × 5 page で 823 件全走査、後述)。

**filters 引数の挙動 (重要、実装時の落とし穴)**: `products(filters: ProductFilterInput)` で
`archived: Boolean` filter がある。実測で:

| filters 値 | totalCount |
|---|---|
| 変数を `null` で渡す (未指定と同義) | **823** (全件) |
| `{}` (空 object を明示的に渡す) | 218 (`archived:false` と同一) |
| `{archived: true}` | 605 |
| `{archived: false}` | 218 |

`605 + 218 = 823` と一致。**`filters: null` (変数省略と同義) で archived 込みの全件が
1 回のクエリで取れる** — 空 object `{}` を渡すと暗黙に `archived:false` 相当に絞られてしまう
ため、実装では `variables={"filters": None}` を明示し、`{}` を渡さないこと。

全 823 件を `first:200` で 5 page (200×4 + 23) 走査し切れることを実測確認。

### Q2: 照合一致 (awaiting_import 滞留 7 件)

| job id | ebay_item_id | map 結果 | product_id | site(s) |
|---|---|---|---|---|
| 5 | 358689688709 | HIT | 728510235 | US(0) |
| 6 | 358724549446 | **MISS** | — | — (raw corpus 全体を文字列検索しても不在 = eBaymag 未取込) |
| 8 | 358663924423 | HIT | 718746508 | UK(3) |
| 9 | 358663938263 | HIT | 718746517 | ES(186) |
| 10 | 358663940394 | HIT | 718746536 | IT(101) |
| 13 | 358663696382 | HIT | 633543750 | UK(3) |
| 36 | 358738647421 | HIT | 740835643 | US(0) |

6/7 が map 一発で解決 (product_id 判明)。残り 1 件 (id=6, Fostex R100T) は
`publicationUrl` 全 823 商品 × 全 site listing (6584 slot) を素の文字列検索しても
`358724549446` が一切出現せず、真に eBaymag へ未取込と確認 (discover のタイトル一致でも
同じ理由で失敗していた実際の awaiting_import ログと整合)。map 化しても解決しない
真の未取込ケースは今まで通り awaiting_import backoff で待つのが正しい挙動。

map 全体の統計: 総商品 823 件、listing slot 総数 6584 (product × site の組)、うち
`publicationUrl` が non-null (=そのサイトで実際に出品中) が 912 件、item_id 抽出成功率
100% (912/912、抽出失敗 0 件 — 6584 中の残り 5672 は "そのサイトでは非出品" で
publicationUrl が null なだけであり、抽出ロジックの欠陥ではない)。unique item_id 数 912、
**item_id 衝突 (2 product_id が同一 item_id を指す) は 0 件**。

### Q3: US listing 有無

6 件の HIT のうち **4 件 (id 8,9,10,13) は US (site 0) listing を持たない**
(UK/ES/IT のみ)。すなわち map は「US listing がある商品」に限定しては作れない —
**どのサイトの publicationUrl から item id が拾えても、その product_id は同一商品を指す**
という前提 (eBaymag の 1 product = 複数サイトへの同時出品) が実測でも成立している。
US 優先ルールは衝突解消の tie-break 専用であり、通常の map 構築ではサイト種別を
問わず item_id が拾えた時点で採用してよい。

## 残課題 (Phase 1 実装スコープ)

- `ebaymag_graphql.list_products` / `ebaymag_driver.fetch_product_map` の実装
  (本 probe の queries をそのまま関数化、`_run_isolated` 経路含む)
- `task_ebaymag_apply_queue._process_job` の discover 呼び出し順序変更
  (map lookup → miss ならタイトル fallback)
- 823 件 map 構築のコスト (GraphQL 呼び出し 5 回、実測で数秒) を毎 apply_queue run で
  行うか、キャッシュ (TTL) するかは Phase 1 設計判断待ち
- T3 レビュー 3 点 (code-reviewer HIGH=0 / Codex or Fugu 2 段 / live 検証) は Phase 1 完了後
