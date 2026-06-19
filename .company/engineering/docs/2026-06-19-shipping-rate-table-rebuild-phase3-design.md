---
title: eBay SpeedPAK DDP Rate Table 全面再設計 — Phase 3 設計書
date: 2026-06-19
status: design (実装前・Codexレビュー対象)
owner: assistant (model claude-fable-5) / user 承認制
related_memory: project-shipping-rate-table-rebuild
---

# eBay SpeedPAK DDP Rate Table 全面再設計 — Phase 3 設計書

## 1. 目的
eBay の DDP 送料テーブル（手作りで金額バラバラ・カバレッジ欠落多数）を、**FedEx/DHL 実費の差額式**に一致させ、**全販売国対応**にし、**月一自動バッチ**で為替・燃料・料金表を取り込んで保守し続ける。本書は Phase 3（新テーブルの金額・行構成の確定）までを記す。Phase 4 以降（eBay 反映）は本書承認後。

## 2. スコープ (user 2026-06-19 承認)
- 対象 = **DDP 系 10 rate table**（id 5284239010/240/241/247/249/251/252/253/254/256010）。
- 紐づく fulfillment policy = **22 件**（クリーン命名 DDP 20 + 相乗り 2: Pre-order 2-3kg → 5284247010 / Flat$50 7business → 5284249010）。
- EXP 系(5271*)・rate table 無し(14 policy)・US_only 等は**対象外**。

## 3. 計算モデル (確定)
```
表示送料(USD) = max(0, ceil[ ( DHL実費 × (1+DHL燃料) − FedEx_US実費 × (1+FedEx燃料) ) / 為替 ])
```
- **DHL実費** = DHL SpeedPAK **非書類(荷物)** 料金、**帯上限重量**、その国の **DHLゾーン**(円)。
- **FedEx_US実費** = FedEx FICP **列E (USW)**、帯上限重量(円)。差額の基準＝US（US送料は商品価格に内包＝US無料運用）。
- **為替 = 157円/$**（前月平均158.16×(1−1%)≈157、月次バッチ恒久ルール）。
- **燃料(最新公示週 2026-06-22..28, 適用6/22〜, 実ブラウザ取得)**: DHL=**45.25%** / FedEx=**41.50%**。⚠️今日(6/19)の当週は 6/15-21 (FedEx 43.00%/DHL 47.00%) だが、テーブルが live になるのは Phase 6 以降=6/22 週以降のため**最新公示週**を採用。いずれにせよ週次変動し月次バッチで自動更新。
- 丸め = 切上げ・マイナスは 0 止め。代表重量＝帯上限（過小=赤字回避優先）。

## 4. データ源と検証 (全て一次情報)
| データ | 源 | 検証 |
|---|---|---|
| 販売可能国 (53国, 全policy共通) | phase0 snapshot shipToLocations | — |
| 地域→国 展開 | eBay GeteBayDetails ExcludeShippingLocationDetails (公式) | Codex HIGH=0 |
| $0漏れ国 (横断ユニーク52) | 上記 × 現rate table行 | Codex HIGH=0 |
| 国→DHLゾーン (218国) | DHL PDF p11 座標抽出 | 12アンカー合格 |
| DHL/FedEx 基本料金 | SpeedPAK PDF (DHL p12-13 / FedEx p13-18) | 7アンカー合格 + Codex HIGH=0 |
| 燃料率 | FedEx/DHL 公式 web (Playwright) | live 当週値 |

**重要な落とし穴 (修正済)**: DHL PDF p12 は 3層([Envelope][書類≤2kg][荷物=非書類0.5kg+])。eBay商品=**荷物**。書類料金(例 0.5kg Z1=2188)でなく荷物料金(2054)を使う。

## 5. 53販売国の DHLゾーン分布
Z1 韓台(2) / Z2 中華圏 CN·HK·MO(3) / Z3 東南ア ID·MY·PH·SG·TH(5) / Z4 India(1) / Z6 欧州主要(32) / Z7 東欧·コーカサス AL·AM·IS(3) / Z8 **Israel(1, 最高額)** / Z9 中央ア·中東·南西ア KZ·NP·OM·PK·QA(5) / Z11 豪(1)。CA/MX(Z5)・US(Z10) は DDP 出荷対象外。

