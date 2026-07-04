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


# ─────────────────────────────────────────────────
# 10. resolve_condition_description_for_rank (#44 バグ2修正 2026-07-04)
#
# user 報告: 「コンディション欄 (ConditionDescription/Seller Notes) に商品説明が
# 残る・入る」。AI 自由文をそのまま採用するのをやめ、As-Is 以外はランクから
# 決定論的にテンプレ文を導出する (65字保証 + 商品固有の長文混入を排除)。
# ─────────────────────────────────────────────────

def test_resolve_condition_description_uses_template_ignoring_ai_freeform():
    from tabs._finishing_panel_state import resolve_condition_description_for_rank
    long_ai_text = (
        "This vintage amplifier comes with the original box, manual, and a rare "
        "bundled cable set that collectors specifically look for."
    )
    result = resolve_condition_description_for_rank("A", long_ai_text)
    assert result == "Rank A — Excellent. Tested, fully working. Minor wear."
    assert long_ai_text not in result


def test_resolve_condition_description_all_non_as_is_within_65_chars():
    from tabs._finishing_panel_state import (
        RANK_CHOICES, RANK_CONDITION_DESCRIPTION_TEMPLATE, resolve_condition_description_for_rank,
    )
    # HIGH-1 (2026-07-04): N (1000) は eBay 仕様上 CD 非対応のためテンプレから除外済。
    # As-Is (7000) は商品固有の理由が必須で定型化不能。この 2 rank はスキップ。
    for rank in RANK_CHOICES:
        if rank in ("N", "As-Is"):
            continue
        assert rank in RANK_CONDITION_DESCRIPTION_TEMPLATE, f"rank={rank} のテンプレが未定義"
        result = resolve_condition_description_for_rank(rank, "any AI freeform text")
        assert len(result) <= 65, f"rank={rank} のテンプレが65字超過: {result!r}"


def test_resolve_condition_description_n_rank_returns_empty_string():
    """HIGH-1 (2026-07-04): N (ConditionID 1000) は eBay 仕様上 ConditionDescription
    非対応のため、テンプレを持たない (空文字を返す)。AI 生成値も採用せず空文字。
    apply 層 (`_apply_content_changes`) 側でも二段防御として cond_id==1000 で
    CD を None 化するが、state 層の入口で空にしておく。"""
    from tabs._finishing_panel_state import (
        RANK_CONDITION_DESCRIPTION_TEMPLATE, resolve_condition_description_for_rank,
    )
    assert "N" not in RANK_CONDITION_DESCRIPTION_TEMPLATE
    assert resolve_condition_description_for_rank("N") == ""
    # AI が長文 CD を返しても採用しない (テンプレ未定義 + fallback は空 rank_code のみ)
    assert resolve_condition_description_for_rank(
        "N", "This vintage amplifier is fully working with box",
    ) == ""


def test_resolve_condition_description_as_is_keeps_ai_generated_reason():
    """As-Is は商品固有の理由が必須で定型化不能なため、AI 生成値をそのまま使う."""
    from tabs._finishing_panel_state import resolve_condition_description_for_rank
    result = resolve_condition_description_for_rank(
        "As-Is", "As-Is — No AC adapter for testing",
    )
    assert result == "As-Is — No AC adapter for testing"


def test_resolve_condition_description_none_rank_falls_back_to_ai_text():
    from tabs._finishing_panel_state import resolve_condition_description_for_rank
    assert resolve_condition_description_for_rank(None, "fallback") == "fallback"
    assert resolve_condition_description_for_rank("", "fallback") == "fallback"
    assert resolve_condition_description_for_rank(None, None) == ""


# ─────────────────────────────────────────────────
# 11. resolve_effective_condition_id_for_cd_dispatch
#     (T1 修正 2026-07-04: N 選択時に CD dispatch を件数から除外する判定)
# ─────────────────────────────────────────────────

def test_resolve_effective_cond_id_prefers_rank_after():
    from tabs._finishing_panel_state import resolve_effective_condition_id_for_cd_dispatch
    # after が最優先 (user が編集中のランク)
    assert resolve_effective_condition_id_for_cd_dispatch(
        {"before": "A", "after": "N"}, "3000",
    ) == "1000"


