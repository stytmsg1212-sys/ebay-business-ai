# 夜間オーケストレーション計画 — MonoDeck 100点化 (2026-06-03 夜)

user 指示: 「codexと100点満点の使いやすいmonodeckに。商品管理に注力したが他タブも同様に。**在庫監視 / 仕入先候補 / 今日の発掘 / 個別出品** を最優先で」。user 就寝中の自律作業。

## 鉄則 (money-direct 安全)
- **表示・配置・CSS のみ**変更。保存ロジック / 競合データ / dirty-flag / DB書込 / 計算式 は各タブで**一切触らない**。
- 各変更: code-reviewer HIGH=0 → pytest tests/ 全green → Q1(Playwright実機: 描画・クラッシュ無・主要データ保持) を通す。
- 壊すリスクある箇所は**実装せず設計提案に留め**朝 user 確認へ。無断の money 数値変更(関税/手数料/価格)は禁止。
- 1ファイル1エージェント (同時編集による競合回避)。

## デザイン言語 (商品管理で確立した「100点」基準)
- 採算パネル方式: `st.markdown(unsafe_allow_html=True)` で表示部を純HTML描画 (2軸/フォント強弱/3色背景 OK緑/警告黄/NG赤/折りたたみ)。
- 金額枠: `st.container(border=True, key=...)` + CSS (amber左ライン)。入力欄はCSSでコンパクト化。
- 上部ナビ (Codex設計): カテゴリ `segmented_control` + 選択カテゴリのページのみ表示。
- mockup: `2026-06-03-product-management-w217-mockup.html` / `2026-06-03-w217-nav-and-rival-mockup.html`。

## フェーズ進行 (完了通知ドリブン)

### Phase A: 在flight (完了待ち)
- [進行中] W217-A HIGH修正 (agent a11bbedaca5dbdfd9): streamlit pin / 競合保持テスト → 完了後 **私がQ1**(ナビ到達+競合保持)。
- [進行中] eBay Finances 実手数料分析 (agent a38695f543836feeb): 読み取りAPI+突合レポート → 完了後 **私が乖離レビュー**。※手数料較正(money変更)は朝 user 承認まで保留、分析と保守的提案のみ。

### Phase B: 商品管理 100点 仕上げ (app.py/tab_product_management.py が空き次第)
- B1a: 上部ナビ (Codex segmented_control 2段=カテゴリ切替+選択ページのみ) → user承認済。app.py 実装→review→Q1。mockup: `2026-06-03-w217-nav-and-rival-mockup.html` 上部。
- B1b: **ライバル集約 (user 2026-06-03夜 修正指示)**: 全幅パネルにしない。**元mockup `2026-06-03-product-management-w217-mockup.html` 通りの2列レイアウト維持** = 左:編集ゾーン / 右:🎯ライバル(監視設定+登録済を**1パネルに集約**, 監視設定→登録済テーブル🥇→競合差→編集folds→検出済fold)を**編集ゾーンの横(右列)**に配置。幅は右列幅(全幅にしない)。tab_product_management.py 実装→review→Q1(競合保持必須)。
- B2: W218 利益内訳に US向け送料 (total_shipping) 追加 → 実装→review。

### Phase C: 4タブ 100点化 (逐次・1ファイルずつ)
対象: tabs/tab_inventory_monitor(在庫監視) / tab_supplier_candidates(仕入先候補) / tab_morning_discovery?(今日の発掘) / tab_individual_listing(個別出品)。
各タブ:
1. C-design: code-architect が現状の散らかりを分析+デザイン言語適用の blueprint (read-only、do-not-touch ロジック特定)。
2. C-impl: generator が 表示/CSS/配置のみ実装 (logic不変)。
3. C-review: code-reviewer HIGH=0 + pytest。
4. C-Q1: 私が Playwright 実機 (描画/クラッシュ無/主要データ保持) + DB確認。

## 既存キュー (並行)
- W219 手数料較正 (分析後・money-direct=朝 user 承認)。
- W215 R-11 (今夜02:30 関税ダイジェスト着弾確認)。

