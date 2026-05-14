# SQLite TIMESTAMP は UTC、JST 直書き禁止 (常時適用)

出典: 2026-05-02 W94 Phase 5 完了直前に assistant が SQL `WHERE called_at >= '2026-05-02'` で JST 日付直書き → 今朝の entries 全部 miss → 「supplier_sweep が fake success」と誤判定 → user push-back で訂正 (false alarm に 30 分消費)。

## ルール

SQLite の `CURRENT_TIMESTAMP` / `datetime('now')` は **常に UTC で保存される** (TZ 設定無視)。

本プロジェクト DB (`monitor.db`) の **全 timestamp カラム** (`called_at`, `created_at`, `started_at`, `finished_at`, `updated_at`, etc.) は UTC で保存されている。

### ❌ NG: JST 日付文字列で直接 query

```sql
-- 「今日 (JST) の entry を取得」のつもり、実際は UTC 比較で 00:00-09:00 JST 帯を miss
SELECT * FROM api_call_log WHERE called_at >= '2026-05-02';

-- 同じく今日朝 (JST 02:30 batch = UTC 5/1 17:30) を miss
SELECT * FROM task_execution_log WHERE started_at >= '2026-05-02 00:00';
```

### ✅ OK パターン (3 択)

#### A. 相対範囲 (`datetime('now', '-N hours')`) — **最も安全 + 簡潔**

```sql
-- 直近 12h (TZ 無関係)
WHERE called_at >= datetime('now', '-12 hours')

-- 直近 24h
WHERE called_at >= datetime('now', '-24 hours')

-- 直近 7d
WHERE called_at >= datetime('now', '-7 days')
```

#### B. JST → UTC 換算で書く (絶対日付が必要な場合)

```sql
-- 「今日 JST = 5/2」を UTC 換算 = 5/1 15:00 UTC ~ 5/2 15:00 UTC
WHERE called_at >= '2026-05-01 15:00:00' AND called_at < '2026-05-02 15:00:00'
```

#### C. JST へ shift してから比較 (DATE 関数で日付にまとめる時)

```sql
-- 「JST の日付ごと集計」
SELECT DATE(called_at, '+9 hours') AS jst_date, COUNT(*)
FROM api_call_log GROUP BY jst_date;

-- 「JST の今日のみ filter」
WHERE DATE(called_at, '+9 hours') = DATE('now', '+9 hours')
```

## SessionStart 時に DB を SQL で query する場合

時刻系を絡めるなら **必ず A (`datetime('now', '-N hours')`)** をデフォルトに。絶対日付が必要な時のみ B/C を選ぶ。

## 検証方法

- query 結果が空 / 想定より少ない → **timezone を疑う前に必ず以下を実行**:
  ```sql
  SELECT MIN(called_at), MAX(called_at), COUNT(*) FROM api_call_log
  WHERE called_at >= datetime('now', '-24 hours');
  ```
- entries が直近 24h で存在するのに自分の query で 0 件なら確実に timezone bug

## 関連事故

- 2026-05-02: assistant が `WHERE called_at >= '2026-05-02'` で 1891 entries (UTC 5/1 15:00 以降) を全部 miss → 「scheduler 死亡 / fake success / Q0 violation」と誤判定 → 30 分の false alarm 調査時間 + user 信頼度低下

## 関連 rule

- `karpathy-principles.md` — K0 仮定を明示 (本事故は SQLite TZ assume の典型)
- `md-files-can-be-wrong.md` — query 結果も assume せず実物検証 (counter-evidence で sanity check)
- `silent-skip-prevention.md` — false positive で「silent skip 検出」と騒ぐのも害 (今回の反省)