def test_resolve_effective_cond_id_falls_back_to_rank_before():
    from tabs._finishing_panel_state import resolve_effective_condition_id_for_cd_dispatch
    # after が None → before を見る
    assert resolve_effective_condition_id_for_cd_dispatch(
        {"before": "N", "after": None}, "3000",
    ) == "1000"


def test_resolve_effective_cond_id_falls_back_to_ebay_condition_id():
    from tabs._finishing_panel_state import resolve_effective_condition_id_for_cd_dispatch
    # rank 全て空 → row の ebay_condition_id を使う
    assert resolve_effective_condition_id_for_cd_dispatch(
        {"before": None, "after": None}, "1000",
    ) == "1000"
    # 全て空
    assert resolve_effective_condition_id_for_cd_dispatch(None, None) is None
    assert resolve_effective_condition_id_for_cd_dispatch({}, "") is None


def test_resolve_effective_cond_id_used_rank_returns_3000():
    """A-D/PO はすべて 3000 (Used) にマップされる (共通コンディション ID)."""
    from tabs._finishing_panel_state import resolve_effective_condition_id_for_cd_dispatch
    for rank in ("A", "B", "C", "D", "PO"):
        assert resolve_effective_condition_id_for_cd_dispatch(
            {"before": None, "after": rank}, None,
        ) == "3000"


# ─────────────────────────────────────────────────
# 12. compute_dirty_dispatch_fields
#     (T1 修正 2026-07-04: 表示件数と実送信件数を一致させる)
# ─────────────────────────────────────────────────

def _make_fields_all_clean() -> dict[str, dict]:
    return {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "A", "after": "A"},
        "condition_description": {"before": "", "after": ""},
        "quantity": {"before": 1, "after": 1},
    }


def test_compute_dirty_dispatch_fields_all_clean_returns_empty():
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    assert compute_dirty_dispatch_fields(_make_fields_all_clean(), "3000") == []


def test_compute_dirty_dispatch_fields_rank_change_to_n_excludes_cd(monkeypatch):
    """T1 core: rank を B → N に変更 + CD が dirty (定型文 → 空) でも、CD は
    件数に入らない (N=1000 は eBay 仕様上 CD 非対応)。件数=1 (rank のみ)."""
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    fields = _make_fields_all_clean()
    fields["rank"] = {"before": "B", "after": "N"}                # dirty (件数 +1)
    fields["condition_description"] = {                            # dirty だが除外
        "before": "Rank B — Good. Tested, fully working. Visible wear.",
        "after": "",
    }
    # effective_condition_id は "N" → "1000" (UI 側で resolve 済み前提)
    result = compute_dirty_dispatch_fields(fields, "1000")
    assert result == ["rank"], (
        f"N (1000) 選択時は CD dirty を件数から除外し rank のみ (got {result!r})"
    )


def test_compute_dirty_dispatch_fields_rank_to_used_includes_cd():
    """T1 対照: rank を N → A に変更 + CD dirty なら CD は件数に入る (A=3000 は
    CD 送信対応、apply 層で bundle 送信される)。件数=2 (rank + cd)."""
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    fields = _make_fields_all_clean()
    fields["rank"] = {"before": "N", "after": "A"}
    fields["condition_description"] = {
        "before": "", "after": "Rank A — Excellent. Tested, fully working. Minor wear.",
    }
    result = compute_dirty_dispatch_fields(fields, "3000")
    assert set(result) == {"rank", "condition_description"}, (
        f"A (3000) 選択時は CD dirty を件数に含める (got {result!r})"
    )


def test_compute_dirty_dispatch_fields_existing_n_listing_cd_only_dirty_excluded():
    """T1: 現行 listing の rank が N (未変更) + CD 単独 dirty のケースでも
    CD は件数に入らない (fallback effective_condition_id で "1000" 判定)."""
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    fields = _make_fields_all_clean()
    fields["rank"] = {"before": "N", "after": "N"}                # not dirty
    fields["condition_description"] = {"before": "old", "after": "new"}  # dirty
    result = compute_dirty_dispatch_fields(fields, "1000")
    assert result == []


