---
name: w158-image-pipeline-shared
description: 個別出品 Step 2.5/2.6/2.7 (ロゴプレート合成 + 背景グレー統一 + EPS upload) を商品管理タブ / 仕入先候補 description 反映 UI にも統合する共通 helper 化 + ReviseItem 拡張 (description / image / both)
layer: wiki
updated: 2026-05-23
revision: v2.2 (Phase 5 Codex GPT-5.5 1 周 HIGH 4 件 fix。第 10 次「内部 + Codex で money-direct silent gap 突破」事例)
sources:
  - tools/ebay-manager/tabs/tab_individual_listing.py L536-1037 (_render_hero_compose_section / _render_additional_photoroom_section / _render_eps_upload_section / _do_hero_compose / _do_process_additional_images / _upload_processed_to_eps_sync の現状実装)
  - tools/ebay-manager/tabs/tab_individual_listing.py L99-150 (_init_session_state, _SS prefix と全 key 一覧)
  - tools/ebay-manager/tabs/tab_individual_listing.py L155-206 (_clear_from_step, hero/additional/processed cascade clear)
  - tools/ebay-manager/tabs/tab_individual_listing.py L1879-1897 (_resolve_listing_image_urls 優先順位契約)
  - tools/ebay-manager/tabs/_supplier_description_pipeline.py L304-332 (apply_description_to_ebay 現状 = description のみ ReviseItem)
  - tools/ebay-manager/tabs/_supplier_description_pipeline.py L335-545 (render_supplier_description_section、_SS = "sup_desc_pipeline_")
  - tools/ebay-manager/tabs/tab_product_management.py L1188-1322 (_render_url_direct_description_section、url_key/result_key 命名)
  - tools/ebay-manager/monitor/ebay_client.py L570-676 (revise_item_pictures + PictureDetails XML builder)
  - tools/ebay-manager/monitor/ebay_client.py L679-793 (revise_item_description + CDATA escape)
  - tools/ebay-manager/monitor/image_composer_photoroom.py L1-80 (compose_cover_with_photoroom、PhotoroomResult)
  - tools/ebay-manager/monitor/image_composer_gemini.py L161-219 (generate_hero_candidates、HeroGenerationResult、k=3 並列)
  - tools/ebay-manager/monitor/ebay_eps_uploader.py L78-218 (upload_images_parallel + DB cache eps_upload_cache)
  - tools/ebay-manager/monitor/credentials.py L45-58 (get_ebay_credentials / ebay_credentials_ok)
related:
  - 2026-05-22-W153-rival-per-listing-detection-design (兄弟 W、独立 scope)
  - 2026-05-21-W149-ebay-orders-fetch-fulfillment-link-design (兄弟 W、独立 scope)
  - .claude/rules/karpathy-principles.md (K0/K1/K2/K3)
  - .claude/rules/silent-skip-prevention.md (Q0)
  - .claude/rules/md-files-can-be-wrong.md (R-1 既存実装の一次情報照合)
  - tools/ebay-manager/CLAUDE.md (eBay 出品ルール / PictureURL 12件上限)
genre: image-pipeline
metadata:
  type: design
  wiki_type: synthesis
---

# W158 画像加工パイプライン統合 設計書

**作成日**: 2026-05-23
**ROADMAP id**: 242 (W158、`data/system_improvements.json` L1462)
**設計フェーズ**: Q3 構造化フロー Phase 4 成果物 (Phase 5 = code-reviewer + Codex GPT-5.5 2 段 review に進む)
**business critical 度**: 中 (描画 quality 一貫性 / 仕入先採用 → 反映の所要操作削減。eBay account 直接的な金銭事故 risk は低い。ただし image-only ReviseItem は失敗時に **商品の画像が消える** 反映崩れ risk があり Q0 silent skip 厳禁)

---

## 1. 概要

`tab_individual_listing.py` で完成している 3 段画像加工 (Photoroom + Gemini ロゴプレート合成 → 全画像 Photoroom 背景統一 → eBay EPS upload) を `monitor/image_pipeline_shared.py` (pure 関数群) + `tabs/_image_pipeline_ui.py` (Streamlit UI section、prefix 引数で session_state namespace 化) に **抽出+整理**。商品管理タブの新規 URL 直接投入セクションと仕入先候補 description 反映セクションから同じ UI 関数を呼び出して使う。同時に `apply_listing_update(ebay_item_id, *, description_html=None, picture_urls=None)` を新設し、description / image / both の 3 反映パターンを **1 度の ReviseItem** で処理する。

「途中で確認を挟む」フロー (1 枚目 hero は 3 候補から user が 1 枚採用 → 2 枚目以降は user が「実行」を押す → preview → EPS upload → 反映) は個別出品と完全同等。opt-in only (明示ボタン経由のみ実行、自動課金 0)。

---

## 2. スコープ

### 含まれる
- **新規 helper module** `monitor/image_pipeline_shared.py` (~250 LOC、pure 関数 4 つ + dataclass 1 つ)
- **新規 UI module** `tabs/_image_pipeline_ui.py` (~280 LOC、`render_image_pipeline_section(prefix, source_urls, sku_hint, *, eps_apply_callback=None)`)
- **ReviseItem 拡張不要 (v2 修正)**: `monitor/ebay_client.py` には新関数を追加しない。既存 `revise_item_description` / `revise_item_pictures` をそのまま使い、それぞれ atomic な ReviseItem 呼出。code-reviewer HIGH-2 (Ack=Warning silent reject Q0) + HIGH-4 (3 関数並走 drift) の 2 件を同時解消。
- **apply_description_to_ebay 拡張**: `tabs/_supplier_description_pipeline.py` の `apply_description_to_ebay` を `apply_listing_update_to_ebay(ebay_item_id, *, description_html=None, picture_urls=None)` にリネーム + 旧名を thin wrapper alias (k2 surgical)。本関数は **既存 `revise_item_description` + `revise_item_pictures` を順次呼ぶ** (両方指定時は description → pictures の順、各 independent atomic、failure independent)。trade-off: HTTP RTT 1 回増 (200-400ms) を許容、Q0 silent skip 防止優先。
- **個別出品 refactor**: `_render_hero_compose_section` / `_render_additional_photoroom_section` / `_render_eps_upload_section` の **UI ロジックを shared に置換**。`_do_hero_compose` / `_do_process_additional_images` / `_upload_processed_to_eps_sync` も shared の pure 関数を呼ぶ薄い wrapper に縮小 (~ -120 LOC)
- **商品管理タブ統合**: `_render_url_direct_description_section` の warning を撤去し、result 表示直下に `render_image_pipeline_section(prefix=f"pm_url_direct_{eid}_w158_", ...)` 呼出を追加 (v2.1 修正 MED-1、§3.4 prefix 命名規則と統一)
- **仕入先候補 UI 統合**: `render_supplier_description_section` の warning を撤去し、preview 直下に同 section 呼出を追加
- **3 反映ボタン**: image / description / both (有効化条件は §5.3 に詳述)
- **pytest 単一 file** `tests/test_image_pipeline_shared.py` (~120 LOC、HTTP mock 経由)
- **既存 test 互換維持**: `test_revise_item_pictures.py` / `test_supplier_photo_pipeline.py` は変更しない (旧 API alias で通る)

