# ブラウザ UI ネイティブ実入力則 (on-demand snippet)

UserPromptSubmit router keyword: `eBaymag / Playwright / CDP / ブラウザ操作 / browser / 自動操作`。
詳細は本 snippet を Read on-demand。always-load 対象外 (topic 限定知識、hybrid 設計に従い snippet 化)。

出典: 2026-06-21 eBaymag「送料無料」チェック解除を `page.evaluate` の JS 合成クリックだけで試して「不能=user 手動」と誤確定 → Playwright ネイティブ `locator.uncheck()` では普通に動いた。`silent-skip-prevention.md` 負の能力主張ゲートの技術編。

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
