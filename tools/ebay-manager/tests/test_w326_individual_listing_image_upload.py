# -*- coding: utf-8 -*-
"""W326 (2026-07-04): 個別出品タブ「ローカル画像アップロード」機能の単体テスト。

依頼ボード原文: 「個別出品で有在庫商品を出品する際、画像をアップロードする方法が
なく出品できない。画像合成以外に画像をアップロードする仕組みを追加してほしい」。

検証対象:
  - _resolve_uploaded_local_urls_ordered: main_idx 反映の並び替え、
    未アップロード/一部失敗 (None 混在) 時は空リストを返す (Q0)。
  - _resolve_listing_image_urls: アップロード画像を既存の
    processed > selected > raw チェーンの先頭に併合 (K2: 既存優先順は不変)。
  - _sync_uploaded_local_images: file_uploader 返却値をディスク保存 + 差分検知。
  - _upload_local_images_to_eps: 失敗ファイルを None で保持 (silent drop 禁止)。
  - STEP5: アップロード未完了時に Add ボタンが disabled になるゲート条件。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import tabs.tab_individual_listing as tab
import monitor.image_pipeline_shared as image_pipeline_shared

_SS = tab._SS


class _FakeStatus:
    def __init__(self):
        self.label = None
        self.state = None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def update(self, label=None, state=None):
        self.label = label
        self.state = state


class _FakeST:
    """_resolve_* / _sync_uploaded_local_images / _upload_local_images_to_eps 用の最小 st。"""

    def __init__(self, session_state: dict):
        self.session_state = session_state
        self.errors: list[str] = []

    def error(self, msg, *_a, **_k):
        self.errors.append(str(msg))

    def warning(self, *_a, **_k):
        return None

    def status(self, *_a, **_k):
        return _FakeStatus()


class _FakeUploadedFile:
    """st.file_uploader が返す UploadedFile の最小互換オブジェクト。"""

    def __init__(self, name: str, content: bytes):
        self.name = name
        self.size = len(content)
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


def _make_png_bytes(width: int, height: int) -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────
# _resolve_uploaded_local_urls_ordered
# ─────────────────────────────────────────────

def test_resolve_uploaded_urls_reorders_main_first(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}, {"path": "b.jpg"}, {"path": "c.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/a", "https://eps/b", "https://eps/c"],
        f"{_SS}uploaded_main_idx": 1,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    result = tab._resolve_uploaded_local_urls_ordered()
    assert result == ["https://eps/b", "https://eps/a", "https://eps/c"]


def test_resolve_uploaded_urls_empty_when_not_uploaded(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._resolve_uploaded_local_urls_ordered() == []


def test_resolve_uploaded_urls_empty_when_partial_failure(monkeypatch):
    """Q0: 一部失敗 (None 混在) の間は silent に画像を減らさず空リストを返す。"""
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}, {"path": "b.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/a", None],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._resolve_uploaded_local_urls_ordered() == []


# ─────────────────────────────────────────────
# _resolve_listing_image_urls (既存優先順の回帰 + アップロード併合)
# ─────────────────────────────────────────────

def test_resolve_listing_image_urls_unchanged_without_upload(monkeypatch):
    """アップロード画像が無ければ従来通り processed > selected > raw。"""
    state = {
        f"{_SS}processed_image_urls": ["https://eps/hero"],
        f"{_SS}selected_image_urls": ["https://supplier/1.jpg"],
        f"{_SS}scraped_product": {"image_urls": ["https://supplier/raw.jpg"]},
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._resolve_listing_image_urls() == ["https://eps/hero"]


def test_resolve_listing_image_urls_merges_upload_in_front(monkeypatch):
    """アップロード画像 (main 先頭) + 既存 processed を併用マージ (dedup 済)。"""
    state = {
        f"{_SS}processed_image_urls": ["https://eps/hero"],
        f"{_SS}selected_image_urls": [],
        f"{_SS}scraped_product": {},
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}, {"path": "b.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/up_a", "https://eps/up_b"],
        f"{_SS}uploaded_main_idx": 1,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    result = tab._resolve_listing_image_urls()
    assert result == ["https://eps/up_b", "https://eps/up_a", "https://eps/hero"]


def test_resolve_listing_image_urls_upload_only_no_supplier_images(monkeypatch):
    """主要ユースケース: 仕入先に画像が無い商品でもアップロード画像だけで出品できる。"""
    state = {
        f"{_SS}processed_image_urls": [],
        f"{_SS}selected_image_urls": [],
        f"{_SS}scraped_product": {"image_urls": []},
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/up_a"],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._resolve_listing_image_urls() == ["https://eps/up_a"]


# ─────────────────────────────────────────────
# _sync_uploaded_local_images
# ─────────────────────────────────────────────

def test_sync_uploaded_local_images_saves_and_dims(monkeypatch, tmp_path):
    state = {
        f"{_SS}sku": "test_w326_sku",
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": ["stale"],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    monkeypatch.chdir(tmp_path)

    f1 = _FakeUploadedFile("photo1.png", _make_png_bytes(600, 800))
    tab._sync_uploaded_local_images([f1])

    saved = state[f"{_SS}uploaded_local_images"]
    assert len(saved) == 1
    assert saved[0]["width"] == 600
    assert saved[0]["height"] == 800
    assert Path(saved[0]["path"]).exists()
    # 画像集合が変わったので EPS 済 URL は無効化される
    assert state[f"{_SS}uploaded_local_eps_urls"] == []


def test_sync_uploaded_local_images_dedupes_unchanged_input(monkeypatch, tmp_path):
    """同一 (name,size) の再 rerun では再保存されない (signature 一致で早期 return)。"""
    state = {
        f"{_SS}sku": "test_w326_sku2",
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    monkeypatch.chdir(tmp_path)

    f1 = _FakeUploadedFile("photo1.png", _make_png_bytes(500, 500))
    tab._sync_uploaded_local_images([f1])
    first_path = state[f"{_SS}uploaded_local_images"][0]["path"]

    # 2回目 (同じ file, 同じ rerun 想定): signature 一致 → 何もしない
    state[f"{_SS}uploaded_local_eps_urls"] = ["https://eps/kept"]
    tab._sync_uploaded_local_images([f1])
    assert state[f"{_SS}uploaded_local_images"][0]["path"] == first_path
    assert state[f"{_SS}uploaded_local_eps_urls"] == ["https://eps/kept"]


# ─────────────────────────────────────────────
# _upload_local_images_to_eps (Q0: 失敗を None で保持、silent drop しない)
# ─────────────────────────────────────────────

def test_upload_local_images_to_eps_partial_failure_kept_as_none(monkeypatch, tmp_path):
    p_ok = tmp_path / "ok.jpg"
    p_ok.write_bytes(b"ok-bytes")
    p_fail = tmp_path / "fail.jpg"
    p_fail.write_bytes(b"fail-bytes")

    state = {
        f"{_SS}uploaded_local_images": [
            {"path": str(p_ok), "name": "ok.jpg"},
            {"path": str(p_fail), "name": "fail.jpg"},
        ],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))

    outcome = image_pipeline_shared.EpsUploadOutcome(
        success=False,
        eps_urls=["https://eps/ok"],
        failed=[("fail.jpg", "eBay error: quota exceeded")],
    )
    monkeypatch.setattr(image_pipeline_shared, "upload_to_eps_cached", lambda *a, **k: outcome)

    tab._upload_local_images_to_eps()

    results = state[f"{_SS}uploaded_local_eps_urls"]
    assert results == ["https://eps/ok", None]


# ─────────────────────────────────────────────
# W326 code review M1: `stock:01` 等コロン含み SKU の Windows mkdir 回避
# ─────────────────────────────────────────────

def test_safe_dir_name_sanitizes_colon_sku():
    """`stock:01` は Windows で mkdir が落ちるので `_` に置換される。"""
    assert tab._safe_dir_name("stock:01") == "stock_01"
    assert tab._safe_dir_name("stock: 1") == "stock__1"
    assert tab._safe_dir_name("ebayyh_p1221413657") == "ebayyh_p1221413657"


def test_safe_dir_name_empty_or_none_falls_back_to_temp():
    """空 / None SKU は temp_<epoch> に fallback (mkdir 対象になる保証)。"""
    assert tab._safe_dir_name(None).startswith("temp_")
    assert tab._safe_dir_name("").startswith("temp_")
    assert tab._safe_dir_name(":::").startswith("___")  # 3 コロン → 3 アンダースコアで残る


def test_sync_uploaded_local_images_mkdir_survives_colon_sku(monkeypatch, tmp_path):
    """`stock:01` SKU で mkdir が OSError で落ちず、画像が保存される (M1 回帰)。"""
    state = {
        f"{_SS}sku": "stock:01",  # 有在庫 SKU: `:` を含むのが正式仕様
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    monkeypatch.chdir(tmp_path)

    f1 = _FakeUploadedFile("stock_photo.png", _make_png_bytes(600, 400))
    tab._sync_uploaded_local_images([f1])

    saved = state[f"{_SS}uploaded_local_images"]
    assert len(saved) == 1
    saved_path = Path(saved[0]["path"])
    assert saved_path.exists()
    # `:` がサニタイズされた dir 名になっていること (Windows mkdir OK)
    assert "stock_01" in saved_path.parts


# ─────────────────────────────────────────────
# W326 code review M2: STEP5 ゲートと resolve の pending 判定を単一 helper に集約
# ─────────────────────────────────────────────

def test_local_upload_pending_false_when_no_upload(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_eps_urls": [],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._local_upload_pending() is False


def test_local_upload_pending_true_when_eps_not_run(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}],
        f"{_SS}uploaded_local_eps_urls": [],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._local_upload_pending() is True


def test_local_upload_pending_true_when_partial_failure(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}, {"path": "b.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/a", None],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._local_upload_pending() is True


def test_local_upload_pending_false_when_all_uploaded(monkeypatch):
    state = {
        f"{_SS}uploaded_local_images": [{"path": "a.jpg"}, {"path": "b.jpg"}],
        f"{_SS}uploaded_local_eps_urls": ["https://eps/a", "https://eps/b"],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    assert tab._local_upload_pending() is False


# ─────────────────────────────────────────────
# W326 code review L1: 保存失敗時に user 可視の警告を出す
# ─────────────────────────────────────────────

def test_sync_uploaded_local_images_shows_warning_on_save_failure(monkeypatch, tmp_path):
    """write_bytes が OSError なら user に見える st.warning を必ず出す (silent 禁止)。"""
    state = {
        f"{_SS}sku": "test_w326_l1",
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }

    warnings: list[str] = []

    class _WarnCaptureST(_FakeST):
        def warning(self, msg, *_a, **_k):
            warnings.append(str(msg))

    monkeypatch.setattr(tab, "st", _WarnCaptureST(state))
    monkeypatch.chdir(tmp_path)

    # write_bytes を OSError で落とす
    from pathlib import Path as _RealPath
    original_write = _RealPath.write_bytes

    def _fail_write(self, data):
        raise OSError("disk full (test)")

    monkeypatch.setattr(_RealPath, "write_bytes", _fail_write)
    try:
        f1 = _FakeUploadedFile("broken.png", _make_png_bytes(500, 500))
        tab._sync_uploaded_local_images([f1])
    finally:
        monkeypatch.setattr(_RealPath, "write_bytes", original_write)

    # 保存失敗 = images list は空、st.warning が呼ばれている
    assert state[f"{_SS}uploaded_local_images"] == []
    assert any("broken.png" in w and "保存できません" in w for w in warnings), warnings


# ─────────────────────────────────────────────
# W326 HEIC 対応 (2026-07-04): iPhone 写真の受入 + JPEG 変換
# ─────────────────────────────────────────────

def _make_heic_bytes(width: int, height: int) -> bytes:
    """テスト用の実 HEIC バイト列を生成 (pillow-heif 経由).

    pillow-heif が読み書き両対応なのを利用 (未導入環境では ImportError で test skip)。
    """
    import io
    import pillow_heif
    pillow_heif.register_heif_opener()
    img = Image.new("RGB", (width, height), (100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="HEIF", quality=80)
    return buf.getvalue()


def test_convert_heic_bytes_to_jpeg_roundtrip(monkeypatch):
    """実 HEIC バイト → JPEG バイト への変換が成功し、PIL で JPEG として読み戻せる。"""
    import io
    heic_bytes = _make_heic_bytes(640, 480)
    jpeg_bytes = tab._convert_heic_bytes_to_jpeg(heic_bytes, "photo.heic")
    assert jpeg_bytes is not None
    # 実際に JPEG として parse できることを確認
    with Image.open(io.BytesIO(jpeg_bytes)) as reopened:
        assert reopened.format == "JPEG"
        assert reopened.size == (640, 480)


def test_convert_heic_bytes_returns_none_when_pillow_heif_missing(monkeypatch):
    """pillow_heif が import 不能な環境では None を返す (Q0: silent 落ちさせない)。"""
    import builtins
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "pillow_heif":
            raise ImportError("simulated missing pillow_heif")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    result = tab._convert_heic_bytes_to_jpeg(b"dummy_heic_bytes", "photo.heic")
    assert result is None


def test_sync_uploaded_local_images_heic_saved_as_jpeg(monkeypatch, tmp_path):
    """HEIC ファイルを file_uploader 経由でアップロードすると JPEG として保存される (実動作 smoke)。"""
    state = {
        f"{_SS}sku": "test_w326_heic",
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))
    monkeypatch.chdir(tmp_path)

    heic_bytes = _make_heic_bytes(600, 400)
    f = _FakeUploadedFile("iphone.heic", heic_bytes)
    tab._sync_uploaded_local_images([f])

    saved = state[f"{_SS}uploaded_local_images"]
    assert len(saved) == 1
    saved_path = Path(saved[0]["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".jpg"  # HEIC → JPEG に変換されている
    # 解像度は保存後の JPEG から再取得されるので元と一致
    assert saved[0]["width"] == 600
    assert saved[0]["height"] == 400
    # 実 JPEG バイトになっていることを確認
    import io
    with Image.open(io.BytesIO(saved_path.read_bytes())) as reopened:
        assert reopened.format == "JPEG"


def test_sync_uploaded_local_images_heic_no_lib_shows_error(monkeypatch, tmp_path):
    """pillow_heif が使えない環境では該当 HEIC ファイルのみ skip + st.error 表示、
    jpg/png は通常どおり処理される (Q0 silent drop 禁止)。"""
    state = {
        f"{_SS}sku": "test_w326_heic_no_lib",
        f"{_SS}uploaded_local_images": [],
        f"{_SS}uploaded_local_sigs": [],
        f"{_SS}uploaded_local_eps_urls": [],
        f"{_SS}uploaded_main_idx": 0,
    }
    errors: list[str] = []
    warnings: list[str] = []

    class _CaptureST(_FakeST):
        def error(self, msg, *_a, **_k):
            errors.append(str(msg))

        def warning(self, msg, *_a, **_k):
            warnings.append(str(msg))

    monkeypatch.setattr(tab, "st", _CaptureST(state))
    monkeypatch.chdir(tmp_path)

    # HEIC 変換だけ強制的に失敗させる (jpg/png 経路は不変)
    monkeypatch.setattr(tab, "_convert_heic_bytes_to_jpeg", lambda *a, **k: None)

    heic = _FakeUploadedFile("iphone.heic", b"dummy_heic_bytes")
    jpg = _FakeUploadedFile("normal.jpg", _make_png_bytes(500, 500))
    tab._sync_uploaded_local_images([heic, jpg])

    saved = state[f"{_SS}uploaded_local_images"]
    # jpg のみ保存、heic は skip
    assert len(saved) == 1
    assert saved[0]["name"] == "normal.jpg"
    # user 可視のエラーメッセージ (silent drop 禁止)
    assert any("HEIC" in e and "iphone.heic" in e for e in errors), errors


def test_upload_local_images_to_eps_all_success(monkeypatch, tmp_path):
    p1 = tmp_path / "1.jpg"
    p1.write_bytes(b"a")
    p2 = tmp_path / "2.jpg"
    p2.write_bytes(b"b")

    state = {
        f"{_SS}uploaded_local_images": [
            {"path": str(p1), "name": "1.jpg"},
            {"path": str(p2), "name": "2.jpg"},
        ],
    }
    monkeypatch.setattr(tab, "st", _FakeST(state))

    outcome = image_pipeline_shared.EpsUploadOutcome(
        success=True,
        eps_urls=["https://eps/1", "https://eps/2"],
        failed=[],
    )
    monkeypatch.setattr(image_pipeline_shared, "upload_to_eps_cached", lambda *a, **k: outcome)

    tab._upload_local_images_to_eps()

    assert state[f"{_SS}uploaded_local_eps_urls"] == ["https://eps/1", "https://eps/2"]
