# 損益分岐の送料(関税)基準ズレ — 分析と設計提案 (2026-06-02)

出典: 2026-06-02 user 指摘 (item 357859072999 Mitsubishi Uni 240色). main agent (Opus 4.8) 分析 → Codex 独立レビュー対象.

## 1. 問題 (user 指摘)

商品管理タブの損益分岐は **実送料 $108 ではなく「商品価格 × duty_rate 20% = $120」を送料(関税)と仮定して逆算**している。現在総額の送料 $108 (eBay 実出品の sync 値) と別物。

user 提案: **実際に設定している送料(関税)で損益分岐を出すべき**。ただし US_only は商品価格に関税を内包する等パターン差がある。US_only 以外の全パターンで不具合が出ないか検証要。

## 2. 現状実装の確定事実 (コード検証済)

- `monitor/lowest_price.py::compute_breakeven_price_usd` は引数に **primary_market を持たない**。`CalcInput(is_ddu=False, country_code="US", duty_pattern=None)` で固定呼出 → `calculator.calculate` 内で `pattern="shipping"` に解決。
- `pattern="shipping"` では `shipping_usd = item_price × duty_rate` (= 20%)、buyer 徴収送料は washed (`duty_cost_jpy=0`)。FVF は (item + shipping) に課金。
- `ebay_listings.shipping_cost` ($108) は **eBay 実出品から sync された実表示送料** (`ebay_sync.py` / `ebay_client.py`)。mixed_global では 差分式により US 表示送料 ≒ DDP 関税。
- **手動計算タブ (app.py L1958-1988)** は duty_pattern を market 別に selectbox で選べ、送料(関税)を手入力 override 可能 = 既に user 提案の挙動を持つ。
- → **商品管理の損益分岐(固定 shipping/20%) と 手動計算タブ(pattern 可変) のロジックが乖離**しているのが根本原因。

## 3. duty_pattern と 4 区分の対応 (reference_shipping_tariff_logic v2.3)

| 区分 | 正 duty_pattern | 商品価格 | US表示送料 | 関税の負担 |
|---|---|---|---|---|
| US_only | included | 商品代+関税(内包) | $0 Free | seller実費 (duty_cost_jpy 計上) |
| mixed_global | shipping | 商品代のみ | 差分0 + DDP関税 | buyer徴収送料 = 関税 (washed) |
| global_only | shipping(US) or ddu(非US主) | 商品代のみ | $0 Free (US客来ない前提) | US来たら自腹許容 |
| unknown | shipping (mixed default) | 商品代のみ | 差分0 + DDP関税 | mixed と同 |

現状 breakeven は **全区分を mixed_global(shipping/20%) で計算** = US_only/global_only は構造的に誤り。

## 4. user 提案の cross-pattern リスク分析 (naive に「configured shipping を使う」場合)

| 区分 | naive 危険度 | 内容 |
|---|---|---|
| US_only | 🔴 重大 | configured shipping=$0 を shipping パターンで使うと関税が消失 → breakeven 過小 → **赤字出品リスク**。正しくは included パターンで duty を seller 実費計上すべき |
| mixed_global | 🟡 注意 | 概ね妥当だが `shipping_cost` が NULL/0 の listing は関税消失 → 過小 breakeven。fallback 必須 |
| global_only | 🟡 注意 | US基準20%は過剰保守だが、US客が来た時の自腹リスクを無視すると過小。country_code='US' 固定との整合要判断 |
| unknown | 🟡 注意 | mixed 同様 fallback 必須 |
| 全般 (washing) | 🟠 構造 | shipping パターンは「buyer送料 = 実関税」を前提に washed。configured shipping が実関税 (特に Section232 25-50%) より低いと profit 過大計上。Section232該当品で危険 |

## 5. 推奨設計 (main agent 案 — Codex 検証対象)

### 方針: breakeven を primary_market-aware にし、手動計算タブと同一の duty_pattern 解決を共有する

1. `compute_breakeven_price_usd` に `primary_market` と `configured_shipping_usd` を追加引数で渡す (商品管理は ebay_listings から取得済)。
2. duty_pattern を market 別に解決 (app.py L1968-1988 のロジックを共通 helper に切出して再利用):
   - US_only → `included` (duty = item×duty_rate を seller 実費、送料$0)
   - mixed_global / unknown → `shipping` + `shipping_usd_override = configured_shipping` (実関税)。**ただし configured が NULL/0/item×duty_rate より大幅に低い場合は item×duty_rate に fallback** (money-direct 保守)
   - global_only → 要判断 (US自腹リスクを取るか non-US DDU で見るか)
