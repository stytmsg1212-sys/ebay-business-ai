# W258: MonoDeck 全タブ UI 刷新 設計書 v2

- 作成: 2026-06-11 / 設計: Fable 5 (main agent、user 指示「設計・チェックは fable5」)
- v2 改訂: 2026-06-11 Codex 2 段レビュー反映 (HIGH 2 / MED 5 / LOW 2 採用、詳細 §7) + Phase A as-built 反映
- 実装: Sonnet 4.6 subagent (フェーズ別 dispatch)
- ROADMAP: id=329 / tag=W258 / 優先度 高

## 1. user 要求 (2026-06-10 verbatim 要約)

| # | 要求 | 設計上の解釈 |
|---|---|---|
| R1 | ボタンや画面の遷移がイケていない。誰でも直感操作できる UI に「ガラッと変えて」 | ナビと作業導線の刷新。見た目だけでなく「次に押すべきボタンが迷わず分かる」状態 |
| R2 | 毎日の作業がサクサク | 毎日タブ 5 つ (DASHBOARD/商品管理/在庫監視/仕入先候補/入荷確認) の操作ステップ数削減 |
| R3 | 一覧がコンパクト=空白減・情報量最大 | 密度 CSS の強化 + カード/テーブルの再設計 |
| R4 | ボタン内文字列の重複 ("ieck"/"heck") 根治 | 真因特定済 (§3.1)。グローバル 1 箇所修正 |
| R5 | スマホでも使用しやすく | レスポンシブ CSS レイヤー新設 (§3.2) |
| R6 | eBay 1枚目画像と仕入先 1枚目画像を並べて比較し、短時間で採用/不採用判断 | 画像比較カード (§3.4)。**本件の最重要価値** (毎日の金銭判断を高速化) |

## 2. 設計原則 (K0-K3 との両立)

- 「ガラッと変えて」を**全 21 タブ同時書き換え**と解釈しない (一括書換は W178/W180/W181 画像合成 3 連続失敗と同型の事故リスク)。**共通基盤レイヤー (全タブに即効) + 毎日タブの作業面刷新** の 2 層で「体感はガラッと、変更は外科的に」を実現する
- money-direct path (採用/不採用/eBay Revise/DB 書込) は**一切触らない**。表示・配置・CSS のみ (W212-supplier-card-cleanup と同方針)
- ナビの routing contract (`_w134_sel` / `_w134_navbtn_<page>` / 21 箇所の if 分岐) は**不変** (W217-A v2 契約の継承)
- フェーズごとに user 実機確認を挟む (Playwright 検証 + user 体感、R-11 同型)

## 3. 設計

### 3.1 Phase A: アイコン文字重複の根治 (R4) — 全タブ即効

**真因** (確定):
- `ui_themes.py` L287-294 は Material Symbols の**フォントのみ**無効化 (`@font-face local('__disabled__')` + `.material-symbols-rounded` 非表示)
- Streamlit 新版 (1.56) はアイコンを `[data-testid="stIconMaterial"]` 要素に "check" 等の**生テキスト**で描画 → フォント死亡で生テキスト露出 → ボタン幅で先頭が欠けて "heck"/"ieck" に見える
- `tabs/tab_product_management.py` L4477-4495 に正しい局所 fix が既存 (要素非表示 + expander caret を `::before` の ▶/▼ で代替)。商品管理タブ表示中のみ有効

**修正**: 局所 fix を `ui_themes.py` の `apply_dark_paper_theme()` へ昇格 (グローバル化):

```css
/* Material icon 生テキスト露出の根治 (W258 / 2026-06-11) */
[data-testid="stIconMaterial"] { display: none !important; }
[data-testid="stExpander"] details summary::before {
    content: '▶'; display: inline-block; margin-right: 0.6em;
    font-size: 0.85em; font-weight: 700; transition: transform 0.15s;
}
[data-testid="stExpander"] details[open] summary::before { content: '▼'; }
```

- 既存 L287-294 の `@font-face` ブロックは**残す** (フォント DL 自体の抑止として有効、競合しない)
- `tab_product_management.py` の局所 fix は**残置** (同一宣言の重複は無害、削除は K2 scope 外。コメントで「グローバル化済 W258」追記のみ)
- リスク: st.status / st.toast 等のアイコンも消える → 商品管理タブで 1 ヶ月運用済みの実績があり、表示崩れ報告なし。許容

### 3.2 Phase A: スマホ対応レイヤー (R5) — 全タブ即効

`ui_themes.py` に media query レイヤーを新設 (apply_dark_paper_theme 内、密度 CSS の後):

