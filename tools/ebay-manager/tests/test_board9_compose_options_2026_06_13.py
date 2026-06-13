#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board#9 (2026-06-13): プレート合成 位置/モデル選択 + manifest compose_options 回帰.

カバレッジ:
  1. build_compose_prompt("auto") = board#9 以前の _COMPOSE_PROMPT と byte 一致
     (sha256 pin。reviewer HIGH-4: 現行定数同士の比較はトートロジーのため、
      git HEAD=e1b1c5e 時点のソースから ast で literal 抽出した hash を固定値で持つ)
  2. 未知 position は auto に fallback (Q0: warning 付き、無音クラッシュなし)
  3. bottom_left / bottom_right variant の placement 指示差分
  4. COMPOSE_MODELS / PLATE_POSITIONS の構成
  5. _manifest_matches の compose_options 互換 (旧 manifest = defaults 扱い)
  6. _write_manifest compose_options=None で既存値温存
  7. supplier 経路 reuse ガード (reviewer HIGH-1: URL list 照合 / HIGH-3: legacy_reuse_ok)
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor.image_composer_gemini as icg
import monitor.image_pipeline_shared as ips


# ─────────────────────────────────────────────
# 1-4: prompt variant / model registry
# ─────────────────────────────────────────────

# board#9 以前 (commit e1b1c5e) の _COMPOSE_PROMPT 文字列 literal の sha256。
# 算出手順: git show e1b1c5e:tools/ebay-manager/monitor/image_composer_gemini.py
#           → ast.parse で _COMPOSE_PROMPT の str literal 抽出 (len=2594)
#           → hashlib.sha256(literal.encode("utf-8")).hexdigest()
# auto variant はこの旧 prompt と byte 一致でなければならない (W178/180/181 教訓:
# baseline prompt の無断変更は合成品質 regression に直結)。
_LEGACY_PROMPT_SHA256 = "21016d814aabe1ded734834d876b61b10d7819da7ac5f42d6bbd7a6e66ce1e9d"


def test_auto_prompt_pinned_to_legacy_sha256():
    """auto variant = board#9 以前の _COMPOSE_PROMPT と byte 一致 (外部 pin、恒真でない)."""
    actual = hashlib.sha256(
        icg.build_compose_prompt("auto").encode("utf-8")
    ).hexdigest()
    assert actual == _LEGACY_PROMPT_SHA256, (
        "auto variant が board#9 以前の baseline prompt から変化した。"
        "意図的な変更なら git show で新 literal の sha256 を再算出して pin を更新すること。"
    )


def test_auto_prompt_identical_to_legacy_compose_prompt():
    """後方互換定数 _COMPOSE_PROMPT も auto variant と一致 (他 module 参照の整合)."""
    assert icg.build_compose_prompt("auto") == icg._COMPOSE_PROMPT


