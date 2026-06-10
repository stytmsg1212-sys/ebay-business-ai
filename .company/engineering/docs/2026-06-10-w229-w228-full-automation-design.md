# W229 → W228 商品リサーチ全自動化パイプライン 実装設計書

**作成**: 2026-06-10 / **状態**: 設計確定・実装着手 / **モデル**: 設計=Opus 4.8 (code-architect) / 判断=Claude Fable 5
**正本仕様書**: `.company/engineering/docs/2026-06-07-product-research-automation-spec.md` (§2 / §7 / §8 を厳守)
**前提**: 2026-06-10 user ヒアリング確定要件 (進行順 W229→W228 残、ゴール=承認のみ、夜間バッチ全自動) を内包

---

## 0. 概要

「Terapeak で発掘 → 売れ行きゲート → フリマ仕入探索 → AI 同一性 → 利益計算 → 承認キュー → 承認後出品下書き」を **夜間バッチで全自動化**し、user の作業を **承認キューでの承認/見送りだけ**にする。最終出品ボタンと仕入購入のみ人間が押す (完全自動購入はしない / 仕様 §7-6)。

既存 W228 (フェーズB半自動 Wizard) は実装済だが、(1) ゲート判定が session_state 揮発で永続化されていない、(2) 利益見込みが手数料控除前で約1.24倍過大、(3) 探索エラー再実行経路欠落、(4) found_condition 未記録、の 4 欠陥がある。これらを **Phase 1 の受け皿整備で先に潰し**、その上に W229 ハーベスト (Phase 2) と B工程全自動 (Phase 3)、承認キュー+下書き自動 (Phase 4) を積む。

設計の背骨は仕様書 §8 P0-1 (独自PK rc_id / sentinel 禁止)、P0-2 (状態機械)、P0-3 (在庫0上限)、P1-1 (weight 欠落で 0 clip 禁止)、P2 (技術失敗と業務判断を別状態) を全 Phase で死守すること。

---

## 1. スコープ

### 含まれる
- research_candidates テーブルへのゲート判定列追加・新規列追加 (migration v69 以降)
- Terapeak Product Research 結果リストのハーベスト読取 (新規開発、W229 本体)
- Terapeak 各商品詳細の自動取得 (90日 sold / active 出品+開始日 / 1〜2年 sold)
- 自動ゲート判定 (既存純関数 `evaluate_sourcing_gate` を バッチから呼ぶ)
- AI 重量推定の転用 (既存 `_estimate_with_claude` パターン)
- フリマ自動探索 → AI 同一性 → 利益計算 (真値) → 承認キュー積み
- 承認キュー UI (Terapeak 売れ行き + 仕入先カード + 利益額 + けいすけ基準バッジ + 推定重量明示)
- 承認後の出品下書き自動生成 (個別出品タブ pre-fill / W226 description 流用)
- 在庫0承認時の keyword watch 自動登録 (W206 基盤)
- ハーベストフィルタの設定画面 (除外KW / カテゴリ / 価格下限 / seller国 / 並び順)

### 含まれない (out-of-scope)
- 完全自動購入 (仕入購入は人間承認・人間操作)
- 最終 eBay 出品公開ボタンの自動押下 (下書きまでが自動、公開は人間)
- Amazon / 楽天 等 EC 仕入 (フリマのみ / 仕様 §2-B、false-OOS Defect 防止)
- Terapeak ログイン自動化 (CDP Chrome は user が事前起動・ログイン)
- HS code 自動確定 (Section232 該当フラグの提示まで。最終分類は人間 / P1-1)
- アニメ/IP 商品の自動採用 (VeRO リスク、人間承認必須 / §9)

---

## 2. フェーズ分割 (実装順)

各 Phase は**単独で価値を出し単独で Q1 検証可能**であること (DoD は §10)。

| Phase | 名称 | 価値 (単独) | 主リスク |
|---|---|---|---|
| **1** | 受け皿整備 (FIX-1〜4) | 既存 Wizard のゲート永続化・利益真値化で**今すぐ手動運用が正しくなる** | money-direct (利益表示) |
| **2** | W229 ハーベスト + 自動ゲート | user が手入力していた発掘を夜間自動化。ゲート判定済候補が朝溜まる | anti-bot / クォータ |
| **3** | B工程自動化 (AI重量→探索→利益→キュー積み) | target_* 候補が自動でフリマ探索・利益計算され承認待ちになる | コスト暴走 / 偽黒字 |
| **4** | 承認キュー UI + 承認後下書き自動 + watch 自動登録 | user の作業が「承認のみ」に到達 (ゴール達成) | 誤承認誘発 / 履行不能 |

**ビルド順の根拠** (仕様 §8 ビルド順改訂を踏襲): 最大リスク (アダプタ層=利益計算の物理データ欠落) は Phase 1 で既に解消済 (research_poc.py)。Phase 2 で次のリスク (anti-bot) を、Phase 4 で最も金銭直結 (出品/監視) を最後に置く。

---

## 3. DB スキーマ変更

### 3-1. migration 番号

既存 v67 で research_candidates 作成済、v68 はライバルセラー監視で使用済 (`monitor/database.py` 確認済)。**本設計は v69 から開始**。全 ALTER は `try/except sqlite3.OperationalError` で冪等化、列実在確認後に `PRAGMA user_version` bump (v66/v67 流儀踏襲)。

### 3-2. v69 — research_candidates 拡張 (FIX-1 / FIX-2 / FIX-4 / W229 受け皿)