def test_compute_dirty_dispatch_fields_item_specifics_dispatch_disabled_excluded():
    """回帰: item_specifics dirty でも dispatch_disabled なら件数から除外
    (H2 baseline 失敗 / MED multi-value 検出のいずれも)."""
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    fields = _make_fields_all_clean()
    fields["item_specifics"] = {
        "before": {"Brand": "Sony"}, "after": {"Brand": "Sony", "Model": "X"},
        "dispatch_disabled": True,
    }
    assert compute_dirty_dispatch_fields(fields, "3000") == []


def test_compute_dirty_dispatch_fields_item_specifics_dispatch_ok_included():
    from tabs._finishing_panel_state import compute_dirty_dispatch_fields
    fields = _make_fields_all_clean()
    fields["item_specifics"] = {
        "before": {"Brand": "Sony"}, "after": {"Brand": "Sony", "Model": "X"},
        "dispatch_disabled": False,
    }
    assert compute_dirty_dispatch_fields(fields, "3000") == ["item_specifics"]


# ─────────────────────────────────────────────────
# 13. RANK_CONDITION_DESCRIPTION_TEMPLATE 新書式
#     (2026-07-04 user 追加報告 358754421540: ランクを見出しに含める書式)
# ─────────────────────────────────────────────────

def test_new_template_format_all_start_with_rank_prefix_within_65_chars():
    """N/As-Is 以外の 6 ランク全てが "Rank X — " で始まり 65 字以内であること."""
    from tabs._finishing_panel_state import (
        RANK_CONDITION_DESCRIPTION_TEMPLATE, resolve_condition_description_for_rank,
    )
    expected_starts = {
        "S": "Rank S — New (Opened).",
        "A": "Rank A — Excellent.",
        "B": "Rank B — Good.",
        "C": "Rank C — Fair.",
        "D": "Rank D — Issues.",
        "PO": "Rank PO — Power-On Only.",
    }
    for rank, expected_start in expected_starts.items():
        cd = RANK_CONDITION_DESCRIPTION_TEMPLATE[rank]
        assert cd.startswith(expected_start), (
            f"rank={rank}: expected startswith {expected_start!r}, got {cd!r}"
        )
        assert len(cd) <= 65, f"rank={rank}: over 65 chars ({len(cd)}): {cd!r}"
        # resolve_condition_description_for_rank 経由でも同値
        assert resolve_condition_description_for_rank(rank) == cd


# ─────────────────────────────────────────────────
# 14. retarget_rank_headers_in_description
#     (バグ2修正 2026-07-04 358754421540: description 本文の Rank 見出し追従)
# ─────────────────────────────────────────────────

def test_retarget_rank_headers_matches_and_replaces_h3_heading():
    """リテラル em-dash 形式の `Rank B — Good` を検出し置換 (置換文字列は
    HIGH-1 修正 2026-07-04 で `&mdash;` エンティティ形式に統一)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank B — Good</h3><p>Tested and working.</p>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True
    assert "<h3>Rank A &mdash; Excellent</h3>" in new_html
    assert "Rank B — Good" not in new_html


def test_retarget_rank_headers_matches_plain_text():
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "This item is Rank B — Good. Full details below."
    new_html, changed = retarget_rank_headers_in_description(html, "C", "Fair")
    assert changed is True
    assert "Rank C &mdash; Fair" in new_html
    assert "Rank B — Good" not in new_html


def test_retarget_rank_headers_no_pattern_returns_unchanged():
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h1>Some title</h1><p>No rank heading present.</p>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is False
    assert new_html == html


def test_retarget_rank_headers_empty_html():
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    new_html, changed = retarget_rank_headers_in_description("", "A", "Excellent")
    assert changed is False
    assert new_html == ""


def test_retarget_rank_headers_uses_default_english_label_when_omitted():
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank PO — Power-On Only</h3>"
    new_html, changed = retarget_rank_headers_in_description(html, "A")
    assert changed is True
    assert "Rank A &mdash; Excellent" in new_html


def test_retarget_rank_headers_multiple_occurrences_all_replaced():
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank B — Good</h3> ... some later section: Rank B — Good again."
    new_html, changed = retarget_rank_headers_in_description(html, "D", "Issues")
    assert changed is True
    assert new_html.count("Rank D &mdash; Issues") == 2
    assert "Rank B — Good" not in new_html


def test_retarget_rank_headers_as_is_uses_full_form_for_round_trip():
    """【round-trip fix 2026-07-04 実機再確認】As-Is も `Rank As-Is &mdash; As-Is` の
    完全形で emit する (旧「Rank As-Is」bare は次のランク変更で再マッチ不可能 = 片道
    切符バグの原因)。v4 テンプレ実物 (358042514439.html) と同形状に統一."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank B — Good</h3>"
    new_html, changed = retarget_rank_headers_in_description(html, "As-Is")
    assert changed is True
    assert "Rank As-Is &mdash; As-Is" in new_html


