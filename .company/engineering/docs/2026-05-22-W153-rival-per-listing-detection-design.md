---
name: w153-rival-per-listing-detection-design
description: 商品ごとの新規ライバル発見の根本作り直し (initial_registered_at base point + rival_watch_started_at + Claude Haiku title-only keyword 助言 + DB 永続化 + UI 編集可能 + W183 監視追加への流入)
layer: wiki
updated: 2026-05-22 (v2 with 2-stage review fix)
revision: v2.1
sources:
  - tools/ebay-manager/tasks/task_rival_detection.py (354 行、現状実装、グローバル known set モデル)
  - tools/ebay-manager/tasks/task_rival_pricing.py (W183 自動値下げ、competitor_products WHERE is_active=1 巡回)
  - tools/ebay-manager/tasks/ebay_browse_api.py (BrowseAPIClient.search_items, item_location_country=JP)
  - tools/ebay-manager/tasks/task_generate_search_keywords.py (W119 既存 Opus batch keyword 生成、本 W は Haiku per-listing 同期版で再利用しない=K1)
  - tools/ebay-manager/monitor/database.py L432-468 (competitor_products / new_competitor_alerts schema)
  - tools/ebay-manager/monitor/database.py L2378-2407 (W151 v49 migration self-heal idiom)
  - tools/ebay-manager/monitor/database.py L2698-2722 (set_initial_registered, W153 base point として参照)
  - tools/ebay-manager/monitor/database.py L2604-2667 (upsert_ebay_listing, additive 列の保全パターン)
  - tools/ebay-manager/monitor/database.py L2840-2855 (add_competitor_product、UNIQUE(competitor_item_id) 制約)
  - tools/ebay-manager/monitor/database.py (claim_alert_dedupe / health_alert_log、weekly reminder 流用)
  - tools/ebay-manager/tabs/tab_product_management.py L2035-2225 (_render_one_product, hero + form/non-form 構造)
  - tools/ebay-manager/tabs/tab_product_management.py L2054-2072 (W151 初期登録 checkbox UI、本 W はその直下に新 section)
  - tools/ebay-manager/tabs/tab_product_management.py L1283-1366 (_render_right_inventory_supplier_rival, form 外 = 個別 button OK)
  - tools/ebay-manager/tabs/tab_product_management.py L1407-1500 (_render_rival_dataframe, 既存 competitor_products 表示)
  - tools/ebay-manager/daily_scheduler.py L620-625 (should_task_run('rival_detection') 呼び出し点、本 W で `run_task(task_key='rival_detection')` 経路維持)
  - tools/ebay-manager/monitor/task_execution_log.py L42 (TASK_SCHEDULE rival_detection entry、display 名のみ変更)
  - tools/ebay-manager/config/schedule_config.json L140-145 (rival_detection block、interval/note 改訂)
related:
  - 2026-05-21-W149-ebay-orders-fetch-fulfillment-link-design (sold 軸の populate 基盤、W153 が依存する売れ筋計算には別 W、本 W は独立稼働)
  - 2026-05-20-W148-alertcrawler-keyword-watch-design (兄弟 W、本ファイルとは独立 scope)
  - .claude/rules/silent-skip-prevention.md (Q0)
  - .claude/rules/db-migration-rules.md (Q2)
  - .claude/rules/sku-rules.md (listing 識別は ebay_item_id、本 W で SKU は不使用)
  - .claude/rules/sqlite-timezone.md (UTC 保存徹底)
  - .claude/rules/cascade-update.md (test pin / MEMORY.md 同 session 更新)
genre: rival-detection
metadata:
  type: design
  wiki_type: synthesis
---

# 新規ライバル発見の根本作り直し 設計書 — W153 (v2)

**作成日**: 2026-05-22 (午前、W149/W150/W151/W152 完走直後の連続着手)
**v2 改訂日**: 2026-05-22 (同日午後、内部 code-reviewer Opus 4.7 + 外部 Codex GPT-5.5 2 段 review 反映)
**v2.1 改訂日**: 2026-05-22 (同日午後、Codex GPT-5.5 2 周目 review で新規 HIGH 3 / MED 3 / LOW 1 を pinpoint 解消)
**ROADMAP id**: 236 (W153、`data/system_improvements.json` L1399 にて既に登録済、status="未着手")
**設計フェーズ**: Q3 構造化フロー Phase 5 (= v2 改訂、Phase 4 v1 設計の 2 段 review 反映後)
**business critical**: user 明示「最安値が取れず売上機会損失中」。W183 (2026-05-10 実装済の自動値下げ) は監視対象に競合 ID が無いと発火しない = **本 W が動かないと W183 が空転**。

---

## 1. 概要 + 業務目的

eBay 出品商品ごとに「初期登録以降に新しく出現したライバルセラー」を確実に発見し、user が UI で「監視追加」した分だけ W183 (ライバル自動値下げ、2026-05-10 実装済) の監視対象 (`competitor_products WHERE is_active=1`) に流し込むことで、最安値追従による売上機会損失を防ぐ。

### v2 で v1 から変更された核心 4 点

1. **`rival_watch_started_at` 列追加** (4 列目): late initial_registration が prior discoveries を since filter で消す silent gap を根治 (H-A)
2. **migration v50 drift recovery を schema_ver と独立化**: user_version=50 後の列・table 欠損 drift も自己修復 (H-B)
3. **errors>0 で task 全体 success=False + Discord 別 message**: 偽装成功の構造排除 (H-D)
4. **`add_or_reactivate_competitor` helper 新規**: 過去 is_active=0 にした listing 再採用パス確保 (H-C)

---

## 2. スコープ

### 含まれる
- 新 DB スキーマ (migration v50): `ebay_listings` に **4 列 additive nullable** 追加 + 新 table `listing_rival_discoveries`
- 新 module `monitor/rival_keyword_generator.py` (Claude Haiku per-listing 同期生成、title-only、~140 LOC)
- 既存 `tasks/task_rival_detection.py` の `run_rival_detection(config)` を **全面書換** (354 → ~280 LOC)。関数名は維持
- 商品 hero 内に新 section「🎯 ライバル監視 (W153)」を W151 checkbox の直下に追加 (~220 LOC)
- 新 helper `add_or_reactivate_competitor` (database.py) + `_send_discord_errors_alert` / `_maybe_remind_user_of_unused_w153` (task_rival_detection.py)
- Discord 通知 2 系統: (a) 集約 new>0 通知 (1 run 1 message) (b) errors>0 別 alert
- 0 listings 永続シナリオで週 1 reminder (claim_alert_dedupe 流用、key='w153_unused_weekly')
- Browse API quota 保護: max_listings_per_run / max_requests_per_run / 429 backoff / UI cooldown 60s
- pytest 単一 file ~400 LOC (新規 test 10 件 + 既存 ~25 件)

### 含まれない (K1)
- 既存 `new_competitor_alerts` テーブルの整理 / migration
- 既存 `known_rival_sellers.json` の整理 / 削除
- 新規 rival 自動採用 (常に user 手動承認)
- listing 単位を超えた cross-listing 集計
- W122 / W148 との統合
- 既存 active competitor_products の整理・再判定
- **monitoring_added tab の「監視解除」button** (L-internal-2 admit)
- **`ebay_listings.brand` / `mpn` 列追加** (M-codex-10 admit、別 W)
- **Browse API quota 共有 monitoring** (H-H admit / Q12 next W)
- **N:1 監視** (同 competitor を複数 listing から監視追加、UNIQUE 緩和は別 W)

---

## 3. 既存システム分析

v2 図解 (rival_watch_started_at + errors alert + reminder + quota guard 追加):

```
[user UI: 商品 hero 内 W153 section]
    ├─ ☑ ライバル監視 ON           → ebay_listings.rival_watch_enabled = 1
    │                                  ebay_listings.rival_watch_started_at = NOW()  ← NEW (H-A anchor)
    ├─ 🤖 検索ワード生成 (Haiku)   → rival_keyword_generator.generate(title=only)
    │     └─ ebay_listings.rival_search_keywords (改行区切り) 保存
    ├─ 📝 検索ワード textarea (上書き可)
    └─ 🔍 今すぐ検索 (this listing)  → run_rival_per_listing_detection_one(eid, sleep_between=0.0)
            └─ UI cooldown 60s (連打 reject)

[cron 02 時 (初期は朝のみ、観察後広げる)]
    └─ run_rival_detection(config)
         ├─ SELECT WHERE rival_watch_enabled=1 AND COALESCE(is_ended,0)=0
         ├─ max_listings_per_run=30, max_requests_per_run=150 cap で early break
         ├─ for each listing:
         │     for each keyword:
         │         BrowseAPI.search_items(query=keyword, country=JP, limit=50)
         │         429 → exponential backoff 3 retry → 失敗時 summary['errors']++
         │         INSERT OR IGNORE INTO listing_rival_discoveries (...)
         ├─ if new_by_listing: 集約 Discord 通知 (1 run 1 message)
         ├─ if summary["errors"] > 0:
         │     _send_discord_errors_alert(config, summary, per_listing_summaries)
         │     summary["success"] = False   ← Q0: errors>0 = task failed
         └─ if listings == [] and last_reminder >= 7 days:
                 _maybe_remind_user_of_unused_w153(config)
                 └─ claim_alert_dedupe('w153_unused_weekly') で週 1 cap

[user UI: 「📋 検出済 rival 一覧 (N 新規)」expander]
    ├─ status='new' tab
    │     ├─ 監視追加 → add_or_reactivate_competitor(...)
    │     │              ├─ action='added'        → 「W183 監視対象に追加」
    │     │              ├─ action='reactivated'  → 「過去解除 → 再アクティブ化」
    │     │              └─ action='conflict'     → 「他 listing で監視中、別 W」明示
    │     └─ 却下     → status='dismissed'
    └─ status='monitoring_added' / 'dismissed' tab (履歴閲覧)
                                │
                  ┌─────────────▼─────────────┐
                  │ competitor_products       │  ←★ user 承認分のみ流入
                  │ is_active=1               │
                  └─────────────┬─────────────┘
                                │
                  ┌─────────────▼─────────────┐
                  │ W183 task_rival_pricing   │
                  └───────────────────────────┘
```