```sql
-- FIX-1: ゲート判定の永続化 (§8 P0-2 違反の修正)。skip/reject 含む全判定を保存。
ALTER TABLE research_candidates ADD COLUMN gate_decision TEXT;       -- target_instock/target_oos_watch/reject_deadstock/skip_too_new/reject_no_demand
ALTER TABLE research_candidates ADD COLUMN gate_reason TEXT;         -- 人間可読の根拠 (evaluate_sourcing_gate の reason)
ALTER TABLE research_candidates ADD COLUMN gate_inputs_json TEXT;    -- {sold_90d, has_active_listing, listing_start_date, sold_1_2yr} スナップショット
ALTER TABLE research_candidates ADD COLUMN gated_at TIMESTAMP;       -- ゲート判定日時 (UTC)

-- W229 ハーベスト由来メタ (発掘元の追跡 + 重複排除キー)
ALTER TABLE research_candidates ADD COLUMN source TEXT DEFAULT 'manual';  -- 'manual'(Wizard手入力) / 'terapeak_harvest'(W229)
ALTER TABLE research_candidates ADD COLUMN harvest_keyword TEXT;          -- ハーベスト時の正規化キーワード (重複排除キー)
ALTER TABLE research_candidates ADD COLUMN ebay_avg_sold_price_usd REAL;  -- Terapeak Avg sold price (発掘時点)
ALTER TABLE research_candidates ADD COLUMN ebay_total_sold INTEGER;       -- 発掘時点の sold 件数 (90日)
ALTER TABLE research_candidates ADD COLUMN harvested_at TIMESTAMP;        -- 発掘日時 (UTC)

-- FIX-2: 利益真値 + けいすけ基準の保存 (money-direct)
ALTER TABLE research_candidates ADD COLUMN profit_jpy_true INTEGER;       -- calculator.calculate の profit (円, 手数料控除後の真値・還付抜き)
ALTER TABLE research_candidates ADD COLUMN profit_usd_true REAL;          -- profit_jpy_true / fx (表示用 USD)
ALTER TABLE research_candidates ADD COLUMN keisuke_pass INTEGER;          -- けいすけ基準合否 0/1
ALTER TABLE research_candidates ADD COLUMN keisuke_detail_json TEXT;      -- {profit_rate, pass_600, pass_rate, threshold_jpy}
ALTER TABLE research_candidates ADD COLUMN section232_flag INTEGER DEFAULT 0;  -- Section232該当推定フラグ (P1-1 赤字警告)
ALTER TABLE research_candidates ADD COLUMN section232_reason TEXT;        -- 該当根拠 (HS推定 + Annex)

-- AI 推定重量の出所明示 (承認画面で「推定値」と明示する根拠)
ALTER TABLE research_candidates ADD COLUMN weight_source TEXT;            -- 'ai_estimate' / 'manual' / null
ALTER TABLE research_candidates ADD COLUMN weight_confidence TEXT;        -- 'high'/'medium'/'low' (AI推定時)

-- 出品下書き / watch 連携の追跡 (Phase 4)
ALTER TABLE research_candidates ADD COLUMN listing_draft_id INTEGER;      -- 生成した出品下書きの参照 (個別出品 prefill key)
ALTER TABLE research_candidates ADD COLUMN watch_ids_json TEXT;           -- 登録した keyword_watches.id のリスト
ALTER TABLE research_candidates ADD COLUMN result_ebay_item_id TEXT;      -- 公開後の実 eBay item id (listed 遷移用、§14-Q7)
```

**注**: 既存 `manual_weight_g` 列は維持し、AI推定値もこの列に書く (出所は `weight_source='ai_estimate'`)。calculator は値の出所を問わず weight_g を消費するため、列を増やさず source で区別する (K1 Simplicity)。

**FIX-4 (found_condition_ja)**: 列は v67 で既に存在 (`found_condition_ja TEXT`)。書込が無いだけ。Phase 3 で claude_evaluator の状態判定結果を `update_research_candidate_result(found_condition_ja=...)` で記録する (DB変更不要、コード修正のみ)。

### 3-3. 新テーブルの要否

**新テーブルは作らない** (K1)。理由:
- ハーベスト候補・探索結果・承認状態・下書き連携は全て rc_id を主体とする 1 entity のライフサイクル → research_candidates 1 テーブルに集約。
- watch 連携は既存 keyword_watches に `add_watch(source='w228_research')` で書き、research_candidates 側は `watch_ids_json` で逆参照 (W185 の UNIQUE 汚染を避けるため keyword_watches に rc_id 列は足さない)。
- ハーベストのクォータ消費記録は既存 `api_call_log` / `task_execution_log` を流用 (新テーブル不要)。

### 3-4. ハーベスト重複排除 (FIX-1 の前提インフラ)

W229 ハーベストは同一商品が新着順で再出現する。**`gate_decision` が確定済の候補は再判定スキップ**する。重複排除キーは:
- `source='terapeak_harvest' AND harvest_keyword = ?` で既存 rc を引き、`gate_decision IS NOT NULL` なら skip。
- `harvest_keyword` は正規化キーワード (小文字化 + 空白正規化)。**SKU は使わない** (rc は ebay_item_id を持たない / sku-rules.md 範囲外)。
- skip した事実は `task_execution_log` に件数記録 (Q0 痕跡)。
- 例外: `skip_too_new` (gate_rejected の一種) は再出現時に開始日を更新して**再判定する** (仕様 §2-A 行3「保留せず再出現時再判定」)。

---

## 4. 状態機械の拡張

### 4-1. 現行 (v67 / research_candidates_db.py)

8 status: `new / sourcing / sourced / not_found / needs_review / identity_approved / identity_rejected / watch_registered`

### 4-2. 拡張後 (ハーベスト由来・承認キュー・下書き生成を追加)

ハーベスト経路はゲート判定が先に来るため、`new` の前段に `harvested` を、承認後の出品下書きに `draft_generated / listed` を足す。**既存 status は一切変更しない** (K2、後方互換)。

| 新 status | 意味 | 由来 Phase |
|---|---|---|
| `harvested` | Terapeak 発掘直後 (ゲート未判定) | Phase 2 |
| `gate_passed` | ゲート target_* 判定済・探索待ち | Phase 2 |
| `gate_rejected` | ゲート reject_*/skip_* (候補リスト非表示・再判定用に保存) | Phase 2 |
| `awaiting_approval` | 探索+利益完了・承認キュー表示中 | Phase 3 |
| `approved` | user が承認 (出品下書き生成へ) | Phase 4 |
| `draft_generated` | 出品下書き生成済 (個別出品 prefill 待ち) | Phase 4 |
| `listed` | user が eBay 公開済 (終端) | Phase 4 |

### 4-3. 遷移グラフ (追加分)

```
[harvested] → gate_passed / gate_rejected / needs_review
[gate_passed] → sourcing / needs_review
[sourcing] → sourced / not_found / needs_review     (既存)
[sourced] → awaiting_approval / needs_review         (※既存の identity_* は手動Wizard経路として残す)
[not_found] → awaiting_approval (在庫0+過去取引ありで監視候補) / needs_review
[awaiting_approval] → approved / gate_rejected (見送り=user却下) / needs_review
[approved] → draft_generated / needs_review
[draft_generated] → listed / needs_review
[gate_rejected] → (終端、再出現時の重複排除キーとして保持。skip_too_new のみ再判定で復帰可)
```

**Q0 死守点**:
- `gate_rejected` は終端だが理由 (`gate_reason`) 必須。候補リストには非表示だが DB には残す (再出現再判定 / 仕様 §2-A 行3)。
- `skip_too_new` は `gate_inputs_json` に開始日を保存し「あと N 日で再判定可」を追跡。
- **技術失敗 (取得エラー/計算不能) = `needs_review`**、**業務判断 (死に筋/赤字/在庫なし) = `gate_rejected`/`not_found`**。両者を混同しない (P2)。needs_review は必ず `needs_review_reason` 必須 (既存 CAS で強制済)。

