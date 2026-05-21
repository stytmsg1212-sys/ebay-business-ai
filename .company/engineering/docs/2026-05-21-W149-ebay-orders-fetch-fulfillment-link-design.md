---
name: w149-ebay-orders-fetch-fulfillment-link-design
description: eBay 売却注文の API 直接取得 + 無在庫 fulfillment との自動ひも付け (B3 当初案を既存実装活用に圧縮)
layer: wiki
updated: 2026-05-22
revision: v2
sources:
  - tools/ebay-manager/tasks/task_order_alert.py
  - tools/ebay-manager/tasks/task_sales_tracking.py
  - tools/ebay-manager/monitor/ebay_client.py (L1392-1464 GetOrders 実装)
  - tools/ebay-manager/monitor/database.py (L2941-2974 add_sale + sales_history schema)
  - tools/ebay-manager/tasks/task_purchase_confirm.py (kind='fulfillment' 既存)
  - https://developer.ebay.com/devzone/xml/docs/reference/ebay/getorders.html (期間上限仕様)
related:
  - 2026-05-20-W148-alertcrawler-keyword-watch-design (兄弟 W、本ファイルとは独立 scope)
genre: orders-fulfillment-linking
metadata:
  type: design
  wiki_type: synthesis
---

# eBay 売却注文取得 + 無在庫 fulfillment 自動ひも付け 設計書 — W149 (v2)

**作成日**: 2026-05-21 (v1) / **改訂**: 2026-05-22 (v2)
**ROADMAP id**: 237 (W149)
**設計フェーズ**: Q3 構造化フロー Phase 4 (= 設計、本セッションで実装着手、Codex 2 段 review は実装完了後)
**圧縮ヒストリー**: B3 当初案「fulfillment と eBay 注文の自動突合」(150 LOC) → 前提検証で `sales_history` 0 件発覚 → user 選択 B (eBay API 直接取得) → 既存 `task_order_alert.py::GetOrders` + `add_sale()` + `task_purchase_confirm.py(kind='fulfillment')` の流用判明で **新規 ~300 LOC + 既存修正 ~50 LOC に再圧縮**

## v2 改訂サマリ (2026-05-22, W150 着手時の self-review で 3 件根治)

1. **shipping/fee 按分 ロジック明示**: v1 「按分 or 0 初期化」は曖昧 → 実コード `ebay_client.get_orders()` (L1481-1512) 確認で **`shipping_usd` は order レベル ShippingServiceCost が全 transaction に複製コピー** = 各 transaction で素朴に `add_sale()` すると 1 order 内 N 商品で shipping を N 倍重複計上 → **利益計算大狂い**。v2 = order_id で group → qty 比按分。eBay fee (FinalValueFee) は GetOrders 戻り値に含まれない (別 API GetItemTransactions / GetAccount) → `ebay_fee_usd=0` 初期化、利益計算詳細は別 W で取得 (透明性として明記)。
2. **`task_sales_tracking.py` 並走 OFF**: メール経由 task が ebay_order_id を持たず、UNIQUE 制約に引っかからず重複 row が生まれるリスク → 本 W で `schedule_config.json` の `task_sales_tracking.enabled = false` に切替 (Q0 silent skip 防止のため stop_reason 記録、別 W で完全廃止)。
3. **30 min polling 失敗時 retry queue**: v1 で `continue` するだけだと永久 skip → 新 table `sales_history_fetch_failures` (order_id, last_attempt_at, attempt_count, last_error) を v47 で追加、次回 polling 先頭で再試行、5 回連続失敗で Discord (eBay Manager webhook)。

## 1. 概要 (本機能の位置づけ)

無在庫商品が eBay で売れた時、現状は user が手動で仕入先から購入 → 「入荷確認」画面で `kind='fulfillment'` 確定する流れ (W133-FU 2026-05-21 で実装済)。しかし「どの eBay 注文に対してどの仕入が対応するか」のひも付けが未記録 = 二重発送防止 / 利益トレース / 履行漏れ検知ができない。

本機能で:
1. eBay 売却注文を API 直接取得して `sales_history` に充填 (現状 0 件、メール経由 task は直近 2 日制限で過去取得不能)
2. 取得後、過去の fulfillment 仕入記録と ebay_item_id + 時系列 FIFO で自動ひも付け
3. 定常運用 (毎日自動) で新規注文も継続的にひも付け