### 含まれない (K1)
- 画像加工 helper 自体の挙動変更 (Photoroom API パラメータ / Gemini モデル等は現行維持)
- DB schema 変更 (本 W は migration なし、`eps_upload_cache` 既存テーブルを流用)
- 個別出品の Step 1-2 / Step 3-5 (scrape / 出品設定 / verify / add) refactor
- 商品管理タブの仕入先候補 (採用済) セクション以外の UI 改修
- 画像加工結果の DB 永続化 (現状は file system + session_state 維持、永続化が要れば別 W)
- 自動 hero 選択 (常に user 3 候補から 1 枚採用、user 明示要件)
- 画像加工コスト recording / monthly summary (別 W にて)
- ReviseFixedPriceItem への切替 (本 W は ReviseItem で統一、両者は eBay 側でほぼ等価)

---

## 3. 既存システム分析

### 3.1 個別出品 Step 2.5/2.6/2.7 の現状実装 (一次情報)

```
[Step 1 URL入力] → [Step 2 scrape + 画像選択 (selected_image_urls[0] = hero source)]
                          │
                          ▼
              [Step 2.5 _render_hero_compose_section]
                  │ ├─ _do_hero_compose(source_url, force_regenerate=False)
                  │ │     ├─ 既存 hero_W*.png + _studio.png あれば API skip 復元
                  │ │     ├─ Photoroom /v2/edit (~10s, $0.02)
                  │ │     └─ Gemini 3 並列合成 (~30-40s, $0.12 = $0.04*3)
                  │ └─ user が 1 候補採用 → hero_selected_path にセット
                          │
                          ▼
              [Step 2.6 _render_additional_photoroom_section]
                  │ ├─ urls = selected_urls - {hero_source_url}
                  │ ├─ _do_process_additional_images(urls, force_regenerate=False)
                  │ │     ├─ 既存 _additional_NN.png あれば skip 復元
                  │ │     └─ Photoroom /v2/edit 並列 (3 worker, $0.02/枚)
                  │ └─ additional_processed に list[{source_url, path}] 保存
                          │
                          ▼
              [Step 2.7 _render_eps_upload_section]
                  │ ├─ paths = [hero] + [a.path for a in additional]
                  │ ├─ upload_images_parallel(paths, max_workers=3, use_cache=True)
                  │ │     └─ DB cache eps_upload_cache (file_hash) で重複 skip
                  │ └─ processed_image_urls に list[str] (最大 24 件) 保存
                          │
                          ▼
              [Step 5 verify/add で _resolve_listing_image_urls 経由で processed > selected > supplier の優先順位で PictureURL 採用]
```

**session_state key (_SS = "il_")**:
- `il_selected_image_urls`: list[str]
- `il_hero_source_url`: str
- `il_hero_candidates`: list[{plate_id, score, path, reasoning}]
- `il_hero_selected_path`: str (絶対 path)
- `il_hero_studio_path`: str
- `il_additional_processed`: list[{source_url, path}] or None
- `il_processed_image_urls`: list[str]

**file system レイアウト** (`data/hero_candidates/{sku}/`):
- `_source.jpg` / `_studio.png` / `hero_W3.png` 等 / `_additional_00.png` 等

### 3.2 既存 ReviseItem 経路 (一次情報)

| 関数 | 場所 | XML | 用途 |
|---|---|---|---|
| `revise_item_description` | `monitor/ebay_client.py:702` | `<Item><ItemID/><Description><![CDATA[...]]></Description></Item>` | 仕入先候補採用後 / URL 直接投入で description のみ更新 |
| `revise_item_pictures` | `monitor/ebay_client.py:593` | `<Item><ItemID/><PictureDetails><PictureURL/>...</PictureDetails></Item>` | W115 H-1 で実装、active listing の画像差替 |
| `revise_item_sku` | `monitor/ebay_client.py:520` | `<Item><ItemID/><SKU/></Item>` | SKU 変更 |

**重要な実装上の事実** (md-files-can-be-wrong R-1 で一次情報照合済):
- `revise_item_description` のみ **CDATA premature close 防止** (`]]>` → `]]]]><![CDATA[>`) を実装。`revise_item_pictures` には不要 (URL に CDATA breaker は来ない)
- 3 関数とも **`ET.ParseError` を graceful に success:False 化** (eBay が HTML エラーページを 200 で返すケースの crash 防止、W148 Codex HIGH の同型対策)
- 3 関数とも **`_resolve_active_token`** で OAuth access token を auto-refresh
- `revise_item_pictures` は **PictureURL 12 件超で error 返却** (silent drop 防止)。**ReviseItem は 12 件上限**である点に注意 (一方 `ebay_lister.py` の AddFixedPriceItem は 24 件まで許容)

### 3.3 商品管理 / 仕入先候補の現状

