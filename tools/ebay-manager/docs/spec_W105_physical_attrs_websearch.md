# W105: 商品物理属性 (重量・寸法) Web 自動取得機能 PRD

**ID**: W105 / `system_improvements.json` id=191
**起案日**: 2026-05-06
**起案者**: user (eBay 物販担当)
**実装者想定**: assistant (Sonnet 4.6 / Haiku 4.5 mixed)
**前提**: `/feature-dev` 経由起動を試みたが Unknown command で失敗 → planner agent で代替設計
**関連 rule**: K0-K3 / Q0 (silent skip 禁止) / Q2 (DB 冪等性) / Q3 (新機能 + 外部 API = 本 PRD で代替)
**SUPERSEDE 対象**: 既存 `task_estimate_weights_claude.py` (Haiku タイトル → 重量推定、寸法非対応) は本機能で代替・退役。`task_enrich_listings_physical.py` (eBay GetItem) は populate 0 件のため scheduler から外す。

---

## 1. 機能名・目的・業務背景

### 機能名
**Physical Attribute WebSearch Enrichment** (略: `pa-websearch` / `phys_websearch`)

### 目的
出品中 441 件 (および新規出品時) の **重量 (g) と外寸 (length / width / height cm)** を、商品名・型番から **Anthropic API の Web Search tool で公式 spec を検索し自動取得**、利益計算 (容積重量) と送料計算に使えるようにする。

### 業務背景 (現状の痛み)
- **手作業**: 利益計算タブ・個別出品タブで user が毎回手入力。体感 1 件 30 秒、1 日 5-10 件で 5 分ロス。
- **DB の信頼性ゼロ**:
  - `weight_g`: 100% 設定済 (見かけ上)
    - 内訳: claude タイトル推定 252 件 (57%) + **`default_500g` 適当決め打ち 189 件 (43%)**
  - `length_cm` / `width_cm` / `height_cm`: **0% (誰も取れていない)**
- **下流影響**:
  - 容積重量 (LWH/5000) が計算できない → 大型軽量品で送料赤字発生
  - `task_enrich_listings_physical` は eBay GetItem を叩くも eBay 側が 0 で返してくる listing が多数 → 寸法 populate 0 件
  - `task_estimate_weights_claude` はタイトルのみで推定 → 寸法対応不可、`default_500g` 189 件解消の半分しか進まない

### 解決アプローチ (確定)
**Anthropic Web Search tool + Haiku 4.5** で、商品タイトル → メーカー公式サイトや eBay 既存出品ページから「型番 + spec」を検索 → JSON 抽出。失敗時は値を入れず `weight_source='websearch_failed'` でマーキング → DASHBOARD で user 手動入力。

---

## 2. 確定仕様 (Q1-Q5 user 確認済 / 2026-05-06)

| ID | 仕様 | 内容 |
|----|------|------|
| Q1 | 取得経路 | **Haiku 4.5 + WebSearch tool のみ**。仕入先 scrape / eBay GetItem / Claude 単独タイトル推定は使わない |
| Q2 | 既存値上書き | **全件上書き** (claude 252 + default_500g 189 = 441 件すべて websearch 結果で更新) |
| Q3 | backfill | **1 回限り 441 件** バッチ実行、寸法 0% 解消最優先 |
| Q4 | UI | 個別出品タブ + 利益計算タブに「自動取得」ボタン。**既存値あれば cache 即返答 / ボタンで強制再取得** |
| Q5 | 失敗時 | 値を **入れない** + `weight_source='websearch_failed'` マーキング、DASHBOARD「物理属性未取得」リストに表示 → user 手動入力 |

### 運用フロー (新規出品時)

```
個別出品ウィザード (W9 タブ)
  ↓ user がタイトル + URL 入力
  ↓ [自動取得] ボタン押下 (もしくはウィザード Step 2 で自動 trigger)
  ↓ websearch_lookup(title, url, brand?) → 5-15 秒 (1 search, max_uses=2)
  ↓ ┌─ 成功: weight_g / L / W / H + confidence 'high|medium' を form に prefill
    ├─ 公式 spec ヒットなし: confidence='low' で参考値 prefill + user 警告 banner
    └─ websearch error: 値空 + 「自動取得失敗、手動入力してください」
  ↓ user が確認・修正後 [出品] 押下
```

### 運用フロー (既存出品 backfill)

```
1 回限りバッチ (`scripts/backfill_physical_websearch.py`)
  ↓ active 441 件を rank 昇順で順次処理
  ↓ 各 listing: title + ebay_item_id → websearch_lookup
  ↓ 成功 → DB 上書き (weight_source='websearch', confidence 記録)
  ↓ 失敗 → weight_source='websearch_failed' マーキング (値は変更しない)
  ↓ Discord 完了通知 (成功 N / 失敗 M / cost 試算 $X)
  ↓ user が DASHBOARD「未取得」タブで失敗分を確認・手動入力
```

### 運用フロー (週次 batch / 新規出品分のみ)

```
daily_scheduler weekly trigger (毎週月曜 02:30 / 朝バッチ枠)
  ↓ WHERE is_ended=0 AND (weight_source IS NULL OR weight_source IN ('default_500g', 'claude'))
    最大 max_items_per_run=20 件
  ↓ websearch_lookup → DB 反映
  ↓ task_execution_log 記録 + Discord サマリー
```