def test_unknown_position_falls_back_to_auto(caplog):
    """未知 position は auto に fallback + warning (Q0 silent skip 防止)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="monitor.image_composer_gemini"):
        result = icg.build_compose_prompt("top_center")
    assert result == icg.build_compose_prompt("auto")
    assert any("top_center" in r.message for r in caplog.records)


def test_bottom_left_variant_has_no_fallback_clause():
    """左下固定 variant は右下 fallback 文を含まず、固定指示を含む."""
    p = icg.build_compose_prompt("bottom_left")
    assert "shift the tag to the" not in p
    assert "bottom-left corner is the only" in p


def test_bottom_right_variant_mirrors_placement():
    """右下固定 variant は BOTTOM-RIGHT 指示を含み、左下指示を含まない."""
    p = icg.build_compose_prompt("bottom_right")
    assert "BOTTOM-RIGHT" in p
    assert "BOTTOM-LEFT CORNER" not in p


def test_variants_share_header_and_tail():
    """3 variant は header / tail (TAG FIDELITY 等) を共有する."""
    for pos in icg.PLATE_POSITIONS:
        p = icg.build_compose_prompt(pos)
        assert p.startswith(icg._PROMPT_HEADER)
        assert p.endswith(icg._PROMPT_TAIL)


def test_plate_positions_and_compose_models_registry():
    assert set(icg.PLATE_POSITIONS) == {"auto", "bottom_left", "bottom_right"}
    assert icg.COMPOSE_MODELS == {
        "standard": icg.MODEL_ID,
        "pro": icg.MODEL_ID_PRO,
    }
    assert icg.MODEL_ID_PRO == "fal-ai/nano-banana-pro/edit"


# ─────────────────────────────────────────────
# 5: _manifest_matches compose_options 互換
# ─────────────────────────────────────────────

_URL = "https://x/source.jpg"


def test_old_manifest_without_options_matches_defaults(tmp_path):
    """compose_options キー無しの旧 manifest = defaults (auto/standard) 扱い."""
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]})
    # 旧 cache を defaults 要求で照合 → 有効 (再課金しない)
    assert ips._manifest_matches(
        tmp_path, _URL, compose_options={"position": "auto", "model": "standard"}
    ) is True


def test_old_manifest_mismatches_non_default_options(tmp_path):
    """旧 manifest に非 default 設定 (右下固定 / pro) を要求すると cache miss."""
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]})
    assert ips._manifest_matches(
        tmp_path, _URL, compose_options={"position": "bottom_right", "model": "standard"}
    ) is False
    assert ips._manifest_matches(
        tmp_path, _URL, compose_options={"position": "auto", "model": "pro"}
    ) is False


def test_new_manifest_matches_same_options_only(tmp_path):
    """compose_options 付き manifest は同一設定でのみ match."""
    opts = {"position": "bottom_left", "model": "pro"}
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]}, compose_options=opts)
    assert ips._manifest_matches(tmp_path, _URL, compose_options=opts) is True
    assert ips._manifest_matches(
        tmp_path, _URL, compose_options={"position": "bottom_left", "model": "standard"}
    ) is False


def test_manifest_matches_without_options_arg_ignores_options(tmp_path):
    """compose_options 未指定の照合 (additional 経路) は設定差を無視 (従来挙動)."""
    opts = {"position": "bottom_right", "model": "pro"}
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]}, compose_options=opts)
    assert ips._manifest_matches(tmp_path, _URL) is True


# ─────────────────────────────────────────────
# 6: _write_manifest compose_options=None 温存
# ─────────────────────────────────────────────

def test_write_manifest_none_preserves_existing_options(tmp_path):
    """compose_options=None の再書込 (additional 単独更新) で hero 設定を消さない."""
    opts = {"position": "bottom_left", "model": "pro"}
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]}, compose_options=opts)
    # additional 更新 (None) → 既存 opts 温存
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"], "additional": ["a1.png"]})
    manifest = ips._read_manifest(tmp_path)
    assert manifest["compose_options"] == opts
    assert manifest["stage_outputs"]["additional"] == ["a1.png"]


def test_write_manifest_none_without_existing_uses_defaults(tmp_path):
    """既存 manifest 無しで compose_options=None → defaults を書く."""
    ips._write_manifest(tmp_path, _URL, {"hero": ["hero_W1.png"]})
    manifest = ips._read_manifest(tmp_path)
    assert manifest["compose_options"] == {"position": "auto", "model": "standard"}


# ─────────────────────────────────────────────
# 7: supplier 経路 reuse ガード (HIGH-1 / HIGH-3 回帰)
# ─────────────────────────────────────────────
# manifest を使わない supplier 経路は side-file (_compose_opts.json /
# _additional_urls.json) で reuse 判定する。streamlit / httpx を mock し、
# 「reuse された = session_state に旧 cache が載る」「reuse されない =
# 生成経路に進む (mock httpx 失敗で結果 0 件)」で判定する。

_CID = 990913  # テスト専用 candidate_id (実データと衝突しない大きい値)


def _setup_supplier_mocks(monkeypatch, tmp_path):
    """chdir + st/httpx mock。戻り値 = (module, fake_session_state)."""
    import tabs._supplier_photo_pipeline as spp

    monkeypatch.chdir(tmp_path)  # out_base=data/... 相対 path を tmp に隔離
    fake_state: dict = {}
    fake_st = MagicMock()
    fake_st.session_state = fake_state
    monkeypatch.setattr(spp, "st", fake_st)
    # 生成経路に入ったら必ず失敗させる (network 遮断 + 課金防止)
    fake_httpx = MagicMock()
    fake_httpx.Client.side_effect = RuntimeError("network blocked in test")
    monkeypatch.setattr(spp, "httpx", fake_httpx)
    return spp, fake_state


def _make_additional_cache(urls: list[str] | None, n_png: int) -> Path:
    """legacy/新形式の additional cache を data/hero_candidates/sup_<cid> に作る."""
    out_base = Path(f"data/hero_candidates/sup_{_CID}")
    out_base.mkdir(parents=True, exist_ok=True)
    for i in range(n_png):
        (out_base / f"_additional_{i:02d}.png").write_bytes(b"png")
    if urls is not None:
        (out_base / "_additional_urls.json").write_text(
            json.dumps(urls), encoding="utf-8"
        )
    return out_base


def test_additional_reuse_requires_url_list_match(monkeypatch, tmp_path):
    """HIGH-1: 保存済 URL list と不一致なら旧 PNG を reuse しない (誤帰属防止)."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    _make_additional_cache(["https://x/a.jpg", "https://x/b.jpg"], n_png=2)

    # picker で hero 変更 → additional 集合が変わった想定 (枚数同じ・中身違い)
    spp._do_supplier_additional_compose(
        _CID, ["https://x/c.jpg", "https://x/b.jpg"], legacy_reuse_ok=False
    )
    # reuse されず生成経路へ → mock httpx 全滅で結果 0 件 (旧 PNG が紛れ込まない)
    assert state[f"sup_additional_processed_{_CID}"] == []


