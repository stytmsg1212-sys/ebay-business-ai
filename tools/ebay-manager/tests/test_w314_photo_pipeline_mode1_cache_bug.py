"""W314 実機 E2E 発覚バグ (2026-07-03): _render_mode1_ai_compose の cached-empty 経路.

QA 実機 (357866912999 BERNINA / 有在庫・candidate_url なし) で:
    商品管理 → パネル → コンテンツ展開 → Description の radio を
    「AI で生成 ↔ 手動編集」で往復 → fragment rerun → 画像フィールドで
    `IndexError: list index out of range`

再現条件:
    `render_supplier_photo_apply_section(candidate_url="", ...)` を 2 回連続で
    render 呼出 (fragment rerun 相当) すると、1 回目で session_state
    [sk_all_urls] に空 list `[]` がキャッシュされ、2 回目でその cached-empty を
    読んで空 list への index アクセス `all_urls[src_idx]` で IndexError。

Root cause: `tabs/_supplier_photo_pipeline.py::_render_mode1_ai_compose` の
    `if not all_urls: st.error(); return` が `if all_urls is None:` 分岐の
    内側にネストしているため、cache 済 [] (None ではない) では guard が
    skip される。Mode② (L1226) では guard が None 分岐の外側にあり正しく
    動くため、同じ pattern に合わせるのが最小修正 (K2 surgical)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _FakeSpinner:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeCtx:
    """`st.container(border=...)` / `st.columns(...)` 戻り値の context manager stub."""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeStreamlit:
    """streamlit 未初期化環境で `_render_mode1_ai_compose` を直接呼べるようにする stub."""
    def __init__(self):
        self.session_state: dict = {}
        self.errors: list = []
        self.captions: list = []

    def container(self, *a, **kw):
        return _FakeCtx()

    def columns(self, spec, *a, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        return [_FakeCtx() for _ in range(n)]

    def caption(self, msg, *a, **kw):
        self.captions.append(msg)

    def markdown(self, *a, **kw):
        pass

    def image(self, *a, **kw):
        pass

    def spinner(self, msg, *a, **kw):
        return _FakeSpinner()

    def error(self, msg, *a, **kw):
        self.errors.append(msg)

    def success(self, *a, **kw):
        pass

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def button(self, *a, **kw):
        return False

    def selectbox(self, *a, **kw):
        # options[index] 相当 (最小)
        opts = kw.get("options") or (a[1] if len(a) > 1 else [])
        idx = kw.get("index", 0)
        return opts[idx] if opts else None

    def radio(self, *a, **kw):
        opts = kw.get("options") or (a[1] if len(a) > 1 else [])
        return opts[0] if opts else None

    def text_input(self, *a, **kw):
        return ""

    def text_area(self, *a, **kw):
        return ""

    def number_input(self, *a, **kw):
        return kw.get("value", 0)

    def expander(self, *a, **kw):
        return _FakeCtx()

    def rerun(self, *a, **kw):
        pass


def _install(monkeypatch):
    """`_supplier_photo_pipeline.st` と `_image_pipeline_ui.st` の両方を差し替える.

    hero_source_index も session_state を参照するため両モジュール共通の fake が必要。
    """
    from tabs import _supplier_photo_pipeline as spp
    from tabs import _image_pipeline_ui as ipu
    fake = _FakeStreamlit()
    monkeypatch.setattr(spp, "st", fake)
    monkeypatch.setattr(ipu, "st", fake)
    return fake


def test_mode1_ai_compose_cached_empty_urls_does_not_crash(monkeypatch):
    """再現テスト: 1 回目 fetch で [] キャッシュ → 2 回目 render で IndexError.

    修正前は `all_urls[src_idx]` で IndexError、修正後は cached-empty も
    「画像が取得できません」エラーで graceful return する。
    """
    fake = _install(monkeypatch)

    # candidate_url="" のため fetch_supplier_images_all は [] を返す想定。
    # 実 fetch はネットワークに出るので確実に [] を返す stub に差し替える。
    from tabs import _supplier_photo_pipeline as spp
    monkeypatch.setattr(spp, "fetch_supplier_images_all", lambda *a, **kw: [])

    # 1 回目 render (初回マウント相当) — sk_all_urls に [] がキャッシュされる
    spp._render_mode1_ai_compose(
        candidate_id="357866912999",
        candidate_url="",
        ebay_item_id="357866912999",
        candidate_title="BERNINA",
    )
    # 1 回目でエラー表示 + 早期 return (既存正常経路)
    assert len(fake.errors) >= 1
    assert any("画像が取得できません" in e for e in fake.errors)
    # cached-empty を確認 (2 回目の rerun でこれを踏む)
    sk_all_urls = f"{spp._SS}all_image_urls_357866912999"
    assert fake.session_state.get(sk_all_urls) == [], (
        f"1 回目 render 後に sk_all_urls=[] がキャッシュされるはず "
        f"(得 {fake.session_state.get(sk_all_urls)!r})"
    )

    # 2 回目 render (fragment rerun 相当) — cached-empty を踏む
    # 修正前は IndexError: list index out of range が raise される。
    # 修正後は guard が発火して同じエラー表示で return する。
    try:
        spp._render_mode1_ai_compose(
            candidate_id="357866912999",
            candidate_url="",
            ebay_item_id="357866912999",
            candidate_title="BERNINA",
        )
    except IndexError as e:
        raise AssertionError(
            f"2 回目 render で IndexError が発生した (修正前バグの再現、修正後は "
            f"raise せず graceful return するはず): {e}"
        ) from e

    # 2 回目もエラー表示 + 早期 return が期待される
    assert len(fake.errors) >= 2, (
        f"2 回目 render も同じ empty-guard で error 表示するはず "
        f"(errors 累計={len(fake.errors)})"
    )


def test_mode1_ai_compose_cached_none_still_fetches_and_guards(monkeypatch):
    """回帰: 1 回目 (未キャッシュ) 経路は従来通り fetch → 空 → guard で return."""
    fake = _install(monkeypatch)
    from tabs import _supplier_photo_pipeline as spp

    fetch_calls = []

    def _fetch(url, *a, **kw):
        fetch_calls.append(url)
        return []

    monkeypatch.setattr(spp, "fetch_supplier_images_all", _fetch)

    spp._render_mode1_ai_compose(
        candidate_id="357866912999",
        candidate_url="https://example.com/x",  # 非空でも fetch stub は [] を返す
        ebay_item_id="357866912999",
        candidate_title="BERNINA",
    )
    assert fetch_calls == ["https://example.com/x"], (
        "1 回目は必ず fetch_supplier_images_all を呼ぶ (回帰防止)"
    )
    assert any("画像が取得できません" in e for e in fake.errors)


def test_mode1_ai_compose_cached_empty_does_not_refetch(monkeypatch):
    """修正の副作用チェック: cached-empty の 2 回目 render では fetch を再呼出しない.

    (guard を外側に出すだけで fetch の再実行が起きないこと = 課金・帯域の無駄防止。
    K2: 挙動変更は「IndexError 回避」のみ、fetch セマンティクスは不変)。
    """
    fake = _install(monkeypatch)
    from tabs import _supplier_photo_pipeline as spp

    fetch_calls = []
    monkeypatch.setattr(
        spp, "fetch_supplier_images_all",
        lambda url, *a, **kw: (fetch_calls.append(url), [])[1],
    )

    # 1 回目 (fetch 呼出、[] をキャッシュ)
    spp._render_mode1_ai_compose(
        candidate_id="cid_reuse", candidate_url="https://example.com/x",
        ebay_item_id="357866912999", candidate_title="",
    )
    assert fetch_calls == ["https://example.com/x"]

    # 2 回目 (cached [] を読み、fetch は呼ばれない)
    spp._render_mode1_ai_compose(
        candidate_id="cid_reuse", candidate_url="https://example.com/x",
        ebay_item_id="357866912999", candidate_title="",
    )
    assert fetch_calls == ["https://example.com/x"], (
        f"cached-empty 2 回目は fetch を再呼出してはいけない (got {fetch_calls!r})"
    )