def test_retarget_rank_headers_recovers_legacy_bare_as_is():
    """【round-trip fix 2026-07-04】旧 bug で emit された bare `Rank As-Is`
    (em-dash + Label 無し) を新書式 `Rank A &mdash; Excellent` に回収する
    (legacy 遷移救済、`_RANK_HEADER_BARE_AS_IS_PATTERN` で拾う)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank As-Is</h3><p>Some content.</p>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True
    assert "Rank A &mdash; Excellent" in new_html
    assert ">Rank As-Is<" not in new_html


def test_retarget_rank_headers_round_trip_as_is_then_a():
    """As-Is → A → As-Is → A の連続遷移で最終値に収束すること (片道切符バグの回帰保証)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank B &mdash; Good</h3>"
    html, _ = retarget_rank_headers_in_description(html, "As-Is")
    assert "Rank As-Is &mdash; As-Is" in html
    html, _ = retarget_rank_headers_in_description(html, "A")
    assert "Rank A &mdash; Excellent" in html
    html, _ = retarget_rank_headers_in_description(html, "As-Is")
    assert "Rank As-Is &mdash; As-Is" in html
    html, _ = retarget_rank_headers_in_description(html, "A")
    assert "Rank A &mdash; Excellent" in html
    assert "Rank As-Is" not in html
    assert "Rank B" not in html


# ─────────────────────────────────────────────────
# 14b. HIGH-1 修正 2026-07-04 verify wave:
#     v4 テンプレ実物 `&mdash;` エンティティ形式の実データ回帰テスト
# ─────────────────────────────────────────────────

def test_retarget_rank_headers_matches_html_entity_mdash():
    """v4 テンプレ (`listing-description-template.md` L239) 実物形状の
    `<h3>Rank B &mdash; Good</h3>` を正しく検出し追従すること (無音 no-op バグ回帰)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank B &mdash; Good</h3><p>Tested and working.</p>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True, (
        f"`&mdash;` entity 形式の見出しは matched であるべき (got {new_html!r})"
    )
    assert "<h3>Rank A &mdash; Excellent</h3>" in new_html
    assert "Rank B &mdash; Good" not in new_html


def test_retarget_rank_headers_matches_html_entity_numeric():
    """数値参照エンティティ `&#8212;` 形式も検出する (テンプレによって出力される可能性)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank C &#8212; Fair</h3>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True
    assert "Rank A &mdash; Excellent" in new_html


def test_retarget_rank_headers_definition_table_row_unchanged():
    """定義表行 `<tr><td>A</td><td>Excellent &mdash; Minor wear</td></tr>` は
    'Rank ' prefix 無しのため誤マッチせず不変 (Rank definitions テーブルの説明を
    ランク変更で壊さないことの回帰保証)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = (
        "<table>"
        "<tr><td>N</td><td>New &mdash; Sealed</td></tr>"
        "<tr><td>A</td><td>Excellent &mdash; Minor wear</td></tr>"
        "<tr><td>B</td><td>Good &mdash; Visible wear</td></tr>"
        "</table>"
    )
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is False, (
        f"definition table 行 (Rank prefix 無し) は不変であるべき (got diff)"
    )
    assert new_html == html


def test_retarget_rank_headers_entity_and_literal_mixed_all_replaced():
    """同一 description 内でリテラル em-dash と `&mdash;` エンティティが混在するケース
    (旧 description と新 description が混在する遷移期) を両方 replace する."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = (
        "<h3>Rank B &mdash; Good</h3>"
        "<p>Additional note: Rank B — Good confirmed.</p>"
    )
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True
    assert new_html.count("Rank A &mdash; Excellent") == 2
    assert "Rank B &mdash; Good" not in new_html
    assert "Rank B — Good" not in new_html