---

## 3. State Machine: weight_source の遷移

```
[NULL] (新規 listing)
   │
   ├─ default_500g (eBay GetItem で重量0 → fallback)        ★既存退役予定
   ├─ claude (タイトル推定)                                  ★既存退役予定
   │
   ▼ Phase 1-2 完成後
[websearch]                  ← Web Search 成功 + 公式 spec ヒット
[websearch_failed]           ← Web Search 実行したが spec 取得不能
[manual]                     ← user が UI で手入力 (上書き優先 = websearch でも上書きしない)
[manual_override]            ← user が websearch 結果を確認後修正
```

### 遷移ルール

- `manual` / `manual_override` は **websearch 自動上書き対象外** (user 信頼を最優先)
- `websearch_failed` は週次 batch で再 retry 対象に含める (Web 上に新規 spec が出る可能性)
- backfill 1 回目は `manual` / `manual_override` 以外すべて上書き対象

### DB schema (既存カラム流用 / 追加 1 つ)

| カラム | 既存/新規 | 値 |
|--------|----------|----|
| `weight_g` | 既存 | float, 重量 (g) |
| `length_cm` / `width_cm` / `height_cm` | 既存 | float, 外寸 (cm) |
| `weight_source` | 既存 | 文字列 (上記 state) |
| `weight_confidence` | 既存 | 'high' / 'medium' / 'low' |
| `weight_estimated_at` | 既存 | TIMESTAMP, 取得時刻 |
| **`physical_attrs_evidence`** | **新規追加** | TEXT (JSON 文字列、citation URL list + cited_text 抜粋、user が確認可能に) |

---

## 4. Phase 3 Clarify 結果 (10 不確実性 → 推奨案 + 代替案)

### #1 Anthropic WebSearch tool: 精度 / rate limit / cost

**事実 (公式 doc 確認済)**:
- 価格: **$10 / 1,000 searches** + token cost (search 結果は input token 加算)
- Haiku 4.5 でも利用可 ($1/MTok input, $5/MTok output)
- `max_uses` で per-request の search 回数制限可
- error 時は 200 応答 + `web_search_tool_result_error` (silent fail にはならない = Q0 適合)
- ZDR 適用時は `allowed_callers` workaround 必要

**推奨案**:
- **Haiku 4.5 + max_uses=2** で 1 listing あたり search 1-2 回 (公式サイト 1 + 補強 1)
- 1 listing あたり実コスト概算:
  - search: 2 × $0.01 = **$0.02**
  - token: input ~5,000 (search results 含む) × $1/MTok + output ~300 × $5/MTok = **$0.0065**
  - 合計 **~$0.027 / listing**
- backfill 441 件 = **~$12** (許容範囲)
- 週次 batch 20 件 × 4 週 = 月 80 件 = **~$2.16 / 月**

**代替案**:
- A: Sonnet 4.6 escalation (公式 spec 取得困難な高 value 商品のみ) — Phase 6 検討
- B: max_uses=1 でコスト半減、ヒット率 -10〜20% の trade-off
- C: confidence='low' 連続発生時のみ Sonnet retry (v1.1 検討)

### #2 ジェネリック商品名 (例「Mouse pad」「USB cable」)

**事実**: 公式 spec が存在しない / 無数のバリエーションあり / メーカー不詳。

**推奨案**:
- websearch で「公式 spec 風の数値が一切ヒットしない」と Haiku が判断したら `confidence='low'` で **値を入れず** `weight_source='websearch_failed'` を記録
- DASHBOARD「物理属性未取得」リストに表示し、user が手入力
- Q5 仕様通り (silent fail せず明示マーキング) で適合

**代替案**:
- A: 「ジェネリック判定キーワード」(例: 出品タイトルが 4 単語以下 + ブランド名なし) で **websearch を skip** し直接 `websearch_failed` マーキング → cost 削減 (推奨)
- B: 平均値 (Mouse pad = 200g 等) を fallback として保持 — **K0 違反 (default_500g の二の舞)** なので却下

### #3 confidence 閾値判定基準

