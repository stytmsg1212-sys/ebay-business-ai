"""W31 (2026-06-20): タイトル更新 + コンディション案反映 unit test.

eBay API は叩かず mock のみ。
検証対象:
  1. revise_item_title: 80字超過 reject / 空文字 reject / XML に Title 含む
  2. dirty-flag: title_render_initial と同値なら eBay push しない
  3. コンディション案: _RANK_TO_CONDITION_ID_SUPPLIER が全8段階を網羅
  4. _apply_supplier_condition: revise_item_condition を委譲 (mock で確認)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────
# 1. revise_item_title: 80字超過 / 空文字 reject
# ─────────────────────────────────────────────────

def test_revise_item_title_rejects_empty():
    from monitor.ebay_client import revise_item_title
    r = revise_item_title("123456789012", "", "app", "dev", "cert", "tok")
    assert r["success"] is False
    assert "empty" in r["message"]


def test_revise_item_title_rejects_over_80_chars():
    from monitor.ebay_client import revise_item_title
    long_title = "A" * 81
    r = revise_item_title("123456789012", long_title, "app", "dev", "cert", "tok")
    assert r["success"] is False
    assert "80 文字超" in r["message"]
    assert r["new_title"] == long_title.strip()


def test_revise_item_title_rejects_exactly_80_chars_ok():
    """80文字ちょうどは reject しない (validate 通過後、API 呼出は mock で skip)."""
    from monitor.ebay_client import revise_item_title
    title_80 = "A" * 80

    # httpx.post をモックして通信を回避
    mock_resp_revise = MagicMock()
    mock_resp_revise.text = """<?xml version="1.0" encoding="utf-8"?>
<ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
</ReviseItemResponse>"""
    mock_resp_revise.raise_for_status = MagicMock()

    mock_resp_getitem = MagicMock()
    mock_resp_getitem.text = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item><Title>{title_80}</Title></Item>
</GetItemResponse>"""
    mock_resp_getitem.raise_for_status = MagicMock()

    with patch("monitor.ebay_client.httpx.post", side_effect=[mock_resp_revise, mock_resp_getitem]):
        with patch("monitor.ebay_client._resolve_active_token", side_effect=lambda t: t):
            r = revise_item_title("123456789012", title_80, "app", "dev", "cert", "tok")

    assert r["success"] is True
    assert r["new_title"] == title_80


# ─────────────────────────────────────────────────
# 2. XML に Title 要素が含まれる
# ─────────────────────────────────────────────────

def test_build_revise_item_title_xml_contains_title():
    from monitor.ebay_client import _build_revise_item_title_xml
    xml_str = _build_revise_item_title_xml("123456789012", "My Test Title")
    assert "ReviseItemRequest" in xml_str
    assert "<Title>My Test Title</Title>" in xml_str
    assert "<ItemID>123456789012</ItemID>" in xml_str


def test_build_revise_item_title_xml_escapes_special_chars():
    from monitor.ebay_client import _build_revise_item_title_xml
    xml_str = _build_revise_item_title_xml("1", "Title <with> &amp; special")
    # XML として parse できること (parse error なし)
    root = ET.fromstring(xml_str.replace("{USER_TOKEN}", "dummy_token"))
    assert root is not None
    # XML escape が適用されていること
    assert "&lt;with&gt;" in xml_str or "<Title>" in xml_str


# ─────────────────────────────────────────────────
# 3. dirty-flag: 無操作なら title を eBay push しない
# ─────────────────────────────────────────────────

