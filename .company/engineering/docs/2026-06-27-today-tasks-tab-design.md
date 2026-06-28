# 本日の作業 タブ — 詳細実装設計 (blueprint)

- **W番号**: **W292** (`data/system_improvements.json` 次の `id: 357` / `tag: "W292"`。W291 が現行最大 W-token、W287/W289/W292 未使用)
- **作成日**: 2026-06-27
- **作成者**: general-purpose (Opus 4.8) — 設計のみ。production code 未変更。実装は後続 agent。
- **設計モデル**: Opus 4.8
- **ステータス**: 設計完了 (実装待ち)

---

## 0. 前提と承認済み方針 (user 確定 2026-06-27)

本タブは MonoDeck「★ 毎日」グループに、**毎朝の初期登録作業を 10 件ずつ消化する作業ボード**として新設する。承認済み 4 方針:

| # | 方針 | 設計への落とし込み |
|---|------|-------------------|
| **A. 完了定義** | 手動チェック維持 + 各カードに「欠落項目バッジ」表示 (ガイドのみ・強制なし) | 完了 = `set_initial_registered(eid, True)` を呼ぶ既存トグルそのまま。バッジは **表示のみ**、チェックを block しない (§5) |
| **B. 優先順位** | `total_sold_count DESC` 主キー → 同点は `competitor_count==0` → 物理属性欠落 | 選定 SQL の `ORDER BY` (§3)。完全タイブレーク式を確定 |
| **C. 10件の固定** | スナップショット固定 (当日 10 件はリロード不変、別テーブル記録) | `daily_task_set` テーブルに当日 JST の 10 行を凍結。再訪時は再利用 (§2,§3) |
| **D. 対象範囲** | active 全 listing (有/無在庫問わず。商品管理「📝初期未完了のみ」と同述語) | 述語 = `not initial_registered` かつ active。`_fetch_all_products` の active 条件を踏襲 (§3) |

### 🚨 .md と実装の差異 (md-files-can-be-wrong R-1。設計は実コードを真とした)

委譲プロンプトの記述と実コードが食い違った箇所。**すべて実コード側を採用**:

| プロンプト記述 | 実コード (真) | 設計での扱い |
|---|---|---|
| 母体 `2026-06-27-today-tasks-tab-proposal.md` を読め | **root `.company/engineering/docs/` に実在** (cwd が `tools/ebay-manager/` だったため `.company/engineering/docs/` を探したが未発見。本修正でルートへ統合) | 提案書 §3「1日の2部構成」= day-part 構成 (🌅リサーチ導線 / 🌙初期登録) を正として採用し、設計書の誤解釈を修正済み |
| `monitor/ui_themes.py` の `apply_neumorph_cream_theme` | テーマ関数は **`ui_themes.py` (リポジトリ root)**、`apply_neumorph_cream_theme()` (L475) が実体。`apply_custom_styling()` (L995) は別名 | app.py が `apply_custom_styling()` で全体適用済 → 本タブは `--nm-*`/`--f-num` トークンに乗るだけ |
| DBヘルパーを `database.py` に置く想定 | duel 先例 (W286) は **専用モジュール `monitor/research_duel_db.py`** に分離、`database.py` は `init_db`/migration のみ | 本設計も **専用モジュール `monitor/daily_task_db.py`** に helper 分離 (K2 surgical, 先例踏襲)。migration のみ `database.py:init_db` へ |
| migration 番号は「現行最新を確認」 | 現行最新 = **v82** (`PRAGMA user_version = 82`、L3884) | 新規 = **v83** (§2) |
| tab render は `config` 引数 | 既存タブ規約 = **`(s: dict)`** (`s = st.session_state.settings`, app.py L228)。商品管理タブのみ例外的に `config` を自前構築 | 本タブ render は `(s: dict)` 規約に合わせる (§6,§7) |

---

## 1. アーキテクチャ概要

```
┌─ app.py ────────────────────────────────────────────────┐
│  _W134_GROUPS["★ 毎日"] に "本日の作業" を追加 (nav)        │
│  if _w134_sel == "本日の作業":  render_today_tasks_tab(s)  │
└────────────────────────┬─────────────────────────────────┘
                         │
        ┌────────────────▼─────────────────┐
        │  tabs/tab_today_tasks.py (新規・UI 層)  │
        │   render_today_tasks_tab(s)             │
        │    - topbar + 🔥 streak chip            │
        │    - 🌅🌙 2部コンディションバー (day-part) │
        │    - 進捗リング X/10                     │
        │    - 10件チェックリスト                  │
        │    - 完了セレブレーション 🎉             │
        │    - チェック = set_initial_registered  │
        │    - 「登録画面へ」= pm_focus_eid 書込   │
        └────────────────┬─────────────────┘
                         │ lazy import
        ┌────────────────▼─────────────────┐
        │  monitor/daily_task_db.py (新規・データ層)  │
        │   get_or_create_today_task_set()             │
        │   get_today_tasks_with_status()              │
        │   get_streak() / bump_streak_on_completion() │
        │   from .database import get_conn             │
        └────────────────┬─────────────────┘
                         │
        ┌────────────────▼─────────────────┐
        │  monitor/database.py : init_db()        │
        │   v83 migration:                        │
        │     daily_task_set / daily_task_streak  │
        └─────────────────────────────────────────┘

  jump-to-登録:
   本タブ「登録画面へ」→ st.session_state["pm_focus_eid"]=eid
                       + st.session_state["_w134_sel"]="商品管理" + st.rerun()
                       ↓
   tab_product_management.render_product_management()
     先頭で pm_focus_eid を 1 回だけ消費 → pm_search に eid を seed
     → 表が 1 行に絞れ、その行クリックで _render_product_editor 展開
```

