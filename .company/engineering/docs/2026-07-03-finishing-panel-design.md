# 商品仕上げパネル (3タブ統一リファクタリング) 設計書 — 確定版

- 日付: 2026-07-03 (user 承認済み: 画面イメージ OK + 判断 3 点回答済み)
- W 番号: W314 (system_improvements.json)
- モックアップ: `2026-07-03-finishing-panel-mockup.html` (同ディレクトリ)
- 設計: code-architect (Opus 4.8) → main (Fable 5) レビュー → user 承認 2026-07-03

## 0. user 確定判断 (2026-07-03)

1. **500px 未満の仕入先画像の「そのまま採用」**: 警告のみで強行可 (合成誘導はしない。警告バッジ表示 + user 判断)
2. **変更履歴の粒度**: description 含め **全文保存** (復元可能性を優先、DB 肥大は許容)
3. **数量は「コンテンツ反映」1 ボタンに含める** (title/desc/画像/rank/qty = T2 束)。価格・送料のみ隔離 (T3)

## 1. コンセプト

listing (`ebay_item_id`) を 1 つの編集対象とみなし、**どのタブ (商品管理/在庫監視/仕入先候補) から入っても同一の「商品仕上げパネル」に合流**させる。パネルは (1) フィールドを意味グループに分割し必要な所だけ開く、(2) 全変更を dirty-flag で追跡し「何が変わるか」プレビュー付きでコンテンツ反映を 1 ボタンに集約、(3) 価格・送料 (money-direct / Q4 T3) は物理的に隔離し別ボタン・2 段確認。既存の followup 共有 render・画像/description パイプライン部品は温存し、パネルはオーケストレーション層 (K1: 完全共通化は Phase 5 まで遅延)。

## 2. 識別と入口

- 識別キーは **ebay_item_id のみ** (SKU 主キー禁止 = sku-rules)。session namespace `pf_{eid}_*`
- 入口 3 経路が同一の `render_finishing_panel(eid, config, *, candidate_id=None, candidate_url=None, source_tab)` に合流
- cid は補充フロー由来のパイプライン継続用の副次キーに降格 (現行 followup の cid 主キーを eid 主キーへ移行)

## 3. フィールドグループと反映先

| グループ | フィールド | 反映先 | tier |
|---|---|---|---|
| コンテンツ | タイトル (80字 + 原産国ガード) | revise_item_title | T2 |
| コンテンツ | description (生成/編集/取得) | revise_item_description | T2 |
| コンテンツ | 画像 (3モード) | revise_item_pictures (+EPS) | T2 |
| 状態 | ランク + ConditionDescription | revise_item_condition | T2 |
| 在庫 | 数量 | revise_inventory_quantity | T2 (束に含める・確定判断3) |
| 仕入先 | 仕入先 URL | DB のみ | T1 |
| 💰 価格・送料 (隔離) | 価格/送料/+each/BP | _apply_to_ebay | **T3** 別ボタン 2 段確認 |

## 4. 画像 3 モード

1. **① AI 合成** (既定・既存フロー無改変): hero picker → Gemini 3 候補 → EPS → revise
2. **② そのまま採用** (新規): `fetch_supplier_images_all` の生画像から選択 → DL → **EPS 経由** (hotlink 直渡し不可、仕入先側 URL 消滅対策) → revise 全置換。**500px 下限チェックを新設、警告のみで強行可 (確定判断1)**
3. **③ メイン 1 枚だけ差し替え** (新規): `ebay_image_fetcher._api_image_urls` (GetItem) で現行全画像取得 → `[new_main] + existing[1:]` を revise で再送 (PictureDetails は全置換のみのため既存を明示再送で保持)。ReviseItem 上限 12 枚ガード維持

## 5. 状態遷移

