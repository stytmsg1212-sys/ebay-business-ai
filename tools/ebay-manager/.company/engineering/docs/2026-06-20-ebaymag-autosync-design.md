# eBaymag 各国版 自動連携 設計書 v2 (依頼ボード #4/#5)

作成: 2026-06-20 / モデル: Opus 4.8 / 状態: **二段レビュー反映済 (Codex + code-architect、HIGH 解消)**
v1→v2: Codex(HIGH5/MED7)+code-architect(HIGH3/MED4) を全反映。relist方式を「窓ゼロ別経路」に再設計、優先国データ源を既存流用に訂正、queue冪等性を2段UPSERTに。

## 0. 目的と確定要件 (user 合意済 2026-06-20)

**ゴール**: 商品登録時に【全国 / 優先国 / カスタム / 出さない】の4択を選べば、あとは eBaymag を一切意識せず自動で随時反映される。

確定した設計判断:
- **アーキ**: 案A = eBaymag 継続 + パネル半自動 driver。既存実装 (依頼#10) の上に最小追加 (K2 surgical)。
- **eBaymag 設定**: 自動インポート ON / 自動出品 OFF を維持。content-sync ON (US編集が1日以内に各国版へ、実測3/47件失敗あり)、終了時アーカイブ ON。
- **反映タイミング**: 「ログイン中に自動でまとめて反映」= CDP Chrome + eBaymag ログイン生存時に未反映キューを定期消化。失敗時のみ Discord 通知。完全リアルタイム無人は不可。
- **relist 衝突 (user決定A=窓ゼロ)**: eBaymag商品のrelistは **CDP在席時に「relist→新productId発見→各国再公開」を同一ウィンドウで完結** (各国版が消える窓を作らない)。深夜02:30の無人relistは **eBaymag商品を対象にしない (#6維持)**。
- **relist 条件変更**: #5 (SKUあり) は Phase1 で撤廃 (cascade更新含む)。**#6 (eBaymag=出さない限定) は daily_relist では維持** (eBaymag商品の relist は別経路 = Phase3)。

## 1. 現状 (実機確認済 2026-06-20)

### 既存実装 (依頼#10) — 再利用する資産
- `ebaymag_products` (v75): `ebay_item_id(PK) ↔ product_id ↔ site_states_json(**実態**キャッシュ) ↔ last_synced/applied_at/result`。**119件のみ登録**。helper: `upsert_ebaymag_product` / `get_ebaymag_product` / `record_ebaymag_apply` (database.py:6614-6715、rowcount 2段流儀の手本)。
- `monitor/ebaymag_driver.py`: `fetch_site_states` / `apply_site_changes`、SITE_MAP=7カ国、itm照合・変動数・定着検証の安全弁。**productId discover は driver に無い** (移植元 = `scripts/ebaymag_publish_driver_2026_06_11.py:239 cmd_discover`)。
- `tabs/tab_product_management.py::_render_ebaymag_section()` (:2029): 7カ国チェックボックス+反映。**誤OFF防止ガード** (:2082-2085、実態未取得時は反映無効)。
- `monitor/ebaymag_segment.py`: **優先国算出が既に本番稼働** — `recompute_ebaymag_segments()` が `market_analysis.countries_breakdown` (database.py:1516、Terapeak買い手国別sold) を読み、`_C2S` (国→7サイト, :17-22) で非US実績国を優先国として `ebaymag_segment` を更新 (:59-69)。
- `ebay_listings.ebaymag_segment`: 全国/優先国/出さない (カスタム未対応)。

### 監査の発見 (前提)
- 各国版361件。各国版はSKU空/共有・タイトル翻訳のため eBay API単独で per-product突合不可 → マッピング権威は eBaymag パネル(itmリンク)。
- 価格/在庫/画像はおおむね同期、**説明文3/47件で同期失敗**。
- relist は ebay_item_id を変える → eBaymagリンク断絶。現状W242で eBaymag商品をrelist除外して回避 (孤児1/119)。

## 2. 中核データモデル: 希望(desired) と 実態(actual) の分離

### 2.1 唯一の真実源 = `ebay_listings` (希望)  [H3/Codex#2 反映]
希望状態は **`ebay_listings` を単一の真実源** とし、queue には複製スナップショットを持たせない (古いsnapshot誤適用を防ぐ)。

`ebay_listings` 列追加 (migration v77、冪等):
| 列 | 型 | 意味 |
|---|---|---|
| `ebaymag_segment` (既存) | TEXT | 全国 / 優先国 / **カスタム** / 出さない (値拡張) |
| `ebaymag_desired_sites_json` (新規) | TEXT | 希望出品国の解決済リスト `["UK","DE"]`。全国=7カ国 / 優先国=`resolve_priority_sites()` / カスタム=user選択 / 出さない=`[]` |
| `ebaymag_desired_updated_at` (新規) | TIMESTAMP | 希望の最終更新 (差分検知) |

### 2.2 `ebaymag_apply_queue` 新規 (migration v78) — 「変更あり信号」 [H3 反映]
| 列 | 型 | 意味 |
|---|---|---|
| `id` | INTEGER PK | |
| `ebay_item_id` | TEXT | 対象 (識別キー、SKU禁止) |
| `reason` | TEXT | `new_listing` / `segment_change` / `relist_relink` / `manual` |
| `status` | TEXT | `pending` / `awaiting_import` / `applying` / `done` / `failed` / `needs_manual` |
| `attempts` | INTEGER | 試行回数 |
| `last_error` | TEXT | 直近エラー (候補数/検索語/expected itm 含む。Q0) |
| `next_attempt_at` | TIMESTAMP | backoff 再試行時刻 (awaiting_import/failed) |
| `created_at` / `updated_at` | TIMESTAMP | |

- **desired_sites_json はここに持たない**。消化時に `ebay_listings.ebaymag_desired_sites_json` を**再読込** (最新の希望で適用)。
- **冪等性 = enqueue helper の2段集約** (部分UNIQUE index は SQLite で ON CONFLICT ターゲット不可のため使わない): `UPDATE ...SET reason=?,updated_at=now WHERE ebay_item_id=? AND status IN('pending','awaiting_import','failed')` → `rowcount==0 なら INSERT` (record_ebaymag_apply と同流儀)。1 item 1 active job に集約。
- **awaiting_import** [M3反映]: discover が None (eBaymag取込ラグ) の時の専用状態。`next_attempt_at` で N回/M時間まで静かにリトライ、閾値超過で needs_manual+通知 (過剰通知も silent skip も防ぐ)。

### 2.3 `ebaymag_products` (既存・実態キャッシュ) — 変更最小
- 実態 (eBaymagが今こうなっている) を保持。希望とは責務分離。
- relist時の旧 mapping 終端用に列 1 追加検討 (`lifecycle_state`: active/relisted/ended)。最小実装なら `last_apply_result` 文字列で代替可 — Phase3で確定 (K1)。

### 2.4 ライフサイクル記録 — Phase3 で要否判断 (K1、先回りしない)
apply_queue (reason/attempts/last_error/旧新item_id) で当面の監査は足りる。フルイベントテーブルは relist窓運用の実績を見てから。

## 3. Phase 設計 (安全順序)

### Phase 1: 基盤 + 4択UI + #5撤廃 (relistは無人#6維持=安全)
1. **migration v77/v78** (冪等。Q2: 全列/全テーブル存在を `sqlite_master`/`PRAGMA table_info` で確認後のみ user_version bump)。
2. **productId discover**: `scripts/...2026_06_11.py:cmd_discover` を `ebaymag_driver.discover_product_id(query, expected_itm) -> str|None` として抽出移植。未発見/複数候補/検索語を返り値+log に残す (silent skip 禁止)。
3. **優先国解決 [H1反映]**: `ebaymag_segment.py:59-69` のサイト算出を `resolve_priority_sites(ebay_item_id) -> list[str]` に関数抽出し、segment再計算と desired解決の両方から呼ぶ (single source = `market_analysis.countries_breakdown`)。実績なし新商品は空 → UIで「全国/カスタム」案内。CSVは不採用。
4. **`resolve_desired_sites(ebay_item_id, segment, custom_sites=None)`**: 全国→7カ国 / 優先国→`resolve_priority_sites` / カスタム→custom_sites / 出さない→`[]`。
5. **4択UI** (商品管理改修 + 個別出品新設、**共通化** [M2反映]):
   - 共通コンポーネントを **「区分選択 (ebay_item_id不要、session_state退避)」** と **「driver反映 (ebay_item_id必須)」** に分離。個別出品は出品前=item_id未確定なので前者のみ、出品確定後/商品管理は両方。
   - 区分選択→`resolve_desired_sites`で `ebaymag_desired_sites_json` 保存+`desired_updated_at`更新→**apply_queue へ enqueue** (2段集約)。
   - 既存「📤 反映」ボタンも **desired保存→同一適用経路** に統一 (raw desired を流さない)。
6. **#5 (SKU条件) 撤廃** [L3反映]: `_select_relist_targets` の `AND sku IS NOT NULL AND sku != ''` 削除。**cascade**: 同ファイル :474-486 の NULL pool 警告クエリも sku 条件整合を更新。docstring「対象SKU1件選出」を item_id 粒度に訂正。回帰テスト (sku='' の relist dry-run/継承/record/通知) 追加。#6維持なので各国版破壊なし。

### Phase 2: 自動消化 + 同期失敗監視
1. **キュー消化タスク** `task_ebaymag_apply_queue` (scheduler **登録が Phase2 完了条件** [Codex#5]):
   - CDP+eBaymagログイン生存を probe → 生存時のみ消化。
   - 各 active job: `ebay_listings` から **最新 desired 再読込** → `fetch_site_states` で**実態再取得** → **差分(turn_on/turn_off)算出** → driver適用 → ebaymag_products更新 → done/failed [M1反映]。
   - productId未登録なら discover→登録、None なら **awaiting_import** (backoff)。
   - 非生存時: 据置 + Discord「eBaymag反映待ち N件、ログイン要」(R-11 実視認)。
   - **Q0**: 全失敗経路で status/attempts/last_error/updated_at 更新。`states={}` を成功扱い禁止。TASK_SCHEDULE 登録 (silent-skip-prevention 必須4要件)。
2. **同期失敗監視** `task_ebaymag_sync_audit` (日次): `audit_ebaymag_update_sync_2026_06_20.py` を本番task化。US本体 vs 各国版の説明文長/価格比/画像枚数を突合→ズレ→通知+UI一覧。
3. **done purge** [L1]: 古い done を定期 purge する1行方針を決める (肥大化対策)。

### Phase 3: eBaymag-aware relist (窓ゼロ) + canary
1. **eBaymag-relist 経路** (新設、CDP在席時のみ走る。daily_relist の #6 は維持):
   - トリガー = キュー消化と同じ「CDP+ログイン生存」確認時。
   - 対象 = eBaymag商品 (segment∈{全国,優先国,カスタム}) で relist条件 (watch=0/rank=E/cooldown/在庫≥1) を満たすもの。
   - 各 item を **同一ウィンドウで一気に**: ① RelistFixedPriceItem (新item_id) → ② `inherit_listing_on_relist` で **`ebaymag_desired_sites_json` も継承** [Codex#3] → ③ 旧 `ebaymag_products` を relisted/ended マーク → ④ 新item_id を discover→登録 → ⑤ desired国へ再公開 (fetch→diff→apply) → ⑥ 定着検証。**①〜⑤を分断しない (窓ゼロ)**。途中失敗は needs_manual+通知 (中途半端な公開を残さない)。
   - **再公開enqueueは `inherit_listing_on_relist` 成功パスからのみ (reason=relist_relink)** [M4反映]。手動終了/売切終了 (relist_history に乗らない) は再公開しない (在庫無し販売=defect防止)。
2. **canary**: 全国/優先国/カスタム 各1件を手動/feature flagで実施。relist→discover→apply→各国実ページ確認が**同一run成功**した item のみ拡大。
3. ライフサイクル記録テーブルの要否をここで確定。

## 4. ライフサイクル対応表 (Codex 22パターンへの回答、v2修正反映)

| パターン | 設計での扱い | Phase |
|---|---|---|
| 新規出品(4択) | segment選択→desired保存→enqueue→消化で各国公開。discover、取込ラグは awaiting_import | 1+2 |
| 優先国・実績なし | `resolve_priority_sites`空→UIで全国/カスタム案内。実績出たら再計算でenqueue | 1+2 |
| カスタム国 | segment=カスタム+desired_sites_json | 1 |
| 出さない | desired=[]。既公開なら全OFF enqueue | 1+2 |
| 在庫0(売切) | eBaymag完売表示=掲載維持。desired不変 | — |
| 再入荷0→N | content-sync想定。sync監視で未復活検知→enqueue | 2 |
| 価格/説明/画像変更 | content-sync ON。sync監視で失敗検出 (説明文3件型) | 2 |
| **relist(新item_id、eBaymag商品)** | **eBaymag-relist別経路で窓ゼロ** (relist→discover→再公開を同一ウィンドウ) | **3** |
| relist(出さない商品) | daily_relist 02:30無人で従来通り (各国版なし) | 1(#5) |
| US完全終了(relistでない) | eBaymag終了時アーカイブON。再公開キューに**入れない** (relist成功パス限定) | 2/3 |
| segment後変更 | desired再計算→差分enqueue | 1+2 |
| 未登録(119件以外) | discover実装で連携登録 | 1 |
| CDP/ログイン切れ | 据置+通知、在席時まとめ消化 | 2 |
| SKU編集 | 識別ebay_item_id固定で不変。優先国算出も item_id粒度 | 1 |
| 優先国実績更新 | market_analysis後に再計算→差分enqueue | 2 |
| eBaymag側勝手な変化 | sync監視+定期fetchで実態とdesired差分→enqueue | 2 |
| 出品上限逼迫 | 公開時に上限チェック→超過は needs_manual+通知 | 2 |
| 部分失敗 | queue status/attempts/last_error、driver定着検証 | 1+2 |

## 5. 安全策・検証 (Q0/Q1/Q2)
- itm照合・誤OFF防止 (実態再取得後の差分適用) を全適用経路で維持。
- Q0: 全失敗を status+last_error+通知。`states={}`/discover None を成功扱い禁止。
- Q2: migration v77/v78 は全列/テーブル/index を `sqlite_master`+`PRAGMA` 確認後のみ user_version bump。
- Q1: Streamlit再起動+Playwrightで4択UI E2E + DB SELECT + **canary実機** (1商品をキュー経由で各国公開→eBaymag/eBay各国実ページ確認)。
- Phase3着手前に **eBaymag-relist canary の実時間 (relist→再公開) を計測し user 承認**。

## 6. 未確定点 (実装時に詰める)
- eBaymag 自動インポートの**実ラグ実測値** (M3 awaiting_import の閾値設計に必要) → Phase1 で新規1件の取込→discover成功時間を計測。
- キュー消化の cadence (専用 cron vs バッチ相乗り) と CDP probe 方法。
- 出品上限の国別予算管理 (Phase2は簡易、本格は後続)。
- ROADMAP: W採番して system_improvements.json 登録 (#4=W263 / #5=W264 紐付け)。

## 付録: レビュー履歴
- Codex(gpt-5.5): HIGH5(relist窓/queue冪等/relist継承/discover失敗/Q0)+MED7 → v2反映。
- code-architect(Opus): HIGH3(優先国既存流用/relist窓=金銭逆効果/queue部分index不可)+MED4 → v2反映。SKU規約違反ゼロ・骨格妥当を確認。
- 両者「要修正→Phase1着手GO」(Phase3はcanary実測+user承認後)。