def test_retarget_rank_headers_rejects_lookalike_non_rank_phrases():
    """厳格化 (2026-07-04 実機事故): "Rank Block" / "Rank definitions" 等の
    非見出し文言は誤マッチしない (1 回目の緩い正規表現で 358754421540 の
    description を破壊した事故の回帰テスト)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = (
        "<div>Rank Block (Enso brush) design</div>"
        "<p>Rank definitions table:</p>"
        "<p>Rank Definitions apply to Used items.</p>"
    )
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is False, (
        f"non-rank 'Rank X' phrases must NOT be replaced (got {new_html!r})"
    )
    assert new_html == html
    # 具体的な誤マッチが起きていないこと
    assert "Rank A — Excellent (Enso brush)" not in new_html
    assert "Rank A — Excellent table" not in new_html
    assert "Rank A — Excellent apply to" not in new_html


def test_retarget_rank_headers_requires_em_dash_and_label():
    """厳格化: 'Rank B' 単独 (em-dash + Label 無し) は誤マッチ余地を排除するため
    非マッチ。見出しは必ず "Rank X — Label" 形式で書かれる前提 (テンプレ / AI 生成
    どちらもこの形式で出力する、CLAUDE.md ConditionDescription 運用方針準拠)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<span>Rank B</span> is our internal grade."
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is False
    assert new_html == html


def test_retarget_rank_headers_only_matches_8_rank_codes():
    """厳格化: rank code 部は N/S/A-D/PO/As-Is の 8 段階集合のみに完全一致
    (それ以外の綴りは非マッチ、Rank Foo — Bar 等の想定外パターンは触らない)."""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "<h3>Rank Foo — Custom</h3>"
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is False
    assert new_html == html


# ─────────────────────────────────────────────────
# 14c. MED-3 hardening (2026-07-04 Codex): label whitelist で prose 誤爆防止
# ─────────────────────────────────────────────────

def test_retarget_rank_headers_prose_not_over_consumed():
    """prose 中の `Rank B — Good condition overall.` を label whitelist で "Good" だけ
    切り取って追従。旧 `[A-Za-z ()\\-]{0,29}` greedy だと `Good condition overall`
    まで飲み込んで prose 破壊 (latent) だった。"""
    from tabs._finishing_panel_state import retarget_rank_headers_in_description
    html = "Note: Rank B — Good condition overall. Also see below."
    new_html, changed = retarget_rank_headers_in_description(html, "A", "Excellent")
    assert changed is True
    assert "Rank A &mdash; Excellent condition overall." in new_html, (
        f"label 'Good' だけ置換され prose ' condition overall' は保持される "
        f"(got {new_html!r})"
    )
    assert "Good condition overall" not in new_html


def test_retarget_rank_headers_all_known_labels_matched():
    """RANK_LABELS_EN 全 8 値 (N/S/A-D/PO/As-Is) が label 部として認識されること."""
    from tabs._finishing_panel_state import (
        RANK_LABELS_EN, retarget_rank_headers_in_description,
    )
    label_pairs = [
        ("N", "New"), ("S", "New (Opened)"),
        ("A", "Excellent"), ("B", "Good"),
        ("C", "Fair"), ("D", "Issues"),
        ("PO", "Power-On Only"), ("As-Is", "As-Is"),
    ]
    for code, label in label_pairs:
        assert RANK_LABELS_EN[code] == label
        html = f"<h3>Rank {code} &mdash; {label}</h3>"
        # 別 rank (A) に追従。As-Is から A への遷移も含む。
        target_code = "B" if code != "B" else "A"
        target_label = RANK_LABELS_EN[target_code]
        new_html, changed = retarget_rank_headers_in_description(
            html, target_code, target_label,
        )
        assert changed is True, f"code={code} label={label} が unmatched"


# ─────────────────────────────────────────────────
# 15. HIGH-1 修正 (2026-07-04 Codex): validate_as_is_condition_description が
#     旧ランク定型残留 / `As-Is` 不在を reject すること
# ─────────────────────────────────────────────────

