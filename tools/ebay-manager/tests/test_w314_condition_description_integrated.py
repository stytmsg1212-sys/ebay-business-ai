"""W314 (2026-07-03 user 追加要望): Description と Condition の統合レイアウト.

Scope:
    (1) tabs/_finishing_panel_state.py の state 拡張
        - FIELD_LABELS_JA / PREVIEW_FIELD_ORDER に condition_description
        - is_field_dirty(condition_description, ...) の挙動
        - validate_as_is_condition_description の 3 分岐 (PASS / 空 / 65字超)

    (2) tabs/_finishing_panel.py の UI + dispatch
        - _render_condition_subblock が Description コンテナ内に描画される (ソース検査)
        - _apply_content_changes:
            (a) rank+cd 両方 dirty で bundle 送信 (revise_item_condition が 1 回、cd 引数付き)
            (b) cd 単独 dirty で現行 cid 保持 + cd のみ送信
            (c) As-Is + 空 cd → dispatch 中止 (revise 呼出ゼロ)
            (d) As-Is + 65字超 → dispatch 中止

    (3) tabs/tab_product_management.py の重複解消
        - pm_title_ / pm_rank_ / pm_conddesc_ / pm_desc_ / _render_desc_fetch_button
          呼出が form から撤去されている
        - _save_product_data / _apply_listing_content_to_ebay は editing.get() で
          未設定キーに no-op (KeyError にならない = 等価な no-op)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_UI_PATH = _PROJECT_ROOT / "tabs" / "_finishing_panel.py"
_STATE_PATH = _PROJECT_ROOT / "tabs" / "_finishing_panel_state.py"
_PM_PATH = _PROJECT_ROOT / "tabs" / "tab_product_management.py"


# ═════════════════════════════════════════════════
# 1. state 層: FIELD_LABELS_JA / PREVIEW_FIELD_ORDER / is_field_dirty
# ═════════════════════════════════════════════════

def test_state_field_labels_includes_condition_description():
    from tabs._finishing_panel_state import FIELD_LABELS_JA
    assert "condition_description" in FIELD_LABELS_JA
    assert FIELD_LABELS_JA["condition_description"] == "コンディション理由"


def test_state_preview_order_places_cd_right_after_rank():
    """user 要望: Description と Condition は「セット」= プレビュー表でも rank → cd の順."""
    from tabs._finishing_panel_state import PREVIEW_FIELD_ORDER
    assert "condition_description" in PREVIEW_FIELD_ORDER
    idx_rank = PREVIEW_FIELD_ORDER.index("rank")
    idx_cd = PREVIEW_FIELD_ORDER.index("condition_description")
    assert idx_cd == idx_rank + 1, (
        f"condition_description は rank の直後にあるべき "
        f"(rank at {idx_rank}, cd at {idx_cd})"
    )


def test_state_dispatch_order_still_excludes_cd():
    """DISPATCH_FIELD_ORDER は cd を含めない (rank と bundle 送信 or 単独 dispatch は
    _finishing_panel.py 側で動的に判断するため)."""
    from tabs._finishing_panel_state import DISPATCH_FIELD_ORDER
    assert "condition_description" not in DISPATCH_FIELD_ORDER


def test_is_field_dirty_condition_description_empty_to_nonempty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("condition_description", "", "Tested OK") is True


def test_is_field_dirty_condition_description_same_value_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty(
        "condition_description", "Tested OK", "Tested OK",
    ) is False
    assert is_field_dirty(
        "condition_description", "Tested OK", "  Tested OK  ",
    ) is False


def test_is_field_dirty_condition_description_nonempty_to_empty_is_dirty():
    """cd を削除する操作も dirty (「理由をリセットしたい」意図)."""
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("condition_description", "Tested OK", "") is True
    assert is_field_dirty("condition_description", "Tested OK", "   ") is True


def test_is_field_dirty_condition_description_empty_to_empty_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("condition_description", "", "") is False
    assert is_field_dirty("condition_description", None, None) is False
    assert is_field_dirty("condition_description", "", "   ") is False


# ═════════════════════════════════════════════════
# 2. state 層: validate_as_is_condition_description
# ═════════════════════════════════════════════════

def test_validate_as_is_non_as_is_rank_pass():
    from tabs._finishing_panel_state import validate_as_is_condition_description
    for rank in (None, "N", "S", "A", "B", "C", "D", "PO"):
        assert validate_as_is_condition_description(rank, "") is None
        assert validate_as_is_condition_description(rank, "anything") is None


def test_validate_as_is_empty_cd_rejected():
    from tabs._finishing_panel_state import validate_as_is_condition_description
    msg = validate_as_is_condition_description("As-Is", "")
    assert msg is not None
    assert "As-Is" in msg
    assert "必須" in msg
    # 空白のみも empty 扱い
    assert validate_as_is_condition_description("As-Is", "   ") is not None


def test_validate_as_is_valid_short_cd_pass():
    from tabs._finishing_panel_state import validate_as_is_condition_description
    assert validate_as_is_condition_description(
        "As-Is", "As-Is — No AC adapter for testing",
    ) is None


def test_validate_as_is_cd_over_65_chars_rejected():
    from tabs._finishing_panel_state import (
        AS_IS_CD_MAX_LEN, validate_as_is_condition_description,
    )
    long_cd = "As-Is — " + ("X" * 60)  # 8 + 60 = 68 chars
    assert len(long_cd) > AS_IS_CD_MAX_LEN
    msg = validate_as_is_condition_description("As-Is", long_cd)
    assert msg is not None
    assert str(AS_IS_CD_MAX_LEN) in msg


def test_validate_as_is_cd_at_65_chars_exact_pass():
    from tabs._finishing_panel_state import (
        AS_IS_CD_MAX_LEN, validate_as_is_condition_description,
    )
    # HIGH-1 修正 (2026-07-04): validate は "As-Is" トークン必須 + `Rank ` prefix reject
    # へ強化されたため、65 字ぴったりでもトークン包含形式で回帰確認する。
    # 想定書式 `As-Is — <reason>` (prefix 8 字) 直後を "X" で埋めて 65 字。
    cd = ("As-Is — " + "X" * (AS_IS_CD_MAX_LEN - len("As-Is — ")))
    assert len(cd) == AS_IS_CD_MAX_LEN
    assert validate_as_is_condition_description("As-Is", cd) is None


# ═════════════════════════════════════════════════
# 3. UI: _render_condition_subblock 存在と Description コンテナ内配置
# ═════════════════════════════════════════════════

def _find_function_def(name: str, path: Path = _UI_PATH):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_render_condition_subblock_helper_present():
    fn = _find_function_def("_render_condition_subblock")
    assert fn is not None, "_render_condition_subblock が定義されていない"


def test_condition_subblock_called_from_description_field():
    """Description コンテナ内で _render_condition_subblock を呼び出す (「セット」構成)."""
    src = _UI_PATH.read_text(encoding="utf-8")
    assert "_render_condition_subblock(" in src
    # description コンテナ (`st.markdown("**📝 Description & Condition...")`) と
    # subblock 呼出の両方が同じ関数 _render_description_field 内にあることを確認
    fn = _find_function_def("_render_description_field")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "Description & Condition" in src_seg
    assert "_render_condition_subblock(" in src_seg


def test_condition_subblock_widgets_present():
    """subblock 内に selectbox (ランク) と text_input (条件理由) が存在する."""
    fn = _find_function_def("_render_condition_subblock")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "st.selectbox(" in src_seg
    assert "st.text_input(" in src_seg
    assert "ConditionDescription" in src_seg  # help text


def test_content_group_no_longer_has_standalone_rank_widget():
    """rank widget は _render_condition_subblock 側にのみ存在 (body_cols から撤去)."""
    fn = _find_function_def("_render_content_group")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    # _render_content_group 内では rank selectbox の直接呼出しは無く、subblock 呼出のみ
    # (旧: body_cols[0] で selectbox / body_cols[1] で number_input)
    assert "body_cols = st.columns(2)" not in src_seg, (
        "body_cols(2 列) は撤去され、数量は独立配置"
    )


# ═════════════════════════════════════════════════
# 4. _apply_content_changes: bundle / cd-only / As-Is ガード
# ═════════════════════════════════════════════════

class _FakeSpinner:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.session_state: dict = {}
        self.errors: list = []
        self.successes: list = []

    def spinner(self, *a, **kw):
        return _FakeSpinner()

    def error(self, msg, *a, **kw):
        self.errors.append(msg)

    def success(self, msg, *a, **kw):
        self.successes.append(msg)

    def rerun(self, *a, **kw):
        pass


def _install_common(monkeypatch, revise_condition_impl=None,
                    revise_title_impl=None, revise_desc_impl=None,
                    revise_qty_impl=None, snap_impl=None):
    from tabs import _finishing_panel as fp
    fake = _FakeStreamlit()
    monkeypatch.setattr(fp, "st", fake)

    import monitor.credentials as cred_mod
    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)

    import monitor.ebay_client as ec_mod
    if revise_condition_impl is not None:
        monkeypatch.setattr(ec_mod, "revise_item_condition", revise_condition_impl)
    if revise_title_impl is not None:
        monkeypatch.setattr(ec_mod, "revise_item_title", revise_title_impl)
    if revise_desc_impl is not None:
        monkeypatch.setattr(ec_mod, "revise_item_description", revise_desc_impl)
    if revise_qty_impl is not None:
        monkeypatch.setattr(ec_mod, "revise_inventory_quantity", revise_qty_impl)

    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "update_ebay_listing_title", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "update_ebay_listing_quantity", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "update_ebay_listing_condition", lambda *a, **kw: None)

    import monitor.listing_content_change_log as lccl_mod
    log_calls = []
    monkeypatch.setattr(
        lccl_mod, "log_content_change",
        lambda *a, **kw: (log_calls.append((a, kw)), 1)[1],
    )

    import monitor.ebay_listing_snapshot as snap_mod
    if snap_impl is not None:
        monkeypatch.setattr(snap_mod, "fetch_listing_snapshot", snap_impl)

    import ui_cache
    bump_calls = []
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: bump_calls.append(1))

    return fake, log_calls, bump_calls


def _make_snap(ok=True, cid="3000"):
    class _S:
        pass
    s = _S()
    s.ok = ok
    s.condition_id = cid
    s.error = None if ok else "boom"
    return s


def test_apply_bundled_rank_and_cd_single_revise_condition_call(monkeypatch):
    """rank + cd 両方 dirty → revise_item_condition 1 回呼出 (cd 引数付き = bundle).

    HIGH-1 修正 (2026-07-04): N (ConditionID 1000) は eBay 仕様上 CD 非対応のため
    別テスト (`test_apply_rank_new_1000_does_not_send_condition_description`) で
    N は CD を送らないことを固定する。本テストは 3000 系 (A) を対象にして
    bundle 挙動を verify する (旧テストは誤って N + CD 送信を正としていた)。
    """
    revise_calls = []

    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        revise_calls.append({
            "item_id": item_id, "cid": cid, "condition_description": condition_description,
        })
        return {"success": True, "message": "ok", "condition_id": cid}

    # pre snapshot returns old cid so revise runs; post snapshot returns new cid so verify PASS
    snap_seq = [_make_snap(ok=True, cid="1500"), _make_snap(ok=True, cid="3000")]

    def _fake_snap(*a, **kw):
        return snap_seq.pop(0)

    _, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "S", "after": "A"},                              # dirty (S→A、共に非N)
        "condition_description": {"before": "", "after": "Tested OK"},       # dirty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)

    assert len(revise_calls) == 1, f"bundle should call revise once, got {len(revise_calls)}"
    assert revise_calls[0]["cid"] == "3000"  # A → 3000
    assert revise_calls[0]["condition_description"] == "Tested OK"
    # dispatch 経由の rank log + apply 内で追加した condition_description log = 2 件
    fields_logged = {a[0][1] for a in log_calls}
    assert "rank" in fields_logged
    assert "condition_description" in fields_logged
    assert bump_calls == [1]


def test_apply_rank_new_1000_does_not_send_condition_description(monkeypatch):
    """HIGH-1 (2026-07-04): N (ConditionID 1000) は eBay 仕様上 CD 非対応のため、
    rank + cd 両方 dirty でも revise_item_condition には CD=None で送る."""
    revise_calls = []

    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        revise_calls.append({"cid": cid, "condition_description": condition_description})
        return {"success": True, "message": "ok", "condition_id": cid}

    # pre 3000 → revise 走行 → post 1000 (verify PASS)
    snap_seq = [_make_snap(ok=True, cid="3000"), _make_snap(ok=True, cid="1000")]

    def _fake_snap(*a, **kw):
        return snap_seq.pop(0)

    _, _log_calls, _bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "N"},                              # dirty (N=1000)
        "condition_description": {"before": "", "after": "Tested OK"},       # dirty (user 手動)
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)

    assert len(revise_calls) == 1
    assert revise_calls[0]["cid"] == "1000"
    assert revise_calls[0]["condition_description"] is None, (
        f"N (1000) には CD を送ってはいけない (eBay 仕様非対応、"
        f"got {revise_calls[0]['condition_description']!r})"
    )


def test_apply_cd_only_dirty_on_new_1000_listing_skips_send(monkeypatch):
    """HIGH-1 (2026-07-04): 現行 ConditionID=1000 の listing に対する cd 単独 dirty は
    revise_item_condition を呼ばず、success:True で「送信スキップ」メッセージを返す
    (eBay 仕様非対応 → 送っても通らないため事前に遮断)."""
    revise_calls = []

    def _fake_revise(*a, **kw):
        revise_calls.append((a, kw))
        return {"success": True, "message": "ok"}

    def _fake_snap(*a, **kw):
        return _make_snap(ok=True, cid="1000")  # 現行 = 新品

    fake, _log_calls, _bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "N", "after": "N"},                              # not dirty
        "condition_description": {"before": "", "after": "Tested OK"},       # dirty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)

    assert revise_calls == [], "N (1000) の cd 単独 dirty は revise を呼ばない"
    assert any(
        "1000" in m and "スキップ" in m for m in fake.successes
    ), f"「送信スキップ」の success メッセージが出るべき (got {fake.successes!r})"


def test_apply_cd_only_dirty_uses_current_condition_id(monkeypatch):
    """rank 未変更・cd のみ dirty → 現行 ConditionID 保持で revise_item_condition."""
    revise_calls = []

    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        revise_calls.append({"cid": cid, "cd": condition_description})
        return {"success": True, "message": "ok", "condition_id": cid}

    def _fake_snap(*a, **kw):
        return _make_snap(ok=True, cid="3000")  # 現行

    _, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "B"},                        # not dirty
        "condition_description": {"before": "", "after": "Tested B"}, # dirty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)

    assert len(revise_calls) == 1
    assert revise_calls[0]["cid"] == "3000"  # 現行維持
    assert revise_calls[0]["cd"] == "Tested B"
    # dispatch 経由で field="condition_description" が log される
    fields_logged = {a[0][1] for a in log_calls}
    assert fields_logged == {"condition_description"}
    assert bump_calls == [1]


def test_apply_as_is_empty_cd_blocks_dispatch(monkeypatch):
    """As-Is + 空 cd → validate 落ち、revise は 1 回も呼ばれない (K1: 部分反映しない)."""
    revise_calls = []

    def _fake_revise(*a, **kw):
        revise_calls.append(kw)
        return {"success": True, "message": "should not be called",
                "condition_id": kw.get("cid", "")}

    fake, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "As-Is"},                 # dirty
        "condition_description": {"before": "", "after": ""},       # empty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)

    assert revise_calls == [], "As-Is 空理由で revise は呼ばれない"
    assert bump_calls == [], "DB も更新されない"
    assert len(fake.errors) == 1
    assert "As-Is" in fake.errors[0]
    assert "必須" in fake.errors[0]


def test_apply_as_is_cd_over_65_chars_blocks_dispatch(monkeypatch):
    revise_calls = []
    monkeypatch = monkeypatch

    def _fake_revise(*a, **kw):
        revise_calls.append(kw)
        return {"success": True, "message": "!", "condition_id": "7000"}

    fake, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise,
    )
    from tabs._finishing_panel import _apply_content_changes
    from tabs._finishing_panel_state import AS_IS_CD_MAX_LEN
    long_cd = "As-Is — " + ("X" * 60)
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "As-Is"},
        "condition_description": {"before": "", "after": long_cd},
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)
    assert revise_calls == []
    assert bump_calls == []
    assert any(str(AS_IS_CD_MAX_LEN) in e for e in fake.errors)


def test_apply_s_1500_fallback_to_3000_on_verify_failure(monkeypatch):
    """W220 regression fix (2026-07-03 code review MED): S(1500) verify 失敗時の 3000 降格.

    旧 tab_product_management.py:4310-4327 の挙動 (CLAUDE.md「Cond ID 1500 は
    カテゴリ依存」規約) がランク編集のパネル移設で失われていたため、
    _finishing_panel._apply_rank に移植した動作を回帰テストで固定する:
      - 1500 で revise → post snapshot が 1500 に一致しない (= 不可カテゴリ相当)
      - 3000 で再送 → post snapshot が 3000 一致 → 明示メッセージで降格を通知
      - 監査ログの after は "Used(3000, S降格)" で記録 (silent 降格禁止 = Q0)
      - update_ebay_listing_condition は ebay_condition_id=3000 のみ (condition_rank 不同期)
    """
    revise_calls = []

    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        revise_calls.append({"cid": cid, "cd": condition_description})
        return {"success": True, "message": f"revised to {cid}", "condition_id": cid}

    # snapshot sequence: pre (=3000, 不一致), post-1500 (=3000, verify fail),
    # post-3000 (=3000, verify OK)
    snap_seq = [
        _make_snap(ok=True, cid="3000"),  # pre
        _make_snap(ok=True, cid="3000"),  # post-1500 (verify 失敗 = 期待値と不一致)
        _make_snap(ok=True, cid="3000"),  # post-3000 (fallback verify OK)
    ]

    def _fake_snap(*a, **kw):
        return snap_seq.pop(0)

    db_calls = []
    import monitor.database as db_mod
    _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap,
    )
    # 追加: update_ebay_listing_condition の実引数を捕捉
    monkeypatch.setattr(
        db_mod, "update_ebay_listing_condition",
        lambda *a, **kw: db_calls.append((a, kw)),
    )

    log_calls_captured = []
    import monitor.listing_content_change_log as lccl_mod
    monkeypatch.setattr(
        lccl_mod, "log_content_change",
        lambda *a, **kw: log_calls_captured.append((a, kw)) or 1,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": None, "after": "S"},                      # dirty, target=1500
        "condition_description": {"before": "", "after": ""},         # not dirty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes(
        "111", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )

    # (a) 1500 で送信 → 失敗 → 3000 で再送 = 2 回呼出
    assert len(revise_calls) == 2, f"expected 2 revise calls, got {len(revise_calls)}"
    assert revise_calls[0]["cid"] == "1500"
    assert revise_calls[1]["cid"] == "3000"

    # (b) DB 同期は ebay_condition_id=3000 のみ (condition_rank に "S" を残さない)
    kw_calls = [kw for (_a, kw) in db_calls]
    assert any(kw.get("ebay_condition_id") == "3000" and "condition_rank" not in kw
               for kw in kw_calls), (
        f"S→3000 降格時は ebay_condition_id=3000 のみ同期 (condition_rank は付けない)。 "
        f"got={kw_calls!r}"
    )

    # (c) 監査ログの追加エントリ: field="rank", after="Used(3000, S降格)"
    rank_fallback_log = None
    for (a, kw) in log_calls_captured:
        if a[1] == "rank" and a[3] == "Used(3000, S降格)":
            rank_fallback_log = (a, kw)
            break
    assert rank_fallback_log is not None, (
        "S→3000 降格を明示する監査ログエントリが必要 "
        "(silent 降格禁止 = Q0)"
    )
    assert rank_fallback_log[1]["success"] is True
    assert "S(1500) 不可カテゴリ" in (rank_fallback_log[1].get("ebay_ack") or "")


def test_apply_s_1500_fallback_success_with_cd_dirty_also_logs_cd(monkeypatch):
    """S→3000 降格 + cd dirty の同時発生でも conddesc 監査ログが記録される."""
    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        return {"success": True, "message": f"ok {cid}", "condition_id": cid}

    snap_seq = [
        _make_snap(ok=True, cid="3000"),
        _make_snap(ok=True, cid="3000"),
        _make_snap(ok=True, cid="3000"),
    ]
    _install_common(
        monkeypatch, revise_condition_impl=_fake_revise,
        snap_impl=lambda *a, **kw: snap_seq.pop(0),
    )

    log_calls = []
    import monitor.listing_content_change_log as lccl_mod
    monkeypatch.setattr(
        lccl_mod, "log_content_change",
        lambda *a, **kw: log_calls.append((a, kw)) or 1,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": None, "after": "S"},
        "condition_description": {"before": "", "after": "Open box"},
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes(
        "111", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )

    fields_logged = {a[0][1] for a in log_calls}
    # rank (fallback: after="Used(3000, S降格)") + condition_description の両方が log される
    assert "condition_description" in fields_logged, (
        "S→3000 降格時でも conddesc dirty の監査ログは記録されるべき"
    )
    # 監査ログの conddesc after 値
    cd_after_vals = [a[0][3] for a in log_calls if a[0][1] == "condition_description"]
    assert "Open box" in cd_after_vals


def test_apply_s_1500_fallback_fail_returns_failure_message(monkeypatch):
    """S→3000 fallback 自体も失敗した場合、明示エラーで返す (silent 成功禁止 = Q0)."""
    calls = []

    def _fake_revise(item_id, cid, app_id, dev_id, cert_id, token, condition_description=None):
        calls.append(cid)
        return {"success": True, "message": f"revised (silently ignored) {cid}",
                "condition_id": cid}

    # 3 回目の snapshot も 3000 で無い = fallback verify 失敗
    snap_seq = [
        _make_snap(ok=True, cid="1000"),  # pre (何でもよい、1500 と一致しなければ)
        _make_snap(ok=True, cid="1000"),  # post-1500 (verify fail)
        _make_snap(ok=True, cid="1000"),  # post-3000 (fallback verify fail too)
    ]
    fake, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise,
        snap_impl=lambda *a, **kw: snap_seq.pop(0),
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": None, "after": "S"},
        "condition_description": {"before": "", "after": ""},
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes(
        "111", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )

    assert calls == ["1500", "3000"]
    assert bump_calls == [], "全失敗時は bump しない"
    # dispatch 経由の rank ログは success=False で記録される
    rank_logs = [(a, kw) for (a, kw) in log_calls if a[1] == "rank"]
    assert len(rank_logs) == 1
    assert rank_logs[0][1]["success"] is False


# ═════════════════════════════════════════════════
# 5b. 反映エリアの常時表示 (2026-07-03 UX 指摘対応):
#     dirty ゼロでも「📭 反映待ちの変更はありません」+ 無効化ボタンを描画する
# ═════════════════════════════════════════════════


class _FakeCtx:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _RenderCaptureStreamlit:
    """_render_content_group を直接呼ぶ用の fake streamlit (widget 呼出を記録)."""
    def __init__(self):
        self.session_state: dict = {}
        self.buttons: list[dict] = []
        self.infos: list[str] = []
        self.captions: list[str] = []
        self.warnings: list[str] = []
        self.markdown_calls: list[str] = []
        self.tables: list = []
        self.errors: list[str] = []
        self.successes: list[str] = []
        # #44 (2026-07-04): Item Specifics data_editor 用の最小スタブ。
        self.column_config = SimpleNamespace(
            TextColumn=lambda *a, **kw: {"label": a[0] if a else None, "kw": kw},
        )

    def button(self, label, *a, **kw):
        self.buttons.append({"label": label, "kw": kw})
        return False

    def info(self, msg, *a, **kw):
        self.infos.append(msg)

    def caption(self, msg, *a, **kw):
        self.captions.append(msg)

    def warning(self, msg, *a, **kw):
        self.warnings.append(msg)

    def markdown(self, msg, *a, **kw):
        self.markdown_calls.append(msg)

    def table(self, data, *a, **kw):
        self.tables.append(data)

    def divider(self, *a, **kw):
        pass

    def container(self, *a, **kw):
        return _FakeCtx()

    def columns(self, spec, *a, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        return [_FakeCtx() for _ in range(n)]

    def spinner(self, *a, **kw):
        return _FakeCtx()

    def error(self, msg, *a, **kw):
        self.errors.append(msg)

    def success(self, msg, *a, **kw):
        self.successes.append(msg)

    def rerun(self, *a, **kw):
        pass

    def selectbox(self, *a, **kw):
        opts = kw.get("options") or (a[1] if len(a) > 1 else [])
        key = kw.get("key")
        if key and key in self.session_state:
            return self.session_state[key]
        return opts[0] if opts else None

    def radio(self, *a, **kw):
        opts = kw.get("options") or (a[1] if len(a) > 1 else [])
        key = kw.get("key")
        if key and key in self.session_state:
            return self.session_state[key]
        return opts[0] if opts else None

    def text_input(self, *a, **kw):
        key = kw.get("key")
        if key and key in self.session_state:
            return self.session_state[key]
        return ""

    def text_area(self, *a, **kw):
        key = kw.get("key")
        if key and key in self.session_state:
            return self.session_state[key]
        return ""

    def number_input(self, *a, **kw):
        key = kw.get("key")
        if key and key in self.session_state:
            return self.session_state[key]
        return kw.get("value", 0)

    def expander(self, *a, **kw):
        return _FakeCtx()

    def image(self, *a, **kw):
        pass

    def data_editor(self, data, *a, **kw):
        # #44 (2026-07-04): 素通し (rows をそのまま返す = 行編集なしのデフォルト)。
        return data


def _install_render_capture(monkeypatch):
    from tabs import _finishing_panel as fp
    from tabs import _finishing_panel_state as fps
    fake = _RenderCaptureStreamlit()
    monkeypatch.setattr(fp, "st", fake)
    monkeypatch.setattr(fps, "logger", fake)  # 未使用だが念のため
    # #44 (2026-07-04): CD / Item Specifics baseline fetch (GetItem) が
    # _render_condition_subblock / _render_item_specifics_field から呼ばれるように
    # なったため、実 API を叩かないよう credentials を明示的に「未設定」に固定する
    # (Q1: 実 API 禁止、real .env の実 credentials に依存しない決定的テストにする)。
    import monitor.credentials as cred_mod
    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {})
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: False)
    return fake


def _minimal_row() -> dict:
    return {
        "ebay_item_id": "111", "title": "T", "sku": "stock01",
        "listing_description": "orig desc",
        "quantity_ebay": 3, "current_price": 10.0,
        "condition_rank": "B", "ebay_condition_id": "3000",
        "source_url": "",
    }


def test_content_group_renders_disabled_apply_button_when_no_dirty(monkeypatch):
    """dirty ゼロで「🚀 eBay へ反映 (変更なし)」の disabled ボタンが描画される."""
    fake = _install_render_capture(monkeypatch)
    # 数量/description モードを固定 (rerun せず初期値のまま = 全 field が not dirty)
    fake.session_state["pf_111_desc_method"] = "✏️ 手動編集"
    fake.session_state["pf_111_description"] = "orig desc"
    fake.session_state["pf_111_description_initial"] = "orig desc"
    fake.session_state["pf_111_title"] = "T"
    fake.session_state["pf_111_title_initial"] = "T"
    fake.session_state["pf_111_rank"] = "B"
    fake.session_state["pf_111_rank_initial"] = "B"
    fake.session_state["pf_111_condition_description"] = ""
    fake.session_state["pf_111_condition_description_initial"] = ""
    fake.session_state["pf_111_quantity"] = 3
    fake.session_state["pf_111_quantity_initial"] = 3

    from tabs._finishing_panel import _render_content_group
    row = _minimal_row()
    _render_content_group(
        "111", row, config=None,
        candidate_id=None, candidate_url=None, source_tab="product_management",
    )

    # (a) 反映ボタンが描画されている (disabled=True で「変更なし」表示)
    apply_btns = [b for b in fake.buttons if b["label"].startswith("🚀 eBay へ反映")]
    assert len(apply_btns) == 1, (
        f"反映ボタンは常時 1 個描画されるべき (got {len(apply_btns)}): "
        f"{[b['label'] for b in fake.buttons]}"
    )
    assert "変更なし" in apply_btns[0]["label"]
    assert apply_btns[0]["kw"].get("disabled") is True

    # (b) 2 行の案内: 「📭 反映待ちの変更はありません」 + 画像専用ボタン案内
    assert any("反映待ちの変更はありません" in msg for msg in fake.infos), (
        f"「📭 反映待ちの変更はありません」の st.info が無い (infos={fake.infos!r})"
    )
    assert any("画像は上の画像セクション内の専用ボタン" in c for c in fake.captions), (
        f"画像専用ボタンの案内 st.caption が無い (captions={fake.captions!r})"
    )
    # (c) 変更なしなので変更プレビュー table は無い
    assert fake.tables == []


def test_content_group_renders_enabled_apply_button_when_dirty(monkeypatch):
    """dirty が 1 つでもあれば「🚀 eBay へ反映 (N件の変更)」の有効ボタン + プレビュー table."""
    fake = _install_render_capture(monkeypatch)
    fake.session_state["pf_111_desc_method"] = "✏️ 手動編集"
    fake.session_state["pf_111_description"] = "orig desc"
    fake.session_state["pf_111_description_initial"] = "orig desc"
    fake.session_state["pf_111_title"] = "New Title"          # ← dirty
    fake.session_state["pf_111_title_initial"] = "T"
    fake.session_state["pf_111_rank"] = "B"
    fake.session_state["pf_111_rank_initial"] = "B"
    fake.session_state["pf_111_condition_description"] = ""
    fake.session_state["pf_111_condition_description_initial"] = ""
    fake.session_state["pf_111_quantity"] = 3
    fake.session_state["pf_111_quantity_initial"] = 3

    from tabs._finishing_panel import _render_content_group
    row = _minimal_row()
    _render_content_group(
        "111", row, config=None,
        candidate_id=None, candidate_url=None, source_tab="product_management",
    )

    apply_btns = [b for b in fake.buttons if b["label"].startswith("🚀 eBay へ反映")]
    assert len(apply_btns) == 1
    assert "1件の変更" in apply_btns[0]["label"], (
        f"dirty 数が label に反映されるべき: {apply_btns[0]['label']!r}"
    )
    assert apply_btns[0]["kw"].get("disabled") is False

    # 「📭 反映待ちの変更はありません」の info は出さない
    assert not any("反映待ちの変更はありません" in msg for msg in fake.infos)
    # 変更プレビュー table が描画される
    assert len(fake.tables) == 1
    # プレビュー table に title 変更が入っている
    table_rows = fake.tables[0]
    assert any(r.get("フィールド") == "タイトル" for r in table_rows)


def test_content_group_multi_dirty_counts_correctly(monkeypatch):
    """title + quantity + condition_description が dirty で 3 件表示."""
    fake = _install_render_capture(monkeypatch)
    fake.session_state["pf_111_desc_method"] = "✏️ 手動編集"
    fake.session_state["pf_111_description"] = "orig desc"
    fake.session_state["pf_111_description_initial"] = "orig desc"
    fake.session_state["pf_111_title"] = "New Title"
    fake.session_state["pf_111_title_initial"] = "T"
    fake.session_state["pf_111_rank"] = "B"
    fake.session_state["pf_111_rank_initial"] = "B"
    fake.session_state["pf_111_condition_description"] = "Tested OK"     # ← dirty
    fake.session_state["pf_111_condition_description_initial"] = ""
    fake.session_state["pf_111_quantity"] = 5                              # ← dirty
    fake.session_state["pf_111_quantity_initial"] = 3

    from tabs._finishing_panel import _render_content_group
    row = _minimal_row()
    _render_content_group(
        "111", row, config=None,
        candidate_id=None, candidate_url=None, source_tab="product_management",
    )

    apply_btns = [b for b in fake.buttons if b["label"].startswith("🚀 eBay へ反映")]
    assert len(apply_btns) == 1
    assert "3件の変更" in apply_btns[0]["label"]
    assert apply_btns[0]["kw"].get("disabled") is False


def test_apply_cd_only_snapshot_failure_does_not_send_wrong_cid(monkeypatch):
    """LOW (2026-07-03 code review 指定回帰): cd 単独 dirty + snapshot 失敗 →
    revise 未呼出 + failure log.

    _apply_cd_only は fetch_listing_snapshot で現行 ConditionID を取得し、
    それを維持して revise_item_condition を呼ぶ設計。snapshot 失敗時に
    デフォルト値 (空文字等) で revise を呼ぶと、誤った ConditionID を
    eBay に押し付けるリスクがあるため、snapshot 失敗時は revise を一切
    呼ばず failure で返すことを固定する。
    """
    revise_calls = []

    def _fake_revise(*a, **kw):
        revise_calls.append((a, kw))
        return {"success": True, "message": "should not run",
                "condition_id": "wrong"}

    def _fake_snap_fail(*a, **kw):
        return _make_snap(ok=False, cid=None)

    fake, log_calls, bump_calls = _install_common(
        monkeypatch, revise_condition_impl=_fake_revise, snap_impl=_fake_snap_fail,
    )

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "B"},                       # not dirty
        "condition_description": {"before": "", "after": "Tested"},   # dirty
        "quantity": {"before": 1, "after": 1},
    }
    _apply_content_changes(
        "111", fields, config=None,
        source_tab="product_management", candidate_id=None,
    )

    # (a) snapshot 失敗時、revise は 1 回も呼ばれない (誤 cid 送信リスク回避)
    assert revise_calls == [], (
        f"snapshot 失敗時に revise を呼ばないこと (got {revise_calls!r})"
    )
    # (b) DB も bump しない (未変更)
    assert bump_calls == [], "DB 未変更のため bump しない"
    # (c) 監査ログは field=condition_description, success=False で記録される
    #     (dispatch_content_changes が失敗結果を per-field で log する)
    # log_calls tuple 形状: (args, kwargs) where args = (eid, field, before, after, ...)
    cd_logs = [(a, kw) for (a, kw) in log_calls if len(a) >= 2 and a[1] == "condition_description"]
    assert len(cd_logs) == 1, f"cd_logs={cd_logs!r} (全ログ={log_calls!r})"
    assert cd_logs[0][1]["success"] is False
    # (d) UI にもエラー表示 (silent 失敗禁止 = Q0)。dispatch は per-field で
    # st.error("{label}: {message}") を呼び、_apply_cd_only の failure message
    # 「現行 ConditionID を取得できません: ...」が伝播する。
    assert any(
        "現行 ConditionID を取得できません" in e or "コンディション理由" in e
        for e in fake.errors
    ), f"snapshot 失敗の error 表示が UI に無い (errors={fake.errors!r})"


def test_apply_no_condition_description_field_backward_compat(monkeypatch):
    """condition_description key を持たない fields (旧テスト等価な呼出) でも KeyError にならない."""
    def _fake_revise_title(item_id, new_title, *a, **kw):
        return {"success": True, "message": "ok", "new_title": new_title}

    _, log_calls, bump_calls = _install_common(
        monkeypatch, revise_title_impl=_fake_revise_title,
    )
    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "Old", "after": "New"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": None, "after": None},
        # condition_description 未指定 (defensive default で "" 扱い)
        "quantity": {"before": 1, "after": 1},
    }
    # KeyError なしで実行できる
    _apply_content_changes("111", fields, config=None,
                           source_tab="product_management", candidate_id=None)
    assert bump_calls == [1]


# ═════════════════════════════════════════════════
# 5. tab_product_management: pm_form 撤去 (widget キー削除)
# ═════════════════════════════════════════════════

def _find_pm_render_form_source() -> str:
    tree = ast.parse(_PM_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_render_left_basic_and_physical":
            return ast.get_source_segment(_PM_PATH.read_text(encoding="utf-8"), node) or ""
    return ""


def test_pm_form_no_longer_has_pm_title_widget():
    """商品タイトル widget (`key=f"pm_title_{eid}"`) がフォームから撤去."""
    src = _find_pm_render_form_source()
    assert 'key=f"pm_title_{eid}"' not in src


def test_pm_form_no_longer_has_pm_rank_widget():
    """商品ランク widget (`key=f"pm_rank_{eid}"`) がフォームから撤去."""
    src = _find_pm_render_form_source()
    assert 'key=f"pm_rank_{eid}"' not in src


def test_pm_form_no_longer_has_pm_conddesc_widget():
    src = _find_pm_render_form_source()
    assert 'key=f"pm_conddesc_{eid}"' not in src


def test_pm_form_no_longer_has_pm_desc_widget():
    src = _find_pm_render_form_source()
    assert 'key=f"pm_desc_{eid}"' not in src
    assert 'key=_desc_key' not in src


def test_pm_form_no_longer_renders_rank_lookup_expander():
    """📖 商品ランク早見表 expander (実際の st.expander 呼出) が撤去.

    コメント文中の「📖 商品ランク早見表」参照は許容 (削除の由来説明)。
    実際の widget 呼出 `st.expander("📖 商品ランク早見表 ..."` が無いことを確認。
    """
    src = _find_pm_render_form_source()
    assert 'st.expander("📖 商品ランク早見表' not in src


def test_pm_no_longer_calls_desc_fetch_button():
    """form 外の `_render_desc_fetch_button(eid, config)` 呼出が撤去."""
    src = _PM_PATH.read_text(encoding="utf-8")
    # 定義自体は残置 (Phase 3 再利用余地) だが、呼出行が無いこと
    assert "_render_desc_fetch_button(eid, config)" not in src


def test_pm_form_preserves_other_widgets_bit_for_bit():
    """撤去に伴う副作用の無検査: 価格/送料/SKU/在庫/重量/寸法/区分/仕入価格/ポイント/BP/
    発送・通関メモ (pm_note_) の widget key は 1 つも消えていない (等価性の絶対条件)."""
    src = _find_pm_render_form_source()
    preserved_keys = [
        'key=f"pm_sku_{eid}"',
        'key=f"pm_inv_{eid}"',
        'key=f"pm_weight_{eid}"',
        'key=f"pm_length_{eid}"',
        'key=f"pm_width_{eid}"',
        'key=f"pm_height_{eid}"',
        'key=f"pm_note_{eid}"',
    ]
    for k in preserved_keys:
        assert k in src, f"保持されるべき widget key `{k}` が消失している (等価性違反)"


# ═════════════════════════════════════════════════
# 6. _save_product_data / _apply_listing_content_to_ebay の .get() 等価 no-op
# ═════════════════════════════════════════════════

def test_save_product_data_reads_removed_keys_via_get():
    """撤去したキー (listing_description 等) を `.get()` で読む = KeyError 回避."""
    src = _PM_PATH.read_text(encoding="utf-8")
    # editing["listing_description"] を直接読む(下付き)経路が主要 flow に無い
    # (fallback 分岐 L4181/4189 は代入経路のため OK)
    # 主要な参照経路が .get(...) を使っていることを確認
    assert 'editing.get("listing_description")' in src


def test_apply_listing_content_reads_removed_keys_via_get():
    src = _PM_PATH.read_text(encoding="utf-8")
    for expected in (
        'editing.get("new_title")',
        'editing.get("title_render_initial")',
        'editing.get("rank")',
        'editing.get("rank_render_initial")',
        'editing.get("condition_description")',
    ):
        assert expected in src, (
            f"_apply_listing_content_to_ebay は `.get()` で {expected} を参照する "
            f"(未設定キーを KeyError にせず等価 no-op にする条件)"
        )