def test_additional_reuse_when_url_list_matches(monkeypatch, tmp_path):
    """HIGH-1 対照: URL list 完全一致なら reuse (課金 0、httpx 不使用)."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    urls = ["https://x/a.jpg", "https://x/b.jpg"]
    out_base = _make_additional_cache(urls, n_png=2)

    spp._do_supplier_additional_compose(_CID, urls, legacy_reuse_ok=False)
    got = state[f"sup_additional_processed_{_CID}"]
    assert [r["source_url"] for r in got] == urls
    assert got[0]["path"] == str(out_base / "_additional_00.png")


def test_additional_legacy_cache_denied_when_picker_moved(monkeypatch, tmp_path):
    """HIGH-3: side-file 無し legacy cache は legacy_reuse_ok=False で reuse しない."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    _make_additional_cache(None, n_png=2)  # board#9 以前の cache (URL 記録なし)

    spp._do_supplier_additional_compose(
        _CID, ["https://x/a.jpg", "https://x/b.jpg"], legacy_reuse_ok=False
    )
    assert state[f"sup_additional_processed_{_CID}"] == []


def _make_hero_cache(opts: dict | None) -> Path:
    out_base = Path(f"data/hero_candidates/sup_{_CID}")
    out_base.mkdir(parents=True, exist_ok=True)
    for name in ("hero_W1.png", "hero_W2.png"):
        (out_base / name).write_bytes(b"png")
    (out_base / "_studio.png").write_bytes(b"png")
    if opts is not None:
        (out_base / "_compose_opts.json").write_text(
            json.dumps(opts), encoding="utf-8"
        )
    return out_base