def test_validate_as_is_rejects_stale_rank_template():
    """B → As-Is 切替時に cd_key に `Rank B — Good. ...` が残ったまま送信を試みる
    ケースを reject する (二重ゲート、UI 側の事前クリアと state 層 validate)."""
    from tabs._finishing_panel_state import validate_as_is_condition_description
    msg = validate_as_is_condition_description(
        "As-Is", "Rank B — Good. Tested, fully working. Visible wear.",
    )
    assert msg is not None
    assert "旧ランク" in msg or "Rank" in msg
    assert "Rank B" in msg or "残留" in msg or "定型" in msg


def test_validate_as_is_rejects_missing_as_is_token():
    """As-Is 理由に 'As-Is' が含まれない値 (例: user が自由に書いた
    'No AC adapter for testing') は reject し、正しい書式を促す."""
    from tabs._finishing_panel_state import validate_as_is_condition_description
    msg = validate_as_is_condition_description("As-Is", "No AC adapter for testing")
    assert msg is not None
    assert "As-Is" in msg


def test_validate_as_is_accepts_proper_format():
    """`As-Is — <reason>` 形式は PASS (回帰、既存挙動不変)."""
    from tabs._finishing_panel_state import validate_as_is_condition_description
    assert validate_as_is_condition_description(
        "As-Is", "As-Is — No AC adapter for testing",
    ) is None


def test_validate_as_is_empty_still_rejected():
    """空文字は従来通り reject (回帰)."""
    from tabs._finishing_panel_state import validate_as_is_condition_description
    msg = validate_as_is_condition_description("As-Is", "")
    assert msg is not None
    assert "必須" in msg


def test_validate_non_as_is_rank_returns_none():
    """As-Is 以外は validate を素通り (回帰)."""
    from tabs._finishing_panel_state import validate_as_is_condition_description
    for rank in ("N", "S", "A", "B", "C", "D", "PO", None):
        assert validate_as_is_condition_description(
            rank, "Rank B — Good. Tested, fully working. Visible wear.",
        ) is None


# ─────────────────────────────────────────────────
# 16. HIGH crash 修正 (2026-07-04 live QA): description retarget pending パターン
#     (Streamlit widget instantiate 後の session_state 書込禁止に抵触しないこと)
# ─────────────────────────────────────────────────

def test_schedule_desc_retarget_writes_to_pending_key_not_desc_key():
    """`_render_condition_subblock` からの retarget は desc_key 直接書込せず
    pending キー (`desc_retarget_pending`) 経由で書くこと (widget 制約回避)."""
    from tabs._finishing_panel_state import pf_key, schedule_desc_retarget
    ss: dict = {}
    eid = "123456789012"
    schedule_desc_retarget(ss, eid, "<h3>Rank A &mdash; Excellent</h3>")
    # 直接 desc_key は触られていない
    assert pf_key(eid, "description") not in ss
    # pending にだけ書かれている
    assert ss[pf_key(eid, "desc_retarget_pending")] == "<h3>Rank A &mdash; Excellent</h3>"


def test_consume_pending_desc_retarget_applies_and_clears():
    """次サイクルで widget 生成前に呼ばれ、pending を desc_key へ反映して
    pending を削除する (widget instantiate より前なので制約に抵触しない)."""
    from tabs._finishing_panel_state import (
        consume_pending_desc_retarget, pf_key, schedule_desc_retarget,
    )
    ss: dict = {}
    eid = "999"
    schedule_desc_retarget(ss, eid, "NEW")
    applied = consume_pending_desc_retarget(ss, eid)
    assert applied is True
    assert ss[pf_key(eid, "description")] == "NEW"
    # pending は消えている
    assert pf_key(eid, "desc_retarget_pending") not in ss


def test_consume_pending_desc_retarget_no_pending_returns_false():
    """pending が無ければ何もしない (通常フローで毎回呼ばれるためコスト無視)."""
    from tabs._finishing_panel_state import consume_pending_desc_retarget, pf_key
    ss: dict = {}
    eid = "999"
    ss[pf_key(eid, "description")] = "ORIGINAL"
    applied = consume_pending_desc_retarget(ss, eid)
    assert applied is False
    assert ss[pf_key(eid, "description")] == "ORIGINAL"