編集 → `pf_{eid}_<field>_dirty` → 変更プレビュー (before→after 表) → [コンテンツ反映 (T2・1 ボタン)] → 各 revise 実行 → **listing_content_change_log に before/after 全文記録** → 成功フィールドの dirty クリア、失敗は残す (Q0: 部分成功を実値表示)。価格・送料は別ボタン。

## 6. 新規/修正ファイル

新規: `tabs/_finishing_panel.py` / `tabs/_finishing_panel_state.py` (純関数・unit test 可) / `tabs/_adopt_candidate.py` (採用ロジック単一化) / `monitor/listing_content_change_log.py`

修正: `tabs/_supplier_photo_pipeline.py` (3モード) / `monitor/ebay_image_fetcher.py` (全枚数返却) / `tabs/_supplier_followup_section.py` (タイトル欄追加 → Phase 2 でパネル化) / `tab_product_management.py` (5 グループ再編 + 性能) / `tab_inventory_monitor.py` / `tab_supplier_candidates.py` / `monitor/database.py` (migration)

### 監査ログ API 契約 (実装間の共有契約)

```python
# monitor/listing_content_change_log.py
def log_content_change(ebay_item_id: str, field: str,
                       before_value: str | None, after_value: str | None,
                       *, source_tab: str | None = None,
                       candidate_id: int | None = None,
                       success: bool = False,
                       ebay_ack: str | None = None) -> int: ...
# field: 'title'|'description'|'images'|'rank'|'quantity'
# before/after: 全文保存 (確定判断2)。images はURL配列を JSON 文字列化
```

### migration v88 (番号は実装時に PRAGMA user_version 再確認)

```sql
CREATE TABLE IF NOT EXISTS listing_content_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_item_id TEXT NOT NULL,
    field TEXT NOT NULL,
    before_value TEXT, after_value TEXT,
    source_tab TEXT, candidate_id INTEGER,
    success INTEGER NOT NULL DEFAULT 0,
    ebay_ack TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- UTC (SQL側、Python bind 禁止)
);
CREATE INDEX IF NOT EXISTS idx_lccl_eid ON listing_content_change_log(ebay_item_id);
```
Q2 冪等 (try/except OperationalError + user_version bump、init_db 2 回実行テスト必須)。

## 7. 性能設計 (Phase 4)

- `_cd_fetch_all_products` ttl=3 撤廃 or 60 (db_version bump の実態を事前実測)
- N+1 → 表示中 eid 群を `WHERE ebay_item_id IN (...)` 一括 SELECT (sku-rules 適合)
- CSS 一回注入 (session_state センチネル)、巨大 HTML は既定閉 expander 内で遅延生成
- `render_finishing_panel` を @st.fragment 化、採用時 scope="app" をパネル scope へ縮小

## 8. Phase 計画

| Phase | 内容 | 工数 | tier |
|---|---|---|---|
| 1 | タイトル欄 + 画像3モード + migration v88 監査ログ | 3-4d | T3 (eBay API 書込経路新設 + migration) = code-reviewer HIGH=0 + Codex 2段 + live GetItem verify |
| 2 | パネル shell + dirty/プレビュー/反映集約 + 価格送料隔離 | 4-5d | T3 (money 隔離境界) |
| 3 | 採用ロジック単一化 + 商品管理 5 グループ再編 + 在庫監視 2 概念分離 | 3-4d | T2 |
| 4 | 性能 (キャッシュ/N+1/CSS/fragment) | 2-3d | T2 |
| 5 | photo_pipeline 2 実装の収斂 (任意・負債返済) | 大 | T2、3箇所目の利用確定後 |

各 Phase DoD (Q1): pytest + Streamlit 再起動 + Playwright E2E + **eBay GetItem 実反映 verify** + DB SELECT。

## 9. 制約

- SKU 規約 / Country of Origin 記載禁止ガード維持 / ReviseItem 12 枚上限 / https 必須
- ItemSpecifics の revise は未実装 (本設計スコープ外、必要になれば別 W)