**推奨案** (Haiku の system prompt で固定):
- `high`: タイトル内に **明確な型番** (例: `ATH-CKS330NC` / `NV-25`) を抽出 + 公式メーカーサイト or 大手 retailer (Amazon/B&H/楽天) の spec ページから数値を取得
- `medium`: 型番一致だが spec 出典が小規模 retailer / blog / forum
- `low`: カテゴリ推定のみ (= 上記 #2 ジェネリック扱い、`websearch_failed` 同等)

Haiku の出力 JSON で `confidence` + `evidence_url` + `cited_text` を必ず返させる。

**代替案**: 5 段階化 (very_high / high / med / low / very_low) — K1 違反 (overengineering)、3 段階で十分。

### #4 仕入先 scrape 補助 (Web 検索失敗時の再 fallback)

**user 明言**: 不要 (Q1 で「WebSearch のみ」確定)。

**推奨案**: 仕入先 scrape は **fallback でも使わない**。Q5 通り `websearch_failed` で人間に escalate。
- 理由: 仕入先サイトには寸法・重量がほぼ載っていない (mercari/yahoo) → 復活させても効果薄
- 既存 `monitor/supplier_scraper.py` の `weight_hint_g` / `length_mm` は本機能で参照しない

**代替案**: 無し (user 確定方針)。

### #5 新規出品 wizard 統合 (W9 個別出品タブの取得 trigger 段階)

**現状コード**: `app.py` 5832行 `render_individual_listing_tab(s)` (詳細未読、`monitor/individual_listing.py` 等)

**推奨案**:
- **明示的な「自動取得」ボタン** をウィザード Step 2 (商品情報入力) に配置
  - 自動 trigger は K1/K2 違反 (毎回 search → cost 暴走 + user 操作妨害) なので避ける
- ボタンクリック → spinner 5-15 秒 → form の重量・寸法 input に prefill
- `weight_source='manual_override'` でマーキング (user 確認済 = 手動扱い)

**代替案**: ウィザード進行で自動 trigger — cost 暴走 + back-and-forth で同じ listing を複数回検索する事故リスク → **却下**。

### #6 DASHBOARD「未取得」リスト UI

**推奨案**:
- 場所: 既存 DASHBOARD タブ内に `tab_dashboard_phys_pending` という子セクション (新タブにはしない、K1)
- 件数表示: 「物理属性未取得: 12 件」(weight_source IN ('websearch_failed', NULL))
- 一括手動入力 form: dataframe (editable) で title / weight_g / L / W / H を inline 編集 → 「保存」ボタンで一括 UPDATE → `weight_source='manual'` マーキング
- Discord 通知: 不要 (DASHBOARD で日常確認可、過剰通知防止)

**代替案**:
- A: 一覧から「個別 web 再検索」ボタン → 個別単位 websearch retry (cost: $0.027/click)
- B: 失敗 listing を出品タブの該当 listing 編集モードに飛ばす — UX 散らかる、却下

### #7 週次 batch cron 時刻 (既存 02:30 main batch との衝突回避)

**事実**: `daily_scheduler.py` は 02:30 / 11:00 / 15:00 / 18:00 / 22:00 の 5 枠、02:30 の main batch は ebay_sync → inventory_check → ... → enrich_listings_physical → estimate_weights_claude → daily_relist の重い直列実行で 30-60 分かかる。

**推奨案**:
- **02:30 朝バッチ枠の同列に新規 task `enrich_physical_websearch` を追加**、既存 `enrich_listings_physical` と `estimate_weights_claude` の **両方を退役** (置き換え)
- 既存 2 task は `tasks_enabled.<key>.enabled = false` で kill switch off、ファイルは残置 (Phase 6 で削除判断)
- 実行頻度: 毎日 02:30 ではなく **週 1 回 (月曜のみ)** → `TASK_SCHEDULE` の `weekdays=[0]`
- 1 回 max_items=20、週次なので 4 週で 80 件まで対応 (新規出品 + websearch_failed retry 用、既存全置換は backfill 1 発で済ませる)

**代替案**: 月次に絞る → 新規出品反映が遅く却下。

### #8 API cost 上限 (Anthropic Console 月予算)

**現状**: `feedback_api_key_hygiene_routine.md` の月予算 cap 設定済 (額は session memory 参照)。

**推奨案**:
- backfill 単発 ~$12 + 週次 batch ~$2/月 = **月 cap への影響軽微**
- backfill 開始前に user 承認の確認 prompt: 「441 件 × ~$0.027 = 概算 $12、実行しますか?」
- 週次 batch は安全装置として `max_items_per_run=20` をハードリミット、cap は変更不要
- API call 1 つごとに `monitor.api_logger.log_anthropic_response` で cost 記録済 (既存資産流用)

**代替案**: 別 API key 払い出し → 運用複雑化 + Console 統合監視できず却下。

### #9 test 戦略 (mock vs 実 API)

**推奨案** (3 層):
1. **Unit test (mock)**: WebSearch のレスポンス JSON 構造 (`web_search_tool_result` ブロック等) を mock し、JSON parse / confidence 分類 / DB 書込ロジックの test。pytest 6-8 件想定。
2. **Integration test (実 API、low-cost)**: Haiku 4.5 で 3 件の「known good」title (ATH-CKS330NC / NV-25 / Razer Huntsman) を実際に search、結果が許容誤差 ±20% に収まるか。pytest mark `slow` + `requires_api_key`。手動実行 only、CI スキップ。コスト ~$0.08/run。
3. **E2E (Streamlit + Playwright)**: 個別出品タブで「自動取得」ボタン押下 → form prefill → DB UPDATE → ebay GetItem 1 往復 (新規出品で値が反映されるか) → Q1 DoD 11 ステップ準拠

**代替案**: VCR (HTTP recording) で実 API レスポンスを録画 → CI で再生 — 設定コスト > 価値、却下。

### #10 失敗時 retry ポリシー

**推奨案** (error_code 別):
| error_code | 対応 |
|------------|------|
| `too_many_requests` | 30 秒 sleep + 1 回 retry、それでも失敗 → `websearch_failed` |
| `unavailable` | 同上 (Anthropic 内部エラー) |
| `max_uses_exceeded` | retry せず `websearch_failed` (max_uses=2 設定で起こる = spec 不在判断) |
| `query_too_long` | retry せず `websearch_failed` (タイトル長過ぎ → 別途 truncate) |
| `invalid_input` | retry せず logger.error + Discord 通知 (実装バグの可能性) |
| network error (httpx 例外) | 30 秒 sleep + 1 回 retry → `websearch_failed` |
| Web に spec ない (Haiku が confidence='low' 出力) | retry せず `websearch_failed` |

最大合計 **2 回試行** (初回 + retry 1)。Q0 silent skip 防止のため失敗は必ず `task_execution_log` + `weight_source='websearch_failed'` で痕跡を残す。

**代替案**: exponential backoff 3 回 retry → cost 暴走 + user 待ち時間長期化、却下。

---

## 5. Phase 分割 + 実装ブループリント

### Phase 0: PRD 確認 + W 番号登録 (本ドキュメント、user 承認 gate)

**所要**: 0.5h (user レビュー)
**DoD**:
- [ ] user が本 PRD を読み Phase 1 着手承認
- [ ] `data/system_improvements.json` に id=191 / W105 登録 (title / status='pending' / phase='design')
- [ ] 既存 `task_estimate_weights_claude` / `task_enrich_listings_physical` の退役計画を user 承認

**成果物**: 本 PRD + system_improvements.json 1 行追加

---

### Phase 1: WebSearch helper module 新規

**ファイル新規作成**:
- `monitor/physical_attrs_websearch.py` (中核 helper)
- `tests/test_physical_attrs_websearch.py` (mock test 6-8 件)

**実装内容**:
```python
# monitor/physical_attrs_websearch.py
"""
商品物理属性 (重量・寸法) Web 自動取得

Anthropic API の web_search tool + Haiku 4.5 で、
商品タイトル → 公式 spec → JSON 抽出。

使い方:
    result = lookup_physical_attrs(title="...", ebay_item_id="...", url="...")
    # result = {
    #   "weight_g": 250.0, "length_cm": 18.0, "width_cm": 12.0, "height_cm": 4.0,
    #   "confidence": "high",
    #   "evidence": [{"url": "...", "cited_text": "..."}],
    #   "source": "websearch",  # or "websearch_failed"
    #   "error_code": None,     # or 'too_many_requests' etc
    # }
"""

MODEL = "claude-haiku-4-5-20251001"
MAX_USES = 2  # search 回数 cap = cost cap

SYSTEM_PROMPT = """あなたは越境EC物販で発送サイズ・重量を確定する専門家です。
入力された商品タイトルを web search し、公式 spec / 大手 retailer (Amazon/B&H/楽天) の数値を取得してください。

出力は厳密 JSON (前後テキスト・```json フェンス禁止):
{
  "weight_g": <number 0-50000 or null>,
  "length_cm": <number 0-200 or null>,
  "width_cm": <number 0-200 or null>,
  "height_cm": <number 0-200 or null>,
  "confidence": "high" | "medium" | "low",
  "evidence_url": "<最も信頼度の高いソース URL>",
  "cited_text": "<spec 数値が記載された原文 (~150字)>",
  "reasoning": "<一文で取得経緯>"
}

