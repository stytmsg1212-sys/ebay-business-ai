---
name: db-migration-rules
description: SQLite DB migration 冪等性ルール (try/except OperationalError、DROP/DELETE one-shot 化、本番 DB 直接書込時 24h retrospective review)
type: rule
---

# DB Migration 冪等性ルール (常時適用 / Q2)

出典: 2026-04-25 W14 v18 migration バグ事故 (customs_requests 95 件消失)、翌日 video_learning UPDATE 直接実行事故 (review 抜き 50/100 点判定)

## 冪等性必須

- 全 `ALTER TABLE` は `try/except sqlite3.OperationalError` でラップ (重複適用で落ちないこと)
- `init_db()` を **2 回連続実行してデータ保持** を verify する自動テスト必須
- DB スキーマ変更後は **code-reviewer 再投入** (修正前 review だけでは不十分、特に DROP/ALTER/DELETE/RENAME)

### 必須冪等性テスト (変更後すぐ実行)

```python
from monitor.database import init_db, get_conn
init_db()
with get_conn() as c:
    c.execute("INSERT INTO <target_table> (...) VALUES (...)")
init_db()  # 再実行
with get_conn() as c:
    rows = c.execute("SELECT COUNT(*) FROM <target_table>").fetchone()
    assert rows[0] >= 1, "データが消失！冪等性違反"
```

## ❌ NG: init_db に DROP / DELETE を書く

```python
# init_db は app/scheduler 起動毎に呼ばれる = 起動毎にデータ wipe
def init_db():
    for tbl in ('customs_requests', ...):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")  # ← 95 件消失の原因
    conn.execute("CREATE TABLE customs_requests (...)")
```

## ✅ OK パターン (2 択)

### A. one-shot script (推奨)

```python
# scripts/fix_v18_glob.py — 一回だけ手動実行
with get_conn() as c:
    c.execute("ALTER TABLE foo RENAME TO foo_old")
    c.execute("CREATE TABLE foo (...新スキーマ...)")
    c.execute("INSERT INTO foo SELECT * FROM foo_old")
    c.execute("DROP TABLE foo_old")
# init_db 自体は変更しない
```

### B. PRAGMA user_version で track

```python
def init_db():
    cur = conn.execute("PRAGMA user_version").fetchone()[0]
    if cur < 19:
        # v19 migration を一度だけ
        ...
        conn.execute("PRAGMA user_version = 19")
```

## 本番 DB 直接書込ルール (INSERT/UPDATE/DELETE/ALTER)

**原則禁止**。やむを得ず実行する場合は **24h 以内に retrospective code-reviewer 投入** + 補正アクション必須。

### 直接実行が許される例外

- READ ONLY (SELECT) — 観測のみ
- ROLLBACK 用 (障害復旧、user 承認済)
- 単一テストデータ削除 (test_xxx prefix)

### 直接実行が禁止される対象

- 本番ステータス遷移 (`status='failed' → 'pending'` 等)
- 数量・金銭関連カラム (price, profit, quantity)
- 外部キー参照のあるカラム
- スキーマ変更 (ALTER, DROP)
- 複数行に影響する UPDATE/DELETE

### やむを得ず直接実行する時の 6 step

⚠️ **安全ゲート (COUNT 確認 / rowcount 確認 / 中断条件) は `assert` でなく明示 `if not cond: raise` で書く** — `python -O` 実行で assert はバイトコードから除去され、破壊系 one-shot が無防備になる (出典: 2026-07-02 idem1 削除 retrospective review M1)。

1. SELECT で対象行を dump (rollback 用 snapshot)
2. WHERE を 1 件に絞って試行 → 期待通り 1 件更新を確認
3. 残りを実行
4. 結果を SELECT で再確認
5. **24h 以内に retrospective code-reviewer** (本 rule + 関連 feedback memory を context に渡す)
6. HIGH 指摘あれば即補正 or rollback

## kill switch 併用

データ修正中・原因不明状態は **関連 task の `tasks_enabled.<task>.enabled = false`** で scheduler 一時停止。恒久対策実装後に再 enable。

## ROADMAP 連携

直接実行が必要になる事象 = **仕組み欠落の signal**。即 `/add_s` か `data/system_improvements.json` で W 番号付き登録し、恒久対策を ROADMAP 化する。

## hook 強制

PreToolUse hook (`quality-gate.sh`) で migration ファイル内の `DROP TABLE` / `DELETE FROM` / `ALTER TABLE` 無 try/except は **物理 BLOCK** される。
