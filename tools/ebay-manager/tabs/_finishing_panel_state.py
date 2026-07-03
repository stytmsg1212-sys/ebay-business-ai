"""W314 Phase 2 S5 (2026-07-03): 統一「商品仕上げパネル」state 層.

設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §1-§5
モックアップ: 2026-07-03-finishing-panel-mockup.html (同ディレクトリ、user 承認済)

streamlit に依存しない純関数群 (dirty 判定 / 変更プレビュー組立 / ヘッダ指標算出 /
反映ディスパッチ) を集約する。UI 層 (`tabs/_finishing_panel.py`) はこれらを呼び出す
薄い wrapper に徹する (unit test しやすさ優先、K1)。

listing 識別は ebay_item_id のみ (sku-rules.md 準拠、SKU は一切使わない)。

Phase 2 スコープ:
    - コンテンツ (タイトル/description/ランク/数量) の dirty 追跡 + 一括反映
    - 画像は対象外 (`tabs._supplier_photo_pipeline.render_supplier_photo_apply_section`
      が独自の反映ボタンで完結するため、本モジュールの一括反映ディスパッチには含めない。
      設計書§5「価格・送料は別ボタン」と同じ隔離思想を画像にも適用した Phase 2 の
      明示的な簡略化 — 詳細は _finishing_panel.py 冒頭コメント参照)
    - 価格・送料は完全に対象外 (T3 money-direct、商品管理タブへ誘導のみ)
"""
from __future__ import annotations

import logging
from typing import Any, Callable, MutableMapping, Optional

logger = logging.getLogger(__name__)

SS_PREFIX = "pf_"

# CLAUDE.md コンディションランク 8 段階
RANK_CHOICES: tuple[str, ...] = ("N", "S", "A", "B", "C", "D", "PO", "As-Is")

RANK_LABELS_JA: dict[str, str] = {
    "N": "N — 新品・未開封",
    "S": "S — 新品同様",
    "A": "A — 美品・動作確認済",
    "B": "B — 並品・動作確認済",
    "C": "C — 使用感あり・動作確認済",
    "D": "D — 難あり・動作確認済",
    "PO": "PO — 通電のみ・動作未確認",
    "As-Is": "As-Is — 未確認/部品取り",
}

# 商品ランク → eBay ConditionID (CLAUDE.md 8 段階表準拠)。
# tab_product_management.py::_RANK_TO_CONDITION_ID /
# _supplier_description_pipeline.py::_RANK_TO_CONDITION_ID_SUPPLIER と同値の
# 3 箇所目の参照 (Phase 3 §8「採用ロジック単一化」で統合予定。現時点は
# 各モジュール独立参照を維持する K1: 新規ファイルのみの scope で既存ファイルを
# 触らないため、統合は別 Phase に委ねる)。
RANK_TO_CONDITION_ID: dict[str, str] = {
    "N": "1000", "S": "1500",
    "A": "3000", "B": "3000", "C": "3000", "D": "3000", "PO": "3000",
    "As-Is": "7000",
}

FIELD_LABELS_JA: dict[str, str] = {
    "title": "タイトル",
    "description": "Description",
    "images": "画像",
    "rank": "ランク",
    "quantity": "数量",
}

# 変更プレビュー表示順 (設計書§3 の表と同順)
PREVIEW_FIELD_ORDER: tuple[str, ...] = ("title", "description", "images", "rank", "quantity")

# コンテンツ一括反映 (🚀 eBay へ反映 ボタン) の対象フィールド。
# 画像は対象外 (モジュール docstring 参照)。
DISPATCH_FIELD_ORDER: tuple[str, ...] = ("title", "description", "rank", "quantity")


# =============================================================================
# session_state ヘルパ (namespace `pf_{eid}_*`)
# =============================================================================

def pf_key(eid: str, suffix: str) -> str:
    """商品仕上げパネル session_state key を組み立てる (`pf_{eid}_{suffix}`)."""
    return f"{SS_PREFIX}{eid}_{suffix}"