---

## 4. 作成/修正ファイル一覧

### 新規作成 (3 file)

| パス | 役割 | 規模 |
|---|---|---|
| `tools/ebay-manager/monitor/rival_keyword_generator.py` | Claude Haiku per-listing 同期 keyword 生成. `generate_keywords(title) -> list[str]` を export. 3-5 候補、各候補 3-6 語、title から抽出可能な brand/model 相当語を含める (M-codex-10 緩和). `claude-haiku-4-5-20251001` 使用. **output filter 強化** (apology/numbering reject、3 ≤ words ≤ 6 enforce、<3 valid で raise). **API key は EBAY_ANTHROPIC_KEY 優先 → ANTHROPIC_API_KEY fallback** | ~140 LOC |
| `tools/ebay-manager/tests/test_w153_rival_per_listing_2026_05_22.py` | pytest 単一 file. 6 セクション + 新規 10 test 追加 | ~400 LOC |
| `tools/ebay-manager/scripts/backfill_rival_watch_initial_2026_05_22.py` | one-shot sanity check. `SELECT COUNT(*) FROM ebay_listings WHERE rival_watch_enabled=1` を出すだけ | ~30 LOC |

### 修正 (5 file)

| パス | 修正内容 | 規模 |
|---|---|---|
| `tools/ebay-manager/monitor/database.py` | (a) migration v50 追加. ebay_listings に 4 列 additive nullable (`rival_watch_enabled` / `rival_search_keywords` / `rival_search_keywords_generated_at` / **`rival_watch_started_at`** ← v2 追加). (b) 新 table `listing_rival_discoveries` + 3 index. (c) **v50 drift recovery を `schema_ver<50` ブランチ外**で実施. (d) helper `set_rival_watch_enabled` (ON 時 started_at=NOW) / `set_rival_search_keywords` / `record_rival_discovery` / `get_rival_discoveries(since=...)` / `update_rival_discovery_status` / **`add_or_reactivate_competitor`** ← v2 新規 | ~220 LOC |
| `tools/ebay-manager/tasks/task_rival_detection.py` | **全面書換** (354 → ~280 LOC). 関数名 `run_rival_detection(config) -> dict` は維持. `run_rival_per_listing_detection_one(eid, config, *, sleep_between=2.0, keywords_override=None)` に sleep arg 追加 (M-internal-7). 429 retry / bad item_id counter / 空 keyword errors++ / `_send_discord_errors_alert` / `_send_discord_aggregate` / `_maybe_remind_user_of_unused_w153`. errors>0 → success=False. max_listings_per_run / max_requests_per_run early break | ~280 LOC |
| `tools/ebay-manager/tabs/tab_product_management.py` | (a) imports 追加. (b) **新 helper `_render_rival_watch_section(p, config)` (~220 LOC)** を W151 checkbox 直下に呼ぶ. (c) form 外 (st.form の外側). (d) ① 監視 ON checkbox + ② textarea + ③ 🤖 生成 + ④ 💾 保存 + ⑤ 🔍 今すぐ検索 (cooldown 60s) + ⑥ 📋 検出済 expander (件数バッジ、status tab 3 種、action 別 message). **session_state 上書きは `*_pending` key 経由** (M-internal-1) | ~220 LOC |
| `tools/ebay-manager/config/schedule_config.json` | `rival_detection` block の `note` 改訂 + `execution_times = [2]` (初期は朝のみ、H-H) + `max_listings_per_run: 30` + `max_requests_per_run: 150`。**具体 JSON diff (v2.1 LOW-7 fix):** 下記 §4.1 参照 | ~6 LOC |

### 4.1 `schedule_config.json` の具体 diff (v2.1 LOW-7 fix)

```diff
   "rival_detection": {
     "enabled": true,
-    "description": "新規ライバルセラー検出（Browse API）",
+    "description": "W153 商品別ライバル検出 (rival_watch_enabled=1 listing のみ Browse API 巡回)",
     "priority": 7,
-    "note": "ebay_sync後の最新キーワードを使用"
+    "execution_times": [2],
+    "max_listings_per_run": 30,
+    "max_requests_per_run": 150,
+    "note": "W153 (2026-05-22 改訂): 商品毎 (rival_watch_enabled=1) の検索ワードで Browse API を巡回し listing_rival_discoveries に新規 rival を蓄積. ebay_sync 後に実行 = is_ended 判定が最新. グローバル known set モデルは廃止、user 承認分のみ competitor_products に流入し W183 値下げ対象に昇格. 対象 0 件の時は task_execution_log に痕跡 + 週 1 Discord reminder (claim_alert_dedupe key='w153_unused_weekly')."
   },
```

`TASK_SCHEDULE` (`monitor/task_execution_log.py:42`) も `hours=[2]` に揃える検証を §13 DoD step 5 に含める。
| `tools/ebay-manager/monitor/task_execution_log.py` | TASK_SCHEDULE display "ライバルセラー検出" → "W153 商品別ライバル検出". hours [2] | ~2 LOC |

### 触らない (K2 Surgical)

- `tasks/task_rival_pricing.py` (W183、I/F 変更なし)
- `monitor/lowest_price.py`
- `monitor/ebay_competitor_monitoring.py`
- `data/known_rival_sellers.json` (legacy、残置)
- `monitor/database.py::new_competitor_alerts` テーブル
- `tabs/tab_product_management.py::_render_rival_dataframe`
- `_render_new_alerts_for_listing`

---

## 5. DB スキーマ変更 (migration v50)

```sql
-- (a) ebay_listings に 4 列 additive nullable
ALTER TABLE ebay_listings ADD COLUMN rival_watch_enabled INTEGER DEFAULT 0;
ALTER TABLE ebay_listings ADD COLUMN rival_search_keywords TEXT;
ALTER TABLE ebay_listings ADD COLUMN rival_search_keywords_generated_at TIMESTAMP;
ALTER TABLE ebay_listings ADD COLUMN rival_watch_started_at TIMESTAMP;  -- ★ v2 追加

-- (b) 新 table listing_rival_discoveries
CREATE TABLE IF NOT EXISTS listing_rival_discoveries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_item_id          TEXT NOT NULL,
    competitor_seller     TEXT NOT NULL,
    competitor_item_id    TEXT NOT NULL,
    competitor_title      TEXT,
    competitor_price_usd  REAL,
    search_keyword        TEXT,
    first_seen_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status                TEXT NOT NULL DEFAULT 'new',  -- 'new' | 'monitoring_added' | 'dismissed'
    status_changed_at     TIMESTAMP,
    UNIQUE(ebay_item_id, competitor_seller, competitor_item_id)
);

CREATE INDEX IF NOT EXISTS idx_lrd_listing_status ON listing_rival_discoveries(ebay_item_id, status);
CREATE INDEX IF NOT EXISTS idx_lrd_first_seen ON listing_rival_discoveries(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_lrd_status_new ON listing_rival_discoveries(status) WHERE status = 'new';
```

### 冪等性 / 自己修復 (Q2 / H-B fix: schema_ver と独立 drift recovery)

```python
# monitor/database.py 末尾、v49 ブロック直後に追加
import sqlite3, logging
logger = logging.getLogger(__name__)

logger.info(f"[init_db] sqlite3.sqlite_version={sqlite3.sqlite_version}")  # M-internal-4

DDL_MAP = {
    'rival_watch_enabled': 'INTEGER DEFAULT 0',
    'rival_search_keywords': 'TEXT',
    'rival_search_keywords_generated_at': 'TIMESTAMP',
    'rival_watch_started_at': 'TIMESTAMP',
}

LRD_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS listing_rival_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_item_id TEXT NOT NULL,
    competitor_seller TEXT NOT NULL,
    competitor_item_id TEXT NOT NULL,
    competitor_title TEXT,
    competitor_price_usd REAL,
    search_keyword TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'new',
    status_changed_at TIMESTAMP,
    UNIQUE(ebay_item_id, competitor_seller, competitor_item_id)
)
"""

LRD_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lrd_listing_status ON listing_rival_discoveries(ebay_item_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_lrd_first_seen ON listing_rival_discoveries(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_lrd_status_new ON listing_rival_discoveries(status) WHERE status = 'new'",
)

# (1) 列存在 check & 欠損 ALTER (schema_ver 無関係 / H-B)
_cols_el = set(r[1] for r in conn.execute("PRAGMA table_info(ebay_listings)").fetchall())
_missing_cols = {c for c in DDL_MAP.keys() if c not in _cols_el}
for col in _missing_cols:
    try:
        conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col} {DDL_MAP[col]}")
        logger.info(f"[init_db v50] recovered missing column: ebay_listings.{col}")
    except sqlite3.OperationalError:
        pass

# (2) listing_rival_discoveries table 存在 check & 欠損 CREATE
_has_lrd = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_rival_discoveries'"
).fetchone()
if not _has_lrd:
    try:
        conn.execute(LRD_CREATE_SQL)
        logger.info("[init_db v50] recovered missing table: listing_rival_discoveries")
    except sqlite3.OperationalError:
        pass

# (3) index 存在 check & 欠損 CREATE (M-internal-8)
for idx_sql in LRD_INDEXES:
    try:
        conn.execute(idx_sql)
    except sqlite3.OperationalError:
        pass

# (4) 完全に揃った後でのみ user_version bump (schema_ver < 50 のとき)
_cols_post = set(r[1] for r in conn.execute("PRAGMA table_info(ebay_listings)").fetchall())
_lrd_post = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_rival_discoveries'"
).fetchone()

if set(DDL_MAP.keys()).issubset(_cols_post) and _lrd_post is not None:
    if schema_ver < 50:
        conn.execute("PRAGMA user_version = 50")
        logger.info("[init_db v50] schema_ver bumped 49 -> 50")
else:
    logger.warning(
        f"[init_db v50] drift recovery incomplete: "
        f"missing_cols={set(DDL_MAP.keys()) - _cols_post}, has_lrd={_lrd_post is not None}"
    )
```

### 既存データへの影響

- ebay_listings 587 listings: 4 列全て default 0 / NULL → 動作変更なし (opt-in 設計)
- listing_rival_discoveries: 初期 0 件
- 既存 competitor_products / new_competitor_alerts は不変

