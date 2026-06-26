# eBaymag 各国版 dispatch を US本体にミラーするシステム — 設計書 (2026-06-26)

## 0. 背景と一次情報 (実機確定 2026-06-26)

W284 で eBaymag 各国版送料ポリシー(MAG_*)を作成・割当済。今回 user 依頼=「各国版の発送日数(1day/7day)を US本体に合わせる」+「US を直したら eBaymag に反映する仕組み」。

### Phase 1 調査で確定した事実 (CDP Chrome GraphQL + eBay GetItem、read-only)

1. **US本体 dispatch は均一に1dayではない**: MAG商品206件中 US DispatchTimeMax=1day が59件 / 7day が147件。
2. **dispatch は在庫種別と強相関** (業務ルール、出典: `scripts/_probe_dispatch_vs_stock.py` 出力):
   | 在庫種別(SKU prefix) | 1day | 7day | 計 |
   |---|---|---|---|
   | 有在庫 `stock*` | 52 | 12 | 64 |
   | 無在庫 `ebay*` | 1 | 88 | 89 |
   | SKU無し/その他※ | 6 | 47 | 53 |
   | **計** | **59** | **147** | **206** |
   ※ SKU無し(48)=US GetItem が item-level SKU を返さない listing (multi-variation 等、SKU は variation 単位)。「その他」(5)=SKU が URL 断片等の異常値。**この53件も drift 評価対象** (drift=5件 は全206件から算出、`scripts/_probe_mismatch_and_1day_values.py`)。
   - → **「全部1day」は無在庫88件で物理破綻**。user 決定 = **真実の源 = US本体をミラー** (各商品の US DispatchTimeMax に追従、US例外設定は尊重)。
3. **eBaymag 割当は既に US をほぼミラー済** (201/206=97.6% 一致)。**ズレは5件のみ**:
   | product | 現割当 | US実dispatch | 正しい割当 | 備考 |
   |---|---|---|---|---|
   | 734008602 Sumitomo JR-6 | MAG_4-5kg_1day | 7day | MAG_4-5kg_7day | 7day twinへ |
   | 729566399 Pioneer DVL-919 | MAG_5-6kg_7day | 1day | MAG_5-6kg_1day | 無在庫なのにUS=1day(US側要確認だが指示通りミラー) |
   | 718746868 Pixel Dock | MAG_0.5-1kg_7day | 1day | MAG_0.5-1kg_1day | light帯=値完備 |
   | 718746698 Leica X310 | MAG_0-0.5kg_7day | 1day | MAG_0-0.5kg_1day | light帯=値完備 |
   | 718746695 BMW Dash Cam | MAG_0.5-1kg_7day | 1day | MAG_0.5-1kg_1day | light帯=値完備 |
4. **重帯 MAG各国送料の$0漏れ** (W284積み残し): 3-4kg〜10-20kg の MAG各国送料が未設定/$0 多数。例 10-20kg=UK/DEのみ・8-10kg_1day=全空。重い品ほど送料高($90〜139)なので金銭漏れ大。
5. **重帯 1day twins の各国 site profile が未生成** (eBaymag非同期): MAG_4-5kg_1day/8-10kg_1day/10-20kg_1day=`shippingEbayProfiles=[]`、5-6kg_1day=DEのみ。`set_values` は既存 site profile しか触れない。
6. 「107 NO STOCK」等 旧ポリシー on DE = eBaymag上 n=0 の幽霊。eBaymag→eBay push が DE に届かず eBay側に残存した**層3(反映停滞)の症状**。eBaymag割当の誤りではない。

### 反映の3層 (制御可能性の境界)
1. **US eBay listing** (真実の源、user が手動編集) ← mirror の入力
2. **eBaymag MAGプロファイル/割当** (GraphQL/REST で制御可) ← **本システムが触れる層**
3. **eBay各国版 listing** (買い手が見る) ← eBaymag の停滞 sync 次第、**制御不可・反映保証しない**

## 1. ゴールと非ゴール (K3 measurable)

**ゴール**:
- G1. eBaymag各国版 dispatch = 各商品の US DispatchTimeMax (1day/7day) になる。現状ズレ5件→0件。
- G2. US dispatch を user が変えたら eBaymag MAG割当を追従させる仕組み (on-demand 再実行で冪等収束)。
- G3. 重帯 MAG各国送料の$0漏れを canonical 正値で埋める (生成済 site profile の範囲で)。