def seed_session_value(session_state: MutableMapping[str, Any], key: str, value: Any) -> Any:
    """key が未設定なら value で初期化し、確定値 (既存 or 新規) を返す (idempotent).

    Streamlit の widget key と併用する場合は widget 生成前に呼ぶこと
    (`value=` 引数は key 既存時に Streamlit が無視するため — tab_product_management.py
    `_resolve_pm_search_seed` と同じ確立パターン)。
    """
    if key not in session_state:
        session_state[key] = value
    return session_state[key]


def seed_initial(
    session_state: MutableMapping[str, Any], eid: str, field: str, value: Any,
) -> Any:
    """フィールドの dirty 判定基準値 (baseline) を 1 度だけ確定する.

    初回 render 時に DB/eBay の現在値を焼き付け、以後 user が widget を編集して
    rerun してもこの基準値は変わらない (widget 側の値との差分で dirty を判定する)。
    """
    return seed_session_value(session_state, pf_key(eid, f"{field}_initial"), value)


def mark_field_synced(
    session_state: MutableMapping[str, Any], eid: str, field: str, value: Any,
) -> None:
    """反映成功後、baseline を反映後の値に更新して dirty をクリアする."""
    session_state[pf_key(eid, f"{field}_initial")] = value


# =============================================================================
# ランク解決
# =============================================================================

def resolve_rank_initial(row: dict) -> str:
    """listing の商品ランク初期値を解決する (condition_rank 優先 → ebay_condition_id 逆引き).

    tab_product_management.py::_condition_widget_initial と同ロジック
    (Used=3000 はサブランク A/B/C/D/PO 逆引き不能のため "" = 未設定を返す)。
    """
    sub = (row.get("condition_rank") or "").strip()
    if sub in RANK_CHOICES:
        return sub
    cid = str(row.get("ebay_condition_id") or "").strip()
    return {"1000": "N", "1500": "S", "7000": "As-Is"}.get(cid, "")


def rank_to_condition_id(rank: Optional[str]) -> Optional[str]:
    """商品ランクコードを eBay ConditionID へ変換 (不明な rank は None)."""
    if not rank:
        return None
    return RANK_TO_CONDITION_ID.get(rank)


# =============================================================================
# dirty 判定
# =============================================================================

def is_field_dirty(field: str, before: Any, after: Any) -> bool:
    """フィールド種別ごとの dirty 判定 (共通ロジック).

    - title/description/rank: 文字列 strip 比較。空文字への変更は dirty
      扱いしない (誤操作で全消し→反映ボタン活性化を防ぐ、既存 title_is_dirty
      `_supplier_followup_state.py` と同方針)。
    - quantity: 整数比較。after が None/変換不能なら dirty 扱いしない
      (0 への変更自体は許容 = 在庫0化は正当な操作)。
    - images / 未知フィールド: 単純な非空 + 不一致判定 (呼出側が summary 文字列
      等を渡す想定、本モジュールの一括反映ディスパッチの対象外だが
      build_change_preview からは利用可能にしておく)。
    """
    if field in ("title", "description", "rank"):
        a = (after or "").strip() if isinstance(after, str) else after
        b = (before or "").strip() if isinstance(before, str) else before
        return bool(a) and a != b
    if field == "quantity":
        if after is None:
            return False
        try:
            return int(after) != int(before or 0)
        except (TypeError, ValueError):
            return False
    return bool(after) and after != before


# =============================================================================
# 変更プレビュー
# =============================================================================

def summarize_description(text: Optional[str], head_chars: int = 120) -> str:
    """description の UI プレビュー用要約 (先頭 head_chars 字 + 全文文字数).

    監査ログ (listing_content_change_log) には全文を保存する (確定判断2、
    設計書§0)。UI プレビューは要約表示で十分 (S5 タスク指示)。
    """
    t = text or ""
    head = t[:head_chars]
    suffix = "…" if len(t) > head_chars else ""
    return f"{head}{suffix} ({len(t)}文字)"