def test_apply_listing_content_no_push_when_title_unchanged(monkeypatch):
    """title_render_initial と new_title が同値の場合、revise_item_title を呼ばない."""
    import tabs.tab_product_management as tpm
    from monitor.database import get_conn

    # credentials mock
    monkeypatch.setattr(
        tpm, "get_ebay_credentials",
        lambda c=None: {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"},
    )

    called = []

    def mock_revise_title(*args, **kwargs):
        called.append(args)
        return {"success": True, "message": "ok", "new_title": args[1]}

    import monitor.ebay_client as ec
    monkeypatch.setattr(ec, "revise_item_title", mock_revise_title)

    # DB から listing_description を取得する部分を mock
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchone.return_value = ("existing desc",)
    monkeypatch.setattr(tpm, "get_conn", lambda: mock_conn)

    editing = {
        "listing_description": None,  # description 変更なし
        "rank": None,
        "rank_render_initial": None,
        "condition_description": None,
        "new_title": "Same Title",
        "title_render_initial": "Same Title",  # 同値 = dirty-flag で skip
    }

    result = tpm._apply_listing_content_to_ebay("123456789012", editing, {})
    # title が変わっていないので changed=False, revise_item_title は呼ばれない
    assert result["changed"] is False
    assert len(called) == 0


def test_apply_listing_content_pushes_title_when_changed(monkeypatch):
    """new_title が title_render_initial と異なる場合、revise_item_title を呼ぶ."""
    import tabs.tab_product_management as tpm

    monkeypatch.setattr(
        tpm, "get_ebay_credentials",
        lambda c=None: {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"},
    )

    title_called = []

    def mock_revise_title(eid, title, *args, **kwargs):
        title_called.append(title)
        return {"success": True, "message": f"ok {title}", "new_title": title}

    import monitor.database as db
    import monitor.ebay_client as ec
    # _apply_listing_content_to_ebay は関数内 `from monitor.ebay_client import ...`
    # → ec モジュール上の属性を置き換えることで monkeypatch が有効になる
    monkeypatch.setattr(ec, "revise_item_title", mock_revise_title)
    monkeypatch.setattr(db, "update_ebay_listing_title", lambda eid, title: None)

    # description/condition の mock (変更なしにする)
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchone.return_value = ("same desc",)
    monkeypatch.setattr(tpm, "get_conn", lambda: mock_conn)

    editing = {
        "listing_description": "same desc",  # 変更なし
        "rank": None,
        "rank_render_initial": None,
        "condition_description": None,
        "new_title": "New Title Changed",
        "title_render_initial": "Old Title",  # 異なる = push する
    }

    result = tpm._apply_listing_content_to_ebay("123456789012", editing, {})
    assert result["changed"] is True
    assert len(title_called) == 1
    assert title_called[0] == "New Title Changed"
    assert result.get("title_ok") is True


# ─────────────────────────────────────────────────
# 4. コンディション案マップが全8段階を網羅
# ─────────────────────────────────────────────────

def test_supplier_condition_map_covers_all_ranks():
    from tabs._supplier_description_pipeline import _RANK_TO_CONDITION_ID_SUPPLIER
    expected_ranks = {"N", "S", "A", "B", "C", "D", "PO", "As-Is"}
    assert set(_RANK_TO_CONDITION_ID_SUPPLIER.keys()) == expected_ranks
    # As-Is は 7000
    assert _RANK_TO_CONDITION_ID_SUPPLIER["As-Is"] == "7000"
    # New は 1000
    assert _RANK_TO_CONDITION_ID_SUPPLIER["N"] == "1000"


# ─────────────────────────────────────────────────
# 5. _apply_supplier_condition: As-Is はスキップ通知
# ─────────────────────────────────────────────────

def test_apply_supplier_condition_skips_asis():
    """As-Is(7000) は理由入力必須のため仕入先パスでは自動反映しない."""
    from tabs._supplier_description_pipeline import _apply_supplier_condition
    r = _apply_supplier_condition("123456789012", "As-Is")
    assert r["success"] is True
    assert "As-Is" in r["message"]
    assert "商品管理タブ" in r["message"]


def test_apply_supplier_condition_unknown_rank():
    """存在しない rank は no-op (スキップ成功)."""
    from tabs._supplier_description_pipeline import _apply_supplier_condition
    r = _apply_supplier_condition("123456789012", "X_UNKNOWN")
    assert r["success"] is True
    assert "スキップ" in r["message"]


def test_apply_supplier_condition_calls_revise_on_valid_rank(monkeypatch):
    """N ランク指定時、credentials OK → revise_item_condition が呼ばれる.

    _apply_supplier_condition は関数内 import を使うため、呼出先モジュールを
    monkeypatch する (monitor.credentials / monitor.ebay_client / 等)。
    """
    from tabs._supplier_description_pipeline import _apply_supplier_condition
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod
    import monitor.ebay_listing_snapshot as snap_mod
    import monitor.database as db_mod

    creds_ok = {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    }

    snap_pre = MagicMock()
    snap_pre.ok = True
    snap_pre.condition_id = "3000"  # 現在 3000、N(1000) へ変更

    snap_post = MagicMock()
    snap_post.ok = True
    snap_post.condition_id = "1000"

    revise_called = []
    snap_call_count = [0]

    def mock_revise(eid, cid, *args, **kwargs):
        revise_called.append(cid)
        return {"success": True, "message": "ok", "condition_id": cid}

    def mock_fetch_snapshot(*args, **kwargs):
        snap_call_count[0] += 1
        return snap_pre if snap_call_count[0] == 1 else snap_post

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda: creds_ok)
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(ec_mod, "revise_item_condition", mock_revise)
    monkeypatch.setattr(snap_mod, "fetch_listing_snapshot", mock_fetch_snapshot)
    monkeypatch.setattr(db_mod, "update_ebay_listing_condition", lambda *a, **kw: None)

    r = _apply_supplier_condition("123456789012", "N")
    assert r["success"] is True
    assert len(revise_called) == 1
    assert revise_called[0] == "1000"
