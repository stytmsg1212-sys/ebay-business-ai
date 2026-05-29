# 2026-05-29 Opus 4.8 総チェック診断レポート + 再設計提案

## 概要

- **背景**: 既存ツールは Opus 4.7 以前のモデルが作成。バグ・非稼働・設計不徹底が混在している前提で、Opus 4.8 視点での総チェックと設計批評を user が依頼。Codex の health-scan (`migration/codex-health-scan-2026-05-28.md`) は 5/29 朝の scheduler 健全性に限定されており、横断的な総チェックではなかった。
- **検証方法**: Sonnet agent 3 並列で fact-gathering (tasks / monitor / UI tabs) → **Opus 4.8 (本体) が HIGH 所見を実コードで個別照合** (trust-but-verify)。モデル選定方針 Q6 に従い、事実収集は Sonnet、設計判断・照合は Opus。
- **スコープ**: (1) 診断レポート = 優先度付き所見一覧 (user が修正対象を選ぶ用)。(2) 再設計提案 = MonoDeck effort パラメータ導入 + Sonnet→Opus 置き換え試算。
- **重要前提 (今回対象外)**: supplier 評価 (`claude_evaluator.py` candidate_evaluate 系) は user 判断で **Sonnet 4.6 据え置き**。

---

## Part 1: 総チェック診断レポート

### 1-A. 確定 HIGH 所見 (Opus 実コード照合済)

| ID | 箇所 | 問題 | 規約 | 判定 |
|---|---|---|---|---|
| **H1** | `monitor/database.py:1080-1082` | `init_db` 内に条件付き `DROP TABLE` (customs_send_audit / customs_kb_pending / customs_requests)。`_need_recreate` は schema 文字列に `"deadline LIKE"` が含まれるかの脆い heuristic で判定 | Q2 (init_db に DROP 禁止、one-shot 化) | **要修正**。通常起動では DROP しないが、将来 schema 文言が変われば判定が崩れ 2026-04-25 の 95 件消失級事故が再発しうる |
| **H2** | `monitor/database.py:429` | `FOREIGN KEY (sku) REFERENCES monitored_items(sku)` — ebay_listings が SKU で FK。listing 識別は `ebay_item_id` のはず | sku-rules (JOIN/キー化禁止) | **要修正**。SQLite は `PRAGMA foreign_keys` 既定 OFF で現状実害は出ていない可能性大だが、schema が規約違反 + ON 化で有在庫の同 SKU 共有が破綻 |
| **H3** | `monitor/database.py:635` | supplier_candidates に `UNIQUE(sku, candidate_url)` | sku-rules (UNIQUE(sku) 禁止) | **本番適用完了** (W185, 2026-05-29)。`UNIQUE(ebay_item_id, candidate_url)` + `ebay_item_id NOT NULL` 化 (v56 gate = warning のみ、RECREATE せず Q2 準拠)、`add_supplier_candidate` 必須化 (ValueError)、`get_supplier_candidates(ebay_item_id=)` 追加。本番張替は `scripts/migrate_supplier_candidates_v56.py` one-shot で **user 承認後 2026-05-29 ~14:00 に --apply 実行**: snapshot `data/backups/monitor_pre_w185_20260529_1336.db` → 773→749 (24 重複 dedup、applied-conflict 0) → `PRAGMA user_version=56` → backup table `supplier_candidates_old_w185` (773 行保持)。独立 SELECT 検証 = UNIQUE autoindex `(ebay_item_id, candidate_url)` / NOT NULL=1 / NULL・空 0 / 重複 group 0。pytest 11 新規 PASS、事前 code-reviewer HIGH=0、本番後 24h retrospective code-reviewer HIGH=0。backup table cleanup は W186 (id 268) で新 schema 安定確認後 |
| **H4** | `app.py:1993-1994, 4849-4850` ほか複数 | `except Exception as _e: pass` (コメントに「silent fail」明記すらあり) | Q0 (silent skip 禁止) | **要修正**。`quality-gate.sh` は `except: pass` / `except Exception: pass` を BLOCK するが `as _e` 付きは擦り抜ける。hook の正規表現の穴 |
| **H5** | `daily_scheduler.py:449` (company_secretary), `:1153` (video_learning_resume) | task_key 付きで実行されるが `task_execution_log.TASK_SCHEDULE` (L26-63) に **未登録** → `find_missed_tasks` が欠落を検知できない | Q0 (silent skip 検知盲点) | **要修正**。company_secretary は毎日実行のはずだが silent skip しても誰も気付かない。2026-04-25 daily_relist 5 日間 silent skip と同じ穴 |

### 1-B. 要確認 HIGH 所見 (Agent 報告のみ・Opus 未照合)

