"""W138-A (2026-05-17): shipping BP の DB 列駆動 (価格同様「最初から
自動表示」) + Codex 指摘解決の固定.

固定する設計:
  - _bp_state_from_db: HIGH-2 NULL 多義性 3 分岐 (unfetched/inline/bp)
  - _fetched_jst_label: UTC→JST 表示変換 (Windows-safe、誤認防止)
  - _sync_db_to_actual: Codex#3 = snap.ok 時 id(None=確定Inline含む) を
    明示 NULL 書込 + fetched_at 同一 UPDATE / not ok 時は BP 列非更新
  - _apply_to_ebay: Codex#1 dirty-flag = selectbox 無操作 (submit==render
    初期DB値) なら bp_changed=False で stale 値の実 eBay 巻き戻し遮断
  - backfill: Codex#2 = UPDATE に write-time guard
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ── fake ShippingPolicyList (Account API 戻り値の最小代役) ──

class _Pol:
    def __init__(self, pid, name):
        self.policy_id = pid
        self.name = name
        self.domestic_service_count = 1
        self.service_names = ["X"]


class _PL:
    def __init__(self, ok=True, policies=None, error=None):
        self.ok = ok
        self.policies = policies or []
        self.error = error

    def name_for(self, pid):
        for p in self.policies:
            if p.policy_id == pid:
                return p.name
        return None


_FAKE_PL = _PL(policies=[_Pol("BP1", "DDP_0.5-1kg"),
                         _Pol("BP2", "STOCK 1day")])


@pytest.fixture
def tpm(monkeypatch):
    import streamlit as st

    class _S(dict):
        pass
    monkeypatch.setattr(st, "session_state", _S())
    from tabs import tab_product_management as _tpm
    return _tpm


def _snap(**kw):
    from monitor.ebay_listing_snapshot import ListingSnapshot
    d = dict(
        item_id="ITEM1", sku="stock:01", start_price_usd=148.0,
        ship_cost_usd=29.0, ship_additional_usd=0.0,
        payment_profile_id="PAY1", return_profile_id="RET1",
        shipping_profile_id="BP1", ack="Success", ok=True, error=None,
    )
    d.update(kw)
    return ListingSnapshot(**d)


# ── _bp_state_from_db: HIGH-2 3 分岐 ──

def test_state_unfetched_when_fetched_at_null(tpm):
    """(a) fetched_at NULL → 未取得。Inline と断定しない."""
    r = tpm._bp_state_from_db(
        {"shipping_profile_id": None, "shipping_profile_fetched_at": None})
    assert r["state"] == "unfetched"
    assert r["ok"] is False
    assert "未取得" in r["error"]


def test_state_inline_when_fetched_but_no_id(tpm):
    """(b) fetched_at あり & id 無し → 確定 Inline (BP 変更不可)."""
    with patch.object(tpm, "_cached_shipping_policies",
                      return_value=_FAKE_PL):
        r = tpm._bp_state_from_db(
            {"shipping_profile_id": "",
             "shipping_profile_fetched_at": "2026-05-17 04:30:00"})
    assert r["state"] == "inline"
    assert r["ok"] is False
    assert "Business Policy 管理ではありません" in r["error"]


def test_state_bp_resolves_name(tpm):
    """(c) fetched_at あり & id あり → BP、name 解決."""
    with patch.object(tpm, "_cached_shipping_policies",
                      return_value=_FAKE_PL):
        r = tpm._bp_state_from_db(
            {"shipping_profile_id": "BP1",
             "shipping_profile_fetched_at": "2026-05-17 04:30:00"})
    assert r["state"] == "bp" and r["ok"] is True
    assert r["id"] == "BP1" and r["name"] == "DDP_0.5-1kg"


def test_state_bp_name_falls_back_to_id_when_policies_fail(tpm):
    with patch.object(tpm, "_cached_shipping_policies",
                      return_value=_PL(ok=False, error="auth")):
        r = tpm._bp_state_from_db(
            {"shipping_profile_id": "BPX",
             "shipping_profile_fetched_at": "2026-05-17 04:30:00"})
    assert r["state"] == "bp" and r["name"] == "BPX"
    assert "名前解決不可" in r["error"]


# ── _fetched_jst_label: UTC→JST (Windows-safe) ──

def test_jst_label_converts_utc_plus9(tpm):
    # 2026-05-17 04:30 UTC → 13:30 JST
    assert tpm._fetched_jst_label("2026-05-17 04:30:00") == "5/17 13:30 JST"


def test_jst_label_date_rollover(tpm):
    # 2026-05-16 20:00 UTC → 翌 5/17 05:00 JST
    assert tpm._fetched_jst_label("2026-05-16 20:00:00") == "5/17 05:00 JST"


def test_jst_label_handles_none_and_garbage(tpm):
    assert tpm._fetched_jst_label(None) == "不明"
    assert tpm._fetched_jst_label("") == "不明"
    assert tpm._fetched_jst_label("not-a-date") == "不明"


# ── _sync_db_to_actual: Codex#3 None-skip 例外 ──

class _Conn:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params):
        self.store["sql"] = sql
        self.store["params"] = params

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sync_writes_bp_id_and_fetched_at_when_ok(tpm, monkeypatch):
    cap = {}
    monkeypatch.setattr(tpm, "get_conn", lambda: _Conn(cap))
    monkeypatch.setattr(tpm, "bump_db_version", lambda: None)
    tpm._sync_db_to_actual("ITEM1", _snap(shipping_profile_id="BP2"))
    assert "shipping_profile_id=?" in cap["sql"]
    assert "shipping_profile_fetched_at=datetime('now')" in cap["sql"]
    assert "BP2" in cap["params"]


def test_sync_writes_explicit_null_for_confirmed_inline(tpm, monkeypatch):
    """Codex#3: GetItem 成功 & BP 無し (確定 Inline) → id を明示 NULL
    で書く (skip しない)。旧 id 残存で (b)/(c) 誤判定を防ぐ."""
    cap = {}
    monkeypatch.setattr(tpm, "get_conn", lambda: _Conn(cap))
    monkeypatch.setattr(tpm, "bump_db_version", lambda: None)
    tpm._sync_db_to_actual("ITEM1", _snap(shipping_profile_id=None))
    assert "shipping_profile_id=?" in cap["sql"]
    # None が params に入る (明示 NULL 書込)
    assert None in cap["params"]
    assert "shipping_profile_fetched_at=datetime('now')" in cap["sql"]


def test_sync_skips_bp_columns_when_getitem_failed(tpm, monkeypatch):
    """not snap.ok → BP 2 列を touch しない (fetched_at 据置=状態(a)維持)."""
    cap = {}
    monkeypatch.setattr(tpm, "get_conn", lambda: _Conn(cap))
    monkeypatch.setattr(tpm, "bump_db_version", lambda: None)
    tpm._sync_db_to_actual(
        "ITEM1", _snap(ok=False, error="boom", start_price_usd=160.0))
    assert "shipping_profile_id" not in cap.get("sql", "")
    assert "shipping_profile_fetched_at" not in cap.get("sql", "")


# ── Codex#1 dirty-flag: stale 巻き戻し遮断 (金銭直結) ──

_CREDS = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "v^T"}


def _patch(tpm, pre, post):
    seq = [pre, post]
    return patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        side_effect=lambda *a, **k: seq.pop(0),
    ), patch.object(tpm, "get_ebay_credentials", return_value=_CREDS)


def test_codex1_untouched_selectbox_does_not_roll_back_external_bp(tpm):
    """金銭直結: DB stale 'BP1' で selectbox 無操作 (submit==render初期値)、
    実 eBay は外部変更で 'BP2' → bp_changed=False、revise_shipping_profile
    を**呼ばない** (実 eBay の BP2 を stale BP1 に巻き戻さない)."""
    pre = _snap(shipping_profile_id="BP2")   # eBay.com 外部変更後の実値
    post = _snap(shipping_profile_id="BP2")
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {
        "new_bp_id": "BP1",               # selectbox default (stale DB)
        "bp_render_initial_id": "BP1",    # render 初期値 = 同じ = 無操作
    }
    with s_patch, c_patch, \
         patch.object(tpm, "revise_shipping_profile") as m:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    m.assert_not_called()                 # ★ 巻き戻し経路が消滅
    assert res["success"] is False
    assert "差分なし" in res["message"]


def test_codex1_touched_selectbox_applies_user_choice(tpm):
    """user が selectbox を操作 (submit != render初期値) → 通常通り
    pre-snapshot と比較し revise."""
    pre = _snap(shipping_profile_id="BP2")
    post = _snap(shipping_profile_id="BP1")
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {
        "new_bp_id": "BP1",               # user が選び直した
        "bp_render_initial_id": "BP2",    # render 初期値と異なる = 操作あり
    }
    with s_patch, c_patch, \
         patch.object(tpm, "revise_shipping_profile",
                      return_value={"success": True}) as m:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    m.assert_called_once()
    assert m.call_args[0][1]["shipping_id"] == "BP1"
    assert res["success"] is True and res["bp_ok"] is True


def test_codex1_no_initial_recorded_preserves_old_behavior(tpm):
    """bp_render_initial_id 未指定 (= 旧 W137/W138 テスト互換) は
    None 扱い → new_bp_id 指定時は従来通り pre-snapshot 比較で動く."""
    pre = _snap(shipping_profile_id="BP_OLD")
    post = _snap(shipping_profile_id="BP1")
    s_patch, c_patch = _patch(tpm, pre, post)
    with s_patch, c_patch, \
         patch.object(tpm, "revise_shipping_profile",
                      return_value={"success": True}) as m:
        res = tpm._apply_to_ebay(
            "ITEM1", {"new_bp_id": "BP1"}, {}, current_sku="x")
    m.assert_called_once()
    assert res["success"] is True


# ── Codex#1-fix2: selectbox widget key が DB BP 値に束ねられている ──

def test_bp_selectbox_key_includes_cur_id_source_contract():
    """金銭直結退行 BLOCK (W134 番人と同じ source-contract 方式)。

    selectbox の key が固定 `pm_bp_{eid}` に戻ると、Streamlit の
    widget value 永続化で ↻/同期後の DB BP 変化を index= が反映できず、
    無操作なのに stale 値を実 eBay へ巻き戻す金銭直結 HIGH が再発する。
    key に DB 由来 cur_id を織り込むこと (widget identity = DB BP 状態)
    を物理的に守る。完全な Streamlit runtime mock は不均衡 (K1) のため
    ソース不変条件で番人化。
    """
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "tabs" / "tab_product_management.py").read_text(
               encoding="utf-8")
    # Shipping Policy selectbox の key 行
    m = re.search(r'"Shipping Policy \(BP\)".*?key=f("|\')'
                  r'(?P<key>pm_bp_[^"\']+)\1',
                  src, re.S)
    assert m, "Shipping Policy selectbox の key 行が見つからない"
    key = m.group("key")
    assert "{eid}" in key and "{cur_id}" in key, (
        f"selectbox key='{key}' が DB BP 値 (cur_id) を含まない。"
        "固定 key への退行 = ↻/同期後 stale BP 巻き戻し金銭直結 HIGH "
        "(Codex#1-fix2) の再発。key=f'pm_bp_{eid}_{cur_id}' を維持せよ。"
    )


# ── MED-2: 差分なしでも post_snapshot を返し DB self-heal ──

def test_no_diff_returns_pre_snapshot_for_db_heal(tpm):
    """差分なし時も post_snapshot=pre-snapshot (実 eBay 値) を返す。
    呼出側 _sync_db_to_actual が stale DB を実 eBay へ自己治癒し
    HIGH-1 を構造補完 (W137 DB:=真実、冪等)."""
    pre = _snap(shipping_profile_id="BP2", start_price_usd=148.0,
                ship_cost_usd=29.0, sku="stock:01")
    post = _snap(shipping_profile_id="BP2")
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {
        "sku": "stock:01", "new_ebay_price": 148.0,
        "new_ship_cost": 29.0,
        "new_bp_id": "BP1", "bp_render_initial_id": "BP1",  # 無操作
    }
    with s_patch, c_patch, \
         patch.object(tpm, "revise_shipping_profile") as m:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    m.assert_not_called()
    assert res["success"] is False and "差分なし" in res["message"]
    assert res["post_snapshot"] is pre, (
        "差分なし path が post_snapshot を捨てている "
        "(stale DB self-heal 不発 = HIGH-1 助長, MED-2)"
    )


# ── Codex#2: backfill UPDATE write-time guard ──

def test_backfill_update_has_write_time_guard(monkeypatch):
    """_update_with_retry の UPDATE は
    `WHERE ebay_item_id=? AND shipping_profile_fetched_at IS NULL`
    (SELECT→GetItem→UPDATE の TOCTOU 窓を封鎖)."""
    import importlib
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    try:
        mod = importlib.import_module(
            "scripts.backfill_shipping_profile_w138a_2026_05_17")
    finally:
        if str(root) in sys.path:
            sys.path.remove(str(root))

    cap = {}

    class _C:
        def execute(self, sql, params):
            cap["sql"] = sql
            cap["params"] = params

            class _Cur:
                rowcount = 1
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "get_conn", lambda: _C())
    rc = mod._update_with_retry("ITEM1", "BP1")
    assert rc == 1
    assert "shipping_profile_fetched_at IS NULL" in cap["sql"]
    assert "WHERE ebay_item_id=?" in cap["sql"]
    assert cap["params"] == ("BP1", "ITEM1")