**非ゴール** (K1/層分離):
- eBay各国版 listing への反映保証 (層3=eBaymag停滞依存、保証しない)。
- US listing 自体の編集 (真実の源、user 手動)。
- 重量帯の再判定 (商品は既に正帯MAGに居る=系列flipのみ)。
- 未生成 site profile の値設定 (eBaymag生成待ち=touchしない、skip+報告)。

## 2. 設計

### 2.1 中核: 系列flip = twin付替 (重量帯不変)

商品は既に正しい重量帯の MAG_<band>_<series> に居る (W284)。US dispatch に合わせるには**同帯の twin (series違い) へ付け替えるだけ**。値は band で決まる=flip で送料は変わらない。

twin map (タイトル parse で動的生成、ハードコード禁止):
```
MAG_<band>_7day  <-->  MAG_<band>_1day   (10帯すべてに両 twin 存在を確認済)
```

### 2.2 新モジュール `monitor/ebaymag_dispatch_mirror.py`

純関数 + オーケストレータ。既存 `ebaymag_graphql` / `ebaymag_assign` を再利用。

- `build_twin_index(profs) -> dict`: policy list から `{policy_id: {band, series, twin_id}}` を生成。MAG_ 以外は無視。
  - **series suffix は `{1day,7day}` 完全一致のみ受理** (それ以外サフィックスの MAG は twin 構築不能として明示エラー、Codex M1)。
  - 両 twin 欠落帯は明示エラー (Q0)。
- `assert_dispatch_axis(page, twin_index)`: **各 MAG の `profile.dispatchTime` 実値が series ラベルと整合するか検証** (Codex H1: dispatch は title ラベルでなく `dispatchTime` フィールドに宿る。`_1day`→dispatchTime==1, `_7day`→==7。不整合は明示エラー)。事前確認済 = 1day twins は dispatchTime=1 だが、起動時に全 MAG を機械検証して silent 未達を防ぐ。
- `us_dispatch_series(item_id) -> "1day"|"7day"|None`: GetItem DispatchTimeMax。`==1 -> 1day`, `==7 -> 7day`。**それ以外の値 (2,3...) は None を返し alert** (黙って誤分類しない、Q0)。
- `sku_expected_series(sku) -> "1day"|"7day"|None`: 業務ルール (`stock*`→1day, `ebay*`→7day)。**ミラーの判定には使わない (真実源は US)**。下記 audit 専用。
- `plan_mirror(page) -> (moves, holds, sku_conflicts)`: 全 MAG商品について US dispatch を読み、現割当 series ≠ US series ならば `Move(product_id, from_policy, to_twin, us_series)` を生成。US本体listingが無い/取得不可は skip+記録。
  - **SKU矛盾監査 (Fugu MED-6 / R1自動化)**: `sku_expected_series(sku) != us_series` の商品を `sku_conflicts` に列挙 (例: 無在庫なのに US=1day な Pioneer)。**ミラー自体は US 通り実行**し、矛盾は user 通知のみ (US手編集の誤りを黙って各国へ伝播させない)。
- `apply_moves(page, moves, dry_run) -> report`: dry_run 既定。apply時は各 Move を `ebaymag_assign.assign_product` (REST PUT + read-back + assert_no_vanish) で実行。1件失敗で即停止 (money-direct)。

**漏れ防止ガード (money-direct、設計の肝。送料軸と dispatch軸は独立 = Codex H2)**:
- 付替先 twin の生成済 site profile (US以外) の各国送料が **`build_canonical_policy(band)` の期待値と一致するか read-back 照合** (Fugu H2: 「None/0」だけでなく「正値だが canonical 不一致/過少」も leak。期待値一致を要件化)。
  - 1サイトでも未生成・不一致・$0 があれば = **値未完備** → 既定で **その商品を移動保留 (hold)**、理由を記録 (leak回避)。
  - 一括 `--force` は持たない。単品 `--force-product <product_id>` のみ (下記 §2.3)。
  - → T2 (値埋め) を先に流して twin を canonical 完備させてから T3、の順序を強制する自然な仕組み。

### 2.3 実行スクリプト `scripts/mirror_us_dispatch_to_ebaymag.py`

- 既定 dry-run: plan を表示 (移動N件 / 各 twin / 値完備可否 / **保留N件＋理由** / **SKU矛盾N件**)。
- `--apply`: 実行 (値完備 twin への移動のみ)。`/shipping` で CSRF 更新 (/products goto 禁止)。
- `--force-product <id>`: **単品限定**で値未完備 twin への移動を許可 (break-glass)。誤爆範囲を1件に閉じる。force時は対象商品＋露出する$0/不一致サイトを**実行前に列挙しログ＋Discord通知** (Q0痕跡、Codex H3/Fugu H3)。bulk force は実装しない。
- これが **T1 (ズレ修正) の実体** でもある (システムを流す=値完備分は収束、未完備分は保留残置)。

