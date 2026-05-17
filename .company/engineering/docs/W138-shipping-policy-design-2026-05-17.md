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