3. **安全床**: configured shipping が実関税を反映している確信が無い限り、breakeven を `item×duty_rate` ベースの保守値より下げない (Q0 赤字出品防止)。
4. 表示: どの pattern/market/送料基準で算出したか UI に明示 (現在の「これ以下で赤字」help を基準明示に変更)。

### 副論点 (同時に検討)
- 消費税還付の扱い: 現 breakeven は `profit` (還付抜き)、仕入先採用判定は `profit_with_refund` (還付込み)。同一システム内で利益定義が不整合 ($601 vs $553)。breakeven も還付込みに統一すべきか (business 判断)。
- weight: 当該品は本日 manual_edit で 6300g (confidence medium)。実測要確認 (breakeven 感度 ±$35)。
- duty_rate 20% は settings global。Section232 該当/非該当で実関税が大きく違う (色鉛筆HS9609=非該当で実15%)。per-listing duty 化も将来課題。

## 6. Codex への問い (独立レビュー依頼)

1. §5 の primary_market-aware 設計は 4 区分すべてで金銭事故 (赤字出品 / profit 過大) を防げるか。見落としパターンは無いか。
2. 「configured shipping を使う」naive 案の §4 リスク分類は妥当か。追加の危険ケースは。
3. washing semantics (shipping パターンで実関税 ≠ buyer送料 の時に profit 過大) を断つには、shipping パターンでも実関税を別途 cost 計上する設計が要るか。
4. breakeven と手動計算タブのロジック共通化 (duty_pattern 解決 helper 切出し) の妥当性。
5. 全体アーキテクチャ視点: 「金額の軸 (商品のみ/送料込み/還付有無/4区分/DDP/DDU) が点在して分かりづらい」を解消する単一 canonical economic model への集約案について意見。

## 7. 2 者独立レビュー結果 (2026-06-02、完全一致)

**code-reviewer (Opus 4.8) + Codex (gpt-5.5) が独立に同一結論**。両者の合意:

### 合意①: 真のバグは breakeven 基準ズレではなく **washing (関税相殺) 自体** (両者 HIGH)
`pattern="shipping"` は `shipping_usd_override` を buyer 徴収額として使い `duty_cost_jpy=0` (相殺済扱い)。profit は revenue_net ベース (calculator.py:315-324, 415-418)。**実関税 > buyer 徴収送料 (特に Section 232 25-50%品) の時、差額が profit から落ちず過大計上 → 赤字を黒字誤認**。breakeven だけ直しても profit 表示・仕入先採用判定 (profit_with_refund, calculator.py:544-572) に過大利益が伝播。

### 合意②: §5 設計は赤字出品 floor には効くが profit 過大計上は防げない (Codex MED)
安全床は「過小 breakeven (赤字出品)」を防ぐが「過大 profit」は防げない。**washing を §5 の scope に格上げ必須**。

### 合意③: 修正の正しい順序 (両者一致)
1. **FIRST: washing を 2 軸分離** — `buyer_shipping_usd` (徴収額) と `actual_duty_cost_jpy` (実関税) を別項目化し、`shipping` パターンでも実関税を明示 cost 計上。calculator の構造修正。
2. **THEN: breakeven を primary_market-aware 化 + 手動計算タブと duty_pattern 解決 helper 共通化** (両者 HIGH)。修正済 calculator の上で。
3. **canonical economic model 集約** (両者 HIGH、ただし K1 で段階的)。最低限のフィールド: item_price_usd / buyer_shipping_usd / actual_duty_cost_jpy / shipping_cost_jpy / fee_base_usd / profit_ex_refund / profit_with_refund / market_pattern / duty_source_confidence。

### 追加リスク (Codex)
- `shipping_cost=0` が「本当に free」か「未取得 NULL」か区別不能 → 関税消失リスク (lowest_price.py:120-121)
- `global_only` を `country_code="US"` 固定で評価 (calculator.py:289) → 「要判断」のまま実装すると default 落ちで赤字
- **per-listing HS/duty が無く global `duty_rate` 20% だけで Section232/非該当を扱う** = 安全床も実 50%/25% を映さず床が低すぎる (要 per-listing 実関税率)

### 設計ノート自体の訂正 (code-reviewer 指摘、md-files-can-be-wrong)
- §3 表「mixed_global … washed」は washed が "正しい" かのような書き方 → 実は L418 の既知欠陥。「(注: 実関税≠徴収送料 で profit 過大、§4🟠)」を併記すべき
- §4 表に **ddu 誤分類** (global_only を ddu に倒すと関税完全ゼロ) リスクが欠落

### 還付不整合 (両者 LOW、business 判断)
breakeven=還付抜き ($601) vs 仕入先採用=還付込み ($553)。**breakeven は還付抜き (保守) 維持推奨** (還付込みは Section232 と重なると危険)。UI に基準明示。