## 夜間 進捗ログ (2026-06-03夜〜06-04)
- ✅ W217-A HIGH修正 (a11bbedaca5dbdfd9): streamlit pin 1.32→1.56 (st.container(key=)は1.36+) / 競合保持回帰テスト2件 / 1842 pass。
- ✅ eBay手数料分析 (a38695f543836feeb): Finances API成功(1073件)。**主較正点=FVF実績が予測より+8.5%** (EbayFeeRates.csv要再検証 or Top Rated Plus 10%割引未適用 or FINAL_VALUE_FEE_FIXED_PER_ORDER欠落)。AD gapは分母ズレの見かけ・実2.22%≒予測2.0%でOK。INTL 1.28%≒1.2%でOK。Payoneerは Finances不可視(別経路)。外れ値 BMUD200-A 95%=要調査(返金/紛争)。**較正は朝承認待ち**。doc: 2026-06-03-ebay-actual-fees-analysis.md。
- ✅ 4タブ統一設計 (aadd45fdcdb1def1e): 色系統は保守案(各タブ本文美学維持・採算2軸+pm-pillのみ寄せ)。質問4点(色統一範囲/共通CSS scope/在庫監視2パラダイム/mockup新パレット)は朝確認。
- ✅ 今日の発掘実装 (a99502cb99c17abfb): _render_discovery_economics_html(採算2軸)・W129見積不能保持・1855 pass・self-contained。
- ⏳ 個別出品実装 (a2d371f5ba2c211f4): STEPラベルDRY+課金ゾーン隔離。進行中。
- ⏳ 商品管理 nav+ライバル集約 (a27d4fa00177e1eb3): segmented_control 2段 + 右列2列集約。進行中(app.py編集中)。
- ⬜ 在庫監視 / 仕入先候補: app.py直列のため商品管理nav完了後に逐次dispatch。
- **Q1検証戦略**: 全実装完了→Streamlit再起動→Playwrightで全タブ render-no-crash + 商品管理 nav全カテゴリ到達 + 競合保持 を一括検証。code-reviewerは高リスク(商品管理nav+rival/app.py 2タブ)に投入。

## 夜間 最終結果 (06-04 朝)
**5タブ全実装+nav完了・Q1全PASS**:
- ✅ 商品管理: nav刷新(segmented 2段) + ライバル集約(右列2列)。
- ✅ 在庫監視: サマリバー + 再出品待ちexpander + scoreバッジ。
- ✅ 仕入先候補: 採算2軸カード + 閾値expander。
- ✅ 個別出品: STEPラベルDRY + 課金ゾーン隔離。
- ✅ 今日の発掘: 採算2軸 + W129見積不能保持。
- **pytest 1912 pass** / 各generator do-not-touch非空白差分ゼロ / **Q1実機(Streamlit再起動+Playwright): 全タブrender-no-crash・nav全カテゴリ到達・NV-25競合保持=PASS・0 errors**。
- ✅ code-reviewer (a67676ffb2f410874): **HIGH=0 (100点)**。6変更全てmoney-direct不変を独立照合・nav routing等価(21分岐1:1)・XSS安全・columnネストOK。
  - 朝対応の軽微LOW: (a) `_supplier_card_html.py` model badge "Opus 4.7"固定→4.8表示修正 (b) `tab_morning_discovery._render_candidate` name/rationale escape欠落(既存・別W)。

## 朝の user 確認事項 (要判断)
1. **eBay手数料較正**: FVF実績が予測+8.5% → EbayFeeRates.csv再検証 or Top Rated Plus 10%割引 or 固定fee追加。**較正適用は承認後**(表示利益が動く)。
2. **BMUD200-A 手数料95%**: 返金/紛争の可能性 → 原因調査。
3. **4タブ色系統**: 保守案(各タブ本文美学維持・採算2軸のみ寄せ)で実装済。全面pm統一にするか確認。
4. **W215 R-11**: 今朝02:30バッチで関税Discordダイジェスト着弾したかDiscord確認。
5. 全変更は未コミット(branch)。commit可否はuser判断。

## 朝の報告予定
- 各タブの before/after、HIGH=0、pytest、Q1結果、未実装(設計提案止まり)項目を Q5 テンプレで。