## 6. 新送料テーブル (Codex HIGH=0 検証済)
USD / 157円 / DHL45.25% / FedEx41.50% / US基準=列E。各帯×ゾーン:

```
ゾーン            0-.5  .5-1  1-2  2-3  3-4  4-5  5-6  6-8  8-10 10-20
Z1 韓台            1     0    0    1    1    0    3   14   17   75
Z2 中華圏          2     0    1    1    1    0    3   14   17   75
Z3 東南ア          2     0    1    1    1    0    3   14   17   75
Z4 India          4     4    5    9   24   33   44   71   86  173
Z6 欧州(32国)      3     7    9   13   17   13   10    0    0  123
Z7 東欧 AL/AM/IS  16    11   19   35   49   60   71   90  103  149
Z8 Israel         34    32   33   37   43   45   84  159  227  244
Z9 中央ア/中東     34    35   40   44   48   48   88  169  243  318
Z11 豪             5     1    0    8    5    0   20   62   97   92
```
- 一部 $0 セル(Z6欧州6-10kg / Z1·2·3·AU 軽帯) = DHL が US実費より安く上乗せ不要（モデル正、user「実費どおり・キャップ無し」承認済）。
- 全数値・国別: `tools/ebay-manager/data/tmp/phase3_new_table.json` / 現行差分: `phase3_diff.json`。
- **行構成 (確定 = DHLゾーン単位 9 行)**: `phase3_rows_design.json` (§7 参照、Codex VERDICT A)。各帯 = この表の縦 1 列をゾーン 9 行に展開。⚠️ `phase3_rows_ebayUI_1-2kg.json` (eBay地域 13 行版) は **deprecated**。

## 7. 行構成 (DHLゾーン単位 = コスト源泉でグループ化) ← **2026-06-19 再訂正 (Codex VERDICT A)**

**確定方針**: 行のグループ化キーは「現在の金額」でも「eBay地域」でもなく **DHLゾーン**(=送料の最小コスト単位)。各テーブル = **DHLゾーン単位 9 行**(`phase3_rows_design.json`)。

**訂正経緯 (2 段階)**:
1. 当初「eBay地域 × 同額」で 13 行版を作成 (`phase3_rows_ebayUI_1-2kg.json`)。前提=「1 行は単一 eBay地域内のみ」と誤推定。
2. 2026-06-19 実機ピッカー確認 (user スクショ) で **1 行は全地域を横断して国を選べる**ことが判明 (ピッカー = 全地域統合ツリー、タイトル "Select the regions and states you can deliver to with expedited service" = 1 送料の適用国を地域横断で全選択)。
3. user 提起「今後の送料変動を考慮し分けるべきか」→ **Codex VERDICT A** = 行は **DHLゾーン単位**にすべき。理由: 送料 = DHLゾーン実費 × 燃料 − FedEx_US。**同一ゾーンの国は全帯・毎月 必ず同額**(永続不変)。今たまたま同額の別ゾーン (例 Z2 中華圏 $1 と Z3 東南ア $1) は来月の燃料/料金改定で **乖離** = 金額統合すると **行分割 (構造変更=UI 手作業) が発生**。月次バッチは `updateShippingCost` (金額のみ) しか自動化できないため、**ゾーン固定 9 行**なら構造変更ゼロで永続運用可。

**$0 ゾーン行も先に作る** (Codex 推奨): 今 $0 の Z1 韓台 / Z11 豪 も $0.00 行として作成。将来 $0→正 に転じてもバッチが API で更新でき構造変更不要。⚠️ eBay が $0.00 行を保存・保持するか実機確認が条件 (不可なら当該ゾーンのみ省略 = 稀な正転時のみ手動追加)。

**1-2kg (table 5284241010) = 9 行** (rateId 昇順 = ゾーン昇順で投入):
| rateId | DHL Zone | USD | 国 |
|---|---|---|---|
| 1 | Z1 韓台 | $0 | Korea South, Taiwan |
| 2 | Z2 中華圏 | $1 | China, Hong Kong, Macau |
| 3 | Z3 東南ア | $1 | Indonesia, Malaysia, Philippines, Singapore, Thailand |
| 4 | Z4 印 | $5 | India |
| 5 | Z6 欧州 | $9 | 欧州31国 + Turkey |
| 6 | Z7 東欧 | $19 | Albania, Armenia, Iceland |
| 7 | Z8 イスラエル | $33 | Israel |
| 8 | Z9 中央ア/中東 | $40 | Kazakhstan, Nepal, Oman, Pakistan, Qatar |
| 9 | Z11 豪 | $0 | Australia |