def test_hero_legacy_cache_denied_when_picker_moved(monkeypatch, tmp_path):
    """HIGH-3: opts 無し legacy hero cache は legacy_reuse_ok=False で reuse しない."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    _make_hero_cache(None)  # board#9 以前の cache (設定記録なし)

    spp._do_supplier_hero_compose(
        _CID, "https://x/second.jpg",
        position="auto", model="standard", legacy_reuse_ok=False,
    )
    # reuse 不成立 → 生成経路の source download が mock httpx で失敗 → 候補未設定
    assert f"sup_hero_candidates_{_CID}" not in state


def test_hero_legacy_cache_reused_on_default_path(monkeypatch, tmp_path):
    """HIGH-3 対照: legacy cache + 1 枚目 picker + auto/standard なら reuse 可."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    out_base = _make_hero_cache(None)

    spp._do_supplier_hero_compose(
        _CID, "https://x/first.jpg",
        position="auto", model="standard", legacy_reuse_ok=True,
    )
    cands = state[f"sup_hero_candidates_{_CID}"]
    assert [c["path"] for c in cands] == [
        str(out_base / "hero_W1.png"), str(out_base / "hero_W2.png"),
    ]
    assert state[f"sup_hero_used_{_CID}"] == {
        "position": "auto", "model": "standard", "source_url": "https://x/first.jpg",
    }


def test_hero_opts_sidefile_mismatch_denies_reuse(monkeypatch, tmp_path):
    """新形式 side-file: 保存設定と要求設定の不一致 (position 違い) は reuse しない."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    _make_hero_cache(
        {"position": "auto", "model": "standard", "source_url": "https://x/first.jpg"}
    )

    spp._do_supplier_hero_compose(
        _CID, "https://x/first.jpg",
        position="bottom_right", model="standard", legacy_reuse_ok=True,
    )
    assert f"sup_hero_candidates_{_CID}" not in state


# ─────────────────────────────────────────────
# 8: 2巡目 HIGH-A 回帰 (失敗時 metadata 無条件書込 + stale 未掃除)
# ─────────────────────────────────────────────

class _FakeResp:
    content = b"imgbytes"

    def raise_for_status(self) -> None:
        pass


def _fake_httpx(fail_suffix: str | None = None) -> MagicMock:
    """download 成功する fake httpx。fail_suffix 終端 URL のみ失敗させられる."""

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            if fail_suffix and url.endswith(fail_suffix):
                # OSError: 実装の except (httpx.HTTPError, OSError) に捕捉される型
                raise OSError("download fail (test)")
            return _FakeResp()

    m = MagicMock()
    m.Client = _FakeClient
    return m


def _fake_photoroom_ok() -> MagicMock:
    pr = MagicMock()
    pr.success = True
    pr.image.save = lambda p: Path(p).write_bytes(b"png")
    return pr


def test_additional_sidefile_not_written_on_partial_failure(monkeypatch, tmp_path):
    """HIGH-A(c): 部分失敗時は _additional_urls.json を新 list で書かず無効化."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    out_base = _make_additional_cache(["https://x/old1.jpg", "https://x/old2.jpg"], n_png=2)
    monkeypatch.setattr(spp, "httpx", _fake_httpx(fail_suffix="fail.jpg"))

    with patch(
        "monitor.image_composer_photoroom.compose_cover_with_photoroom",
        return_value=_fake_photoroom_ok(),
    ):
        spp._do_supplier_additional_compose(
            _CID, ["https://x/a.jpg", "https://x/fail.jpg"],
            force_regenerate=True, legacy_reuse_ok=False,
        )

    got = state[f"sup_additional_processed_{_CID}"]
    assert [r["source_url"] for r in got] == ["https://x/a.jpg"]  # 1/2 のみ成功
    # 部分失敗 → side-file 無効化 (旧 list も残さない) = 次回 reuse 不成立
    assert not (out_base / "_additional_urls.json").exists()
    # 旧 hero 構成時代の stale PNG は再生成前に掃除済 (index 01 = 失敗分に旧画像が残らない)
    assert not (out_base / "_additional_01.png").exists()