### 2.4 T2 重帯値埋め = 既存 `scripts/_set_values_batch.py` を流す

- 既存実装が canonical (`build_canonical_policy`) で各MAGの生成済サイトに値設定・冪等・read-back。
- **未生成サイトは skip** (eBaymag生成待ち=層3制約)。skip を**正直に報告** (Q0、「全部直した」と偽らない)。
- 本番投入後に site profile が増えたら再実行で追加収束。

### 2.5 自動化 (G2) の運用形態

- **第一形態 = on-demand script** (user が US を直した後に実行、冪等収束)。K1: まずこれ。
- 第二形態 (任意・将来) = 日次 scheduled task で mirror dry-run→drift>0 で Discord 通知 (適用は手動承認)。**money-direct自動適用は当面しない** (通知のみ)。本設計では module を scheduled 化可能な純関数構成にしておくに留める。

## 3. 実装順序 (漏れ防止の依存)

1. module + script 実装 (dry-run で plan 検証 + `assert_dispatch_axis` で全MAG dispatchTime 整合確認)。
2. **T2: `_set_values_batch.py` 実行** → 生成済サイトの$0/不一致を canonical 正値で修正 + skip報告 (未生成サイトは正直に列挙)。
3. **T1/T3: mirror --apply** → 値完備 twin への移動を収束。値未完備 twin への移動は保留 (T2で完備した分のみ動く)。
4. Q1 検証:
   - **層2(eBaymag、必須)**: 移動後に各商品 shippingProfileId read-back + 移動後 twin の dispatchTime が US と一致を確認 (G1 の goal verify、Codex H1)。`assign_product` 内蔵 read-back で担保。
   - **冪等性 (Codex M2)**: apply 直後に再 `plan_mirror` → drift=0 を確認 (db-migration の冪等テスト思想)。
   - **層3(eBay各国版)**: GetItem で反映確認したいが **各国版は user≠seller で error17** (正本確定) + eBaymag同期停滞 → **best-effort、未反映は明記** (層3保証しない)。US本体(siteId=0、user=seller)は GetItem 可。

## 4. リスク / 残課題

- R1. Pioneer(729566399) は無在庫なのに US=1day。指示通りミラーするが US側設定が誤りの可能性 → 移動前に user に1点確認 or US側修正を促す。
- R2. 重帯 1day twins の site profile 未生成 → 値設定不能。eBaymag生成を待つしかない (層3)。dry-run で「保留N件」を明示。
- R3. eBaymag→eBay 反映停滞 (層3) は本システムの管轄外。G1-G3 は全て層2 (eBaymag) での収束を意味し、買い手画面(層3)反映は保証しない=設計で明記済。
- R4. `us_dispatch_series` が {1,7} 以外を返す商品 → alert + skip (要 user 判断)。

## 5. レビュー観点 (Codex / Fugu へ)

- 系列flip=twin付替で値不変、という前提は正しいか (band で値が決まる設計依存)。
- 漏れ防止ガード (値未完備 twin への移動保留) の十分性。`--force` の危険性。
- 各国送料は **zone別DHL実費 (canonical、W283以降)**。reference_shipping_tariff_logic v1.0 の差分式は US本体listing側の話で、eBaymag各国版 canonical とは別レイヤ (Codex C1 訂正)。dispatch とは独立軸。
- 「真実の源=US」を mirror する設計が、無在庫=7day 業務ルールと矛盾しないか (矛盾は plan_mirror の SKU監査で warning 列挙、ミラーは US 通り)。
- Q0 (skip/偽成功)・money-direct ガードの抜け。

## 6. レビュー反映ログ (2026-06-26 Codex + Fugu 2段)
- Codex HIGH: H1/H2 dispatch軸独立(dispatchTime検証追加) / H3 --force単品化。MED: M1 series厳密parse / M2 冪等検証。cascade: C1 差分式→zone実費訂正。
- Fugu HIGH: H2 cost floor→canonical一致照合 / H3 --force単品化。MED: M4 「収束」表現緩和+保留明示 / M6 SKU矛盾監査追加。LOW: L7 snapshot出典。
- 却下: Fugu H1(件数矛盾)=誤読(53件行見落とし、実際206整合) → 総計行明記で対応。Fugu MED5(層3検証必須化)=各国版error17で技術的に best-effort 据置。circular truth / 休日ロジック = 存在しない/K1違反で却下。