- `tab_product_management.py:1188 _render_url_direct_description_section`: scrape → rank → description 生成までは個別出品同等 → result 表示 → `apply_description_to_ebay(eid, description_html)` で **description のみ** ReviseItem。L1213-1226 に W158 warning コメントあり。
- `_supplier_description_pipeline.py:335 render_supplier_description_section`: L366-377 に W158 warning コメントあり。挙動は description のみ ReviseItem。

**両者とも image_urls は `result["image_urls"]` (scraped raw URL list) を保持しているが、image 加工は未実装**。

### 3.4 _SS prefix 衝突調査結果

| ファイル | prefix | 用途 |
|---|---|---|
| `tab_individual_listing.py` | `il_` | 出品 workflow 全般 |
| `_supplier_description_pipeline.py` | `sup_desc_pipeline_` | 採用済 supplier_candidate per-candidate |
| `tab_product_management.py` (URL 直接) | `pm_url_direct_` | per-ebay_item_id |
| `tab_product_management.py` (商品 hero / フィルタ等) | `pm_` ほか | per-ebay_item_id |

W158 で導入する shared UI は **caller から prefix 引数を受け取って自身の key を name-space 化** する。caller 側の prefix 命名規則 (v2 修正 HIGH-1):
- 個別出品: `il_` 維持 (互換のため、内部 suffix も既存と同じ)
- 商品管理 URL 直接: `pm_url_direct_{eid}_w158_` (**既存 `pm_url_direct_input_{eid}` / `pm_url_direct_result_{eid}` と同 namespace 配下**、source URL 変更検知時の cascade clear を一意化可能)
- 仕入先候補: `sup_desc_pipeline_{candidate_id}_w158_` (**既存 `sup_desc_pipeline_*_{candidate_id}` と同 namespace 配下**)

**cascade clear 仕様** (v2 追加 / v2.1 補強 MED-2): source_urls[0] が変化した時、caller 側で `clear_pipeline_keys(prefix)` (v2.1 で `_image_pipeline_ui.py` が export する新規 helper、~15 LOC) を呼んで shared 内 hero/additional/processed/eps の全 key を一括クリアする責任を caller が持つ。shared 内部の cascade clear は実装しない (HIGH-1 二重作用回避)。

**caller 側実装の具体** (v2.1 追加):
- 商品管理 URL 直接: `_render_url_direct_description_section` 内、`url_input.strip() != st.session_state.get(url_key, "")` 検知時に `clear_pipeline_keys(prefix=f"pm_url_direct_{eid}_w158_")` を呼ぶ
- 仕入先候補: `render_supplier_description_section` 内、prefetch 結果の url が変わった時 (既存 sk_prefetch 破棄経路と同タイミング) に同 helper を呼ぶ
- 個別出品: 既存 `_clear_from_step(1)` の step2_keys 処理を **shared module 経由に置換せず**、prefix=`il_` で同等動作を維持 (互換、refactor 最小化)

---

## 4. 作成 / 修正 / 削除ファイル一覧

| 種別 | ファイル | LOC 概算 | 役割 |
|---|---|---|---|
| **新規** | `monitor/image_pipeline_shared.py` | +250 | pure 関数 (`compose_hero_candidates_cached` / `unify_additional_backgrounds_cached` / `upload_to_eps_cached` / `resolve_final_picture_urls`) + dataclass |
| **新規** | `tabs/_image_pipeline_ui.py` | +280 | Streamlit UI section (`render_image_pipeline_section(prefix, source_urls, sku_hint, ebay_item_id, description_html=None, on_apply_image=None, on_apply_description=None, on_apply_both=None)`) |
| **新規** | `tests/test_image_pipeline_shared.py` | +120 | pure 関数の unit test (Photoroom/Gemini/EPS の HTTP mock) |
| ~~新規~~ | ~~`tests/test_revise_item_listing.py`~~ | ~~+80~~ | **v2 削除**: 新関数撤回、既存 test_revise_item_description / test_revise_item_pictures をそのまま流用 |
| ~~修正~~ | ~~`monitor/ebay_client.py`~~ | ~~+80~~ | **v2 削除**: 新関数撤回、既存 2 関数を順次呼ぶ設計に変更 (HIGH-2 + HIGH-4 同時解消) |
| 修正 | `tabs/_supplier_description_pipeline.py` | +70 / -10 | `apply_listing_update_to_ebay` 新設 (既存 2 関数の sequencer ~60 LOC) + `apply_description_to_ebay` を thin wrapper alias 化 + shared section 呼出追加 + cascade clear 呼出追加 (v2.1 LOC 修正 MED-3) |
| 修正 | `tabs/tab_product_management.py` | +30 / -5 | W158 warning 撤去 + shared section 呼出追加 |
| 修正 | `tabs/tab_individual_listing.py` | +80 / -250 | Step 2.5/2.6/2.7 を shared section 呼出に置換 |
| 修正 | `data/system_improvements.json` | id=242 status 更新 | "未着手" → "実装中" → "完了" |

**合計** (v2.1 修正): +890 / -265 ≒ net +625 LOC (新関数撤回で ebay_client.py +80 を削除、sequencer ロジック追加で _supplier_description_pipeline.py +30 を加算、`clear_pipeline_keys` helper +15 を _image_pipeline_ui.py に追加)

### 4.1 削除 (K1 surgical)
削除対象なし。旧 API は **互換 alias で残置** (K2 Surgical、既存 test 影響 0)。

---

## 5. コンポーネント設計

### 5.1 `monitor/image_pipeline_shared.py` (pure 関数群)

**設計原則**: session_state 非依存、入出力は dataclass / 純データ。Streamlit / st.* 一切呼ばない。