---

## 6. コンポーネント設計

### 6.1 `monitor/rival_keyword_generator.py` (新規)

```python
"""W153: per-listing 同期 keyword 生成 (Claude Haiku, title-only).

ebay_listings に Brand / MPN 列が無いため、本 W は title のみで生成
(user 合意済 2026-05-22). Brand/MPN 列追加は別 W (M-codex-10 admit).
"""
import logging
import os
import re
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)

KEYWORD_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 200

_PROMPT_TEMPLATE = """You are an eBay search keyword generator for a Japan→US cross-border seller.

Given an eBay listing's title, output 3-5 search keyword candidates that find direct competitor listings on eBay.

# Rules
- Each candidate: 3-6 words, separated by spaces (URL-friendly)
- Include any brand/model number that you can extract from the title (narrowing is important)
- Add differentiating attributes (color / capacity / size / variant) when extractable
- Skip filler words: condition (NEW/Used/Mint), year, packaging, region tags ("from Japan", "F/S")
- Output ONE candidate per line, no numbering, no quotes, no explanations, no apologies
- If the title is in Japanese, keep brand names in English / Latin script when possible
- Output ONLY the candidates, nothing else

# Title
{title}

# Output (3-5 lines)"""

# H-F: apology / explanation / numbering pattern reject
_APOLOGY_PATTERN = re.compile(
    r"(?i)(I (cannot|can'?t|am sorry|apolog|don'?t know|need)|sorry|here are|note:|please)"
)
_NUMBERED_PATTERN = re.compile(r"^[0-9]+[\.\)]")


def _resolve_api_key() -> str:
    """H-F: EBAY_ANTHROPIC_KEY 優先 → ANTHROPIC_API_KEY fallback."""
    key = os.environ.get("EBAY_ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "Anthropic API key not set "
            "(checked EBAY_ANTHROPIC_KEY, ANTHROPIC_API_KEY)"
        )
    return key


def generate_keywords(
    *,
    title: str,
    brand: Optional[str] = None,  # 将来拡張用 signature 保持
    mpn: Optional[str] = None,
    specifics: Optional[dict[str, str]] = None,
) -> list[str]:
    """3-5 candidate keywords for eBay rival search (title-only in W153).

    Raises:
        RuntimeError: API key 未設定
        ValueError: Haiku output 異常 (<3 valid)
    """
    api_key = _resolve_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    prompt = _PROMPT_TEMPLATE.format(title=title or "(empty)")

    msg = client.messages.create(
        model=KEYWORD_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text if msg.content else ""

    candidates: list[str] = []
    for line in raw.split("\n"):
        s = line.strip().strip('"\'.,!?;:')
        if not s:
            continue
        if len(s) > 100:
            continue
        # H-F output filter
        if _APOLOGY_PATTERN.search(s):
            logger.warning(f"[W153 generator] rejected apology-like line: {s[:60]!r}")
            continue
        if _NUMBERED_PATTERN.match(s):
            logger.warning(f"[W153 generator] rejected numbered line: {s[:60]!r}")
            continue
        words = s.split()
        if not (3 <= len(words) <= 6):
            logger.warning(f"[W153 generator] rejected wrong-word-count line: {s[:60]!r}")
            continue
        candidates.append(s)

    if len(candidates) < 3:
        raise ValueError(
            f"Haiku returned only {len(candidates)} valid candidates "
            f"(need >=3). raw={raw!r}"
        )

    return candidates[:5]
```

### 6.2 `monitor/database.py` 新規 helper 関数

```python
def set_rival_watch_enabled(ebay_item_id: str, enabled: bool) -> bool:
    """W153: ライバル監視 ON/OFF.

    ON 時は rival_watch_started_at = COALESCE(既存, NOW()) も set (H-A、再 ON で巻き戻さない).
    OFF 時は rival_watch_started_at を維持 (NULL に戻さない).

    ★ v2.1 設計判断 (HIGH-1 admit, Codex 2 周目): OFF→keyword 変更→再 ON で「履歴連続性」を
    優先する設計を採用。再 ON で anchor リセット (rival_watch_started_at=NOW で旧 discovery を
    since filter から消す) は、user の「監視を一度止めただけ」と「監視対象を完全に変えた」の
    意図区別が UI では不可能なため、保守的 (継続) を default。
    「監視リセット」UI button は別 W で議論 (本 W K1 scope 外)。
    user が再 ON 後に新キーワードで cron 走らせると、同 competitor は UNIQUE で既存 row を
    last_seen_at 更新するため、dismissed status は維持される (= 過去判断尊重)。新規 competitor
    は status='new' で出現。これは silent gap ではなく明示された設計判断。
    """
    with get_conn() as conn:
        if enabled:
            cur = conn.execute(
                "UPDATE ebay_listings "
                "SET rival_watch_enabled = 1, "
                "    rival_watch_started_at = COALESCE(rival_watch_started_at, CURRENT_TIMESTAMP) "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_watch_enabled = 0 "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
    return cur.rowcount == 1


def set_rival_search_keywords(
    ebay_item_id: str,
    keywords_text: str,
    *,
    mark_generated: bool = False,
) -> bool:
    """改行区切り keyword text を保存. mark_generated=True で generated_at も更新."""
    normalized = "\n".join(
        line.strip() for line in (keywords_text or "").split("\n") if line.strip()
    )
    with get_conn() as conn:
        if mark_generated:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_search_keywords = ?, "
                "rival_search_keywords_generated_at = CURRENT_TIMESTAMP "
                "WHERE ebay_item_id = ?",
                (normalized, ebay_item_id),
            )
        else:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_search_keywords = ? "
                "WHERE ebay_item_id = ?",
                (normalized, ebay_item_id),
            )
    return cur.rowcount == 1


def record_rival_discovery(
    *,
    ebay_item_id: str,
    competitor_seller: str,
    competitor_item_id: str,
    competitor_title: str = "",
    competitor_price_usd: Optional[float] = None,
    search_keyword: str = "",
) -> Optional[int]:
    """claim-then-act: INSERT OR IGNORE → rowcount==1 で新規 id、0 で既存更新."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword,
                first_seen_at, last_seen_at, status)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'new')""",
            (ebay_item_id, competitor_seller, competitor_item_id,
             competitor_title, competitor_price_usd, search_keyword),
        )
        if cur.rowcount == 1:
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """UPDATE listing_rival_discoveries
               SET last_seen_at = CURRENT_TIMESTAMP,
                   competitor_price_usd = COALESCE(?, competitor_price_usd)
               WHERE ebay_item_id = ? AND competitor_seller = ?
                 AND competitor_item_id = ?""",
            (competitor_price_usd, ebay_item_id, competitor_seller, competitor_item_id),
        )
        return None


def get_rival_discoveries(
    ebay_item_id: str,
    status: str = 'new',
    *,
    since: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """W153: discoveries を取得. since は ISO timestamp (anchor は呼び側で計算)."""
    sql = (
        "SELECT * FROM listing_rival_discoveries "
        "WHERE ebay_item_id = ? AND status = ?"
    )
    args: list = [ebay_item_id, status]
    if since:
        sql += " AND first_seen_at >= ?"
        args.append(since)
    sql += " ORDER BY first_seen_at DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def update_rival_discovery_status(discovery_id: int, new_status: str) -> bool:
    if new_status not in ('new', 'monitoring_added', 'dismissed'):
        raise ValueError(f"invalid status: {new_status}")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listing_rival_discoveries "
            "SET status = ?, status_changed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (new_status, discovery_id),
        )
    return cur.rowcount == 1


# ────────────────────────────────────────────────────────────
# H-C: add_or_reactivate_competitor (新規 helper)
# 過去 is_active=0 にした listing から「監視追加」で IntegrityError
# で永久に W183 流入しない silent gap を根治.
# ────────────────────────────────────────────────────────────
def add_or_reactivate_competitor(
    *,
    our_item_id: str,
    our_sku: str,   # M-internal-2: 補助情報 (sku-rules: 識別キー化はしない)
    competitor_seller: str,
    competitor_item_id: str,
) -> tuple[int, str]:
    """W153 → W183 流入の単一エントリポイント.

    Returns:
        (id, action) where action in {'added', 'reactivated', 'conflict'}
    """
    import sqlite3 as _sq
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO competitor_products
                   (our_item_id, our_sku, competitor_seller, competitor_item_id,
                    seller_location, is_active)
                   VALUES (?, ?, ?, ?, 'Japan', 1)""",
                (our_item_id, our_sku, competitor_seller, competitor_item_id),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return (new_id, 'added')
        except _sq.IntegrityError:
            row = conn.execute(
                "SELECT id, our_item_id, is_active FROM competitor_products "
                "WHERE competitor_item_id = ?",
                (competitor_item_id,),
            ).fetchone()
            if row is None:
                raise
            existing_id = row['id']
            existing_our_iid = row['our_item_id']
            if existing_our_iid == our_item_id:
                # ★ v2.1 MED-6 fix: reactivation 時に our_sku stale を防ぐ (Codex 2 周目検出)
                # SKU は識別キー化はしない (sku-rules) が、補助情報として stale だと downstream
                # 表示 / log が紛らわしい。空文字列なら既存値維持、それ以外は最新値で上書き。
                conn.execute(
                    "UPDATE competitor_products "
                    "SET is_active = 1, "
                    "    our_sku = COALESCE(NULLIF(?, ''), our_sku), "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (our_sku, existing_id),
                )
                return (existing_id, 'reactivated')
            return (existing_id, 'conflict')
```

### 6.3 `tasks/task_rival_detection.py` 全面書換