**K1 最小設計の遵守**: 新規テーブルは **2 つだけ** (`daily_task_set` / `daily_task_streak`)。完了フラグは既存 `ebay_listings.initial_registered` を流用 (新設しない)。新規 DB 操作モジュール 1 つ、新規 UI タブ 1 つ、既存 2 ファイル (app.py / tab_product_management.py) に最小フック。

---

## 2. DB 設計 (migration v83)

### 2.1 テーブル定義 (冪等)

`monitor/database.py` の `init_db()` 内、**v82 ブロック直後** (現状 L3889 の後・`init_db` 末尾 L3891 の前) に v83 ブロックを追加する。v80/v81/v82 と同一 idiom (`schema_ver == 82` ガード → `CREATE TABLE IF NOT EXISTS` を try/except `sqlite3.OperationalError` → sqlite_master で存在確認後のみ bump)。

```python
        # ---- v83 (本日の作業タブ W292 / 2026-06-27): daily_task 2 テーブル ----
        # 毎朝の初期登録作業を「売れ筋上位の未登録 10 件」に固定して消化する作業ボード。
        #   - daily_task_set:    JST 当日 × rank(1-10) で凍結した 10 件のスナップショット。
        #                        当日リロードしても不変 (承認方針 C)。listing 識別は
        #                        ebay_item_id (sku-rules.md: SKU を一意キーにしない)。
        #                        完了状態は ebay_listings.initial_registered を都度参照する
        #                        ので本表には複製しない (真値の二重化回避)。
        #   - daily_task_streak: 連続達成日数 (🔥 streak chip 用)。metric 別 1 行。
        # 冪等: CREATE TABLE IF NOT EXISTS + 2 テーブル存在確認後のみ bump (v82 流儀)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 82:
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_task_set (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        jst_date     TEXT NOT NULL,          -- 'YYYY-MM-DD' (JST)
                        rank         INTEGER NOT NULL,        -- 1-10 (選定順 = 表示順)
                        ebay_item_id TEXT NOT NULL,           -- listing 識別 (sku-rules)
                        title_snap   TEXT,                    -- 選定時タイトル (表示安定用スナップショット)
                        sold_snap    INTEGER,                 -- 選定時 total_sold_count (監査用)
                        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(jst_date, rank),
                        UNIQUE(jst_date, ebay_item_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_task_streak (
                        metric        TEXT PRIMARY KEY,        -- 'initial_register' (将来拡張余地)
                        current_streak INTEGER NOT NULL DEFAULT 0,
                        best_streak    INTEGER NOT NULL DEFAULT 0,
                        last_done_date TEXT,                   -- 最後に「10件全完了」を記録した JST 日付
                        updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_daily_task_set_date "
                    "ON daily_task_set(jst_date)"
                )
                logger.info("[init_db v83] daily_task_set/daily_task_streak created")
            except sqlite3.OperationalError as e:
                logger.warning(f"[init_db v83] daily_task tables create skipped: {e}")
            _v83_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('daily_task_set','daily_task_streak')"
            ).fetchone()[0]
            if _v83_ok == 2:
                conn.execute("PRAGMA user_version = 83")
                logger.info("[init_db v83] schema_ver bumped to 83")
            else:
                logger.warning(
                    f"[init_db v83] テーブル未作成 ({_v83_ok}/2)。次回 init_db で再試行。"
                )
```

### 2.2 設計判断メモ

- **完了状態を `daily_task_set` に複製しない**: 完了は `ebay_listings.initial_registered` が唯一の真値。`daily_task_set` は「当日どの 10 件を選んだか」の凍結のみを担う。商品管理タブで `initial_registered` を OFF に戻しても本タブが同じ真値を参照 = 不整合ゼロ。
- **`title_snap`/`sold_snap`**: 当日選定時点の表示安定 + 監査用スナップショット。表示の主は `daily_task_set` JOIN `ebay_listings` の **live** title (rename 追従)。`title_snap` は live が消えた時の fallback。
- **`UNIQUE(jst_date, ebay_item_id)`**: 同日に同一 listing が 2 rank を占めないことを DB で保証。
- **`daily_task_streak` は metric キー化**: 現状 `'initial_register'` 1 行のみ。将来別作業 streak を足す余地を残すが、行は作らない (K1: 必要時に INSERT)。
- **`CURRENT_TIMESTAMP` = UTC** (sqlite-timezone.md)。日付判定は `jst_date` TEXT 列で行い、`created_at` は監査用。`jst_date` は呼出側が `DATE('now','+9 hours')` で生成して bind (§3)。

### 2.3 冪等性

v82→v83 を 2 回連続実行してもデータ保持 (テスト §9.1)。`CREATE TABLE IF NOT EXISTS` のみ、DROP/DELETE/ALTER なし (Q2)。bump は 2 テーブル作成確認後のみ。

---

## 3. 選定 SQL (完全形)

### 3.1 選定ロジック (`get_or_create_today_task_set`)

当日 (JST) の `daily_task_set` 行が **無ければ生成・あれば再利用** (承認方針 C スナップショット固定)。