def test_desc_retarget_helpers_do_not_touch_desc_key_directly():
    """AST 静的解析: `_render_condition_subblock` は st.session_state[_desc_key] へ
    直接代入しない (pending 経由のみ)。1 回目の実装で live クラッシュした形の再発防止."""
    import ast
    import inspect
    from tabs import _finishing_panel
    src = inspect.getsource(_finishing_panel._render_condition_subblock)
    tree = ast.parse(src)
    # `st.session_state[_desc_key] = ...` の代入が存在しないこと
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if not isinstance(tgt, ast.Subscript):
                    continue
                # Subscript.value = st.session_state, slice = _desc_key
                v = tgt.value
                if (isinstance(v, ast.Attribute) and v.attr == "session_state"
                        and isinstance(tgt.slice, ast.Name)
                        and tgt.slice.id == "_desc_key"):
                    raise AssertionError(
                        "`_render_condition_subblock` が st.session_state[_desc_key] へ"
                        "直接代入している (Streamlit widget instantiate 制約に抵触)。"
                        "schedule_desc_retarget/consume_pending_desc_retarget 経由に"
                        "変更してください。"
                    )


# ─────────────────────────────────────────────────
# 17. retarget_rank_block_in_description (v4 テンプレ 3 要素同時追従)
#     部分追従バグ完遂 2026-07-04 実機再確認
# ─────────────────────────────────────────────────

# v4 テンプレ実物 fixture (data/testdesc16_previews/358027482174.html から抜粋)
_V4_RANK_BLOCK_B_HTML = (
    '<div class="mh-rank">'
    '<div class="mh-rank-brush">'
    '<div class="mh-rb-letter">B</div>'
    '</div>'
    '<h3>Rank B &mdash; Good</h3>'
    '<div class="mh-rank-jp">Tested &middot; Visible Wear</div>'
    '<div class="mh-quick">Tested and confirmed working. Cosmetic wear visible on the housing but does not affect operation.</div>'
    '</div>'
)


def test_retarget_rank_block_all_three_elements_change_for_a():
    """v4 テンプレ実構造 B → A 遷移で letter/h3/chip の 3 要素が同時追従."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    res = retarget_rank_block_in_description(_V4_RANK_BLOCK_B_HTML, "A", "Excellent")
    assert res["h3_changed"] is True
    assert res["letter_changed"] is True
    assert res["chip_changed"] is True
    assert res["any_changed"] is True
    assert res["quick_notes_present"] is True  # mh-quick 存在
    new_html = res["new_html"]
    assert '<div class="mh-rb-letter">A</div>' in new_html
    assert '<h3>Rank A &mdash; Excellent</h3>' in new_html
    assert '<div class="mh-rank-jp">Tested &middot; Minor Wear</div>' in new_html
    # 旧値の残存無し
    assert '<div class="mh-rb-letter">B</div>' not in new_html
    assert 'Rank B &mdash; Good' not in new_html
    assert 'Tested &middot; Visible Wear' not in new_html


def test_retarget_rank_block_round_trip_a_to_b_to_as_is_to_a():
    """A → B → As-Is → A の 3 連遷移で letter/h3/chip 全てが最終 A に収束."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    html = _V4_RANK_BLOCK_B_HTML
    # B → A
    html = retarget_rank_block_in_description(html, "A", "Excellent")["new_html"]
    assert '<div class="mh-rb-letter">A</div>' in html
    # A → B
    html = retarget_rank_block_in_description(html, "B", "Good")["new_html"]
    assert '<div class="mh-rb-letter">B</div>' in html
    assert '<h3>Rank B &mdash; Good</h3>' in html
    assert 'Tested &middot; Visible Wear' in html
    # B → As-Is (完全形で emit されるため round-trip 可能)
    html = retarget_rank_block_in_description(html, "As-Is")["new_html"]
    assert '<div class="mh-rb-letter">As-Is</div>' in html
    assert '<h3>Rank As-Is &mdash; As-Is</h3>' in html
    assert '<div class="mh-rank-jp">Not Tested</div>' in html
    # As-Is → A (往復対応の hard check)
    html = retarget_rank_block_in_description(html, "A", "Excellent")["new_html"]
    assert '<div class="mh-rb-letter">A</div>' in html
    assert '<h3>Rank A &mdash; Excellent</h3>' in html
    assert '<div class="mh-rank-jp">Tested &middot; Minor Wear</div>' in html
    # As-Is / B の残存無し
    assert '<div class="mh-rb-letter">As-Is</div>' not in html
    assert '<div class="mh-rb-letter">B</div>' not in html
    assert 'Rank B &mdash; Good' not in html
    assert 'Rank As-Is' not in html
    assert 'Not Tested' not in html
    assert 'Visible Wear' not in html