confidence 基準:
- 'high': 型番が明示され、メーカー公式サイト or 大手 retailer から spec を取得
- 'medium': 型番一致だが小規模 retailer / forum / blog
- 'low': カテゴリ推定のみ、または spec 数値が見つからない

low の場合は weight_g / 寸法を null で返してください (推測値を入れない)。
梱包込み発送重量 (送料計算用) を優先 (本体実重量よりやや重め)。"""

def lookup_physical_attrs(title: str, ebay_item_id: str, url: str | None = None) -> dict:
    """1 件 lookup。retry は呼び出し側で実装、ここは 1 試行のみ。"""
    # anthropic.Anthropic() client
    # tools=[{"type":"web_search_20250305","name":"web_search","max_uses":MAX_USES}]
    # messages=[{"role":"user","content":f"Title: {title}\nURL: {url or 'N/A'}\neBay ItemID: {ebay_item_id}"}]
    # 結果から server_tool_use error_code 検出 / JSON parse / 数値 sanity check (50kg/200cm 上限)
    ...

def lookup_with_retry(title: str, ebay_item_id: str, url: str | None = None, max_retries: int = 1) -> dict:
    """retry 込み (最大 1 retry, 30s sleep)、失敗時 source='websearch_failed' を返す。"""
    ...
```

**追加 DB 関数** (`monitor/database.py`):
```python
def update_ebay_listing_physical_websearch(
    ebay_item_id: str, *,
    weight_g: float | None,
    length_cm: float | None, width_cm: float | None, height_cm: float | None,
    confidence: str,
    source: str,  # 'websearch' or 'websearch_failed'
    evidence_json: str,
) -> None:
    """websearch 結果を ebay_listings に書き込む。
    source='websearch_failed' の時は weight_g / 寸法を更新せず、
    weight_source / weight_estimated_at / physical_attrs_evidence のみ更新。
    """
```

**migration**: `physical_attrs_evidence TEXT` カラム追加 (try/except OperationalError、Q2 冪等性必須)
- `monitor/database.py` の ALTER TABLE 追加箇所に append

**所要**: 4-5h (実装 + mock test + 冪等性 verify)

**DoD**:
- [ ] `monitor/physical_attrs_websearch.py` 新規 (~150 行)
- [ ] `tests/test_physical_attrs_websearch.py` 6-8 件 PASS:
  - test_parse_success_high_confidence
  - test_parse_success_low_confidence (null fields)
  - test_error_too_many_requests_retry
  - test_error_max_uses_exceeded_no_retry
  - test_invalid_json_response
  - test_weight_out_of_range (>50kg → reject)
  - test_dim_out_of_range (>200cm → reject)
  - test_db_update_websearch_failed (値変更なし)
- [ ] `python -c "from monitor.database import init_db; init_db(); init_db()"` で `physical_attrs_evidence` カラム冪等
- [ ] `monitor/database.py` の冪等性自動テスト (CLAUDE.md Q2 必須テスト) 実行 PASS

---

### Phase 2: backfill task (1 回限り 441 件)

**ファイル新規作成**:
- `scripts/backfill_physical_websearch.py` (one-shot script)
- `tests/test_backfill_physical_websearch.py` (mock 2-3 件)

**実装内容**:
```python
# scripts/backfill_physical_websearch.py
"""
1 回限り backfill: 出品中 441 件の重量・寸法を websearch で全件上書き。

