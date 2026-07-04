"""W314 Phase 2 S5 (2026-07-03): 統一「商品仕上げパネル」UI (_finishing_panel.py) 検証.

streamlit の実 render (widget 描画) は ScriptRunContext が無いと実行できないため、
既存 followup / supplier_candidates テスト群と同方針で
(a) import 可能性 (py_compile 相当), (b) 呼び出し契約 (シグネチャ),
(c) @st.fragment decorator の存在, (d) T3 money-direct 隔離 (価格・送料 revise
関数を一切 import しない) を AST / ソース静的検証で守る。
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_UI_PATH = _PROJECT_ROOT / "tabs" / "_finishing_panel.py"


def _find_function_def(name: str) -> ast.FunctionDef | None:
    tree = ast.parse(_UI_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ─────────────────────────────────────────────────
# 1. import 可能性 + 呼び出し契約
# ─────────────────────────────────────────────────

def test_finishing_panel_module_importable():
    from tabs._finishing_panel import render_finishing_panel
    assert callable(render_finishing_panel)


def test_render_finishing_panel_signature_matches_call_contract():
    """S6 (結線担当) の呼び出し契約: (eid, config, *, candidate_id, candidate_url, source_tab)."""
    from tabs._finishing_panel import render_finishing_panel

    sig = inspect.signature(render_finishing_panel)
    params = list(sig.parameters.values())

    assert params[0].name == "eid"
    assert params[0].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD

    assert params[1].name == "config"
    assert params[1].default is None

    kw_only = {p.name: p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY}
    assert "candidate_id" in kw_only and kw_only["candidate_id"].default is None
    assert "candidate_url" in kw_only and kw_only["candidate_url"].default is None
    assert "source_tab" in kw_only
    assert kw_only["source_tab"].default == "product_management"
    # user feedback 2026-07-03: ヘッダ直下に差し込む top_slot callable
    assert "top_slot" in kw_only and kw_only["top_slot"].default is None
    # W314 Phase 3 (2026-07-03): パネル末尾に差し込む bottom_slot callable
    assert "bottom_slot" in kw_only


def test_render_finishing_panel_rejects_empty_eid_without_crashing():
    """eid 未指定でも import 済 streamlit の st.error 呼出だけで例外を投げない.

    (st スクリプトコンテキスト外では st.error はログ出力にフォールバックする
    ため呼出自体は成立する。ここでは「関数が eid='' を弾く分岐を持つ」ことを
    ソース上で確認するに留める — 実 render の smoke は Playwright E2E 側で行う。)
    """
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "if not eid:" in src


# ─────────────────────────────────────────────────
# 2. @st.fragment decorator (W174-pm と同方針: button rerun をパネル scope に限定)
# ─────────────────────────────────────────────────

def test_fragment_function_has_st_fragment_decorator():
    fn = _find_function_def("_render_finishing_panel_fragment")
    assert fn is not None, "_render_finishing_panel_fragment 関数が見つからない"
    found = False
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr == "fragment":
            found = True
        elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                and dec.func.attr == "fragment":
            found = True
    assert found, "_render_finishing_panel_fragment に @st.fragment decorator が必要 (設計書§7)"


def test_render_finishing_panel_delegates_to_fragment_function():
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "_render_finishing_panel_fragment(" in src


# ─────────────────────────────────────────────────
# 3. T3 money-direct 隔離 (価格・送料の revise 関数を一切 import しない)
# ─────────────────────────────────────────────────

def test_no_price_or_shipping_revise_calls_in_finishing_panel():
    """商品仕上げパネル (Phase 2) は価格・送料編集を含まない (T3 隔離、設計書§3)."""
    src = _UI_PATH.read_text(encoding="utf-8")
    forbidden = (
        "_apply_to_ebay",  # 既存 money-direct apply 関数 (tab_product_management.py)
        "revise_item_price",
        "update_shipping",
        "ShippingServiceCostOverride",
    )
    for token in forbidden:
        assert token not in src, f"money-direct token '{token}' が finishing panel に混入している"


def test_money_zone_only_has_jump_button_no_price_input():
    """money zone セクションは商品管理タブへの誘導ボタンのみ (価格 st.number_input が無い)."""
    fn = _find_function_def("_render_money_zone")
    assert fn is not None
    src = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "number_input" not in src
    assert "pm_focus_eid" in src  # W292 jump 流儀


# ─────────────────────────────────────────────────
# 4. 画像は一括反映ディスパッチの対象外 (state 側 DISPATCH_FIELD_ORDER と整合)
# ─────────────────────────────────────────────────

def test_apply_content_changes_does_not_reference_images_field():
    fn = _find_function_def("_apply_content_changes")
    assert fn is not None
    src = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert '"field": "images"' not in src
    assert "'field': 'images'" not in src


# ─────────────────────────────────────────────────
# 5. 仕入先 URL 未指定時の案内文言 (設計書§4 / タスク指示)
# ─────────────────────────────────────────────────

def test_image_field_always_renders_photo_apply_section():
    """モックパリティ (2026-07-03 修正): 画像セクションは URL 未解決でも常時 3 モード表示.

    従来「URL 未指定なら photo section を出さない」動作 (info メッセージのみ) は
    モックアップ tab_t1 の 3 モード常時表示 (imgmode-select + 各 img-mode-panel)
    と乖離していたため撤去。URL 未解決時はセクション上部に URL 入力欄を出し、
    render_supplier_photo_apply_section は常に呼ぶ (③メイン差し替えは URL 無しでも
    現行 eBay 画像を表示できるため無条件呼出しが正当)。
    """
    src = _UI_PATH.read_text(encoding="utf-8")
    # (a) 3 モードは常時レンダー = render_supplier_photo_apply_section が
    #     _render_image_field 内で無条件に呼ばれる
    assert "render_supplier_photo_apply_section" in src
    # (b) URL 未解決時のガイダンス文言 (モック §「(仕入先 URL 未指定時は入力欄)」)
    assert "①合成 / ②そのまま採用 モードを使うには URL 入力が必要" in src
    # (c) 旧「利用できません」メッセージが残っていないこと (混乱防止)
    assert "画像モードは利用できません" not in src


# ─────────────────────────────────────────────────
# 6. W292 jump 流儀の 3 点セット (pm_focus_eid / _w134_sel / _w217a_cat_view)
# ─────────────────────────────────────────────────

def test_money_zone_jump_sets_all_three_session_keys():
    src = _UI_PATH.read_text(encoding="utf-8")
    assert 'st.session_state["pm_focus_eid"] = eid' in src
    assert 'st.session_state["_w134_sel"] = "商品管理"' in src
    assert 'st.session_state["_w217a_cat_view"] = "★ 毎日"' in src


# ─────────────────────────────────────────────────
# 7. _apply_content_changes 挙動テスト (2026-07-03 code review MED 対応)
#    M1: 反映成功時に bump_db_version() を 1 回呼ぶ / 全失敗時は呼ばない
#    M2: title は strip 後の値が revise_item_title に渡される
# ─────────────────────────────────────────────────


class _FakeSpinner:
    """`with st.spinner(...) as s:` を no-op で通すための最小 stub."""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeStreamlit:
    """streamlit 未初期化環境で `_apply_content_changes` を直接呼べるようにする stub.

    テスト対象は _apply_content_changes の副作用 (revise 引数 / bump_db_version 呼出) の
    みで、UI 描画そのもの (spinner/success/error) や rerun は verify 対象外。
    """
    def __init__(self):
        self.session_state: dict = {}
        self.spinner_calls: list = []
        self.success_msgs: list = []
        self.error_msgs: list = []
        self.warning_msgs: list = []
        self.info_msgs: list = []
        self.rerun_calls: list = []

    def spinner(self, msg):
        self.spinner_calls.append(msg)
        return _FakeSpinner()

    def success(self, msg):
        self.success_msgs.append(msg)

    def error(self, msg):
        self.error_msgs.append(msg)

    def warning(self, msg):
        self.warning_msgs.append(msg)

    def info(self, msg):
        self.info_msgs.append(msg)

    def rerun(self, *a, **kw):
        # rerun は本物の streamlit では例外を raise して抜けるが、テストでは
        # 「呼ばれた事実」だけ記録して素通しする (以後の assert に到達させる)
        self.rerun_calls.append((a, kw))


def _install_fake_streamlit(monkeypatch):
    from tabs import _finishing_panel as fp
    fake = _FakeStreamlit()
    monkeypatch.setattr(fp, "st", fake)
    return fake


def _install_fake_credentials(monkeypatch):
    import monitor.credentials as cred_mod
    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)


def _install_neutral_db_updates(monkeypatch):
    """全 revise 系 DB 同期関数を no-op に差替 (副作用を持たせない)."""
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "update_ebay_listing_title", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "update_ebay_listing_quantity", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "update_ebay_listing_condition", lambda *a, **kw: None)


def _install_log_stub(monkeypatch):
    """listing_content_change_log を no-op に差替 (監査ログは本テストの対象外)."""
    import monitor.listing_content_change_log as lccl
    monkeypatch.setattr(lccl, "log_content_change", lambda *a, **kw: 1)


def test_apply_content_changes_bumps_db_version_on_success(monkeypatch):
    """M1: title 反映が成功したら bump_db_version() が 1 回呼ばれる."""
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": True, "message": "ok", "new_title": a[1]},
    )

    bump_calls = []
    import ui_cache
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: bump_calls.append(1))

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Old Title", "after": "New Title"},
        "description": {"before": "same", "after": "same"},  # not dirty
        "rank": {"before": None, "after": None},              # not dirty
        "quantity": {"before": 3, "after": 3},                # not dirty
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert bump_calls == [1], (
        f"success 時に bump_db_version() は 1 回呼ばれるべき (got {len(bump_calls)} 回)"
    )


def test_apply_content_changes_bumps_only_once_for_multiple_success(monkeypatch):
    """M1: 複数フィールド成功でも bump_db_version() は 1 回だけ (連続 rerun 抑制)."""
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": True, "message": "ok", "new_title": a[1]},
    )
    monkeypatch.setattr(
        ec_mod, "revise_inventory_quantity",
        lambda *a, **kw: {"success": True, "message": "qty ok"},
    )

    bump_calls = []
    import ui_cache
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: bump_calls.append(1))

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Old", "after": "New"},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 5},
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert bump_calls == [1], f"複数成功でも bump は 1 回のみ (got {len(bump_calls)} 回)"


def test_apply_content_changes_does_not_bump_when_all_fail(monkeypatch):
    """M1: 全 revise が失敗したら bump_db_version() は呼ばない (DB 変わっていないので)."""
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": False, "message": "API エラー"},
    )

    bump_calls = []
    import ui_cache
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: bump_calls.append(1))

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Old", "after": "New"},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 3},
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert bump_calls == [], "全失敗時は bump_db_version() を呼ばない (DB 未変更)"


def test_apply_content_changes_strips_title_before_revise(monkeypatch):
    """M2: 末尾/前後空白付き title は strip 後の値が revise_item_title に渡る.

    未 strip だと eBay/DB/監査ログの 3 経路が末尾空白の有無で乖離する
    (revise_item_title と update_ebay_listing_title は内部で strip するが、
    dispatch_content_changes へ渡す after 引数は生値だった)。
    """
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)

    revise_calls = []

    def _revise(item_id, new_title, *args, **kwargs):
        revise_calls.append(new_title)
        return {"success": True, "message": "ok", "new_title": new_title}

    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(ec_mod, "revise_item_title", _revise)

    log_calls = []
    import monitor.listing_content_change_log as lccl
    monkeypatch.setattr(
        lccl, "log_content_change",
        lambda eid, field, before, after, **kw: log_calls.append((field, after)),
    )

    import ui_cache
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: None)

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Old Title", "after": "  New Title  "},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 3},
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    # revise には strip 済みが渡っている
    assert revise_calls == ["New Title"], (
        f"revise_item_title は strip 済み title を受け取るべき (got {revise_calls!r})"
    )
    # 監査ログの after にも strip 済みが記録される (3 経路一致)
    title_logs = [after for field, after in log_calls if field == "title"]
    assert title_logs == ["New Title"], (
        f"監査ログの after も strip 済みであるべき (got {title_logs!r})"
    )


# ─────────────────────────────────────────────────
# 8. モックパリティ (2026-07-03 修正):
#    description 3 方式 (🤖 AI で生成 / ⬇️ eBay から取得 / ✏️ 手動編集)
# ─────────────────────────────────────────────────

def test_description_field_renders_three_method_radio():
    """モック .btnrow 相当: description に 3 方式選択が明示表示される."""
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "🤖 AI で生成" in src
    assert "⬇️ eBay から取得" in src
    assert "✏️ 手動編集" in src
    # 3 方式を明示する仕組み (radio) が入っていること
    assert 'st.radio(' in src


def test_description_ai_controls_helper_present():
    fn = _find_function_def("_render_description_ai_controls")
    assert fn is not None, "_render_description_ai_controls が定義されていない"
    src = _UI_PATH.read_text(encoding="utf-8")
    # AI 生成経路は state 層の wrapper を経由 (既存 pipeline を import 利用)
    assert "generate_description_via_ai" in src


def test_description_ebay_fetch_helper_present():
    fn = _find_function_def("_render_description_ebay_fetch")
    assert fn is not None, "_render_description_ebay_fetch が定義されていない"


def test_description_ai_uses_resolve_source_url_order():
    """URL 解決順は candidate_url > row.source_url > user 入力 (resolve_source_url 経由)."""
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "resolve_source_url(" in src


# ─────────────────────────────────────────────────
# 9. モックパリティ: 画像 3 モードは URL 未解決でも常時表示
# ─────────────────────────────────────────────────

def test_image_field_helper_present():
    fn = _find_function_def("_render_image_field")
    assert fn is not None, "_render_image_field が定義されていない"


def test_image_field_calls_photo_apply_section_unconditionally():
    """_render_image_field は render_supplier_photo_apply_section を分岐なしで呼ぶ.

    (URL 空文字でも 3 モード renderer 側が自前で error を出す設計。③モードは
    URL 無しでも現行 eBay 画像を取得できるため、常時呼出しが正しい挙動。)
    """
    fn = _find_function_def("_render_image_field")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "render_supplier_photo_apply_section(" in src_seg
    # 3 モード常時表示のため section 全体をラップする if 分岐は無い
    # (URL 未解決 → 入力欄を表示、resolved URL を下流に渡す構造)
    assert "img_source_url" in src_seg


# ─────────────────────────────────────────────────
# 10. state 層 (新規追加): resolve_source_url / generate_description_via_ai
# ─────────────────────────────────────────────────

def test_state_resolve_source_url_priority():
    from tabs._finishing_panel_state import resolve_source_url
    # candidate_url > row.source_url > user_input
    assert resolve_source_url(
        "https://cand.example/1", {"source_url": "https://row.example/2"},
        "https://user.example/3",
    ) == "https://cand.example/1"
    assert resolve_source_url(
        None, {"source_url": "https://row.example/2"}, "https://user.example/3",
    ) == "https://row.example/2"
    assert resolve_source_url(
        None, None, "https://user.example/3",
    ) == "https://user.example/3"
    assert resolve_source_url(None, None, None) == ""
    assert resolve_source_url("", {}, "") == ""
    # 空白のみは未指定扱い
    assert resolve_source_url("   ", {"source_url": "https://row.example/x"}, None) \
        == "https://row.example/x"


def test_state_resolve_source_url_row_none_safe():
    from tabs._finishing_panel_state import resolve_source_url
    # row=None でも落ちず ""
    assert resolve_source_url(None, None, None) == ""


def test_state_generate_description_via_ai_empty_url_rejected():
    from tabs._finishing_panel_state import generate_description_via_ai
    r = generate_description_via_ai("")
    assert r["success"] is False
    assert "URL" in r["message"]
    r2 = generate_description_via_ai("   ")
    assert r2["success"] is False


def test_state_generate_description_via_ai_delegates_to_pipeline(monkeypatch):
    """既存 generate_supplier_description に candidate_id=0 で委譲される (既存 pipeline 再利用)."""
    from tabs import _finishing_panel_state as fps_mod

    calls = []

    def _fake_gen(**kwargs):
        calls.append(kwargs)
        return {
            "success": True, "description_html": "<p>hi</p>",
            "rank_code": "A", "title_en": "Hi", "message": "ok",
        }

    # generate_supplier_description は関数内で lazy import されるため
    # tabs._supplier_description_pipeline 側にパッチする
    import tabs._supplier_description_pipeline as sdp_mod
    monkeypatch.setattr(sdp_mod, "generate_supplier_description", _fake_gen)

    result = fps_mod.generate_description_via_ai(
        "https://example.com/x",
        candidate_id=42, in_stock=True,
        rank_override_code="B", extra_instructions="test",
    )
    assert result["success"] is True
    assert result["description_html"] == "<p>hi</p>"
    assert result["rank_code"] == "A"
    assert len(calls) == 1
    kw = calls[0]
    assert kw["candidate_id"] == 42
    assert kw["candidate_url"] == "https://example.com/x"
    assert kw["in_stock"] is True
    assert kw["rank_override_code"] == "B"
    assert kw["extra_instructions"] == "test"


def test_state_generate_description_via_ai_pipeline_exception_returns_failure(monkeypatch):
    from tabs import _finishing_panel_state as fps_mod
    import tabs._supplier_description_pipeline as sdp_mod

    def _boom(**kwargs):
        raise RuntimeError("scrape blew up")

    monkeypatch.setattr(sdp_mod, "generate_supplier_description", _boom)
    result = fps_mod.generate_description_via_ai("https://example.com/x")
    assert result["success"] is False
    assert "scrape blew up" in result["message"]


# ─────────────────────────────────────────────────
# 11. user feedback 2026-07-03 レイアウト変更:
#     ヘッダ → top_slot (詳細編集 従来) → コンテンツ、
#     「仕入先」「💰 価格・送料」 expander は撤去
# ─────────────────────────────────────────────────

def test_finishing_panel_no_longer_opens_supplier_expander():
    """user feedback 2026-07-03: 「仕入先」expander (仮置き) はパネルから撤去."""
    src = _UI_PATH.read_text(encoding="utf-8")
    # panel の body で `st.expander("仕入先"...)` が呼ばれなくなっている
    assert 'st.expander("仕入先"' not in src


def test_finishing_panel_no_longer_opens_money_zone_expander():
    """user feedback 2026-07-03: 「💰 価格・送料」expander (仮置き) はパネルから撤去.

    価格・送料の編集は詳細編集 (従来) 側で完結するため案内も不要
    (T3 隔離は tab_product_management 側の従来フォーム 2 段確認で確保、
    money-direct 関数の import 禁止テストは他所で維持)。
    """
    src = _UI_PATH.read_text(encoding="utf-8")
    assert 'st.expander("💰 価格・送料' not in src


def test_finishing_panel_helpers_supplier_and_money_zone_preserved():
    """撤去は「呼出のみ」で、関数体自体は Phase 3 再利用余地を残して温存する.

    (関数削除 = 破壊的変更、K1 撤去に留めるため helper は残す。既存の
    test_money_zone_only_has_jump_button_no_price_input 等が引き続き機能する
    ことを保証。)
    """
    assert _find_function_def("_render_supplier_group") is not None, (
        "_render_supplier_group は将来再利用のため残置 (呼出のみ削除)"
    )
    assert _find_function_def("_render_money_zone") is not None, (
        "_render_money_zone は将来再利用のため残置 (呼出のみ削除)"
    )


def test_top_slot_called_between_header_and_content():
    """top_slot は _render_header の直後、コンテンツ expander の手前で呼ばれる.

    AST ベース検査で: `_render_header(...)` の Call → `top_slot()` の Call →
    `st.expander("コンテンツ"...)` の With がこの順序で現れる (user 2026-07-03 要望)。
    docstring 内の "top_slot()" 言及を誤検出しないよう AST を使う。
    """
    fn = _find_function_def("_render_finishing_panel_fragment")
    assert fn is not None

    idx_header = idx_top = idx_content = None
    for node in ast.walk(fn):
        if idx_header is None and isinstance(node, ast.Call) \
                and isinstance(node.func, ast.Name) and node.func.id == "_render_header":
            idx_header = node.lineno
        if idx_top is None and isinstance(node, ast.Call) \
                and isinstance(node.func, ast.Name) and node.func.id == "top_slot":
            idx_top = node.lineno
        if idx_content is None and isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) \
                        and ctx.func.attr == "expander" and ctx.args:
                    arg = ctx.args[0]
                    if isinstance(arg, ast.Constant) and arg.value == "コンテンツ":
                        idx_content = node.lineno
                        break
    assert idx_header is not None, "_render_header() 呼出が見つからない"
    assert idx_top is not None, "top_slot() 呼出が見つからない"
    assert idx_content is not None, "コンテンツ expander が見つからない"
    assert idx_header < idx_top < idx_content, (
        f"順序不正: _render_header L{idx_header} → top_slot L{idx_top} "
        f"→ コンテンツ L{idx_content} の順であるべき"
    )


def test_top_slot_is_optional_and_skippable():
    """top_slot=None (followup 経由 = source_tab != 'product_management') では
    差し込まない = 従来通り「ヘッダ → コンテンツ」になること."""
    fn = _find_function_def("_render_finishing_panel_fragment")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    # None ガード分岐が存在すること
    assert "if top_slot is not None:" in src_seg


# ─────────────────────────────────────────────────
# 12. tab_product_management 側の結線: 詳細編集を top_slot として渡す
# ─────────────────────────────────────────────────

_PM_PATH = _PROJECT_ROOT / "tabs" / "tab_product_management.py"


def test_pm_passes_legacy_editor_as_top_slot():
    """tab_product_management は 「🔧 詳細編集 (従来)」を closure でラップし
    render_finishing_panel(top_slot=...) で渡す (user 2026-07-03 要望).
    """
    src = _PM_PATH.read_text(encoding="utf-8")
    # closure 定義
    assert "def _legacy_editor_top_slot()" in src
    # closure 内で従来 expander を開く
    assert '"🔧 詳細編集 (従来)"' in src
    # top_slot= キーワードで panel に渡す
    assert "top_slot=_legacy_editor_top_slot" in src


def test_pm_no_longer_calls_panel_without_top_slot():
    """商品管理タブでは top_slot 抜きで render_finishing_panel を呼ばない
    (詳細編集を必ずヘッダ直下に差し込むため)."""
    src = _PM_PATH.read_text(encoding="utf-8")
    # 旧 form の呼出 `render_finishing_panel(eid, config, source_tab="product_management")`
    # (改行なし・top_slot 無し) が残存していないこと
    assert (
        'render_finishing_panel(eid, config, source_tab="product_management")'
        not in src
    ), "商品管理タブは top_slot 引数付きで呼び出すべき (2026-07-03 user 要望)"


# ─────────────────────────────────────────────────
# 13. #44 バグ2修正 (2026-07-04): コンディション欄に商品説明が残る/入る対策
# ─────────────────────────────────────────────────

def test_condition_subblock_auto_syncs_cd_on_rank_change():
    """ランク変更時は CD (ConditionDescription) をランク定型へ強制同期し dirty 化する
    (description だけ反映して旧 CD が残存する事故対策、As-Is は対象外)."""
    fn = _find_function_def("_render_condition_subblock")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "cd_auto_last_rank" in src_seg
    assert "resolve_condition_description_for_rank" in src_seg
    assert 'new_rank != "As-Is"' in src_seg


def test_description_ai_controls_uses_deterministic_cd_not_ai_freeform():
    """description AI 生成時、CD は resolve_condition_description_for_rank 経由で
    決定論的に決まる (AI の condition_description をそのまま採用しない)."""
    fn = _find_function_def("_render_description_ai_controls")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "resolve_condition_description_for_rank" in src_seg


# ─────────────────────────────────────────────────
# 14. #44 H2 最終ガード (2026-07-04): item_specifics dispatch_disabled
# ─────────────────────────────────────────────────

def test_apply_content_changes_skips_item_specifics_when_dispatch_disabled(monkeypatch):
    """H2 最終ガード: baseline (GetItem) 取得失敗時は item_specifics が dirty でも
    apply_item_specifics_to_ebay を呼ばない (全置換で既存 Item Specifics を消す事故防止,
    ボタン disabled だけに頼らず apply 関数自体にも防御を入れる)."""
    fake = _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    from tabs import _finishing_panel as fp
    spy_calls = []
    monkeypatch.setattr(
        fp, "apply_item_specifics_to_ebay",
        lambda *a, **kw: spy_calls.append((a, kw)) or {
            "success": True, "message": "ok", "removed_names": [],
        },
    )

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Same", "after": "Same"},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 3},
        "item_specifics": {
            "before": {"Brand": "Sony"},
            "after": {"Brand": "Sony", "Model": "X"},
            "dispatch_disabled": True,
            "dispatch_disabled_reason": "現行値取得失敗",
        },
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert spy_calls == [], (
        "dispatch_disabled=True では apply_item_specifics_to_ebay を呼んではいけない"
    )
    assert any("現行値取得に失敗" in m for m in fake.warning_msgs), (
        "dispatch_disabled のスキップは st.warning で明示すべき (Q0: silent skip 禁止)"
    )


def test_apply_content_changes_applies_item_specifics_when_baseline_ok(monkeypatch):
    """回帰: dispatch_disabled=False (baseline 取得成功) では従来通り反映される."""
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    from tabs import _finishing_panel as fp
    spy_calls = []
    monkeypatch.setattr(
        fp, "apply_item_specifics_to_ebay",
        lambda eid, specifics, **kw: spy_calls.append((eid, specifics)) or {
            "success": True, "message": "ok", "removed_names": [],
        },
    )

    from tabs._finishing_panel import _apply_content_changes

    fields = {
        "title": {"before": "Same", "after": "Same"},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 3},
        "item_specifics": {
            "before": {"Brand": "Sony"},
            "after": {"Brand": "Sony", "Model": "X"},
            "dispatch_disabled": False,
        },
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert len(spy_calls) == 1
    assert spy_calls[0][0] == "123456789012"
    assert spy_calls[0][1] == {"Brand": "Sony", "Model": "X"}


def test_apply_content_changes_item_specifics_not_dirty_no_call(monkeypatch):
    """回帰: item_specifics が dirty でなければ dispatch_disabled 有無に関わらず
    apply_item_specifics_to_ebay を呼ばない (既存 dirty ガード自体は不変)."""
    _install_fake_streamlit(monkeypatch)
    _install_fake_credentials(monkeypatch)
    _install_neutral_db_updates(monkeypatch)
    _install_log_stub(monkeypatch)

    from tabs import _finishing_panel as fp
    spy_calls = []
    monkeypatch.setattr(
        fp, "apply_item_specifics_to_ebay",
        lambda *a, **kw: spy_calls.append((a, kw)) or {
            "success": True, "message": "ok", "removed_names": [],
        },
    )

    from tabs._finishing_panel import _apply_content_changes

    same = {"Brand": "Sony"}
    fields = {
        "title": {"before": "Same", "after": "Same"},
        "description": {"before": "same", "after": "same"},
        "rank": {"before": None, "after": None},
        "quantity": {"before": 3, "after": 3},
        "item_specifics": {"before": same, "after": dict(same), "dispatch_disabled": True},
    }
    _apply_content_changes(
        "123456789012", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )
    assert spy_calls == []


# ─────────────────────────────────────────────────
# 15. HIGH (2026-07-04 Codex): AI 生成 item_specifics は baseline と merge、
#     完全置換で既存 Brand/MPN 等が消えるバグの回帰テスト
# ─────────────────────────────────────────────────

class _FakeStreamlitFullish:
    """`_render_description_ai_controls` を単体で駆動できる最小 Streamlit stub.

    このパネルで呼ばれる widget は text_input / selectbox / text_area / button /
    spinner / warning / info / rerun / session_state 全部を最小実装で持たせる。
    ボタン挙動は button_returns dict の key マッチで制御する
    (key 一致で True を返し 1 回だけ「クリックされた」ことにする)。
    """
    def __init__(self, button_returns=None):
        self.session_state: dict = {}
        self.warning_msgs: list = []
        self.info_msgs: list = []
        self.success_msgs: list = []
        self.error_msgs: list = []
        self.caption_msgs: list = []
        self.rerun_calls: list = []
        self._button_returns = button_returns or {}

    def _widget_noop(self, *a, **kw):
        return self.session_state.get(kw.get("key"), None)

    def text_input(self, *a, **kw):
        return self._widget_noop(*a, **kw)

    def text_area(self, *a, **kw):
        return self._widget_noop(*a, **kw)

    def selectbox(self, *a, **kw):
        return self._widget_noop(*a, **kw)

    def button(self, label, *, key=None, **kw):
        return bool(self._button_returns.get(key, False))

    def spinner(self, *a, **kw):
        return _FakeSpinner()

    def warning(self, msg):
        self.warning_msgs.append(msg)

    def info(self, msg):
        self.info_msgs.append(msg)

    def caption(self, msg):
        self.caption_msgs.append(msg)

    def success(self, msg):
        self.success_msgs.append(msg)

    def error(self, msg):
        self.error_msgs.append(msg)

    def rerun(self, *a, **kw):
        self.rerun_calls.append((a, kw))


def _install_fake_streamlit_fullish(monkeypatch, button_returns=None):
    from tabs import _finishing_panel as fp
    fake = _FakeStreamlitFullish(button_returns=button_returns or {})
    monkeypatch.setattr(fp, "st", fake)
    return fake


def test_ai_generation_merges_specifics_with_baseline_preserving_missing_keys(monkeypatch):
    """HIGH (2026-07-04 Codex): AI 生成 Item Specifics は baseline (eBay 現在値) と
    merge され、AI が省略した Key (例: MPN/UPC) が session_state に保持されること."""
    from tabs._finishing_panel_state import pf_key

    eid = "123456789012"
    button_returns = {pf_key(eid, "desc_ai_run_btn"): True}
    fake = _install_fake_streamlit_fullish(monkeypatch, button_returns=button_returns)

    # baseline (eBay 現在値) を session_state に事前セット
    # (通常 `_render_item_specifics_field` の seed_initial が立てる)
    fake.session_state[pf_key(eid, "item_specifics_initial")] = {
        "Brand": "Sony",
        "MPN": "WH-1000XM5",
        "UPC": "027242920163",
        "Model": "WH-1000XM5",
    }

    # AI 生成結果: reference Keys 5 個 + Brand 上書き + 新規 Color (Codex 指摘の典型)。
    # MPN / UPC は AI 出力に含まれない (省略)。
    def _fake_gen(*a, **kw):
        return {
            "success": True,
            "description_html": "<div>desc</div>",
            "rank_code": "A",
            "title_en": "T",
            "item_specifics": {
                "Brand": "Sony Corp",   # 上書き
                "Type": "Headphones",   # 新規
                "Color": "Black",        # 新規
            },
            "condition_description": "Tested and fully working.",
            "message": "生成完了",
        }

    from tabs import _finishing_panel_state as fps
    monkeypatch.setattr(fps, "generate_description_via_ai", _fake_gen)
    from tabs import _finishing_panel as fp
    monkeypatch.setattr(fp, "generate_description_via_ai", _fake_gen)

    row = {
        "sku": "stock:01", "title": "T", "condition_rank": "A",
        "source_url": "", "listing_description": "",
    }
    fp._render_description_ai_controls(
        eid, row, desc_key=pf_key(eid, "description"),
        candidate_id=None, candidate_url=None,
    )

    current = fake.session_state.get(pf_key(eid, "item_specifics_current"))
    assert current is not None, "AI 生成成功時は item_specifics_current が session_state に立つ"
    # baseline の非対象 Key (MPN / UPC) が残存
    assert current.get("MPN") == "WH-1000XM5", (
        "AI が省略した MPN は baseline から保持されるべき "
        f"(got {current!r})"
    )
    assert current.get("UPC") == "027242920163", (
        "AI が省略した UPC は baseline から保持されるべき"
    )
    # AI の上書きは効いている
    assert current["Brand"] == "Sony Corp"
    # AI の新規追加も入っている
    assert current["Type"] == "Headphones"
    assert current["Color"] == "Black"
    # baseline にのみ存在した Model も保持
    assert current["Model"] == "WH-1000XM5"


def test_ai_generation_merge_still_works_when_baseline_missing(monkeypatch):
    """HIGH 回帰: baseline (item_specifics_initial) 未 seed 時 (transient エラーで
    dispatch_disabled 予定) でも AI 生成 auto-set が壊れないこと (merge 元が空 dict と
    して振る舞う)。"""
    from tabs._finishing_panel_state import pf_key

    eid = "999999999999"
    button_returns = {pf_key(eid, "desc_ai_run_btn"): True}
    fake = _install_fake_streamlit_fullish(monkeypatch, button_returns=button_returns)

    def _fake_gen(*a, **kw):
        return {
            "success": True, "description_html": "d", "rank_code": "A",
            "title_en": "T", "item_specifics": {"Brand": "Sony"},
            "condition_description": "Tested.", "message": "ok",
        }

    from tabs import _finishing_panel as fp
    from tabs import _finishing_panel_state as fps
    monkeypatch.setattr(fps, "generate_description_via_ai", _fake_gen)
    monkeypatch.setattr(fp, "generate_description_via_ai", _fake_gen)

    row = {"sku": "stock:01", "title": "T", "condition_rank": "A",
           "source_url": "", "listing_description": ""}
    fp._render_description_ai_controls(
        eid, row, desc_key=pf_key(eid, "description"),
        candidate_id=None, candidate_url=None,
    )
    current = fake.session_state.get(pf_key(eid, "item_specifics_current"))
    assert current == {"Brand": "Sony"}, (
        f"baseline 未 seed でも AI 生成値のみで設定される (got {current!r})"
    )


# ─────────────────────────────────────────────────
# 16. MED (2026-07-04 Codex): multi-value aspect 検出 → dispatch_disabled
# ─────────────────────────────────────────────────

_MULTI_VALUE_ITEMSPECIFICS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ConditionDescription>Tested.</ConditionDescription>
    <ItemSpecifics>
      <NameValueList>
        <Name>Brand</Name>
        <Value>Sony</Value>
      </NameValueList>
      <NameValueList>
        <Name>Features</Name>
        <Value>Bluetooth</Value>
        <Value>Noise Cancelling</Value>
        <Value>Waterproof</Value>
      </NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>
"""


