---
name: w148-alertcrawler-keyword-watch-design
description: AlertCrawler 移植 — メルカリ/ヤフオク キーワード新着監視 (Discord 通知 + 選別移行) の設計書 v2 (Codex 2段指摘反映)
layer: wiki
updated: 2026-05-20
revision: v2
sources:
  - C:/Users/gucch/Desktop/work/EBAY/EBAY/AlertCrawler/data.db
  - C:/Users/gucch/Desktop/work/EBAY/EBAY/AlertCrawler/AlertCrawler.exe.config
  - tools/ebay-manager/monitor/mercari_search.py
  - tools/ebay-manager/monitor/yahoo_search.py
  - tools/ebay-manager/daily_scheduler.py
  - tools/ebay-manager/monitor/database.py
  - tools/ebay-manager/tasks/task_supplier_candidate_search.py
related:
  - 2026-05-14-W126-W123-W125_unified_design_v4
genre: keyword-watch
metadata:
  type: design
  wiki_type: synthesis
---

# キーワード新着監視 (AlertCrawler 移植) 設計書 — W148 (v2)

**作成日**: 2026-05-20 (v1) / **v2 改訂**: 2026-05-20 (Codex 1回目指摘反映) / **v2.1 改訂**: 2026-05-20 (Codex 2回目指摘反映)
**ROADMAP id 候補**: 232 (次空き) / **tag 候補**: W148 (次空き、W147 まで使用済)
**設計フェーズ**: Q3 構造化フロー (Phase 4 = 設計、実装は別途指示)

## v2 → v2.1 改訂履歴 (Codex 2 回目指摘反映)

| # | v2 の問題 | v2.1 修正 | 根拠 |
|---|---|---|---|
| HIGH-A | `_launch_subprocess` が `res.stdout` (string) を返却。`_run_isolated_task` は dict + `.get("success")` を期待 → AttributeError で成功も failed 記録になる | **`_launch_subprocess` が `{"success": True, "message": stdout_tail}` dict を返す** | Codex: daily_scheduler.py:1148 で `r.get("success", False)` を呼ぶ。確認済 |
| HIGH-B | per-watch `consecutive_zero_count` ≥ 5 で DOM 変更疑い alert。鳴かない watch (例: AMADA DIGIPRO は数か月新着なくて当然) でも 10h で false alarm | **センチネル方式に置換**: 各サイトに「常にヒットが出る保証付きキーワード」を 1 件登録 (例: メルカリ "iPhone"、ヤフオク "iPhone")。**全サイト sentinel が同時に 0 件 = site-wide DOM 変更 or bot ban** と判定 → Discord 警告 1 回/run | Codex: 「per-watch quietness != DOM rot, need site-level signal」。Canary-in-coal-mine pattern |
| MEDIUM | `_check_price_range(price, None, None)` が True を返す。§15-Q1「両方NULL = 通知無効」と矛盾 | **`if pmin is None and pmax is None: return False` を関数先頭に追加** | Codex: §15-Q1 と pseudocode の矛盾 |
| MEDIUM (Codex 3回目で追加検出) | sentinel 未登録サイトは site_health に出現せず DOM 検知が永久発火しない silent gap | **run 終了時に `watched_sites - sentinel_sites` の orphan_sites を logger.warning + summary に記録 + UI 巡回サマリで赤帯 warning 表示** | Codex 3回目: 「if a site has 0 active sentinels, no DOM/ban alert can ever fire」= Q0 silent skip |

## v1 → v2 改訂履歴 (Codex 1 回目指摘反映)

| # | 旧設計 (v1) | 新設計 (v2) | 根拠 |
|---|---|---|---|
| HIGH-1 | scheduler 内で `import + call` (in-process、APScheduler worker thread) | **subprocess (`python -m tasks.task_keyword_watch_crawl`) 化** | Codex: mercari_search.py L13-14 が「sync_playwright は thread-safe でない・main thread sequential 前提」と明記、APScheduler worker thread での運用実証なし。subprocess で根本回避 |
| HIGH-2 | 「last_error に no cards found 記録」だけで DOM 変更気付き | **`consecutive_zero_count` 列で連続 N 回 0 件 → 「DOM 変更疑い」自動警告** | Codex: search_mercari/yahoo は「正常 0 件」と「セレクタ壊れ 0 件」を区別しない (return [])。W148 側でヒューリスティック |
| MEDIUM | per-watch try/except のみ、ImportError 経路で task_execution_log 痕跡なし | **`run_keyword_watch_crawl` 全体を top-level try/except、ImportError は `_run_isolated_task` 内で `subprocess.CalledProcessError` 化** | Codex: 設計が「always dict 返却」と謳うが擬似コードでは保証されていなかった |
| LOW | subprocess 仕様未明 | **`sys.executable + stdin=DEVNULL + capture_output + timeout=600` を §5.3 で明記** | Codex: Windows pythonw deadlock 防止策の具体化 |
| LOW | minute=20 (02:30 batch 10分前) | 同 (受容)、`max_watches_per_run=30` × ~5min = 02:25 完了見込み | Codex: 衝突なしを確認。実装時に余裕見て minute=25 等微調整可 |
| LOW | Discord 送信失敗時の再送パスなし | 受容 (claim 済で次回も skip)。`discord_sent=0` の hit を UI に「未通知」表示で user が手動対応 | Codex: 意図された trade として明示 |

---

## 1. 概要 (本機能の位置づけ)

