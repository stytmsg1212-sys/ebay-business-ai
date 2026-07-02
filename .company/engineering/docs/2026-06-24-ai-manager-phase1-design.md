# AI店長 Phase 1 実装設計書（ブループリント）

- 作成: 2026-06-24 / code-architect (Opus 4.8)
- 正本仕様: `.company/engineering/docs/2026-06-22-ai-manager-hearing-minutes.md` §1〜§13
- 上流ブリーフ: `tools/ebay-manager/data/tmp/ai_manager_design_brief.md`(Codex/Fugu合議)
- **実装前に §11 の質問(特にQ1: 既存W183停止合意 / Q2: 既存採用分の扱い)を user 確認必須**

## 1. 概要
W153が`listing_rival_discoveries`に**全件無差別蓄積**するライバル候補を、**ハード除外 → スコア → グレーのみClaude判定**の3段ハイブリッドで「真ライバル/ノイズ/要確認」に自動分類。結果を**採用(is_active)とは独立した`pricing_eligible`フラグ**として`competitor_products`に持たせ、W183自動値下げ(`task_rival_pricing`)の抽出を「`is_active=1 AND pricing_eligible=1`」に絞り、偽ライバル採用→自動追従→損失を構造的に遮断。立ち上げは**Shadowモード**(AI判定を記録するが`pricing_eligible`を立てない=実価格影響ゼロ)で誤判定率を実測。並行してGetItem(§8実証済)で競合のQuantitySold/在庫/評価/国をsnapshot蓄積する器を最小実装(Phase2のキーセラー判定の土台)。

