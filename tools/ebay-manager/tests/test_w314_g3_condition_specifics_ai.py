"""#44 実装タスク G3 (2026-07-04): description AI 生成と ConditionDescription /
ItemSpecifics の連動.

Scope:
    (1) monitor/listing_generator.py
        - GeneratedListing.condition_description フィールド (65字truncate)
        - generate_listing() が Claude JSON の condition_description を parse する
    (2) tabs/_supplier_description_pipeline.py
        - generate_supplier_description() の戻り値に item_specifics /
          condition_description が追加される (成功パス)
    (3) tabs/_finishing_panel_state.py
        - fetch_condition_and_specifics_from_ebay (GetItem baseline 取得)
        - apply_item_specifics_to_ebay (G2 revise_item_specifics 契約への委譲、
          未実装時 no-op fallback)
        - is_field_dirty("item_specifics", ...) / summarize_specifics
        - FIELD_LABELS_JA / PREVIEW_FIELD_ORDER に item_specifics
        - generate_description_via_ai が item_specifics/condition_description を透過
    (4) tabs/_finishing_panel.py
        - _render_item_specifics_field 関数の存在 + Description&Condition 枠の下で
          呼ばれること
        - _apply_content_changes が item_specifics dirty 時に dispatch する
          (revise_item_specifics 経由、removed_names をメッセージに反映)

eBay 実 API / 実 AI は一切叩かない (全 mock)。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_UI_PATH = _PROJECT_ROOT / "tabs" / "_finishing_panel.py"


def _find_function_def(name: str, path: Path = _UI_PATH):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ═════════════════════════════════════════════════
# 1. monitor.listing_generator: condition_description スキーマ + parse
# ═════════════════════════════════════════════════

def test_generated_listing_condition_description_default_empty():
    from monitor.listing_generator import GeneratedListing
    g = GeneratedListing()
    assert g.condition_description == ""


def test_generated_listing_condition_description_full_init():
    from monitor.listing_generator import GeneratedListing
    g = GeneratedListing(condition_description="Tested OK")
    assert g.condition_description == "Tested OK"


def _make_claude_response(payload: dict) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    import json as _json
    block.text = _json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 10
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


from dataclasses import dataclass  # noqa: E402


@dataclass
class _ScrapedProduct:
    url: str = ""
    platform: str = "mercari"
    title_ja: str | None = None
    price_jpy: int | None = None
    condition_ja: str | None = None
    includes_ja: str | None = None
    description_ja: str | None = None
    weight_hint_g: int | None = None
    image_urls: list | None = None

    def __post_init__(self):
        if self.image_urls is None:
            self.image_urls = []


@dataclass
class _Rank:
    rank_code: str = "A"
    rank_label: str = "Excellent"
    rank_jp: str = "Tested"
    ebay_condition_id: str = "3000"
    confidence: float = 0.9
    reasoning: str = "test"


def _tpl() -> str:
    return "<div class='wrap {{mode_class}}'><h1>{{product_name}}</h1></div>"


def test_generate_listing_parses_condition_description():
    product = _ScrapedProduct(title_ja="Sony ヘッドホン 美品", price_jpy=10000)
    rank = _Rank()
    claude_payload = {
        "title": "Sony Headphones Excellent",
        "product_name": "Sony Headphones Excellent",
        "quick_notes": "Tested working",
        "includes_items": [], "specs": [], "spec_strip": [],
        "category_id": "293", "category_name": "Consumer Electronics",
        "category_candidates": [],
        "item_specifics": {"Brand": "Sony"},
        "condition_description": "Tested and fully working (2026-07). Minor wear.",
    }
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_claude_response(claude_payload)

    with patch("monitor.listing_generator._get_client", return_value=fake_client):
        with patch("monitor.listing_generator.log_anthropic_response", create=True):
            with patch("monitor.ebay_taxonomy.get_category_suggestions", return_value=[]):
                from monitor.listing_generator import generate_listing
                result = generate_listing(product, None, rank, _tpl())

    assert result.generate_error is None
    assert result.condition_description == "Tested and fully working (2026-07). Minor wear."


def test_generate_listing_truncates_condition_description_to_65_chars():
    product = _ScrapedProduct(title_ja="X", price_jpy=1000)
    rank = _Rank()
    long_cd = "A" * 200
    claude_payload = {
        "title": "X", "product_name": "X", "quick_notes": "n",
        "includes_items": [], "specs": [],
        "item_specifics": {}, "condition_description": long_cd,
    }
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_claude_response(claude_payload)

    with patch("monitor.listing_generator._get_client", return_value=fake_client):
        with patch("monitor.listing_generator.log_anthropic_response", create=True):
            from monitor.listing_generator import generate_listing
            result = generate_listing(product, None, rank, _tpl())

    assert len(result.condition_description) == 65


def test_generate_listing_missing_condition_description_defaults_empty():
    """Claude が condition_description キーを返さなくても KeyError にならず空文字."""
    product = _ScrapedProduct(title_ja="X", price_jpy=1000)
    rank = _Rank()
    claude_payload = {
        "title": "X", "product_name": "X", "quick_notes": "n",
        "includes_items": [], "specs": [], "item_specifics": {},
    }
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_claude_response(claude_payload)

    with patch("monitor.listing_generator._get_client", return_value=fake_client):
        with patch("monitor.listing_generator.log_anthropic_response", create=True):
            from monitor.listing_generator import generate_listing
            result = generate_listing(product, None, rank, _tpl())

    assert result.condition_description == ""


def test_listing_generator_prompt_mentions_condition_description_rule():
    """プロンプトが「付属品欠品などの詳細は description へ、CD はランク要約のみ」を明記."""
    from monitor.listing_generator import _STABLE_SYSTEM_PROMPT
    assert "condition_description" in _STABLE_SYSTEM_PROMPT
    assert "65" in _STABLE_SYSTEM_PROMPT
    assert "付属品" in _STABLE_SYSTEM_PROMPT


# ═════════════════════════════════════════════════
# 2. tabs._supplier_description_pipeline: 戻り値に item_specifics / condition_description
# ═════════════════════════════════════════════════

def _fake_product():
    return SimpleNamespace(
        title_ja="テスト商品", platform="amazon", url="http://x",
        price_jpy=1000, condition_ja=None, includes_ja=None,
        weight_hint_g=None, description_ja=None, image_urls=[],
    )


def test_generate_supplier_description_passes_through_item_specifics_and_cd():
    from tabs import _supplier_description_pipeline as pipe

    fake_generated = SimpleNamespace(
        ebay_description="<div>desc</div>", title_en="Test", generate_error=None,
        item_specifics={"Brand": "Sony", "Model": "X"},
        condition_description="Tested OK. Minor wear.",
    )

    with patch("monitor.listing_generator.generate_listing", return_value=fake_generated), \
         patch("monitor.database.get_description_templates",
               return_value=[{"id": 1, "is_default": 1}]), \
         patch("monitor.database.get_description_template",
               return_value={"body": "<div>{{product_name}}</div>"}):
        res = pipe.generate_supplier_description(
            candidate_id=0, candidate_url="http://x", in_stock=False,
            prefetched_product=_fake_product(), rank_override_code="A",
        )

    assert res["success"] is True
    assert res["item_specifics"] == {"Brand": "Sony", "Model": "X"}
    assert res["condition_description"] == "Tested OK. Minor wear."


def test_generate_supplier_description_missing_attrs_default_empty():
    """generate_listing 実装が condition_description/item_specifics を持たなくても
    (dataclass 未同期な環境等) KeyError にならず空値になる."""
    from tabs import _supplier_description_pipeline as pipe

    fake_generated = SimpleNamespace(
        ebay_description="<div>desc</div>", title_en="Test", generate_error=None,
    )

    with patch("monitor.listing_generator.generate_listing", return_value=fake_generated), \
         patch("monitor.database.get_description_templates",
               return_value=[{"id": 1, "is_default": 1}]), \
         patch("monitor.database.get_description_template",
               return_value={"body": "<div>{{product_name}}</div>"}):
        res = pipe.generate_supplier_description(
            candidate_id=0, candidate_url="http://x", in_stock=False,
            prefetched_product=_fake_product(), rank_override_code="A",
        )

    assert res["success"] is True
    assert res["item_specifics"] == {}
    assert res["condition_description"] == ""


# ═════════════════════════════════════════════════
# 3. tabs._finishing_panel_state: fetch_condition_and_specifics_from_ebay
# ═════════════════════════════════════════════════

def test_fetch_condition_and_specifics_missing_credentials(monkeypatch):
    from tabs._finishing_panel_state import fetch_condition_and_specifics_from_ebay
    import monitor.credentials as cred_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {})
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: False)

    result = fetch_condition_and_specifics_from_ebay("123456789012")
    assert result["success"] is False
    assert result["condition_description"] == ""
    assert result["item_specifics"] == {}
    assert "credentials" in result["message"]


def test_fetch_condition_and_specifics_success(monkeypatch):
    from tabs._finishing_panel_state import fetch_condition_and_specifics_from_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(ec_mod, "_resolve_active_token", lambda t: t)

    xml_response = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>123456789012</ItemID>
    <ConditionDescription>Tested OK. Minor wear.</ConditionDescription>
    <ItemSpecifics>
      <NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>
      <NameValueList><Name>Model</Name><Value>WH-1000XM5</Value></NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>"""

    class _FakeResp:
        text = xml_response
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "httpx.post", lambda *a, **kw: _FakeResp(),
    )

    result = fetch_condition_and_specifics_from_ebay("123456789012")
    assert result["success"] is True
    assert result["condition_description"] == "Tested OK. Minor wear."
    assert result["item_specifics"] == {"Brand": "Sony", "Model": "WH-1000XM5"}