### 4-4. 自動経路と手動経路の共存

既存の `identity_approved / identity_rejected / watch_registered` は**手動 Wizard 経路**として残す (K2)。自動経路は `awaiting_approval → approved → draft_generated → listed`。承認キュー UI (Phase 4) では自動経路の status を扱い、手動 Wizard (既存セクション C) はそのまま温存。`_ALLOWED_TRANSITIONS` に新 status を追加するのみ。

---

## 5. Terapeak スクレイパー拡張 (W229 本体 / Phase 2)

現行 `terapeak_scraper.py` は **SOLD タブの Buyer Location 集計特化**。W229 は以下 3 つを**新規開発**する。既存の SOLD 集計ロジック・anti-bot 対策・CDP attach・thread wrapper は流用する。

### 5-0. 収穫 2 パターン (2026-06-10 user 確定要件)

毎晩のハーベストは **2 パターン × 各カテゴリ** で結果リストを取得する:

| パターン | 期間フィルタ | 並び替え | 採取窓 | 狙い |
|---|---|---|---|---|
| **① fresh_24h (鮮度型)** | Last 7 days | Date last sold **新しい順** | 行の Date last sold が**直近 24 時間内**のものだけ | 「いま売れた」需要の鮮度重視 |
| **② two_year_echo (2年前型)** | Last 2 years | Date last sold **古い順** | 行の Date last sold が**「今日−2年」の 24 時間窓** (≈730日前) のものだけ | 当時売れたが現在は出品が薄い商品の再発掘 + 年周期需要 |

実装上の帰結:
- `HarvestedProduct` に **`date_last_sold` フィールドを追加** (24h 窓のクライアント側判定に必須。Terapeak 結果行の "Date last sold" 列から `_parse_terapeak_date` で抽出)
- `research_candidates` に **`harvest_pattern` 列を追加** (`'fresh_24h'` / `'two_year_echo'`、Phase 2 migration v70。承認キュー UI と Discord 通知で由来を表示)
- **重複排除は両パターン共通の `harvest_keyword`** で行う (①と②に同一商品が出たら先着の gate_decision を尊重 / 既存 dedup 仕様のまま)
- **50件/日 cap は両パターン合算**。①を優先で詰め、残枠を②に回す (鮮度型は当日逃すと価値が落ちるため。配分は設定 UI で変更可にしない / K1、実測後に必要なら見直し)
- パターン②の期間指定: preset "Last 2 years" (dayRange=730 相当) の URL param、または CUSTOM 日付範囲 (startDate/endDate) — **probe 確定 (2026-06-10): dayRange=730 + startDate/endDate (ms) で "Jun 10, 2024 – Jun 10, 2026" が画面反映、実機 PASS**
- 並び替え (Date last sold 昇順/降順) — **probe 確定: URL param `sorting=-datelastsold` (新着順) / `sorting=datelastsold` (古い順)。URL 直叩きでソート状態が完全再現、UI クリック不要**
- 採取窓に 1 件も該当しない夜 (特に②は 2 年前のその日に売れた商品数に依存) は **0 件で正常終了** (Discord に「パターン② 0件」と痕跡を残す / Q0)

#### 5-0-a. 実機 probe 確定事項 (2026-06-10、エビデンス: data/terapeak_probe/*.html + probe4_results_2026_06_10.json)

| 項目 | 確定値 |
|---|---|
| **🚨 keywords 必須 / 括弧記法で除外のみ可** | 空 keywords + categoryId はリダイレクトで不可、裸の除外のみ (`-abcd -Card ...`) も「no results」で不可。**ただし括弧くくり `(-Card) (-camera) ...` なら除外語のみクエリが成立** (probe5-B、50行返却、"Japan" 無タイトル商品も収穫可、user の手動運用と同形式)。`*` ワイルドカード不可、UI でキーワード空検索は eBay 通常検索へリダイレクト (probe5-A/C) |
| ソート | `sorting=-datelastsold` (降順=新着) / `sorting=datelastsold` (昇順=古い)。URL 再現可 |
| 期間① | `dayRange=7&startDate=<ms>&endDate=<ms>` → "Jun 3 – Jun 10" 反映 PASS |
| 期間② | `dayRange=730&...` → "Jun 10, 2024 – Jun 10, 2026" 反映 PASS |
| 価格下限 | `minPrice=100` URL param 有効 (全行 $100+ 実証) |
| 行 selector | `tr.research-table-row`、td class suffix: `__product-info` / `__avgSoldPrice` / `__avgShippingCost` / `__totalSoldCount` / `__totalSalesValue` / `__dateLastSold` ("Jun 5, 2026" 形式 = 日付粒度のみ、時刻なし) |
| ページング | `offset=N&limit=50` (50/page) |
| DOM 取得 | `page.content()` は SSR (テーブル無し) — `document.documentElement.outerHTML` の live DOM 必須。networkidle 不可 (SPA 常時通信) → domcontentloaded + ポーリング |
| 24h 窓の粒度 | Date last sold は**日付のみ**。①の「直近24h」= JST で {今日, 昨日} の 2 日マッチ + dedup で近似 (03:30 実行時は大半が昨日日付)。②= 「今日−2年」の日付一致 |
| 未検証 (採用しない) | dayRange=1 / 過去日 endDate の CUSTOM 窓 — probe 未実証のため使わない (K0)。実装は上記の検証済み組合せ (7d+新着順 / 730d+古い順 + クライアント側窓フィルタ) のみ |

#### 5-0-b. シード検索クエリ (2026-06-10 user 決定: 括弧記法の除外語のみ方式)

- **確定クエリ (user の手動運用と同一形式)**: `(-abcd)  (-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)` — 正キーワードなし。括弧くくりにより Terapeak が受理する (probe5-B 実証)。"Japan" シード案は user 却下 (Japan 無タイトルのセラーを取りこぼすため)
- ハーベスト単位 = **シード entry (除外クエリ + 任意 categoryId + minPrice)**。「各カテゴリーごと」= categoryId を変えて同一除外クエリを実行
- 除外語は設定 UI (§8 設定タブ) で追加・削除可 (Carddass 型の word-boundary 通過は除外語追加で補完)
- ~~⚠️ probe5-B は dayRange=30 デフォルトでの成立確認。①②の harvest パラメータとの組合せは実機スモークで最終確認~~ → **✅ 検証済み (2026-06-10 実機スモーク)**: ① fresh_24h = 50 件 PASS (`scripts/smoke_harvest_2026_06_10.py`)、② two_year_echo = 37 件全件 target 日付一致 PASS (`scripts/smoke_harvest_2yecho_utc7.py`、UTC-7 整列修正後)。確定除外クエリ × 両パターンの組合せ成立を確認済み

