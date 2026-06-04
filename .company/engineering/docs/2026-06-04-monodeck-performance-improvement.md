# MonoDeck パフォーマンス改善 設計ドラフト (2026-06-04)

出典: user 「各処理がかなり遅い。サクサク動かしたい。まず Chrome(現Streamlit)前提で改善、大きく無理ならアプリ化も検討。Codex と協働で」。Claude (Opus 4.8) ドラフト → Codex レビュー → 統合。

## 0. 前提・計測

- Streamlit **1.56.0** (st.fragment / st.navigation 利用可)。
- 規模: `app.py` 約 **7,000 行・21 タブ単一スクリプト** (`if _w134_sel == "..."` 分岐)。商品管理 listing 約 **530 件** (ページ 25 件)。
- ⚠️ **本ドラフトは静的コード分析ベース**。実 wall-clock 計測 (どの操作が何 ms か) は未取得 = 最初に計測すべき (下記 Phase 0)。

## 1. 遅さの構造的主因 (Streamlit の本質 + 実装)

### A. Streamlit の rerun モデル (フレームワーク本質)
どの widget を 1 つ操作しても **スクリプト全体が top→bottom で再実行**される。これが体感遅延の根本。緩和は「再実行範囲を狭める (fragment)」「再実行コストを下げる (cache/描画量削減)」の 2 方向。

### B. 商品管理タブが st.fragment 未使用 (最大の実装要因)
- `tabs/tab_product_management.py` に `st.fragment` が **1 箇所も無い** (fragment は app.py の仕入先候補/個別出品でのみ使用)。
- 結果: ページ内 **25 商品**を表示中、1 商品の入力/ボタン操作で **25 商品すべての render が再実行** (各商品で profit breakdown 2 回呼び・BP state・hero metrics・rival section)。
- さらに Streamlit は **折りたたみ expander の body も毎 rerun 実行**する (コード内コメントで認知済) → 閉じた商品も計算が走る。

### C. 単一巨大スクリプト (app.py 7000 行・21 タブ)
- 毎 rerun に import + module-level コードが評価される。分岐 body は該当 1 つのみ実行だが、top-level の準備コスト + 1 ファイルの parse は常時。
- st.navigation 多ページ化で「アクティブページのコードのみ load」にでき、初期 load と rerun が軽くなる余地。

### D. per-rerun の重い処理
- DB: `_fetch_all_products` は `@st.cache_data` + db_version key 化済 (◯)。
- profit breakdown: `@st.cache_data(ttl=3)` 済だが ttl=3 秒で頻繁に miss し得る + 商品ごと 2 回 (DDP/DDU) calculate。
- eBay GetItem: per-render は廃止済 (BP は DB 駆動) (◯)。

## 2. 改善案 — Tier 1: Chrome/Streamlit 現状維持 (高ROI・低リスク)

| # | 施策 | 効果 | リスク/工数 |
|---|------|------|------|
| **T1-1** | **商品管理の 1 商品 render を `@st.fragment` 化** | ★最大。1 商品の操作が他 24 商品を rerun しない = 体感激変 | 中 (form/submit と fragment scope の整合確認要)。money-direct UI なので Q1 必須 |
| **T1-2** | **折りたたみ商品の body 計算を skip** (開いている商品だけ profit/BP/rival を計算、閉は header のみ) | 大。25→開いてる1-2件分の計算に | 中 (expander は閉でも body 実行されるため、`pm_keep_open_eid`/明示 state で「開いてる時だけ重処理」guard) |
| **T1-3** | profit breakdown の cache 強化 (ttl 撤廃し db_version + settings_mtime + 入力 hash のみを key に) | 中。ttl=3 の頻繁 miss を解消 | 低 |
| **T1-4** | ページサイズ既定を 25→10 + 「もっと見る」 | 中。初期描画 widget 数減 | 低 |
| **T1-5** | 重い tab (在庫監視 data_editor 等) の遅延描画 (subtab/expander 内に隔離) | 中 | 低 |
| **T1-6** | 画像 (hero/合成) の lazy load + cache | 中 (画像が多い tab) | 低 |

### Tier 1 の期待
商品管理の体感遅延は **T1-1 (fragment) + T1-2 (閉body skip)** で大幅改善見込み (推定: 1 操作の処理量を 25 商品分→1 商品分へ ≒ 1/25 オーダー)。**まず Phase 0 計測 → T1-1 から段階導入し各段で実測。**

## 3. 改善案 — Tier 2: Streamlit 構造改革 (中ROI・中リスク)

| # | 施策 | 効果 | リスク/工数 |
|---|------|------|------|
| **T2-1** | `st.navigation` / 多ページ化で app.py 7000 行を tab 別ファイルに分割 | 中。アクティブページのみ load = 初期/rerun 軽量化 + 保守性 | 大 (21 分岐の機械移植・W134 contract 影響)。要設計 |
| **T2-2** | DB を `@st.cache_resource` の単一 connection / WAL read 最適化 | 小〜中 | 低 |
| **T2-3** | `st.connection` / クエリ結果の TTL cache 統一 | 小 | 低 |

## 4. 改善案 — Tier 3: アプリ化 (大ROI だが大工数 / 要判断)

⚠️ **重要な事実**: 「Streamlit を Electron/webview で包むだけ」のアプリ化は **rerun モデルの遅さを解決しない** (中身は同じ Streamlit エンジン)。体感速度は Tier 1/2 とほぼ同じ。アプリ化が速度に効くのは **フロントを置き換える**場合のみ。