```python
"""W153 (2026-05-22 改訂): 商品別ライバル検出.

旧 (グローバル known set) は廃止. user が UI で「監視 ON」 した listing
についてのみ、商品個別の検索ワードで eBay Browse API を巡回.
"""
import json
import logging
import sys
import time
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from monitor.credentials import get_ebay_credentials
from monitor.database import (
    get_conn, record_rival_discovery, get_rival_discoveries,
    set_rival_search_keywords, claim_alert_dedupe,
)

logger = logging.getLogger(__name__)


def _get_my_seller_username(config: dict) -> Optional[str]:
    return (config.get('ebay') or {}).get('seller_id') or None


def _fetch_target_listings() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ebay_item_id, title, rival_search_keywords, "
            "       initial_registered_at, rival_watch_started_at "
            "FROM ebay_listings "
            "WHERE COALESCE(rival_watch_enabled, 0) = 1 "
            "  AND COALESCE(is_ended, 0) = 0 "
            "ORDER BY ebay_item_id"
        ).fetchall()
    return [dict(r) for r in rows]


def _split_keywords(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.split("\n") if line.strip()]


def _backoff_sleep(retry_count: int) -> float:
    """H-H: exponential backoff (1s, 2s, 4s ... cap 30s)."""
    return min(2.0 ** retry_count, 30.0)


def run_rival_per_listing_detection_one(
    eid: str,
    config: dict,
    *,
    keywords_override: Optional[list[str]] = None,
    sleep_between: float = 2.0,  # M-internal-7: UI 経路 0.0、cron 2.0
    max_requests_remaining: Optional[int] = None,
) -> dict:
    """単一 listing の検索. UI/cron 双方から呼ぶ."""
    summary = {
        "success": False, "ebay_item_id": eid,
        "new_discoveries": 0, "refreshed": 0, "errors": 0,
        "skipped_bad_item_id": 0, "requests_used": 0,
        "message": "",
    }
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT title, rival_search_keywords "
                "FROM ebay_listings WHERE ebay_item_id = ?", (eid,)
            ).fetchone()
        if not row:
            summary["message"] = f"listing not found: {eid}"
            return summary
        keywords = keywords_override or _split_keywords(row["rival_search_keywords"])
        if not keywords:
            # H-D: 空 keyword で errors++ + success=False
            logger.warning(f"[W153] {eid}: no keywords (UI で生成 or 保存してください)")
            summary["errors"] += 1
            summary["message"] = "no keywords"
            return summary

        creds = get_ebay_credentials(config)
        from tasks.ebay_browse_api import BrowseAPIClient
        try:
            import httpx
            _HTTP_ERRORS = (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError)
        except ImportError:
            _HTTP_ERRORS = (Exception,)

        client = BrowseAPIClient(creds.get('app_id', ''), creds.get('cert_id', ''))
        my_seller = _get_my_seller_username(config)

        for kw in keywords:
            if max_requests_remaining is not None and max_requests_remaining <= 0:
                logger.warning(f"[W153] {eid}: max_requests budget exhausted")
                summary["message"] = "max_requests_per_run reached"
                break
            items = None
            for retry in range(3):
                # ★ v2.1 HIGH-3 fix: 試行 *前* に counter を消費 (failed retry も含めて quota cap 厳守)
                # Codex 2 周目で検出: 旧版は成功後のみ decrement → timeout/429/5xx でも実リクエスト
                # 送信済なのに counter 減らず、retry で max_requests_per_run を実質超過 → 5K/day cap hit リスク
                summary["requests_used"] += 1
                if max_requests_remaining is not None:
                    max_requests_remaining -= 1
                try:
                    items = client.search_items(query=kw, limit=50, item_location_country="JP")
                    break
                except _HTTP_ERRORS as e:
                    msg = str(e)
                    if "429" in msg or "rate" in msg.lower() or "5" in msg[:3]:
                        sleep_s = _backoff_sleep(retry)
                        logger.warning(
                            f"[W153] {eid}: '{kw[:40]}' rate/server error, "
                            f"retry {retry+1}/3 in {sleep_s}s: {e}"
                        )
                        time.sleep(sleep_s)
                        continue
                    logger.warning(f"[W153] {eid}: '{kw[:40]}' API error: {e}")
                    summary["errors"] += 1
                    items = None
                    break
            if items is None:
                summary["errors"] += 1
                continue

            for it in items:
                seller = (it.get("seller") or "").strip()
                if not seller or seller == my_seller:
                    continue
                raw_iid = it.get("item_id") or ""
                parts = raw_iid.split("|")
                competitor_iid = parts[1] if len(parts) >= 2 else raw_iid
                if not competitor_iid:
                    # H-G: silent gap 排除
                    logger.warning(
                        f"[W153] {eid}: Browse API returned item without "
                        f"competitor_item_id (kw={kw!r}, raw_iid={raw_iid!r})"
                    )
                    summary["skipped_bad_item_id"] += 1
                    continue
                new_id = record_rival_discovery(
                    ebay_item_id=eid,
                    competitor_seller=seller,
                    competitor_item_id=competitor_iid,
                    competitor_title=(it.get("title") or "")[:200],
                    competitor_price_usd=it.get("price_usd"),
                    search_keyword=kw,
                )
                if new_id is not None:
                    summary["new_discoveries"] += 1
                else:
                    summary["refreshed"] += 1

        # H-D: errors>0 で success=False
        summary["success"] = (summary["errors"] == 0)
        summary["message"] = (
            f"new={summary['new_discoveries']} "
            f"refreshed={summary['refreshed']} "
            f"err={summary['errors']} "
            f"bad_iid={summary['skipped_bad_item_id']}"
        )
        if sleep_between > 0:
            time.sleep(sleep_between)
    except Exception as e:
        logger.exception(f"[W153] {eid} run_one failed")
        summary["message"] = f"top-level: {type(e).__name__}: {e}"
        summary["success"] = False
    return summary


def run_rival_detection(config: dict) -> dict:
    """cron 経路. 旧版と互換 (success / sellers / new_sellers_count / total_scanned / message)."""
    summary = {
        "success": False, "new_sellers_count": 0, "total_scanned": 0,
        "listings_processed": 0, "new_discoveries_total": 0,
        "errors": 0, "skipped_bad_item_id": 0, "requests_used": 0,
        "sellers": [], "message": "",
    }
    per_listing_summaries: list[dict] = []
    new_by_listing: dict = {}
    try:
        listings = _fetch_target_listings()
        if not listings:
            # H-E: 0 listings 週 1 reminder
            _maybe_remind_user_of_unused_w153(config)
            logger.info("[W153] no listings with rival_watch_enabled=1, skip")
            summary["success"] = True
            summary["message"] = "0 listings monitored (UI で監視 ON にしてください)"
            return summary

        # H-H: per-run cap
        cfg_block = (config.get('tasks_enabled') or {}).get('rival_detection') or {}
        max_listings = int(cfg_block.get('max_listings_per_run', 30))
        max_requests = int(cfg_block.get('max_requests_per_run', 150))
        if len(listings) > max_listings:
            logger.info(
                f"[W153] {len(listings)} listings > max_listings_per_run={max_listings}, "
                f"truncating"
            )
            listings = listings[:max_listings]
        requests_remaining = max_requests

        for lst in listings:
            eid = lst["ebay_item_id"]
            if requests_remaining <= 0:
                logger.warning(
                    f"[W153] max_requests_per_run={max_requests} exhausted, "
                    f"stopping at listings_processed={summary['listings_processed']}"
                )
                summary["message"] = "max_requests_per_run reached"
                break
            res = run_rival_per_listing_detection_one(
                eid, config,
                sleep_between=2.0,
                max_requests_remaining=requests_remaining,
            )
            per_listing_summaries.append(res)
            summary["listings_processed"] += 1
            summary["new_discoveries_total"] += res["new_discoveries"]
            summary["errors"] += res["errors"]
            summary["skipped_bad_item_id"] += res["skipped_bad_item_id"]
            summary["requests_used"] += res["requests_used"]
            requests_remaining -= res["requests_used"]
            if res["new_discoveries"] > 0:
                new_by_listing[eid] = {
                    "new": res["new_discoveries"],
                    "title": (lst["title"] or "")[:40],
                    "tail4": eid[-4:],
                }

        summary["sellers"] = []
        summary["new_sellers_count"] = summary["new_discoveries_total"]
        summary["total_scanned"] = summary["listings_processed"]
        summary["message"] = (
            f"listings={summary['listings_processed']} "
            f"new={summary['new_discoveries_total']} "
            f"err={summary['errors']} "
            f"bad_iid={summary['skipped_bad_item_id']} "
            f"reqs={summary['requests_used']}"
        )

        if new_by_listing:
            _send_discord_aggregate(config, new_by_listing)

        # H-D: errors>0 で 別 Discord alert + success=False
        if summary["errors"] > 0:
            _send_discord_errors_alert(config, summary, per_listing_summaries)
            summary["success"] = False
        else:
            summary["success"] = True
    except Exception as e:
        logger.exception("[W153] run_rival_detection failed")
        summary["message"] = f"top-level: {type(e).__name__}: {e}"
        summary["success"] = False
    return summary


def _send_discord_aggregate(config: dict, new_by_listing: dict) -> None:
    """new>0 集約通知 (1 run 1 message)."""
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        return
    from notifiers.discord_notifier import DiscordNotifier
    lines = [
        f"- **{v['title']}** ({v['tail4']}): {v['new']} 名"
        for v in new_by_listing.values()
    ]
    content = (
        f"🎯 **W153 新規ライバル検出** ({len(new_by_listing)} listings)\n"
        + "\n".join(lines[:20])
    )
    if len(lines) > 20:
        content += f"\n... 他 {len(lines) - 20} listings"
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        logger.warning(f"[W153] discord aggregate notify failed: {e}")


def _send_discord_errors_alert(
    config: dict, summary: dict, per_listing: list[dict]
) -> None:
    """H-D: errors>0 専用 alert."""
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        return
    from notifiers.discord_notifier import DiscordNotifier
    err_entries = [r for r in per_listing if r.get("errors", 0) > 0]
    excerpt = [
        f"- {r.get('ebay_item_id', '?')}: {r.get('message', '')[:100]}"
        for r in err_entries[:5]
    ]
    extra = len(err_entries) - 5
    content = (
        f"⚠️ **W153 errors 検出** "
        f"(listings={summary['listings_processed']}, errors={summary['errors']})\n"
        + "\n".join(excerpt)
        + (f"\n... 他 {extra} listings" if extra > 0 else "")
    )
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        logger.warning(f"[W153] discord errors-alert notify failed: {e}")


def _maybe_remind_user_of_unused_w153(config: dict) -> None:
    """H-E: 0 listings 週 1 reminder. claim_alert_dedupe(168h cap).

    ★ v2.1 MED-4 fix: webhook 存在確認を claim *前* に実施 (Codex 2 周目検出)。
    webhook 未設定で claim 消費 = reminder 永久失効を防ぐ。
    """
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        # webhook 未設定 = 何もできない、claim も消費しない (次回設定後に発火可能)
        return
    if not claim_alert_dedupe('w153_unused_weekly', dedupe_hours=168):
        return  # 既に週内に通知済
    from notifiers.discord_notifier import DiscordNotifier
    content = (
        "ℹ️ **W153 リマインダー**: 「ライバル監視 ON」の商品がありません。\n"
        "商品管理タブの hero 「🎯 ライバル監視 (W153)」section で ON にすると、"
        "W183 自動値下げの監視対象に新規 rival を流入させられます。"
    )
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        # 送信失敗 = claim は消費済なので来週まで再試行できない (1 週ロス admit、再試行 logic は別 W)
        logger.warning(f"[W153] discord reminder notify failed (1 week lost): {e}")


if __name__ == "__main__":
    from monitor.config_loader import load_config
    cfg = load_config()
    r = run_rival_detection(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
```