> 以下は Sonnet agent の報告。本体での実コード照合は未実施。**「確定」と混同しない** (Q5 honesty)。修正着手前に Opus 照合必須。

| ID | 箇所 | Agent 報告内容 |
|---|---|---|
| H6 | `monitor/ebay_client.py:791` | revise_item_pictures の `ET.fromstring` が unguarded → 不正 XML レスポンスで crash |
| H7 | `monitor/ebay_sync.py:94-104` | enrichment 失敗時に stale metrics で処理継続 (失敗が握り潰される疑い) |

### 1-C. 棄却した所見 (false positive)

| Agent 報告 | 棄却理由 |
|---|---|
| `find_missed_tasks` が JST-naive 比較で誤判定 (Agent1 HIGH-1) | **W170 (2026-05-25) で修正済**。`task_execution_log.py:286-291` のコメントに経緯あり。`started_at` は `datetime.now()` bind = JST naive なので `DATE(started_at)=jst_today` が正しい。Sonnet agent が W170 の修正コメントを読まず「UTC 比較すべき」と誤判定した典型例 = **Opus 照合の価値の実証** |

### 1-D. MEDIUM / LOW (要対応だが緊急度低)

- **M1**: `data_sync` / `price_optimization` が `TASK_SCHEDULE` で `hours: None` = 全 batch slot (02/11/15/18/22 = 5x/日) 期待。実処理が LLM を呼ばなければコスト無害だが、毎 slot 実行が意図通りか要確認 (Agent1 は HIGH 報告 → Opus は実コスト次第で MEDIUM 判定)。
- **M2**: 真の dead code 3 件 — `tasks/task_process_search_results.py` / `task_product_search_executor.py` / `task_research.py.deleted_W21`。削除候補 (W21 で廃止済の残骸)。
- **M3**: SKU 文字列が UI で user に露出 (`tab_market_strategy.py:295,355,362,451,639`)。`tools/ebay-manager/CLAUDE.md`「商品の呼称」違反 (title で呼ぶべき)。
- **M4**: `datetime.now()` bind と `CURRENT_TIMESTAMP` の TZ 混在 (sqlite-timezone で既知、新規 INSERT は CURRENT_TIMESTAMP 統一推奨)。
- **L1**: `app.py` が 7100+ 行 monolith (後述「設計批評」)。

### 1-E. 設計レベル批評 (そもそもの設計)

user が「設計がいけてない指摘も受け入れる」としたため、個別バグより上位の構造的問題を 4 点挙げる:

1. **SKU 結合の schema 残存 (H2/H3 の根)**: migration v26 (2026-04-29) で listing を `ebay_item_id` 単位化したが、schema 定義 (FK / UNIQUE) と一部クエリに SKU 結合が残存。「コードは v26 で直したが schema 層が追従していない」= 修正の不徹底。W139-fix (2026-05-19) も同根の再発だった。**schema 全体の SKU 結合を一掃する棚卸しが必要**。
2. **quality-gate hook の機械強制が緩い (H4 の根)**: `except Exception as _e: pass` を BLOCK できない正規表現。「機械強制で防ぐ」という設計思想に対し、抜け穴が放置され実際に擦り抜けコードが増殖。hook 自体が要修正対象。
3. **TASK_SCHEDULE が手動二重管理 (H5 の根)**: daily_scheduler の実 task と `TASK_SCHEDULE` レジストリが別管理 → 登録漏れが構造的に発生 (company_secretary 等)。daily_scheduler 側の task_key を single source of truth にし、registry を自動導出する設計が望ましい。
4. **app.py monolith (L1)**: 21 タブが単一 7100+ 行ファイル。tab_*.py への分離は進むが app.py 本体が肥大。保守性・Streamlit hot-reload 安定性・LLM 編集効率すべてに悪影響。

---

## Part 2: 再設計提案

### 2-1. 概要

実 DB (api_call_log 直近 30 日) の実測に基づき、(A) MonoDeck の LLM 呼び出しへの `effort` パラメータ導入、(B) Sonnet→Opus 置き換えの実スコープと試算を提示する。

**重要な実測事実** (付録参照):
- 記録上の LLM 月額は ~$140 だが、Opus 4.7 行が旧 $15/$75 誤単価で記録されていたため、**補正後の実支出は ~$82/月**。
- Sonnet 支出 $51/月の **95% は supplier 評価** (`candidate_evaluate` 系)。これは user 判断で Sonnet 据え置き。
- → **Sonnet→Opus の実置き換えスコープは `listing_generate` ($2.41/月) のみ**。

### 2-2. 設計・方針

#### (A) MonoDeck effort パラメータ導入