def summarize_images(mode_label: str, count: int) -> str:
    """画像プレビュー用の要約表示 (モード名 + 枚数)."""
    return f"{mode_label} ({count}枚)"


def _default_display(field: str, value: Any) -> str:
    if field == "description":
        return summarize_description(value)
    if value is None or value == "":
        return "—"
    return str(value)


def build_change_preview(fields: dict[str, dict]) -> list[dict]:
    """dirty フィールドのみを (field, label, before, after) 表示用リストで返す.

    Args:
        fields: {field: {'before': Any, 'after': Any,
                          'before_display': 任意, 'after_display': 任意}}
            before_display/after_display 省略時は _default_display で自動要約する
            (description は summarize_description、それ以外は str()).

    Returns:
        PREVIEW_FIELD_ORDER 順の [{'field','label','before','after'}] (dirty のみ)。
    """
    preview: list[dict] = []
    for field in PREVIEW_FIELD_ORDER:
        data = fields.get(field)
        if not data:
            continue
        before, after = data.get("before"), data.get("after")
        if not is_field_dirty(field, before, after):
            continue
        before_disp = data.get("before_display")
        after_disp = data.get("after_display")
        preview.append({
            "field": field,
            "label": FIELD_LABELS_JA.get(field, field),
            "before": before_disp if before_disp is not None else _default_display(field, before),
            "after": after_disp if after_disp is not None else _default_display(field, after),
        })
    return preview


# =============================================================================
# ヘッダ指標 (価格・利益・在庫・ステータス)
# =============================================================================

def compute_header_metrics(row: dict, settings: Optional[dict] = None) -> dict:
    """パネルヘッダの 4 指標 (価格・利益・在庫・ステータス) を算出する.

    Args:
        row: monitor.database.get_ebay_listing_by_item_id() が返す 1 行 dict。
        settings: calculator.load_settings() 相当 (省略時は自動読込)。

    tab_product_management.py::_render_hero_metrics の軽量版 (設計書§3「軽量版」
    指示準拠)。bp_state pill / カテゴリ FVF pill 等の重い付随表示は含めない。

    purchase_yen / weight_g が DB に欠けていれば profit は None を返す
    (0 円等の誤値で誤魔化さない = Q0)。利益試算で例外が出てもヘッダ全体は
    落とさず None のまま返す (ヘッダは補助表示、パネル本体を壊さない)。
    """
    price = float(row.get("current_price") or 0)
    quantity = int(row.get("quantity_ebay") or 0)
    status = "Ended" if int(row.get("is_ended") or 0) == 1 else "Active"

    result: dict[str, Any] = {
        "price_usd": price,
        "profit_jpy": None,
        "profit_rate_pct": None,
        "quantity": quantity,
        "status": status,
    }

    pyen = row.get("purchase_yen")
    weight_g = row.get("weight_g")
    if not (price > 0 and pyen and weight_g):
        return result

    try:
        from calculator import CalcInput, calculate
        if settings is None:
            from calculator import load_settings
            settings = load_settings()
        calc_res = calculate(
            CalcInput(
                purchase_yen=float(pyen),
                item_price_usd=price,
                weight_g=float(weight_g),
                length_cm=float(row.get("length_cm") or 0),
                width_cm=float(row.get("width_cm") or 0),
                height_cm=float(row.get("height_cm") or 0),
                category_id=int(row.get("category_id") or 0),
                is_ddu=False,
                country_code="US",
                point_yen=(
                    float(row["point_yen"]) if row.get("point_yen") is not None else None
                ),
            ),
            settings,
        )
        if calc_res.service_results:
            best = max(calc_res.service_results, key=lambda s: s.profit_with_refund)
            result["profit_jpy"] = round(best.profit_with_refund)
            result["profit_rate_pct"] = round(best.profit_with_refund_rate * 100, 1)
    except Exception as e:  # noqa: BLE001 -- ヘッダは補助表示、失敗しても panel を壊さない
        logger.warning(
            "compute_header_metrics: 利益試算失敗 eid=%s: %s",
            row.get("ebay_item_id"), e,
        )
    return result