### 5-1. 新規関数 (terapeak_scraper.py に追加、既存関数は触らない / K2)

| 関数 | 役割 | 入力 → 出力 |
|---|---|---|
| `harvest_product_list(filters, max_items)` | Product Research 結果リストの 1 ページを読取り、商品名+Avg price+sold数+商品リンクを抽出 | filters dict → `list[HarvestedProduct]` |
| `scrape_product_detail(keyword)` | 1 商品の詳細: 90日 sold / active 出品有無+最古開始日 / 1〜2年 sold | keyword → `ProductGateData` |
| `_extract_active_listing_start_dates(html)` | ACTIVE タブの出品開始年月を抽出 (新規セレクタ) | html → `list[date]` |
| `_extract_sold_count(html, day_range)` | 指定期間の sold 件数を抽出 (90d / 730d を別 navigate) | html, day_range → int |

```python
@dataclass
class HarvestedProduct:
    title: str                    # Terapeak 商品名 (英語タイトル)
    avg_sold_price_usd: float|None
    sold_count_90d: int|None
    research_url: str             # この商品の Research 詳細 URL
    image_url: str|None

@dataclass
class ProductGateData:
    keyword: str
    sold_90d: int                 # evaluate_sourcing_gate へ
    has_active_listing: bool
    listing_start_date: str|None  # "YYYY-MM" (最古の active 出品)
    sold_1_2yr: int
    avg_sold_price_usd: float|None
    success: bool = False
    error: str|None = None
```

### 5-2. セレクタ戦略 (実コードベースの方針踏襲)

- **DOM 取得は v2 (live outerHTML) 優先 + v1 fallback**、必要セレクタのみ抽出 (OOM 防止、既存 evaluate パターン踏襲)。
- **Product Research 結果リスト**: 結果テーブル各行のタイトル・avg price・sold count を行単位で locator 抽出。dropdown menu item の誤検出を避けるため `results-table` スコープ内に限定 (既存 `_detect_actual_dayrange` の results-header__left スコープ限定と同思想)。
- **ACTIVE 出品開始日**: ACTIVE タブへ `tabName=ACTIVE` で navigate し、各出品の "Listed: Mar 2025" 相当を `_parse_terapeak_date` (既存・locale非依存) で抽出。最古を `listing_start_date` とする。
- **dayRange 検証必須**: `_detect_actual_dayrange` で実 dayRange を照合 (SPA state ドリフト防御、既存 Q0 パターン)。90日と730日で別 navigate するため各々検証。
- **Condition filter 自動解除**: 既存 `_clear_condition_filters` を流用 (全 condition 集計が業務基準)。

### 5-3. クォータ管理 (1日250件 / market_analysis と共有)

仕様 §1: 250件/日は **Terapeak UI 側の実運用制限・要実測・アカウントリスク**。market_analysis_refresh と**同一 eBay アカウントでクォータを共有**するため調停が必須。

- **収穫上限 50件/日** (user 確定要件): 1 ハーベストバッチで最大 50 商品。
- **クォータ計上**: `api_call_log` に `api='terapeak_read'` で navigate 単位記録。バッチ冒頭で当日累計を SELECT し、`market_analysis` 消費と合算して **残 ≥ 必要数** を確認、不足なら skip + Discord 通知 (silent skip 禁止)。
  - SQLite TIMESTAMP は UTC → `WHERE DATE(called_at, '+9 hours') = DATE('now', '+9 hours')` で JST 当日集計 (sqlite-timezone.md 準拠)。
- **直列実行 + wait + jitter**: 既存 `time.sleep(sleep_seconds * jitter[0.7,1.5])` を流用。並行 navigate しない。
- **fail-loud (anti-bot)**: 既存 `stop_on_consecutive_failures=5` + `_is_ebay_error_redirect` を流用。連続失敗 / error redirect で**即停止し Discord 通知**、残件は翌日再開 (W7-A 前例)。

### 5-4. クォータ調停の具体策

market_analysis は**日曜02:00 のみ**、W229 ハーベストは**毎日 03:30** に置けば曜日衝突は日曜のみ。日曜は:
- 同一 CDP Chrome を順次使用 (並行しない)。market_analysis (02:00) が長引いて 03:30 に未完了なら、ハーベストは当日累計クォータを見て**残不足なら skip** (Discord 通知)。
- `daily_scheduler` の `max_instances=1` は同一 job の再入のみ防ぐ。別 job 間は**クォータ DB チェックで論理排他**する (thread global は使わない / silent-skip-prevention.md の thread 跨ぎ教訓)。

---

## 6. 夜間バッチ task 設計 (Phase 2 / Phase 3)

### 6-1. task_key と実行時刻

02:30 朝バッチは既に長い (~03:21 まで)。**ハーベストと B工程自動化は独立 CronJob** として 02:30 batch の外に置く (market_analysis_refresh / order_alert と同じく `setup_scheduler` に直接 add_job)。

| task_key | 関数 | 時刻 (JST) | 理由 |
|---|---|---|---|
| `research_harvest` | `_run_research_harvest` | **毎日 03:30** | 02:30 batch 終了後。CDP Chrome 専有が他と被らない。market_analysis (日02:00) とは残クォータで調停 |
| `research_sourcing` | `_run_research_sourcing` | **毎日 04:30** | harvest 完了後。gate_passed 候補のフリマ探索+AI+利益。Playwright + Claude API |

**根拠**: ハーベスト (CDP/Terapeak) と sourcing (フリマ Playwright + Claude) を分離することで、anti-bot 停止時もハーベスト分の候補は残り、sourcing は翌日リトライ可能 (Phase 独立性)。

### 6-2. 新規 scheduled task の必須要件 (silent-skip-prevention.md 準拠)

両 task とも:
1. **`task_key` 必須**: `run_task(..., task_key='research_harvest')` 形式 (market_analysis_refresh と同型ラッパ)。
2. **`TASK_SCHEDULE` 登録**: `('research_harvest', '商品リサーチ発掘', [3], None, 'research')` / `('research_sourcing', '商品リサーチ探索', [4], None, 'research')`。ヘルスチェックが欠落を Discord 通知。
3. **`scheduled_hour` 引き渡し**: `add_job(args=[config, hour])`。`datetime.now().hour` 直参照禁止。
4. **`max_instances=1`** (job_defaults で既定)。

### 6-3. 観測可能性 3 経路 (全 scheduled task 必須)

| 経路 | 実装 |
|---|---|
| DB log | `task_execution_log` (started/completed/failed/skip_*) + 件数を `api_call_log` |
| Discord 通知 | バッチ完了時に「発掘 N / ゲート通過 M / 探索成功 K / 承認待ち追加 J / コスト $X」 |
| UI 表示 | MonoDeck「定時実行」タブ + 承認キュー UI (Phase 4) に件数バッジ |

