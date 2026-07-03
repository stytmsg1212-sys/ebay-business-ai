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

def test_no_candidate_url_shows_guidance_message():
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "仕入先 URL 未指定のため画像モードは利用できません" in src


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
        self.rerun_calls: list = []

    def spinner(self, msg):
        self.spinner_calls.append(msg)
        return _FakeSpinner()

    def success(self, msg):
        self.success_msgs.append(msg)

    def error(self, msg):
        self.error_msgs.append(msg)

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