```python
def get_or_create_today_task_set(today_jst: str, limit: int = 10) -> list[dict]:
    """JST 当日の作業 10 件を取得 (無ければ選定して凍結、あれば再利用)。

    today_jst: 'YYYY-MM-DD' (JST)。呼出側で DATE('now','+9 hours') を渡す。
    返り値: rank 昇順の list[dict]。各 dict は daily_task_set + ebay_listings 結合の
            live 値 (title / sold / competitor_count / 物理属性 / initial_registered)。
    冪等: 同日に複数回呼んでも同じ 10 件 (UNIQUE(jst_date, rank) で二重 INSERT を回避)。
    """
```

**Step 1 — 当日セット存在チェック + 再利用** (あれば即 return、無ければ Step 2):

```sql
SELECT COUNT(*) FROM daily_task_set WHERE jst_date = ?;   -- (today_jst,)
```

**Step 2 — 未済プール抽出 + 売れ筋 DESC ソートで上位 N 選定** (`COUNT==0` の時のみ実行):

```sql
-- 未済 (initial_registered=0/NULL) かつ active な listing を承認順でソートし上位 limit 件。
-- active 条件は _fetch_all_products と同一 (is_ended NULL/0 + title 非空)。
-- 並び (承認方針 B):
--   1. total_sold_count DESC               (売れ筋 = 主キー)
--   2. competitor_count==0 を先 (ライバル未登録優先)
--   3. 物理属性/損益分岐の欠落数 DESC       (欠落が多い = 着手価値が高い)
--   4. ebay_item_id ASC                    (決定性タイブレーク)
SELECT
    el.ebay_item_id,
    el.title,
    COALESCE(el.total_sold_count, 0)                            AS sold,
    (SELECT COUNT(*) FROM competitor_products cp
       WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1) AS competitor_count
FROM ebay_listings el
WHERE (el.is_ended IS NULL OR el.is_ended = 0)
  AND el.title IS NOT NULL AND el.title != ''
  AND COALESCE(el.initial_registered, 0) = 0
ORDER BY
    COALESCE(el.total_sold_count, 0) DESC,
    CASE WHEN (SELECT COUNT(*) FROM competitor_products cp
                 WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1) = 0
         THEN 0 ELSE 1 END ASC,
    (
        (CASE WHEN el.purchase_yen   IS NULL OR el.purchase_yen   = 0 THEN 1 ELSE 0 END) +
        (CASE WHEN el.weight_g       IS NULL OR el.weight_g       = 0 THEN 1 ELSE 0 END) +
        (CASE WHEN el.length_cm IS NULL OR el.width_cm IS NULL OR el.height_cm IS NULL
              OR el.length_cm = 0 OR el.width_cm = 0 OR el.height_cm = 0 THEN 1 ELSE 0 END) +
        (CASE WHEN el.lp_breakeven_usd IS NULL OR el.lp_breakeven_usd = 0 THEN 1 ELSE 0 END)
    ) DESC,
    el.ebay_item_id ASC
LIMIT ?;     -- (limit,)
```

> ⚠️ tie-break #3 の欠落判定式は `_apply_filter_and_sort` の `only_missing` フィルタ (L439-445) と**同じ列・同じ「空/0」基準**で揃える。商品管理タブ「⚠️ 未 FIX (仕入¥/重/寸/BE)」と本タブの欠落バッジ (§5) で判定を一致させ、user の体感を統一する。

**Step 3 — 選定結果を `daily_task_set` に凍結** (`enumerate` で rank 1..N):

```sql
INSERT OR IGNORE INTO daily_task_set
    (jst_date, rank, ebay_item_id, title_snap, sold_snap)
VALUES (?, ?, ?, ?, ?);
-- (today_jst, rank, ebay_item_id, title, sold) を 1 件ずつ
```

> `INSERT OR IGNORE` + `UNIQUE(jst_date, rank)` で、並行 rerun による二重生成を吸収 (先勝ち。後続は IGNORE)。Q0: ただし「全件 IGNORE で 0 件凍結」が起きていないか、INSERT 後に Step 4 の SELECT 件数で検証し、0 件なら `logger.warning` (silent skip 防止)。

**Step 4 — 凍結済みセットを live 値で結合して返す** (Step 1 再利用パスと共通):

```sql
-- daily_task_set (当日凍結) × ebay_listings (live) を rank 昇順で結合。
-- live が delist 済 (is_ended=1) でも本タブには表示する (当日の作業対象として固定。
-- title が消えていれば title_snap で fallback)。完了状態は live の initial_registered。
SELECT
    dts.rank,
    dts.ebay_item_id,
    COALESCE(el.title, dts.title_snap)                         AS title,
    COALESCE(el.total_sold_count, dts.sold_snap, 0)            AS sold,
    el.sku,
    el.primary_market,
    el.purchase_yen, el.weight_g,
    el.length_cm, el.width_cm, el.height_cm,
    el.lp_breakeven_usd,
    COALESCE(el.initial_registered, 0)                         AS initial_registered,
    el.initial_registered_at,
    (SELECT COUNT(*) FROM competitor_products cp
       WHERE cp.our_item_id = dts.ebay_item_id AND cp.is_active = 1) AS competitor_count,
    (el.ebay_item_id IS NULL)                                  AS listing_gone
FROM daily_task_set dts
LEFT JOIN ebay_listings el ON el.ebay_item_id = dts.ebay_item_id
WHERE dts.jst_date = ?                                          -- (today_jst,)
ORDER BY dts.rank ASC;
```