### 6-4. コスト保護 (W209 fail-closed パターン)

- **日次 AI コスト上限**: `schedule_config.json` の `tasks_enabled.research_sourcing.daily_cost_cap_usd` (初期 $3) を超えたら**処理中断 + Discord 通知**。当日累計は `api_call_log` の `cost_usd` 合算 (W209 と同方式)。
- **再評価 skip**: 重量推定は `weight_source='ai_estimate'` 済の rc は再推定しない。同一性判定は同 keyword + 同 found_url の既評価を skip (W223 思想)。「再生成」は承認キュー UI の別ボタンに隔離 (誤課金防止)。

---

## 7. 承認キュー UI 設計 (Phase 4)

### 7-1. 配置

既存 `tab_w228_research.py` に**セクション D「承認キュー」を追加** (K2: 既存 A/B/C は触らない)。`status='awaiting_approval'` の候補を一覧表示。

### 7-2. 表示項目 (user 確定要件: Terapeak 売れ行き + 仕入先カード + 利益額 を見て承認/見送り)

各候補を `st.container(border=True)` のカードで表示:

```
┌─ rc_id=123  Sony WH-1000XM5 ────────────────────────────┐
│ 【Terapeak 売れ行き】                                      │
│   90日 sold: 5件 / 1〜2年: 18件 / Avg $248.00            │
│   ゲート判定: target_instock (在庫あり寄り)               │
│ 【仕入先候補 (フリマ)】                                    │
│   [商品画像]  メルカリ ¥18,500  状態: 美品(A)             │
│   AI 一致度: 85 (高)  根拠: 型番・色一致、付属品同等        │
│   [仕入先リンク]                                          │
│ 【利益額 (真値)】                                         │
│   利益: ¥4,200 / $28.00   ← calculator.calculate 真値     │
│   けいすけ基準: PASS [緑バッジ]                            │
│   ⚠ Section232該当の可能性 [赤バッジ]                      │
│ 【推定重量】 280g (AI推定・確信度 medium)                  │
│  [承認 → 出品下書き生成]   [見送り]   [重量を手動修正]      │
└──────────────────────────────────────────────────────────┘
```

### 7-3. 設計ポイント (誤承認誘発の防止)

- **利益は真値 (FIX-2)**: `profit_jpy_true` / `profit_usd_true` を表示。けいすけ基準バッジは `keisuke_pass` を緑/赤で。
- **AI推定重量は必ず「推定値」と明示** + 確信度。利益が境界付近 (§14-Q2) の候補は `needs_review` に落とし**承認キューに出さない** → needs_review 一覧で重量手動入力後に再計算。
- **Section232 赤字警告**: 赤バッジ + 「DDP関税で赤字化リスク」。自動 BLOCK しない (機会損失回避) が user に明示。
- **見送り** = `update_status(rc_id, 'gate_rejected', reason='user 見送り')`。候補リストから消えるが DB に残る。
- **form 外ボタン** (W225 事故教訓)。連打防止は既存 `_EVAL_PROCESS_LOCK` パターン。

### 7-4. 承認後の出品下書き自動 (Phase 4)

「承認 → 出品下書き生成」押下で:
1. `update_status(rc_id, 'approved')`
2. **W226 description 生成を流用**して英語 description + Item Specifics を生成 (同期 + spinner、§14-Q5)。
3. **個別出品タブに pre-fill** (W176 即時反映 / altlist prefill 機構を流用): title / weight / condition_rank / description / 仕入先 URL を session_state に積む。
4. `update_status(rc_id, 'draft_generated')` + `listing_draft_id` 記録。
5. **在庫0承認時**: keyword watch 自動登録 (既存 `_register_keyword_watch` 流用)、`watch_ids_json` 記録。**在庫0アクティブ上限件数チェック** (P0-3) を通過した場合のみ。
6. 最終 eBay 公開は人間 (個別出品タブの公開ボタン)。公開成功ハンドラで `status='listed'` + `result_ebay_item_id` 記録 (§14-Q7)。

### 7-5. 在庫0出品の上限件数 (P0-3 / 履行不能=Defect 防止)

- `schedule_config.json` の `research.max_oos_active_listings` (初期 20)。
- 在庫0 watch 登録前に「現在 `target_oos_watch` 由来で active な listing 数」をカウントし、上限超過なら**登録を止めて Discord 通知** (Q0)。
- handling time 最大化・承認ラグ中に売れた場合の緊急フロー (即取下げ) は **USER_MANUAL.md に明記** (本設計では上限ガードのみ実装)。

---

## 8. 作成 / 修正ファイル一覧

### 作成

| ファイル | 役割 | Phase |
|---|---|---|
| `tasks/task_research_harvest.py` | Terapeak ハーベスト + 自動ゲート判定バッチ | 2 |
| `tasks/task_research_sourcing.py` | gate_passed 候補の AI重量→フリマ探索→利益→キュー積みバッチ | 3 |
| `monitor/research_section232.py` | タイトル→HS推定→Section232該当フラグ (ルールベース純関数、§14-Q4) | 3 |
| `tests/test_research_harvest.py` | ハーベスト抽出 + 重複排除 + ゲート連携 (mock html) | 2 |
| `tests/test_research_sourcing.py` | sourcing バッチ + コスト cap + 利益真値 | 3 |
| `tests/test_research_gate_persistence.py` | FIX-1 ゲート永続化 + 重複スキップ | 1 |
| `tests/test_research_profit_true.py` | FIX-2 利益真値 + けいすけ基準保存 | 1 |

### 修正

| ファイル | 修正内容 | Phase |
|---|---|---|
| `monitor/database.py` | v69 migration (research_candidates 拡張列、冪等) | 1 |
| `monitor/research_candidates_db.py` | 新 status 定数 + 遷移グラフ追加 / gate_*・profit_*_true・keisuke_* 書込関数 / found_condition_ja 書込 (FIX-4) | 1,2,4 |
| `monitor/research_poc.py` | FIX-2: 利益を calculator.calculate 真値に差し替え + けいすけ基準算出 / FIX-3: 再探索経路 / FIX-4: found_condition 記録 | 1 |
| `monitor/research_gate.py` | (変更最小) gate_inputs スナップショット用ヘルパ追加のみ検討 | 1 |
| `monitor/terapeak_scraper.py` | harvest_product_list / scrape_product_detail / active開始日抽出 追加 (既存関数は不変 / K2) | 2 |
| `tabs/tab_w228_research.py` | セクション D 承認キュー追加 / 承認後下書き生成 + watch + 在庫0上限ガード / FIX-3 再実行ボタン / stale docstring 修正 | 1,4 |
| `daily_scheduler.py` | `_run_research_harvest` / `_run_research_sourcing` ラッパ + add_job (03:30 / 04:30) | 2,3 |
| `monitor/task_execution_log.py` | TASK_SCHEDULE に research_harvest / research_sourcing 登録 | 2,3 |
| `config/schedule_config.json` | research セクション (filters / max_oos / daily_cost_cap / max_items) | 2 |
| 設定タブ | ハーベストフィルタ設定 UI (除外KW/カテゴリ/価格/seller国/並び順) | 2 |
| `USER_MANUAL.md` | 在庫0承認ラグ中に売れた場合の緊急手順、CDP事前起動手順 | 4 |