Opus 4.8 / Sonnet 4.6 は `output_config: {effort: "low|medium|high|xhigh|max"}` をサポート (high=既定)。effort は全 token 量に影響: low → tool call / 出力削減 (高速・低コスト)、xhigh/max → コーディング・agentic 深掘り。

**方針**: 用途特性に応じて effort を明示設定し、(1) bulk・短文タスクはコスト/レイテンシ削減、(2) 業務判断・出品文は品質確保。

#### (B) Sonnet→Opus 置き換え

**方針**: supplier 評価据え置きにより、置き換え候補は `listing_generate` (出品文生成) のみ。コスト差分は月 +$1.6〜$3.0 と微小なため、**listing 品質 (title / item specifics / condition description = 売上・defect 率に直結) の向上が見込めるなら採用に値する**。ただし品質向上の実証は A/B が必要 (本提案のスコープ外、別途)。

### 2-3. 詳細

#### effort マッピング案 (LLM 呼び出し箇所別)

| 呼び出し箇所 | 現モデル | operation | 推奨 effort | 根拠 |
|---|---|---|---|---|
| `tab_research_brain.py` | Opus | (業務判断) | **high〜xhigh** | 深い業務判断、ゴール複雑 |
| `tab_individual_listing.py` | Opus/Sonnet/Haiku/Gemini | listing_generate 等 | **medium〜high** | 出品文品質確保 |
| `claude_evaluator.py` | Sonnet (据置) | candidate_evaluate 系 | **low〜medium** | 高頻度 (9198 calls/30d)、構造化判定。low でコスト/レイテンシ削減余地大 |
| email 分類 | Haiku | (分類) | **low** | bulk・短文、tool call 不要 |
| `tab_market_strategy.py` | Haiku | (価格判定) | **low** | bulk |
| code-architect / code-reviewer agent | Opus | (設計・レビュー) | **xhigh** | コーディング agentic |

> ⚠️ effort:low は supplier 評価の精度に影響しうる (match_score の質)。導入時は match_score 分布を before/after で比較し回帰がないか検証 (Q1 DoD)。

#### コスト試算 (Sonnet→Opus, listing_generate のみ)

- 現状 Sonnet: **$2.41 / 30 日** (520 calls)
- Opus per-token = Sonnet の **1.67x** ($5/$25 ÷ $3/$15)
- Opus は新トークナイザで同一テキスト最大 **+35% token** → 実効 **1.67〜2.25x**
- 想定 Opus コスト: **$4.0〜$5.4 / 30 日**
- **月差分: +$1.6〜$3.0** (無視できる微増)

#### 実装ステップ (採用時)

1. **H1〜H5 の修正を優先** (再設計より先。特に H1=Q2 / H5=Q0 は事故再発リスク)。
2. effort: 各 LLM 呼び出しの API request に `output_config={"effort": ...}` を追加。まず claude_evaluator の low 化を A/B (match_score 回帰チェック)。
3. Sonnet→Opus: listing_generate を Opus 4.8 化 + 出品文品質 A/B (title 長 / item specifics 充足 / condition desc 適切性)。
4. 全変更後 code-reviewer HIGH=0 + Q1 DoD (Streamlit + Playwright + DB + scheduler.log)。

---

## 付録: 実コスト実測 (api_call_log, 直近 30 日)

集計: `scripts/_tmp_model_usage_2026_05_29.py` (実行後削除)。`called_at` は UTC、`datetime('now','-30 days')` 相対範囲。

### モデル別

| model | calls | 記録 cost | 補正後 cost | 備考 |
|---|---|---|---|---|
| claude-opus-4-7 | 3535 | $86.75 | **~$28.92** | 旧 $15/$75 誤単価で記録 = 実 $5/$25 で 1/3。**遡及補正は Q2 判断保留中** |
| claude-sonnet-4-6 | 9718 | $51.35 | $51.35 | 正単価、補正不要 |
| claude-haiku-4-5 | 1355 | $1.77 | $1.77 | 正単価 |
| gemini-2.5-flash | 4 | $0.37 | $0.37 | |
| trading_api (eBay) | 713 | $0.00 | $0.00 | token 課金なし |
| **合計** | | **$140.24** | **~$82.41** | |

### Sonnet 4.6 operation 別

| operation | calls | cost | Opus 化スコープ |
|---|---|---|---|
| candidate_evaluate_batch | 7130 | $31.99 | **除外** (user 判断 Sonnet 据置) |
| candidate_evaluate | 2068 | $16.95 | **除外** (同上) |
| listing_generate | 520 | $2.41 | ★**唯一の置き換え候補** |

> 全期間: 19,219 行 / 2026-04-19 〜 2026-05-28 (UTC)。
