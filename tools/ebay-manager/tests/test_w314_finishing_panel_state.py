"""W314 Phase 2 S5 (2026-07-03): 統一「商品仕上げパネル」state 層 unit test.

対象: tabs/_finishing_panel_state.py (dirty 判定 / 変更プレビュー組立 /
ヘッダ指標算出 / 反映ディスパッチ / description eBay 取得)。

streamlit runtime を必要とする render 本体 (_finishing_panel.py) はここでは
テストせず、test_w314_finishing_panel_ui.py で AST / import 検証する
(既存 followup テスト群と同方針)。eBay API は mock のみ (Q1: 実 API 呼出なし)。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ─────────────────────────────────────────────────
# 1. pf_key / seed_session_value / seed_initial / mark_field_synced
# ─────────────────────────────────────────────────

def test_pf_key_format():
    from tabs._finishing_panel_state import pf_key
    assert pf_key("123456789012", "title") == "pf_123456789012_title"


def test_seed_session_value_idempotent():
    from tabs._finishing_panel_state import seed_session_value
    ss: dict = {}
    v1 = seed_session_value(ss, "k", "first")
    v2 = seed_session_value(ss, "k", "second")
    assert v1 == "first"
    assert v2 == "first"
    assert ss["k"] == "first"


def test_seed_initial_uses_pf_key_and_field_suffix():
    from tabs._finishing_panel_state import seed_initial
    ss: dict = {}
    seed_initial(ss, "999", "title", "Original Title")
    assert ss["pf_999_title_initial"] == "Original Title"


def test_mark_field_synced_resets_baseline():
    from tabs._finishing_panel_state import mark_field_synced, seed_initial
    ss: dict = {}
    seed_initial(ss, "1", "title", "Old")
    mark_field_synced(ss, "1", "title", "New")
    assert ss["pf_1_title_initial"] == "New"


# ─────────────────────────────────────────────────
# 2. resolve_rank_initial / rank_to_condition_id
# ─────────────────────────────────────────────────

def test_resolve_rank_initial_prefers_condition_rank():
    from tabs._finishing_panel_state import resolve_rank_initial
    row = {"condition_rank": "B", "ebay_condition_id": "1000"}
    assert resolve_rank_initial(row) == "B"


def test_resolve_rank_initial_falls_back_to_condition_id():
    from tabs._finishing_panel_state import resolve_rank_initial
    assert resolve_rank_initial({"ebay_condition_id": "1000"}) == "N"
    assert resolve_rank_initial({"ebay_condition_id": "1500"}) == "S"
    assert resolve_rank_initial({"ebay_condition_id": "7000"}) == "As-Is"


def test_resolve_rank_initial_used_condition_unresolvable():
    """3000 (Used) はサブランク逆引き不能 → 未設定 ("")."""
    from tabs._finishing_panel_state import resolve_rank_initial
    assert resolve_rank_initial({"ebay_condition_id": "3000"}) == ""
    assert resolve_rank_initial({}) == ""


def test_rank_to_condition_id_mapping():
    from tabs._finishing_panel_state import rank_to_condition_id
    assert rank_to_condition_id("N") == "1000"
    assert rank_to_condition_id("S") == "1500"
    assert rank_to_condition_id("As-Is") == "7000"
    assert rank_to_condition_id("A") == "3000"
    assert rank_to_condition_id(None) is None
    assert rank_to_condition_id("") is None
    assert rank_to_condition_id("unknown") is None


# ─────────────────────────────────────────────────
# 3. is_field_dirty
# ─────────────────────────────────────────────────

def test_is_field_dirty_title_same_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("title", "Same", "Same") is False
    assert is_field_dirty("title", "Same", "  Same  ") is False


def test_is_field_dirty_title_changed_is_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("title", "Old", "New") is True


def test_is_field_dirty_title_empty_after_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("title", "Old", "") is False
    assert is_field_dirty("title", "Old", "   ") is False


def test_is_field_dirty_description_same_logic_as_title():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("description", "abc", "abc") is False
    assert is_field_dirty("description", "abc", "abcd") is True
    assert is_field_dirty("description", "abc", "") is False


def test_is_field_dirty_rank_none_after_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("rank", "A", None) is False
    assert is_field_dirty("rank", "A", "B") is True
    assert is_field_dirty("rank", None, "N") is True
    assert is_field_dirty("rank", "N", "N") is False


def test_is_field_dirty_quantity_int_comparison():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("quantity", 3, 3) is False
    assert is_field_dirty("quantity", 3, 5) is True
    assert is_field_dirty("quantity", 3, 0) is True  # 0 への変更は正当 (在庫0化)
    assert is_field_dirty("quantity", 0, 0) is False


def test_is_field_dirty_quantity_none_after_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("quantity", 3, None) is False


def test_is_field_dirty_quantity_before_none_defaults_zero():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("quantity", None, 1) is True
    assert is_field_dirty("quantity", None, 0) is False


def test_is_field_dirty_images_generic_compare():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("images", "① AI 合成 (0枚)", "② そのまま採用 (3枚)") is True
    assert is_field_dirty("images", "① AI 合成 (0枚)", "① AI 合成 (0枚)") is False
    assert is_field_dirty("images", "① AI 合成 (0枚)", "") is False


# ─────────────────────────────────────────────────
# 4. summarize_description / summarize_images
# ─────────────────────────────────────────────────

def test_summarize_description_short_text_no_ellipsis():
    from tabs._finishing_panel_state import summarize_description
    result = summarize_description("short text")
    assert result == "short text (10文字)"
    assert "…" not in result


def test_summarize_description_long_text_truncates_with_count():
    from tabs._finishing_panel_state import summarize_description
    text = "A" * 200
    result = summarize_description(text, head_chars=120)
    assert result.startswith("A" * 120 + "…")
    assert "(200文字)" in result


def test_summarize_description_empty():
    from tabs._finishing_panel_state import summarize_description
    assert summarize_description(None) == " (0文字)"
    assert summarize_description("") == " (0文字)"


def test_summarize_images_format():
    from tabs._finishing_panel_state import summarize_images
    assert summarize_images("① AI 合成 (従来)", 3) == "① AI 合成 (従来) (3枚)"


# ─────────────────────────────────────────────────
# 5. build_change_preview
# ─────────────────────────────────────────────────

def test_build_change_preview_only_dirty_fields():
    from tabs._finishing_panel_state import build_change_preview
    fields = {
        "title": {"before": "Old Title", "after": "New Title"},
        "description": {"before": "abc", "after": "abc"},  # not dirty
        "rank": {"before": "A", "after": "B"},
        "quantity": {"before": 1, "after": 1},  # not dirty
    }
    preview = build_change_preview(fields)
    result_fields = [p["field"] for p in preview]
    assert result_fields == ["title", "rank"]


def test_build_change_preview_order_matches_preview_field_order():
    from tabs._finishing_panel_state import build_change_preview
    fields = {
        "quantity": {"before": 1, "after": 5},
        "title": {"before": "Old", "after": "New"},
        "rank": {"before": "A", "after": "B"},
        "description": {"before": "x", "after": "y"},
        "images": {"before": "mode1 (0枚)", "after": "mode2 (3枚)"},
    }
    preview = build_change_preview(fields)
    assert [p["field"] for p in preview] == [
        "title", "description", "images", "rank", "quantity",
    ]


def test_build_change_preview_description_uses_summary():
    from tabs._finishing_panel_state import build_change_preview
    long_text = "B" * 150
    fields = {"description": {"before": "", "after": long_text}}
    preview = build_change_preview(fields)
    assert len(preview) == 1
    assert preview[0]["after"].startswith("B" * 120 + "…")
    assert "(150文字)" in preview[0]["after"]


def test_build_change_preview_empty_when_nothing_dirty():
    from tabs._finishing_panel_state import build_change_preview
    fields = {"title": {"before": "Same", "after": "Same"}}
    assert build_change_preview(fields) == []


def test_build_change_preview_missing_field_key_ignored():
    from tabs._finishing_panel_state import build_change_preview
    # title のみ渡す (description/rank/quantity 未指定でも落ちない)
    fields = {"title": {"before": "Old", "after": "New"}}
    preview = build_change_preview(fields)
    assert [p["field"] for p in preview] == ["title"]


def test_build_change_preview_respects_custom_display_values():
    from tabs._finishing_panel_state import build_change_preview
    fields = {
        "images": {
            "before": "mode1", "after": "mode2",
            "before_display": "① AI 合成 (0枚)", "after_display": "② そのまま採用 (3枚)",
        },
    }
    preview = build_change_preview(fields)
    assert preview[0]["before"] == "① AI 合成 (0枚)"
    assert preview[0]["after"] == "② そのまま採用 (3枚)"


# ─────────────────────────────────────────────────
# 6. compute_header_metrics
# ─────────────────────────────────────────────────

def test_compute_header_metrics_minimal_row_no_profit():
    from tabs._finishing_panel_state import compute_header_metrics
    row = {
        "current_price": 89.99, "quantity_ebay": 3, "is_ended": 0,
        # purchase_yen / weight_g 欠落 → profit は算出不能
    }
    m = compute_header_metrics(row)
    assert m["price_usd"] == 89.99
    assert m["quantity"] == 3
    assert m["status"] == "Active"
    assert m["profit_jpy"] is None
    assert m["profit_rate_pct"] is None


def test_compute_header_metrics_is_ended_status():
    from tabs._finishing_panel_state import compute_header_metrics
    row = {"current_price": 10.0, "quantity_ebay": 0, "is_ended": 1}
    m = compute_header_metrics(row)
    assert m["status"] == "Ended"


def test_compute_header_metrics_profit_computed_when_calculator_succeeds(monkeypatch):
    """calculator.calculate を mock し、best profit_with_refund を採用する."""
    import calculator as calc_mod
    from tabs._finishing_panel_state import compute_header_metrics

    fake_service_low = SimpleNamespace(profit=100, profit_with_refund=1000, profit_with_refund_rate=0.10)
    fake_service_high = SimpleNamespace(profit=200, profit_with_refund=3240, profit_with_refund_rate=0.142)
    fake_result = SimpleNamespace(service_results=[fake_service_low, fake_service_high])

    monkeypatch.setattr(calc_mod, "calculate", lambda inp, settings: fake_result)

    row = {
        "current_price": 89.99, "quantity_ebay": 3, "is_ended": 0,
        "purchase_yen": 5000, "weight_g": 300,
        "length_cm": 10, "width_cm": 10, "height_cm": 5,
        "category_id": 58248, "point_yen": None,
    }
    m = compute_header_metrics(row, settings={})
    assert m["profit_jpy"] == 3240
    assert m["profit_rate_pct"] == 14.2


def test_compute_header_metrics_profit_calc_exception_falls_back_to_none(monkeypatch):
    """利益試算で例外が出てもヘッダ全体は落ちず profit=None を返す (Q0)."""
    import calculator as calc_mod
    from tabs._finishing_panel_state import compute_header_metrics

    def _boom(inp, settings):
        raise RuntimeError("csv missing")

    monkeypatch.setattr(calc_mod, "calculate", _boom)

    row = {
        "current_price": 50.0, "quantity_ebay": 1, "is_ended": 0,
        "purchase_yen": 3000, "weight_g": 200,
    }
    m = compute_header_metrics(row, settings={})
    assert m["profit_jpy"] is None
    assert m["price_usd"] == 50.0
    assert m["quantity"] == 1


def test_compute_header_metrics_zero_price_no_profit_attempt():
    from tabs._finishing_panel_state import compute_header_metrics
    row = {"current_price": 0, "quantity_ebay": 1, "is_ended": 0,
           "purchase_yen": 5000, "weight_g": 300}
    m = compute_header_metrics(row)
    assert m["price_usd"] == 0
    assert m["profit_jpy"] is None


# ─────────────────────────────────────────────────
# 7. fetch_description_from_ebay
# ─────────────────────────────────────────────────

def test_fetch_description_from_ebay_missing_credentials(monkeypatch):
    from tabs._finishing_panel_state import fetch_description_from_ebay
    import monitor.credentials as cred_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {})
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: False)

    result = fetch_description_from_ebay("123456789012")
    assert result["success"] is False
    assert "credentials" in result["message"]


def test_fetch_description_from_ebay_success(monkeypatch):
    from tabs._finishing_panel_state import fetch_description_from_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(
        ec_mod, "get_single_listing",
        lambda *a, **kw: {"description": "<p>Hello</p>"},
    )
    result = fetch_description_from_ebay("123456789012")
    assert result["success"] is True
    assert result["description"] == "<p>Hello</p>"


def test_fetch_description_from_ebay_none_response(monkeypatch):
    from tabs._finishing_panel_state import fetch_description_from_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(ec_mod, "get_single_listing", lambda *a, **kw: None)

    result = fetch_description_from_ebay("123456789012")
    assert result["success"] is False


# ─────────────────────────────────────────────────
# 8. dispatch_content_changes
# ─────────────────────────────────────────────────

def test_dispatch_content_changes_all_success_logs_and_returns_results():
    from tabs._finishing_panel_state import dispatch_content_changes

    log_calls = []

    def fake_log(eid, field, before, after, **kwargs):
        log_calls.append((eid, field, before, after, kwargs))
        return 1

    changes = [
        {"field": "title", "before": "Old", "after": "New",
         "apply": lambda: {"success": True, "message": "title ok"}},
        {"field": "quantity", "before": 1, "after": 5,
         "apply": lambda: {"success": True, "message": "qty ok"}},
    ]
    results = dispatch_content_changes(
        "123456789012", changes, source_tab="product_management",
        candidate_id=None, log_fn=fake_log,
    )
    assert results["title"] == {"success": True, "message": "title ok"}
    assert results["quantity"] == {"success": True, "message": "qty ok"}
    assert len(log_calls) == 2
    assert log_calls[0][:4] == ("123456789012", "title", "Old", "New")
    assert log_calls[0][4]["success"] is True
    assert log_calls[0][4]["source_tab"] == "product_management"


def test_dispatch_content_changes_partial_failure_continues_others():
    """1 フィールド失敗しても残りを続行し、実値を返す (Q0)."""
    from tabs._finishing_panel_state import dispatch_content_changes

    log_calls = []

    def fake_log(eid, field, before, after, **kwargs):
        log_calls.append((field, kwargs.get("success")))
        return 1

    def _fail():
        return {"success": False, "message": "API エラー: boom"}

    def _ok():
        return {"success": True, "message": "ok"}

    changes = [
        {"field": "title", "before": "Old", "after": "New", "apply": _fail},
        {"field": "quantity", "before": 1, "after": 5, "apply": _ok},
    ]
    results = dispatch_content_changes("123456789012", changes, log_fn=fake_log)
    assert results["title"]["success"] is False
    assert results["quantity"]["success"] is True
    assert ("title", False) in log_calls
    assert ("quantity", True) in log_calls


def test_dispatch_content_changes_apply_exception_caught_and_logged():
    """apply() が例外を送出しても dispatch は落ちず failure として記録する."""
    from tabs._finishing_panel_state import dispatch_content_changes

    log_calls = []

    def fake_log(eid, field, before, after, **kwargs):
        log_calls.append((field, kwargs.get("success"), kwargs.get("ebay_ack")))
        return 1

    def _boom():
        raise RuntimeError("network down")

    changes = [{"field": "description", "before": "a", "after": "b", "apply": _boom}]
    results = dispatch_content_changes("123456789012", changes, log_fn=fake_log)
    assert results["description"]["success"] is False
    assert "network down" in results["description"]["message"]
    assert log_calls == [("description", False, "RuntimeError: network down")]


def test_dispatch_content_changes_log_failure_does_not_swallow_apply_result():
    """監査ログ自体が失敗しても revise 結果 (results dict) は正しく返す."""
    from tabs._finishing_panel_state import dispatch_content_changes

    def _boom_log(*a, **kw):
        raise RuntimeError("db locked")

    changes = [
        {"field": "rank", "before": "A", "after": "B",
         "apply": lambda: {"success": True, "message": "rank ok"}},
    ]
    results = dispatch_content_changes("123456789012", changes, log_fn=_boom_log)
    assert results["rank"] == {"success": True, "message": "rank ok"}


def test_dispatch_content_changes_default_log_fn_imports_real_module(monkeypatch):
    """log_fn 省略時は monitor.listing_content_change_log.log_content_change を使う."""
    from tabs._finishing_panel_state import dispatch_content_changes
    import monitor.listing_content_change_log as lccl_mod

    calls = []
    monkeypatch.setattr(
        lccl_mod, "log_content_change",
        lambda *a, **kw: calls.append((a, kw)) or 1,
    )
    changes = [
        {"field": "title", "before": "Old", "after": "New",
         "apply": lambda: {"success": True, "message": "ok"}},
    ]
    dispatch_content_changes("123456789012", changes)
    assert len(calls) == 1
    assert calls[0][0][:4] == ("123456789012", "title", "Old", "New")


def test_dispatch_content_changes_empty_list_returns_empty_dict():
    from tabs._finishing_panel_state import dispatch_content_changes
    assert dispatch_content_changes("123456789012", []) == {}


# ─────────────────────────────────────────────────
# 9. constants sanity (RANK_CHOICES / FIELD_LABELS_JA / order)
# ─────────────────────────────────────────────────

def test_rank_choices_covers_all_8_levels():
    from tabs._finishing_panel_state import RANK_CHOICES
    assert set(RANK_CHOICES) == {"N", "S", "A", "B", "C", "D", "PO", "As-Is"}


def test_dispatch_field_order_excludes_images():
    from tabs._finishing_panel_state import DISPATCH_FIELD_ORDER
    assert "images" not in DISPATCH_FIELD_ORDER
    assert set(DISPATCH_FIELD_ORDER) == {"title", "description", "rank", "quantity"}


def test_preview_field_order_includes_images():
    from tabs._finishing_panel_state import PREVIEW_FIELD_ORDER
    assert "images" in PREVIEW_FIELD_ORDER