### 6.4 UI: `_render_rival_watch_section` (tabs/tab_product_management.py)

```python
import time as _time

_UI_COOLDOWN_SEC = 60.0  # H-H


def _render_rival_watch_section(p: dict, config: dict) -> None:
    """W153: 商品 hero 内「🎯 ライバル監視」section. form 外."""
    import streamlit as st
    from monitor.database import (
        set_rival_watch_enabled, set_rival_search_keywords,
        get_rival_discoveries, update_rival_discovery_status,
        add_or_reactivate_competitor,
    )
    from monitor.rival_keyword_generator import generate_keywords
    from tasks.task_rival_detection import run_rival_per_listing_detection_one

    eid = p["ebay_item_id"]
    # H-A 確定式: rival_watch_started_at 優先、None なら initial_at fallback
    since_base = p.get("rival_watch_started_at") or p.get("initial_registered_at")

    st.markdown(
        '<div class="pm-section-label">🎯 ライバル監視 (W153)</div>',
        unsafe_allow_html=True,
    )

    # ── ① 監視 ON checkbox ──
    cur_on = bool(p.get("rival_watch_enabled"))
    new_on = st.checkbox(
        "ライバル監視 ON (cron 巡回対象に含める)",
        value=cur_on, key=f"pm_rival_on_{eid}",
    )
    if new_on != cur_on:
        set_rival_watch_enabled(eid, new_on)

    if not new_on:
        st.caption("OFF: このセクションでの編集・検索は無効")
        return

    # ── ② textarea + ③ 生成 + ④ 保存 + ⑤ 今すぐ検索 ──
    # M-internal-1: session_state 上書きは *_pending key 経由
    pending_key = f"pm_rival_kw_{eid}_pending"
    if pending_key in st.session_state:
        initial_kw = st.session_state.pop(pending_key)
    else:
        initial_kw = p.get("rival_search_keywords") or ""

    gen_at = p.get("rival_search_keywords_generated_at") or "未生成"
    st.caption(f"検索ワード (改行区切り、最終生成: {gen_at} UTC)")

    if not since_base:
        st.caption("⚠️ W151 初期登録未完了 = since filter は rival_watch_started_at のみで動作")

    new_kw = st.text_area(
        "検索ワード",
        value=initial_kw, height=100,
        key=f"pm_rival_kw_{eid}",
        label_visibility="collapsed",
        help="1 行 1 keyword. brand/model 相当語を含めると精度向上. 例: 'Ohuhu PEN 320 FINE'",
    )

    btn_cols = st.columns([1, 1, 1])
    with btn_cols[0]:
        if st.button("🤖 Claude 生成", key=f"pm_rival_gen_{eid}",
                     help="Claude Haiku で 3-5 候補生成"):
            with st.spinner("Haiku 生成中..."):
                try:
                    cands = generate_keywords(title=p.get("title") or "")
                    joined = "\n".join(cands)
                    set_rival_search_keywords(eid, joined, mark_generated=True)
                    st.session_state[pending_key] = joined  # M-internal-1
                    st.success(f"生成 {len(cands)} 件 → DB 保存")
                    st.rerun()
                except ValueError as e:
                    st.error(f"Haiku 出力異常: {e}. 手動入力してください")
                except RuntimeError as e:
                    st.error(f"API key 未設定: {e}")
                except Exception as e:
                    st.error(f"生成失敗: {type(e).__name__}: {e}")
    with btn_cols[1]:
        if st.button("💾 検索ワード保存", key=f"pm_rival_save_{eid}",
                     help="textarea 内容を DB 保存"):
            ok = set_rival_search_keywords(eid, new_kw, mark_generated=False)
            st.success("保存完了") if ok else st.error("保存失敗 (listing 不在?)")

    with btn_cols[2]:
        # H-H: UI cooldown 60s
        cooldown_key = f"pm_rival_search_at_{eid}"
        last_at = st.session_state.get(cooldown_key, 0.0)
        now_s = _time.monotonic()
        on_cooldown = (now_s - last_at) < _UI_COOLDOWN_SEC
        if st.button("🔍 今すぐ検索",
                     key=f"pm_rival_search_btn_{eid}",
                     type="primary",
                     disabled=on_cooldown,
                     help="この listing を今すぐ Browse API 巡回 (60 秒 cooldown)"):
            kws = [line.strip() for line in new_kw.split("\n") if line.strip()]
            if not kws:
                st.warning("検索ワードが空です")
            else:
                st.session_state[cooldown_key] = now_s
                with st.spinner("Browse API 巡回中..."):
                    res = run_rival_per_listing_detection_one(
                        eid, config,
                        keywords_override=kws,
                        sleep_between=0.0,  # M-internal-7
                    )
                if res["success"]:
                    st.success(
                        f"新規 {res['new_discoveries']} / 既知更新 {res['refreshed']} / "
                        f"err {res['errors']} / bad_iid {res['skipped_bad_item_id']}"
                    )
                else:
                    st.error(f"失敗: {res['message']}")
                st.rerun()
        if on_cooldown:
            remaining = int(_UI_COOLDOWN_SEC - (now_s - last_at))
            st.caption(f"cooldown {remaining}s")

    # ── ⑥ 検出済 expander (status 3 tab + 件数バッジ) ──
    # L-internal-4
    new_count_for_label = len(get_rival_discoveries(eid, status='new', since=since_base))
    with st.expander(f"📋 検出済 rival 一覧 ({new_count_for_label} 新規)", expanded=False):
        tab_new, tab_added, tab_dismissed = st.tabs(
            ["🆕 新規 (未対応)", "✅ 監視追加済", "🗑️ 却下"],
        )
        with tab_new:
            discoveries = get_rival_discoveries(eid, status='new', since=since_base)
            if not discoveries:
                st.caption(
                    "新規 rival なし"
                    + (f" (since {since_base} UTC)" if since_base else "")
                )
            else:
                for d in discoveries[:50]:
                    cols = st.columns([3, 1, 1, 1])
                    with cols[0]:
                        st.markdown(
                            f"**{d['competitor_seller']}** | "
                            f"[{(d['competitor_title'] or '')[:60]}]"
                            f"(https://www.ebay.com/itm/{d['competitor_item_id']})"
                            + (f" | ${d['competitor_price_usd']:.2f}"
                               if d['competitor_price_usd'] else "")
                        )
                        st.caption(
                            f"first_seen: {d['first_seen_at']} UTC | "
                            f"keyword: {d['search_keyword']}"
                        )
                    with cols[1]:
                        if st.button("➕ 監視追加",
                                     key=f"pm_rdisc_add_{d['id']}",
                                     type="primary"):
                            try:
                                _id, action = add_or_reactivate_competitor(
                                    our_item_id=eid,
                                    our_sku=p.get('sku', '') or '',  # M-internal-2
                                    competitor_seller=d['competitor_seller'],
                                    competitor_item_id=d['competitor_item_id'],
                                )
                                # ★ v2.1 HIGH-2 fix: conflict 時は status を遷移させない
                                # (W183 流入されていないのに「追加済み」分類されると user の目から
                                # 永久消失する silent gap、Codex 2 周目で検出)
                                if action == 'added':
                                    update_rival_discovery_status(d['id'], 'monitoring_added')
                                    st.success("W183 監視対象に追加")
                                elif action == 'reactivated':
                                    update_rival_discovery_status(d['id'], 'monitoring_added')
                                    st.success("過去解除されていた → 再アクティブ化")
                                else:  # conflict
                                    # status='new' 維持 (W183 流入不可なので user が再判断できるよう残す)
                                    # M-codex-8
                                    st.warning(
                                        "他 listing で監視中のため、本 listing には追加できませんでした"
                                        " (N:1 監視は別 W で対応予定)。"
                                        "本 discovery は new tab に残ります"
                                    )
                                st.rerun()
                            except Exception as e:
                                st.error(
                                    f"追加失敗: {type(e).__name__}: {e}. "
                                    f"DB 状態確認をお願いします"
                                )
                    with cols[2]:
                        if st.button("🗑️ 却下", key=f"pm_rdisc_dis_{d['id']}"):
                            update_rival_discovery_status(d['id'], 'dismissed')
                            st.rerun()
        with tab_added:
            added = get_rival_discoveries(eid, status='monitoring_added')
            # L-internal-2: 「監視解除」button は本 W scope 外として明示
            st.caption("(監視解除 button は本 W scope 外 / UX 一貫性は別 W で議論)")
            if not added:
                st.caption("監視追加済の rival なし")
            else:
                import pandas as pd
                st.dataframe(pd.DataFrame([
                    {"seller": d["competitor_seller"],
                     "item_id": d["competitor_item_id"],
                     "title": (d["competitor_title"] or "")[:60],
                     "first_seen": d["first_seen_at"],
                     "status_at": d["status_changed_at"]}
                    for d in added[:30]
                ]), hide_index=True, use_container_width=True)
        with tab_dismissed:
            dis = get_rival_discoveries(eid, status='dismissed')
            if not dis:
                st.caption("却下した rival なし")
            else:
                for d in dis[:30]:
                    cols = st.columns([4, 1])
                    with cols[0]:
                        st.caption(
                            f"{d['competitor_seller']} | "
                            f"{(d['competitor_title'] or '')[:60]} | "
                            f"dismissed at {d['status_changed_at']}"
                        )
                    with cols[1]:
                        if st.button("↩️ 差戻", key=f"pm_rdisc_undo_{d['id']}"):
                            update_rival_discovery_status(d['id'], 'new')
                            st.rerun()

    st.markdown("---")
```

