"""W158 (2026-05-23): image_pipeline_shared pure 関数の unit test.

Photoroom / Gemini / EPS upload の HTTP 経路は mock し、cache 復元 (manifest)
を中心に振る舞いを検証.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from monitor import image_pipeline_shared as ips


# ─────────────────────────────────────────────
# resolve_final_picture_urls (pure, mock 不要)
# ─────────────────────────────────────────────

def test_resolve_final_picture_urls_priority_order():
    """processed > selected > fallback の優先順位で append."""
    kept, dropped = ips.resolve_final_picture_urls(
        processed_eps_urls=["https://eps/a.jpg", "https://eps/b.jpg"],
        selected_raw_urls=["https://raw/x.jpg"],
        fallback_raw_urls=["https://fallback/y.jpg"],
        cap=12,
    )
    assert kept == [
        "https://eps/a.jpg", "https://eps/b.jpg",
        "https://raw/x.jpg", "https://fallback/y.jpg",
    ]
    assert dropped == []


def test_resolve_final_picture_urls_dedupe_across_lists():
    """同一 URL が processed と selected 両方にあるとき processed 側で 1 度のみ."""
    kept, dropped = ips.resolve_final_picture_urls(
        processed_eps_urls=["https://x/p.jpg", "https://x/q.jpg"],
        selected_raw_urls=["https://x/p.jpg", "https://x/r.jpg"],  # p は重複
        fallback_raw_urls=[],
        cap=12,
    )
    assert kept == ["https://x/p.jpg", "https://x/q.jpg", "https://x/r.jpg"]
    assert dropped == []


def test_resolve_final_picture_urls_non_https_filtered():
    """non-https は除外 (eBay 仕様)."""
    kept, dropped = ips.resolve_final_picture_urls(
        processed_eps_urls=["http://insecure.com/a.jpg", "https://ok.com/b.jpg"],
        selected_raw_urls=["ftp://bad/c.jpg"],
        fallback_raw_urls=[""],
        cap=12,
    )
    assert kept == ["https://ok.com/b.jpg"]
    assert dropped == []


def test_resolve_final_picture_urls_cap_returns_dropped():
    """v2.2 HIGH-Codex-3 ↔ HIGH-3 fix: 13+ 件 input で dropped が返り silent drop しない."""
    urls = [f"https://eps/{i:02d}.jpg" for i in range(15)]
    kept, dropped = ips.resolve_final_picture_urls(
        processed_eps_urls=urls,
        selected_raw_urls=[], fallback_raw_urls=[], cap=12,
    )
    assert len(kept) == 12
    assert len(dropped) == 3
    assert dropped == urls[12:]


def test_resolve_final_picture_urls_cap_custom_value():
    """cap 引数で件数制御可能."""
    urls = [f"https://x/{i}.jpg" for i in range(10)]
    kept, dropped = ips.resolve_final_picture_urls(
        processed_eps_urls=urls,
        selected_raw_urls=[], fallback_raw_urls=[], cap=5,
    )
    assert len(kept) == 5
    assert len(dropped) == 5


# ─────────────────────────────────────────────
# manifest helpers
# ─────────────────────────────────────────────

def test_manifest_matches_returns_false_on_missing(tmp_path):
    """manifest 不在 → False."""
    assert ips._manifest_matches(tmp_path, "https://x/source.jpg") is False


def test_manifest_matches_returns_false_on_url_mismatch(tmp_path):
    """v2.2 HIGH-Codex-2: source URL 不一致 → False (silent gap 防止)."""
    ips._write_manifest(tmp_path, "https://x/old.jpg", {"hero": ["hero_W1.png"]})
    assert ips._manifest_matches(tmp_path, "https://x/new.jpg") is False


def test_manifest_matches_returns_true_on_url_match(tmp_path):
    """source URL 完全一致 → True (cache 復元 OK)."""
    ips._write_manifest(tmp_path, "https://x/same.jpg", {"hero": ["hero_W1.png"]})
    assert ips._manifest_matches(tmp_path, "https://x/same.jpg") is True


def test_manifest_matches_returns_false_on_pipeline_version_mismatch(tmp_path):
    """pipeline_version 不一致 (将来 bump 時) → cache miss."""
    # 古い pipeline_version を含む manifest を直接書く
    p = tmp_path / ips.MANIFEST_FILENAME
    payload = {
        "source_url": "https://x/y.jpg",
        "source_sha256": ips._sha256_url("https://x/y.jpg"),
        "pipeline_version": "0",  # 現在は "1", 不一致
        "prompt_version": ips.PROMPT_VERSION,
        "stage_outputs": {},
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert ips._manifest_matches(tmp_path, "https://x/y.jpg") is False


def test_write_manifest_atomic_tmp_rename(tmp_path):
    """atomic 書込: tmp → rename で旧ファイル消失しない."""
    ips._write_manifest(tmp_path, "https://x/a.jpg", {"hero": ["h1.png"]})
    p = tmp_path / ips.MANIFEST_FILENAME
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["source_url"] == "https://x/a.jpg"
    assert data["pipeline_version"] == "1"
    # tmp 残ってない
    assert not (tmp_path / (ips.MANIFEST_FILENAME + ".tmp")).exists()


# ─────────────────────────────────────────────
# compose_hero_candidates_cached (mock 経由)
# ─────────────────────────────────────────────

def test_compose_hero_cache_restore_with_matching_manifest(tmp_path, monkeypatch):
    """v2.2 HIGH-Codex-2: manifest 一致時に既存ファイル復元、API skip."""
    # 既存 hero ファイル + studio + manifest を準備
    (tmp_path / "hero_W1.png").write_bytes(b"fake")
    (tmp_path / "hero_W2.png").write_bytes(b"fake")
    (tmp_path / "_studio.png").write_bytes(b"fake")
    ips._write_manifest(tmp_path, "https://x/src.jpg", {
        "hero": ["hero_W1.png", "hero_W2.png"],
        "hero_meta": [
            {"filename": "hero_W1.png", "plate_id": "W1", "score": 0.9, "reasoning": "..."},
            {"filename": "hero_W2.png", "plate_id": "W2", "score": 0.7, "reasoning": "..."},
        ],
    })

    # Photoroom / Gemini が呼ばれないことを保証
    monkeypatch.setattr(
        "monitor.image_composer_photoroom.compose_cover_with_photoroom",
        mock.Mock(side_effect=AssertionError("API called despite cache hit")),
        raising=False,
    )

    candidates, studio = ips.compose_hero_candidates_cached(
        "https://x/src.jpg", tmp_path,
    )
    assert len(candidates) == 2
    assert candidates[0].plate_id == "W1"
    assert candidates[0].score == 0.9
    assert studio == tmp_path / "_studio.png"


def test_compose_hero_cache_miss_on_source_url_change(tmp_path, monkeypatch):
    """v2.2 HIGH-Codex-2: source URL 違うと cache 復元しない (silent gap 防止).

    本 test は cache miss 経路に入ることのみ確認 (API は mock で抑止).
    """
    # 古い source URL の manifest + ファイル
    (tmp_path / "hero_W1.png").write_bytes(b"fake_old")
    (tmp_path / "_studio.png").write_bytes(b"fake_old")
    ips._write_manifest(tmp_path, "https://x/OLD.jpg", {"hero": ["hero_W1.png"]})

    # API を mock で「呼ばれた」記録
    photoroom_called = []
    monkeypatch.setattr(
        "monitor.image_composer_photoroom.compose_cover_with_photoroom",
        lambda *a, **kw: photoroom_called.append(True) or mock.Mock(success=False, error="mocked"),
        raising=False,
    )
    # source download も mock
    monkeypatch.setattr(
        "httpx.Client",
        mock.Mock(side_effect=lambda *a, **kw: mock.MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda *a: False,
            get=lambda *a, **kw: mock.MagicMock(
                content=b"", raise_for_status=lambda: None,
            ),
        )),
    )

    ips.compose_hero_candidates_cached("https://x/NEW.jpg", tmp_path)
    # photoroom が呼ばれた = cache miss が正しく動作
    assert len(photoroom_called) >= 1


# ─────────────────────────────────────────────
# upload_to_eps_cached
# ─────────────────────────────────────────────

def test_upload_to_eps_empty_input_returns_failure():
    """空 paths は success=False で failed に explicit message."""
    out = ips.upload_to_eps_cached([])
    assert out.success is False
    assert len(out.failed) == 1
    assert "empty input" in out.failed[0][1]


def test_upload_to_eps_missing_paths_explicit_failed(tmp_path, monkeypatch):
    """v2.2: 存在しない path は missing 扱いで failed に追加 (silent drop 防止 Q0)."""
    existing = tmp_path / "real.png"
    existing.write_bytes(b"fake png")
    missing = tmp_path / "ghost.png"  # 存在しない

    # upload_images_parallel を mock (real のみ success)
    from monitor.ebay_eps_uploader import EpsUploadResult
    monkeypatch.setattr(
        "monitor.ebay_eps_uploader.upload_images_parallel",
        lambda paths, **kw: [
            EpsUploadResult(success=True, eps_url=f"https://eps/{p.name}")
            for p in paths
        ],
    )

    out = ips.upload_to_eps_cached([existing, missing])
    # missing は failed に
    assert any(f[0] == "ghost.png" for f in out.failed)
    # real は eps_urls に
    assert any("real.png" in url for url in out.eps_urls)


def test_upload_to_eps_all_failed(tmp_path, monkeypatch):
    """全 upload 失敗 → success=False, eps_urls=[]."""
    p1 = tmp_path / "a.png"
    p1.write_bytes(b"fake")

    from monitor.ebay_eps_uploader import EpsUploadResult
    monkeypatch.setattr(
        "monitor.ebay_eps_uploader.upload_images_parallel",
        lambda paths, **kw: [EpsUploadResult(success=False, error="mock fail")],
    )

    out = ips.upload_to_eps_cached([p1])
    assert out.success is False
    assert out.eps_urls == []
    assert any("a.png" in f[0] for f in out.failed)


# ─────────────────────────────────────────────
# Dataclass serialization
# ─────────────────────────────────────────────

def test_hero_candidate_to_dict_roundtrip():
    c = ips.HeroCandidate(
        plate_id="W1", score=0.9, path=Path("/tmp/h.png"), reasoning="r",
    )
    d = c.to_dict()
    c2 = ips.HeroCandidate.from_dict(d)
    assert c2.plate_id == "W1"
    assert c2.score == 0.9
    assert c2.path == Path("/tmp/h.png")


def test_additional_processed_to_dict_roundtrip():
    a = ips.AdditionalProcessed(
        source_url="https://x/y.jpg", path=Path("/tmp/a.png"),
    )
    d = a.to_dict()
    a2 = ips.AdditionalProcessed.from_dict(d)
    assert a2.source_url == "https://x/y.jpg"
    assert a2.path == Path("/tmp/a.png")
