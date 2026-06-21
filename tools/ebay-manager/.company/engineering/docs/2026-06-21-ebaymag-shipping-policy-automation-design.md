# eBaymag 送料ポリシー正規化＋全ライフサイクル自動適用 設計書

- 作成: 2026-06-21 / 使用モデル: Opus 4.8 (code-architect 設計 → 本書) / Q3 設計フェーズ
- 出典/前提: `session_2026_06_20_ebaymag_shipping_leak_diagnosis.md`、本セッションの実機検証(eBaymag各国版 送料$0漏れ、ポリシー編集→各国版伝播の実証)
- ステータス: 設計 (実装前。Codex 事前レビュー → Phase 0 spike → 実装 → Q1 検証)

## 1. 背景・要件

eBaymag 各国版の送料 $0 漏れ (General Policy が「Worldwide 無料」のため各国版が $0 発送)。AU 手動パッチでなく根治する。user 要件:
1. 全パターンの eBaymag 送料ポリシーを正規化作成
2. 自動で設定 (手動でなく仕組み)
3. 全ライフサイクル対応: 新規出品 / 変更 / end listing / Sell Similar

## 2. 真因 (実機確定)

1. eBaymag「配送ポリシー同期は初回インポート時のみ」→ rate table 修正は各国版へ永久に届かない
2. rate table は `marketplaceId=EBAY_US` 専用 → 各国版に技術的に乗らない
3. 漏れ 4 ポリシー (DDP_6-8kg/2-3kg/1-2kg/10-20kg) が全て「General=Worldwide 無料」

実証: ポリシーの **per-country タブ (ebay.com.au)** に AU=$62 設定 → 実 ebay.au ページで $62 を user 目視確認 = **ポリシー値は各国版に伝播する**。

## 3. アーキテクチャ核心

(a) 重量帯ごとの canonical な eBaymag 送料ポリシーをシステムが定義 (値は rate table 由来) → (b) 各商品を帯で正ポリシーに **CDP 自動付替** → (c) 新規/変更/relist/Sell Similar の全ライフサイクルで自動維持 → (d) 実 eBay 各国版ページ(権威)で検証。

値の源泉 = 既存 `scripts/shipping_rate_batch/` の zone 別 USD (manifest)。適用経路 = W284 CDP driver (`monitor/ebaymag_driver.py`) 拡張。

## 4. 核心判断

### 判断A: 適用方式 = 案1「既存ポリシー付替」主軸

| 案 | 内容 | feasibility | 採否 |
|---|---|---|---|
| **案1 ポリシー付替** | canonical ポリシーを **user が一度手動作成** (type dropdown 問題を1回で回避) → 以降 商品ごとに「別のポリシーを選択」を CDP 付替 | 付替 UI の CDP 可否 = **Phase0 spike** | **第一推奨** |
| 案2 per-country タブ金額入力 | サイト別タブに金額を CDP 入力 | 未検証。spike 次第で補助 | 補助 |
| 案3 General 地域別化 | General type を地域別に | **不可確定** (portal widget) | 却下 |

理由: canonical ポリシーは有限個(帯数)。一度作れば商品側は「選択」だけ = mutation 面積小・可逆 (K1)。

### 判断B: zone(rate table) → eBaymag 地域マッピング

eBaymag 粒度 = Worldwide/Americas/Europe/Asia + 国別(AU/CA/GB/DE/FR/JP/CN/MX/BR/RU)。rate table = 9 zone。粒度不一致 → 粗い地域(Europe/Asia)に異 zone 国を混ぜない方針:
1. **サイト別オーバーライド**: 各国版が存在する 8 サイト(US/CA/UK/DE/IT/FR/ES/AU)に zone 値。US=$0(本体 rate table が課金)、AU=zone11、UK/DE/IT/FR/ES=zone6(EU)、CA=Americas 相当
2. **地域 catch-all**: 各国版が無いが buyer が付く地域は Europe/Asia へ zone 代表値
3. **除外国** (判断: 配送不可で除外): India/Israel/Kazakhstan/Nepal/Oman/Pakistan/Qatar/Iceland/Albania/Armenia (高コスト・107 ポリシー前例)