# =============================================================================
# description の eBay 取得 (「⬇️ eBay から取得」ボタン)
# =============================================================================

def fetch_description_from_ebay(ebay_item_id: str, config: Optional[dict] = None) -> dict:
    """eBay GetItem で現行 description を取得する.

    tab_product_management.py::_render_desc_fetch_button と同じ
    `monitor.ebay_client.get_single_listing` 経由 (既存パターン踏襲)。

    Returns:
        {'success': bool, 'description': str, 'message': str}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_client import get_single_listing

    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001 -- credentials 解決の多様な例外を UI に伝える
        return {"success": False, "description": "", "message": f"credentials 取得エラー: {e}"}
    if not ebay_credentials_ok(creds):
        return {"success": False, "description": "", "message": "eBay credentials 未設定"}

    snap = get_single_listing(
        ebay_item_id, creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )
    if snap is None or snap.get("description") is None:
        return {
            "success": False, "description": "",
            "message": "取得失敗 (GetItem 応答なし / Description 空)",
        }
    return {"success": True, "description": snap.get("description") or "", "message": "取得しました"}


# =============================================================================
# コンテンツ一括反映ディスパッチ
# =============================================================================

def dispatch_content_changes(
    ebay_item_id: str,
    changes: list[dict],
    *,
    source_tab: Optional[str] = None,
    candidate_id: Optional[int] = None,
    log_fn: Optional[Callable[..., int]] = None,
) -> dict[str, dict]:
    """コンテンツ一括反映のディスパッチ (dirty フィールド列 → revise 実行 → 監査ログ).

    Args:
        ebay_item_id: 反映先 listing (sku-rules: 識別は ebay_item_id のみ)。
        changes: 各要素は以下の dict:
            {'field': str, 'before': Any, 'after': Any, 'apply': Callable[[], dict]}
            `apply()` は revise API 呼出 + (成功時) DB 同期まで完結させ
            {'success': bool, 'message': str} を返す呼び出し側の責務。
            revise 関数そのものを DI するのではなく「呼べば完結する callable」を
            DI することで、本関数は streamlit / credentials / DB 更新の詳細に
            一切依存しない (streamlit 非依存の純関数 unit test を可能にする)。
        log_fn: `monitor.listing_content_change_log.log_content_change` の
            差し替え用 (テスト DI)。省略時は実モジュールを import する。

    Returns:
        {field: {'success': bool, 'message': str}} — 全フィールドについて実値を
        返す (1 つ失敗しても残りは続行し、握り潰さない = Q0)。

    監査ログは成功/失敗を問わず全フィールドに対して記録する (success 列で判別、
    `apply_followup_title_to_ebay` と同方針)。ログ記録自体の失敗で revise 結果を
    握り潰さない (try/except を分離)。
    """
    if log_fn is None:
        from monitor.listing_content_change_log import log_content_change as log_fn

    results: dict[str, dict] = {}
    for change in changes:
        field = change["field"]
        before = change.get("before")
        after = change.get("after")
        apply_fn = change["apply"]

        try:
            outcome = apply_fn()
        except Exception as e:  # noqa: BLE001 -- 1 フィールド失敗で残りを止めない (Q0)
            logger.warning(
                "dispatch_content_changes: apply 例外 eid=%s field=%s: %s",
                ebay_item_id, field, e,
            )
            outcome = {"success": False, "message": f"{type(e).__name__}: {e}"}

        ok = bool(outcome.get("success"))
        message = outcome.get("message") or ""
        results[field] = {"success": ok, "message": message}

        try:
            log_fn(
                ebay_item_id, field, before, after,
                source_tab=source_tab, candidate_id=candidate_id,
                success=ok, ebay_ack=message,
            )
        except Exception as e:  # noqa: BLE001 -- 監査ログ失敗で revise 結果を握り潰さない
            logger.warning(
                "dispatch_content_changes: log_content_change 失敗 eid=%s field=%s: %s",
                ebay_item_id, field, e,
            )

    return results