```python
@dataclass(frozen=True)
class HeroCandidate:
    plate_id: str
    score: float
    path: Path
    reasoning: str

@dataclass(frozen=True)
class AdditionalProcessed:
    source_url: str
    path: Path

@dataclass(frozen=True)
class EpsUploadOutcome:
    success: bool
    eps_urls: list[str]                  # 元順序保持
    failed: list[tuple[str, str]]        # (local_filename, error)
    skipped_cache_hits: int

def compose_hero_candidates_cached(
    source_url: str, out_base: Path, *,
    force_regenerate: bool = False, k: int = 3, max_parallel: int = 3,
) -> tuple[list[HeroCandidate], Optional[Path]]:
    """Returns (candidates, studio_path).
    既存 hero_W*.png + _studio.png 両方あり force_regenerate=False なら API skip。
    Photoroom/Gemini 失敗時は candidates=[] を返す (例外は呼出元に raise しない)。
    """

def unify_additional_backgrounds_cached(
    urls: list[str], out_base: Path, *,
    force_regenerate: bool = False, max_workers: int = 3,
) -> list[AdditionalProcessed]:
    """既存 _additional_NN.png が len(urls) 枚以上 ある + force_regenerate=False で skip 復元。
    失敗 URL は結果に含めず caller が部分失敗判定 (len(result) < len(urls))。
    """

def upload_to_eps_cached(
    paths: list[Path], *, max_workers: int = 3, use_cache: bool = True,
) -> EpsUploadOutcome:
    """存在しない path は missing 扱いで failed に追加 (silent drop 防止 Q0)。"""

def resolve_final_picture_urls(
    *, processed_eps_urls: list[str], selected_raw_urls: list[str],
    fallback_raw_urls: list[str], cap: int = 12,
) -> tuple[list[str], list[str]]:
    """Returns (kept, dropped). v2 修正 HIGH-3: silent drop 防止のため
    dropped を明示的に返す。caller (UI section) は len(dropped) > 0 時に
    st.warning(f"{len(dropped)} 枚は ReviseItem 上限超過のため反映されません.
    AddFixedPriceItem は 24 枚許容ですが本経路は ReviseItem (12 枚) です") を
    必須表示する責任を負う。
    processed > selected > fallback 優先順位で cap 件まで kept、それ以降は dropped。
    non-https は除外 (eBay reject 仕様)。
    """
```

### 5.2 `tabs/_image_pipeline_ui.py` (Streamlit UI section)

```python
def render_image_pipeline_section(
    *, prefix: str, source_urls: list[str], sku_hint: str,
    ebay_item_id: Optional[str] = None, description_html: Optional[str] = None,
    on_apply_image: Optional[Callable[[list[str]], dict]] = None,
    on_apply_description: Optional[Callable[[str], dict]] = None,
    on_apply_both: Optional[Callable[[str, list[str]], dict]] = None,
) -> None:
    """3 タブ共通の画像加工 + 反映 section.

    Step A: hero 合成ボタン (再使用/再生成 $0.14)
    Step B: 3 候補から 1 採用
    Step C: 2 枚目以降の背景統一ボタン ($0.02 x N)
    Step D: EPS upload ボタン
    Step E: 反映 3 button (image-only / description-only / both)

    session_state 全 key は f"{prefix}{suffix}" で衝突回避.

    v2 修正 HIGH-5 credentials guard 経路:
    - section 描画前に `get_photoroom_credentials()` / `get_gemini_credentials()` / `ebay_credentials_ok()` を check
    - 未設定 → `st.warning("⚠️ 画像加工に必要な API key が未設定です: <list>")` + return (button 表示せず)
    - hero compose / additional / EPS upload は別々の credentials を必要とするが、本 W では一括 check (個別 disable は K1 out-of-scope)

    v2 修正 HIGH-3 PictureURL cap warning:
    - resolve_final_picture_urls の戻値 dropped が len > 0 なら st.warning() で必須表示
    - 「{N} 枚は ReviseItem 上限超過のため反映されません」

    v2 修正 HIGH-2 「両方反映」path:
    - on_apply_both callback 内で apply_listing_update_to_ebay(eid, description_html=desc, picture_urls=urls) を呼ぶ
    - apply_listing_update_to_ebay は内部で revise_item_description + revise_item_pictures を順次呼ぶ
    - 戻値 dict に updated:{description: bool, pictures: bool} (各 step の独立 success フラグ)
    - 片方失敗時 UI は ✅成功 + ❌失敗 を per-step 表示 (Q0 silent skip 防止)
    """
```

### 5.3 反映ボタン 3 種の有効化条件マトリクス

| 反映種別 | 必要状態 | button label |
|---|---|---|
| image-only | `processed_image_urls` ≥ 1 AND `ebay_item_id` 非空 | "画像だけ eBay に反映" |
| description-only | `description_html` 非空 AND `ebay_item_id` 非空 | "説明文だけ eBay に反映" |
| both | image AND description 両方 ready | "両方 eBay に反映 (1 度の ReviseItem)" |

### 5.4 `apply_listing_update_to_ebay` 設計 (v2 修正: 新関数撤回、既存 2 関数の sequencer)

```python
def apply_listing_update_to_ebay(
    ebay_item_id: str, *,
    description_html: Optional[str] = None,
    picture_urls: Optional[list[str]] = None,
) -> dict:
    """description / picture / both の 3 path を既存 2 関数の sequencer として実装.

    v2 修正 (HIGH-2 + HIGH-4):
    - 新関数 revise_item_listing は撤回
    - 既存 revise_item_description / revise_item_pictures を順次呼ぶ
    - 両方指定時: description → pictures の順 (描画影響度に従い description 先)
    - 各 ReviseItem は独立 atomic、片方の Ack=Warning が他方に影響しない
    - HTTP RTT は 1→2 回 増 (200-400ms penalty)、ただし Q0 silent skip 防止優先

    Pre-validation:
      - 両方 None → success=False
      - picture_urls > 12 件 → success=False (revise_item_pictures 内で reject)
      - non-https URL → success=False (同上)
      - description 空 → description 経路 skip (両方経路では None 扱い skip)

    Returns:
      {'success': bool,                # 指定された全 step success ならば True
       'message': str,                 # 各 step の status 結合
       'updated': {'description': bool, 'pictures': bool},
       'description_len': int,          # description 反映成功時のみ非0
       'picture_urls': list[str],       # pictures 反映成功時のみ
       'description_result': dict,      # revise_item_description 戻値 (両方経路の partial diagnosis 用)
       'pictures_result': dict}         # revise_item_pictures 戻値 (同上)

    Error path:
      - credentials 未設定 → success=False, message="credentials missing", updated 両 False
      - 通信例外 → graceful success=False per-step
      - description 成功 + pictures 失敗 → success=False, updated={description:True, pictures:False}
        (= "片方反映済" の Q0 透明性確保。caller UI で per-step 結果表示)
    """

# 旧名 alias (K2 surgical、既存 callsite 影響 0)
def apply_description_to_ebay(ebay_item_id: str, description_html: str) -> dict:
    return apply_listing_update_to_ebay(ebay_item_id, description_html=description_html)
```