- DHLゾーンと eBay地域はズレる (Turkey は eBay=Middle East だが DHL Z6 = 欧州行に同居 / Z9 は KZ·NP·PK[Asia] + OM·QA[Middle East] 横断)。**1 行が複数 eBay地域をまたぐのが前提** (実機確認済)。
- 行は Expedited 区分にのみ作る (Standard/Economy は空のまま=実機確認済)。
- ⚠️ 旧 `phase3_rows_ebayUI_1-2kg.json` (13 行版) は **deprecated**。正は `phase3_rows_design.json` (DHLゾーン 9 行版)。

**月次バッチの必須要件 (Codex VERDICT A 運用面)**:
- canonical manifest を保持: `(table_id, weight_band, zone, expected_countries, rate_id)`。
- バッチは `zone→rateId` キャッシュを盲信しない。毎回 getRateTable で構造を読み、**9 ゾーン行が欠落/重複/想定外国セットなら fail-closed** (Q0: 異常時は前回値維持 + アラート、適用しない)。
- 「行なし国 = $0」を**正常な無料運用と見なさず設定エラー**として扱う (Codex 指摘: 欠落行は config error)。
- human が UI 編集すると eBay が rateId を再採番し得る → バッチは適用前に rateId 実在を再検証。

## 8. API / UI 仕分け
- **初期投入 = eBay UI**: 行の再編（ゾーン構造へ）＝構造変更。`getRateTables` v1 は読取のみ、構造変更APIは無い前提。
- **月次の金額更新 = `updateShippingCost` v2 API**: ゾーン構造固定後は金額のみ API 更新。✅**2026-06-19 実機検証済**（未使用 domestic test table 5254186010 で書込→読戻し→復元）:
  - endpoint: `POST https://api.ebay.com/sell/account/v2/rate_table/{id}/update_shipping_cost`
  - payload: `{"rates":[{"rateId":"1","shippingCost":{"value":"8.88","currency":"USD"}}]}` (bare array は 400 / `{"rates":[...]}` で包む / rateId 昇順 / 変更行のみでよい)
  - 成功=204 No Content。冪等・rollback(元値再POST)動作確認済。Codex MED「未検証」クローズ。
  - ⚠️ **API は既存 rateId の金額更新のみ。行(国)の追加・再編は不可＝初期構造投入は UI 必須**。
- ⚠️ **blast radius**: (a) 全 DDP table は **1day policy と 7day policy が同一 rateTableId を共有**（10 table=20 policy）。(b) さらに 5284247010(2-3kg)=Pre-order が、5284249010(3-4kg)=Flat$50 が相乗り（計22 policy）。1テーブルの金額変更は共有する全 policy に即波及。

## 9. ロールアウト (1ステップ承認制)

**大前提 (Codex HIGH 対応)**: 本作業は **rate table（コストのみ）の編集**であり、**policy の shipToLocations（＝実際の配送可能国）は一切変更しない**。eBay では配送可能国は shipToLocations が決め、rate table 行の有無はコストのみ（行なし国は base=$0 で配送は継続）。よって行のゾーン再編は「配送可能国の変更」ではなく「コスト構造の変更」。安全ルール §10「金額と配送可能国を同一作業にしない」は、各テーブル編集の**前後で policy shipToLocations を getFulfillmentPolicy で取得し diff=0 を機械的に証明**することで担保する。

4. **Phase 4 一部実施済 (2026-06-19)**:
   - (a) 実UI構造確認済(Expedited5行/Standard・Economy空、1行=同額国集合、保存=全listing反映)。eBay未変更。
   - (b) ✅ **updateShippingCost API 実機検証済**(§8参照、未使用 domestic test table で書込→読戻し→復元、payload形式確定、冪等・rollback動作)。
   - (c) 🔴残: 国際throwaway tableで「1行の地域またぎ可否」「保存挙動」予行(任意・非ブロッカー)。