EU 逆転 (3-4kg EU=$17 vs 6-8kg EU=$0): **本セッションで調査済 = モデル正 (差額式 max(0, DHL_EU − FedEx_US) の正常挙動、Codex 検証済)**。EU=$0 は正値として canonical に採用。本設計のブロッカーではない。

### 判断C: 範囲 = 漏れ4帯先行

漏れている 4 帯 (1-2/2-3/6-8/10-20kg) を最優先で canonical 化。残り帯は実需要が出てから (K1、speculative 禁止)。AU 値(帯別、ebay.com.au タブ): 1-2kg=$0 / 2-3kg=$8 / 6-8kg=$62 / 10-20kg=$92。

### 判断(確定済デフォルト、user 委任 2026-06-21)
- 除外国: **配送不可で除外** (undercharge 無し)
- 範囲: **漏れ4帯先行**
- rate table 追従: **当面 通知専用** (money-direct 保護、W283 dry_run と同思想)

## 5. データモデル (SKU 不使用、ebay_item_id 識別)

### migration vN: 新規 `ebaymag_shipping_policies` (canonical 正本)
band / policy_title / ebaymag_policy_token / site_values_json / region_values_json / excluded_countries_json / source_run_id / status(draft|live|deprecated) / timestamps。`UNIQUE(band, status)` (band=重量帯=値集合キー、listing 識別でない)。

### migration vN+1: `ebay_listings` 列追加 (冪等 try/except OperationalError)
`ebaymag_shipping_band` TEXT / `ebaymag_policy_applied_at` TIMESTAMP。(`ebaymag_segment`/`ebaymag_desired_sites_json` は既存=実 DB で存在確認)

### apply_queue reason 拡張 (スキーマ変更不要)
既存 `ebaymag_apply_queue` の reason に `shipping_policy` 追加。W284 queue 再利用 (別 queue 新設しない、K2)。

### 値の正本ファイル
`data/ebaymag_shipping_policies/canonical_{band}.json` (manifest 生成)。DB は反映状態ミラー。shipping_rate_batch と同じファイル正本パターン。

## 6. 値マッピングモジュール `monitor/ebaymag_policy_mapping.py` (新規)

`build_canonical_policy(band) -> dict`: `phase6_manifest_{band}.json` の zone 別 USD を読み、`zone_definitions.json` の iso[] を単一 source に site_values/region_values/excluded_countries を生成 (除外国は手書きせず zone から展開 = cascade 安全)。US=$0 固定 (本体二重課金回避)。

## 7. CDP 自動適用 (`monitor/ebaymag_driver.py` 拡張、既存関数不変=K2)

- `probe_policy_assignment(product_id, expected_itm)` — **Phase0 spike**: 「別のポリシーを選択」UI の CDP 操作可否 + ポリシー一覧取得を read-only probe。itm 照合安全弁を先頭で通す。**全設計の Go/No-Go**。
- `assign_policy(product_id, expected_itm, target_policy_token)` — spike 成功後。itm 照合 → 付替 → 保存 → リロード定着検証。subprocess 隔離。canary 前 flag OFF。
- `set_policy_site_value(policy_token, site, usd)` — 案2 補助。入力後リロード読戻し一致必須 (money-direct)。

## 8. ライフサイクルフック (全て apply_queue reason=shipping_policy へ enqueue)

| イベント | 検知点 | 処理 |
|---|---|---|
| 新規出品 | 出品確定 (個別出品/承認キュー) | weight→band→band 保存→enqueue。取込ラグは awaiting_import (W284 backoff) |
| Sell Similar | 新規 listing→eBaymag 自動 import。新 item_id discover (W284) | band 付替を discover 経路に追加 |
| weight 変更 | 商品管理重量編集 / estimate_weights_claude | 旧band≠新band で band 更新→enqueue |
| end listing | W284 終了アーカイブ (既存) | 追加処理不要 (K1) |
| relist(eBaymag) | W284 Phase3 窓ゼロ relist | band 継承→新 item_id へ付替 enqueue |
| 月次 rate table | W283 run_batch 成功後 | §9 (通知専用) |

weight→band は `config.BAND_UPPER_KG` 再利用。

## 9. rate table 同期 (W283 追従、通知専用)

`run_batch.py` auto 成功パス末尾にフック1点: canonical 再生成→差分なら draft 更新 + Discord('pricing') 通知 (R-11)。**自動 mutation は spike+canary 後のみ**。当面通知専用。