実行前に user 承認確認:
  - 対象件数表示 (active && weight_source NOT IN ('manual', 'manual_override'))
  - 概算 cost 表示 (件数 × $0.027)
  - 「実行しますか? (y/N)」 prompt

実行中:
  - rank 昇順で順次 lookup (10 件ごとに進捗 print + Discord)
  - sleep_between=1.0s (rate limit 緩和)
  - 各件: lookup_with_retry → DB UPDATE
  - 成功 / 失敗 / cost 累計を集計

完了時:
  - サマリー print + Discord 通知
  - weight_source 内訳の before/after 比較を出力
"""

if __name__ == "__main__":
    targets = _fetch_backfill_targets()
    print(f"対象: {len(targets)} 件、概算 cost: ${len(targets) * 0.027:.2f}")
    if input("実行? (y/N): ").lower() != 'y':
        print("中止")
        sys.exit(0)
    # ... 実行 ...
```

**Q4 既存値上書き対応**:
- `WHERE is_ended=0 AND weight_source NOT IN ('manual', 'manual_override')` で 441 件抽出
- claude / default_500g / NULL すべてが対象
- `manual` / `manual_override` は user 入力を保護

**所要**: 3-4h (実装 + mock test + 実 API 5 件試走 + 本番実行)

**DoD**:
- [ ] `scripts/backfill_physical_websearch.py` 新規 (~120 行)
- [ ] `tests/test_backfill_physical_websearch.py` 2-3 件 PASS (target fetch + 実行 1 件 mock)
- [ ] **実 API 5 件試走** (active rank=A 上位 5 件、user 立会い): 期待 confidence=high が 4/5 以上
- [ ] **本番 backfill 実行** (user 承認後):
  - 完了後 DB 内訳が `websearch` ≥ 60% / `websearch_failed` ≤ 40% / `manual*` 不変
  - `length_cm > 0` の件数が 0 → 200 件以上に増加 (寸法 0% 解消)
- [ ] backfill 後 24h 以内 retrospective code-reviewer (Q4 + Q2 本番 DB 直接書込ルール)
- [ ] `task_execution_log` に backfill レコード残存

---

### Phase 3: UI 統合 (個別出品 / 利益計算 / DASHBOARD)

**修正ファイル**:
- `app.py` 利益計算タブ (1434-1492行) に「自動取得」ボタン追加
- `monitor/individual_listing.py` (W9) ウィザード Step 2 に「自動取得」ボタン追加 (詳細位置は実装時 grep)
- `app.py` DASHBOARD タブ (tab_dashboard) に「物理属性未取得」サブセクション追加

**実装内容**:

#### 3-A. 利益計算タブ 「自動取得」ボタン

```python
# app.py 1450 行付近 (発送情報 col2 内)
with col2:
    st.subheader("発送情報")
    weight_g = st.number_input("重量（g）", ...)
    # ... サイズ inputs ...
    
    # 新規: 自動取得ボタン (任意のタイトル + URL 入力 form と組み合わせ)
    with st.expander("自動取得 (Web 検索)", expanded=False):
        wsearch_title = st.text_input("商品名", key="calc_ws_title")
        wsearch_url = st.text_input("URL (任意)", key="calc_ws_url")
        if st.button("自動取得", key="calc_ws_btn"):
            from monitor.physical_attrs_websearch import lookup_with_retry
            with st.spinner("Web 検索中... (5-15 秒)"):
                r = lookup_with_retry(wsearch_title, ebay_item_id="adhoc", url=wsearch_url or None)
            if r.get("source") == "websearch" and r.get("weight_g"):
                st.success(f"取得成功: {r['weight_g']}g, {r['length_cm']}×{r['width_cm']}×{r['height_cm']}cm (信頼度: {r['confidence']})")
                # session_state に保存して rerun → number_input 初期値に反映
                st.session_state["calc_ws_result"] = r
                st.rerun()
            else:
                st.warning(f"取得失敗: {r.get('error_code', 'spec 不在')}。手動入力してください。")
```

利益計算タブは ad-hoc 計算用 (DB 書込なし)、session_state 経由で number_input prefill する。

#### 3-B. 個別出品タブ Step 2 「自動取得」ボタン

```python
# monitor/individual_listing.py のウィザード Step 2 内
if st.button("📐 自動取得 (Web 検索)", key="il_phys_websearch"):
    title = st.session_state.get("il_title", "")
    url = st.session_state.get("il_source_url", "")
    if not title:
        st.warning("先にタイトルを入力してください")
    else:
        with st.spinner("Web 検索中..."):
            r = lookup_with_retry(title=title, ebay_item_id="il_wizard", url=url)
        if r.get("source") == "websearch":
            st.session_state["il_weight_g"] = r["weight_g"]
            st.session_state["il_length_cm"] = r["length_cm"]
            # ... etc ...
            st.success(f"取得: {r['weight_g']}g (信頼度 {r['confidence']})")
            st.rerun()
```

出品確定時に DB 書込で `weight_source='manual_override'` を付与 (user 確認済 = manual 扱い)。

#### 3-C. DASHBOARD「物理属性未取得」セクション

```python
# app.py tab_dashboard 内
st.subheader("物理属性未取得")
unfetched = _fetch_unfetched_physical()  # weight_source IN ('websearch_failed', NULL) AND is_ended=0
if not unfetched:
    st.success("未取得なし")
else:
    st.caption(f"{len(unfetched)} 件 未取得 — 下表で手動入力するか、個別 Web 再検索を実行")
    edited = st.data_editor(
        pd.DataFrame(unfetched),
        column_config={
            "ebay_item_id": st.column_config.TextColumn(disabled=True),
            "title": st.column_config.TextColumn(disabled=True),
            "weight_g": st.column_config.NumberColumn(min_value=0, max_value=50000, step=10),
            "length_cm": st.column_config.NumberColumn(min_value=0.0, max_value=200.0, step=0.5),
            # ... etc ...
        },
        key="phys_unfetched_editor",
    )
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("変更を保存 (manual)"):
            _save_manual_physical_bulk(edited)
            st.success("保存完了")
            st.rerun()
    with col_b:
        retry_ids = st.multiselect("再検索対象", [r["ebay_item_id"] for r in unfetched])
        if st.button(f"選択 {len(retry_ids)} 件を Web 再検索"):
            for iid in retry_ids:
                # 個別 retry
                ...
```

**所要**: 5-6h (3 箇所 UI + Streamlit hot reload 確認 + Playwright E2E)

**DoD** (Q1 11 ステップ準拠):
- [ ] Phase 0: 修正前 Streamlit 起動 → 当該タブ表示確認
- [ ] Phase 1: 実装 + pytest PASS (UI ロジック単体)
- [ ] Phase 2 Streamlit 再起動 (hot reload 信用しない、CLAUDE.md 既存方針)
- [ ] Phase 3-A 利益計算タブ「自動取得」: 実 API 1 回 → form prefill 確認 (Playwright)
- [ ] Phase 3-B 個別出品タブ「自動取得」: 実 API 1 回 → form prefill + 出品時 `weight_source='manual_override'` で DB 記録
- [ ] Phase 3-C DASHBOARD「未取得」: bulk edit 1 行修正 → 保存 → DB UPDATE で `weight_source='manual'`
- [ ] DB 直接 SELECT で 3 件の遷移を verify
- [ ] eBay GetItem 1 往復 E2E (出品結果 listing の Item 物理属性が反映されるか) — Phase 3-B 完了 gate
- [ ] code-reviewer HIGH=0

---

### Phase 4: 週次 batch + scheduler 統合

**新規 task**:
- `tasks/task_enrich_physical_websearch.py` (週次実行 entry point)

**修正ファイル**:
- `daily_scheduler.py` (新 task 登録 + 既存 2 task の kill switch)
- `monitor/task_execution_log.py` `TASK_SCHEDULE` に登録
- `config/schedule_config.json` 新 task 設定追加 + 既存 2 task の `enabled=false`

**実装内容**:
```python
# tasks/task_enrich_physical_websearch.py
def run_enrich_physical_websearch(config: dict) -> dict:
    """週 1 (月曜 02:30) 実行。新規出品 + websearch_failed retry 対象。"""
    task_cfg = (config or {}).get("tasks_enabled", {}).get("enrich_physical_websearch") or {}
    max_items = int(task_cfg.get("max_items_per_run", 20))
    
    # 対象: weight_source IS NULL OR weight_source IN ('websearch_failed', 'default_500g', 'claude')
    # ※ 'manual' / 'manual_override' / 'websearch' は除外 (新規 + retry のみ)
    targets = _fetch_weekly_targets(max_items)
    
    if not targets:
        return {"success": True, "processed": 0, "message": "対象なし"}
    
    updated = 0
    failed = 0
    for t in targets:
        r = lookup_with_retry(t["title"], t["ebay_item_id"], None)
        if r["source"] == "websearch":
            update_ebay_listing_physical_websearch(...)
            updated += 1
        else:
            update_ebay_listing_physical_websearch(...)  # source='websearch_failed' のみ
            failed += 1
        time.sleep(1.0)
    
    return {
        "success": failed < len(targets),
        "processed": len(targets), "updated": updated, "failed": failed,
        "message": f"{updated}件取得 / {failed}件失敗 / 対象{len(targets)}件",
    }
```

**`TASK_SCHEDULE` 追加**:
```python
{"key": "enrich_physical_websearch", "display": "物理属性Web取得 (週次)",
 "hours": [2], "weekdays": [0], "owner": "main"},  # 月曜のみ
```

**既存 task 退役**:
- `enrich_listings_physical`: `enabled=false` (eBay GetItem populate 0 件 → 役目を websearch に譲る)
- `estimate_weights_claude`: `enabled=false` (タイトル推定は websearch で完全置換)

**所要**: 2-3h

**DoD**:
- [ ] `tasks/task_enrich_physical_websearch.py` 新規 (~80 行)
- [ ] `daily_scheduler.py` で `should_task_run('enrich_physical_websearch', config)` で実行
- [ ] `TASK_SCHEDULE` 登録、weekdays=[0] (月曜)
- [ ] `task_execution_log` に `enrich_physical_websearch` started/completed 記録 (silent skip 防止)
- [ ] `tasks_enabled.enrich_listings_physical.enabled=false` / `tasks_enabled.estimate_weights_claude.enabled=false`
- [ ] 月曜 02:30 batch 実行で `task_execution_log` に records (実走 verify は次月曜まで)
- [ ] **代替 verify**: 手動で `python tasks/task_enrich_physical_websearch.py` を実行 → 1-2 件処理されることを確認

---

### Phase 5: pytest + E2E DoD verify (Q1 11 ステップ準拠)

**所要**: 2h

**DoD** (全体):
- [ ] **DB クエリ verify**:
  ```sql
  SELECT weight_source, COUNT(*) FROM ebay_listings WHERE is_ended=0 GROUP BY weight_source;
  -- 期待: websearch ~250+, websearch_failed ~50-150, manual* ~少数, NULL=0
  
  SELECT COUNT(*) FROM ebay_listings WHERE is_ended=0 AND length_cm > 0;
  -- 期待: 200 件以上 (Phase 2 backfill 前 = 0 件)
  ```
- [ ] **pytest 件数**: Phase 1 (8) + Phase 2 (3) + 既存 task テスト = 11 件 全 PASS
- [ ] **UI 動作確認** (Streamlit + Playwright):
  - 利益計算タブ「自動取得」→ 5-15s spinner → form 反映
  - 個別出品タブ「自動取得」→ 出品 → eBay GetItem で物理値反映
  - DASHBOARD「未取得」→ inline edit → 保存
- [ ] **eBay GetItem 1 往復 E2E**:
  - 個別出品タブから 1 件試出品 (Sandbox or rank=E 低価格 listing)
  - eBay GetItem API で `ShippingPackageDetails` の DimensionLength / Width / Height が我々が送った値と一致
- [ ] **code-reviewer agent HIGH=0** (Phase 1-4 すべての変更ファイルを context 投入)
- [ ] **scheduler.log 確認**:
  - `grep enrich_physical_websearch scheduler.log | tail -20` で正常実行ログ
  - silent skip / fake success が出ていない (Q0)
- [ ] **24h retrospective**: 本番 DB 直接書込 (backfill) 後 24h 以内に code-reviewer 再投入 (Q2)

---

### Phase 6: Cleanup (オプション、1 ヶ月運用後)

**条件**: Phase 5 完了から 1 ヶ月運用、`websearch_failed` 比率 < 30% / cost 月次概算 < $5 を維持

**実施内容**:
- `tasks/task_estimate_weights_claude.py` 物理削除 (退役確定)
- `tasks/task_enrich_listings_physical.py` 物理削除
- `monitor/database.py` `update_ebay_listing_weight_estimate` 関数削除
- `daily_scheduler.py` の対応 step 削除
- `TASK_SCHEDULE` から旧 2 entry 削除

**所要**: 1h

**DoD**:
- [ ] 削除前後で pytest 全 PASS
- [ ] DB スキーマ変更なし (カラムは残置、データ保護)
- [ ] system_improvements.json で W105 status='completed', phase='cleanup_done'

---

## 6. DoD (全体完了判定基準)

| カテゴリ | 基準 |
|---------|------|
| **DB 内訳変化** | weight_source 内訳: websearch 250+ / failed 50-150 / manual* 少数, length_cm>0 200+ |
| **pytest** | 11 件全 PASS (mock + integration mark slow) |
| **UI** | 3 箇所 (利益計算 / 個別出品 / DASHBOARD) で「自動取得」+ 「手動入力」両 path 動作 |
| **eBay E2E** | 新規出品 1 件で物理属性が GetItem で確認可能 |
| **scheduler** | 月曜 02:30 batch で `enrich_physical_websearch` 正常実行、Q0 silent skip なし |
| **cost** | backfill ~$12 / 週次 ~$2/月 が概算と一致 (api_call_log 集計 verify) |
| **code-reviewer** | HIGH=0、Phase 1-5 すべての変更で実施 |
| **report** | Q5 完了報告 4 行テンプレ準拠 (使用モデル / 検証経路 / 実機ログ / 残リスク) |

---

## 7. HIGH 級リスク (5-10 件)

| # | リスク | 影響 | 対策 |
|---|--------|------|------|
| **H1** | WebSearch tool 仕様変更 (tool version `web_search_20250305` deprecate) | API 呼出失敗、全 listing で websearch_failed 連鎖 | Phase 1 で tool version を const 化、Anthropic 側変更告知監視 (Discord で月次手動 check)、`web_search_20260209` (新) への switch を v1.1 で予定 |
| **H2** | Web に正確 spec ないジェネリック商品 (Mouse pad / USB cable 等) の運用 | 失敗率 30-40% に膨張、user の手動入力負担増 | Q5 仕様で明示マーキング + DASHBOARD 一覧で対処、ジェネリック判定で websearch skip (Phase 1 Optional 実装) |
| **H3** | cost 暴走 (per-listing $0.027 を超える、複雑商品で max_uses=2 を使い切る) | 月予算 cap オーバー → API key suspend | `max_uses=2` ハード固定、`max_items_per_run=20` 週次 cap、`api_call_log` で cost daily watch、Phase 5 で 1 週間運用後 cost 実績 verify |
| **H4** | `default_500g` 189 件全件上書きの retrospective 評価 | websearch 結果が default_500g より精度低い場合、送料計算が悪化する逆効果リスク | Phase 2 backfill 後 7 日間で送料赤字 listing が増えていないか売上 KPI でモニタ (`feedback_w94_phase7_real_day1.md` 同等の検証) |
| **H5** | 公式サイトが日本語のみ (KEYENCE 等) で Haiku 4.5 が読み損なう | 工業計測器等で confidence='high' 誤認 → 異常値 prefill | system prompt で「日本語/英語両対応」明示、Phase 1 mock test に日本語サイト 1 件含める、出力 JSON で `evidence_url` を必ず保存し user が確認可 |
| **H6** | manual_override が websearch で誤上書き | user の手入力データ消失 | Phase 1 の DB UPDATE で `WHERE weight_source NOT IN ('manual', 'manual_override')` を **必ず付ける**、Phase 1 unit test で「manual の listing が上書きされない」を verify |
| **H7** | Anthropic API rate limit (1 minute spike) | backfill 中盤で連続失敗 | `sleep_between_items_sec=1.0` 既定 + retry 1 回 30s sleep、441 件 backfill = 約 8 分以上、user に「実行中は他 API 呼出控えて」と prompt |
| **H8** | Streamlit 再起動忘れによる UI hot reload 不整合 (CLAUDE.md 既存方針違反) | 新ボタン未表示 / 旧ロジック動作 | Phase 3 DoD に「Streamlit 完全再起動」明記、Q1 11 ステップ準拠 |
| **H9** | Q0 違反: websearch エラー時に値を強制 default_500g にする逃避修正の誘惑 | 「ちゃんと取れた風」の偽装、信頼性 0% に逆戻り | Phase 1 実装中に「絶対 default 入れない、failed 明示」を K3 self-check、code-reviewer で最重点監視 |
| **H10** | 既存 `task_estimate_weights_claude` / `task_enrich_listings_physical` の急な kill が下流 task に影響 | profit 計算で weight_g=NULL になり supplier_select が None 連鎖 | Phase 4 で 2 task disable と同時に Phase 2 backfill 完了が前提、未 backfill listing は default_500g が残る (悪化はしない) |

---

## 8. W 番号 + ROADMAP 登録

**W 番号**: **W105**
**`system_improvements.json` id**: **191** (現在最新 190 = id=190 の次)

**登録 entry (推奨)**:
```json
{
  "id": 191,
  "tag": "W105",
  "title": "商品物理属性 Web 自動取得 (Anthropic web_search tool)",
  "category": "automation",
  "priority": "high",
  "status": "design",
  "phase": "phase0_prd_review",
  "created_at": "2026-05-06",
  "owner": "assistant",
  "supersedes": ["task_estimate_weights_claude", "task_enrich_listings_physical"],
  "estimated_hours": 17,
  "estimated_cost_usd": 14,
  "doc_path": "docs/spec_W105_physical_attrs_websearch.md"
}
```

---

## 9. 総工数見積

| Phase | 内容 | hours |
|-------|------|------:|
| Phase 0 | PRD レビュー + W 番号登録 | 0.5 |
| Phase 1 | WebSearch helper module + mock test | 4-5 |
| Phase 2 | backfill script + 実 API 試走 + 本番 backfill | 3-4 |
| Phase 3 | UI 統合 (3 箇所) + Streamlit + Playwright E2E | 5-6 |
| Phase 4 | 週次 batch + scheduler 統合 + 既存 task disable | 2-3 |
| Phase 5 | E2E DoD + code-reviewer + 24h retrospective | 2 |
| Phase 6 | (オプション) cleanup 1 ヶ月後 | 1 |
| **合計** | (Phase 0-5) | **17-20** |

cost 見積:
- backfill 一括: **$12**
- 月次運用 (週次 batch + UI 自動取得 個別 ~10/日): **$5-8/月**
- API call log で actual 値を monthly review

---

## 10. Q5 完了報告 4 行テンプレ (Phase 5 完了時 想定)

```
- 使用モデル: Sonnet 4.6 (実装) + Haiku 4.5 (websearch 実行モデル)
- 検証経路: pytest 11 件 / 実 API integration 5 件 / Streamlit + Playwright E2E / eBay GetItem 1 往復 / DB SELECT 3 ク エリ
- 実機ログ: scheduler.log 月曜 02:30 batch ログ抜粋 + backfill task_execution_log 1 行
- 残リスク: ジェネリック商品 ~30% が websearch_failed 残存、cost 月次 $5-8 で予算内、H5 (日本語サイト) 1 ヶ月モニタ予定
```

---

**END of W105 PRD**