def test_retarget_rank_block_no_structure_falls_back_to_h3_only():
    """v4 テンプレ構造 (mh-rb-letter / mh-rank-jp) が無い自作 HTML の場合、
    h3 だけ追従して letter/chip は変更なし (該当要素は skip)."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    html = "<h3>Rank B &mdash; Good</h3><p>plain description</p>"
    res = retarget_rank_block_in_description(html, "A", "Excellent")
    assert res["h3_changed"] is True
    assert res["letter_changed"] is False
    assert res["chip_changed"] is False
    assert res["any_changed"] is True
    assert res["quick_notes_present"] is False  # mh-quick 無し
    assert '<h3>Rank A &mdash; Excellent</h3>' in res["new_html"]


def test_retarget_rank_block_no_pattern_at_all_returns_no_change():
    """Rank 見出しも Rank ブロック要素も含まない description は any_changed=False."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    html = "<h1>Product title</h1><p>Just a description.</p>"
    res = retarget_rank_block_in_description(html, "A", "Excellent")
    assert res["any_changed"] is False
    assert res["new_html"] == html


def test_retarget_rank_block_prose_not_over_consumed_by_h3():
    """prose 誤爆防止 (MED-3 hardening 継承): `Rank B — Good condition overall`
    は label whitelist の 'Good' だけ切り取って追従、 prose は保持."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    html = "Note: Rank B — Good condition overall. See below."
    res = retarget_rank_block_in_description(html, "A", "Excellent")
    assert res["h3_changed"] is True
    assert "Rank A &mdash; Excellent condition overall." in res["new_html"]
    assert "Rank A &mdash; Excellent condition overall condition overall" not in res["new_html"]


def test_retarget_rank_block_quick_notes_flag_only_when_present():
    """quick_notes_present は mh-quick クラス存在で判定 (自由文自動追従不可の
    警告 caption を呼出側に出させるための flag)."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    # mh-quick あり
    res1 = retarget_rank_block_in_description(_V4_RANK_BLOCK_B_HTML, "A", "Excellent")
    assert res1["quick_notes_present"] is True
    # mh-quick なし
    html = '<div class="mh-rb-letter">B</div><h3>Rank B &mdash; Good</h3>'
    res2 = retarget_rank_block_in_description(html, "A", "Excellent")
    assert res2["quick_notes_present"] is False


def test_retarget_rank_block_letter_uses_rank_code_not_label():
    """letter バッジの中身は rank code (単一/短縮)。As-Is はコード自体を入れる."""
    from tabs._finishing_panel_state import retarget_rank_block_in_description
    html = '<div class="mh-rb-letter">B</div>'
    for code in ("N", "S", "A", "B", "C", "D", "PO", "As-Is"):
        res = retarget_rank_block_in_description(html, code)
        assert f'<div class="mh-rb-letter">{code}</div>' in res["new_html"]


def test_retarget_rank_block_chip_uses_middot_entity():
    """chip 語彙は `&middot;` エンティティで統一 (v4 テンプレ実物と整合)."""
    from tabs._finishing_panel_state import (
        RANK_CHIP_EN, retarget_rank_block_in_description,
    )
    html = '<div class="mh-rank-jp">Tested &middot; Working</div>'
    res = retarget_rank_block_in_description(html, "A")
    assert RANK_CHIP_EN["A"] in res["new_html"]
    # A/B/C/D は `&middot;` を含む
    for code in ("A", "B", "C", "D"):
        assert "&middot;" in RANK_CHIP_EN[code], f"rank {code} chip missing middot"
    # PO/S/N/As-Is は単一語なので middot 無し (回帰)
    for code in ("PO", "S", "N", "As-Is"):
        assert "&middot;" not in RANK_CHIP_EN[code], f"rank {code} chip has unexpected middot"