```css
@media (max-width: 640px) {
    /* タッチターゲット 44px (iOS HIG)。デスクトップ密度 34px を上書き */
    [data-testid="stButton"] > button, ... { min-height: 44px !important; font-size: 14px !important; }
    /* メイン余白を最小化 */
    .main .block-container { padding-left: 0.6rem !important; padding-right: 0.6rem !important; }
    /* dataframe / table は横スクロール */
    [data-testid="stDataFrame"] { overflow-x: auto !important; }
}
```

**ナビのモバイル化** (app.py の W217-A CSS に追記):

```css
@media (max-width: 640px) {
    /* ページボタン行: 折返し縦積みでなく横スクロール 1 行 (親指スワイプ) */
    [data-testid="stHorizontalBlock"]:has([class*="st-key-_w134_navbtn_"]) {
        flex-wrap: nowrap !important; overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
    }
    /* sticky nav はモバイルでも維持 (常に脱出可能) */
}
```

- K0 仮定: Streamlit columns のモバイル挙動 (自動縦積みの有無) は version 依存 → **Playwright viewport 390x844 で実測してから微調整** (設計段階で断定しない)
- 画像比較カード (§3.4) は st.columns を使わず **自前 flex HTML** で組む = モバイルでも必ず左右並びを維持 (比較が本質のため縦積み禁止)

**Phase A as-built (2026-06-11 実装完了、commit a98e266)**:
- CSS だけでは不足だった: app.py のナビが Python 側で「最大 4 列/行」にチャンク分割していたため物理 2 行になり、横スクロール 1 行が成立しない (5 ページ目の仕入先候補が画面外消失)。**チャンク分割を廃止し全ページを単一 columns 行に変更** (列幅は既存 pill CSS flex:0 0 auto が担保、デスクトップ 7 ページカテゴリで崩れなし実測)
- カテゴリ segmented_control も `[role="radiogroup"]` が flex-wrap:wrap → nowrap + overflow-x:auto + ボタン flex-shrink:0 (省略表示「★ …」を防止)
- selector 知見: Streamlit は日本語ボタン key の st-key class を全て `-` にサニタイズ (`st-key-_w134_navbtn_----`) するため、日本語ページの DOM 特定は**ボタン innerText 一致**が唯一の安定手段 (Playwright 検証もこの方式)
- 実測結果: 毎日 5 タブ巡回で icon 可視 0 / icon 名リーク 0 / stException 0、モバイル 390x844 で nav scrollW 551>clientW 336 (スクロール成立)・ボタン高 44px・body 横はみ出し 0px、デスクトップ 1280px で 7 ボタン 1 行 (bbox top 一致)

### 3.3 Phase A: 一覧コンパクト化 (R3) — 全タブ即効

- W217-C 密度 CSS (app.py L110-185) を維持しつつ追加: `st.divider` の margin 20px→10px / expander 内 padding 詰め / `stDataFrame` 行高 compact
- 毎日タブの「1 画面あたり可視件数」を Phase D の per-tab 改善で個別に上げる (テーブル列の優先順位整理、不要 caption の削減)

### 3.4 Phase B: 画像比較カード (R6) — 本丸

**目的**: 仕入先候補レビューと在庫監視 (在庫切れ→置換候補) で、eBay 出品の 1 枚目画像と仕入先候補の 1 枚目画像を**左右並び**で見せ、「同じ商品か」を 2-3 秒で判断 → 採用/不採用。

**現状の欠落**:
- `ebay_listings.ebay_image_url` (v63) は active 506 件中 **127 件のみ** → backfill 必要
- `supplier_candidates` に**画像列なし** → migration 必要
- 現レビュー画面は eBay/仕入先のページを別タブで開いて目視比較 (user の不満点)

**DB 変更 (migration v71、Q2 冪等)**:

> ⚠️ v2 改訂 (Codex H1): 当初案の「v67」は **W228 `research_candidates` が使用済み** (database.py:3121)、現行 DB は user_version=70。v67 のまま実装すると `schema_ver < 70` 条件で ALTER ブロックがスキップされ**列が永遠に追加されない silent skip (Q0/Q2 級)** になる。次番 = **v71**。

```sql
ALTER TABLE supplier_candidates ADD COLUMN candidate_image_url TEXT;
ALTER TABLE supplier_candidates ADD COLUMN candidate_image_fetched_at TEXT;  -- 取得時刻 (UTC)
```

実装パターン (Codex H2): **v69 ブロック (database.py:3279-3337、research_candidates の同型 ADD COLUMN) を雛形に流用**。各 ADD COLUMN を `try/except sqlite3.OperationalError` で個別ラップし、全列の `PRAGMA table_info` 存在確認後にのみ `user_version = 71` を bump。検証 = init_db 2 回連続実行でデータ保持 + **部分 migration 状態 (1 列だけ存在) からの再実行でも残列が追加される**こと (L2)。

