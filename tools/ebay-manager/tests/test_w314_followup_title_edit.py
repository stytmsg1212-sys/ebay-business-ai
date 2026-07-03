"""W314 Phase 1 S3 (2026-07-03): 採用後フォローアップ欄のタイトル編集 unit test.

対象: tabs/_supplier_followup_state.py (title_is_dirty / detect_origin_risk_words /
apply_followup_title_to_ebay) + tabs/_supplier_followup_section.py の結線。

streamlit runtime を必要とする render 本体はテストせず (既存 followup テスト群と
同方針)、純関数ロジックの unit test + ソース結線 (wiring) test で守る。
eBay API は mock のみ (Q1: 実 API 呼出なし)。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_TABS = _PROJECT_ROOT / "tabs"


# ─────────────────────────────────────────────────
# 1. title_is_dirty
# ─────────────────────────────────────────────────

def test_title_is_dirty_same_value_not_dirty():
    from tabs._supplier_followup_state import title_is_dirty
    assert title_is_dirty("Same Title", "Same Title") is False


def test_title_is_dirty_whitespace_only_diff_not_dirty():
    from tabs._supplier_followup_state import title_is_dirty
    assert title_is_dirty("  Same Title  ", "Same Title") is False


def test_title_is_dirty_changed_value_is_dirty():
    from tabs._supplier_followup_state import title_is_dirty
    assert title_is_dirty("New Title", "Old Title") is True


def test_title_is_dirty_empty_new_title_not_dirty():
    """空文字への変更は dirty 扱いしない (誤って反映ボタンが活性化しない)。"""
    from tabs._supplier_followup_state import title_is_dirty
    assert title_is_dirty("", "Old Title") is False
    assert title_is_dirty("   ", "Old Title") is False


# ─────────────────────────────────────────────────
# 2. 80 字境界 (revise_item_title 委譲 / dirty 判定と組合せ)
# ─────────────────────────────────────────────────

def test_title_is_dirty_80_char_boundary_dirty():
    from tabs._supplier_followup_state import title_is_dirty
    title_80 = "A" * 80
    assert title_is_dirty(title_80, "Old Title") is True


def test_apply_followup_title_delegates_80_char_reject_to_revise(monkeypatch):
    """81 文字は revise_item_title 側で reject され、DB 更新もログ記録も走らない。"""
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)

    update_called = []
    monkeypatch.setattr(
        db_mod, "update_ebay_listing_title",
        lambda eid, title: update_called.append((eid, title)),
    )

    long_title = "A" * 81
    result = apply_followup_title_to_ebay(
        "123456789012", long_title, "Old Title",
        source_tab="followup", candidate_id=1,
    )
    assert result["success"] is False
    assert "80 文字超" in result["message"]
    assert update_called == []


# ─────────────────────────────────────────────────
# 3. detect_origin_risk_words
# ─────────────────────────────────────────────────

def test_detect_origin_risk_words_flags_made_in():
    from tabs._supplier_followup_state import detect_origin_risk_words
    hits = detect_origin_risk_words("Vintage Camera Made in China Excellent")
    assert "made in" in hits


def test_detect_origin_risk_words_ignores_japan_alone():
    """Japan 単体はブランド名/型番等で普通に使われるため対象外 (禁止語ではない)。"""
    from tabs._supplier_followup_state import detect_origin_risk_words
    hits = detect_origin_risk_words("Japan Import Rare Vintage Toy")
    assert hits == []


def test_detect_origin_risk_words_empty_title():
    from tabs._supplier_followup_state import detect_origin_risk_words
    assert detect_origin_risk_words("") == []


# ─────────────────────────────────────────────────
# 4. apply_followup_title_to_ebay: 正常系
# ─────────────────────────────────────────────────

def test_apply_followup_title_success_updates_db(monkeypatch):
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)

    revise_calls = []

    def mock_revise(eid, title, *args, **kwargs):
        revise_calls.append((eid, title))
        return {"success": True, "message": "ok", "new_title": title}

    monkeypatch.setattr(ec_mod, "revise_item_title", mock_revise)

    update_calls = []
    monkeypatch.setattr(
        db_mod, "update_ebay_listing_title",
        lambda eid, title: update_calls.append((eid, title)),
    )

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
        source_tab="followup", candidate_id=42,
    )
    assert result["success"] is True
    assert revise_calls == [("123456789012", "New Title")]
    assert update_calls == [("123456789012", "New Title")]


def test_apply_followup_title_failure_skips_db_update(monkeypatch):
    """eBay revise 失敗時は DB を更新しない (DB↔eBay 乖離防止)。"""
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": False, "message": "API エラー: boom"},
    )
    update_calls = []
    monkeypatch.setattr(
        db_mod, "update_ebay_listing_title",
        lambda eid, title: update_calls.append((eid, title)),
    )

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
        source_tab="followup", candidate_id=42,
    )
    assert result["success"] is False
    assert update_calls == []


def test_apply_followup_title_db_update_exception_still_logs_and_reports_success(monkeypatch):
    """F11 (Codex MED 2026-07-03): eBay 反映済みで DB 更新のみ失敗の三重不整合防止.

    - update_ebay_listing_title が例外 → 上に throw させない (UI エラーで
      「eBay 未変更」誤認 → 二重編集事故を防ぐ)
    - 監査ログは success=True で必ず記録 (eBay 実値が真実の source)
    - 戻り値 success=True + message に DB 反映失敗 + 自然回復案内
    """
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": True, "message": "ItemID xxx の Title を更新",
                          "new_title": a[1]},
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db_mod, "update_ebay_listing_title", _boom)

    log_calls = []
    fake_mod = MagicMock()
    fake_mod.log_content_change = lambda *a, **kw: log_calls.append((a, kw))
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "monitor.listing_content_change_log", fake_mod)

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
        source_tab="followup", candidate_id=7,
    )

    # (a) 上に throw されない = 例外なしでここに到達
    # (b) 監査ログは success=True で記録される (eBay 実値が真実)
    assert len(log_calls) == 1
    args, kwargs = log_calls[0]
    assert args[1] == "title"
    assert args[2] == "Old Title"
    assert args[3] == "New Title"
    assert kwargs["success"] is True
    assert "DB 更新失敗" in (kwargs.get("ebay_ack") or "")
    assert "database is locked" in (kwargs.get("ebay_ack") or "")

    # (c) 戻り値 = success=True + 警告 message + 自然回復案内
    assert result["success"] is True
    assert "eBay タイトルは更新済み" in result["message"]
    assert "DB 反映に失敗" in result["message"]
    assert "自然回復" in result["message"]


def test_apply_followup_title_missing_credentials(monkeypatch):
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {})
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: False)

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
    )
    assert result["success"] is False
    assert "credentials" in result["message"]


# ─────────────────────────────────────────────────
# 5. 監査ログ (listing_content_change_log) — 並行実装中モジュールへの契約
# ─────────────────────────────────────────────────

def test_apply_followup_title_calls_log_content_change_when_available(monkeypatch):
    """listing_content_change_log が利用可能なら log_content_change を呼ぶ (設計書§6契約)。"""
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": True, "message": "ok", "new_title": a[1]},
    )
    monkeypatch.setattr(db_mod, "update_ebay_listing_title", lambda *a, **kw: None)

    log_calls = []
    fake_mod = MagicMock()
    fake_mod.log_content_change = lambda *a, **kw: log_calls.append((a, kw))
    monkeypatch.setitem(sys.modules, "monitor.listing_content_change_log", fake_mod)

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
        source_tab="followup", candidate_id=7,
    )
    assert result["success"] is True
    assert len(log_calls) == 1
    args, kwargs = log_calls[0]
    assert args[0] == "123456789012"
    assert args[1] == "title"
    assert args[2] == "Old Title"
    assert args[3] == "New Title"
    assert kwargs["source_tab"] == "followup"
    assert kwargs["candidate_id"] == 7
    assert kwargs["success"] is True


def test_apply_followup_title_import_error_fallback_does_not_fail_title_update(monkeypatch):
    """listing_content_change_log が未実装 (ImportError) でも title 反映自体は成功する。

    sys.modules[name] = None は Python の標準挙動で import 文に ImportError を
    起こさせる (実ファイルの有無に依存しない決定的なテスト)。
    """
    from tabs._supplier_followup_state import apply_followup_title_to_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.database as db_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(
        ec_mod, "revise_item_title",
        lambda *a, **kw: {"success": True, "message": "ok", "new_title": a[1]},
    )
    update_calls = []
    monkeypatch.setattr(
        db_mod, "update_ebay_listing_title",
        lambda eid, title: update_calls.append((eid, title)),
    )
    monkeypatch.setitem(sys.modules, "monitor.listing_content_change_log", None)

    result = apply_followup_title_to_ebay(
        "123456789012", "New Title", "Old Title",
        source_tab="followup", candidate_id=7,
    )
    assert result["success"] is True
    assert update_calls == [("123456789012", "New Title")]


# ─────────────────────────────────────────────────
# 6. UI 結線 (wiring): _supplier_followup_section.py がタイトル欄を呼ぶ
# ─────────────────────────────────────────────────

def test_followup_section_wires_title_subsection():
    src = (_TABS / "_supplier_followup_section.py").read_text(encoding="utf-8")
    assert "_render_followup_title_subsection" in src
    assert "タイトルも直す" in src


def test_followup_section_title_subsection_importable():
    from tabs._supplier_followup_section import _render_followup_title_subsection
    assert callable(_render_followup_title_subsection)


def test_followup_state_exports_title_helpers():
    from tabs._supplier_followup_state import (
        apply_followup_title_to_ebay,
        detect_origin_risk_words,
        title_is_dirty,
    )
    assert callable(title_is_dirty)
    assert callable(detect_origin_risk_words)
    assert callable(apply_followup_title_to_ebay)
