---
title: W138 設計書 — 商品管理 Shipping Policy 表示 + 変更
status: design-review (実装前。内部review→Codex→user確認→実装)
created: 2026-05-17
author: code-architect (Opus 4.7) / orchestrated by Claude Opus 4.7
roadmap: W138 (id=222, 進行中)
process: 設計書 → 内部レビュー&修正 → Codex レビュー → user 確認(Q-1〜Q-6) → 実装&テスト(Q1 DoD)。user 確認(Q-1〜Q-6)は実装(build step 4b-6)着手前の必須ゲート (Codex MED-4)
---

# W138 設計書: 商品管理タブ — Shipping Policy (Business Policy) 表示 + 変更機能

## 内部レビュー反映 (2026-05-17 / code-reviewer Opus HIGH-1〜4 修正、本節が下位矛盾箇所に優先)

初版 (architect) は「XML builder 無改修で `seller_profiles['shipping_id']` 差替えのみ」と
楽観前提を置いたが、内部レビューで実コードと矛盾と判明。以下を**確定設計**とする
(下記旧本文の該当記述より本節が優先。contradiction-annotation):

- **HIGH-1 (確定)**: `_build_revise_with_shipping_xml:947` は `<SellerProfiles>` を
  `if ship_cost_usd is not None and shipping_id` でガード。`ship_cost_usd=None` だと
  `<ShippingProfileID>` を運ぶ `<SellerProfiles>` が出ず BP 変更不能。さらに
  `revise_fixed_price_with_shipping:1011` は price/ship 両 None で API 呼ばず早期
  return。→ 案2「送料非同梱で BP のみ revise」は現状**物理的に実行されず偽装成功
  (Q0 違反) を構造的に生む**。**対策 = W136 override ガードを壊さない別経路を新設**
  (K2: 既存 override 経路に副作用を与えない):
  - 新規 `monitor/ebay_client.py::_build_revise_bp_only_xml(item_id, seller_profiles)`
    = `<Item><ItemID>` + `<SellerProfiles>`(Payment/Return/Shipping 3 ID) のみ出力、
    `<ShippingServiceCostOverrideList>` も `<StartPrice>` も出さない。
  - 新規 `monitor/ebay_client.py::revise_shipping_profile(item_id, seller_profiles,
    app_id, dev_id, cert_id, user_token) -> dict` = 上記 XML を `_call_trading_api`
    ("ReviseFixedPriceItem") で送出。早期 return ガードは持たない (BP のみで実行)。
  - `_build_revise_with_shipping_xml` / `revise_fixed_price_with_shipping` の
    W136 override 経路は**一切改修しない** (gate 条件不変 = 既存テスト/W136 不変、
    D1 後方互換維持)。これにより auto-HIGH の W136 回帰リスクを構造的に排除。
- **HIGH-2 (確定)**: `_apply_to_ebay` の Phase2 差分検出 / Phase4 verify は
  price/ship/sku のみ。BP 経路を以下で接続:
  - 入力: handler が `editing["new_bp_id"]` (selectbox 選択 BP id) を設定。
  - Phase2: `bp_changed = bool(new_bp_id) and new_bp_id != snap.shipping_profile_id`。
  - 「実 eBay と差分なし」early-return 条件に `bp_changed` を OR で追加 (BP 単独
    変更で early-return しない = silent skip 防止)。
  - Phase3: `bp_changed` のみなら `revise_shipping_profile(eid,
    {payment_id,return_id,shipping_id=new_bp_id}, *creds)` を呼ぶ。
  - Phase4: `bp_ok = (snap2.shipping_profile_id == new_bp_id)` を実値 verify し
    `overall_ok` に AND 合成。戻り dict に `bp_ok: Optional[bool]`。
- **HIGH-3 (確定)**: 上記 revise が実行されれば post snapshot 取得 →
  caller L1395 `if snap2 is not None: _sync_db_to_actual(eid, snap2)` で
  **BP default に化けた実 ship_cost が DB に同期される** (`_sync_db_to_actual` は
  snap2.ship_cost_usd を書く既存実装、W137)。`_hero_effective` が DB.shipping_cost
  追従。bp_changed 単独でも post_snapshot が非 None になることを実装で担保。
- **HIGH-4 (確定)**: 警告文言を **DDP 関税 buffer 喪失の業務インパクト明示**に
  書換 (下記 §C / UI フロー参照)。本プロジェクト送料は US 軸差分式 + DDP 関税
  buffer 上乗せ (`reference_shipping_tariff_logic.md`)。custom override =
  DDP 関税込み戦略送料、BP default = buffer 無し素送料の蓋然性が高く、BP 変更で
  リセット = **売主が DDP 関税丸被り赤字方向** (Section 232 派生品は数百ドル/件)。