5. **backup + 構造rollback 手順 (Codex HIGH対応)**:
   - 適用直前に getRateTable で対象 table を再 snapshot。
   - ⚠️ **API は金額のみ更新可・行構造の追加/削除は不可** → 構造を誤って保存すると API で戻せない。よって rollback は **UI で旧行を手入力で再投入**。1-2kg の旧状態は確定済 = **5行: Macau$12 / Indonesia$4 / Netherlands$3 / India$4 / (Maldives,Nepal,Pakistan,Sri Lanka)$13**(Expedited区分)。これを復元手順として明記・保持。
   - **保存直後に必ず getRateTable で読戻し検証**(9ゾーン行・各国セット・各金額が設計値と一致)してから次へ。不一致なら即 UI で修正 or 上記5行へ復元。
6. **1本目 = 1-2kg (table 5284241010, user確定)**: UI で **既存5行を削除 → DHLゾーン単位9行を投入**(§7 表の通り: Z1 KR·TW$0 / Z2 CN·HK·MO$1 / Z3 ID·MY·PH·SG·TH$1 / Z4 IN$5 / Z6 欧州31+TR$9 / Z7 AL·AM·IS$19 / Z8 IL$33 / Z9 KZ·NP·OM·PK·QA$40 / Z11 AU$0)。**最終保存は人間**。前後で shipToLocations diff=0 を証明 + 保存直後に getRateTable 読戻し検証。⚠️1day+7day両policy共有=同時反映。⚠️ $0.00 行 (Z1/Z11) の保存可否を 1 行目入力時に実機テスト (不可なら当該2ゾーン省略=7行)。
7. 段階展開（現役listing数の多い順: 1-2kg(131)→2-3kg(103)→10-20kg(70)→6-8kg(51)…）。各テーブルで shipToLocations diff=0 + 保存直後の読戻し一致。各帯も同じ **DHLゾーン9行**構成 (`phase3_rows_design.json` の該当帯、金額のみ差替)。
8. 全量監査（getRateTable で全テーブル読戻し、設計値と一致確認）。
9. 月次バッチ実装: 取得(為替/燃料/基本料金 実ブラウザ)→計算→getRateTable diff→updateShippingCost→読戻し→Discord。**サニティガード必須**（取得失敗/異常値は適用せず前回値維持+アラート、Q0）。**前提=§4(b)で API 更新セマンティクスが検証済であること**。

## 10. リスクと安全ルール
- 「金額変更」と「配送可能国変更」を同一作業にしない。金額更新前に国集合diff=0を証明。
- region名の国展開は eBay 公式定義のみ（地理感禁止）。ISO正規化。
- 共有rateTableId の blast radius を作業前に明示。
- UI編集は入力補助+diff読取+スクショ、**最終保存は人間確認**。
- tmp 成果物(data/tmp/*.py,*.json)は Phase 9 で scripts/ へ昇格・本実装が必要（現状は分析用 throwaway、月次自動化には未対応）。
- 既知の軽微: `dhl_country_zone.json` に junk code "PO" 1個（無害、PT正）。昇格時クリーン化。

## 11. 確定パラメータ一覧 (user 2026-06-19 承認)
為替157 / 対象DDP10table・22policy / カバレッジ先行 / FedEx燃料web取得 / $0漏れPhase3一括 / 混在最大額寄せ / US基準=列E(USW保守) / 月次バッチ=完全自動取得即適用(サニティガード付).

**1本目 = 1-2kg (table 5284241010) に確定 (user 2026-06-19 承認)**: 現役 **131件で最大ボリューム** かつ欧州ほぼ全域が $0 漏れ＝**実損失(値上げ幅×注文量)が最大**。per-unit 最高の Z9($318)/Z8 Israel($244) 重帯ではなく、ボリューム基準で選定。展開順 = 現役件数降順 (1-2kg→2-3kg 103→10-20kg 70→6-8kg 51→…)。
（参考: 新マトリクスの per-unit 最高は Z9(KZ/NP/OM/PK/QA) で Israel ではない。当初「Israel=損失最大」は誤りで訂正済。）
