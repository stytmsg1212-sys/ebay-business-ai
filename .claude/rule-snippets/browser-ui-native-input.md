# ブラウザ UI ネイティブ実入力則 (on-demand snippet)

UserPromptSubmit router keyword: `eBaymag / Playwright / CDP / ブラウザ操作 / browser / 自動操作 / graphql`。
詳細は本 snippet を Read on-demand。always-load 対象外 (topic 限定知識、hybrid 設計に従い snippet 化)。

出典: 2026-06-21 eBaymag「送料無料」チェック解除を `page.evaluate` の JS 合成クリックだけで試して「不能=user 手動」と誤確定 → Playwright ネイティブ `locator.uncheck()` では普通に動いた。`silent-skip-prevention.md` 負の能力主張ゲートの技術編。

## ⚡ 着手前チェックリスト (2026-06-21 時間浪費の教訓、必ず最初に読む)

web 自動化 (特に eBaymag) に着手したら **UI を触る前に** 以下を確認する。今回これを怠り、flaky な仮想化リストと数十回格闘してから GraphQL に気づき、膨大な時間を浪費した。

1. **network/API 層を先に見る (最優先)**: `page.on('response')` / `page.on('request')` で XHR/GraphQL を捕捉する。多くの SPA は裏で clean な GraphQL/REST を叩いており、**DOM スクレイプより桁違いに確実**。flaky な DOM と格闘する前に必ず API があるか確認する。
   - **eBaymag は GraphQL** (`https://ebaymag.com/graphql`)。下記「eBaymag GraphQL 契約」参照。
2. **DOM の前に DOM 構造を疑う**: 「リストが出ない/出る顔ぶれが変わる」= 仮想化 or **サーバ側ページネーション**。スクロール格闘は収束しないことが多い → API 層 (1) に切替える。
3. **UI 操作は native locator メソッド** (`locator.uncheck()/.fill()/.click()`)、`page.evaluate(el.click())` は使わない (下記「合成クリック≠実入力」)。
4. **複雑な JS は file に書いて実行**: `python -c` や bash inline に複雑な JS (正規表現 `/[\t\n]+/` 等) を埋めると **escape 地獄で何度も SyntaxError** になり時間を溶かす。`.py` ファイルに JS を raw string で置き `python -m` で回す。今回 inline JS の escape で 5+ 回失敗した。
5. **money-direct は保存前 snapshot + 保存後 read-back hard-abort** (可逆保証)。

## 核心: 合成クリック ≠ 実入力

`page.evaluate(() => el.click())` / `dispatchEvent` の合成イベントは **`isTrusted=false`**。React / Vue 等の controlled component は信頼イベントしか受けないことが多く、合成クリックを**黙って無視**する。「クリックしたのに状態が変わらない」の典型原因。

Playwright の **ネイティブ locator メソッド** は CDP の `Input.dispatch*` で**信頼イベント**を出すため、controlled component でも動く。

## 証拠強度の優先順 (上から試す)

1. **native locator action**: `locator.check()` / `.uncheck()` / `.fill()` / `.click()` / `.press()` / `.select_option()` — 第一選択
2. **role-based locator**: `get_by_role` / `get_by_label` / `get_by_text` で要素特定 → 上記 action
3. **CDP input** (native の内部、必要時のみ明示利用)
4. **DOM 検査** (`page.evaluate`): 構造調査・状態 read 専用
5. **`page.evaluate` の `el.click()` / `dispatchEvent`**: 最終手段。controlled component には効かない前提

## 要素特定は name/role 属性で確実に

テキスト近傍・親 label を勘で狙うと外す。`input[name="..."]` / role / label で実体を直接掴む。

### eBaymag 各国タブ構造 (実測 2026-06-21)

各国版送料ポリシー編集の各国タブ内 input name (`{pid}` = productId、`{country}` = au / co.uk / de / it / fr / es ...):