### ビルドシーケンス (依存順)

```
Phase 1: v69 migration → research_candidates_db 拡張 → research_poc FIX-2/3/4 → test → 既存Wizard で Q1
Phase 2: schedule_config research セクション → terapeak_scraper harvest関数 → research_section232 →
         task_research_harvest (重複排除+ゲート保存) → TASK_SCHEDULE登録 → scheduler add_job →
         設定UI → test → CDP実機 harvest 1ページ Q1
Phase 3: task_research_sourcing (重量AI→探索→利益真値→awaiting_approval) → コストcap → test →
         実機 gate_passed数件で sourcing Q1
Phase 4: 承認キューUI (セクションD) → 承認後下書き(W226流用) → 個別出品prefill(W176流用) →
         在庫0上限ガード → watch自動登録 → test → Playwright E2E Q1
```

---

## 9. データフロー

```
[user: CDP Chrome 起動 + eBay ログイン (事前1回)]
            │
03:30 ┌─────▼──────────────────────── task_research_harvest ──────────────┐
      │ 1. クォータ残チェック (api_call_log JST当日, market_analysis と合算)  │
      │      └ 残不足 → skip + Discord (Q0)                                │
      │ 2. 収穫 2パターン×各カテゴリ (§5-0):                                │
      │      ① Last7d/新着順 → 直近24h窓 / ② Last2y/古い順 → 730日前24h窓   │
      │      合算 max=50 (①優先で詰め、残枠を②へ)                          │
      │ 3. 各商品: harvest_keyword 正規化 → 既存 gate_decision 済なら skip   │
      │      (skip_too_new のみ再判定)                                      │
      │ 4. scrape_product_detail: 90d sold / active開始日 / 1-2yr sold     │
      │      (§14-Q6: sold_90d≥2 で確定する商品は active 取得 skip)         │
      │      └ anti-bot 連続失敗 → 即停止 + Discord、残翌日 (W7-A)          │
      │ 5. evaluate_sourcing_gate(...) 純関数 → decision/reason            │
      │ 6. research_candidates へ gate_* 保存:                             │
      │      target_* → status='gate_passed' / それ以外 → 'gate_rejected'  │
      │ 7. Discord: 発掘N / 通過M / 除外K                                  │
      └────────────────────────────────────────────────────────────────┘
            │ (gate_passed 候補)
04:30 ┌─────▼──────────────────────── task_research_sourcing ─────────────┐
      │ 1. 日次コスト cap チェック (W209 fail-closed)                       │
      │ 2. AI 重量推定 (流用) → manual_weight_g + weight_source/confidence  │
      │ 3. フリマ探索 (mercari/yahoo/paypay) ← research_poc 流用            │
      │      取得エラー → needs_review (技術失敗) / 0件 → not_found (業務)    │
      │ 4. claude_evaluator 同一性 → match_score + found_condition_ja(FIX4)│
      │ 5. 利益真値: calculator.calculate → profit_jpy_true (FIX-2)         │
      │      けいすけ基準 (§14-Q1: 還付抜き profit で 率6% OR ¥600)          │
      │ 6. Section232 推定 (ルールベース) → section232_flag                 │
      │ 7. 境界判定 (§14-Q2) → needs_review / それ以外 → awaiting_approval  │
      │ 8. Discord: 探索成功K / 承認待ち追加J / コスト$X                    │
      └────────────────────────────────────────────────────────────────┘
            │ (awaiting_approval 候補)
[user: 承認キュー UI で承認/見送り]  ← user の唯一の作業
            │ 承認
      ┌─────▼──── Phase4 承認後自動 ────────────────────────────────────┐
      │ approved → W226 description 生成 (同期+spinner) → 個別出品 prefill  │
      │ 在庫0時: 在庫0上限ガード (P0-3) → keyword watch 登録 (W206)        │
      │ → draft_generated                                               │
      └────────────────────────────────────────────────────────────────┘
            │
[user: 個別出品タブで最終確認 → eBay 公開ボタン] → listed (+result_ebay_item_id)
```

---

## 10. 各 Phase の DoD (Q1: pytest + 実機検証経路)

各 Phase 完了は pytest PASS だけでは不可 (K3)。Streamlit + DB クエリ + 実機ログまで。

### Phase 1 DoD
- pytest: ゲート永続化 / 利益真値が calculator.calculate と一致 / けいすけ基準保存 / found_condition_ja 書込 / v69 冪等 (init_db 2連続でデータ保持)
- DB SELECT: gate_decision / profit_jpy_true / keisuke_pass が NULL でないことを実機確認
- Streamlit: 既存 Wizard セクション A で判定 → DB に gate_decision が残る (再起動後も保持)
- 検算: 旧表示 (1.24倍過大) と新真値の差を 1 候補で手計算照合 (verify_numbers)

### Phase 2 DoD
- pytest: harvest 抽出 (mock html) / 重複排除 / ゲート 5分岐が DB status に正しく落ちる / クォータ不足 skip
- 実機: CDP Chrome で Terapeak 1ページ実 harvest → 3〜5商品が gate_decision 付きで DB 着地。scheduler.log に「発掘N/通過M」
- anti-bot: 連続失敗時に即停止 + Discord 到達 (R-11 user 実視認)
- TASK_SCHEDULE: MonoDeck 定時実行タブに research_harvest が出る

### Phase 3 DoD
- pytest: sourcing バッチ / コスト cap 超過で中断 / 利益真値 / 境界判定で needs_review
- 実機: gate_passed 数件で sourcing 実行 → match_score + profit_jpy_true が DB 着地、api_call_log にコスト記録
- Q0: 取得エラー = needs_review、0件 = not_found に分離されることを DB で確認

### Phase 4 DoD
- pytest: 承認 → status 遷移 / 下書き生成 / 在庫0上限ガード / watch 登録 + watch_ids_json
- Playwright E2E: 承認キューで1候補承認 → 個別出品タブに prefill される (実画面往復)
- DB: keyword_watches に source='w228_research' で行追加、watch_ids_json 記録
- R-11: 承認キュー件数の Discord 通知 user 実視認