def test_hero_manifest_invalidated_when_zero_candidates(monkeypatch, tmp_path):
    """HIGH-A(a): Gemini 全滅時は manifest hero=[] 化 + stale hero 掃除 (誤復元防止)."""
    out_base = tmp_path / "hero"
    out_base.mkdir()
    # 旧 (standard) run の成功 cache (additional cache 同居 = 3巡目 M3 温存検証)
    (out_base / "hero_W3.png").write_bytes(b"png")
    (out_base / "_studio.png").write_bytes(b"png")
    ips._write_manifest(
        out_base, _URL,
        {
            "hero": ["hero_W3.png"], "hero_meta": [],
            "additional": ["_additional_00.png"],
            "additional_urls": ["https://x/add0.jpg"],
        },
        compose_options={"position": "auto", "model": "standard"},
    )

    # ips.httpx は丸ごと差し替えない (except httpx.HTTPError が壊れる) — Client のみ
    monkeypatch.setattr(ips.httpx, "Client", _fake_httpx().Client)
    fake_gem = MagicMock()
    fake_gem.candidates = []  # 全滅
    with patch(
        "monitor.image_composer_photoroom.compose_cover_with_photoroom",
        return_value=_fake_photoroom_ok(),
    ), patch(
        "monitor.image_composer_gemini.generate_hero_candidates",
        return_value=fake_gem,
    ):
        cands, _ = ips.compose_hero_candidates_cached(
            _URL, out_base, position="bottom_right", model="pro",
        )

    assert cands == []
    manifest = ips._read_manifest(out_base)
    assert manifest["stage_outputs"]["hero"] == []  # 新設定で有効 cache を主張しない
    assert not (out_base / "hero_W3.png").exists()  # stale 掃除済
    # M3: additional cache は巻き添え無効化されない (Photoroom 再課金防止)
    assert manifest["stage_outputs"]["additional"] == ["_additional_00.png"]
    assert manifest["stage_outputs"]["additional_urls"] == ["https://x/add0.jpg"]
    # 次回同設定で呼んでも復元されない (hero=[] → 生成経路へ落ち download で止まる)
    monkeypatch.setattr(
        ips.httpx, "Client", _fake_httpx(fail_suffix="source.jpg").Client
    )
    cands2, _ = ips.compose_hero_candidates_cached(
        _URL, out_base, position="bottom_right", model="pro",
    )
    assert cands2 == []


def test_hero_restore_excludes_stale_plate_files(tmp_path):
    """HIGH-A(a): cache 復元は manifest list 基準 — disk 残存の stale W7 を返さない."""
    out_base = tmp_path / "hero"
    out_base.mkdir()
    for name in ("hero_W3.png", "hero_W5.png", "hero_W7.png"):  # W7 = stale (旧設定産)
        (out_base / name).write_bytes(b"png")
    (out_base / "_studio.png").write_bytes(b"png")
    opts = {"position": "auto", "model": "pro"}
    ips._write_manifest(
        out_base, _URL,
        {"hero": ["hero_W3.png", "hero_W5.png"], "hero_meta": []},
        compose_options=opts,
    )

    cands, studio = ips.compose_hero_candidates_cached(
        _URL, out_base, position="auto", model="pro",
    )
    assert [c.path.name for c in cands] == ["hero_W3.png", "hero_W5.png"]
    assert studio == out_base / "_studio.png"


def test_supplier_hero_zero_candidates_invalidates_opts(monkeypatch, tmp_path):
    """HIGH-A(b): supplier hero 0 件時は opts side-file 無効化 + stale 掃除 (Q0)."""
    spp, state = _setup_supplier_mocks(monkeypatch, tmp_path)
    out_base = _make_hero_cache(
        {"position": "auto", "model": "standard", "source_url": "https://x/first.jpg"}
    )
    monkeypatch.setattr(spp, "httpx", _fake_httpx())

    fake_gem = MagicMock()
    fake_gem.candidates = []  # 全滅
    with patch(
        "monitor.image_composer_photoroom.compose_cover_with_photoroom",
        return_value=_fake_photoroom_ok(),
    ), patch(
        "monitor.image_composer_gemini.generate_hero_candidates",
        return_value=fake_gem,
    ):
        spp._do_supplier_hero_compose(
            _CID, "https://x/first.jpg",
            position="bottom_right", model="pro", legacy_reuse_ok=False,
        )

    assert state[f"sup_hero_candidates_{_CID}"] == []
    assert not (out_base / "_compose_opts.json").exists()  # 次回 reuse 不成立
    assert not (out_base / "hero_W1.png").exists()  # stale 掃除済