**画像の取得経路 (3 系統、追加 HTTP リクエストほぼゼロ)**:
1. **新規候補**: supplier_evaluate / supplier_sweep が候補評価時に既にページを scrape している → `scrape_supplier_url()` の戻り値 `product.image_urls[0]` を `add_supplier_candidate()` 経由で保存 (関数シグネチャに `candidate_image_url` 追加)
2. **既存 pending 108 件**: one-shot backfill script (`scripts/backfill_candidate_images_2026_06_11.py`)。既存 scraper で 1 件ずつ取得、rate limit 尊重 (ドメイン毎 2s sleep)、Q2 6-step (dry-run 既定 / snapshot / 1件試行)
3. **eBay 側 379 件**: 取得経路は **`monitor/ebay_image_fetcher.py::_api_image_urls`** (GetItem→PictureURL 正規表現抽出、2026-06-05 raw キー修正済) を再利用 (Codex M5: 当初案の「v63 の fetch 実装」は曖昧 — v63 は列追加 migration であって fetcher ではない)。one-shot は **resume state 必須** (取得済 ebay_item_id を逐次記録し中断後再開可) + 100 件/batch + 成功/失敗/quota カウントをログ出力

**カード設計** (`tabs/_supplier_card_html.py` 拡張、純関数原則維持):

```
┌─ sc-card ──────────────────────────────────────────┐
│ score 82 │ ¥12,800 │ 利益 +¥4,210 (24%) │ メルカリ │   ← 既存 row1 (不変)
│ ┌─────────────┬─────────────┐                       │
│ │ [eBay 1枚目]  │ [仕入先1枚目] │  ← 新設 sc-imgpair    │
│ │  eBay $189    │  ¥12,800     │     高さ 150px 固定   │
│ └─────────────┴─────────────┘     クリックで原寸へ    │
│ タイトル / match_reasoning (既存)                      │
└────────────────────────────────────────────────────┘
  [採用] [不採用]   ← 既存 Streamlit ボタン (money-direct 不変)
```

- `sc-imgpair`: `display:flex; gap:8px;` の自前 HTML。各画像 `max-height:150px; object-fit:contain; loading="lazy"`、`<a href=原寸 target=_blank>` ラップ
- 画像欠落時: 灰色プレースホルダ + 「画像未取得」(silent skip 禁止 = 欠落を明示)
- モバイル: flex のまま左右維持 (各 ~45vw)。比較が目的なので縦積みにしない
- 在庫監視タブの `_render_oos_block` (置換候補インライン) にも同カードを適用

### 3.5 Phase C: ナビ・遷移の直感化 (R1, R2)

- 上段カテゴリ + 下段ページの 2 段ナビ (W217-A v2) は**構造を維持** (routing contract 不変) し、以下を改善:
  1. **「★ 毎日」を常時表示**: 毎日 5 タブだけはカテゴリ切替なしで常にワンタップ到達 (上段右側に固定 pill 群 or 下段 2 行化)。毎日の作業開始が常に 1 タップ
  2. **バッジ表示**: 仕入先候補 (pending 件数) / 在庫監視 (要対応件数) / 入荷確認 (未確認件数) をナビボタンに数字バッジ表示 → 「どこに作業が溜まっているか」が見ただけで分かる。DB count は `ui_cache` の ttl=3s cache に乗せ、ナビ描画を遅くしない
  3. アクティブページの視認強化は既存 (青 accent) を維持
- 遷移の直感化: 各毎日タブの先頭に「今日やること」サマリ行 (件数 + 主要ボタン) を統一フォーマットで置く (Phase D で per-tab 適用)

### 3.6 Phase D: 毎日タブの per-tab 磨き込み (R1-R3、後続反復)

優先順: 仕入先候補 → 在庫監視 → 商品管理 → DASHBOARD → 入荷確認。各タブ 1 リリース単位で user 体感 feedback を取りながら反復。本設計書では枠のみ定義し、詳細は各回の小設計で確定 (一括 big-bang をしない)。

## 4. 実装フェーズと検証 (Q1)