---

## 11. リスク表とガード

| リスク | 影響 | ガード | Phase |
|---|---|---|---|
| **anti-bot 検知** (Terapeak) | アカウント停止 | 連続失敗5で即停止 + error redirect 検知 + jitter sleep + 50件/日上限 + fail-loud Discord (W7-A 流用) | 2 |
| **履行不能** (在庫0で買えない) | Defect=アカウント停止 (最優先) | 在庫0 active 上限 (P0-3) + handling time 最大化 + 緊急手順 (USER_MANUAL) + watch 上限価格必須 | 4 |
| **誤承認誘発** (UI 誤情報) | 赤字仕入 | 利益真値表示 + けいすけバッジ + Section232警告 + 推定重量明示 + 境界は非表示 | 1,4 |
| **コスト暴走** (Claude API) | 課金事故 | 日次 cost cap fail-closed (W209) + 再評価 skip + 「再生成」別ボタン隔離 + 50件/日 | 3 |
| **クォータ枯渇** (250/日 共有) | market_analysis 阻害 | バッチ冒頭で JST 当日累計合算チェック、残不足 skip + Discord。時間分離 (日02:00 vs 毎日03:30) | 2 |
| **偽黒字** (weight 0 clip) | 誤仕入 | weight 欠落で needs_review (P1-1)。AI推定失敗時も 0 clip しない | 3 |
| **ゲート再判定漏れ** | 機会損失 | skip_too_new は再出現時に開始日更新して再判定 | 2 |
| **状態機械迂回** | silent skip | 全遷移 CAS + `_ALLOWED_TRANSITIONS` 経由のみ | 1 |
| **migration 半成立** | スキーマ不整合 | 列実在確認後 user_version bump (v67 流儀) + try/except 冪等 | 1 |

### 環境特異性チェックリスト (Windows / pythonw / Streamlit)
- pythonw: `sys.stdout.reconfigure` ガード必須、`print(file=sys.stderr)` 禁止 (Quality Gate hook)
- Streamlit: form 内 st.button 禁止 (W225)、threading.Lock プロセス共有で直列化 (既存)
- Playwright: ProactorEventLoop を別 thread で set (既存 thread wrapper 流用)
- cp932: ファイル read/write は UTF-8 明示
- CDP: Chrome port 9222 事前起動 + eBay ログインは user 操作
- SQLite TZ: api_call_log.called_at は UTC、JST 当日集計は `+9 hours` shift

---

## 12. Plan → Verify → Persist → Automate

- **Plan**: 本設計書 (Q3 構造化フロー、Clarify は 2026-06-10 ヒアリングで完了)
- **Verify**: 各 Phase の DoD (§10) を code-reviewer HIGH=0 + Codex 2段で通過してから次 Phase
- **Persist**: research_candidates に全状態永続化 (session_state 揮発を FIX-1 で解消)
- **Automate**: 03:30/04:30 夜間バッチ + 観測3経路 (DB/Discord/UI)、user 作業=承認のみ

---

## 13. 設計質問 (code-architect 提出、7件)

1. けいすけ基準の「還付抜き」整合性 (check_supplier_candidate_profitable は還付込み引数)
2. 境界レビュー閾値 ±20% の母数 (利益額 or 基準ライン)
3. ハーベスト除外KW の初期リスト (vero_brands.json 流用可否)
4. Section232 推定の手段 (ルールベース or Claude)
5. 承認後下書き生成の同期/非同期
6. ゲート分岐1 確定商品の active 取得 skip (クォータ節約)
7. `listed` への遷移トリガ (手動 or 自動検知)

## 14. 設計決定 (2026-06-10 Fable 5 判断、仕様書・実コード・既定値で確定)

| Q | 決定 | 根拠 |
|---|---|---|
| **Q1** | **けいすけ基準は専用純関数を新設** (`keisuke_check(profit_jpy, revenue_jpy)` = 率6% OR ¥600 either-or)。入力は calculator.calculate の `profit` (= **還付抜き**、calculator.py L474-481 で `profit_with_refund` と別計上を実証済) | 仕様 §7-4「還付抜き」が正本。既存 `check_supplier_candidate_profitable` は別基準 (¥600 + 10-20%線形 floor = 仕入候補用) であり、けいすけ基準 (6% OR ¥600) とは**別物** — 流用すると基準が変わる。K1 で小さな純関数を足すのが正 |
| **Q2** | 境界 = **「達成に必要な最小利益ライン (min(¥600, 売上×6%)) の ±20% 帯」**。profit がこの帯内なら needs_review | 母数を基準ラインに固定すると挙動が直感的 (¥600 商品なら ¥480〜720 が要レビュー帯)。AI 推定重量の誤差が利益判定を反転させ得る範囲だけ人間に回す |
| **Q3** | **vero_brands.json を除外KW 初期リストとして流用** + アニメ/IP 系ワードを追加。設定 UI で編集可 | 既存資産流用 (K1)。VeRO 自動採用禁止 (仕様 §9) と一致 |
| **Q4** | **(a) ルールベース** (鋳鉄/トランス/炊飯器/冷蔵/モーター等のキーワード辞書 → Annex I-A/I-B フラグ)。最終分類は人間 | コスト 0、Section232 KB (topics/section_232_tariff_2026_04.md) の HS リストから辞書化。Claude 推定は精度向上が必要になってから (3回ルール) |
| **Q5** | **同期 + spinner** (承認ボタン押下 → その場で description 生成)。失敗時は status='approved' のまま + 「下書き再生成」ボタン (Q0 痕跡 + リトライ経路) | 承認は 1 件ずつの操作で数十秒待ちは許容。非同期バッチだと「承認したのに下書きがない」状態が長く Q0 的に不健全 |
| **Q6** | **YES — sold_90d≥2 (分岐1) で確定する商品は active タブ取得を skip** | ゲート行1 は active 不問で確定 (research_gate.py 実装と一致)。クォータ ~30% 節約見込み |
| **Q7** | **個別出品タブの公開成功ハンドラで自動遷移** (prefill に rc_id を載せ、AddItem 成功時に `listed` + `result_ebay_item_id` 記録)。ebay_sync スキャン方式は採らない | 公開経路が rc_id を知っている時点で更新するのが決定論的。sync スキャンは SKU/タイトル突合が必要になり脆い (sku-rules 抵触リスク) |
| **Q8** (2026-06-10 Phase 1 レビュー MED-1 受け) | **けいすけ基準 6% の分母 = calculator の revenue (商品価格+送料、円)** に統一。Terapeak avg×fx の独自計算は使わない | 分子 profit は calculator 由来で buyer 送料収入込み。分母だけ商品価格のみだと率が実態より高く出て PASS 側に甘くなる (money-direct)。分子分母を同一基盤 (calculator) に対称化 = 判定はやや厳しめ側で安全。基準値は keisuke_detail_json に分母値も保存し監査可能化 |
| **Q9** (同上 HIGH-1 受け) | **found_condition_ja は仕入先タイトルの日本語キーワード辞書で推定** (CLAUDE.md「Claude 自動推定」表: 新品/未開封→N 系 … ジャンク/動作未確認→As-Is 系)。キーワード無一致は None (推定を捏造しない) | claude_evaluator の EvaluationResult に condition 属性は存在せず (実装時の getattr no-op が HIGH-1)。evaluator prompt 改修より既存規約のキーワード表流用が K1。AI 構造化抽出は精度不足が実証されてから (3回ルール) |
| **Q10** (2026-06-10 user 確定) | **収穫は 2 パターン × 各カテゴリ** (§5-0): ① Last 7 days + Date last sold 新着順 → 直近 24h 窓 / ② Last 2 years + 古い順 → 「今日−2年」24h 窓。50件/日 cap は合算、①優先で詰めて残枠を② | user 指示 (verbatim 2026-06-10)。①優先配分は assistant 判断 (鮮度型は当日逃すと価値喪失、②は翌日でも同等の窓が来る)。HarvestedProduct に date_last_sold、rc に harvest_pattern 列 (v70) を追加 |
| **Q11** (2026-06-10 Phase 1 レビュー LOW 知見、Phase 4 実装時必須) | **承認キュー UI は `keisuke_pass=0` を「不合格」と「未判定」に区別して表示する**: `keisuke_detail_json` が `{}` (空) なら未判定 (利益計算不能 = needs_review 系)、値ありなら真の不合格 | save_profit_true は keisuke_result=None でも keisuke_pass=0 を書く (NOT NULL 列)。DB 上 0 が二義的なため、表示層で detail_json の有無により判別。Phase 1 では実害なし (3 巡目レビュー LOW-1) |