**対象データ**: 2026/1/1 以降の売却注文 (user 確定 #1、約 5 ヶ月分)

## 2. スコープ

### 含まれる
- 2026/1/1 から現在までの **初回一括取得** (one-shot backfill script、90 日チャンク × 2 で eBay API 上限回避)
- 毎日自動の **新規分追加取得** (既存 30 分 polling task に追加 or 新 daily task)
- 取得結果を既存 `sales_history` テーブルに格納 (UNIQUE(ebay_order_id) で二重防止)
- 取得直後に **同 ebay_item_id の未マッチ fulfillment 仕入と FIFO マッチング** → 新 table `fulfillment_order_link` に記録
- `confirm_purchase(kind='fulfillment')` 末尾でリアルタイムマッチ (取得済 sales_history に対応する物があれば即ひも付け)
- 入荷確認 UI に「ひも付け済 eBay 注文 ID 表示」追加
- 未マッチ警告 (logger.warning + UI に「未対応売却 N 件」表示。Discord 通知は将来)

### 含まれない (3 回出てから議論)
- 有在庫の sales tracking (有在庫は inventory_count 管理が真実源、ひも付け概念不要)
- メール経由の sales_tracking task の修理 (本機能で API 直接取得に置換、メール経路は将来 deprecate 候補)
- buyer 国別 / カテゴリ別 etc 分析機能 (W21 etc 別 W で対応)
- 利益自動計算 (仕入価格との突合は将来 W、本 W は ID ひも付けまで)
- 履行漏れ自動検知 + Discord (本 W は record まで、検知 alert は別 W)
- Override #2 改 アラート機能の改修 (`task_order_alert.py` の既存 alert 部分は維持)

## 3. 作成/修正ファイル一覧

### 新規作成 (3 file)

| パス (project 相対) | 役割 | 規模 |
|---|---|---|
| `tools/ebay-manager/scripts/backfill_sales_history_2026_05_21.py` | one-shot. 2026/1/1〜現在を 90 日チャンク × 2 で `ebay_client.GetOrders` を呼び `add_sale()` で sales_history 充填 + マッチング 1 回実行 | ~150 LOC |
| `tools/ebay-manager/monitor/fulfillment_order_matcher.py` | マッチング logic 純関数. `link_unmatched()` (一括) と `link_one(ebay_item_id)` (リアルタイム単発) を export. FIFO (sales_history 古い順 → fulfillment 古い順) | ~120 LOC |
| `tools/ebay-manager/tests/test_fulfillment_order_link.py` | pytest. (1) backfill script DB 冪等性 (2) FIFO マッチ順序 (3) 1:N の正常動作 (4) 未マッチ警告 (5) リアルタイム単発マッチ | ~80 LOC |

### 修正 (4 file、v2 で schedule_config.json 追加)

| パス | 修正内容 | 規模 |
|---|---|---|
| `tools/ebay-manager/monitor/database.py` | (a) `sales_history` に `ebay_order_id TEXT UNIQUE` 列追加 (現状 column なし、二重 INSERT 防止に必須). migration v47 ALTER + UNIQUE INDEX. **W140 v44 と同型の Q2 自己修復**: sqlite_master 確認後 user_version bump. **v46 は W148 (キーワード新着監視) で先行消費 (2026-05-21 cascade) のため本 W149 は v47 へ繰り上げ確定**. (b) 新 table `fulfillment_order_link` 追加. (c) **新 table `sales_history_fetch_failures` 追加** (v2 = retry queue). (d) `add_sale()` に `ebay_order_id` 引数追加 (default None で後方互換) + `INSERT OR IGNORE ... ON CONFLICT(ebay_order_id) DO NOTHING` で dedupe | ~55 LOC |
| `tools/ebay-manager/tasks/task_order_alert.py` | 既存 GetOrders 結果ループ末尾に **(1) `add_sale(...)` 呼び出し追加** (DDP-B / 高額 EU alert はそのまま維持、sales_history 充填も同時に行う方が DRY) + **(2) shipping/fee 按分 (qty 比、v2)** + **(3) 失敗 order を `sales_history_fetch_failures` に記録 (v2)** + **(4) polling 先頭で queue 内 retry (v2)**. 既存 alert logic を改修しない (K2 Surgical) | ~50 LOC |
| `tools/ebay-manager/tasks/task_purchase_confirm.py` | `confirm_purchase(kind='fulfillment')` の正常 return 直前に `fulfillment_order_matcher.link_one(ebay_item_id)` を呼ぶ. マッチ結果を return dict に `matched_order_id` キーで含める | ~15 LOC |
| `tools/ebay-manager/config/schedule_config.json` (v2) | `task_sales_tracking.enabled = false` に切替 + `stop_reason = "W149 で API 直接取得に置換、本 task は重複 INSERT リスクのため OFF (別 W で完全廃止)"` 追記 (Q0 silent skip 防止) | ~5 LOC |

### UI 修正 (1 file、軽微)

| パス | 修正内容 | 規模 |
|---|---|---|
| `tools/ebay-manager/tabs/tab_purchase_confirm.py` | fulfillment 確定後の `st.session_state["pc_last_confirm"]` 表示に `matched_order_id` があれば「✅ eBay 注文 {id} に紐付け完了」を追加表示. 未マッチなら「⚠️ 対応する eBay 売却が見つかりません (取得待ち or 未売却)」 | ~10 LOC |

### 触らない (K2 Surgical)

- `tasks/task_sales_tracking.py` (メール経由、直近 2 日制限) — 本 W で API 直接取得に置換するため deprecate 候補だが、本 W では removal せず並走 (削除は別 W で)
- 既存 `ebay_client.py` の GetOrders 実装 (流用のみ)
- 既存 `task_order_alert.py` の DDP-B / 高額 EU alert logic

## 4. DB スキーマ (migration v47)

```sql
-- ============================================================
-- v47 (W149 / 2026-05-21): eBay 売却注文取得 + 無在庫 fulfillment ひも付け
-- 番号繰り上げ理由: v46 は W148 (キーワード新着監視) 着手時に確定 (cascade)
-- ============================================================

-- (a) sales_history に ebay_order_id 追加 (UNIQUE で二重 INSERT 防止)
-- 既存行は ebay_order_id = NULL のまま (現状 0 件なので影響なし)
ALTER TABLE sales_history ADD COLUMN ebay_order_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_history_ebay_order_id
    ON sales_history(ebay_order_id) WHERE ebay_order_id IS NOT NULL;

-- (b) fulfillment_order_link 新規 (purchase_confirmation_log と sales_history の対応関係)
CREATE TABLE IF NOT EXISTS fulfillment_order_link (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_confirmation_log_id INTEGER NOT NULL,
    sales_history_id             INTEGER NOT NULL,
    matched_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    match_method                 TEXT NOT NULL,  -- 'realtime' | 'batch' | 'manual'
    FOREIGN KEY (purchase_confirmation_log_id) REFERENCES purchase_confirmation_log(id),
    FOREIGN KEY (sales_history_id) REFERENCES sales_history(id),
    UNIQUE(purchase_confirmation_log_id),  -- 1 仕入 = 1 注文 (1:1)
    UNIQUE(sales_history_id)               -- 1 注文 = 1 仕入 (1:1)
);
CREATE INDEX IF NOT EXISTS idx_fulfillment_order_link_pcl ON fulfillment_order_link(purchase_confirmation_log_id);
CREATE INDEX IF NOT EXISTS idx_fulfillment_order_link_sh ON fulfillment_order_link(sales_history_id);

-- (c) sales_history_fetch_failures 新規 (v2 = polling 失敗時の retry queue)
CREATE TABLE IF NOT EXISTS sales_history_fetch_failures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_order_id     TEXT NOT NULL UNIQUE,  -- 同 order の再失敗は attempt_count++ のみ
    first_attempt_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    last_error        TEXT,
    discord_notified  INTEGER NOT NULL DEFAULT 0  -- 5 回失敗時 1、再通知抑止
);
CREATE INDEX IF NOT EXISTS idx_shff_last_attempt ON sales_history_fetch_failures(last_attempt_at);

-- W140 v44 と同型の Q2 自己修復: migrate 後 sqlite_master で実在確認 → PRAGMA user_version=47 bump
```

## 5. 初回 backfill 設計

`scripts/backfill_sales_history_2026_05_21.py`:

```python
# 擬似コード
START = datetime(2026, 1, 1)
END   = datetime.utcnow()
CHUNK_DAYS = 90  # eBay GetOrders CreateTimeFrom/To 上限

chunks = []
cur = START
while cur < END:
    nxt = min(cur + timedelta(days=CHUNK_DAYS), END)
    chunks.append((cur, nxt))
    cur = nxt
# 2026/1/1〜現在 (5/21) で 2 chunk (1/1-3/31, 4/1-5/21)

total_fetched = 0
for (frm, to) in chunks:
    result = ebay_client.get_orders(create_time_from=frm, create_time_to=to)
    if not result.get("success"):
        logger.error(...)  # Q0: 失敗痕跡必須、retry 余地あり
        continue
    # v2: get_orders() は transaction flatten 構造で、shipping_usd は order レベルが
    # 全 transaction に複製コピーされている (ebay_client.py L1481-1512 で確認).
    # 各 transaction を素朴に add_sale すると 1 order 内 N 商品で shipping を N 倍
    # 重複計上 = 利益計算大狂い. → order_id で group → qty 比按分.
    orders_by_id: dict[str, list] = {}
    for txn in result.get("orders", []):  # 注: "orders" は transaction list
        orders_by_id.setdefault(txn["order_id"], []).append(txn)
    for order_id, txns in orders_by_id.items():
        total_qty = sum(t.get("qty", 1) for t in txns) or 1
        order_shipping = txns[0].get("shipping_usd", 0.0)  # 全 txn 同値
        for txn in txns:
            qty = txn.get("qty", 1)
            ship_share = order_shipping * qty / total_qty  # qty 比按分
            add_sale(
                ebay_item_id=txn["ebay_item_id"],
                sku=txn.get("sku", ""),
                title=txn["title"],
                sold_price_usd=txn["item_price_usd"],
                sold_at=txn["paid_time"],
                buyer_country=txn["buyer_country"],
                shipping_cost_usd=ship_share,
                ebay_fee_usd=0.0,  # v2: GetOrders 戻り値に FinalValueFee 無し
                                   # 利益計算詳細は別 W (GetItemTransactions or GetAccount で取得)
                source_cost_jpy=0.0,  # 仕入紐付けは fulfillment_order_link 経由
                profit_jpy=0.0,
                ebay_order_id=order_id,  # UNIQUE 制約で再実行冪等
            )
            total_fetched += 1
    time.sleep(2.0)  # 礼儀 sleep (API レート制限回避)

# backfill 完了後、一括マッチ
link_count = fulfillment_order_matcher.link_unmatched()
logger.info(f"backfill: {total_fetched} sales / {link_count} fulfillment linked")
```

**冪等性**: ebay_order_id UNIQUE INDEX により再実行で重複 INSERT を物理排除 (Q2 db-migration-rules 準拠)

**按分根拠** (v2): `ebay_client.get_orders()` 戻り値構造を実コード確認 (L1481-1512) すると、`shipping_usd` は order の `<ShippingServiceCost>` を全 transaction にコピーした値. 例えば 1 注文 ($30 shipping) に商品 A (qty=1, $10) + 商品 B (qty=2, $20) の場合、qty 合計 3 → A に $10 (30×1/3)、B に $20 (30×2/3) を按分. v1 の「按分 or 0」議論は API 構造誤読に由来、v2 で正常化.

## 6. 定常運用 (新規分追加取得)

既存 `task_order_alert.py` (30 分 polling) を流用:

```python
# task_order_alert.py 既存 logic 末尾に追加 (v2: 擬似コード)
# Step 1: polling 先頭で retry queue を処理 (前回失敗 order を再試行)
retry_queue = SELECT FROM sales_history_fetch_failures
    WHERE attempt_count < 5 OR discord_notified = 0
    ORDER BY last_attempt_at ASC LIMIT 20  # batch あたり 20 件まで
for failed_order_id in retry_queue:
    try:
        result = ebay_client.get_orders_single(failed_order_id)  # 個別取得 helper
        process_order(result)  # 成功なら下記 Step 2 と同じ処理
        DELETE FROM sales_history_fetch_failures WHERE ebay_order_id = ?
    except Exception as e:
        UPDATE sales_history_fetch_failures SET
            attempt_count = attempt_count + 1,
            last_attempt_at = CURRENT_TIMESTAMP,
            last_error = ?
        WHERE ebay_order_id = ?
        if attempt_count >= 5 AND NOT discord_notified:
            _send_discord_warning(f"⚠️ W149 polling 失敗 5 回: order={failed_order_id} err={e}")
            UPDATE ... SET discord_notified = 1

# Step 2: 通常 polling (新規 order)
result = ebay_client.get_orders(num_days=1)  # 過去 1 日分 (30min polling なので十分)
orders_by_id = group_by_order_id(result["orders"])
for order_id, txns in orders_by_id.items():
    try:
        # 同じ qty 比按分 (backfill と共通 helper 化推奨)
        total_qty = sum(t.get("qty", 1) for t in txns) or 1
        order_shipping = txns[0].get("shipping_usd", 0.0)
        for txn in txns:
            ship_share = order_shipping * txn.get("qty", 1) / total_qty
            sale_id = add_sale(
                ...,
                shipping_cost_usd=ship_share,
                ebay_fee_usd=0.0,
                ebay_order_id=order_id,
            )
            if sale_id:  # 新規 INSERT 成立時
                fulfillment_order_matcher.link_one_by_sale(sale_id)
        # ... 既存の DDP-B / 高額 EU alert 判定 (K2 Surgical で変更なし) ...
    except Exception as e:
        # 失敗 order を retry queue に積む (v2)
        INSERT OR IGNORE INTO sales_history_fetch_failures (ebay_order_id, last_error)
        VALUES (?, ?)
        logger.error(f"order {order_id} failed: {e}")
```

**間隔**: 既存の 30 分 polling をそのまま流用 (新規 task 追加せず、K2 Surgical / DRY)
**retry policy** (v2): 5 回連続失敗で Discord 通知 (eBay Manager webhook), 以降は `discord_notified=1` で再通知抑止 (フィードバック alert fatigue 防止)

## 7. ひも付け logic (`fulfillment_order_matcher.py`)

```python
# 擬似コード
def link_unmatched() -> int:
    """過去 fulfillment 全件を未マッチ sales_history と FIFO ひも付け. backfill 用."""
    # 未マッチ fulfillment 一覧 (古い順)
    unmatched_fulfillments = SELECT FROM purchase_confirmation_log
        WHERE fulfillment_kind='fulfillment'
          AND id NOT IN (SELECT purchase_confirmation_log_id FROM fulfillment_order_link)
        ORDER BY confirmed_at ASC

    count = 0
    for fulfillment in unmatched_fulfillments:
        # 同 ebay_item_id の未マッチ sales_history 最古
        sale = SELECT FROM sales_history
            WHERE ebay_item_id = fulfillment.ebay_item_id
              AND id NOT IN (SELECT sales_history_id FROM fulfillment_order_link)
              AND sold_at <= fulfillment.confirmed_at  # 売却が先、仕入が後
            ORDER BY sold_at ASC LIMIT 1
        if sale:
            INSERT INTO fulfillment_order_link (..., match_method='batch')
            count += 1
    return count

def link_one(ebay_item_id: str) -> Optional[int]:
    """リアルタイム 1 件マッチ. confirm_purchase 末尾呼び出し用."""
    # 同 ebay_item_id の最古未マッチ fulfillment と最古未マッチ sale をマッチ
    # 該当 fulfillment は呼び出し元 (たった今 INSERT) が指定するなら id 引数化検討
    ...
```

**FIFO 根拠**: 同 listing が複数回売れて複数回仕入された場合、現実の業務フローは「1 番目に売れたものを 1 番目に仕入れる」のが自然 (時系列対応)。例外は手動修正 UI で別 W 対応。

**未マッチの扱い**:
- fulfillment 仕入後にまだ対応する sales_history が無い (= eBay 注文取得タイミングが遅い) → confirm_purchase return dict に `matched_order_id=None` で UI 警告
- sales_history はあるが fulfillment が無い (= まだ仕入してない、または有在庫で売れた) → 警告不要 (正常な未対応売却)

## 8. UI 変更 (最小)

`tab_purchase_confirm.py`:
- 確定成功表示に「✅ eBay 注文 {id} に紐付け完了 (買い手: {country}, 売却日: {date})」追加
- 未マッチ時 (matched_order_id=None) は「⚠️ 対応する eBay 売却がまだ取得されていません。明日の自動取得後に自動ひも付けされます」と平易メッセージ

商品管理タブ等への可視化は本 W スコープ外 (別 W で「fulfillment_order_link 一覧 / 履行漏れ alert」を実装)。

## 9. テスト方針

`tests/test_fulfillment_order_link.py`:

1. **migration v47 冪等性**: init_db() 2 回連続実行で sales_history.ebay_order_id 列 + fulfillment_order_link table が保持
2. **backfill script dedupe**: 同じ ebay_order_id を 2 回 add_sale() しても INSERT OR IGNORE で重複しない
3. **FIFO マッチ順序**: 同 ebay_item_id で sales=[s1<s2], fulfillment=[f1<f2] (confirmed_at 順) → s1-f1, s2-f2 で 1:1 マッチ
4. **時系列ガード**: fulfillment.confirmed_at < sales.sold_at の組はマッチさせない (売却前の仕入は別物)
5. **リアルタイム 1 件マッチ**: confirm_purchase(kind='fulfillment') 後の return dict に `matched_order_id` が含まれ、fulfillment_order_link に 1 行追加されている
6. **既存 test 影響なし**: `test_purchase_confirm.py` の既存 case が PASS (kind='restock' / undo 系)

## 10. Q1 検証 (DoD 11 ステップ、実装後)

1. pytest test_fulfillment_order_link.py PASS
2. pytest 全件 (現状 ~1333+) regression なし
3. migration v47 を **本番 DB に 1 回 + 再起動冪等性確認** (2 回連続 init_db で sales_history.ebay_order_id 列保持)
4. backfill script を本番 DB で **dry-run** モード実装 (--dry-run で API 呼ぶが INSERT しない) → 期待件数 estimation
5. backfill 本実行 → sales_history 充填件数 + fulfillment_order_link マッチ件数を DB SELECT で実測
6. scheduler 再起動 → next 30 min polling で task_order_alert がエラーなく動く (scheduler.log 確認)
7. Streamlit 再起動 → 入荷確認タブで kind='fulfillment' を 1 件確定 → 「✅ eBay 注文 X に紐付け完了」表示確認
8. Playwright MCP で 1 往復 E2E 確認
9. retrospective code-reviewer 投入 (Q4、HIGH=0 まで)
10. Codex 2 段 review (W149 規模 = money-direct + 新規 API = 必須)
11. ROADMAP id=233 完了 marker + commit & push

## 11. 残リスク

| リスク | 影響 | 緩和策 |
|---|---|---|
| eBay API レート制限 hit | backfill 失敗、retry コスト | 2 chunk = 2 calls なので即 hit 不可、礼儀 sleep 2s |
| order の `transactions` 配列が multi-line (1 注文に複数商品) | 1 order に対し N sale 行 → fulfillment_order_link UNIQUE(sales_history_id) は OK だが、UNIQUE(purchase_confirmation_log_id) で 1 fulfillment が複数注文に対応するケース未対応 | 本 W では 1:1 限定、N:1 / 1:N は別 W |
| 既存 fulfillment 0 件 (W133-FU 未使用) | マッチ件数 0 で「動いてない」見え | backfill 完了後 user に「fulfillment 確定操作を行うと自動でひも付きます」明示 |
| ~~sales_history の line_item 単位 vs order 単位の混同~~ (v2 で解決) | ~~1 order に複数 transactions あるとき shipping/fee の按分が必要~~ | **v2 解決**: order_id で group → qty 比按分を §5 §6 で擬似コード明示. eBay fee は GetOrders 戻り値に含まれず別 W で取得、本 W では 0 初期化 (透明性) |
| task_order_alert の既存 logic 副作用 | DDP-B / 高額 EU alert に影響したら本番事故 | K2 Surgical (logic 改修なし、追加のみ) + retrospective review |
| ~~user が「リアルタイムマッチ希望」だがメール経由 task が deprecate されてないため scheduler に並走~~ (v2 で解決) | ~~sales_history への二重書き込み?~~ | **v2 解決**: `schedule_config.json` で `task_sales_tracking.enabled = false` に切替 + `stop_reason` 記録 (Q0). 完全廃止は別 W |
| ~~30 min polling 失敗 order の永久 skip~~ (v2 で解決) | ~~失敗した order が永遠に sales_history に入らない、sold/profit 集計が永久に不正確~~ | **v2 解決**: `sales_history_fetch_failures` retry queue table 追加. 次回 polling 先頭で再試行、5 回連続失敗で Discord 通知 |

## 12. 次のステップ (本セッション終了時)

- 本設計書 v1 をコミット (push せず、Codex review 後に v2 へ)
- 次セッションで Codex review 投入 → v2 改訂 → code-reviewer (Opus) → 実装着手
- 実装着手前に user に W149 着手判断を仰ぐ (Phase 0 Clarify 再確認)