| 選択肢 | 速度 | 工数 | 備考 |
|---|---|---|---|
| **A. Streamlit を desktop 化** (stlite / Electron wrap / pywebview) | △ (Streamlit と同等) | 小 | 配布/常駐は楽になるが**速度改善はほぼ無い** |
| **B. フロント全面置換** (React/Tauri or Next.js + FastAPI backend) | ◎ (native 級) | 特大 | 既存ロジック (calculator/eBay/DB) は backend 再利用可。UI は全書き直し。数週間規模 |
| **C. 別 Python UI framework** (NiceGUI / Reflex / FastUI) | ○ | 大 | rerun モデルでない分速いが移植コスト大 |

**推奨スタンス**: **まず Tier 1 を実施して体感を測る**。Tier 1 で十分サクサクになれば アプリ化不要。なお足りなければ Tier 2 (多ページ化) → それでも不足なら Tier 3-B (本格 backend+frontend 分離) を W 番号付き大型機能として設計。Tier 3-A (単純 wrap) は速度目的では非推奨。

## 5. 推奨ロードマップ

1. **Phase 0 (計測)**: 主要操作 (商品管理ページ表示 / 1 商品編集→保存 / タブ切替) の wall-clock を実測 (Playwright + console timing or st 内 timer)。"遅い"を数値化。
2. **Phase 1 (Tier 1)**: T1-1 fragment → T1-2 閉body skip → T1-3 cache。各段 Q1 (Streamlit+Playwright) で実測比較。
3. **Phase 2 (再評価)**: Tier 1 後の体感を user 判定。十分なら終了。
4. **Phase 3 (不足時)**: Tier 2 多ページ化 or Tier 3-B backend 分離を W 設計。

## 6. Codex への質問 (協働ポイント)
- T1-1 (fragment) と既存 `st.form` (submit まで rerun 抑制) の**二重 rerun 抑制の整合**: form + fragment を同一商品ブロックで併用する際の落とし穴は?
- T1-2 「閉 expander の body skip」を Streamlit で安全に実現する idiom (expander は閉でも body 実行される前提で、どう計算を guard するのが正攻法か)。
- 多ページ化 (T2-1) と単一スクリプト維持、530 listing 規模での体感差の見積り。
- アプリ化 Tier 3 の費用対効果: Streamlit 前提なら wrap は無意味との理解で正しいか。backend 分離 (3-B) を選ぶ判断境界。

## 7. Codex (gpt-5.5) 回答 + 統合結論 (2026-06-04)

Codex CLI で諮問 (初回は exec が承認待ちハング→`--sandbox read-only` で解決)。私の分析と概ね一致、以下を精緻化:

**Q1 form+fragment**: 正しい形 = 「1商品=1 fragment、その中に form」。落とし穴: (1)fragment外コンテナに描画しない (2)widget key を商品ID で完全固定 (3)fragment戻り値に依存しない (4)一覧件数/集計など fragment外の更新は session_state 更新後に必要時だけ full st.rerun() (5)st.rerun(scope="fragment") は fragment rerun中以外で例外=乱用しない。

**Q2 閉expander skip**: 状態追跡し open の時だけ body 描画。閉body の widget は未描画になるため、未submit入力/一時widget state に依存しない設計 (保存値・計算結果は session_state/DB に寄せる)。

**Q3 Tier1 vs Tier2**: Tier1 が先。1商品fragment化だけで商品内操作の体感は 50-90% 短縮 目安。Tier2(多ページ化)の速度寄与は限定的 — 現状 if _w134_sel==... で既に非選択タブを実行していないため、Tier2 の主効果は保守性・起動/共通処理削減・事故防止であって速度ではない。25件再描画問題は Tier2 では解けない。

**Q4 アプリ化**: wrap(Electron/pywebview)は速度ほぼ不変=理解は正しい。backend分離/React を選ぶ境界 = 行単位更新・仮想リスト・複雑テーブル編集・ドラッグ・低遅延・複数同時編集・業務ロジックAPI化が必要になった時。530件/25件ページなら まず Streamlit 内最適化で十分。

**Q5 追加高ROI**: (1)一覧を st.data_editor/テーブル中心にして各商品カードの常時描画量を減らす。詳細・rival・画像・metrics は開いた商品だけ。(2)profit計算は入力正規化して TTLなし cache か DB version 連動。(3)画像はサムネ/固定サイズ/必要時のみ。(4)プロファイル必須: full rerun時間 / 商品1件render / 25件render / 画像+markdown送信量 を実測。rerunごとの描画要素数削減が一番効く。

### 統合結論 (Claude + Codex 合意)
1. アプリ化は速度目的では不要 (wrapは無意味、フロント置換は大工事で現規模には過剰)。現Streamlitのまま大幅改善できる。
2. 最優先 = Tier1: (1)1商品 fragment化(form を内包) (2)閉商品の重処理skip (3)一覧テーブル化＋詳細は開いた時だけ (4)profit cache強化 (5)画像lazy。1商品操作の体感 50-90% 改善見込み。
3. Tier2(多ページ化)は速度でなく保守性目的として後回し可。
4. 必ず Phase0 でプロファイル ("遅い"を数値化) してから着手し、各段で実測。