---

## 15. ROADMAP 連携

- **W229 (id300)**: Phase 2 (ハーベスト + 自動ゲート) が本体。status を「実装着手 2026-06-10」へ
- **W228 (id299)**: Phase 1 (FIX-1〜4) + Phase 3/4 (B自動化 + 承認キュー) が残作業。status 更新
- 進行順は user 指示どおり W229 優先だが、FIX-1 (ゲート永続化) が W229 の前提インフラのため **Phase 1 → 2 → 3 → 4** の実装順は不変

---

## 16. Phase 1 実装記録 (2026-06-10 完了、Q1 実機検証済)

### 16-1. Q1 実機検証が発見した追加修正 3 件 (pytest 全 PASS でも実機は壊れていた)

| # | 発見 | 真因 | 修正 |
|---|---|---|---|
| **FIX-E** | UI からの仕入先検索が 3 サイト全滅 → 全商品 not_found 偽装 (3 回連続 0 件で発覚) | Streamlit (tornado) は Windows で SelectorEventLoop = **子プロセス起動不可** → Playwright が NotImplementedError 即死 → mercari_search.py 等の except が warning だけで空リスト返却 → evaluate_product が「0 件 = 在庫なし」と誤判定 (インフラ故障が業務判定に化ける Q0 型)。証拠: logs/streamlit_start.err の NotImplementedError traceback | **subprocess 分離**: 新規 `monitor/research_search_cli.py` (`python -m`、keyword=stdin UTF-8、JSON stdout)。フレッシュ python = ProactorEventLoop で Playwright 正常。`_search_freemarket` は subprocess 失敗時 **raise** → search_errors 機構が needs_review に落とす (故障と 0 件の構造的区別)。scheduler 共用の検索本体は無変更 (K2) |
| **FIX-A 追補1** | リトライで行分裂 (rc_id=5 gate行 / rc_id=6 利益行に分離、gate_decision=None) | tab 側が gate rc_id を **pop-once** で消費 → 1 回目が入力不備で needs_review に落ちると、リトライは gate 連携なしの新行 | pop → get (キー保持)。同 title の再実行は同じ行を更新。誤着地は title 一致ガード + evaluate_product 側 DB title 照合 (HIGH-1) の 2 層で防御済み |
| **FIX-A 追補2** | 再利用パスで terapeak_avg_price_usd / manual_weight_g が NULL のまま利益だけ書かれる (rc_id=10) | 再利用パスは INSERT を通らず入力スナップショット 5 列が書き戻されない | `rc_db.update_input_snapshot` 新設 (verbatim 上書き = 行は常に最後の評価入力を反映、利益値と入力値の不整合を作らない。rowcount=0 は ValueError / Q0) |

### 16-2. 検証エビデンス

- 実機 E2E (Playwright + Streamlit 再起動 + DB SELECT) 計 6 回: FIX-A 着地 / checkbox 正常 / FIX-E 発見 / sourced 経路 (rc_id=6) / リトライ同居 (rc_id=10) / スナップショット書き戻し (rc_id=13)
- 最終形 rc_id=13: gate_decision=target_instock + terapeak=120.0 + weight=300.0 + profit_jpy_true=10974 + keisuke_pass=1 が**同一行に同居** (承認キューの前提成立)
- けいすけ手計算照合 (rc_id=6): 14254/26140=54.53% ✓ / threshold=min(600, 26140×0.06=1568)=600 ✓ / pass_600 ✓ / pass_rate ✓
- pytest: W228 系 96 PASS (gate 永続 + search_cli 12 + gate 2026-06-07 + profit_true)
- レビュー: code-reviewer (Fable 5) 4 巡 + main agent 直接 2 巡 + Codex 2 段、HIGH=0 到達 (HIGH-1 stale rc_id 汚染 / except-pass CRITICAL ほか捕捉)

### 16-3. Phase 振り分け済み残項目

| 項目 | 内容 | Phase |
|---|---|---|
| MED-2 | sourcing 滞留行 (評価中クラッシュ) の回復導線 | Phase 4 |
| MED-3 | セクション C にけいすけ判定の表示 (Q11 と同じ場所) | Phase 4 (必須要件昇格) |
| LOW (新規) | セクション A の判定ボタン連打で gate 行が複数できる (rc_id=11/12 残骸)。承認キューは sourced 中心のため実害小だが、stale gate_passed 行の掃除 or 同 title 直近行再利用を検討 | Phase 4 |
| LOW-3 | keisuke_pass=0 の「不合格/未判定」二義性 → Q11 で表示層判別 | Phase 4 |
| テスト行掃除 | research_candidates の Q1 検証行 (rc_id=1〜13) 削除は user 確認後 (one-shot script、Q2) | user 確認待ち |