メルカリ・ヤフオクで「狙っている型番が希望価格レンジで新規出品された瞬間」を Discord に通知する **攻めの市場ディスカバリ機能**。既存の MonoDeck 在庫監視 (W7 inventory_check / W94 supplier_sweep / W139 ensure_monitor_coverage) は「すでに無在庫出品で紐付けた仕入先 URL が OOS / 値上げになっていないか」を見る **守り** = 1 SKU : 1 URL 軸の保全タスクであるのに対し、本機能は **検索 URL : N 商品** 軸の「新着流入を漏らさない」発掘タスク。両者は同じ Selenium/Playwright を使うが概念上独立 (用途・テーブル・タブを混ぜない = K2 Surgical / user 確定スコープ #2)。

C# .NET WinForms の AlertCrawler v1.2.2 (450 件のウォッチ URL 蓄積) を MonoDeck に移植する。**移植元データは選別 UI で user チェック後のみ取り込む** (user 確定スコープ #5)。

---

## 2. スコープ

### 含まれる
- 対象サイト: **メルカリ + ヤフオクのみ** (user 確定 #1)
- 通知: **Discord webhook 「eBay Manager」流用** (user 確定 #3)。ChatWork/GAS メールは採用しない
- スケジューラ: **1〜2 時間間隔** (user 確定 #2、`CronTrigger(hour='*/2', minute=20)` 等)
- スクレイパ: **既存 `monitor/mercari_search.py` + `monitor/yahoo_search.py` (Playwright 版) を再利用** (DOM セレクタ運用実績あり、Selenium 別系統を新規追加しない)
- 新タブ「キーワード新着監視」: 一覧 / 追加 / 編集 / 削除 / 手動巡回 / 移行 UI
- AlertCrawler `data.db` 450 件の選別移行 UI (SJIS デコード対応)

### 含まれない (将来拡張禁止、3 回出てから議論)
- 3 サイト目以降 (駿河屋 / メルカリショップ / ラクマ / PayPay 等)
- 商品状態 (新品/中古) フィルタ — 価格レンジのみで判定
- 自動仕入れ / 自動 watchlist 登録
- 価格レンジ以外の高度フィルタ (出品者 NG リスト / カテゴリ等)
- 既存 supplier_candidates / W7 / W122 との統合 (user 確定 #2 で別タブと確定)
- ChatWork / メール通知再現
- C# ツールの WinForms 操作互換 (移行 1 回限り)

---

## 3. 作成/修正ファイル一覧

### 新規作成
| パス (project 相対) | 役割 |
|---|---|
| `tools/ebay-manager/tabs/tab_keyword_watch.py` | 新タブ本体 (一覧 / 追加 / 編集 / 削除 / 手動巡回ボタン / 移行 UI)。`render_keyword_watch_tab()` を export |
| `tools/ebay-manager/tasks/task_keyword_watch_crawl.py` | task 本体。`run_keyword_watch_crawl(config) -> dict` を export (UI「今すぐ巡回」が直接呼ぶ in-process 経路)。**末尾に `if __name__ == "__main__":` 追加** (cron は subprocess 経由で起動するため)。1 ウォッチずつ既存 `search_mercari` / `search_yahoo` を呼ぶ |
| `tools/ebay-manager/monitor/keyword_watch_db.py` | DB helpers (CRUD + claim-then-act dedupe + 統計取得)。`add_watch / list_watches / update_watch / delete_watch / record_hit_claim / get_recent_hits` |
| `tools/ebay-manager/scripts/import_alertcrawler_legacy.py` | one-shot. AlertCrawler `data.db` を読んで `data/alertcrawler_legacy_export.json` に dump (SJIS デコード + url query から keyword 推定)。**migration 内には書かない** (Q2: ALTER 系以外の bulk INSERT は別 one-shot 化、db-migration-rules.md) |
| `tools/ebay-manager/tests/test_keyword_watch.py` | pytest: DB 冪等性 / dedupe / 価格レンジ filter / URL parse / 移行 dump parser |

### 修正
| パス | 修正内容 |
|---|---|
| `tools/ebay-manager/monitor/database.py` | 末尾に migration v45 追加 (`keyword_watches` / `keyword_watch_hits` / 各 index)。**W140 v44 と同型の Q2 自己修復**: `CREATE` 後に sqlite_master で 2 テーブル実在を確認してから `PRAGMA user_version = 45` を bump |
| `tools/ebay-manager/daily_scheduler.py` | (a) `setup_scheduler()` 末尾に `scheduler.add_job(_run_keyword_watch_crawl, CronTrigger(hour='*/2', minute=20), id='keyword_watch_crawl', max_instances=1, replace_existing=True)` 追加。(b) `_run_keyword_watch_crawl(config)` 関数追加: **subprocess で `python -m tasks.task_keyword_watch_crawl` を起動** (Playwright sync_playwright を APScheduler worker thread から外す = HIGH-1 根治、Codex 2段指摘)。`_run_isolated_task` ラッパで task_execution_log への started/completed/failed 記録、subprocess 終了コード非 0 で failed 記録 (Q0 痕跡) |
| `tools/ebay-manager/monitor/task_execution_log.py` | `TASK_SCHEDULE` リストに `{"key": "keyword_watch_crawl", "display": "W148 キーワード新着監視 (2h ごと)", "hours": None, "weekdays": None, "owner": "keyword_watch", "kind": "interval", "interval_minutes": 120}` を追加 (claude_loop_healthcheck と同型) |
| `tools/ebay-manager/config/schedule_config.json` | `tasks_enabled.keyword_watch_crawl` ブロック追加 (`enabled / description / interval_hours=2 / max_watches_per_run / sleep_between_watches_sec / browser_idle_timeout_sec`) |
| `tools/ebay-manager/app.py` | (a) `from tabs.tab_keyword_watch import render_keyword_watch_tab` を tabs import 群に追加。(b) `_W134_TABS` に `"キーワード新着監視"` を追加 (「仕入先候補」の後)。(c) `if _w134_sel == "キーワード新着監視": render_keyword_watch_tab()` 分岐追加 (morning_discovery 分岐の並びに置く) |
| `tools/ebay-manager/data/system_improvements.json` | W148 entry 追加 (id=232) |

### 触らない (K2 Surgical)
- 既存 `monitor/mercari_search.py` / `monitor/yahoo_search.py` (流用のみ。セレクタ更新が必要になったらその時の別 W で対応)
- 既存 supplier_candidates / monitored_items / W122 morning_discovery_candidates テーブル (本機能は独立)
- 既存 Discord 通知関数 (`notifiers/discord_notifier.py` は `send_message(content, embed)` のみ利用、新メソッド追加なし)

---

## 4. DB スキーマ (migration v45)

```sql
-- ============================================================
-- v45 (W148 / 2026-05-20): キーワード新着監視 (AlertCrawler 移植)
-- listing 識別は使わず、検索 URL : N 商品 hits 軸。
-- claim-then-act dedupe = UNIQUE(watch_id, found_item_url) で
-- 二重巡回・二重 Discord を物理排除 (既存 inventory_decrement_log
-- v37 / listing_sale_warnings v44 と同型 idiom)。
-- ============================================================
CREATE TABLE IF NOT EXISTS keyword_watches (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    site                  TEXT NOT NULL,                  -- 'mercari' | 'yahoo_auctions'
    search_url            TEXT NOT NULL,                  -- 完全な検索 URL
    keyword               TEXT NOT NULL,                  -- url query から抽出した検索語
    price_min_jpy         INTEGER,                        -- NULL = 下限なし
    price_max_jpy         INTEGER,                        -- NULL = 上限なし
    memo                  TEXT,                           -- action メモ
    is_active             INTEGER NOT NULL DEFAULT 1,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_crawled_at       TIMESTAMP,                      -- UTC (sqlite-timezone.md)
    last_error            TEXT,                           -- 直近巡回エラー (Q0 痕跡)
    is_sentinel           INTEGER NOT NULL DEFAULT 0,     -- v2.1: 1 = サイト健康センチネル (DOM変更/ban 検知用、常に hits が出るキーワード)
    source                TEXT DEFAULT 'manual',          -- 'manual' | 'imported_alertcrawler' | 'sentinel'
    UNIQUE(site, search_url)                              -- 同 URL の二重登録防止
);

-- v2.1 (Codex 2回目指摘反映): per-watch 連続 0 件は alert fatigue を生む
-- (鳴かない watch は normal、10h で false alarm)。代わりに **センチネル方式**:
-- 各サイトに「常にヒットが出るキーワード」を 1 件 is_sentinel=1 で登録 (例:
-- メルカリ "iPhone", ヤフオク "iPhone")。run 終了時にサイトごとに sentinel の
-- 結果を確認、**全 sentinel が 0 件 = site-wide DOM 変更 or bot ban** と判定し
-- Discord 警告 1 回/run。sentinel の登録は UI の「センチネル初期化」ボタンで
-- 1 回実施 (migration 内 INSERT は db-migration-rules.md で禁止のため)。

CREATE INDEX IF NOT EXISTS idx_kw_active
    ON keyword_watches(is_active, last_crawled_at);

CREATE TABLE IF NOT EXISTS keyword_watch_hits (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id              INTEGER NOT NULL,
    found_item_url        TEXT NOT NULL,
    title                 TEXT,
    price_jpy             INTEGER,
    image_url             TEXT,
    in_price_range        INTEGER NOT NULL,
    discord_sent          INTEGER NOT NULL DEFAULT 0,
    detected_at           TIMESTAMP NOT NULL,
    notified_at           TIMESTAMP,
    FOREIGN KEY (watch_id) REFERENCES keyword_watches(id),
    UNIQUE(watch_id, found_item_url)
);

CREATE INDEX IF NOT EXISTS idx_kwh_recent
    ON keyword_watch_hits(watch_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_kwh_unnotified
    ON keyword_watch_hits(discord_sent, detected_at DESC)
    WHERE in_price_range = 1;
```

### 冪等性 / 自己修復 (Q2 / W140 v44 流儀踏襲)

```python
if schema_ver < 45:
    for _ddl in (
        "CREATE TABLE IF NOT EXISTS keyword_watches (...)",
        "CREATE TABLE IF NOT EXISTS keyword_watch_hits (...)",
        "CREATE INDEX IF NOT EXISTS idx_kw_active ON keyword_watches(...)",
        "CREATE INDEX IF NOT EXISTS idx_kwh_recent ON keyword_watch_hits(...)",
        "CREATE INDEX IF NOT EXISTS idx_kwh_unnotified ON keyword_watch_hits(...) WHERE in_price_range = 1",
    ):
        try:
            conn.execute(_ddl)
        except sqlite3.OperationalError:
            pass
    _w148_ok = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('keyword_watches','keyword_watch_hits')"
    ).fetchone()[0]
    if _w148_ok == 2:
        conn.execute("PRAGMA user_version = 45")
```

### sku-rules 適合
本機能は **SKU を一切扱わない** (検索キーワードと商品 URL のみ)。listing 一意キー JOIN / dedup なし → 抵触なし。

---

## 5. コンポーネント設計

### 5.1 `monitor/keyword_watch_db.py`

```python
def add_watch(*, site: str, search_url: str, keyword: str,
              price_min_jpy: Optional[int], price_max_jpy: Optional[int],
              memo: str = "", source: str = "manual") -> int:
    """INSERT OR IGNORE で UNIQUE(site, search_url) 重複は静かに skip し、
    既存 id を返す。新規 insert なら lastrowid。
    cursor.rowcount で 0/1 を判定し UI に「既登録」「新規追加」を伝える。"""

def list_watches(active_only: bool = True) -> list[dict]: ...

def update_watch(watch_id: int, **fields) -> bool:
    """is_active / price_min_jpy / price_max_jpy / memo / keyword のみ更新可。
    search_url / site は不変 (変更したい場合は delete + add で別 watch 化)。"""

def delete_watch(watch_id: int) -> bool: ...

def record_hit_claim(*, watch_id: int, found_item_url: str,
                     title: str, price_jpy: Optional[int],
                     image_url: Optional[str],
                     in_price_range: bool) -> Optional[int]:
    """claim-then-act:
       INSERT OR IGNORE INTO keyword_watch_hits ... を先に実行 + rowcount チェック。
       rowcount=0 (既知 URL) → None を返す (重複 Discord 防止)。
       rowcount=1 (新規) → lastrowid を返す = caller が Discord 送信して
         mark_hit_notified(hit_id) を呼ぶ責任を持つ。"""

def mark_hit_notified(hit_id: int) -> None:
    """discord_sent=1, notified_at=now() を UPDATE。"""

def get_recent_hits(watch_id: int, limit: int = 20) -> list[dict]: ...

def get_watch_stats() -> dict:
    """UI ヘッダ用統計: active / total / 24h_hits / 7d_hits / dom_rot_suspected。"""

def update_watch_last_crawled(watch_id: int, error: Optional[str] = None) -> None:
    """last_crawled_at = datetime('now'), last_error = error (None ならクリア) を UPDATE。"""

def init_default_sentinels() -> int:
    """v2.1: 各サイトに DOM 健康センチネル watch を 1 件ずつ登録 (既登録は skip)。
    default: メルカリ "iPhone" / ヤフオク "iPhone"。is_sentinel=1, source='sentinel',
    price_min/max=None (sentinel は通知対象でない、健康確認用なので価格レンジ無効
    = §15-Q1 で in_price_range=False になり Discord 個別通知は飛ばない)。
    Returns: 新規登録件数。UI の「センチネル初期化」ボタンが 1 回呼ぶ。"""

def list_active_sentinels() -> list[dict]:
    """is_sentinel=1 AND is_active=1 の watch を返す (site でグループ化用)。"""
```

### 5.2 `tasks/task_keyword_watch_crawl.py`

```python
def run_keyword_watch_crawl(config: dict) -> dict:
    """戻り値: {success: bool, message: str, watches_crawled: int,
                 new_hits: int, in_range_hits: int, errors: int,
                 discord_sent: int}
    必ず dict を返す (Q0 偽装成功防止: 例外時も success=False で dict)。"""
```

処理フロー (擬似コード、v2 = top-level try/except + DOM ヒューリスティック):

```python
def run_keyword_watch_crawl(config: dict) -> dict:
    # v2 Codex MEDIUM 対応: top-level try/except で必ず dict を返す。
    # 例外時も success=False + error_message + 部分集計を含む dict 返却 (Q0)。
    summary = {"success": False, "message": "", "watches_crawled": 0,
               "new_hits": 0, "in_range_hits": 0, "errors": 0,
               "discord_sent": 0, "dom_rot_suspected": 0}
    try:
        cfg = config.get('tasks_enabled', {}).get('keyword_watch_crawl', {})
        if not cfg.get('enabled', True):
            return {**summary, "success": True, "message": "disabled"}

        webhook = (config.get('discord', {}) or {}).get('webhook_url') or ""
        # sentinel は normal watch と一緒に巡回するが、site_health 集計用に取り分ける
        watches = list_watches(active_only=True)
        max_per_run = int(cfg.get('max_watches_per_run', 30))
        sleep_sec = float(cfg.get('sleep_between_watches_sec', 4))
        # 古い last_crawled_at 順 = 公平 rotation。sentinel は必ず含める (集計の為)
        sentinels = [w for w in watches if w.get('is_sentinel')]
        non_sent = [w for w in watches if not w.get('is_sentinel')]
        watches = sentinels + non_sent[:max(0, max_per_run - len(sentinels))]

        # v2.1 Codex HIGH-B: site-level センチネル集計 (per-watch 連続0件は false alarm)
        site_health = {}  # site -> {'sentinel_total': N, 'sentinel_zero': N}

        for w in watches:
            hits = []
            err = None
            try:
                if w['site'] == 'mercari':
                    from monitor.mercari_search import search_mercari
                    hits = search_mercari(w['keyword'], max_results=10, headless=True)
                elif w['site'] == 'yahoo_auctions':
                    from monitor.yahoo_search import search_yahoo
                    hits = search_yahoo(w['keyword'], max_results=10, headless=True)
                else:
                    continue  # unsupported site = skip
            except Exception as e:
                # per-watch 例外: 痕跡を残して次へ (Q0)
                err = f"crawl error: {type(e).__name__}: {e}"
                summary["errors"] += 1

            # v2.1: sentinel watch の結果を集計 (per-watch alert はしない)
            if w.get('is_sentinel') and err is None:
                st = site_health.setdefault(w['site'], {'sentinel_total': 0, 'sentinel_zero': 0})
                st['sentinel_total'] += 1
                if not hits:
                    st['sentinel_zero'] += 1

            for h in hits:
                in_range = _check_price_range(h.price_jpy,
                                              w['price_min_jpy'], w['price_max_jpy'])
                hit_id = record_hit_claim(watch_id=w['id'], found_item_url=h.url,
                                          title=h.title, price_jpy=h.price_jpy,
                                          image_url=h.image_url,
                                          in_price_range=in_range)
                if hit_id is None:
                    continue  # 既知 URL (二重防止)
                summary["new_hits"] += 1
                if in_range:
                    summary["in_range_hits"] += 1
                    ok = _send_discord_for_hit(webhook, w, h, hit_id)
                    if ok:
                        mark_hit_notified(hit_id)
                        summary["discord_sent"] += 1

            update_watch_last_crawled(w['id'], error=err)
            summary["watches_crawled"] += 1
            time.sleep(sleep_sec)

        # v2.1 Codex HIGH-B: site-level DOM/ban センチネルチェック (run 終了時)
        for site, st in site_health.items():
            if st['sentinel_total'] > 0 and st['sentinel_zero'] == st['sentinel_total']:
                # 当該サイトの全 sentinel が 0 件 = 高確度で DOM 変更 or bot ban
                msg = (f"[W148 警告] {site}: 全センチネル ({st['sentinel_total']}件) が 0 件 = "
                       f"DOM 変更 or bot ban の可能性。selector / user_agent / IP を点検してください。")
                logger.warning(msg)
                _send_discord_site_health(webhook, site, msg)  # 1 サイト 1 通知/run
                summary["dom_rot_suspected"] += 1

        # v2.1 Codex 3回目 MEDIUM 対応: sentinel 未登録サイトの silent gap 防止
        # active watch があるのに sentinel が 0 件のサイトは DOM 変更検知が永久に
        # 発火しない = Q0 silent gap。run 終了時に検出して logger.warning + summary
        # に dom_rot_orphan_sites を記録 (UI 側で「⚠️ sentinel 未登録」赤帯表示)。
        if cfg.get('sentinel_health_check_enabled', True):
            watched_sites = {w['site'] for w in watches}
            sentinel_sites = {s for s, st in site_health.items() if st['sentinel_total'] > 0}
            orphan_sites = watched_sites - sentinel_sites
            if orphan_sites:
                msg = (f"[W148 注意] sentinel 未登録サイト: {sorted(orphan_sites)}。"
                       f"DOM 変更/ban 自動検知が無効。UI「センチネル初期化」を実行推奨。")
                logger.warning(msg)
                summary["dom_rot_orphan_sites"] = sorted(orphan_sites)

        summary["success"] = True
        summary["message"] = (
            f"crawled={summary['watches_crawled']} new={summary['new_hits']} "
            f"in_range={summary['in_range_hits']} discord={summary['discord_sent']} "
            f"err={summary['errors']} dom_rot={summary['dom_rot_suspected']}"
        )
        return summary
    except Exception as e:
        # config/DB/Discord 初期化等の最上層例外 (Codex MEDIUM 対応)
        logger.exception("run_keyword_watch_crawl top-level failure")
        summary["message"] = f"top-level failure: {type(e).__name__}: {e}"
        return summary  # success=False のまま


if __name__ == "__main__":
    # v2 HIGH-1 対応: cron は subprocess 経由でこの __main__ を起動。
    # config を読み込み run_keyword_watch_crawl を呼び、結果を stdout JSON
    # で出し、success に応じて exit code を返す (scheduler 側 _run_isolated_task
    # が exit code で started/completed/failed を task_execution_log に記録)。
    import json, sys, logging
    from monitor.config import load_config  # 既存 config loader
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    res = run_keyword_watch_crawl(load_config())
    print(json.dumps(res, ensure_ascii=False, default=str))
    sys.exit(0 if res.get("success") else 1)
```

価格レンジ判定 (v2.1 Codex MEDIUM 対応: 両方NULL = 通知無効):

```python
def _check_price_range(price: Optional[int], pmin: Optional[int], pmax: Optional[int]) -> bool:
    if price is None:
        return False                  # 価格未取得は通知しない (誤発火防止)
    if pmin is None and pmax is None:
        return False                  # 両方NULL = 価格レンジ未設定 = 通知無効 (§15-Q1)
    if pmin is not None and price < pmin:
        return False
    if pmax is not None and price > pmax:
        return False
    return True
```

### 5.3 `daily_scheduler.py` への追加 (v2: subprocess 化)

**Codex 2段 HIGH-1 対応**: APScheduler worker thread 内で sync_playwright を直呼びすると `mercari_search.py:13-14` の「main thread sequential 前提」と衝突する。subprocess で別プロセスに分離し、Playwright を main thread (= subprocess の main) で動かす。`_run_isolated_task` は subprocess 起動・終了コード判定をラップし started/completed/failed を task_execution_log に記録 (Q0)。

```python
import subprocess, sys, os, pathlib
from apscheduler.triggers.cron import CronTrigger

kw_cfg = (config.get('tasks_enabled', {}).get('keyword_watch_crawl') or {})
if kw_cfg.get('enabled', True):
    interval_hours = int(kw_cfg.get('interval_hours', 2))
    scheduler.add_job(
        _run_keyword_watch_crawl,
        trigger=CronTrigger(hour=f'*/{interval_hours}', minute=20, second=0),
        args=[config],
        id='keyword_watch_crawl',
        name=f'W148 キーワード新着監視 ({interval_hours}h ごと :20)',
        replace_existing=True,
        max_instances=1,
    )
    logger.info(f"W148 キーワード新着監視 発火: {interval_hours} 時間ごと :20 分 (subprocess)")


def _run_keyword_watch_crawl(config: dict):
    """W148 — subprocess で task_keyword_watch_crawl を別プロセス起動 (sync_playwright を APScheduler worker thread から外す)."""
    def _launch_subprocess():
        cwd = pathlib.Path(__file__).resolve().parent  # tools/ebay-manager
        timeout_sec = int((config.get('tasks_enabled', {})
                                 .get('keyword_watch_crawl', {})
                                 .get('subprocess_timeout_sec', 600)))
        try:
            res = subprocess.run(
                [sys.executable, "-m", "tasks.task_keyword_watch_crawl"],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,          # Windows pythonw deadlock 防止
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                # 子プロセスへ親の環境をそのまま伝播 (DB_PATH 等)
                env={**os.environ},
                # Windows でコンソール窓を開かない
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"keyword_watch_crawl subprocess timeout ({timeout_sec}s)"
            ) from e
        if res.returncode != 0:
            # 失敗 → _run_isolated_task が failed として記録できるよう raise
            raise RuntimeError(
                f"keyword_watch_crawl subprocess failed (exit={res.returncode}): "
                f"stderr={(res.stderr or '')[:500]} stdout={(res.stdout or '')[:300]}"
            )
        stdout_tail = (res.stdout or '').strip()[:300]
        logger.info(f"keyword_watch_crawl OK: {stdout_tail}")
        # v2.1 Codex HIGH-A 対応: _run_isolated_task は dict + .get("success") を
        # 期待する (daily_scheduler.py:1148 `r.get("success", False)`)。string 返却
        # だと AttributeError で成功も failed 記録になるため必ず dict を返す。
        return {"success": True, "message": stdout_tail}

    _run_isolated_task('keyword_watch_crawl', 'W148 キーワード新着監視',
                       _launch_subprocess,
                       scheduled_hour=None)  # interval task = claude_loop_healthcheck 流儀
```

**UI「今すぐ巡回」ボタン経路は in-process 呼出** (Streamlit script は main thread = Playwright 安全)。同じ `run_keyword_watch_crawl(config)` を直接 import して呼ぶ。並行で cron subprocess が走っていても DB の claim-then-act + UNIQUE で安全 (別プロセス間でも SQLite WAL + UNIQUE 制約が二重 hit を物理排除)。

### 5.4 Discord 通知 (既存資産流用)

```python
def _send_discord_for_hit(webhook: str, watch: dict, hit, hit_id: int) -> bool:
    from notifiers.discord_notifier import DiscordNotifier
    notifier = DiscordNotifier(webhook)
    site_label = '🛒 メルカリ' if watch['site'] == 'mercari' else '🔨 ヤフオク'
    price_str = f"¥{hit.price_jpy:,}" if hit.price_jpy else "(価格不明)"
    range_str = _format_range(watch['price_min_jpy'], watch['price_max_jpy'])
    embed = {
        'title': f"{site_label} 新着: {hit.title[:80]}",
        'url': hit.url,
        'color': 3066993 if watch['site'] == 'mercari' else 15105570,
        'fields': [
            {'name': '価格', 'value': f"{price_str}  (希望 {range_str})", 'inline': True},
            {'name': 'キーワード', 'value': watch['keyword'][:80], 'inline': True},
            {'name': 'メモ', 'value': (watch.get('memo') or '—')[:200], 'inline': False},
        ],
        'image': {'url': hit.image_url} if hit.image_url else None,
    }
    return notifier.send_message(
        f"🔔 キーワード新着 ({site_label})", embed=embed
    )
```

### 5.5 タブ UI (`tabs/tab_keyword_watch.py`)

3 セクション構成 (`st.container(border=True)` で縦並び、既存 W122 流儀):

1. **巡回サマリ**: active 件数 / sentinel 件数 / 直近 24h hits / 最終巡回時刻 / **「センチネル初期化」ボタン** (v2.1: 初回 1 回押下で各サイトに「常にヒットが出る」 sentinel watch を 1 件ずつ自動登録 = `init_default_sentinels()`、登録済 skip) / 「今すぐ巡回」ボタン (押下で `run_keyword_watch_crawl(config)` を同期実行、Streamlit 内で完了まで spinner) / **v2.1: 巡回サマリ冒頭で「watched_sites - sentinel_sites」を計算し、未登録があれば「⚠️ センチネル未登録: {sites}。DOM 変更/ban 自動検知が無効です」赤帯 warning 表示** (Codex 3回目 MEDIUM 対応、Q0 silent gap 防止)
2. **ウォッチ一覧** (`st.dataframe` or `st.data_editor`):
   - 列: id / サイト / キーワード / 価格レンジ / メモ / 最新検出 / last_crawled_at / is_active / last_error
   - 各行に「編集」「削除」「履歴表示」ボタン (`st.button(key=f"edit_{id}")` で widget key を id に bind = W138-A の HIGH-2 教訓踏襲)
3. **追加 form** (`st.form`):
   - サイト selectbox (mercari / yahoo)
   - キーワード text_input (送信時に内部で `search_url` を自動構築)
   - 価格レンジ (min / max、両端任意)
   - メモ text_area
   - 「追加」submit ボタン → `add_watch()` + `bump_db_version()` + `st.rerun()`
4. **移行 UI** (st.expander 風で常時可視):
   - 「AlertCrawler legacy 取込」ボタン → `scripts/import_alertcrawler_legacy.py` を subprocess 起動
   - 結果 JSON を `st.data_editor(num_rows='dynamic')` でチェックボックス付き表示
   - 「選択分を登録」ボタン → 各行を `add_watch(source='imported_alertcrawler')`

### 5.6 `schedule_config.json` 追加ブロック

```json
"keyword_watch_crawl": {
    "enabled": true,
    "description": "W148 キーワード新着監視: メルカリ/ヤフオクを 2 時間毎 subprocess 巡回し希望価格レンジ合致を Discord 通知",
    "priority": 9,
    "interval_hours": 2,
    "max_watches_per_run": 30,
    "sleep_between_watches_sec": 4,
    "subprocess_timeout_sec": 600,
    "sentinel_health_check_enabled": true,
    "note": "巡回件数 × sleep で総時間 ~ max*5s 以内。30 件 = 約 4-6 分 (timeout 10 分に余裕)。v2.1 Codex 2回目 HIGH 対応: per-watch 連続0件カウンタは false alarm なので廃止。代わりに **センチネル方式** — 各サイトに「常にヒットが出る」 watch (is_sentinel=1) を 1 件登録し、全 sentinel が同時に 0 件 = site DOM 変更 or bot ban として警告 1 回/run。sentinel 初期化は UI ボタンから 1 回 (migration data write 禁止のため)。"
}
```

### 5.7 `task_execution_log.TASK_SCHEDULE` 追加

```python
{"key": "keyword_watch_crawl",
 "display": "W148 キーワード新着監視 (2h ごと)",
 "hours": None, "weekdays": None,
 "owner": "keyword_watch",
 "kind": "interval", "interval_minutes": 120},
```

`kind='interval'` マーカーで `get_today_expected_tasks` から expected slot 模型を除外。

---

## 6. データフロー図

```
[user 登録 / 移行 UI]
    └─→ add_watch(site, search_url, keyword, price_min, price_max, memo)
            └─→ keyword_watches テーブル (INSERT OR IGNORE / UNIQUE site+url)

[cron 2 時間ごと :20 分]
    └─→ _run_keyword_watch_crawl (isolated, thread-local batch_ctx)
            └─→ run_keyword_watch_crawl(config)
                    │
                    ├─→ list_watches(active_only=True) → 古い last_crawled_at 順 max 30 件
                    │
                    └─→ for each watch:
                            ├─→ mercari → search_mercari(keyword, 10件)  [既存 Playwright]
                            └─→ yahoo   → search_yahoo(keyword, 10件)    [既存 Playwright]
                                    │
                                    └─→ for each hit:
                                            ├─→ record_hit_claim() (UNIQUE で claim)
                                            │       既知 URL → None で skip (二重防止)
                                            │       新規 URL → hit_id 返却
                                            │
                                            └─→ if in_price_range:
                                                    ├─→ DiscordNotifier.send_message(embed)
                                                    │       └─→ Discord webhook "eBay Manager"
                                                    │
                                                    └─→ mark_hit_notified(hit_id)
                            └─→ update_watch_last_crawled (success/error)
                            └─→ sleep 4s (bot 検知回避)
                    │
                    └─→ task_execution_log に完了/失敗を必ず記録 (Q0)

[新タブ「キーワード新着監視」]
    ├─→ 巡回サマリ (db: get_watch_stats)
    ├─→ 一覧 (db: list_watches + get_recent_hits)
    ├─→ 追加 form (db: add_watch + ui_cache.bump_db_version)
    └─→ 移行 UI:
        ├─→ subprocess: scripts/import_alertcrawler_legacy.py
        │       └─→ Desktop\work\EBAY\EBAY\AlertCrawler\data.db  読込
        │       └─→ SJIS デコード + URL query から keyword 抽出
        │       └─→ dataC「【価格】安:¥X 高:¥Y」regex parse
        │       └─→ data/alertcrawler_legacy_export.json 出力
        └─→ st.data_editor でチェック → 選択分 add_watch(source='imported_alertcrawler')
```

---

## 7. ビルドシーケンス

依存順序に沿った実装ステップ。各 step で **pytest PASS + 1 stake** 検証後に次へ進む。

1. **DB migration v45** (`monitor/database.py` 末尾追加)
2. **DB helpers** (`monitor/keyword_watch_db.py` 新規)
3. **scheduled task** (`tasks/task_keyword_watch_crawl.py` 新規)
4. **scheduler 統合** (`daily_scheduler.py` + `task_execution_log.TASK_SCHEDULE` + `schedule_config.json`)
5. **タブ UI** (`tabs/tab_keyword_watch.py` 新規 + `app.py` 3 行追加)
6. **移行 script** (`scripts/import_alertcrawler_legacy.py` 新規)
7. **移行 UI 統合** (tab 内移行セクション)
8. **E2E 実機巡回** (1 ウォッチで本物のメルカリ叩く)
9. **本番 cron 投入** (scheduler 再起動)

---

## 8. リスク・未決事項

### 高リスク (両論併記、contradiction-annotation.md 流儀)

| # | リスク | 影響 | 対策案 |
|---|---|---|---|
| R1 | **DOM セレクタ陳腐化** (v2.1: Codex 2回目で per-watch heuristic → センチネル方式へ変更) | 0 件返却で silent skip 化 | **v2.1: センチネル方式** — 各サイトに「常にヒットが出る」 watch (例: メルカリ "iPhone") を `is_sentinel=1` で 1 件登録。run 終了時に **全 sentinel が 0 件 = site-wide DOM 変更 or bot ban** として Discord 警告 1 回/run。鳴かない通常 watch (例: AMADA DIGIPRO は数か月新着なくて当然) で false alarm を起こさない (Codex 2回目で per-watch consecutive_zero_count は廃止)。selector fallback は既存 `_CARD_SELECTOR_CANDIDATES` (yahoo_search.py) に任せる |
| R2 | **bot 検知 / IP ban** (v2.1: センチネル方式に統合) | 全 watch が 0 件返却 | 既存 search_mercari の `user_agent: Chrome/120` 流用 + `sleep_between_watches_sec=4` + max 30 件/run。v2.1: R1 と同じセンチネル方式で検知 (DOM変更と ban は症状が同じなので警告は共通、message に「selector / user_agent / IP を点検」両方記載) |
| R3 | **Playwright sync_playwright が APScheduler worker thread で動くか** (v2: Codex HIGH-1 で根本対応) | task 起動毎に crash の可能性 | **v2: subprocess 化で APScheduler worker thread から完全分離**。subprocess の main thread で sync_playwright を呼ぶため `mercari_search.py:13-14` の前提 (main thread sequential) を満たす。supplier_sweep は別系統 (`task_supplier_candidate_search.py:184-185`) で同じ search_mercari を**直接 worker thread で呼んでいる**が、本機能はそれに依存しない安全側設計 |
| R4 | **`_batch_ctx` thread-local 影響** | 新 cron 追加で別 task の hour clobber 再発? | `_run_isolated_task` 経由 + ThreadLocal 化済のため構造的に塞がれている |
| R5 | **chromedriver / Playwright browser 自動更新** | OS update 後にバイナリ不一致で crash | 既存 supplier_sweep 等と共通パス。本機能は追加負担なし (K1) |
| R6 | **1 サイクル所要時間** | 30 件 × ~5-8s = 約 3-4 分 | 上限 30 件強制。古い順 rotation で 450 件全件を 30h で 1 周 |
| R7 | **移行 450 件全選択で初回 cron 過負荷** | 1 サイクル 30-60 分 (cron 衝突) | (a) hard cap 30 件 (b) 移行 UI に「初回推奨 50 件以下」warning (c) user 選別 UI で絞り込ませる |
| R8 | **SJIS 「【価格】安:¥X 高:¥Y」のパース失敗** | 価格レンジ NULL 登録 → 通知 0 | regex で抽出、失敗は dataC 原文を memo に流す |
| R9 | **2026-05-20 時点でメルカリ DOM が変更されている可能性** | 0 件返却 | step 8 で必ず実機巡回 → 0 件なら本設計の前提崩壊 → 別 W で DOM 再調査 |

### 未決事項 (user 判断必要)

| # | 質問 | 候補 |
|---|---|---|
| Q1 | 価格レンジ NULL ウォッチを Discord 通知するか | **(b) NULL は通知せず履歴のみ (推奨)** |
| Q2 | 同一商品 URL の値下げ再通知 | (a) UNIQUE で 1 回のみ (現設計、推奨) / (b) 将来拡張 |
| Q3 | スケジュール間隔 | **2 時間 (推奨)** |
| Q4 | 移行 450 件のうち初期登録想定件数 | 50 件 / 100 件 / 全件 |
| Q5 | 「今すぐ巡回」ボタン | **(a) 全 active watch (推奨)** |
| Q6 | キーワード重複登録 | UNIQUE(site, search_url) で OK |

---

## 9. 完了判定基準 (Q1 DoD 11 ステップ準拠)

| # | ステップ | 確認手段 | 期待結果 |
|---|---|---|---|
| 1 | DB migration 適用 | `PRAGMA user_version` | `(45,)` |
| 2 | DB migration 冪等性 | init_db 2回連続 + 行保持 | 行が消えない |
| 3 | pytest 全体 | `pytest tests/` | 全 PASS、HIGH=0 |
| 4 | scheduler 再起動 | `scheduler.log` grep | 「W148 キーワード新着監視 発火」 |
| 5 | scheduler 内 job 一覧 | `get_jobs()` 出力 | `id='keyword_watch_crawl'` |
| 6 | task_execution_log 記録 | DB SELECT | status=completed |
| 7 | E2E 実機巡回 | UI「今すぐ巡回」 | hits 行追加 |
| 8 | Discord 実視認 (R-11) | user 目視 | 通知表示 + URL 遷移 |
| 9 | dedupe 動作 | 同一巡回 2 回 | hits 行不変 + Discord 1 回 |
| 10 | 移行 UI | Playwright 3 件選択登録 | DB INSERT 確認 |
| 11 | code-reviewer + Codex 2 段 | HIGH=0 ループ | HIGH=0 |

---

## 10. Plan → Verify → Persist → Automate

| phase | 本機能での実体 |
|---|---|
| **Plan** | 本設計書 (W148) |
| **Verify** | step 1-7 の pytest + Streamlit + 実機巡回 |
| **Persist** | DB v45 migration (冪等) + `system_improvements.json` W148 entry |
| **Automate** | scheduler 2h cron + 健康チェック既存経路 |

---

## 11. 観測可能性 3 経路 (silent skip prevention 物理排除)

| 経路 | 媒体 | 表示内容 |
|---|---|---|
| **DB log** | `task_execution_log` | 各巡回の started/completed/failed |
| **Discord 通知** | `eBay Manager` webhook | (a) 価格レンジ合致 hit (b) 健康チェック未実行アラート |
| **UI 表示** | 「キーワード新着監視」タブ | last_crawled_at / last_error / 直近 hits |

3 経路すべてに必ず痕跡が残るため、silent skip は構造的に発生しない。

---

## 12. 環境特異性チェックリスト

| 項目 | 対応 |
|---|---|
| **pythonw.exe (sys.stdout None)** | `print()` 系を使わない (logging 経由) |
| **Streamlit hot reload** | 新 module 追加、UI 変更時のみ手動再起動 |
| **Windows cp932** | scheduler `utf8_console` 既存経路に乗る |
| **OAuth token cache** | 本機能は Discord webhook のみ → 不要 |
| **SQLite TIMESTAMP UTC** | `detected_at` / `last_crawled_at` UTC 保存、UI で +9h shift |
| **DB lock (WAL)** | INSERT OR IGNORE は WAL で問題なし |

---

## 13. コスト保護

本機能は **課金 API ゼロ** (Claude / Gemini / eBay API 不使用、Playwright + Discord webhook のみ)。**唯一のコストはローカル Chrome 起動 ~ 数百 MB メモリ × 30 サイクル/run** で既存 supplier_sweep と同等。

---

## 14. cascade-update 影響範囲

| ファイル | 更新要否 | 内容 |
|---|---|---|
| `tools/ebay-manager/data/system_improvements.json` | **touching** | W148 entry (id=232) |
| `tools/ebay-manager/USER_MANUAL.md` | **touching** | 新タブ操作手順追加 |
| `CLAUDE.md` (project root) | **unrelated** | 本機能は eBay 規制業務外 |
| `tools/ebay-manager/CLAUDE.md` | **unrelated** | 同上 |
| `.claude/rules/sku-rules.md` | **unrelated** | SKU 不使用 |
| `.claude/rules/discord-notification.md` | **unrelated** | 既存パターン |
| memory `MEMORY.md` | **不要** | 完了後 `session_2026_05_XX_w148_keyword_watch.md` 新規 |

---

## 15. 質問リスト (v2: 全て user 推奨採用で resolve 済)

| # | 旧 (v1) 質問 | v2 確定 |
|---|---|---|
| 1 | 価格レンジ NULL を Discord 通知するか | **通知しない (履歴のみ)** = `_check_price_range` が `pmin/pmax None なら True` でなく、片方でも NULL かつ price が範囲外なら False を厳密に。両方 NULL の watch は実装で `in_price_range = False` 固定の方針 (UI に warning「価格レンジ未設定 = 通知無効」表示) |
| 2 | cron 間隔 | **2h** 確定 (config で変更可、`interval_hours` setting) |
| 3 | 「今すぐ巡回」ボタン対象 | **全 active watch** (1 ボタン、simple) |
| 4 | 移行元 `data.db` path | **hardcode + UI 上書き可能** (移行 1 回限り、`scripts/import_alertcrawler_legacy.py` の default = `C:\Users\gucch\Desktop\work\EBAY\EBAY\AlertCrawler\data.db`、UI text_input で変更可) |
| 5 | `tab_keyword_watch.py` 挿入位置 | **「仕入先候補」直後** (攻め/守りを並べる意図) |
| 6 | DOM 変更時の対応 | **v2.1: §8 R1 でセンチネル方式 (各サイトに「常にヒットが出る」watch を 1 件、全 sentinel 0 件で site-wide 警告 = Codex 2回目反映)**。手動再調査タスクは別 W (機能としては継続稼働、Discord 警告 + UI last_error で user が把握) |

残未決事項: 実装着手時の **ROADMAP entry 確定 (id=232, priority=中)** のみ。user 承認後 `/add_s` 経由で `system_improvements.json` に登録。

---

## 16. 補足: 既存資産活用判断の根拠

| 既存資産 | 活用 / 新規 | 根拠 |
|---|---|---|
| `monitor/mercari_search.py` (Playwright) | **活用** | supplier_sweep で運用中、セレクタ実績 |
| `monitor/yahoo_search.py` (Playwright) | **活用** | 同上 + fallback 機構あり |
| `notifiers/discord_notifier.py` | **活用** | `send_message(content, embed)` 流用 |
| `tasks/task_supplier_sweep.py` | **参考のみ** | code 複製はしない (3 回出てから共通化) |
| `_run_isolated_task` (daily_scheduler) | **活用** | thread-local 安全性が自動付与 |
| W140 v44 自己修復 migration 流儀 | **準拠** | `_w148_ok` 実在確認 → bump |

---

## 17. ROADMAP entry 雛形

```json
{
    "id": 232,
    "tag": "W148",
    "title": "AlertCrawler 移植: メルカリ/ヤフオク キーワード新着監視 (Discord 通知 + 移行 UI)",
    "description": "C# AlertCrawler v1.2.2 (450件) を MonoDeck に移植。メルカリ + ヤフオクのみ対象、2時間cron、価格レンジ filter、claim-then-act dedupe、Discord 通知 (eBay Manager webhook 流用)。既存 monitor/mercari_search.py + yahoo_search.py 流用。新規 tab + tasks/task_keyword_watch_crawl.py + DB v45。移行は scripts/import_alertcrawler_legacy.py + UI 選別。supplier_sweep (守り) と別系統の発掘 (攻め) タスク。",
    "status": "未着手",
    "priority": "中",
    "created": "2026-05-20"
}
```

---

# 設計書ここまで

実装着手前の確認:
- **§15 の質問 6 件** を user 回答後に着手
- 着手時は本書 §7 のビルドシーケンス順に進める
- 各 step で **K3 Goal-Driven** の検証 (DoD §9 表) を必ず通す
- 完了報告は Q5 4 行テンプレに従う