- **auto-HIGH 回帰テスト**: ビルドシーケンスに W136 override 経路の非回帰
  + BP-only 経路テストを必須化 (§ビルドシーケンス・DoD#1 参照)。
- **MEDIUM 反映**: Q-6 (Inline shipping = 非 BP listing 混在で
  `shipping_profile_id=None` の扱い) を未確定点に追加。DoD/build の「44 policy」は
  「全 active fulfillment policy (件数は実測記録)」と件数非依存表現に。

---

## 概要

商品管理タブ (`tabs/tab_product_management.py`) の各商品 expander に、(A) 現在 eBay に
設定されている shipping Business Policy (BP) 名を表示し、(B) Account API 由来の BP
一覧から別 BP を選択して `ReviseFixedPriceItem` で変更できるようにする。(C) 最重要課題
として、eBay 仕様上 BP を別 policy に変更すると per-listing の `ShippingServiceCostOverride`
(Buyer pays first / each) が BP default cost に戻る挙動を、W137 の pre/post GetItem
verify・fake success 排除原則と矛盾しない形で安全に取り扱う。本書は実装せず複数
アプローチ+推奨を提示する設計フェーズ成果物 (Q3 /feature-dev Phase 4)。

## スコープ

### 含まれる
- A: 現在 BP 名の UI 表示
- B: BP 一覧取得 client (Account API `fulfillment_policy`)、selectbox UI、`ReviseFixedPriceItem` で `SellerShippingProfile` 変更
- C: override-reset 挙動の複数アプローチ + 推奨
- D: W137 (pre/post snapshot verify / `_sync_db_to_actual` / `_hero_effective` / fake success 排除) との整合
- DB スキーマ要否の判断、ビルドシーケンス、リスク表、未確定点、Q1 DoD

### 含まれない (K1 Simplicity)
- 4 区分 primary_market 別の自動 BP 切替 (別 W)
- payment / return policy の表示・変更 (要望外)
- BP 自体の作成・編集 (listing への割当のみ)
- 一括 (bulk) BP 変更 (要望は 1 listing 単位)
- `shipping_policy_selector.py` (settings.json 重量マップ) の改修

## 重要な調査結果 (実コード/一次情報で確認)

| 項目 | 実態 |
|---|---|
| `ListingSnapshot` | **payment_profile_id / return_profile_id / shipping_profile_id の 3 ID を抽出済** (W137 実装、ebay_listing_snapshot.py:44-46)。BP-only revise はこの 3 ID を `<SellerProfiles>` に同梱必須 (shipping のみは不可、W136 と同構造=不完全 SellerProfiles だと Ack=Fail or 意図せぬ payment/return policy 適用 risk)。`shipping_profile_name` は **未返却** (名前は Account API id→name lookup) |
| Account API BP 一覧取得 | **既存実装なし**。`shipping_policy_selector.py` は settings.json マップのみ → 新規 client 必要。W137 prep の 44 policy 取得は使い捨て検証由来 |
| `_build_revise_with_shipping_xml` | `seller_profiles` で `<SellerProfiles>` 同梱出力。現状 `_apply_to_ebay` は pre-snapshot の shipping_id を渡す (= BP 維持)。BP **変更**には新 BP ID を渡す経路拡張が必要 |
| DB `ebay_listings` | `shipping_profile_id`/`_name` 列 **無し**。`_sync_db_to_actual` は price/shipping_cost/sku のみ同期 |
| eBay 一次情報 (WebSearch 2026-05-17) | (1) override は listing-level concept、BP profile 自体は変えない (2) **「service priority/option が call と BP profile で不一致なら error」** (3) Revise で shipping field 省略すると消える |

## 作成 / 修正ファイル

| 区分 | パス | 役割 |
|---|---|---|
| 新規 | `monitor/ebay_account_policy.py` | Account API `GET /sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US` の read-only client。OAuth は `get_valid_access_token()` (sell.account scope 既存) |
| 新規 | `tests/test_ebay_account_policy.py` | HTTP モック単体 (200/401/parse error/空/marketplace) |
| 修正 | `tabs/tab_product_management.py` | (A) hero pill に現 BP 名 / (B) form 内 selectbox + `_apply_to_ebay` BP 変更分岐 / (C) override-reset 警告 UX |
| 修正**必須** | `monitor/ebay_client.py` | **新規 `_build_revise_bp_only_xml` + `revise_shipping_profile`** を追加 (BP のみ変更専用、`<SellerProfiles>` = **Payment/Return/Shipping 3 ID を pre-snapshot 由来で同梱**。shipping のみ出力は不可=W136 と同構造、不完全だと Ack=Fail/誤 policy 適用 risk=Codex HIGH-2)。`StartPrice`/`ShippingServiceCostOverrideList` は出さない。W136 override 経路 (`_build_revise_with_shipping_xml`/`revise_fixed_price_with_shipping`) は**改修しない** (gate 不変=既存テスト/W136 不変、D1)。理由: 初版「無改修見込」は実コード矛盾 (内部レビュー HIGH-1 で訂正) |
| 新規 | `tests/test_ebay_client_bp_revise.py` | BP-only XML 出力 + W136 override 経路非回帰 (HIGH-1/auto-HIGH) |
| 修正(要否) | `monitor/database.py` | **推奨: 列追加せずメモリ表示のみ** (下記) |
| 参照更新 | `reference_shipping_tariff_logic.md` / `source_ebay_api_shipping.md` | 実装後 cascade-update。`source_ebay_api_shipping.md` v2.3 §5.2 (SellerProfiles 同梱で override bind / priority 説 falsified) に **W138 の BP-only 経路 (SellerProfiles 3ID のみ・override 非同梱で BP 単独変更が成立)** を追記 (v2.3 知見の自然拡張、Codex cascade 提案)。本設計段階は予告 |

## DB 変更: **推奨 = スキーマ変更なし**

根拠 (K1 + W137 思想): W137 核心は「DB を信頼せず実 eBay GetItem が真実源」。DB に
`shipping_profile_id` を持つと W137 が排除した DB↔eBay 乖離を新カラムで再生産。BP 現在値は
表示の都度 pre-snapshot で取得すれば最新保証。expander を開いた listing のみ on-demand 取得
(W137 `_apply_to_ebay` pre-snapshot と整合)。列追加の唯一の正当化は「一覧画面に BP 列」
だが要望外 (K1) → 未確定点 Q-1。

## コンポーネント設計

### 1. `monitor/ebay_account_policy.py` (新規, read-only)
```
def fetch_shipping_policies(config: dict, marketplace_id: str = "EBAY_US") -> ShippingPolicyList
  ShippingPolicyList(policies: list[ShippingPolicyInfo], ok: bool, error: Optional[str])
  ShippingPolicyInfo(policy_id: str, name: str, service_names: list[str], domestic_service_count: int)
```
- OAuth: `get_valid_access_token()` → Bearer。sell.account scope 既存 (追加 consent 不要)。
- `ok=False` 時 `policies=[]` + `error` (Q0: 不明を空成功と偽らない、UI は selectbox 非表示+明示)。
- HTTP 失敗 / 非 JSON / total=0 を graceful (raise しない、`_call_trading_api` F5 防御と同思想)。
- キャッシュは UI 層 (`@st.cache_data(ttl=300)` 相当) でラップ、client 自体は純関数 (W134 流儀)。

### 2. `ebay_listing_snapshot.py`: **推奨 = 拡張しない**
名前は `fetch_shipping_policies()` の id→name dict で UI 側 lookup。GetItem に
ShippingProfileName が安定して入るかは一次情報未確認 (憶測リスク)。Account API は
`fulfillmentPolicy.name` を確実に返す → id→name 解決を Account API 1 経路に集約。

### 3. `tab_product_management.py` 修正
| 関数 | 変更 |
|---|---|
| `_render_left_basic_and_physical` | 「💵 eBay 出品」section に `st.selectbox("Shipping Policy", ...)` を **form 内** に追加 (st.form submit 確定。form 外配置は毎 rerun=画面暗化回帰 R6) |
| 新規 `_get_current_bp(eid, config)` | 軽量 snapshot → shipping_profile_id → 一覧 dict で name 解決。snapshot 失敗時 selectbox disabled + 「現在BP取得不可」(Q0) |
| `_render_hero_metrics` | hero pill 行に `Ship BP: <name>` pill 追加 (専用 section でなく pill 推奨=既存統一、二重表示回避) |
| `_apply_to_ebay` | BP 変更分岐追加。`seller_profiles['shipping_id']` を新 BP ID に差替えて revise。pre/post snapshot で shipping_profile_id 一致を verify (Ack でなく実値)。戻り dict に `bp_ok: Optional[bool]` 追加 |
| `_sync_db_to_actual` | DB 列追加しない方針なら無改修 |

## C (最重要): BP 変更が override をリセットする挙動

> ⚠️ 本章の案2 実装機序・警告文言は **冒頭「内部レビュー反映」節 (HIGH-1/HIGH-4)**
> で確定版に訂正済。案2 は「送料非同梱で BP のみ revise」でなく **新規
> `revise_shipping_profile` (専用 BP-only 経路)** で実行する。警告は DDP 関税
> buffer 喪失の業務インパクトを明示する。以下旧記述は経緯保存 (両論併記)。

### eBay 仕様 (一次情報 + 憶測明示)
- 【確定】override は listing-level、BP profile は変えない。**「service priority/option が
  call と BP profile で不一致なら error」**。Revise で shipping field 省略=削除。
- 【憶測・未確証】user 報告「eBay 画面で BP 変更すると Buyer pay/each が default に戻る」は
  eBay UI 挙動。API で「新 BP + override を同一 ReviseFixedPriceItem 同梱」時に override が
  新 BP に効くかは **本プロジェクト未検証** (W136 実証済は「同一 BP 維持 + override」のみ)。

### アプローチ比較

**案1: 同一 Revise で「新 BP + 現フォーム送料 override」同時送出 (custom 送料維持試行)**
- 操作 1 revise + 2 GetItem。fake success リスク低 (post 実値 verify)。ただし新旧 BP の
  service 不一致で **Ack=Fail or override 無音無視** の一次情報リスク。verify ❌ で警告 →
  fake success にはならないが「default 送料に化けた」期間が verify 表示まで一瞬。
- UX 最シンプル (1 操作)。money: override 効けば維持、効かねば post verify が実値併記。

**案2: BP のみ変更 + 事前明示警告 (override 捨て、推奨主)**
- selectbox 変更時「⚠️ BP 変更で custom 送料 Buyer pays/each は新 BP default にリセット
  されます」を form 内表示。送料欄送らず BP のみ revise。post snapshot で BP id verify +
  実 BP default 送料を `_sync_db_to_actual` で DB 反映 (DB:=真実)。
- 操作 1 revise + 2 GetItem。fake success リスク **最低** (ship 変更なし→verify は BP id のみ)。
  UX: custom 送料必要なら BP 変更後に送料再入力+再反映 (2 操作) だが挙動予測可能・透明。
  money **最低リスク** (eBay 仕様を隠さず明示)。

**案3: override 退避→BP 変更→override 再適用 の 2 段 revise**
- 2 revise + 3〜4 GetItem (最重)。状態複雑・API 倍増。K1 違反気味。**不採用**。

### 推奨: **案2 を主実装。案1 は「未確定点 Q-2」として実機 falsify + user 判断後の別フェーズ**
根拠: (1) 金銭直結 + 案1 の「別 BP × override 同梱」eBay 挙動が憶測 → 仕様確定の案2 を
先行 (CLAUDE.md「憶測明示+実機検証」、W137 で priority 説を実機 falsify した教訓)。
(2) 案2 は W137 fake success 排除と最も整合 (ship 変更しない→BP id verify だけで成否明確)。
(3) 案1 は実機 falsify-or-confirm 後に「custom 送料維持」チェックボックス付き上位 UX
として後続フェーズ追加 (K1: 確証なき speculative を今やらない)。

実装スコープ (本 W138): **案2 のみ**。

## UI フロー
```
[商品 expander 展開]
  ├─ hero pill 行: [ID][SKU][区分][Rank][仕入先][Ship BP: <現BP名>]   ← A
  │     現BP = _get_current_bp(eid) = 軽量snapshot→id→一覧dictでname
  ├─ st.form(pm_form_{eid}) 内「💵 eBay 出品」:
  │     [商品価格][送料 Buyer pays][送料 +each]
  │     [Shipping Policy ▼ selectbox] options=一覧name, index=現BP   ← B
  │       現BP取得失敗時: disabled + "現在BP取得不可、変更不可"
  │     選択≠現BP の時 form 内注意 (HIGH-4 確定文言):
  │       "⚠️ BP を変更すると送料が新 BP の default に戻ります。現在の
  │        custom 送料に **DDP 関税 buffer** が含まれる場合、buffer が消え
  │        **売主の関税負担 (赤字方向、Section 232 該当品は数百ドル/件)** が
  │        発生し得ます。変更後に送料を再設定してください。"
  │       + 数値起点 (Codex MED-3): pre-snapshot 取得の現 custom 額を併記
  │        "現在 Buyer pays $X / +each $Y → BP 変更後は新 BP default に置換
  │         (新 default 額は BP 適用後 GetItem で判明=変更前取得不可、eBay仕様)"
  └─ [💾DB保存][📤eBay反映][💡利益計算]   ← 既存3ボタン
```

## データフロー
```
expander open
  → _cached_shipping_policies(config)   # Account API 全active policy (N件,実測) (UI層 ttl=300)
  → _get_current_bp(eid, config)        # GetItem snapshot.shipping_profile_id
  → name = policies_dict.get(id)        # id→name (Account API由来)
  → hero pill / selectbox 描画

[📤 eBay 反映] submit
  → _apply_to_ebay(eid, editing, config)
      Phase1 pre snapshot (GetItem) shipping_profile_id (W137既存)
      Phase2 差分: bp_changed = bool(new_bp_id) and new_bp_id != snap.shipping_profile_id
              no-diff early-return 条件に bp_changed を OR (BP単独で skip しない)
      Phase3 revise (案2 確定): bp_changed のみ→ **revise_shipping_profile(eid,
              {payment_id,return_id,shipping_id=new_bp_id}, *creds)** (専用
              BP-only XML、override 非同梱、HIGH-1 訂正経路)
              ※ price/ship 併存時の順序 = Q-3
      Phase4 post snapshot: bp_ok=(snap2.shipping_profile_id==new_bp_id) # 実値verify
              ship_cost は BP default 化値を取得→メッセージ併記+overall_ok合成
      return {..., bp_ok, post_snapshot}
  → _sync_db_to_actual (price/shipping_cost/sku; BP列なし方針)
      BP変更で ship_cost→BP default に変化 → DB.shipping_cost も実値同期 (DB:=真実)
  → _hero_effective: DB.shipping_cost 更新済→hero「現在総額」自動追従
```

## ビルドシーケンス
1. `monitor/ebay_account_policy.py` 新規 (read-only client, Q0 流儀)
2. `tests/test_ebay_account_policy.py` (HTTP モック) pytest PASS
3. 実機 read-only: 全 active fulfillment policy 取得 (件数は実測記録、ハードコードしない)、fulfillmentPolicyId == GetItem ShippingProfileID 突合 (W137 prep 実証の再確認)。同時に BP 管理率を実測し Q-6 (Inline shipping 混在) を判断
4. `tab_product_management.py`: `_cached_shipping_policies`/`_get_current_bp`/hero pill (A) — eBay write なし、Streamlit+Playwright 描画確認
4b. **(HIGH-1)** `ebay_client.py` 新規 `_build_revise_bp_only_xml` + `revise_shipping_profile`。`tests/test_ebay_client_bp_revise.py`: (i) BP-only XML が `<SellerProfiles>`(新BP) 出力 + `ShippingServiceCostOverrideList`/`StartPrice` 非出力 (ii) **W136 回帰**: `_build_revise_with_shipping_xml` の ship_cost_usd 指定時は従来通り override+SellerProfiles 同梱 (gate 不変) (iii) BP-only が早期 return で弾かれない。pytest PASS
5. selectbox + `_apply_to_ebay` BP 変更分岐 (案2=revise_shipping_profile) + DDP buffer 警告 (B,C)。`editing["new_bp_id"]` 入力経路
6. `_apply_to_ebay` Phase2 bp_changed / no-diff early-return に bp_changed OR / Phase4 bp_ok 実値 verify→overall_ok 合成 / bp_changed 単独でも `_sync_db_to_actual` 発火担保 (HIGH-2/3、D 整合)
7. code-reviewer HIGH=0 ループ (Q4) — W136 override 経路非回帰を必須確認
8. Codex 外部 review (案1 eBay 仕様 falsify-or-confirm 含む)
9. user 確認 (Q-1〜**Q-6**) = **実装着手前の必須ゲート** (Codex MED-4: step 4b-6 の control flow を Q-3/Q-6 が規定するため、本ステップは step 4b より論理的に先行。user 指定プロセス Codex→user確認→実装 と整合)。step 4b 以降は Q 回答後に着手
10. Q1 DoD 11 ステップ (実 BP 変更は user 監視下、検証後原状回復、W137 安全線)

## リスク表
| # | リスク | 影響 | 緩和 |
|---|---|---|---|
| R1 | 別 BP×override 同梱の eBay 挙動 未確証 (案1) | 案1 採用時 fake success/送料化け | 本 W は案2 のみ。案1 は実機検証+user 判断後別フェーズ (Q-2) |
| R2 | BP 変更で override→BP default = 意図しない送料で販売 | 金銭直結 (過小→赤字/過大→売れない) | 案2=事前明示警告 + post snapshot で実 BP default 送料併記 + DB 実値同期 |
| R3 | 新旧 BP service priority/option 不一致で Revise Ack=Fail | BP 変更失敗 | 案2 は override 非同梱で priority 不一致起きにくい。post で BP id 不一致→❌ 明示 (Q0) |
| R4 | Account API 取得失敗 (token/quota/障害) | selectbox 出せない | ok=False+error 明示、disabled、Q0 で偽らない |
| R5 | DB に BP 列→W137 が消した乖離再生産 | 表示が古い | DB 列追加しない (推奨)、都度 snapshot |
| R6 | selectbox form 外→毎 rerun 画面暗化回帰 | UX 劣化 | form 内配置 (st.form submit 遵守) |
| R7 | price/ship 変更と BP 変更を同一 submit 併発 | 順序依存・部分成功 | Q-3 で順序方針確認。最小は「BP 変更時 price/ship 別 submit」制約も選択肢 |
| R8 | quality-gate hook | block で停止 | ALTER 不使用 (DB変更なし) / bare except 不使用 / print(stderr) 不使用 = block 対象なし |
| R9 | 観測可能性 | silent skip | UI 表示 (BP pill + verify メッセージ)。本機能は user 操作起点で scheduled task でない (Discord/DB log 非該当を明記) |

## 未確定点 (実装前に user 確認)
- **Q-1**: BP 名を一覧画面 (`_fetch_all_products` 行) にも出すか? 出すなら DB 列追加 (推奨=出さない=expander のみ、DB 列なし)
- **Q-2**: 案1 (新 BP + custom 送料 override 同時送出で送料維持) を将来欲しいか? 欲しければ実機 falsify を W138 内 or 別 W
- **Q-3**: 1 回の「📤eBay反映」で BP 変更と価格/送料変更を**同時**許可するか、BP 変更時は price/ship 別操作に制約するか
- **Q-4**: snapshot 拡張 (`shipping_profile_name` を GetItem から) 許可するか、Account API name lookup 1 経路統一か (推奨=統一)
- **Q-5**: BP selectbox 表示順 (API 返却順 / name 昇順 / 現行 settings.json マップ該当先頭)。全 active policy (件数実測、44 はハードコードしない) の絞り込み要否
- **Q-6** (内部レビュー MEDIUM 追加): 対象 listing に **Inline shipping (非 BP 管理) listing が混在**するか。混在時 GetItem に `<SellerProfiles>` が無く `shipping_profile_id=None` → 「現在 BP 取得不可」表示 + BP selectbox disabled で扱う設計でよいか (build step3 の policy 突合で BP 管理率を実測し判断)

## 完了判定 (Q1 DoD 11 ステップ)
| # | 検証 | 合格 |
|---|---|---|
| 1 | pytest unit | `test_ebay_account_policy.py` 全 PASS + `test_shipping_policy_selector.py` 等 regression PASS |
| 2 | 実機 Account API (read-only) | 全 active fulfillment policy 取得 (件数=step3 実測値、ハードコード比較しない)、fulfillmentPolicyId==GetItem ShippingProfileID 突合 1 件+ |
| 3 | Streamlit 再起動 | `run_monodeck.py` 経由でハングなし起動 |
| 4 | Playwright UI | expander 開→BP pill / selectbox に全 active policy が step3 実測件数で表示 / DDP buffer 警告キャプション 視認 |
| 5 | 実 BP 変更 (user 監視下) | テスト 1 listing→別 BP→GetItem で SellerShippingProfile/ShippingProfileID 新 BP 一致 (実値) |
| 6 | override reset 実機確認 | BP 変更後 GetItem 送料が新 BP default (eBay 仕様通り)、UI 併記 |
| 7 | DB SELECT | **BP のみ変更**した listing で `SELECT shipping_cost` が post snapshot の新 BP default と一致 (`_sync_db_to_actual` が bp_changed 単独でも発火=HIGH-3 担保。旧 custom 送料が DB に残らない) |
| 8 | 復元 (user 監視下) | 変更 listing を元 BP に戻し GetItem 確認 (原状回復) |
| 9 | code-reviewer | HIGH=0 ループ (Q4) |
| 10 | Codex review | 憶測箇所 (案1) 含め HIGH=0、2-stage |
| 11 | Q5 完了報告 4 行 | 使用モデル/検証経路/実機ログ/残リスク |

注: pytest PASS のみ完了宣言は K3 違反禁止。実 eBay GetItem verify (5,6,8) + DB SELECT (7)
必須。eBay write (BP 変更=売上直結) は W137 同様 **必ず user 監視下**実行+検証後原状回復。

## eBay 仕様の不確実箇所 (憶測明示)
- 【憶測】案1「別 BP へ変更 + 同一 Revise で ShippingServiceCostOverrideList 同梱」で
  override が新 BP に bind するか — 本プロジェクト未検証。実装前 Codex + sandbox/実機
  falsify 必須 (W137 priority 説を実機で覆した教訓)。
- 【一次情報確定】override は listing-level / 「service priority・option 不一致なら error」
  / Revise で shipping field 省略=削除 (eBay 公式 Trading API doc, 2026-05-17)。
- 【憶測】GetItem 応答に SellerShippingProfile の Name 子要素が安定して入るか — 未確認 →
  名前解決は Account API (name 返却確定) に一本化推奨。
- 【要再確認】eBay API は 2-4 週で改訂あり得る。実装時 sources doc 鮮度確認。

## Sources
- https://developer.ebay.com/devzone/xml/docs/Reference/ebay/types/ShippingServiceCostOverrideListType.html
- https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/SellerShippingProfileType.html
- https://developer.ebay.com/devzone/xml/docs/reference/ebay/ReviseFixedPriceItem.html
- https://developer.ebay.com/api-docs/sell/static/seller-accounts/business-policies.html
- https://developer.ebay.com/api-docs/sell/account/resources/fulfillment_policy/methods/getFulfillmentPolicies (Codex LOW-6: 中核 client の依存 endpoint)

> 【要再確認 / API version pin】eBay API は 2-4 週で改訂あり得る。実装時に
> Trading API version (現行 1453 系) と Account REST 版を pin + doc 鮮度確認
> (Codex LOW-6 / eBay 改訂規約)。

---

# W138-A 設計 addendum (2026-05-17、user 指示 A): BP を DB 列化し価格/送料と同一構造へ

## 経緯 / Q-1 矛盾アノテーション (contradiction-annotation)

**現状の見解 (2026-05-17 A 以降、内部レビュー HIGH-1 反映で訂正)**: shipping BP
は `ebay_listings` の **DB 列** (`shipping_profile_id` + `shipping_profile_fetched_at`)
として保持し、表示は DB 列から即時 (per-listing GetItem 不要・ボタン不要・
価格と同じ「最初から表示」)。**ただし価格と「完全同一の鮮度」ではない** — 後述
の鮮度非対称性 (HIGH-1) を `shipping_profile_fetched_at` の併記で正直に開示し、
📤eBay反映 時の `_sync_db_to_actual` 自動同期 + per-listing `↻ 再取得` で補う。

**過去の見解 (〜2026-05-17 A)**: Q-1 で「BP を DB 列化しない (W137 の
DB↔eBay 乖離再生産防止)、表示は都度 snapshot」と決定。実装は is_open
トリガ→(バグ修正で)明示ボタン lazy 取得。**さらに addendum 初稿 (〜内部
レビュー前) は「BP DB列 = 価格/送料と完全同一構造・乖離しない」と記載した
が、これは過大主張だった (下記)。**

**矛盾点 / 変更理由**:
- 契機: user 指摘「なぜ価格のように最初から表示しないのか」→ DB 列化 (A) 採用。
  さらに内部レビュー HIGH-1 が初稿の「価格と完全同一鮮度」を反証。
- 何が違うか (UX): ボタン式は劣化 UX。DB 列化で「最初から表示・per-listing
  API ゼロ」を実現する (user の要望どおり)。
- **何が違うか (鮮度、HIGH-1 の核心 = 初稿の誤りの訂正)**: 価格 (`current_price`)
  の鮮度は **2 系統**で保たれる — ① 定期 `task_ebay_sync`
  (`monitor/ebay_sync.py::sync_listings_from_ebay`、scheduler、GetMyeBaySelling)
  ② 📤eBay反映 時 `_sync_db_to_actual`。**GetMyeBaySelling は SellerProfiles /
  ShippingProfileID を返さない** (item_id/title/sku/quantity/price のみ。
  price は返るが SellerProfiles/ShippingProfileID は不在 = Codex#6 訂正) ため、BP は
  ① に相乗りできず ② のみ。よって「BP DB列 = 価格と完全同一鮮度・乖離しない」は
  **技術的に偽**。eBay.com 側で直接 BP を変更すると、その listing は次の
  `↻ 再取得` か 📤eBay反映 まで DB が stale。
- 何が同じか: 「実 eBay = 真実源」原則 (W137) は不変。DB 列は実 eBay に追従する
  **キャッシュ**であり権威ではない。stale 値を「真実」と偽らないため
  `shipping_profile_fetched_at` (最終取得時刻) を必ず併記する (Q0 透明性)。

**Q-7 (user 確定 2026-05-17)**: BP 変更運用 = 「eBay.com 直接変更も混じる」。
→ stale リスク実在を受理し、**option 2 = DB列自動表示 + 最終取得時刻併記 +
per-listing `↻ 再取得` + 📤反映時自動同期** を採用。「per-listing GetItem ゼロ」と
「常に最新」の両取りは不可、を正直に開示し staleness 窓を明示する (両取り主張禁止)。

## 変更内容 (価格パターン踏襲 + 鮮度非対称性の正直開示 + W135 backfill)

| 区分 | 対象 | 内容 |
|---|---|---|
| 修正 | `monitor/database.py` | **migration v41** (現行最新 v40 の次、実コード確認済): 既存 v26-v40 慣習に整合させ **`if user_version < 41:` ガード内**で `ALTER TABLE ebay_listings ADD COLUMN shipping_profile_id TEXT` + `ALTER TABLE ebay_listings ADD COLUMN shipping_profile_fetched_at TEXT` (最終取得 UTC、HIGH-1/HIGH-2 解決の要) を実行 → 成功後 `PRAGMA user_version = 41` (Codex#4)。name は持たず Account API cached lookup=Q-4 一本化維持。**Q2 冪等**: 各 ALTER を個別 `try/except sqlite3.OperationalError`、init_db に DROP/DELETE 書かない、番号昇順 v41 |
| 修正 | `tabs/tab_product_management.py` `_sync_db_to_actual` | post snapshot の `shipping_profile_id` + `shipping_profile_fetched_at=utcnow` を DB 同期に追加。**Codex#3 (None-skip 慣習の例外)**: 既存 `_sync_db_to_actual` は `if snap.X is not None` で None を skip するが、`shipping_profile_id` は **GetItem 成功時に限り None も明示 NULL 書込** (skip しない) + `fetched_at` と**同一 UPDATE で原子的に**書く。さもないと確定 Inline (id=None) 時に旧 BP id が残存し HIGH-2 の (b)/(c) 判定が崩れる。GetItem 失敗時は両列とも touch しない (fetched_at 据置=状態(a))。📤eBay反映 後に実 eBay BP が DB へ追従 (価格の②系統に相当、①系統は BP に無いため鮮度は価格に劣る=HIGH-1) |
| 修正 | `_fetch_all_products` SELECT (database.py 由来の一覧クエリ) | `shipping_profile_id`, `shipping_profile_fetched_at` を SELECT に追加 → 一覧 dict に乗る (current_price と同じ流路) |
| 修正 | `tab_product_management.py` `_render_one_product` / `_render_hero_metrics` / `_render_left_basic_and_physical` | **ボタン (`pm_bpbtn_`/`pm_bpshow_`) と per-render `_get_current_bp` を廃止**。BP id = `p["shipping_profile_id"]` (DB、無料)、name = 既存 `_cached_shipping_policies()` (Account API、ttl=300、ページ1回 cached) で解決。**hero 🚚 pill に最終取得時刻を併記** (例: `🚚 DDP_0.5-1kg (取得 5/16 21:30 JST)`。`shipping_profile_fetched_at` は UTC 保存だが UI 表示は **JST 変換** = `sqlite-timezone.md` 準拠 `DATE/strftime(col,'+9 hours')`、stale 時刻の user 誤認防止)、stale 疑い時の `↻ 再取得` ボタンを 1 個併設 (per-listing GetItem 1 回・opt-in、毎 render でない) |
| 修正 | HIGH-2: NULL 多義性の解消 | DB 状態を `shipping_profile_fetched_at` で 3 分岐: **(a) fetched_at IS NULL** = 未取得 (backfill未/GetItem失敗) → selectbox 非表示 +「BP 未取得 — ↻ で取得」(Inline と**断定しない**)。**(b) fetched_at NOT NULL かつ shipping_profile_id NULL/''** = 取得済 SellerProfiles 不在 = **確定 Inline** →「Inline (BP なし)」。**(c) fetched_at NOT NULL かつ id あり** = BP 値表示 + selectbox。silent degradation 防止 |
| 削除 | `_get_current_bp` (per-render GetItem) | 廃止。ただし `↻ 再取得` は単一 listing 1 回 GetItem で `shipping_profile_id`+`fetched_at` を更新する**置換関数**として残す (毎 render でない・opt-in)。**`↻` GetItem 失敗時は backfill と同一原則** = `fetched_at` を据置 (成功時刻で上書きしない) + UI に「再取得失敗」痕跡表示 (Q0 silent skip 防止、単発 UI 操作でも担保) |
| 新規 | `scripts/backfill_shipping_profile_w138a_*.py` | W135 方式 one-shot: **全 active listing (~580、有/無在庫問わず、108 ではない)** を GetItem (読取) し `shipping_profile_id`+`fetched_at` を一括投入。HIGH-3 詳細仕様↓ |
| 新規 | `tests/test_w138a_*` | migration 冪等 (init_db 2回データ保持) / `_sync_db_to_actual` が 2 列同期 / 3 分岐 (HIGH-2) ロジック / hero・selectbox が DB列駆動 / `↻` が単一 GetItem / backfill 冪等 |
| 修正 | `tests/test_w68_step1_init_db_drift.py` | canonical HEAD v40→**v41** に伴う user_version 表明更新 (本テストの不変条件は「HEAD 追従」と明記済、migration 必然 cascade) |
| 修正 (番人) | `tests/test_w134_cache_invalidation.py::test_cache_ttl_is_short` | **安全 guard の scope 是正** (要 reviewer/Codex 注視)。番人の真の目的 = 「bump 漏れで *DB-backed per-listing 金銭/在庫/ランク* が stale 表示される退行阻止」。`_cached_shipping_policies` は Account API の **BP 一覧カタログ** (非 DB-backed・bump と無関係・per-listing 金銭データでない・月単位変化) で W138 設計上 ttl=300 が正当 (5s 化 = Account API 連打で設計破壊)。全 ttl 一律 ≤5 の過大 scan を、**根拠明記必須の `_LONG_TTL_ALLOWED` allowlist** 方式へ。`_cd_fetch_all_products`(ttl=3) 等 DB-backed 金銭 reader の厳格 ≤5s は不変・退行検出能力維持 |
| cascade | (外部対象なし) | **判定 2026-05-17 (実装確定後、R-12 再検証)**: Q-1 撤回 (BP DB 列化) の矛盾アノテーションは本設計書内に自己完結。`reference_shipping_tariff_logic.md §5` は revise XML override 機構 (W136 真因) の権威で、W138-A は `revise_shipping_profile`/`_build_revise_*` を改修せず (UI 表示/変更層のみ) → **unrelated、K2 で触らない**。`source_ebay_*` も API 挙動不変ゆえ unrelated。外部 cascade 対象なしと確定 (cascade-update「unrelated→触らない」/ Codex#7 の pending は本判定で解消) |

### HIGH-3: backfill 詳細仕様 (db-migration-rules 6-step、~580 active)

- 対象 = **全 active listing** (`ebay_listings` の active、有在庫+無在庫、W135 の在庫108 とは別母数 ~580)。SELECT 抽出も最終 `UPDATE` 句も両方 `WHERE ... AND shipping_profile_fetched_at IS NULL` (**write-time guard**、Codex#2): SELECT→GetItem→UPDATE の間に user が `↻`/📤反映 で同 listing を先に更新した場合、遅い backfill の上書きを防ぐ (TOCTOU 窓封鎖)。冪等ガード兼 resume (再実行で取得済 skip)
- batch / rate-limit: 50 件/バッチ、GetItem 間 sleep (eBay API rate-limit 準拠)、バッチ毎に進捗 stdout + 中断耐性 (再実行で残のみ)
- **GetItem 失敗 listing**: `fetched_at` を **NULL のまま据え置く** (= 状態(a)「未取得」、Inline と誤断定しない=HIGH-2 整合)。失敗 item_id を log
- db-migration-rules 6-step: ① SELECT 全対象 dump → `data/w138a_backfill/snapshot_<ts>.json` (rollback 用) ② `--dry-run` で対象件数・GetItem 成功率を先行確認 (1 件試行含む) ③ `--apply` で残実行 ④ DB SELECT で投入率・(a)/(b)/(c) 分布を再確認 ⑤ 24h retrospective code-reviewer (本 rule + db-migration context) ⑥ HIGH 指摘あれば補正/rollback
- eBay write **ゼロ** (GetItem 読取専用)、code-reviewer HIGH=0 後に dry-run→apply
- **Codex#5 (scheduler 協調)**: backfill apply 中は `tasks_enabled.ebay_sync=false` で定時 `task_ebay_sync` を一時停止 (db-migration-rules kill switch 併用、apply 後 re-enable)。task_ebay_sync は `current_price/shipping_cost/quantity/title` のみ upsert し本機能の 2 列を touch しない (実コード確認済 = 値破壊リスク無) が、SQLite WAL writer ロック競合 (`database is locked`) 回避のため停止 + backfill 側に locked retry も実装

### Codex#1 解決 (金銭直結 HIGH): selectbox 初期値 と bp_changed 判定の整合

**問題**: selectbox 初期値を stale 可能性ありの DB 列にし、bp_changed を pre-snapshot 基準のままにすると、eBay.com 外部 BP 変更 (A→B) で DB stale A → user が **selectbox 無操作**で価格だけ変えて 📤反映 → Streamlit form は default の stale A を submit → `bp_changed=(A≠pre-snapshot B)=True` → **MonoDeck が実 eBay の B を stale A に巻き戻す** = DDP buffer 喪失 (Section 232 数百ドル/件)。本機能が防ぐべき失敗そのもの。

**解決 = dirty-flag 方式 (Streamlit widget default ≠ user 意図)**:
- `bp_changed = (user が selectbox を実際に操作した) AND (操作後の選択値 != pre-snapshot 実 eBay 値)`
- **user 操作判定**: submit 時の selectbox 値 ≠ **render 時に selectbox を初期化した値** (= DB 列値) なら「操作あり=BP 変更意図」。等しければ「無操作=BP 意図なし」。
- **無操作時**: `bp_changed=False`。BP は一切 touch しない (stale DB 値を eBay へ送らない)。DB は post-snapshot から `_sync_db_to_actual` で実 eBay 値へ resync されるので結果整合。
- **操作時**: 選択値を pre-snapshot 実 eBay と比較し、異なれば revise (W137 真実源・pre-snapshot 基準を維持)。
- DB 列 `p["shipping_profile_id"]` の用途は **selectbox 初期 index + hero 表示 + 上記「操作判定の基準値」** に限定。bp_changed の**比較相手は依然 pre-snapshot 実 eBay** (DB 基準にしない=LOW 維持)。
- **残留 (安全方向のみ、正直開示)**: DB stale 表示中に user がその stale 表示値を**明示的に再選択**し、かつ実 eBay が異なる場合 → 無操作扱いで適用されない。ただし失敗方向は「stale を eBay に書かない」= **安全側 no-op** であり、危険な「stale を実 eBay へ巻き戻す」経路は消滅。UX ガイド = 「BP を確実に変えたい時は先に `↻ 再取得` で最新化してから選択」+ hero の最終取得時刻併記で stale を明示済。

## 不変 (W138 既存実装、副作用ゼロ)
- `revise_shipping_profile` / `_build_revise_bp_only_xml` (W136 非改修・3ID必須・Warning-fatal降格)
- `_apply_to_ebay` の **bp_changed 比較相手は pre-snapshot 実 eBay を維持** (LOW 反映、W137 真実源)。ただし上記 Codex#1 解決により **「user が selectbox を操作したか」(dirty-flag) を前段ガードに追加** — 無操作なら pre-snapshot と無関係に bp_changed=False (stale 巻き戻し経路を遮断)
- Q-3 同時拒否・Phase4 post snapshot 実値 verify (bp_ok)・fake success 排除
- selectbox の form 内配置・index/format_func 重複名安全・DDP buffer 喪失警告

## ビルドシーケンス
1. `database.py` migration v41 (2 列・各冪等 ALTER) + 冪等性 pytest (init_db 2回データ保持)
2. `_sync_db_to_actual` に 2 列 (id+fetched_at) 同期追加 + pytest
3. `_fetch_all_products` SELECT 追加 → hero/selectbox を DB列駆動化 (HIGH-2 3分岐)、最終取得時刻併記、`↻ 再取得` 併設、ボタン(表示用)/`_get_current_bp`(per-render) 廃止 + pytest
4. `scripts/backfill_shipping_profile_w138a_*.py` (HIGH-3 仕様、6-step) + code-reviewer HIGH=0 → dry-run→apply
5. code-reviewer HIGH=0 ループ + Codex 2段 (W136 非回帰・migration 冪等・bp_changed が pre-snapshot 基準・GetItem ゼロ(表示時)・鮮度非対称の正直開示)
6. Streamlit 再起動 (user MonoDeck 停止調整) + Playwright (expander 通常クリックで 🚚 pill+最終取得時刻+selectbox が**ボタン無し即表示**・`↻` 動作確認) + 実 BP 変更 (user 監視下、原状回復) + **eBay.com 直接変更→`↻`で DB 追従** を 1 回実証

## リスク表
| # | リスク | 緩和 |
|---|---|---|
| RA1 | migration で既存データ破壊 | Q2: ADD COLUMN ×2 のみ (DROP/DELETE/RENAME なし)、各 try/except OperationalError、init_db 2回冪等 pytest、v41 番号昇順 |
| RA1' | **Codex MED-3 残存リスク (要判断→現状維持)**: v41 の broad `except sqlite3.OperationalError` は ALTER 中の transient lock 等も握り潰し user_version=41 を立て得る (列欠落で SELECT が no such column)。**判断**: `ebay_listings` は v41 前に無条件 CREATE 済で no such table 不発生 + 本 except 形は Q2/db-migration-rules/quality-gate hook が **明示規定する標準** で v26-v40 と byte 一致 (v41 のみ厳格化 = K2 一貫性違反)。init_db は起動時・並行 writer 前で lock 発生確率低。`test_w138a_migration` 冪等テスト (init_db 2回・version 強制巻戻し再突入で 2 列存在 verify) が実質ガード。→ **現状維持 + 本残存リスク明記で受容** |
| RA2 | **DB↔eBay BP 乖離 (HIGH-1、Q-1 懸念の実在化)** | 両取り不可を正直開示: ①`shipping_profile_fetched_at` を hero に**常時併記** (stale を真実と偽らない=Q0) ②📤eBay反映 で `_sync_db_to_actual` 自動同期 (価格②系統相当) ③per-listing `↻ 再取得` で能動更新 ④初回 backfill。**残留 staleness 窓**: eBay.com 直接変更〜次の ↻/📤反映 (user 受理済 Q-7) |
| RA3 | backfill 中の誤値/中断 | HIGH-3 仕様: GetItem 読取のみ・`WHERE fetched_at IS NULL` 冪等(resume)・batch/rate-limit・SELECT snapshot・dry-run 先行・失敗は fetched_at NULL 据置 (Inline 誤断定回避)・eBay write ゼロ |
| RA4 | HIGH-2 NULL 多義 silent degradation | fetched_at で (a)未取得/(b)確定Inline/(c)BPあり を厳密 3 分岐。(a) を Inline と断定しない |
| RA5 | Account API 取得失敗時に name 解決不可 | `_cached_shipping_policies` ok=False → name は profile_id 生表示 + selectbox「BP 一覧取得失敗」明示 (Q0、現 W138 挙動踏襲) |
| RA6 | W136 override 経路への副作用 | W138-A は表示/同期/backfill のみ。revise_shipping_profile/_build_revise_* 不改修 (Codex 再確認) |
| RA7 | quality-gate hook | ALTER は try/except OperationalError ラップ、init_db に DROP/DELETE 書かない |
| RA8 | **stale DB 初期値が実 eBay BP を巻き戻す (Codex#1、金銭直結)** | dirty-flag: selectbox 無操作 (submit値==render初期値=DB列) なら bp_changed=False で BP 一切 touch しない。stale 値が eBay へ送られる経路を構造的に遮断。残留は安全側 no-op のみ (上記「Codex#1 解決」節) |

## DoD (Q1 11 ステップ準拠、pytest 単独完了宣言禁止=K3)
- pytest: migration v41 冪等 (init_db 2回データ保持・user_version gate) / _sync_db_to_actual が id+fetched_at 同期・id=None も明示NULL書込 (Codex#3) / HIGH-2 3分岐 / **Codex#1 dirty-flag: stale DB初期値で selectbox 無操作時 bp_changed=False (実 eBay 巻き戻し無)・操作時のみ pre-snapshot 比較で revise** / backfill UPDATE write-time guard (Codex#2) / `↻` 単一 GetItem / hero・selectbox DB列駆動 / 既存 W136/W137/W138 回帰全 PASS
- 実機 read-only: backfill dry-run で対象 ~580 件・GetItem 成功率
- backfill apply 後 DB SELECT: (a)/(b)/(c) 分布・投入率、W135 同様 snapshot/冪等/24h retrospective
- Streamlit 再起動 (run_monodeck.py) + Playwright: expander 通常クリックで **ボタン無し**に 🚚 pill + **最終取得時刻** + selectbox 即表示、`↻ 再取得` クリックで該当 listing のみ更新
- 実 BP 変更 (user 監視下): ① MonoDeck 経由 別BP→📤反映→GetItem 実値一致→`_sync_db_to_actual` で DB 2列追従を SELECT 確認→原状回復 ② **eBay.com 直接変更→MonoDeck `↻`→DB 追従** を 1 回実証 (HIGH-1 緩和の実機確認) ③ **Codex#1 巻き戻し防止実証**: eBay.com で BP を外部変更 (DB stale 化) → MonoDeck で selectbox 無操作のまま価格のみ変更→📤反映 → GetItem で **実 eBay BP が巻き戻っていない** (外部変更値のまま) ことを確認 (金銭直結の最重要 verify)
- code-reviewer HIGH=0 + Codex 2段 (W136 非回帰・bp_changed pre-snapshot 基準必須)
- Q5 4 行報告

## クローズ記録 (2026-05-17、Q0 透明性 = DoD ステップを黙って落とさない)

**W138 (id=222) = 完了 / completed 2026-05-17。** 実施・検証エビデンス:

| DoD ステップ | 状態 | エビデンス |
|---|---|---|
| pytest (全項目) | ✅ | full **1196 passed / 2 skipped**。新規 `test_w138a_migration`(3)・`test_w138a_bp_db_driven`(13: HIGH-2 3分岐/Codex#1 dirty-flag/Codex#3 None明示NULL/Codex#2 guard/JST/source-contract 番人) + W136/W137/W68/W134 回帰 |
| 実機 read-only dry-run | ✅ | 対象 **421** (設計概算 ~580 の実母数)・GetItem 成功率 **100%** (BP420/Inline1/err0) |
| backfill apply + DB SELECT | ✅ | apply 421件 (BP418/Inline3/err0)。DB 3-way: fetched 421/421・未取得残0・(c)418・(b)3。snapshot 取得・冪等 guard・kill-switch 運用 |
| Streamlit 再起動 + Playwright | ✅ | run_monodeck.py 経由 PID45264 健全。実 listing で 🚚 BP名+取得時刻(JST) **ボタン無し自動表示**・selectbox 即表示・旧ボタン廃止・**↻ E2E (14:59→15:24 JST 更新)**・console 0 errors |
| code-reviewer HIGH=0 + Codex 2段 | ✅ | 設計: 内部HIGH=0→Codex#1→解決→再HIGH=0。実装: 内部HIGH=0→Codex Finding1(金銭直結)→修正→再HIGH=0。計 内部review×3 + Codex 2段×2 |
| 24h retrospective (db-migration) | ✅ | backfill apply 遡及 review **HIGH=0・補正不要・rollback 不要・6-step 充足** |
| **実 BP 変更 (live eBay 書込、③含む)** | **⚠️ user 判断で意図的に省略** | **2026-05-17 user 決定**: write 経路 (`revise_shipping_profile`/dirty-flag) は pytest (`test_codex1_*`/`test_w137` BP系/source-contract 番人) + 内部HIGH=0 + Codex 2段 で論理担保済 → live eBay mutation を**省略してクローズ**。設計上は「user 監視下」必須としていたが、上記カバレッジを以て user が waive (Q0: 黙って落とさず本記録に明示)。残リスク = live revise 経路の実機未踏 (pytest+2段review で代替担保、契機: W133 item2 無監視 real-eBay 書込事故回避とのトレードオフを user が判断) |
| Q5 4 行報告 | ✅ | 実施済 |

cascade: 外部対象なし確定 (上表 cascade 行)。MonoDeck は W138-A コードで稼働継続 (PID 45264)。