### 3.2 JST 日付 (sqlite-timezone.md 準拠)

`today_jst` は **呼出側 (UI / DB helper) が `DATE('now','+9 hours')` で 1 回求めて引き回す** (lowest_price.py L587 / task_research_harvest.py L107 と同一手法)。Python の `date.today()` は OS ローカル (Windows=JST) で一致するが、**SQL 由来に統一** して TZ 曖昧性をゼロにする。helper の中で:

```python
row = conn.execute("SELECT DATE('now','+9 hours')").fetchone()
today_jst = row[0]   # 'YYYY-MM-DD'
```

### 3.3 確認した実カラム名 (選定 SQL の根拠)

`_fetch_all_products()` (tab_product_management.py L146-219) の SELECT で実在を確認済:

| 用途 | 実カラム | 出典 |
|------|---------|------|
| 売れ筋 (主キー) | `ebay_listings.total_sold_count` | L166, ソート L468 |
| ライバル登録数 | `competitor_products` の `COUNT(*) WHERE our_item_id=? AND is_active=1` → エイリアス `competitor_count` | L188-190 |
| listing 識別 | `ebay_listings.ebay_item_id` | L152 (sku-rules: SKU 不使用) |
| 仕入¥ 欠落 | `ebay_listings.purchase_yen` | L154, only_missing L442 |
| 重量 欠落 | `ebay_listings.weight_g` | L153, only_missing L442 |
| 寸法 欠落 | `ebay_listings.length_cm / width_cm / height_cm` | L153, only_missing L443 |
| 損益分岐 欠落 | `ebay_listings.lp_breakeven_usd` | L154, only_missing L444 |
| 完了フラグ | `ebay_listings.initial_registered` (+ `initial_registered_at`) | L200-201, 述語 L459 |
| active 条件 | `WHERE (is_ended IS NULL OR is_ended=0) AND title IS NOT NULL AND title != ''` | L214-215 |
| 表示補助 | `sku` / `primary_market` / `title` | L152, L155 |

---

## 4. データ層 ヘルパー関数シグネチャ (`monitor/daily_task_db.py`)

duel 先例 (`monitor/research_duel_db.py`) と同流儀: 冒頭 `from .database import get_conn`、関数粒度の薄い CRUD、副作用は明示。

```python
from .database import get_conn

def _today_jst(conn) -> str:
    """SQL 由来の JST 当日 'YYYY-MM-DD' (sqlite-timezone.md)。"""

def get_or_create_today_task_set(today_jst: str | None = None,
                                 limit: int = 10) -> list[dict]:
    """JST 当日の作業 10 件 (無ければ選定凍結・あれば再利用)。§3 のロジック。
    today_jst=None なら conn 内で _today_jst() を使う。
    返り値: rank 昇順 list[dict] (live 結合 + initial_registered + 欠落判定の素データ)。
    冪等: 同日複数回呼出で同一結果 (UNIQUE(jst_date,rank) で二重凍結回避)。
    Q0: 凍結 0 件 (全 IGNORE) は logger.warning で可視化。"""

def get_today_tasks_with_status(today_jst: str | None = None) -> dict:
    """UI 集計用の薄いラッパ。get_or_create_today_task_set を呼び、
    {"tasks": [...], "done": int, "total": int, "all_done": bool} を返す。
    done = initial_registered==1 の件数。total = len(tasks) (通常 10、
    未済プールが 10 未満なら少ない件数のまま = 残作業が少ない朝)。"""

def get_streak(metric: str = "initial_register") -> dict:
    """{"current_streak": int, "best_streak": int, "last_done_date": str|None}。
    行が無ければ全 0 / None を返す (INSERT しない = 読取専用)。"""

def bump_streak_on_completion(today_jst: str | None = None,
                              metric: str = "initial_register") -> dict:
    """当日 10 件が all_done になった瞬間に呼ぶ。冪等:
      - last_done_date == today_jst なら何もしない (同日二度押し吸収)。
      - last_done_date == 昨日(JST) なら current_streak += 1。
      - それ以外 (飛び) なら current_streak = 1 にリセット。
      - best_streak = max(best_streak, current_streak)。
      - last_done_date = today_jst, updated_at = CURRENT_TIMESTAMP。
    UPSERT (INSERT ... ON CONFLICT(metric) DO UPDATE)。返り値 = 更新後 streak dict。
    「昨日」は SQL DATE(?, '-1 day') で算出 (today_jst 基準)。"""
```

**冪等性方針 (まとめ)**:
- `get_or_create_*` = `INSERT OR IGNORE` で並行・再訪安全。
- `bump_streak_on_completion` = `last_done_date == today` ガードで同日多重呼出を吸収 (UI が all_done を毎 rerun 検知しても streak が暴走しない)。
- `get_streak` = 純読取 (副作用ゼロ、行不在で 0)。

---

## 5. 欠落バッジ ロジック (承認方針 A: 表示のみ・強制しない)

各 listing dict (§3.1 の結合行) から **5 種の欠落バッジ**を算出。チェック (`set_initial_registered`) は一切 block しない — バッジは「あと何が残っているか」のガイド表示。