## 10. 検証 (Q1 DoD)

権威 = 実 eBay 各国版ページ (eBaymag UI は stale)。
- pytest: zone→site/region/excluded マッピング、band 導出、enqueue 冪等、canonical 生成
- canary 実機: テスト品で付替→ebay.{site}/itm/{各国版ID} を買い手ロケーションで送料正値目視
- DB SELECT: status='live'、ebaymag_policy_applied_at 更新
- scheduler.log: apply_queue shipping_policy reason applied
- code-reviewer HIGH=0 + Codex 2段

## 11. ビルドシーケンス

- **Phase 0 (Go/No-Go)**: CDP 付替 spike (最重要) / 取込ラグ計測。※EU 逆転は調査済(モデル正)
- **Phase 1**: migration + mapping module + canonical 生成 (mutation なし) + user が canonical ポリシー手動1回作成→token backfill + 単体テスト
- **Phase 2**: assign_policy driver + apply_queue 配線 (canary)
- **Phase 3**: ライフサイクル全フック
- **Phase 4**: rate table 追従 (spike 成功時のみ自動化、当面通知専用)

## 12. 作成/修正ファイル

新規: `monitor/ebaymag_policy_mapping.py` / `data/ebaymag_shipping_policies/canonical_{band}.json` / `scripts/gen_ebaymag_canonical_policies_2026_06_21.py` / `scripts/ebaymag_policy_probe_2026_06_21.py` / `tests/test_ebaymag_policy_mapping.py`
修正: `monitor/ebaymag_driver.py` / `monitor/database.py` (table+列+migration) / `tasks/task_ebaymag_apply_queue.py` (reason 分岐) / `monitor/ebaymag_segment.py` (band 導出+enqueue) / `tabs/tab_product_management.py` + `tab_individual_listing.py` (配線+状態表示) / `scripts/shipping_rate_batch/run_batch.py` (通知フック) / `tasks/task_ebaymag_relist.py` (band 継承)

## 13. リスク

| リスク | 緩和 |
|---|---|
| money-direct (送料誤設定) | canonical 値は実ページ canary 必須。付替は可逆。spike 前は通知専用 |
| CDP 自動化信頼性 | itm 照合+定着検証 (W284 実証)。fail=needs_manual+通知。subprocess 隔離 |
| ポリシー新規作成不可 | user 手動1回。system は付替のみ自動化 |
| zone↔地域粒度不一致 | 国別オーバーライド+除外国。zone_definitions 単一 source |
| 二重課金 (US 版) | US=$0 固定。canary で US 版も目視 |
| 取込ラグ | W284 awaiting_import backoff。Phase0 で実測 |
| 多通貨 (AUD/EUR) | canary 目視は現地通貨額。診断の通貨ミス教訓を手順化 |

## 14. 残・開かれた点 (spike 後に確定)

- 案1/案2 最終 (Phase0 spike 結果)
- canonical ポリシー手動作成の帯数 (漏れ4帯先行で確定)
- FVF 上乗せ (Codex 指摘) を各国版送料に乗せるか
- 既存漏れ商品の一括是正速度 (canary 後) → **実装後 reviewer MED-1 で解決**: 付替 enqueue 契機は band 変化 (lifecycle) と relist のみで、既存 $0 漏れ品は flag ON でも自動では拾われない。`scripts/backfill_ebaymag_shipping_band_2026_06_21.py --apply` で実インポート済 (ebaymag_products mapping あり) listing の band を weight から一括設定+enqueue する (canary 確認後に実行)。dry-run で 118 件対象を確認済。
- **CA 値源泉確定 → CA を canonical 対象に戻す別フェーズ** (HIGH-3、下記)

## 15. Codex 事前レビュー反映 (2026-06-21, VERDICT B → 修正確定)

Codex (gpt-5.5) + Claude(Opus 4.8) 2段レビューで HIGH 3 / MED 2 を検出 (全 accept)。実装前に以下を設計に反映:

### HIGH-1: 消化側 `_process_job` の reason 非分岐 → **ebay_listings 状態駆動に再設計**
実コード `task_ebaymag_apply_queue.py:127-274` の `_process_job` は reason を一切参照せず、無条件で「desired_sites と実態の差分で国 ON/OFF」する。このまま shipping_policy job を入れると国トグルが走り、**desired_sites 空の listing で全サイト OFF = 出品消失**の二次災害。
**修正**: 消化側は **ebay_listings を単一真実源として 2 軸を 1 CDP パスで適用** する:
- (軸1 国) `desired_sites_json` vs 実サイト状態 → 既存 `apply_site_changes` 経路 (本体不変=K2)
- (軸2 送料) `ebaymag_shipping_band` の target policy token vs `ebaymag_applied_policy_token` → 不一致なら `assign_policy`
- reason は「どの軸を主因に enqueue したか」の情報のみ (処理の唯一の分岐にしない)。**shipping_policy 起因でも国トグルを勝手に走らせない** (軸1 は desired と実態が一致していれば no-op)。
- desired_sites 空での全 OFF は既存挙動だが、shipping_policy 単独 enqueue で誤発火しないよう、軸1 は「desired が明示的に設定済みの時のみ」適用するガードを確認 (実コードの現挙動を Phase2 実装時に再検証=md-files-can-be-wrong)。

### HIGH-2: enqueue 集約の reason 上書き衝突 → **採用案(b) policy 信号を列で持つ**
`enqueue_ebaymag_apply` (database.py:6903-6938) は 1 ebay_item_id = 1 active job 集約で reason を上書き → site-toggle と shipping-policy の意図が片方消失 (money-direct)。
**修正(案b)**: policy 適用要否を **queue payload でなく `ebay_listings` 列** で表現:
- `ebaymag_shipping_band` (target band) + `ebaymag_applied_policy_token` (最後に適用した policy token)
- 消化時、両者から「付替要否」を導出。queue は「この item に変更あり」の信号のみ (v77 の desired 単一真実源・queue=信号 と同思想)。
- これで reason 衝突自体が消える (国も送料も ebay_listings 由来、queue payload に意図を載せない)。
- `ebaymag_apply_queue` の reason 列は維持 (情報/監査用)。スキーマ変更不要。

### HIGH-3: CA/Americas の送料値源泉が manifest に無い → **CA を canonical 対象外 (別フェーズ)**
`zone_definitions.json` に Americas/Canada/MX/BR の zone が無い (実在 zone: 1/2/3/4/6/7/8/9/11)。「CA=Americas 相当」は無根拠 → CA 誤課金 = 漏れ再生産。
**修正**: **CA(ebay.ca) は canonical 適用対象から除外** (US と同じ「触らない」)。AU(zone11)/UK・DE・IT・FR・ES(zone6=EU)/US(=$0) のみ canonical 対象。CA は DHL/FedEx 実費を別途 manifest 化できた後の別フェーズ。「Americas 相当」表記は廃止。§4 判断B のサイト別オーバーライド表から CA を外す。

### MED-1: band 判定の参照を明確化
`BAND_UPPER_KG` は `scripts/shipping_rate_batch/config.py:45` のみに存在。monitor から scripts への逆 import を避けるため、**band 判定ヘルパーを `monitor/ebaymag_policy_mapping.py` 内に持つ** (閾値は同 config を single source とし、値生成 one-shot 側でのみ import。実行時の monitor 経路は monitor 内 helper)。設計内の「config」参照は全てフルパス明記。

### MED-2: weight 変更の band 更新と消化の race
band 更新と enqueue を **同一 DB トランザクション**で行い、消化 (`assign_policy` 経路) は処理冒頭で `ebay_listings` から band/applied_token を**再読込** (snapshot 複製しない)。max_instances=1 は別 job 間 race を防がない (silent-skip-prevention の _batch_ctx 教訓)。

### LOW: `UNIQUE(band, status)` の deprecated 複数行
status='deprecated' を複数持つ必要があるなら `UNIQUE(band,status)` が衝突 → 実装時に `UNIQUE(band) WHERE status='live'` 等の部分 index か再検討。SKU キー違反ではない (Codex/Claude 一致)。

### データモデル修正 (§5 差分)
`ebay_listings` 追加列: `ebaymag_shipping_band` / `ebaymag_policy_applied_at` に加え **`ebaymag_applied_policy_token` TEXT** を追加 (HIGH-2 案b)。
