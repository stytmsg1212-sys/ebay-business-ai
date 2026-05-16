# SKU 用途ルール (常時適用 / 違反 = 品質事故)

出典: 2026-04-29 W7-A SKU 主キー崩壊事故 (migration v26 で `ebay_item_id` 単位化に修正) / 2026-04-30 assistant が `tools/ebay-manager/CLAUDE.md` の `(連番)` `(一意キー)` 記述を真に受けて「stock:01 active 58 件 = SKU 一意性崩壊」と誤判定。CLAUDE.md 自体の記述ミスが温床。

## SKU 用途は **2 つだけ**

1. **有在庫 / 無在庫 の判定** (prefix で判別)
2. **無在庫の場合、SKU 変換 → 仕入先候補 URL を得る**

これ以外の用途で SKU を使うのは **絶対禁止**。

## SKU 形式

| 種別 | SKU 形式 | 性質 |
|---|---|---|
| **有在庫** | `stock**` で始まる文字列 (stock:01 / stock1 / stock / stock01 / stock: 1 等、表記揺れあり) | **同一 SKU を多数 listing が持つのが正常** (在庫種別フラグであって集約キーではない。在庫数・識別は `ebay_item_id` 単位、SKU で束ねない) |
| **無在庫** | `ebay**_*****` (例: `ebayyh_p1221413657` / `ebayme_m32400850054` / `ebayPF_z587339852`) | SKU 変換 → 仕入先 URL (`tools/ebay-manager/sku_mapping_manager.py`) |

## 絶対禁止 (違反 = 品質事故)

- ❌ SKU を listing 一意キー (主キー / 重複検出キー) として使う
- ❌ `WHERE sku=?` で 1 listing を特定するクエリ
- ❌ `WHERE sku IN (...)` で複数 listing を抽出 (`ebay_item_id IN (...)` を使う)
- ❌ `GROUP BY sku` で listing 統計を集計
- ❌ `UNIQUE(sku)` 制約 / `PRIMARY KEY (sku)` 制約
- ❌ `JOIN ... ON a.sku = b.sku` (listing 1:1 紐付けには `ebay_item_id` を使う)
- ❌ `dict[sku] = listing` (Python 辞書のキー化、上書きで listing 消失)
- ❌ `set(skus)` での重複排除 (有在庫の正常複数共有が消える)
- ❌ 「同 SKU が複数 listing に存在 = 異常」と判定する
- ❌ SKU 重複を検出して警告/排除するロジック

## 必ず使うキー

- ✅ **listing 識別 = `ebay_item_id`** (eBay 側の一意 ID)
- ✅ migration v26 (2026-04-29) で `ebay_listings` テーブルは listing 単位化済
- ✅ 仕入先候補 URL = SKU から派生計算 (`sku_mapping_manager.generate_url()`)

## 許可される使い方 (用途 2 つに限定、2026-04-30 user 公認)

### 1. 在庫種別判定 (prefix で判別)

「`stock**` で始まる = 有在庫」「`ebay***` で始まる = 無在庫」の判定は OK。

```python
# OK: 種別フィルタ (listing 識別ではない)
def is_in_stock(sku: str) -> bool:
    return sku.startswith("stock")

def is_supplier_sourced(sku: str) -> bool:
    return sku.startswith("ebay")
```

```sql
-- OK: 種別フィルタ (集合に対するクエリ、特定 listing を狙わない)
SELECT * FROM ebay_listings WHERE sku LIKE 'stock%';
SELECT * FROM ebay_listings WHERE sku LIKE 'ebay%';
```

**判定の前提**: prefix 完全一致 (case-sensitive) のみで判定。正規化関数は **不要** (有/無在庫の二択判定にしか使わないため、表記揺れ吸収は本ロジックでは扱わない)。`STOCK01` のような大文字 SKU は現状仕様外 — 検出時は user に判断仰ぐ (勝手に正規化しない)。

### 2. 仕入先 URL 変換 (無在庫のみ)

`tools/ebay-manager/sku_mapping_manager.generate_url(sku)` で SKU → 仕入先 URL を派生計算。詳細: `project_sku_mapping.md` / `sku_conversion_ui_implementation.md`

### 禁止と許可の境界線

| ケース | 例 | 判定 |
|---|---|---|
| 種別フィルタ | `WHERE sku LIKE 'stock%'` | ✅ OK |
| URL 変換派生 | `generate_url(sku)` | ✅ OK |
| 1 listing 特定 | `WHERE sku = 'ebayyh_p1221413657'` | ❌ NG (`ebay_item_id=?` を使う) |
| 重複検出 | `GROUP BY sku HAVING count>1` | ❌ NG |
| UNIQUE 制約 | `UNIQUE(sku)` | ❌ NG |

## 違反検出時の対応

assistant が将来このルールに違反したら:
1. **即座に正直に user 報告** (隠さない、Q0 silent skip 禁止)
2. `feedback_sku_misuse_repeat_offense.md` に追記
3. 再発防止 hook / test を即作成

## 過去事故 (再発禁止)

| 日付 | 事故 | 修正 |
|---|---|---|
| 2026-04-29 | W7-A SKU 主キー崩壊 (設計時に SKU を listing 主キー扱い) | Phase 3 / migration v26 で `ebay_item_id` 単位化 |
| 2026-04-30 | SKU 一意性誤推論 (CLAUDE.md `(一意キー)` 記述を無批判参照) | CLAUDE.md / rules / memory 5 か所鏡像更新 + 本 rule 制定 |

## 関連 rule

- `md-files-can-be-wrong.md` — .md ファイルも誤りを含み得る (本事故の根因)
- `silent-skip-prevention.md` — Q0 サイレントスキップ禁止
- `karpathy-principles.md` — K0 仮定を明示
