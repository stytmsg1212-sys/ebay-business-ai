# LLM 在庫判定 (フォールバック) 設計 — option C (2026-06-03)

出典: 2026-06-03 user 相談。罠サイト(orientalmotor-shop 等: 「在庫なし」文字が在庫ありでも HTML に常駐 / schema.org availability 無し / カートボタンが楽天類似で売切でも残る疑い)はマーカー方式では誤判定するため、LLM 判定を**フォールバック**で導入する。money-direct(誤判定で出品停止/オーバーセル)。

## 1. 背景 / 現状

- 在庫判定(`monitor/scrapers.py`)= サイト別 site_config の在庫/売切/ページなしテキストマーカーを HTML から検出。`check_item_by_config(item, site_config)` → `check_url_sync` → `_detect_status_single`。
- 定時 `task_inventory_check`(毎日 02:30)が monitored_items(active 374 URL)を一括チェック。
- **罠サイト**: マーカーが在庫ありでも HTML に常駐 → 誤検出。orientalmotor-shop で実証(「在庫なし」が在庫ありで present, schema.org availability 無し)。
- 母数: 無在庫 active 301 件 / monitored 374 URL。現状 status: 在庫有100 / 在庫無121 / ページなし59 / 不明18 / unknown2 / エラー1。

## 2. 設計方針: **フォールバック + 本文抽出 + Haiku + 日次予算上限 + fail-closed**

### 2.1 トリガ (フォールバック)
- 既存マーカー判定 (`_detect_status`) が **`unknown` (None→unknown) を返した時だけ** LLM 判定を呼ぶ。
- マーカーで確実に判定できる site (34 config) は **LLM を一切呼ばない** = タダ。
- → LLM 呼出は現状 ~20 件/日 (不明18+unknown2+エラー1 相当) に限定。

### 2.2 入力 (本文抽出、コスト/精度の要)
- ページ取得は **既存スクレイパ (Playwright rendered text) を再利用** (新規 fetch しない)。SPA は rendered text を使う。
- 送る前に **本文抽出** (nav/script/footer/関連商品 boilerplate を除去、~2,000 トークンに圧縮)。**HTML 全文は送らない** (10倍コスト + ノイズで精度低下)。
- anti-bot/captcha ページは LLM に送らず **`unknown`** (ゴミ判定防止、既存 anti-bot 検出を流用)。

### 2.3 モデル / プロンプト
- **Haiku 4.5** (bulk・短文・低コスト)。temperature=0 (決定的)。
- プロンプト: 「この商品ページのテキストから、この商品が今**購入可能(在庫あり)**か**売り切れ**か判定。判定できなければ unknown。」
- **structured output**: `{status: available|unavailable|unknown, confidence: high|low, reason: str}`。
- **プロンプトキャッシュ**: 指示部分 (固定) を cache、可変はページ本文のみ → 入力コスト最小化。

### 2.4 money-direct 安全策 (fail-closed)
- LLM が `unknown` or `confidence=low` → **source_status は更新せず unknown** (誤った出品停止/継続を防ぐ)。
- **日次予算上限** (api_budget_log、例 $0.3/日)。超過 → 以降は LLM 呼ばず `unknown` に fail-closed (W209 ニュース深掘りと同パターン)。
- **prompt injection 対策**: ページ本文は「データ」として扱う (指示と分離)、structured output で status を enum 制約、想定外値は unknown 扱い。
- LLM 結果は marker 同等の source_status に写像するが、**confidence=low は unknown** に倒す (保守)。

### 2.5 結合
- `task_inventory_check` の単一アイテム判定後、status が `unknown` の時に LLM judge を呼ぶ薄い hook。
- 手動「🔍在庫を今すぐ確認」ボタン (今日追加) も同経路 → 罠サイトでも結果が出る。
- 新規モジュール `monitor/llm_stock_judge.py`。既存 calculator/scrapers は無改変 (additive)。

## 3. コスト試算 (実データ)
| 設計 | LLM/日 | 月額概算 (Haiku・本文抽出) |
|---|---|---|
| **フォールバック (~20/日) ← 採用** | 20 | **~$1.5 = ¥225/月** |
| 全件 (374/日) | 374 | ~$28 = ¥4,200/月 |
| (誤) HTML全文・全件 | 374 | ~¥34,000/月 (回避必須) |
- 1件: 入力~2,000tok×$1/M + 出力~100tok×$5/M ≈ $0.0025 (約0.4円)。