- `{pid}-cp-{country}-switcher` … 各国調整トグル (checkbox)。**先に native check で ON にすると各国 cost 欄が出現**
- `{pid}-cp-{country}-ds-0.cost.price` … 送料額 (real input、`.fill()` で書換可)
- `{pid}-cp-{country}-ds-0.cost.additional` … 追加送料
- `{pid}-cp-{country}-ds-0.cost.free` … **送料無料 (real `input[type=checkbox]`)**。`.uncheck()` で解除 → price 欄が enabled に連動
- `{pid}-cp-{country}-excluded-countries` … 除外国 (hidden)

CDP 接続: `chromium.connect_over_cdp("http://localhost:9222")` (eBaymag ログイン済セッション)。

⚠️ **/shipping 一覧 UI は使うな** (2026-06-21 確定): ポリシー一覧は **GraphQL のサーバ側ページネーション** で 1 ロード ~7 件しか DOM に出ず、出る顔ぶれもロード毎に変動。スクロール格闘は収束しない。ポリシー編集は下記 **GraphQL 経路** を使う。UI は値検証の目視のみ。

## eBaymag GraphQL 契約 (2026-06-21 リバースエンジニアリング済、再導出禁止)

eBaymag は全て `https://ebaymag.com/graphql` (POST)。ログイン済 CDP の `page.request` で叩ける。

**読取** (ポリシー詳細): 応答 `data.profile.shippingEbayProfiles[]` = 各 eBay サイト 1 件。
- `{id, title, siteId, managedByUser, payload.shippingOptions[].shippingServices[].shippingServiceCode}`
- siteId → サイト: `0=US / 2=CA / 3=UK / 15=AU / 71=FR / 77=DE / 101=IT / 186=ES`
- serviceCode 例: `CA_StandardShipping` / `AU_ExpressDelivery` / `UK_ExpeditedShippingFromOutside` / `DE_ExpressversandAusDemAusland` / `FR_ExpressDeliveryFromAbroad` / `ES_ExpressDeliveryFromAbroad` / `IT_ExpressCourier`

**書込** (送料設定): mutation `ShippingProfileSave` → `upsertProfile(input: upsertProfileInput!)`。
- `variables.input.profile` に profile 全体 (title/id/excludedCountries/tariffs/country/city/**ebayProfiles[]**) を送る
- 対象サイトの ebayProfile を `managedByUser:true` + `domsEbayTariffs:[{shippingServiceCode:"{CC}_...", freeShipping:(usd==0), shippingCost:usd, additionalShippingCost:0}]` にする
- **他サイト・他フィールドは読取値をそのまま保持して送る** (差分でなく全体 upsert。落とすと消える)
- read/write の ebayProfile id は同一系統
- 捕捉スクリプト: `scripts/_capture_graphql_save.py` (mutation) / `_capture_graphql_read.py` (profile 読取)

**手順**: ①profile 読取 (id/serviceCode 取得) → ②canonical 値で ebayProfiles 構築 (他は保持) → ③upsertProfile 送信 → ④read 再取得で値 exact 一致を hard-abort 検証 → ⑤実 ebay.{site} ページ目視。1 ポリシー canary → 全展開。

## 成否検証は application state で

controlled component は DOM の `checked` 属性だけでなく **連動する別要素** で確認する (例: cost.free を uncheck → cost.price が `is_enabled()=True` に変わる)。DOM state 単独では不十分。

## money-direct 時 (送料/価格の mutate)

- 保存 (「変更を適用」) 前に **itm 照合** (誤商品 mutation 防止の権威安全弁)
- 保存後は **実 ebay.{site} ページを買い手ロケーションで目視** (Browse API 送料は信頼不可、実ページが正)
- spike は **reload 破棄** で安全に検証 (保存しなければ何も変わらない)

## 関連

- `.claude/rules/silent-skip-prevention.md` 負の能力主張ゲート (本 snippet の behavioral 親)
- memory `feedback_ebaymag_native_playwright_input_works.md` (事故詳細)
- memory `feedback_check_full_tool_surface_before_cannot.md` (全ツール面棚卸し)