## 2. スコープ
**含む(Phase1)**: ハード除外(国≠JP/JUNK・AS-IS/売切れ/評価稼ぎ)/スコア足切り/グレーのみClaude判定(商品同一性)/構造化JSON保存/3分岐ルーティング/`pricing_eligible`分離+W183抽出変更/Shadow/DDU育つリストのDB化/GetItem snapshot最小蓄積。
**含まない(Phase2+)**: per-productアクション判断エンジン/キーセラー・Best Match実順位/状態ポジショニング値付け/撤退判断/警告ブランド業務運用(結線のみPhase1)/DDU Description自動取得/snapshot消費(販売速度レーダー等)/自動昇格(Shadow→自動はuser合意ボタン)/**Best Offer (値引きオファー) 自動応対 = Phase3 候補** (利益床エンジン共用・提案モードから開始、2026-07-02 総点検⑤-2)。

## 3. 作成/修正ファイル
**新規**: `monitor/rival_classifier.py`(純ロジック3分岐) / `monitor/rival_ai_judge.py`(グレーのみClaude Haiku) / `tasks/task_rival_classify.py`(定時タスク) / `monitor/competitor_snapshot.py`(GetItem蓄積) / `data/dou_blacklist.json`(DDUセラー育つリスト) / tests×3。
**修正**: `monitor/database.py`(migration v81) / `tasks/task_rival_pricing.py:58,141`(pricing_eligibleゲート) / `monitor/lowest_price.py:311`(手動採用もdefault 0) / `monitor/task_execution_log.py:48`(TASK_SCHEDULE) / `config/schedule_config.json`(rival_classify block) / `daily_scheduler.py`(dispatch結線) / `tabs/tab_lowest_price.py`(AI判定列+eligibleトグル) / `tools/ebay-manager/CLAUDE.md`(vero_brands.json dangling訂正)。

## 4. DB変更(migration v81・冪等性必須・db-migration-rules準拠)
- `competitor_products`にALTER: `pricing_eligible INTEGER DEFAULT 0`(採用と値下げ適格の分離=最重要安全策、既存行default0=Shadow安全側) + `pricing_eligible_set_by/at` + `ai_classification` + `ai_confidence`。
- 新規`rival_classifications`(判定ログ=Shadow突合の正本): discovery_id/ebay_item_id/competitor_item_id/classification(real/noise/review)/route/exclude_reason/title_similarity/price_ratio/same_product/variant_risk/ai_condition/confidence/reason/ai_model/shadow_mode/would_be_eligible/created_at(UTC)。JOINは`discovery_id`(=LRD.id)。
- 新規`competitor_snapshots`(GetItem定点観測の器、Phase1は蓄積のみ): competitor_item_id/our_item_id/quantity_sold/quantity_total/quantity_available/seller_feedback_score/seller_positive_pct/seller_country/price_usd/shipping_usd/captured_at(UTC)。
- **SKU規約**: listing識別は全て`ebay_item_id`/`competitor_item_id`。`JOIN ON sku`/`GROUP BY sku`/`UNIQUE(sku)`禁止。
- 冪等性テスト: `init_db()`2回連続でデータ保持をassert。全ALTERは`try/except sqlite3.OperationalError`、実在確認後に`user_version=81`bump。

## 5. コンポーネント設計
- **`rival_classifier.py`(純ロジック・最重要)**: `classify_discovery(signals, dou_blacklist, thresholds) -> ClassifyResult{classification, route, exclude_reason, title_similarity, price_ratio, needs_ai}`。ハード除外(国≠JP/売切れ/JUNK・AS-IS(自社動作品時)/farmer(評価低×価格極端安)/DDU黒)。**安全弁(§3)**: farmerでも実際に売れているsignalあれば除外解除→Phase1はsnapshot未取得時点では保守的に`review`へ(真ライバルを捨てない優先、誤採用はShadowが拾う)。`title_similarity`=token Jaccard+型番一致ブースト(**embedding不使用=K1**)。閾値はconfigから(ハードコード禁止、speculativeパラメータ化もしない)。
- **`rival_ai_judge.py`(グレーのみClaude)**: `judge_rival(...) -> AIJudgeResult{same_product, variant_risk(none/voltage/cable/language/accessory/unknown), condition(NEW/USED/AS-IS/JUNK,§3:eBay condition欄見ない), confidence, reason, ai_model}`。`claude_evaluator`のrate-limit/client/JSONパース流用。**Haiku 4.5**(1件≈$0.007、グレーのみ)。`ai_model`必ずDB保存(Q5)。`max_ai_calls_per_run`上限、超過は`review`+痕跡(Q0)。`variant_risk='language'`は内部フラグのみ(出品文変えない§13.1)。
- **`task_rival_classify.py`(オーケストレーション)**: status='new'読込→signals構築(LRD+自社ebay_listings JOIN ON ebay_item_id)→classify(hard/score確定、needs_aiのみjudge、cap超過は'review')→save_rival_classification→3分岐(noise→rejected積極/real→accepted+competitor_products upsert[Shadowはpricing_eligible=0+would_be_eligible=1記録、本番は高確信のみ=1]/review→new維持)→Discord集約+task_execution_log。**採用保守/却下積極の非対称**。

## 6. データフロー
```
02:30 rival_detection(既存W153) → listing_rival_discoveries(status='new',全件)
03:00 rival_classify(★Phase1本体)
  ├ status='new'+自社ebay_listings JOIN(ebay_item_id)
  ├ classify_discovery → 国≠JP/売切れ/JUNK/DDU黒/farmer明白=noise / 型番一致+価格近+状態近=real / グレー=needs_ai
  │    └ judge_rival(Claude Haiku){same_product,variant_risk,condition,confidence,reason}
  ├ save_rival_classification → rival_classifications(突合ログ)
  └ 3分岐: noise→rejected / review→new維持(既存triage UI) / real→accepted+competitor_products upsert
            ┌Shadow: pricing_eligible=0, would_be_eligible=1
            └本番:   pricing_eligible=1(高確信のみ)
00/06/12/18 rival_pricing_refresh(既存W183,★抽出条件のみ変更)
  └ WHERE is_active=1 AND COALESCE(pricing_eligible,0)=1 ← 偽ライバル遮断。床(lp_min_price)厳守は不変
任意 competitor_snapshot(新規,蓄積のみ) → GetItem → competitor_snapshots(Phase2消費)
```

## 7. ビルドシーケンス(各ステップ後 pytest+code-reviewer HIGH=0、money-direct S3/S4厳格)
1. **S1 migration v81のみ**(列+2テーブル+ヘルパ、冪等性テスト)。この時点で誰も値下げされない(全eligible=0)。
2. **S2 `rival_classifier.py`純ロジック+単体テスト**(除外各条件・スコア境界・3分岐・farmer安全弁)。
3. **S3 W183抽出に`pricing_eligible=1`ゲート追加**(`task_rival_pricing.py:58,141`)+回帰テスト。**=money-direct中核ガードをclassifyより先に**(既存competitor_productsに偽ライバルが居ても全eligible=0で値下げ停止=安全側)。⚠️**既存W183を一旦止める副作用→user事前合意必須**(§11 Q1)。
4. **S4 `rival_ai_judge.py`+Claude結線**(Haiku、test_mode、JSON堅牢パース、cap、痕跡、実API1往復確認)。
5. **S5 `task_rival_classify.py`+scheduler結線+Shadow固定**(4要件、eligible立たない+ログ残るテスト)。
6. **S6 UI**(`tab_lowest_price.py`: AI判定列+Shadowバッジ+eligibleトグル[user専権]、Streamlit再起動+Playwright)。
7. **S7 `competitor_snapshot.py`**(GetItem蓄積の器、低頻度、Phase1は消費しない)。
8. **S8 cascade**(CLAUDE.md dangling訂正+`dou_blacklist.json`初期化)。
9. **検証(Q1 DoD)**: scheduler.log実行確認/DB蓄積+pricing_eligible=0維持/Discord R-11/数日Shadow後would_be_eligible vs user実採用の突合SQL。

## 8. money-direct安全の組込点(§11/§13.7)
- **床=lp_min_price厳守**: 既存`task_rival_pricing.py:443`を一切変えない(K2)。
- **pricing_eligible分離**: v81列+`task_rival_pricing.py:58,141`に`AND pricing_eligible=1`+`lowest_price.py:311`手動採用もdefault 0。
- **Shadow=提案止まり**: `shadow_mode=true`時はrealでもpricing_eligible=0固定、would_be_eligible=1のみ記録→W183は1件も追従しない。
- **採用保守/却下積極の非対称**: real自動採用閾値を高く、noise積極reject、曖昧は全部review。
- **fail-closed/abstain**: AI error/データ不足/国不明はrealにしない(review据え置き)。pricing_eligible=1になる経路をコードに作らない。
- **Q0**: cap超過/例外はreview+log+Discord、success偽装しない、必ず痕跡。
- **⚠️ 2026-07-02 user 改訂 (実装必須)**: ①値下げは 1 回 5% 上限 (lp_min_price 床に加える第 2 の安全弁)、②同一 ebay_item_id が連続 3 回値下げされたら Discord アラート (値下げ合戦スパイラル検知、ライバルも自動値下げシステム使用想定)。適用先 = 既存 W183 `task_rival_pricing.py` の実行経路 (Phase1 の Shadow とは独立に、稼働中の W183 保護として先行実装候補)。
- **kill switch**: `rival_classify.enabled=false`で停止、W183は独立停止可。
- **二層防壁**: pricing_eligibleゲート(S3先行投入)+Shadow(eligible立てない)。Shadow解除はuser合意ボタンで`shadow_mode=false`(Phase1はShadow固定、自動昇格しない)。

## 9. 既存コードとの結線(file:line)
- W153供給元: `database.py:4317 record_rival_discovery`/`listing_rival_discoveries`(L2440) — 読むだけ
- W183値下げ抽出(**改変**): `task_rival_pricing.py:58,141`
- 採用書込: `lowest_price.py:311 upsert_listing_competitors`
- Claude手法流用: `claude_evaluator.py:62,83,391,552`
- GetItem: `ebay_client.py:261 get_item_details_batch`/`_call_trading_api`(L1298) ※`_test_competitor_sold.py`が抽出実証済
- scheduler登録: `task_execution_log.py:47-48`/`daily_scheduler.py`/`config/schedule_config.json:143`
- triage UI: `tab_product_management.py:3468`/`tab_lowest_price.py:2452`
- Discord: `task_rival_pricing.py:648`パターン流用
- cascade: `tools/ebay-manager/CLAUDE.md` VeRO行

## 10. リスク
- **既存W183への影響(最大)**: S3でゲート投入の瞬間、既存active competitor_products(全default0)が全て値下げ対象外に。設計意図(Shadow立上げ)だが現在自動値下げ稼働中なら一時停止→**S3前にuser合意必須**。Shadow期間=値下げ実質停止と理解の上で。
- **classify誤判定で真ライバルをnoise reject**: 機会損失。Shadowでは値下げ影響なし。farmer安全弁+曖昧review、rejectも痕跡残し復活可能。
- **AIコスト**: グレーのみHaiku≈$0.007/件。cap+score足切りで絞る、Shadowで実測。
- **国判定**: Browse location vs GetItem Countryの食違い。ハード除外の国判定は保守的(JP確定でないものを除外しない)、確実判定はGetItem Country(snapshot)に委ねる。
- **移行整合**: 既存採用分はv81で全default0→Shadow整合。classifyはstatus='new'のみ対象で過去分は自動再判定しない→§11 Q2。

## 11. 質問(K0・設計判断の不確実点)
1. **S3ゲート投入で既存W183自動値下げが一時停止**する点の合意。現在W183稼働中か?(MEMORYでは$1M上限でdaily_relist停止記述あり、値下げ現況要確認)。Shadow期間=値下げ実質停止で問題ないか。
2. **既存competitor_products active採用分(過去手動採用)の扱い**: (a)全eligible=0でclassify再判定待ち / (b)過去採用分はeligible=1温存し新規のみShadow / (c)一括再classify one-shot。§13.7に最忠実は(a)。
3. **自動採用confidence閾値の初期値**: same_product=True かつ confidence≥0.85=real自動採用、0.6-0.85=review、未満=noise で妥当か(Shadow実測調整前提)。
4. **classify cron時刻**: rival_detection 02:30、03:00で良いか(thread-local batch ctx制約留意)。
5. **DDUブラックリスト判定タイミング**: Phase1は手動追記リスト一致のみ(自動疑い検知=評価1000以上×安値×関税無し送料、Description検証はPhase2)で良いか。
6. **警告ブランドwatchlist**: Phase1はdangling訂正+Holbein第1号まで、撤退連動(§6即削除)はPhase2で良いか。

## 12. 完了判定(DoD)
pytest3新規+全体回帰PASS / 冪等性(init_db2回でデータ保持) / DB SELECT(pricing_eligible全0維持・rival_classifications蓄積・would_be_eligible記録) / W183検証(eligible=0を0件返す実クエリ) / scheduler.log(rival_classify記録・silent skipなし) / Discord R-11実視認 / Streamlit+Playwright(AI判定列・Shadowバッジ・トグル) / Shadow数日後would_be_eligible vs user実採用の突合(誤採用率) / code-reviewer HIGH=0 + money-direct部(S3/S5)はCodex2段。