def test_fetch_condition_and_specifics_api_error(monkeypatch):
    from tabs._finishing_panel_state import fetch_condition_and_specifics_from_ebay
    import monitor.credentials as cred_mod
    import monitor.ebay_client as ec_mod

    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)
    monkeypatch.setattr(ec_mod, "_resolve_active_token", lambda t: t)

    xml_response = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><LongMessage>Item not found</LongMessage></Errors>
</GetItemResponse>"""

    class _FakeResp:
        text = xml_response
        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResp())

    result = fetch_condition_and_specifics_from_ebay("000000000000")
    assert result["success"] is False
    assert "Item not found" in result["message"]


# ═════════════════════════════════════════════════
# 4. tabs._finishing_panel_state: apply_item_specifics_to_ebay
# ═════════════════════════════════════════════════

def test_apply_item_specifics_to_ebay_no_op_fallback_when_g2_not_implemented(monkeypatch):
    """revise_item_specifics が monitor.ebay_client に無い場合 (G2 未実装) は
    ImportError を no-op fallback で吸収し success=False + 明示メッセージ."""
    from tabs._finishing_panel_state import apply_item_specifics_to_ebay
    import monitor.ebay_client as ec_mod

    if hasattr(ec_mod, "revise_item_specifics"):
        monkeypatch.delattr(ec_mod, "revise_item_specifics")

    result = apply_item_specifics_to_ebay(
        "123456789012", {"Brand": "Sony"},
        app_id="a", dev_id="d", cert_id="c", user_token="t",
    )
    assert result["success"] is False
    assert "未実装" in result["message"] or "revise_item_specifics" in result["message"]
    assert result["removed_names"] == []


def test_apply_item_specifics_to_ebay_delegates_when_implemented(monkeypatch):
    """revise_item_specifics が実装されていれば委譲し、removed_names も透過する."""
    from tabs._finishing_panel_state import apply_item_specifics_to_ebay
    import monitor.ebay_client as ec_mod

    calls = []

    def _fake_revise(item_id, specifics, *, app_id, dev_id, cert_id, user_token,
                      replace_all=True):
        calls.append((item_id, specifics, app_id, dev_id, cert_id, user_token, replace_all))
        return {"success": True, "message": "ok", "removed_names": ["Country of Origin"]}

    monkeypatch.setattr(ec_mod, "revise_item_specifics", _fake_revise, raising=False)

    result = apply_item_specifics_to_ebay(
        "123456789012", {"Brand": "Sony", "Country of Origin": "Japan"},
        app_id="a", dev_id="d", cert_id="c", user_token="t",
    )
    assert result["success"] is True
    assert result["removed_names"] == ["Country of Origin"]
    assert len(calls) == 1
    assert calls[0][6] is True  # replace_all=True


def test_apply_item_specifics_to_ebay_exception_returns_failure(monkeypatch):
    from tabs._finishing_panel_state import apply_item_specifics_to_ebay
    import monitor.ebay_client as ec_mod

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(ec_mod, "revise_item_specifics", _boom, raising=False)

    result = apply_item_specifics_to_ebay(
        "123456789012", {"Brand": "Sony"},
        app_id="a", dev_id="d", cert_id="c", user_token="t",
    )
    assert result["success"] is False
    assert "network down" in result["message"]


# ═════════════════════════════════════════════════
# 5. tabs._finishing_panel_state: is_field_dirty("item_specifics", ...) / summarize_specifics
# ═════════════════════════════════════════════════

def test_is_field_dirty_item_specifics_same_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    d = {"Brand": "Sony", "Model": "X"}
    assert is_field_dirty("item_specifics", d, dict(d)) is False


def test_is_field_dirty_item_specifics_value_changed_is_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty(
        "item_specifics", {"Brand": "Sony"}, {"Brand": "Panasonic"},
    ) is True


def test_is_field_dirty_item_specifics_key_added_is_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty(
        "item_specifics", {"Brand": "Sony"}, {"Brand": "Sony", "Model": "X"},
    ) is True


def test_is_field_dirty_item_specifics_key_removed_is_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty(
        "item_specifics", {"Brand": "Sony", "Model": "X"}, {"Brand": "Sony"},
    ) is True


def test_is_field_dirty_item_specifics_empty_to_empty_not_dirty():
    from tabs._finishing_panel_state import is_field_dirty
    assert is_field_dirty("item_specifics", {}, {}) is False
    assert is_field_dirty("item_specifics", None, None) is False


def test_summarize_specifics_empty():
    from tabs._finishing_panel_state import summarize_specifics
    assert summarize_specifics({}) == "—"
    assert summarize_specifics(None) == "—"


def test_summarize_specifics_short():
    from tabs._finishing_panel_state import summarize_specifics
    out = summarize_specifics({"Brand": "Sony", "Model": "X"})
    assert "Brand: Sony" in out
    assert "(2項目)" in out


def test_summarize_specifics_truncates_many_items():
    from tabs._finishing_panel_state import summarize_specifics
    specifics = {f"K{i}": f"V{i}" for i in range(8)}
    out = summarize_specifics(specifics, max_items=5)
    assert "他3件" in out
    assert "(8項目)" in out


# ═════════════════════════════════════════════════
# 6. tabs._finishing_panel_state: FIELD_LABELS_JA / PREVIEW_FIELD_ORDER / DISPATCH_FIELD_ORDER
# ═════════════════════════════════════════════════

def test_field_labels_includes_item_specifics():
    from tabs._finishing_panel_state import FIELD_LABELS_JA
    assert FIELD_LABELS_JA["item_specifics"] == "Item Specifics"


def test_preview_order_places_item_specifics_after_condition_description():
    from tabs._finishing_panel_state import PREVIEW_FIELD_ORDER
    idx_cd = PREVIEW_FIELD_ORDER.index("condition_description")
    idx_spec = PREVIEW_FIELD_ORDER.index("item_specifics")
    assert idx_spec == idx_cd + 1


def test_dispatch_field_order_still_excludes_item_specifics():
    """rank/condition_description と同様、item_specifics も動的判断のため
    DISPATCH_FIELD_ORDER には含めない。"""
    from tabs._finishing_panel_state import DISPATCH_FIELD_ORDER
    assert "item_specifics" not in DISPATCH_FIELD_ORDER


def test_build_change_preview_item_specifics_uses_summary():
    from tabs._finishing_panel_state import build_change_preview
    fields = {
        "item_specifics": {"before": {}, "after": {"Brand": "Sony"}},
    }
    preview = build_change_preview(fields)
    assert len(preview) == 1
    assert preview[0]["field"] == "item_specifics"
    assert "Brand: Sony" in preview[0]["after"]


# ═════════════════════════════════════════════════
# 7. tabs._finishing_panel_state: generate_description_via_ai の透過
# ═════════════════════════════════════════════════

def test_generate_description_via_ai_passes_through_specifics_and_cd(monkeypatch):
    from tabs import _finishing_panel_state as fps_mod
    import tabs._supplier_description_pipeline as sdp_mod

    def _fake_gen(**kwargs):
        return {
            "success": True, "description_html": "<p>hi</p>",
            "rank_code": "A", "title_en": "Hi", "message": "ok",
            "item_specifics": {"Brand": "Sony"},
            "condition_description": "Tested OK.",
        }

    monkeypatch.setattr(sdp_mod, "generate_supplier_description", _fake_gen)

    result = fps_mod.generate_description_via_ai("https://example.com/x")
    assert result["item_specifics"] == {"Brand": "Sony"}
    assert result["condition_description"] == "Tested OK."


def test_generate_description_via_ai_missing_keys_default_empty(monkeypatch):
    """既存 pipeline 実装 (旧テスト等) が item_specifics/condition_description を
    返さなくても KeyError にならず空値になる (後方互換)."""
    from tabs import _finishing_panel_state as fps_mod
    import tabs._supplier_description_pipeline as sdp_mod

    def _fake_gen(**kwargs):
        return {
            "success": True, "description_html": "<p>hi</p>",
            "rank_code": "A", "title_en": "Hi", "message": "ok",
        }

    monkeypatch.setattr(sdp_mod, "generate_supplier_description", _fake_gen)

    result = fps_mod.generate_description_via_ai("https://example.com/x")
    assert result["item_specifics"] == {}
    assert result["condition_description"] == ""


# ═════════════════════════════════════════════════
# 8. tabs._finishing_panel (UI): _render_item_specifics_field 存在 + 呼出位置
# ═════════════════════════════════════════════════

def test_render_item_specifics_field_helper_present():
    fn = _find_function_def("_render_item_specifics_field")
    assert fn is not None, "_render_item_specifics_field が定義されていない"


def test_item_specifics_field_called_after_description_field():
    """_render_content_group 内で _render_description_field の直後に
    _render_item_specifics_field が呼ばれる (Description & Condition 枠の下、#44)."""
    fn = _find_function_def("_render_content_group")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    idx_desc = src_seg.index("_render_description_field(")
    idx_spec = src_seg.index("_render_item_specifics_field(")
    assert idx_desc < idx_spec, "Item Specifics は description フィールドの後に描画されるべき"


def test_item_specifics_field_uses_data_editor():
    fn = _find_function_def("_render_item_specifics_field")
    assert fn is not None
    src_seg = ast.get_source_segment(_UI_PATH.read_text(encoding="utf-8"), fn) or ""
    assert "st.data_editor(" in src_seg


def test_condition_subblock_accepts_config_param():
    """#44: CD baseline 取得に config (credentials 解決用) が必要なため
    _render_condition_subblock は config 引数を受け取る."""
    fn = _find_function_def("_render_condition_subblock")
    assert fn is not None
    arg_names = [a.arg for a in fn.args.args]
    assert "config" in arg_names


# ═════════════════════════════════════════════════
# 9. tabs._finishing_panel (dispatch): item_specifics dirty → revise_item_specifics
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

    def info(self, *a, **kw):
        pass

    def rerun(self, *a, **kw):
        pass


def _install_common(monkeypatch, revise_specifics_impl=None):
    from tabs import _finishing_panel as fp
    fake = _FakeStreamlit()
    monkeypatch.setattr(fp, "st", fake)

    import monitor.credentials as cred_mod
    monkeypatch.setattr(cred_mod, "get_ebay_credentials", lambda config=None: {
        "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
    })
    monkeypatch.setattr(cred_mod, "ebay_credentials_ok", lambda c: True)

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

    import monitor.ebay_client as ec_mod
    if revise_specifics_impl is not None:
        monkeypatch.setattr(
            ec_mod, "revise_item_specifics", revise_specifics_impl, raising=False,
        )

    import ui_cache
    bump_calls = []
    monkeypatch.setattr(ui_cache, "bump_db_version", lambda: bump_calls.append(1))

    return fake, log_calls, bump_calls


def _base_fields():
    return {
        "title": {"before": "T", "after": "T"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": "B", "after": "B"},
        "condition_description": {"before": "", "after": ""},
        "quantity": {"before": 1, "after": 1},
    }


def test_apply_item_specifics_dirty_dispatches_revise_and_marks_synced(monkeypatch):
    calls = []

    def _fake_revise(item_id, specifics, *, app_id, dev_id, cert_id, user_token,
                      replace_all=True):
        calls.append((item_id, dict(specifics)))
        return {"success": True, "message": "ok", "removed_names": []}

    fake, log_calls, bump_calls = _install_common(monkeypatch, revise_specifics_impl=_fake_revise)

    from tabs._finishing_panel import _apply_content_changes
    fields = _base_fields()
    fields["item_specifics"] = {"before": {"Brand": "Sony"}, "after": {"Brand": "Panasonic"}}

    _apply_content_changes(
        "111", fields, config=None, source_tab="product_management", candidate_id=None,
    )

    assert len(calls) == 1
    assert calls[0] == ("111", {"Brand": "Panasonic"})
    assert bump_calls == [1]
    # baseline (item_specifics_initial) は反映後の dict に更新される (JSON 文字列ではない)
    assert fake.session_state.get("pf_111_item_specifics_initial") == {"Brand": "Panasonic"}
    # 監査ログには JSON 文字列化された before/after が渡る
    spec_logs = [a for (a, kw) in log_calls if a[1] == "item_specifics"]
    assert len(spec_logs) == 1
    assert spec_logs[0][2] == '{"Brand": "Sony"}'
    assert spec_logs[0][3] == '{"Brand": "Panasonic"}'


def test_apply_item_specifics_removed_names_shown_in_message(monkeypatch):
    """G2 が Country of Origin 等を自動除去した場合、メッセージに明示される."""
    def _fake_revise(item_id, specifics, *, app_id, dev_id, cert_id, user_token,
                      replace_all=True):
        return {"success": True, "message": "ok",
                "removed_names": ["Country of Origin", "Manufacturer"]}

    fake, log_calls, bump_calls = _install_common(monkeypatch, revise_specifics_impl=_fake_revise)

    from tabs._finishing_panel import _apply_content_changes
    fields = _base_fields()
    fields["item_specifics"] = {
        "before": {}, "after": {"Brand": "Sony", "Country of Origin": "Japan"},
    }
    _apply_content_changes(
        "111", fields, config=None, source_tab="product_management", candidate_id=None,
    )

    assert any(
        "Country of Origin" in msg and "Manufacturer" in msg for msg in fake.successes
    ), f"removed_names がメッセージに反映されていない: {fake.successes!r}"


def test_apply_item_specifics_not_dirty_no_dispatch(monkeypatch):
    calls = []

    def _fake_revise(*a, **kw):
        calls.append(1)
        return {"success": True, "message": "should not run", "removed_names": []}

    fake, log_calls, bump_calls = _install_common(monkeypatch, revise_specifics_impl=_fake_revise)

    from tabs._finishing_panel import _apply_content_changes
    fields = _base_fields()
    fields["item_specifics"] = {"before": {"Brand": "Sony"}, "after": {"Brand": "Sony"}}
    _apply_content_changes(
        "111", fields, config=None, source_tab="product_management", candidate_id=None,
    )
    assert calls == []
    assert bump_calls == []


def test_apply_item_specifics_g2_not_implemented_reports_failure_no_crash(monkeypatch):
    """G2 revise_item_specifics が未実装でも panel 全体はクラッシュせず
    failure メッセージを表示する (no-op fallback, Q0)."""
    import monitor.ebay_client as ec_mod
    fake, log_calls, bump_calls = _install_common(monkeypatch, revise_specifics_impl=None)
    if hasattr(ec_mod, "revise_item_specifics"):
        monkeypatch.delattr(ec_mod, "revise_item_specifics")

    from tabs._finishing_panel import _apply_content_changes
    fields = _base_fields()
    fields["item_specifics"] = {"before": {}, "after": {"Brand": "Sony"}}
    _apply_content_changes(
        "111", fields, config=None, source_tab="product_management", candidate_id=None,
    )
    assert bump_calls == [], "revise 未実装で失敗した場合は DB を更新しない"
    assert any("Item Specifics" in msg for msg in fake.errors)


def test_apply_no_item_specifics_field_backward_compat(monkeypatch):
    """item_specifics key を持たない fields (旧テスト等価な呼出) でも KeyError にならない."""
    def _fake_revise_title(item_id, new_title, *a, **kw):
        return {"success": True, "message": "ok", "new_title": new_title}

    fake, log_calls, bump_calls = _install_common(monkeypatch)
    import monitor.ebay_client as ec_mod
    monkeypatch.setattr(ec_mod, "revise_item_title", _fake_revise_title)

    from tabs._finishing_panel import _apply_content_changes
    fields = {
        "title": {"before": "Old", "after": "New"},
        "description": {"before": "d", "after": "d"},
        "rank": {"before": None, "after": None},
        "condition_description": {"before": "", "after": ""},
        "quantity": {"before": 1, "after": 1},
        # item_specifics 未指定
    }
    _apply_content_changes(
        "111", fields, config=None, source_tab="product_management", candidate_id=None,
    )
    assert bump_calls == [1]