```python
def _missing_badges(t: dict) -> list[str]:
    """残作業バッジのラベル list。空 list = 全項目埋まり (✅ 完備)。"""
    out = []
    if not (t.get("competitor_count") or 0):
        out.append("ライバル未登録")
    if not t.get("purchase_yen"):
        out.append("仕入¥未")
    if not t.get("weight_g"):
        out.append("重量未")
    if not (t.get("length_cm") and t.get("width_cm") and t.get("height_cm")):
        out.append("寸法未")
    if not t.get("lp_breakeven_usd"):
        out.append("損益分岐未")
    return out
```

- 判定基準は §3.1 tie-break #3 / 商品管理 `only_missing` (L439-445) と **同一**。`purchase_yen`/`weight_g`/`lp_breakeven_usd` は `not x` (None/0 を欠落扱い)、寸法は 3 軸 AND。
- 表示: 各カードに欠落バッジを赤系チップ (`--nm-warn`/`--nm-err`)、欠落ゼロなら `✅ 物理属性 完備` を緑チップ (`--nm-ok`)。
- **チェックボックスは欠落があっても押せる** (方針 A: 強制なし)。欠落ありで完了にした時は `st.caption("⚠️ 未入力項目が残っていますが完了にしました")` を出すだけ (誘導、block しない)。

---

## 6. タブ module 構造 (`tabs/tab_today_tasks.py`)

`render_today_tasks_tab(s: dict)` を関数分解 (duel タブ §_inject_css/_render_* 流儀)。session_state プレフィクス `_SS = "today_"` で衝突回避。

```python
_SS = "today_"

def _inject_css() -> None:
    """本タブ専用 最小 custom CSS (--nm-* / --f-num トークンに乗せる)。
    進捗リング (conic-gradient) / streak chip / カードの完了/未完スタイル /
    🌅🌙 コンディションバーのグラデーション のみ定義。"""

def _render_topbar(streak: dict, done: int, total: int) -> None:
    """上部: タイトル + 🔥 streak chip (current_streak 日連続 / best 併記) +
    本日 X/10 のサマリ。"""

def _render_progress_ring(done: int, total: int) -> None:
    """conic-gradient で X/total の進捗リング描画 (HTML/CSS、JS 不要)。
    中央に大きく done/total。全完了で金色リング。"""

def _render_condition_bar(tasks: list[dict]) -> None:
    """🌅 早朝の部 / 🌙 夜間の部 の 2 部コンディションバー (day-part 構成)。
    user の「早朝=リサーチ / 夜間=商品管理」という 1 日のリズムを表現。
    🌅 早朝の部: 既存リサーチタブへのジャンプボタン 2 個。
      - 「リサーチ対戦」: st.session_state["_w134_sel"] = "リサーチ対戦" + st.rerun()
      - 「今日の発掘」 : st.session_state["_w134_sel"] = "今日の発掘"   + st.rerun()
      (§8 と同じ routing contract: 素のページ名を代入して rerun。ページ名は
       _W134_GROUPS の実在文字列 "リサーチ対戦" / "今日の発掘" を使う)
    🌙 夜間の部: 初期登録 10 件ゴール X/10 の進捗表示 (本タブの主役)。
    10件リストは rank 連続のまま分割しない (時間帯/午前午後で割らない)。K1。"""

def _render_task_row(t: dict, idx: int) -> None:
    """1 件: チェックボックス (完了) + タイトル(クリックで商品ページの慣習) +
    sold バッジ + 欠落バッジ群 (§5) + 「📝 登録画面へ」ボタン (§8 jump フック)。
    チェック on/off で set_initial_registered(eid, val) → bump_db_version → rerun。
    完了 listing は打消線 + ✅ 帯。"""

def _render_celebration(streak: dict) -> None:
    """10件 all_done 時のセレブレーション。st.balloons() + 🎉 大見出し +
    streak 更新後の「N 日連続達成！」表示。"""

def render_today_tasks_tab(s: dict) -> None:
    """本タブ本体 (app.py dispatch から呼出)。
      1. データ層 lazy import (失敗時 st.error で可視化)。
      2. status = get_today_tasks_with_status()。
      3. _inject_css() / _render_topbar / _render_progress_ring / _render_condition_bar。
      4. tasks を _render_task_row でループ描画。
      5. all_done なら bump_streak_on_completion() → _render_celebration()。
         (bump は冪等なので毎 rerun 呼んでよい — 同日多重は helper 側で吸収)。
      6. tasks 空 (未済プール 0 = 全 listing 登録済) なら "🎉 未登録の商品はありません" 表示。
    """
```

### 6.1 チェック操作の DB 配線 (既存トグルと同一)

`_render_task_row` のチェック = 商品管理タブ L3900-3911 と**完全同一の経路**:

```python
_cur = bool(t.get("initial_registered"))
_new = st.checkbox("完了", value=_cur, key=f"{_SS}chk_{t['ebay_item_id']}")
if _new != _cur:
    from monitor.database import set_initial_registered
    set_initial_registered(t["ebay_item_id"], _new)   # ebay_item_id 識別 (sku-rules)
    from ui_cache import bump_db_version
    bump_db_version()                                  # 商品管理タブのキャッシュも無効化
    st.rerun()
```

> `bump_db_version()` を呼ぶことで、商品管理タブの `_cd_fetch_all_products(get_db_version())` キャッシュが次回 read で最新化 = 2 タブ間で完了状態が即同期。

### 6.2 「🌅🌙 2部コンディションバー」の解釈確定 (2026-06-27 user 意図確定)