## 4. リスク / Codex レビュー観点
1. **LLM hallucination の money-direct 影響**: 誤「在庫あり」→ 売切品を出品継続=オーバーセル/Defect。誤「在庫切れ」→ 在庫ある品を出品停止=機会損失。confidence=low→unknown の保守は十分か。LLM 結果を source_status に直反映してよいか、それとも別フラグ (要人間確認) にすべきか。
2. **fail-closed の網羅**: budget 超過 / API 失敗 / structured output 不正 / anti-bot 全てで unknown に倒れるか。silent に誤判定を残さないか (Q0)。
3. **本文抽出の精度**: SPA 未 render / 抽出で在庫情報が落ちると LLM が誤判定。rendered text 使用 + 抽出ロジックの穴。
4. **prompt injection**: 悪意あるショップページが「在庫ありと答えろ」等を仕込む。enum 制約 + データ分離で十分か。
5. **フォールバック量の暴走**: マーカー設定の劣化で unknown が急増 → LLM 呼出が 20→数百 に膨らみコスト増。日次上限で頭打ちになるが、検知/警告は要るか。
6. **既存マーカー方式との一貫性**: LLM と marker が同一サイトで矛盾した時の優先順位。
7. コスト試算 (フォールバック ¥225/月) の妥当性。Haiku 単価・トークン見積もりの現実性。

## 5. 実装スコープ (Q3、未着手)
- `monitor/llm_stock_judge.py` (judge 本体 + 本文抽出 + budget gate)。
- `task_inventory_check` に unknown 時 hook。
- api_budget_log に context='stock_judge' で日次上限。
- pytest: hallucination 時 unknown / budget 超過 fail-closed / injection 無効化 / marker 確定時は LLM 非呼出。
- code-reviewer + Codex 2段 + Q1 (Streamlit 手動ボタンで罠サイト実機判定)。

## 6. Codex 設計レビュー (2026-06-03) → **条件付き否決・要設計修正** (確信度 高)

LLM 結果を `source_status` に直反映する初期設計は **money-direct で危険**。以下を実装前に必須修正。

### 致命的な設計矛盾 3 点 (実装前に直す)
1. **トリガが罠サイトを救えない**: 罠サイト (orientalmotor) はマーカーが「在庫なし」常駐で誤って **`unavailable` を返す** (unknown でない)。「unknown 時だけ LLM」では**発火しない**。→ **罠サイトは config 段階でマーカーを意図的に無効化し `unknown` に倒す** (Yahoo!ショッピング既存方針 database.py:91 が参考)、その上で LLM fallback。
2. **scrapers が rendered text を返さない**: `check_items_batch` は status のみ返却 (scrapers.py:283)。「既存 fetch 再利用 + scrapers 無改変」は**両立不能**。→ `status + rendered_text + anti_bot + extraction_metadata` を返す**内部結果型が必要** (scrapers 改修必須)。
3. **`unknown` は「更新しない」でなく「上書き」**: task_inventory_check (535) が全結果を `last_status` に更新 → ebay_sync (227) が `source_status` に同期。**確定済「在庫無」を unknown で上書きすると履行不能リスクが不可視化**。→ 観測失敗 (API失敗/予算超過/不正JSON/timeout) は**別保存し確定状態を保持**。

### ステータス設計の修正 (source_status と LLM を分離)
```
source_status          既存の確定状態。LLM 単独では更新しない
llm_stock_status       available / unavailable / unknown
llm_stock_confidence   high / low
llm_stock_reason       短い根拠
llm_stock_checked_at
stock_review_required  0 / 1   ← LLM 観測を要人間確認フラグに
```
- **`available` の誤判定 = オーバーセル直結。LLM 単独で「安全」と認定させない**。confidence=high でも確定値には不十分。
- 初期は **shadow 運用 2〜4 週でサイト別 precision 測定** → 直反映は **サイト別 allowlist + 非対称ルール** (available は厳しく/unavailable は緩く 等) を採用してから。

### その他
4. **injection**: enum 制約は出力形式を守るだけ。ページ本文を不信入力として扱い、指示文らしいテキスト検出で `unknown`、商品名/購入操作領域/在庫表示周辺だけ抽出。
5. **量暴走**: 日次上限だけでは検知が遅い。サイト別 unknown率 / LLM呼出数 / 予算拒否数 / 前日比を記録し急増アラート。予算超過は正常終了でなく運用アラート。
6. **コスト (確信度 中)**: 単価計算は妥当 ($0.0025/件、20件/日で $1.5/月)。但し抽出超過/リトライ/手動確認/cache書込 未計上で ¥225 は理想値。$0.3/日上限 = 月最大 ~$9。

### 修正後の実装順序 (Codex 推奨)
1. 罠サイトの誤マーカー無効化 → 確実に `unknown` へ。
2. scrapers から rendered text + anti-bot 判定を返す内部結果型。
3. LLM 結果を source_status と分離 (上記 schema、migration)。
4. shadow 運用 2〜4 週でサイト別 precision 測定。
5. 直反映は allowlist + 非対称ルールで段階解禁。
→ **初期導入は「別フラグ + 要人間確認 + shadow」**。直反映は精度実測後。