---

## 7. データフロー (シーケンス)

```
Phase A: user 初期セットアップ
    1. ☑ 監視 ON          → set_rival_watch_enabled(eid, True)
                              └─ rival_watch_enabled = 1
                              └─ rival_watch_started_at = NOW() (H-A anchor)
    2. 🤖 Claude 生成     → generate_keywords(title=only) (Haiku 4.5)
                              └─ output filter (apology/numbering/word-count)
                              └─ <3 valid なら ValueError
                              set_rival_search_keywords(eid, joined, mark_generated=True)
    3. (textarea 編集)
    4. 💾 検索ワード保存   → set_rival_search_keywords(eid, edited, mark_generated=False)

Phase B: cron 巡回 (初期は朝 02 JST のみ)
    run_task(task_key='rival_detection') → run_rival_detection(config)
        ├─ _fetch_target_listings()
        │     └─ 0 件なら _maybe_remind_user_of_unused_w153 (週 1 cap)
        ├─ max_listings_per_run=30 / max_requests_per_run=150 cap
        └─ for each listing (max 30):
              run_rival_per_listing_detection_one(eid, sleep_between=2.0)
                    ├─ 空 keyword → errors++ で return (Q0)
                    └─ for each keyword:
                          429 backoff retry 3 回 (1s, 2s, 4s)
                          BrowseAPI.search_items
                          for each item: record_rival_discovery (INSERT OR IGNORE)
                            ├─ competitor_iid 不在: WARNING + skipped_bad_item_id++ (H-G)
        ├─ if new_by_listing: _send_discord_aggregate
        └─ if errors > 0: _send_discord_errors_alert + success=False (H-D)

Phase C: user 採否判断 (UI expander)
    since_base = rival_watch_started_at or initial_registered_at (H-A)
    discoveries = get_rival_discoveries(eid, status='new', since=since_base)
    ➕ 監視追加 → add_or_reactivate_competitor(...)  (H-C)
                  ├─ 'added'        → 「W183 監視対象に追加」
                  ├─ 'reactivated'  → 「過去解除 → 再アクティブ化」
                  └─ 'conflict'     → 「他 listing で監視中、N:1 は別 W」
                  update_rival_discovery_status(disc_id, 'monitoring_added')
    🗑️ 却下     → update_rival_discovery_status(disc_id, 'dismissed')

Phase D: W183 自動値下げに流入 (既存)
    SELECT DISTINCT our_item_id FROM competitor_products WHERE is_active=1
```

---

## 8. ビルドシーケンス

| step | 内容 | DoD |
|---|---|---|
| 1 | DB migration v50 (drift recovery は schema_ver 独立) | `init_db()` 2 回 + 既存データ非破壊 + PRAGMA user_version=50 + 4 列実在 + drift simulation PASS |
| 2 | DB helpers (set_rival_watch_enabled / set_rival_search_keywords / record_rival_discovery / get_rival_discoveries / update_rival_discovery_status / **add_or_reactivate_competitor**) | pytest 全 PASS |
| 3 | rival_keyword_generator.py (title-only, apology filter, EBAY_ANTHROPIC_KEY 優先) | pytest mock + apology reject + <3 valid raise |
| 4 | task_rival_detection.py 全面書換 (errors→success=False, 429 backoff, max_*_per_run, bad_iid counter, weekly reminder) | pytest 全 R1-R15 PASS |
| 5 | schedule_config.json (execution_times=[2] + max_*_per_run) + TASK_SCHEDULE display 改訂 | scheduler.log に「W153 商品別ライバル検出 (02:00)」表示 |
| 6 | UI section _render_rival_watch_section (cooldown 60s + pending key + 件数バッジ + action 別 message) | Streamlit + Playwright Console 0 errors |
| 7 | E2E 実機 (added / reactivated / conflict 全テスト) | DB SELECT + Playwright snapshot |
| 8 | 本番 cron 投入 (02:00 batch slot で動作確認) | scheduler.log listings_processed > 0 |
| 9 | code-reviewer Opus 4.7 (HIGH=0 まで loop) | HIGH=0 |
| 10 | Codex GPT-5.5 2 周目 (本 v2 設計が HIGH=0) | HIGH=0 |
| 11 | Q1 DoD 11-step | 完走 |
| 12 | commit + push + ROADMAP id=236 完了 marker | done |

依存: 1 → 2 → 3 ⊥ 4 (parallel) → 5 → 6 → 7-12 直列

---

## 9. テスト計画

`tests/test_w153_rival_per_listing_2026_05_22.py` (単一 file)、6 セクション + v2 で 10 test 追加:

### Section 1: migration v50 冪等性 + drift recovery (H-B)

```python
def test_v50_idempotent_init_db_twice_retains_data(tmp_db): ...
def test_v50_self_heal_when_table_missing(tmp_db): ...
# H-B fix: drift recovery を schema_ver 独立に
def test_v50_drift_recovery_at_version_50(tmp_db):
    """user_version=50 だが列・table 欠損 → init_db で再 ALTER/CREATE."""
```

### Section 2: DB helpers (H-A / H-C 含む)

```python
def test_set_rival_watch_enabled_on_sets_started_at(tmp_db):
    """ON 時に rival_watch_started_at = NOW() set (H-A)."""
def test_set_rival_watch_enabled_off_preserves_started_at(tmp_db):
    """OFF 時に rival_watch_started_at 維持 (NULL に戻さない)."""
def test_set_rival_watch_enabled_returns_false_on_missing(tmp_db): ...
def test_set_rival_search_keywords_normalizes_blank_lines(tmp_db): ...
def test_set_rival_search_keywords_mark_generated_sets_timestamp(tmp_db): ...
def test_record_rival_discovery_new_returns_id(tmp_db): ...
def test_record_rival_discovery_existing_returns_none_and_updates_last_seen(tmp_db): ...
def test_get_rival_discoveries_since_filter(tmp_db): ...
def test_update_rival_discovery_status_validates_value(tmp_db): ...
# H-C: add_or_reactivate_competitor
def test_add_or_reactivate_added_returns_added_action(tmp_db): ...
def test_add_or_reactivate_same_listing_reactivates(tmp_db): ...
def test_add_or_reactivate_other_listing_returns_conflict(tmp_db): ...
# H-A: late initial_registration test
def test_late_initial_registration_does_not_hide_discoveries(tmp_db):
    """anchor = rival_watch_started_at で固定、late initial_at が future にずらさない."""
```

### Section 3: rival_keyword_generator (H-F 含む)

```python
@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_returns_3_to_5(mock_client): ...
def test_generate_keywords_filters_too_long_lines(mock_client): ...
def test_generate_keywords_raises_on_empty_output(mock_client): ...
def test_generate_keywords_raises_on_lt_3_valid_candidates(mock_client):
    """<3 valid で raise (H-F)."""
def test_generate_keywords_uses_ebay_anthropic_key_first(monkeypatch):
    """EBAY_ANTHROPIC_KEY 優先, ANTHROPIC_API_KEY fallback (H-F)."""
def test_generate_keywords_raises_on_missing_api_key(monkeypatch): ...
def test_haiku_apology_filter(mock_client):
    """'I cannot answer' を含む output → ValueError raise."""
```

### Section 4: detection task (H-D / H-E / H-G / H-H)

```python
@patch("tasks.task_rival_detection.BrowseAPIClient")
def test_run_one_inserts_discoveries(mock_browse, tmp_db): ...
def test_run_one_dedupes_via_unique_constraint(mock_browse, tmp_db): ...
def test_run_one_skips_own_seller(mock_browse, tmp_db): ...
# H-D: 空 keyword で errors++ + success=False
def test_run_one_keyword_empty_increments_errors(mock_browse, tmp_db): ...
def test_errors_gt_zero_sets_success_false(mock_browse, tmp_db): ...
def test_run_rival_detection_zero_listings_returns_success_with_message(tmp_db): ...
# H-E: 0 listings 週 1 reminder
@patch("tasks.task_rival_detection.claim_alert_dedupe")
@patch("tasks.task_rival_detection.DiscordNotifier")
def test_zero_listings_weekly_reminder(mock_notifier, mock_dedupe, tmp_db): ...
# H-G: bad item_id counter
def test_skipped_bad_item_id_counter(mock_browse, tmp_db, caplog): ...
# H-H: max_requests_per_run early break
def test_max_requests_per_run_early_break(mock_browse, tmp_db): ...
# H-H: 429 backoff
def test_429_triggers_exponential_backoff(mock_browse, tmp_db, monkeypatch): ...
# Codex #11 / H-I-4: cron-UI race
def test_cron_ui_concurrent_record_safe(mock_browse, tmp_db): ...
```

### Section 5: UI rendering

```python
def test_render_rival_watch_section_appears_in_hero(streamlit_app_test, tmp_db): ...
def test_render_rival_off_shows_early_return_caption(streamlit_app_test, tmp_db): ...
# M-internal-1: pending key
def test_pending_key_overrides_textarea_default(streamlit_app_test, tmp_db): ...
# H-C UI: action 別 message
def test_render_added_message_on_new_competitor(streamlit_app_test, tmp_db): ...
def test_render_reactivated_message_on_existing_inactive(streamlit_app_test, tmp_db): ...
def test_render_conflict_warning_on_other_listing(streamlit_app_test, tmp_db): ...
# M-codex-8: IntegrityError user-facing
def test_integrity_error_user_facing_message(streamlit_app_test, tmp_db): ...
# H-H: UI cooldown 60s
def test_ui_cooldown_blocks_rapid_clicks(streamlit_app_test, tmp_db): ...
# L-internal-4: 件数バッジ
def test_expander_label_shows_new_count(streamlit_app_test, tmp_db): ...
```

期待件数: 約 35 tests total, ~400 LOC。

---