| Phase | 内容 | 触るファイル | 検証 |
|---|---|---|---|
| A | アイコン根治 + モバイル CSS + 密度追補 + ナビ単一行化 | ui_themes.py / app.py | **完了 (a98e266)**: Playwright 毎日 5 タブ巡回 (icon 可視 0 / "heck"・"ieck" 等リーク 0 / 例外 0) + モバイル 390x844 bbox 実測 (44px / スクロール成立 / はみ出し 0) + デスクトップ 1280px 7 ボタン 1 行 + スクショ目視 |
| B | migration **v71** + 画像取得結線 + backfill 2 本 + 比較カード | database.py / supplier_evaluate 経路 / _supplier_card_html.py / tab_supplier_candidates.py / tab_inventory_monitor.py / scripts 2 本 | 冪等テスト (init_db 2 回 + 部分 migration 再実行) + pytest + **Playwright で実 CDN 画像が Streamlit ページ内 <img> に実表示されることを目視 (Codex M4 PoC)** + DB SELECT (画像 URL 件数) |
| C | ナビ改善 (毎日固定 + バッジ) | app.py ナビ部 | Playwright 遷移 E2E + バッジ数値と DB count の一致 |
| D | per-tab 磨き込み (反復) | 各 tab_*.py | 各回 Q1 |

- 実装は各 Phase を Sonnet 4.6 subagent に dispatch、終了ごとに Fable 5 がチェック + code-reviewer HIGH=0 ループ
- Phase A/B/C 完了ごとに Streamlit 再起動 + user 報告 (progress-touchpoint)

## 5. 明示的に「やらないこと」 (K1/K2)

- 21 タブ全コードの一括書換 / framework 移行 (React 化等) — 事故リスク過大
- routing contract (`_w134_sel`) の変更
- money-direct path (採用/不採用/Revise/DB 書込ロジック) の変更
- テーマ配色の全面変更 (Dark Paper theme は user 既承認の世界観、維持)

## 6. リスクと対策

| リスク | 対策 |
|---|---|
| stIconMaterial 全消しで予期せぬアイコン消失 | 商品管理タブで ~1 ヶ月運用実績あり。Playwright 全タブ巡回で目視枠確認 |
| eBay GetItem backfill 379 件の API quota | 分割実行 (100 件/batch) + 失敗時は欠落明示で続行 (Q0) |
| 仕入先画像 URL の期限切れ (メルカリ CDN 等) | 表示時 onerror でプレースホルダ fallback。再取得ボタンは Phase D 候補 |
| 仕入先/eBay 画像 CDN の hotlink 制限で <img> 表示不可 | **実証済み解消 (2026-06-11)**: i.ebayimg.com / static.mercdn.net / auctions.c.yimg.jp の 3 CDN とも Referer なし・`http://localhost:8501/` Referer ありの両方で 200 + image/jpeg を返却 (httpx 実測)。URL 直接 <img> 参照方式で問題なし |
| モバイル CSS が Streamlit update で崩れる | セレクタは data-testid + `st-key-*` (user 指定 key 由来) に限定。⚠️ v2 訂正 (Codex M2): 当初の「クラス名ハッシュ非依存」は不正確 — data-testid も st-key も**公開安定 API ではない** (内部ハッシュよりは安定、程度)。Streamlit version 更新時は Playwright 巡回 scan を再実行する運用で担保。基点は app 所有コンテナ (`.st-key-_w217a_navbar` / `.st-key-_w134_nav_group`) 配下に限定 |

## 7. Codex 2 段レビュー採否記録 (2026-06-11)

| # | 指摘 | 採否 | 反映先 |
|---|---|---|---|
| H1 | migration v67 は W228 使用済 (現行 v70) → silent skip | **採用 (最重要)** | §3.4 v71 改番 + §4 表 |
| H2 | ALTER の Q2 冪等パターン未規定 (hook BLOCK 対象) | 採用 | §3.4 v69 雛形流用を明記 |
| M1 | stIconMaterial は非公開 testid | 採用 (partial) | Phase A 検証で実在確認済 (iconTotal>0 / display:none 有効) |
| M2 | selector の「ハッシュ非依存」主張と実装の矛盾 | 採用 | §6 リスク表訂正 |
| M3 | モバイル検証が screenshot 目視止まり | 採用 | Phase A で bbox 実測実施済 (§3.2 as-built) |
| M4 | 仕入先 CDN の hotlink/Referer ブロックで全件プレースホルダ化 | 採用 (観点) → **httpx 実測で 3 CDN とも 200 を確認し懸念は大幅緩和** (§6)。Playwright 実表示 PoC は Phase B 検証に残す | §4 Phase B 検証 |
| M5 | GetItem backfill の経路曖昧 + resume なし | 採用 | §3.4 系統 3 具体化 |
| L1 | `_supplier_card_html.py` 不在 | **却下** (実在確認済、Codex 側 Stage-1 の前提誤り) | — |
| L2 | "heck"/"ieck" 両方 grep + 部分 migration 再実行テスト | 採用 | §3.4 / §4 |
| L3 | 画像欠落理由の区別列 (`candidate_image_status`) | **見送り (K1)** — hotlink 懸念が実測で緩和され、欠落は「画像未取得」プレースホルダ明示で足りる。運用で再取得ニーズが出たら Phase D で列追加検討 | — |