**user 意図 = day-part 構成 (🌅リサーチ導線 / 🌙初期登録) で確定 2026-06-27**。
提案書 (`C:\Users\gucch\projects\claude\.company\engineering\docs\2026-06-27-today-tasks-tab-proposal.md`) §3「1日の2部構成」に明記:
> 「早朝=リサーチ / 夜間=商品管理」という user の 1 日のリズムに沿わせる。
> 🌅 早朝の部 = ジャンプボタン (「リサーチ対戦」/ 「今日の発掘」)
> 🌙 夜間の部 = 本タブの主役 (10件チェックリスト)

設計 agent が提案書を読めなかった (cwd 相違) ため「rank 5/5 の午前/午後 cosmetic 分割」と誤解釈していたが、本修正で day-part 構成に訂正。

**実装要点**:
- コンディションバーは 2 列レイアウト: 左 = 🌅 早朝の部 (ボタン 2 個)、右 = 🌙 夜間の部 (X/10 ゴール表示)
- 🌅 ボタンの routing は `_w134_sel = "リサーチ対戦"` / `"今日の発掘"` + `st.rerun()` (§8 と同一 contract)
- 10 件リストは **rank 連続のまま分割しない** (時間帯で DB/表示を分けない / K1)

---

## 7. app.py 配線 (正確な挿入箇所)

### 7.1 nav エントリ (1 個)

`_W134_GROUPS["★ 毎日"]` (L235-242)。**"DASHBOARD" (L236) の直後**に挿入 = 毎朝最初に開く想定で先頭付近。

```python
    "★ 毎日": [
        "DASHBOARD",
        "本日の作業",       # W292 (2026-06-27) ← 追加
        "依頼ボード",       # W266 (2026-06-12)
        "商品管理",         # W119 (2026-05-11)
        ...
```

> `_W134_TABS` (L269) / `_PAGE_TO_GROUP` (L431) は `_W134_GROUPS` から自動派生するので追加変更不要。

### 7.2 routing 分岐 (1 個)

dispatch 群 (L524 以降の `if _w134_sel == "...":` 連鎖)。**商品管理ブロック (L546-564) の直前**、または DASHBOARD ブロック (L524-526) の直後に挿入。duel タブ (L639-644) と同じ try/except で描画エラーを可視化:

```python
# ========== 本日の作業 タブ (W292 / 2026-06-27) ==========
# 売れ筋上位の「初期登録 未完了」listing 10 件を当日固定で消化する作業ボード。
# 完了 = 商品管理タブと同じ initial_registered トグル。10件全完了で streak +1。
if _w134_sel == "本日の作業":
    try:
        from tabs.tab_today_tasks import render_today_tasks_tab
        render_today_tasks_tab(s)
    except Exception as _e:
        st.error(f"本日の作業タブ 描画エラー: {_e}")
```

> `s = st.session_state.settings` (L228) を渡す (既存タブ規約)。商品管理タブのような `config` 自前構築は不要 (本タブは settings に依存しない / K1)。

---

## 8. jump-to-登録 フック設計 (商品管理タブへの接続)

### 8.1 接続点 (実コードで確認した具体箇所)

商品管理タブの listing 解決フローは:

1. `render_product_management(config)` (tab_product_management.py **L5179**) → `_apply_filter_and_sort(products)` (**L5602** 呼出 / 定義 **L376**) が `st.text_input(key="pm_search")` (**L379**) を読み、`pm_search` で title/SKU/Item ID 部分一致フィルタ (**L431-438**)。
2. フィルタ後 `_build_list_dataframe(filtered)` (**L4924** / 呼出 **L5647**) → `st.dataframe(..., key="pm_list_table", on_select="rerun", selection_mode="single-row")` (**L5648**)。
3. 行選択 `_event.selection.rows` (**L5670**) → `_sel_eid = _list_df.iloc[_idx]["Item ID"]` (**L5677**) → `_render_product_editor(_sel_p, config)` (**L5690**)。

`st.dataframe` の選択行は widget key (`pm_list_table`) 管理で**プログラム的な事前選択が不可**。そこで **`pm_search` を eid で seed** して表を 1 行に絞り、user がその行をクリックして editor を開く方式を採る (最小・確実)。

### 8.2 フック実装 (2 ファイル最小変更)

**(a) 送り手 = `tab_today_tasks.py` `_render_task_row`**: 「📝 登録画面へ」ボタン。

```python
if st.button("📝 登録画面へ", key=f"{_SS}jump_{t['ebay_item_id']}"):
    st.session_state["pm_focus_eid"] = t["ebay_item_id"]   # 受け渡し focus
    st.session_state["_w134_sel"] = "商品管理"              # タブ切替 (routing contract)
    st.rerun()
```

> `_w134_sel` への代入は app.py routing contract (L479「素のページ名を代入」) に準拠 = 素の `"商品管理"`。

**(b) 受け手 = `tab_product_management.py` `render_product_management` 冒頭** (L5566 `products = ...` の直前あたり、`st.title("商品管理")` 後)。`pm_focus_eid` を **1 回だけ消費**して `pm_search` に seed:

```python
# W292: 本日の作業タブからの jump 着地。pm_focus_eid を 1 度だけ消費し
# 検索欄 (pm_search) に Item ID を seed → 表が当該 1 行に絞られる。
# (st.dataframe は事前行選択 API が無いため検索で 1 行化 = user が即クリック可能。)
_focus = st.session_state.pop("pm_focus_eid", None)
if _focus:
    st.session_state["pm_search"] = str(_focus)
    st.info(f"📝 本日の作業から遷移: Item ID `{_focus}` を検索欄に設定しました。"
            "下の表で行をクリックすると編集ゾーンが開きます。")
```