**eBay Trading API への HTTP 呼出は既存 2 関数を経由するため、本 W では `monitor/ebay_client.py` に新関数を追加しない**。OAuth token refresh / ET.ParseError graceful 化 / CDATA escape / PictureURL 12 件 cap / non-https reject 等の全 ガードは既存 2 関数で実装済 (drift 物理排除、HIGH-4 解消)。

**success 判定詳細** (v2.1 追加 MED-4):
- `description_html != None AND picture_urls != None` (両方指定): 両方 step success のみ overall success=True
- `description_html != None` のみ: description step success ならば overall success=True
- `picture_urls != None` のみ: pictures step success ならば overall success=True
- 両方 None: validation error で success=False (実行前 reject)

**両方指定時の実行順序と片方失敗時の挙動** (v2.1 追加 HIGH-B):
- 順序: **description → pictures 固定**
- 順序根拠: description 反映成功は buyer の「商品情報の正確性」担保が先。画像は EPS upload まで完了済なので失敗時の retry 容易性が pictures 側にある (本 session の paths が残置されている)
- description 失敗 → pictures **実行しない** (sequencer は description 失敗で early return、`attempted={description:True, pictures:False}`, `skipped_reason='description_failed_early_return'`、UI 文言で「画像反映は未実行」明示)。**ただし**, user が「画像だけ反映」button を明示的に押せばその経路で実行可能 (per-button 独立性は §5.3 で担保)
- description 成功 + pictures 失敗 → overall success=False、`updated={description: True, pictures: False}`, `attempted={description:True, pictures:True}`。**description は既に反映済 (rollback 不可)** だが、pictures は EPS upload paths が session_state に残存しているため UI に「🔄 画像反映だけ再試行」button を表示 (`pictures 失敗時のみ active 化`)。

**v2.2 修正 HIGH-Codex-4 (設計書内矛盾解消)**: §8.3 で「pictures path 継続」と書いていた旧表記は誤り。§5.4 と §8.3 双方とも「description 失敗 → pictures 未実行」で統一。戻値 dict に `attempted: {description: bool, pictures: bool}` と `skipped_reason: Optional[str]` を追加。

**rollback / retry 設計** (v2.1 追加 HIGH-B、Q0 透明性 + Boris Tip 2 reproducible):
- description rollback は本 W では**未対応** (旧 description_html を保持する mechanism が無い)。別 W で `listing_description_history` テーブル新設 (旧 description を 30 日保持 → 「直前の description に戻す」button 提供) を検討
- pictures rollback は本 W では**未対応** (旧 PictureURL list を保持しない)。別 W で `picture_url_history` 検討
- pictures 失敗時の retry: shared section に「🔄 画像反映だけ再試行」button を表示。session_state の `processed_image_urls` が残置されているため再 button push で revise_item_pictures を即 retry 可能 (EPS upload は cache hit で再課金 0)
- **本 W の Q0 透明性方針**: rollback は提供せず、per-step 失敗を UI に **per-step success/fail を全て可視化** (✅ description / ❌ 画像反映 等)。user が GetItem で確認 → 失敗側 button で retry の運用

---

## 6. データフロー (概要)

```
[3 タブそれぞれ] → scrape + rank + description 生成
       ↓
render_image_pipeline_section(prefix, source_urls, sku_hint, ebay_item_id, description_html, callbacks)
       ↓
   compose_hero_candidates_cached → 3 候補 → user 採用
       ↓
   unify_additional_backgrounds_cached → 全画像 grey 統一
       ↓
   upload_to_eps_cached → EPS URLs
       ↓
   [image-only / description-only / both 3 button] → callback
       ↓
   apply_listing_update_to_ebay(eid, description_html, picture_urls)
       ↓ (両方指定時は description → pictures の順、各 atomic、片方失敗で他方継続実行)
   revise_item_description (既存) + revise_item_pictures (既存) の sequencer
       ↓
   {success, updated:{description, pictures}, description_result, pictures_result, message, ...}
```

---

## 7. ビルドシーケンス (Phase 6 実装順序)

1. **Step 1**: `monitor/image_pipeline_shared.py` 作成 (pure 関数 4 + dataclass)
2. **Step 2**: `tests/test_image_pipeline_shared.py` 作成 (HTTP mock 経由 ~25 件)
3. **Step 3** (v2.1 修正): `_supplier_description_pipeline.py` に `apply_listing_update_to_ebay` 新設 (既存 `revise_item_description` + `revise_item_pictures` の sequencer、~60 LOC) + `apply_description_to_ebay` を thin wrapper alias 化 + 既存 `tests/test_supplier_photo_pipeline.py` 互換確認 + 新規 sequencer test ~5 件
4. **Step 4**: `tabs/_image_pipeline_ui.py` 作成 (UI section、3 反映ボタン + `clear_pipeline_keys` helper)
5. **Step 5**: `_supplier_description_pipeline.py:render_supplier_description_section` の W158 warning 撤去 + shared section 呼出 + caller 側 `clear_pipeline_keys` 呼出
6. **Step 6**: `tab_product_management.py:_render_url_direct_description_section` の W158 warning 撤去 + shared section 呼出 + caller 側 cascade clear (url_input 変化検知)
7. **Step 7**: `tab_individual_listing.py` refactor (Step 2.5/2.6/2.7 を shared に置換、prefix=`il_` 流用)
8. **Step 8**: pytest 全体 PASS (新規 + 既存)
9. **Step 9**: Q1 DoD 11 ステップ実機 verify (Streamlit + Playwright + eBay GetItem)
10. **Step 10-12**: code-reviewer → Codex → 修正 loop → commit & push