## 10. Money-direct silent gap リスク (v2 reframe)

| # | リスク | 影響 | 予防策 (v2) |
|---|---|---|---|
| **R1** | keyword 空のまま rival_watch_enabled=1 (H-D) | success=True 偽装 | `summary["errors"]+=1` + `summary["success"]=False` |
| **R2** | Browse API 429/5xx で偽装成功 (H-D + H-H) | failed なのに「0 件 new」 | 429/5xx backoff 3 回 + 失敗時 errors++ + 別 message Discord alert |
| **R3** | add_competitor_product UNIQUE 違反 silent skip (H-C) | UI IntegrityError キャッチ漏れ | `add_or_reactivate_competitor` で 3 action 明示 + UI 別 message |
| **R4** | initial_registered_at が NULL (W151 未完了) | since filter 効かず | anchor を **rival_watch_started_at** に統一 (H-A), initial_at は fallback |
| **R5** | rival_watch_enabled=1 listing が 0 件継続 (H-E) | 「動いてるが何もしてない」 | `_maybe_remind_user_of_unused_w153` で週 1 Discord reminder |
| **R6** | Haiku 異常 output (H-F) | textarea に「I cannot answer」混入 | apology pattern reject + numbering reject + 3≤words≤6 + <3 valid raise |
| **R7** | 複数 cron run の INSERT race | UNIQUE 違反 | INSERT OR IGNORE + rowcount 判定 = race 安全 |
| **R8** | competitor_products UNIQUE 違反 (N:1) (H-C) | 別 listing 既登録で永久流入不能 | `action='conflict'` 明示 + UI warning + schema 緩和は別 W |
| **R9** | Haiku API 障害 | UI 🤖 button 永久失敗 | `generate_keywords` が raise → UI で `st.error` |
| **R10** | legacy new_competitor_alerts 枯れ | 既存 UI 空表示 | touch しない、新 data は W153 section で表示 |
| **R11** | Browse API item_id 形式異常 (H-G) | 空 competitor_iid で INSERT 失敗 | `logger.warning + summary["skipped_bad_item_id"]+=1 + continue` |
| **R12** | execution_times 過剰 (H-H) | 5 batch/day で過負荷 | 初期 `[2]` + max_listings_per_run=30 + max_requests_per_run=150 + 観察 |
| **R13 (新)** | late initial_registration silent gap (H-A) | initial_at が後で set → prior discoveries 消失 | anchor を `rival_watch_started_at` に固定 |
| **R14 (新)** | drift after user_version=50 (H-B) | 列・table 欠損永久放置 | drift recovery を schema_ver 独立で毎回 check |
| **R15 (新)** | UI 連打 (H-H) | 「今すぐ検索」連打で quota 浪費 | cooldown 60s + disabled button + caption |

**Q0 痕跡 3 経路**:

| 経路 | 内容 |
|---|---|
| DB log | task_execution_log + listing_rival_discoveries + ebay_listings.rival_search_keywords_generated_at + rival_watch_started_at |
| Discord | (a) 集約 new>0 (b) errors>0 別 alert (c) 0 listings 週 1 reminder |
| UI | 監視 ON/OFF / keyword 生成時刻 / 件数バッジ / status tab / cooldown caption / action 別 message |

---

## 11. cascade scan 対象 (L-internal-1 fix)

| ファイル | 要否 | 内容 |
|---|---|---|
| `tools/ebay-manager/data/system_improvements.json` | **touching** | W153 (id=236) status 遷移 + completed日 + progress_note |
| `tools/ebay-manager/tasks/task_rival_detection.py` | **touching** | docstring に「v1: グローバル known set 廃止 / 旧 data/known_rival_sellers.json は legacy 残置」明示 |
| `tools/ebay-manager/USER_MANUAL.md` | **touching** | 「ライバル監視 (W153)」section 追加 |
| `tests/test_w138a_migration_2026_05_17.py` | **touching** | `assert ver == 49` → `>= 50` 5 箇所 pin 更新 |
| `tests/test_w68_step1_init_db_drift.py` | **touching** | `assert ver == 49` → `>= 50` 5 箇所 pin 更新 |
| **`tests/test_w148_keyword_watch_2026_05_21.py:83`** | **touching** ★ v2 追加 | `assert ver == 46` → `>= 50` 形式に更新 |
| **`tests/test_w149_fulfillment_order_link_2026_05_22.py:129`** | **touching** ★ v2 追加 | `assert ver == 48` → `>= 50` |
| **`tests/test_w151_initial_registered_2026_05_22.py:81`** | **touching** ★ v2 追加 | `assert ver == 49` → `>= 50` |
| `monitor/task_execution_log.py` | **touching** | TASK_SCHEDULE display + hours 更新 ([2]) |
| memory `MEMORY.md` | **touching ★ 同 session 内** (L-internal-1) | `session_2026_05_22_w153_*.md` を完了後ではなく同 session 内で追加 |
| memory `feedback_no_silent_skip_no_fake_success.md` | **不要** | R1-R15 で痕跡確保、新規違反パターンなし |

**前 W との連動**:
- W148 (v46) → W149 (v47/v48) → W150-152 (v48/v49 消費) → **本 W = v50**
- W151 `initial_registered_at` は本 W で **fallback 用** (anchor は rival_watch_started_at 優先)
- W183 (2026-05-10 実装済) は本 W の流出先

---

## 12. 未確定事項

| # | 不確定事項 | 暫定対応 |
|---|---|---|
| Q1 | ebay_listings に Brand / MPN 列なし | title のみで Haiku 生成 (M-codex-10)、列追加は別 W |
| Q2 | Haiku 4.5 model id 安定性 | `claude-haiku-4-5-20251001` (実績あり)、改訂時は別 W |
| Q3 | Browse API limit=50 で十分か | 既存と同設定、Discord 集約で観察、必要なら別 W |
| Q4 | N:1 監視 (UNIQUE 緩和) | `action='conflict'` で明示、schema 緩和は別 W (v51 候補) |
| Q5 | UI cooldown 60s で十分か | UI 連打防止には十分 |
| Q6 | dismissed 古い row の cleanup | 無制限 (K1)、問題化したら別 W で 90 日 cleanup |
| Q7 | monitoring_added 解除パス | 本 W では「監視解除」button なし (L-internal-2) |
| Q8 | execution_times | 初期 [2]、観察後 user 判断で拡大 |
| Q9 | Haiku cost monitoring | per-click ~$0.0005、W94 cost monitor で吸収 |
| Q10 | run_one の同時実行 race | INSERT OR IGNORE で安全 (test verify) |
| **Q11 (新)** | anchor 計算式 | `since_base = rival_watch_started_at or initial_registered_at` (started_at 優先、None fallback) |
| **Q12 (新)** | Browse API quota 共有 monitoring | 本 W は max_requests_per_run でローカル保護、共有監視は別 W |

---

## 13. 完了判定基準 (Q1 DoD 11 ステップ)

| # | ステップ | 確認手段 | 期待結果 |
|---|---|---|---|
| 1 | DB migration v50 適用 | `PRAGMA user_version` | `(50,)` |
| 2 | migration 冪等性 + drift recovery | init_db() 2 回 + drift simulation | データ消失なし + 再修復 PASS |
| 3 | pytest 全件 | `pytest tests/` | 全 PASS, 新 test 約 35 件 |
| 4 | scheduler 再起動 | logs/scheduler.log grep | 「W153 商品別ライバル検出 (02:00)」表示 |
| 5 | get_jobs() | rival_detection が新ロジック呼出 | OK |
| 6 | task_execution_log | DB SELECT | completed + summary message |
| 7 | E2E 実機 (3 シナリオ) | UI: added/reactivated/conflict 全テスト | action 別 message |
| 8 | Discord 実視認 (R-11) | user 目視 | 3 系統 (集約 / errors / weekly) 確認 |
| 9 | dedupe 動作 | 同 listing で 2 回連続検索 | rowcount 不変 + last_seen_at 更新 |
| 10 | anchor filter 動作 | rival_watch_started_at base 検証 | late initial_at が anchor をずらさない |
| 11 | 2 段 review HIGH=0 | code-reviewer Opus 4.7 + Codex GPT-5.5 | HIGH=0 |

---

## 14. Plan → Verify → Persist → Automate

| phase | 実体 |
|---|---|
| Plan | 本設計書 (W153 v2) |
| Verify | §13 DoD 11-step |
| Persist | DB v50 + system_improvements.json + commit & push |
| Automate | 既存 daily_scheduler cron + 0 listings 週 1 reminder |

---

## 15. 環境特異性チェックリスト

| 項目 | 対応 |
|---|---|
| pythonw.exe (sys.stdout None) | `task_rival_detection.py` 冒頭ガード |
| Streamlit hot reload | UI section 追加 = 手動再起動 必要 |
| Streamlit session_state widget 上書き | `*_pending` key 経由 (M-internal-1) |
| Windows cp932 | scheduler `utf8_console` 既存 |
| OAuth token cache | Browse は AppToken (既存)、Anthropic は env var (EBAY_ANTHROPIC_KEY 優先) |
| SQLite TIMESTAMP UTC | 全 timestamp 列 UTC (CURRENT_TIMESTAMP) |
| DB lock (WAL) | INSERT OR IGNORE は WAL で安全 |
| APScheduler thread-local batch_ctx | `run_task(task_key='rival_detection')` 経由 = 自動的に thread-local (M-codex-9 訂正) |
| SQLite version | `sqlite3.sqlite_version` log (M-internal-4) |

---

## 16. コスト保護 + Browse API quota 保護 (H-H)

### 16.1 Claude Haiku 4.5

- cost: ~$0.0005 / click (input 200 tok + output 100 tok)
- 587 listings 全件でも ~$0.30
- cron 内では keyword 未生成 listing を自動補完しない (= UI ボタン経路に限定)

### 16.2 eBay Browse API

- 5K calls/day cap
- 試算 (v2): 30 listings × 5 keywords × 1 cron/day = 150 calls/day

**保護機構 4 段**:

| 機構 | 設定 | 効果 |
|---|---|---|
| execution_times = [2] | schedule_config.json | cron 1/day |
| max_listings_per_run = 30 | early break | listing 100+ でも 30 で stop |
| max_requests_per_run = 150 | early break | request 数 cap |
| 429 backoff | 1s/2s/4s 3 回 retry | rate limit hit でも retry |
| UI cooldown 60s | session_state | 連打防止 (★ v2.1 MED-5 admit: 単一 Streamlit セッション内のみ有効、別タブ/別プロセスでは共有されない。本 W は max_requests_per_run + 429 backoff が backend cap として効くので OK。複数タブからの quota 暴走を厳格に防ぐ backend cooldown は別 W) |

**増加時の判断フロー**:
- 100+ listing → max_listings_per_run を 50/100 へ (別 W)
- 300+ → execution_times を [2, 14] へ (別 W)
- 500+ → quota 共有 monitoring (Q12 別 W)

---

## 17. 観測可能性 3 経路

| 経路 | 内容 |
|---|---|
| DB log | task_execution_log + listing_rival_discoveries + ebay_listings.rival_search_keywords_generated_at + rival_watch_started_at |
| Discord | (a) 集約 new>0 (b) errors>0 別 alert (c) 0 listings 週 1 reminder |
| UI | 監視 ON/OFF / keyword 生成時刻 / 件数バッジ / status tab / cooldown caption / action 別 message |

---

## 18. ROADMAP entry (id=236)

```json
{
    "id": 236,
    "tag": "W153",
    "title": "新規ライバル発見の根本作り直し: 商品毎 rival 検出 + 検索ワード助言 + UI 編集",
    "status": "進行中",
    "progress_note": "2026-05-22 設計 v2: 2 段 review で HIGH 9 / MED 12 / LOW 5 解消. DB v50 (4 列 + listing_rival_discoveries + 3 index, drift recovery schema_ver 独立) + 新 module rival_keyword_generator (Haiku 4.5, title-only, apology filter, <3 valid raise) + task_rival_detection 全面書換 (354→280 LOC, errors>0 で success=False, 429 backoff, max_listings/requests cap, weekly reminder, bad_iid counter) + UI section _render_rival_watch_section (~220 LOC, pending key, cooldown 60s, action 別 message, 件数バッジ) + add_or_reactivate_competitor helper. anchor は rival_watch_started_at (initial_registered_at は fallback)."
}
```

---

## 19. v2 自己 review 観点

| # | 観点 | 評価 |
|---|---|---|
| 1 | K1 Simplicity | add_or_reactivate_competitor は HIGH-C 修正で必須、speculative 抽象なし |
| 2 | K2 Surgical | task_rival_detection 全面書換だが署名維持、daily_scheduler 変更なし |
| 3 | K3 Goal-Driven | §13 DoD 11-step + §17 観測 3 経路、test 35 件で測定可 |
| 4 | Q0 silent-skip | R1-R15 全列挙、errors>0 で success=False で構造排除 |
| 5 | Q2 migration 冪等 + drift recovery | v50 ブロック自己修復、drift recovery は schema_ver 独立 |
| 6 | sku-rules | listing 識別は ebay_item_id、SKU は補助情報のみ |
| 7 | money-direct リスク | §10 で 15 件列挙 |
| 8 | cascade scan | §11 で test_w148/w149/w151 pin 追加 + MEMORY.md 同 session 更新 |
| 9 | 2 段 review 反映 | HIGH 9 / MED 12 / LOW 5 全解消 (§20 改訂履歴) |

---

## 20. 改訂履歴 (v1 → v2 → v2.1)

### v2 → v2.1 (Codex GPT-5.5 2 周目 review 反映、2026-05-22 同日)

Codex 2 周目で新たに HIGH 3 / MED 3 / LOW 1 検出 (前回 HIGH 9 のうち H-B / H-D / H-F / H-G / H-I は完全解消確認、H-A / H-C / H-H の派生で 3 新規)。本セッション内 pinpoint Edit で全件解消:

| ID | 内容 | v2.1 修正 (該当 §) |
|---|---|---|
| **v2.1 HIGH-1** (派生 H-A) | OFF→ON 再開で `COALESCE(rival_watch_started_at, NOW)` が旧 epoch を維持、user の「監視リセット」意図と一致しない可能性 | §6.2 `set_rival_watch_enabled` docstring に **設計判断 admit**: 「履歴連続性」優先 default、「リセット」UI button は別 W (本 W K1 scope 外)。silent gap ではなく明示判断 |
| **v2.1 HIGH-2** (派生 H-C) | `action='conflict'` で W183 流入せずに UI が `monitoring_added` に status 遷移 → 永久消失 silent gap | §6.4 UI button で `if action in ('added', 'reactivated'): update_rival_discovery_status(...)` に分岐、`conflict` は `'new'` 維持 + warning 文に「new tab に残ります」明示 |
| **v2.1 HIGH-3** (派生 H-H) | `requests_used` / `max_requests_remaining` が success 後のみ decrement、429/5xx retry で実 quota 超過リスク | §6.3 try ブロック **前** に counter 消費 + コメントで Codex 検出経緯を記録 |
| **v2.1 MED-4** | `claim_alert_dedupe` を webhook 確認/送信前に claim、webhook 未設定で reminder 永久失効 | §6.3 `_maybe_remind_user_of_unused_w153` で webhook 確認後 claim、送信失敗時は WARNING + 1 週ロス admit |
| **v2.1 MED-5** | UI cooldown session_state ベース = 別タブ/別プロセス共有不可、quota guard 弱い | §15 で **admit + backend cooldown は別 W**。本 W は max_requests_per_run + 429 backoff が backend cap として効くので OK |
| **v2.1 MED-6** | `add_or_reactivate_competitor` reactivation で `our_sku` 更新されず stale | §6.2 で `UPDATE ... SET our_sku = COALESCE(NULLIF(?, ''), our_sku), updated_at = CURRENT_TIMESTAMP` に修正 |
| **v2.1 LOW-7** | `schedule_config.json` の具体 JSON diff が不在、実装漏れリスク | §4.1 で具体 diff snippet 追加 (description / execution_times / max_listings_per_run / max_requests_per_run / note) |

**集計**: v2.1 HIGH 3 + MED 3 + LOW 1 = 7 件全解消。v2 確認済解消は v2 §20 を参照。
**累計**: v1 → v2 → v2.1 で HIGH 12 / MED 15 / LOW 6 = 計 33 件解消。

---

## (旧) v1 → v2

### HIGH 9 件 解消

| ID | 内容 | v2 修正 |
|---|---|---|
| **H-A** | late initial_registration が prior discoveries を since filter で消す silent gap | `rival_watch_started_at` 列追加 (4 列目)、anchor を `rival_watch_started_at` 優先 (initial_at fallback) に確定。`set_rival_watch_enabled(ON)` で `COALESCE(既存, NOW())` |
| **H-B** | v50 自己修復が schema_ver<50 内のみ | drift recovery を schema_ver と独立に毎回 check |
| **H-C** | 競合 reactivation pattern 欠落 | `add_or_reactivate_competitor` 新規 helper (3 action: added/reactivated/conflict) |
| **H-D** | 空 keyword / Browse 429-5xx で success=True 偽装 + errors 専用 Discord 未実装 | errors+=1 + success=False + `_send_discord_errors_alert` |
| **H-E** | 0 listings 永続 silent skip | `_maybe_remind_user_of_unused_w153` + claim_alert_dedupe (week cap) |
| **H-F** | Haiku 異常 output filter 不足 | apology pattern reject + numbering reject + 3≤words≤6 + <3 valid で raise |
| **H-G** | competitor_item_id 不在で silent gap | WARNING + `skipped_bad_item_id` counter |
| **H-H** | Browse API quota 保護未実装 | execution_times=[2] + max_listings_per_run + max_requests_per_run + 429 backoff + UI cooldown 60s |
| **H-I** | テスト不足 5 件 | §9 に 10 test 追加 |

### MED 12 件 解消

| ID | v2 修正 |
|---|---|
| M-internal-1 | `*_pending` key 経由 |
| M-internal-2 | `add_or_reactivate_competitor(our_sku=...)` 補助情報引数 |
| M-internal-3 | specific exception tuple `(httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError)` |
| M-internal-4 | `sqlite3.sqlite_version` log |
| M-internal-7 | sleep_between arg (UI=0.0, cron=2.0) |
| M-internal-8 | index existence check |
| M-codex-7 | `add_or_reactivate_competitor` として明示 |
| M-codex-8 | IntegrityError 専用 + user-facing action 別 message |
| M-codex-9 | 「`run_task(task_key='rival_detection')` 経由」訂正 |
| M-codex-10 | 「title から抽出可能な brand/model 相当語を含める」緩和 |

### LOW 5 件 解消 (cascade pin 漏れ含む)

| ID | v2 修正 |
|---|---|
| L-internal-1 | MEMORY.md 更新を「同 session 内」に |
| L-internal-2 | 「監視解除」button は本 W scope 外として明示 admit |
| L-internal-3 | Haiku prompt template に「Japanese title は brand を英字保持」追記 |
| L-internal-4 | UI expander タイトルに件数バッジ |
| **cascade pin 漏れ** | §11 表に test_w148/w149/w151 の `assert ver` 更新を追加 |

---

## 21. 次のステップ

1. 本設計書を保存 (parent agent が Write 完了)
2. **Codex GPT-5.5 2 周目 review** に投入 (本 v2 が HIGH=0 達成か verify)
3. HIGH=0 なら ROADMAP id=236 status="進行中" 更新 (実装着手 marker)
4. DB v50 migration から実装着手 (§8 ビルドシーケンス step 1)
5. Q1 DoD 11-step で本番 verify
6. commit + push + ROADMAP id=236 status="完了"

---

# 設計書ここまで (W153 v2, 2026-05-22)

**使用モデル**: Opus 4.7 (code-architect 等価運用、v2 改訂)
**v1 → v2 review**: 内部 code-reviewer Opus 4.7 + Codex GPT-5.5 (2 段)
**解消 finding**: HIGH 9 件 + MED 12 件 + LOW 5 件 (cascade pin 漏れ含む)
**設計フェーズ完了**: Q3 構造化フロー Phase 5
**次フェーズ**: Codex 2 周目 review (HIGH=0 verify) → Phase 6 実装着手