> `pm_search` widget は L379 で `key="pm_search"` 生成。widget **生成前**に session_state へ書けば初期値として反映される (Streamlit 仕様)。`pop` で 1 回消費 = 以後 user が検索を消しても再 seed されない (focus の粘着を防ぐ)。

### 8.3 なぜ自動展開 (editor 直開き) にしないか

`st.dataframe` の選択は widget 内部状態で、外部から `selection.rows` を注入する公式 API が無い (streamlit#11345 系)。無理に session_state を捏造すると money-direct ガード (L5623-5640 の filter_sig 破棄ロジック) と競合し、**別 listing の価格/送料を誤編集するリスク** (W225 HIGH-2 が警告済)。よって「検索で 1 行に絞る → user が明示クリック」が安全。editor 冒頭の確認バナー (L5686「✏️ 編集中…Item ID 一致確認」) もそのまま機能する。

---

## 9. テスト計画

`tests/test_daily_task_db.py` (新規) を中心に。Q1 実機検証は §10 build sequence の各段で実施。

### 9.1 冪等性 (init_db 2 回でデータ保持)

```python
def test_v83_idempotent(tmp monitor.db):
    init_db()
    # daily_task_set に 1 行 INSERT
    init_db()  # 再実行
    assert daily_task_set の行が保持 (>=1)
    assert PRAGMA user_version == 83
```

### 9.2 選定ロジック

- **売れ筋 DESC が主キー**: sold 高い listing が rank 1。
- **タイブレーク順**: 同 sold で `competitor_count==0` が先、次に欠落数多い順、最後に ebay_item_id 昇順 (決定性)。
- **active 条件**: `is_ended=1` / title 空 listing は選定プールに入らない。
- **`initial_registered=1` 除外**: 既登録は選定されない。
- **snapshot 固定 (方針 C)**: 同日 2 回 `get_or_create_today_task_set` を呼ぶ → **同一 10 件・同一 rank** (間に未済 listing を増やしても当日セットは不変)。
- **JST 境界**: `today_jst` を跨ぐと別セットが生成される (異なる jst_date で別 10 件)。`DATE('now','+9 hours')` の値で別日と判定。
- **プール < 10**: 未済が 3 件なら 3 件だけ凍結 (total=3、エラーにしない)。
- **プール 0**: 凍結 0 件、`logger.warning` 発火、UI は「未登録なし」。

### 9.3 ストリーク

- 当日 all_done → `bump_streak_on_completion` で current_streak=1。
- 翌日 all_done → current_streak=2 (連続)。
- 1 日飛ばし → current_streak=1 にリセット。
- 同日二度押し → streak 不変 (`last_done_date==today` ガード)。
- best_streak は max 追従。
- `get_streak` 行不在 → 全 0 / None (副作用なし)。

### 9.4 欠落バッジ

- 5 列すべて埋まり → `_missing_badges` 空 list。
- 各列を 1 つずつ欠落 → 対応ラベルが出る。
- 寸法は 3 軸のうち 1 つ欠けただけで「寸法未」。
- 商品管理 `only_missing` (L439-445) と同 listing で同判定になること。

---

## 10. build sequence + ファイル割当

委譲オーケストレーション前提 (各段で code-reviewer HIGH=0 → 次段)。並列時は同一 file を複数 agent が触らない。

### 段1: データ層 (DB + migration + test)

| ファイル | 変更 | 担当 model 目安 |
|---|---|---|
| `monitor/database.py` | `init_db` に v83 ブロック追加 (§2.1)。**既存行は触らない (K2)** | Sonnet 4.6 (money/DB-direct) |
| `monitor/daily_task_db.py` | **新規**。§4 の helper 5 関数 + §3 選定 SQL + §5 `_missing_badges` の素データ部分 | Sonnet 4.6 |
| `tests/test_daily_task_db.py` | **新規**。§9.1-9.4 | Sonnet 4.6 |

**段1 検証**: pytest (新規テスト + 既存回帰) + 冪等性テスト (init_db 2 回でデータ保持・user_version=83) + DB SELECT で選定 SQL を実 DB で叩き売れ筋 DESC・snapshot 固定を目視。**code-reviewer HIGH=0** (DB migration なので DROP/ALTER 無 try/except・Q2 を重点)。

### 段2: UI 層 (タブ + app.py 配線 + jump フック + test)

| ファイル | 変更 | 担当 model 目安 |
|---|---|---|
| `tabs/tab_today_tasks.py` | **新規**。§6 の render 分解 + §5 バッジ表示 + §8.2(a) jump ボタン | Sonnet 4.6 |
| `app.py` | §7.1 nav 1 行 + §7.2 routing 分岐 1 個 (計 2 箇所、**他は不変**) | Haiku 4.5 (定型 2 箇所) |
| `tabs/tab_product_management.py` | §8.2(b) 受け手フック (`render_product_management` 冒頭に ~5 行)。**既存 editor/dataframe ロジックは不変 (K2, money-direct ガード保持)** | Sonnet 4.6 |

**段2 検証 (Q1 DoD)**:
1. Streamlit 再起動 (config 起動時固定のため必須)。
2. Playwright で「本日の作業」タブを開く → 10 件表示・売れ筋順・進捗リング・欠落バッジ・streak chip を視認。
3. 1 件チェック → 完了帯化 + 進捗リング X/10 増加。商品管理タブに切替え同 listing が `initial_registered=1` で同期 (bump_db_version 経路) を視認。
4. 「📝 登録画面へ」→ 商品管理タブに遷移し `pm_search` に eid が入り 1 行に絞れる → 行クリックで editor 展開を視認。
5. 全件チェック → 🎉 セレブレーション + balloons + streak +1。
6. DB SELECT で `daily_task_set` 10 行・`daily_task_streak` の current_streak を確認。
7. ページリロード → 同 10 件 (snapshot 固定) を確認。
8. **code-reviewer HIGH=0** (jump フックが money-direct ガードと競合しないこと・session_state 衝突なしを重点)。

### 段3: ROADMAP 登録

`data/system_improvements.json` に追記 (`/add_s` または手動)。スキーマ (id 356 の例に準拠):

```json
{
  "id": 357,
  "tag": "W292",
  "title": "本日の作業タブ (売れ筋上位の初期登録未完了10件を当日固定で消化+streak)",
  "description": "MonoDeck「★毎日」に本日の作業タブを新設。売れ筋total_sold_count DESC主の未登録listing 10件をdaily_task_setにJST当日固定(snapshot)、欠落項目バッジ(表示のみ強制なし)、進捗リング/🔥streak、完了=既存initial_registeredトグル流用、商品管理へのjump-toフック。migration v83(daily_task_set/daily_task_streak)。",
  "status": "in_progress",
  "priority": "通常",
  "created": "2026-06-27",
  "category": "ui"
}
```

---

## 11. 設計上の判断ポイント・risk まとめ

| 項目 | 判断 / risk | 対処 |
|------|------------|------|
| **完了真値の単一化** | `initial_registered` を唯一の完了真値とし `daily_task_set` に複製しない | 2 タブ間不整合ゼロ。`bump_db_version` で即同期 |
| **snapshot 固定の実装** | `daily_task_set` の `UNIQUE(jst_date,rank)` + `INSERT OR IGNORE` | 並行 rerun・再訪で二重生成しない。0 件凍結は warning |
| **jump-to が直開きでない** | st.dataframe 事前選択 API 不在 + money-direct ガード競合 risk | `pm_search` seed で 1 行化 → user 明示クリック (W225 HIGH-2 警告を尊重) |
| **「🌅🌙 2部」の解釈** | **day-part 構成で確定 2026-06-27**: 🌅=リサーチジャンプボタン / 🌙=初期登録ゴール表示。10件を午前/午後で割らない | 提案書 §3 を正典として採用。rank 分割の誤解釈を修正済み (§6.2) |
| **未済プール < 10 / 0** | 少件数 / 0 件を正常系として扱う | total を実件数で出す。0 は「未登録なし」表示 |
| **listing が当日中に delist** | LEFT JOIN + `title_snap` fallback で当日セットに残す | 作業対象の当日固定を維持 (`listing_gone` フラグで注記可) |
| **streak の暴走** | 毎 rerun で all_done 検知 → bump 多重呼出 | `last_done_date==today` ガードで冪等吸収 |
| **テーブル名の SKU 不使用** | 両テーブルとも `ebay_item_id` 識別、SKU カラムを持たない | sku-rules 完全準拠 |
| **既存タブの非破壊** | 商品管理タブは受け手フック ~5 行のみ追加、editor/dataframe/money-direct ガードは不変 | K2 surgical。段2 で code-reviewer が確認 |

---

## 付録: 確認した実体クイックリファレンス

- 選定の主キー列: `ebay_listings.total_sold_count` (実在 / `_fetch_all_products` L166)
- ライバル数: `competitor_products.COUNT WHERE our_item_id=? AND is_active=1` → `competitor_count` (L188-190)
- 欠落判定列: `purchase_yen` / `weight_g` / `length_cm`+`width_cm`+`height_cm` / `lp_breakeven_usd` (L153-154)
- 完了フラグ: `ebay_listings.initial_registered` / `initial_registered_at` (L200-201)
- active 述語: `(is_ended IS NULL OR is_ended=0) AND title IS NOT NULL AND title != ''` (L214-215)
- 完了トグル helper: `monitor.database.set_initial_registered(ebay_item_id, registered) -> bool` (L4354、`ebay_item_id` 識別・CURRENT_TIMESTAMP=UTC)
- migration 現行最新: **v82** (`PRAGMA user_version = 82`, database.py L3884) → 新規 **v83** (挿入点 = L3889 の後・init_db 末尾 L3891 の前)
- nav: `_W134_GROUPS["★ 毎日"]` (app.py L235) / routing `if _w134_sel == "...":` (L524 以降) / tab 引数 `s = st.session_state.settings` (L228)
- jump 着地: `render_product_management` 冒頭 (L5179) / `pm_search` widget (L379) / dataframe 選択 (L5648-5690)
- テーマ: `ui_themes.apply_neumorph_cream_theme` (root ui_themes.py L475)、トークン `--nm-*` / `--f-num`、app.py で `apply_custom_styling` (L995→475) 全体適用済
- データ層先例: `monitor/research_duel_db.py` (`from .database import get_conn`、薄い CRUD)
- W番号: 次 `id: 357` / `tag: "W292"` (現行最大 W-token=W291、W287/W289/W292 未使用)