(v2.1: 旧 Step 3 = `revise_item_listing` 新設 + 専用 test を撤回、新 Step 3 に `apply_listing_update_to_ebay` sequencer 化を統合)

---

## 8. リスク分析

### 8.1 既存機能への影響範囲

| 影響先 | risk | 緩和策 |
|---|---|---|
| 個別出品タブ session_state key | prefix 化で `il_hero_*` 等の参照箇所 (draft load/save) crash | **prefix を `il_` のまま流用** (互換維持)、suffix も既存と同じ |
| `listing_drafts` DB の image_urls 列 | dict 形変更 | shared dataclass を session 保存時 `asdict` で **既存 dict 完全互換** |
| `apply_description_to_ebay` callsite 2 箇所 | signature 変更で crash | **alias 関数で旧 signature 維持** (新引数 keyword-only + default None) |
| 既存 revise_item_pictures test | 影響 0 | confirm: 関数自体は変更しない |

### 8.2 パフォーマンス
- Photoroom / Gemini / EPS の並列度 = 各 3 (既存と同)
- both 経路は image-only + description-only の 2 回 ReviseItem より HTTP RTT 1 回短縮 (200-400ms)

### 8.3 失敗時の挙動 (Q0 silent skip 排除)

| 失敗位置 | 検知 | UI 表示 | 反映 button 影響 |
|---|---|---|---|
| **credentials_missing** (v2 HIGH-5) | section 描画前 check | "⚠️ API key 未設定: <list>" | shared section 全体 hide (button 表示せず) |
| Photoroom (hero) | candidates=[] | "Photoroom 失敗: <err>" | image / both 無効化 |
| Gemini (hero 全失敗) | candidates=[] | "Gemini 合成全失敗" | 同上 |
| Gemini 部分失敗 | len(cands)<3 | "{N}/3 候補生成" | 採用可、有効 |
| Photoroom (additional 部分) | len(result)<len(urls) | "{N}/{total} 枚処理成功" | EPS upload は成功分のみ、警告表示 |
| EPS upload | EpsUploadResult.success=False | "{N}/{total} upload、{failed} 件失敗" | 0 成功 → image 無効、1+ → 有効 |
| **PictureURL 13+ 枚 silent drop** (v2 HIGH-3) | resolve_final_picture_urls の dropped non-empty | "{N} 枚は ReviseItem 上限超過のため反映されません" | image / both 内部 cap 12 で反映継続 |
| ReviseItem (description) | revise_item_description.success=False | "❌ 説明文反映失敗。画像反映は未実行 (説明文先実行 → 失敗で stop)" | `updated.description=False`, **pictures path skip (HIGH-Codex-4 v2.2 修正)** |
| ReviseItem (pictures) | revise_item_pictures.success=False | "❌ 画像反映失敗 (画像だけ再実行可能)" | `updated.pictures=False`, description は既に反映済 (両方経路) |
| **両方経路で description 失敗** (v2.2 HIGH-Codex-4) | `attempted.pictures=False, skipped_reason='description_failed_early_return'` | "❌ 説明文反映失敗。画像は未実行" | 画像だけ反映 button 推奨 |
| **両方経路で description 成功 + pictures 失敗** (v2.2 HIGH-Codex-4) | per-step | "✅ 説明文反映済 / ❌ 画像反映失敗" | 画像だけ再実行 button active 化 |

### 8.4 特殊ケース (Codex 論点)
- **画像 ReviseItem 成功 + description warn**: Ack=Warning は success=True (現行と同方針)
- **partial success**: 同 Item 内 element は API 仕様で atomic、ただし `updated` dict は送信時指定をそのまま入れる (GetItem 検証は user 責任)

### 8.5 Codex GPT-5.5 1 周目 HIGH fix (v2.2 追加)

**HIGH-Codex-1: in-flight lock (連打 / Streamlit rerun 重複課金防止)**

- 問題: `compose_hero_candidates_cached` / `unify_additional_backgrounds_cached` は完了後の file cache でしか重複 skip できない。実行中に user が再生成連打 / Streamlit rerun / 複数タブで同 `out_base` 操作した時、Photoroom/Gemini API が **重複課金** される (1 click = $0.14 が 2-3 倍に膨らむ)。
- 修正: shared 内に **per (item_id, source_hash, stage) の in-flight lock** を導入。
  - session_state key `{prefix}_lock_{stage}` (stage ∈ {hero, additional, eps}) を bool で持つ
  - API call 前に lock 取得 (False → True)、finally で必ず release
  - lock 中は button disabled (st.button(..., disabled=lock))
  - 異常終了の TTL は session 単位 (rerun 時に再描画で wedged lock → user が手動 clear)
  - shared section の `_pipeline_lock_state(prefix, stage) -> bool` helper + `_acquire_pipeline_lock(prefix, stage)` / `_release_pipeline_lock(prefix, stage)`

**HIGH-Codex-2: 画像 cache の source/content 不一致検知 (manifest 必須化)**

- 問題: `compose_hero_candidates_cached` は「既存 `hero_W*.png` + `_studio.png` 両方ありで skip 復元」だけ。**同じ `out_base` (例: `temp_{eid}_*`) で source URL が変わった**ら、古い商品の hero が新商品にそのまま使い回されて user は気づかず eBay 反映する **money-direct silent gap**。
- 修正: cache 復元時は **manifest JSON 必須**化。`{out_base}/_manifest.json` に以下を保存:
  ```json
  {
    "source_url": "https://...",
    "source_sha256": "abc123...",
    "pipeline_version": "1",
    "prompt_version": "1",
    "stage_outputs": {
      "hero": ["hero_W1.png", "hero_W2.png", "hero_W3.png"],
      "additional": ["_additional_00.png", "_additional_01.png", ...]
    },
    "created_at": "2026-05-23T10:00:00+09:00"
  }
  ```
  - cache 復元時は manifest の `source_url` + `source_sha256` が現在の入力と一致する場合のみ skip
  - 不一致 → cache miss として API 再実行 + manifest 更新
  - UI で「復元元 source: ✓ 現在 source と一致」を 1 行表示 (user 確認用)
  - shared.compose_hero_candidates_cached / unify_additional_backgrounds_cached 両方で manifest 読み書き
  - 既存個別出品の `out_base` には manifest がない → 初回 cache miss + manifest 生成 (1 回の API 課金で済む、migration コスト 1 回限り)