def test_fetch_condition_and_specifics_flags_multi_value_aspects(monkeypatch):
    """MED (2026-07-04 Codex): 同一 Name に複数 <Value> がある aspect を検出し
    `multi_value_names` に列挙されること (dict 側は先頭値で保持)."""
    import httpx
    from tabs._finishing_panel_state import fetch_condition_and_specifics_from_ebay

    class _R:
        text = _MULTI_VALUE_ITEMSPECIFICS_XML
        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _R())

    import monitor.credentials as cred_mod
    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)

    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(ec_mod, "_resolve_active_token", lambda t: t)
    monkeypatch.setattr(ec_mod, "_build_get_item_xml", lambda iid: "<x>{USER_TOKEN}</x>")

    res = fetch_condition_and_specifics_from_ebay("111111111111")
    assert res["success"] is True
    assert res["multi_value_names"] == ["Features"], (
        f"'Features' が multi-value として検出されるべき (got {res['multi_value_names']!r})"
    )
    # dict は先頭値のみ (dispatch_disabled で反映されない前提)
    assert res["item_specifics"]["Brand"] == "Sony"
    assert res["item_specifics"]["Features"] == "Bluetooth"


def test_render_item_specifics_field_flags_dispatch_disabled_for_multi_value(monkeypatch):
    """MED (2026-07-04 Codex): multi_value_names 非空 → fields['item_specifics'] の
    dispatch_disabled=True で載る (H2 と同じフラグ)."""
    from tabs._finishing_panel_state import pf_key

    eid = "222222222222"
    fake = _install_fake_streamlit_fullish(monkeypatch)

    # data_editor / markdown / column_config は fake に無いので最小実装を足す
    def _fake_data_editor(rows, **kw):
        return rows

    class _ColConfig:
        def TextColumn(self, *a, **kw):
            return None

    fake.data_editor = _fake_data_editor
    fake.column_config = _ColConfig()
    fake.markdown = lambda *a, **kw: None

    from tabs import _finishing_panel as fp
    # snapshot cache を multi_value_names 有りで直接注入
    fake.session_state[pf_key(eid, "cond_snapshot")] = {
        "success": True,
        "condition_description": "Tested.",
        "item_specifics": {"Brand": "Sony", "Features": "Bluetooth"},
        "multi_value_names": ["Features"],
        "message": "取得しました",
    }
    fields: dict = {}
    fp._render_item_specifics_field(eid, {}, config=None, fields=fields)

    assert fields["item_specifics"]["dispatch_disabled"] is True
    reason = fields["item_specifics"]["dispatch_disabled_reason"] or ""
    assert "複数値項目" in reason
    assert "Features" in reason
    assert any("複数値項目" in m for m in fake.warning_msgs), (
        f"multi-value 検出は st.warning で明示すべき (got {fake.warning_msgs!r})"
    )