**HIGH-Codex-3: EPS upload cache の content_sha256 化 (旧 EPS URL 再利用 risk)**

- 問題: `upload_to_eps_cached(paths, use_cache=True)` は DB `eps_upload_cache` で重複 skip するが、cache key が path ベースだと、**再生成で `_additional_01.png` が上書きされたあと、cache が hit して旧 EPS URL が返る** → user は新画像を upload したつもりで旧画像が eBay に反映される silent gap (money-direct)。
- 修正: cache key を **`absolute_path + content_sha256(file)`** にする。content_sha256 が変わったら cache miss として再 upload。
  - 既存 DB schema `eps_upload_cache` (file_hash 列がある可能性が高い) を確認、なければ migration で追加 (v54 候補)
  - shared.upload_to_eps_cached の戻値 `EpsUploadOutcome` に `cache_hit_files: list[Path]` と `content_hashes: dict[Path, str]` を追加
  - UI preview のサムネイル表示時に「content hash = upload 対象 hash」一致を 1 行 caption で表示

**HIGH-Codex-4: sequencer 方針の設計書内矛盾解消**

- 問題: §5.4 は「description 失敗 → pictures 実行しない (early return)」、§8.3 は「ReviseItem description 失敗 → pictures path 継続実行」と書いており **設計書内矛盾**。
- 修正: §5.4 の「description 失敗 → pictures **実行しない**」を **採用** (Q0 silent skip 防止に整合: 一方 fail 時に他方を盲目的に進めるとロールバック不能な部分反映を生む)。§8.3 を更新して「description 失敗 → pictures **未実行** (skipped_reason='description_failed_early_return')」と書き換える。
- 戻値 dict に `attempted: {description: bool, pictures: bool}` と `skipped_reason: Optional[str]` を追加。UI 文言:
  - description 失敗 + pictures 未実行: "❌ 説明文反映失敗。画像反映は未実行 (説明文先実行 → 失敗で stop)。説明文を確認後、画像だけ反映 button を押してください"
  - description 成功 + pictures 失敗: "✅ 説明文反映済 / ❌ 画像反映失敗 (画像だけ再実行可能)"

**LOC 影響 (v2.2)**: shared module +30 (manifest 読み書き) + UI module +20 (lock state UI) + EPS cache key 変更で `monitor/ebay_eps_uploader.py` 修正 +30 (現状 path-only → +content_sha256)、合計 **+80** 追加で **net +705 LOC**。

---

## 9. Q1 DoD 11 ステップ

1. [ ] code-reviewer Opus 4.7 HIGH=0
2. [ ] Codex GPT-5.5 2 段 HIGH=0
3. [ ] pytest 全件 PASS (新規 ~25 + 既存)
4. [ ] Streamlit 再起動 (PID 入替確認)
5. [ ] Playwright で個別出品タブ既存挙動破壊なし
6. [ ] Playwright で商品管理タブ URL 直接投入 → 3 反映パターン
7. [ ] Playwright で仕入先候補 採用 → 3 反映パターン
8. [ ] **eBay live ReviseItem 検証**: 1 商品 test で 3 経路 GetItem 実反映確認
9. [ ] DB eps_upload_cache に重複なし (cache hit 確認)
10. [ ] Console 0 errors
11. [ ] commit & push & ROADMAP id=242 完了マーク

---

## 10. 設計判断の論点 (Codex review への種)

### 論点 A: prefix 戦略 (alias vs 完全 namespace 化)
- **採用案**: 個別出品 prefix="il_" 流用、商品管理 / 仕入先候補は新 prefix
- **根拠**: K2 Surgical (既存 listing_drafts 列との互換維持)
- **論点**: 将来 W159+ で個別出品に新規セクションが増えた時の衝突 risk

### 論点 B: ReviseItem 1 度 vs 2 度 (v2: code-reviewer HIGH-2 + HIGH-4 を受けて 2 度採用)
- **v1 採用案**: both は `revise_item_listing` 新設で 1 度 → **撤回**
- **v2 採用案**: both は既存 `revise_item_description` + `revise_item_pictures` の 2 度 (順次)
- **v2 根拠**:
  - Ack=Warning で片方 child element が silent reject される過去事例リスク (eBay Trading API は warning でも一部 element の internal validation reject が起こり得る、`revise_item_pictures` の既存 docstring L613-614 が「12 件超は eBay 側で silent drop」と明記している通り)
  - 新関数 + 旧 2 関数 alias = 3 並走で OAuth refresh / ET.ParseError ガード / API endpoint 等 cascade 更新義務が永続化 (cascade-update.md R-4 違反)
  - HTTP RTT 1→2 増 (200-400ms) の penalty より Q0 silent skip 防止 + drift 物理排除を優先
- **trade-off**: 「両方反映」で片方失敗時に user は片方反映後の状態で手動 retry (eBay GetItem で確認 → 失敗側のみ再反映ボタン押下) する必要あり。UI に per-step success 表示で透明性確保。

### 論点 C: PictureURL 上限 (12 vs 24)
- **採用案**: ReviseItem 経路は 12 件 cap
- **根拠**: 一次情報照合済 (revise_item_pictures L618 の `>12 reject`)
- **論点**: 13+ 件来た時は **明示的に [:12] slice + UI に warning 表示** (silent drop 防止 Q0)

### 論点 D: source_url 変更検知のスコープ
- **個別出品の既存挙動**: hero_source_url が変わると候補破棄 cascade clear
- **論点**: 商品管理タブ側 result_key クリアと shared 内 cascade clear の二重作用 → code-reviewer 確認

### 論点 E: hero source = source_urls[0] 強制
- **採用案**: 個別出品と同 K1 (並び替え UI は out of scope)

### 論点 F: 反映後の session_state クリア
- **採用案**: shared section は加工結果保持 (再反映可能)、`result_key` クリアは caller 責任

### 論点 G: K1 vs DRY
- **K1 範囲確認**: hero compose / additional photoroom / EPS upload は 3 タブで使う = 3 回出る = 共通化条件 PASS

### 論点 H: alias deprecate 計画
- **本 W**: 残置 (K2)、別 W で deprecate 検討
- **緩和**: alias 関数本体に `# DEPRECATED: use revise_item_listing (W158, 2026-05-23)` + drift 物理排除

### 論点 I: cost recording
- **本 W**: なし (K1)、別 W で `image_processing_cost_log` 検討

### 論点 J: temp_xxx sku の cache 衝突
- **採用案**: `sku_hint=f"eid_{ebay_item_id}"` で cache 復元
- **緩和**: source_url 変化検知 → cascade clear (論点 D)

### 論点 K: 既存実装の落とし穴 (md-files-can-be-wrong R-1)
- 個別出品 L915 `urls[:24]` は AddFixedPriceItem 上限。**ReviseItem 経路は 12 件 cap で再 cap が必要**
- L869-883 `missing` 判定: 既存ファイル削除後の error 表示挙動を維持
- L965: `existing_studio.exists()` skip 条件は両方の AND
- L932: 全失敗 state="error" / 部分失敗 state="complete"

---

## 11. cascade scan (md-files-can-be-wrong R-1 / cascade-update.md)

| keyword | hit file | 対応 |
|---|---|---|
| `apply_description_to_ebay` | tab_product_management.py:1306 / _supplier_description_pipeline.py:535 | alias 残置で callsite 無修正 |
| `revise_item_description` | 関連 test + ebay_client.py | wrapper alias で残置 |
| `revise_item_pictures` | 同上 | 同上 |
| `_render_hero_compose_section` | tab_individual_listing.py 内のみ | shared 化 |
| `il_hero_*` | tab_individual_listing.py 内のみ (draft load/save 含) | prefix 不変方針 |
| W158 warning コメント (2 箇所) | _supplier_description_pipeline.py / tab_product_management.py | 撤去 |

---

## 12. 完了判定基準 (DoD)

1. [ ] pytest 全件 PASS (~1469 件 = 既存 1444 + 新規 25)
2. [ ] code-reviewer Opus 4.7 HIGH=0
3. [ ] Codex GPT-5.5 2 段 HIGH=0
4. [ ] Streamlit 再起動 + Playwright 3 タブ Console 0 errors
5. [ ] **eBay live ReviseItem 検証**: 1 商品で description / image / both 3 経路 GetItem 実反映確認
6. [ ] DB eps_upload_cache 重複 0
7. [ ] data/system_improvements.json id=242 "完了"
8. [ ] commit & push
9. [ ] session memory に Q5 4 行テンプレ
10. [ ] cascade scan 痕跡記録

---

## 13. 質問リスト (Phase 5 で確認したい未確定要件)

1. **Q-1**: 商品管理 URL 直接投入で 画像加工結果を listing_drafts に保存するか? → 本 W は session 内のみ (K1)
2. **Q-2**: 仕入先候補 image-only 反映時、competitor_products に "画像反映済" flag を書くか? → 本 W は書かない (K1)
3. **Q-3**: 「両方反映」で Ack=Warning の時 UI 表示は ✅/⚠️/❌? → 案: ✅ + warning message expander (現行統一)
4. **Q-4**: out_base path のクリーンアップは本 W に含めるか? → 含めない (別 W)
5. **Q-5**: 「再生成 ($0.14)」ボタンの料金表示は 3 タブ共通か? → 個別出品と完全同 UI で進める (K1)
6. **Q-6**: 商品管理 / 仕入先候補は source_urls をどう分割するか? → `[0]=hero`, `[1:]=additional` 自動分割 (個別出品 Step 2 のチェックボックス UI は out of scope)
7. **Q-7**: image-only 反映の roll-back 経路は本 W か? → 含めない (K1)

---

## 14. 学び / Phase 0 発見 (Codex review 入力用)

- **発見 1**: ReviseItem (12 件) と AddFixedPriceItem (24 件) で PictureURL 上限が異なる → `resolve_final_picture_urls(cap=12)` で明示
- **発見 2**: CDATA escape は description のみ。本 W `_build_revise_item_listing_xml` でも description のみ escape
- **発見 3**: ET.ParseError graceful 化は W148 Codex HIGH 対策パターン。本 W も冒頭から実装
- **発見 4**: `_resolve_listing_image_urls` 優先順位 (processed > selected > supplier) は AddFixedPriceItem 用。ReviseItem 経路は processed 空なら "画像反映しない" 方が安全
- **発見 5**: `_RANK_CHOICES` / `_RANK_LABEL_HINTS` の重複は K1 既存判断 (touch しない)
- **発見 6**: `_do_hero_compose` skip 条件は `existing_heros AND existing_studio.exists()` の両方
- **発見 7**: `upload_images_parallel` は空 paths で例外を投げず空 list を返す
- **発見 8**: 個別出品 L915 `urls[:24]` は AddFixedPriceItem 経路 hard cap。ReviseItem 経路では 12 件 cap が必要

---

## 完了報告テンプレ (Q5)

```
- 使用モデル: Opus 4.7 (設計) / Sonnet 4.6 (実装) / Haiku 4.5 (test 追加)
- 検証経路: pytest unit (shared 25 + XML 11) / Playwright UI (3 タブ) / eBay GetItem ReviseItem 実反映 (3 経路) / DB SELECT (eps_upload_cache)
- 実機ログ: scheduler.log 影響なし (UI 経路のみ) / Streamlit reload PID 確認済
- 残リスク: <Codex 2 段の MED が残るか / future W で deprecate alias 整理 / image cost recording 別 W>
```

---

*本設計書は Phase 4 成果物。Phase 5 で code-reviewer Opus 4.7 + Codex GPT-5.5 2 段 review、HIGH=0 まで loop してから Phase 6 (実装) 着手予定。前 9 例 (W139/W142/W138A/W7/W139fix/W148/W149/W153/W153v2) で実証された「内部 + Codex で money-direct silent gap 突破」パターンを踏襲する。*
