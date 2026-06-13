#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W158 (2026-05-23): 画像加工パイプライン共通 helper.

個別出品タブ Step 2.5/2.6/2.7 のロジックを **session_state 非依存の
pure 関数群** として抽出. 商品管理タブの URL 直接投入セクション +
仕入先候補 description 反映 UI からも同じ関数を呼んで使う.

提供する 4 つの主要関数:
  - compose_hero_candidates_cached: Photoroom + Gemini 3 候補 hero 合成
  - unify_additional_backgrounds_cached: Photoroom 背景グレー統一 (並列)
  - upload_to_eps_cached: eBay EPS upload (content_sha256 cache 経由)
  - resolve_final_picture_urls: ReviseItem PictureURL 確定 (cap=12 + dropped 返却)

設計書: .company/engineering/docs/2026-05-23-W158-image-pipeline-shared.md (v2.2)

Codex GPT-5.5 review fix (v2.2):
  - HIGH-Codex-2: manifest.json で source URL 不一致 cache miss を実装
  - HIGH-Codex-3: EPS cache は既存 _file_sha256 (content hash) 化済 = false alarm
  - in-flight lock は UI 層で実装 (本 module は pure)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# manifest version. cache 互換性を維持しつつ pipeline ロジック変更時に
# bump して全 cache invalidate する.
PIPELINE_VERSION = "1"
PROMPT_VERSION = "1"

MANIFEST_FILENAME = "_manifest.json"

# board#9 (2026-06-13): hero 合成オプションの default。
# 旧 manifest (compose_options キー無し) はこの値で生成された扱いにして
# cache 有効性を維持する (auto + standard = 改修前と完全同一の挙動)。
_DEFAULT_COMPOSE_OPTIONS = {"position": "auto", "model": "standard"}


# ─────────────────────────────────────────────
# Dataclasses (frozen, session 可搬)
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class HeroCandidate:
    """Gemini が生成した hero 候補 1 件."""
    plate_id: str
    score: float
    path: Path
    reasoning: str

    def to_dict(self) -> dict:
        return {
            "plate_id": self.plate_id,
            "score": self.score,
            "path": str(self.path),
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HeroCandidate":
        return cls(
            plate_id=str(d.get("plate_id") or ""),
            score=float(d.get("score") or 0.0),
            path=Path(str(d.get("path") or "")),
            reasoning=str(d.get("reasoning") or ""),
        )


@dataclass(frozen=True)
class AdditionalProcessed:
    """背景統一済の追加画像 1 件."""
    source_url: str
    path: Path

    def to_dict(self) -> dict:
        return {"source_url": self.source_url, "path": str(self.path)}

    @classmethod
    def from_dict(cls, d: dict) -> "AdditionalProcessed":
        return cls(
            source_url=str(d.get("source_url") or ""),
            path=Path(str(d.get("path") or "")),
        )


@dataclass(frozen=True)
class EpsUploadOutcome:
    """eBay EPS upload 結果. content_sha256 cache 経由."""
    success: bool
    eps_urls: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (filename, error)
    skipped_cache_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "eps_urls": list(self.eps_urls),
            "failed": [list(t) for t in self.failed],
            "skipped_cache_hits": self.skipped_cache_hits,
        }


# ─────────────────────────────────────────────
# Helpers (manifest / hashing)
# ─────────────────────────────────────────────

def _sha256_url(url: str) -> str:
    """source URL の sha256 (manifest 用)."""
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()


def _read_manifest(out_base: Path) -> Optional[dict]:
    """{out_base}/_manifest.json を読む. なければ None."""
    p = out_base / MANIFEST_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"manifest 読込失敗 ({p}): {e}")
        return None


def _write_manifest(
    out_base: Path,
    source_url: str,
    stage_outputs: dict,
    compose_options: Optional[dict] = None,
) -> None:
    """{out_base}/_manifest.json を atomic 書込.

    stage_outputs = {"hero": [...], "additional": [...]} 形.
    compose_options = {"position": ..., "model": ...} (board#9)。None なら
    既存 manifest の値を温存 (additional 単独更新で hero 設定を消さない)。
    """
    p = out_base / MANIFEST_FILENAME
    if compose_options is None:
        existing = _read_manifest(out_base) or {}
        compose_options = existing.get("compose_options") or _DEFAULT_COMPOSE_OPTIONS
    payload = {
        "source_url": source_url,
        "source_sha256": _sha256_url(source_url),
        "pipeline_version": PIPELINE_VERSION,
        "prompt_version": PROMPT_VERSION,
        "compose_options": compose_options,
        "stage_outputs": stage_outputs,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        # atomic write: tmp → rename
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning(f"manifest 書込失敗 ({p}): {e}")


def _manifest_matches(
    out_base: Path,
    source_url: str,
    compose_options: Optional[dict] = None,
) -> bool:
    """manifest があり source_url が現入力と一致するか.

    v2.2 HIGH-Codex-2: source URL 不一致時に古い cache を skip 復元する
    silent gap を防止する.

    board#9: compose_options (position/model) 指定時はそれも一致必須。
    manifest 側にキーが無い旧 cache は _DEFAULT_COMPOSE_OPTIONS 扱い
    (= auto/standard 要求なら旧 cache 有効のまま)。
    """
    manifest = _read_manifest(out_base)
    if not manifest:
        return False
    if manifest.get("source_sha256") != _sha256_url(source_url):
        return False
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        return False
    if manifest.get("prompt_version") != PROMPT_VERSION:
        return False
    if compose_options is not None:
        stored = manifest.get("compose_options") or _DEFAULT_COMPOSE_OPTIONS
        if stored != compose_options:
            return False
    return True


# ─────────────────────────────────────────────
# Public: hero 合成
# ─────────────────────────────────────────────

def compose_hero_candidates_cached(
    source_url: str,
    out_base: Path,
    *,
    force_regenerate: bool = False,
    k: int = 3,
    max_parallel: int = 3,
    download_timeout: float = 30.0,
    position: str = "auto",
    model: str = "standard",
) -> tuple[list[HeroCandidate], Optional[Path]]:
    """Photoroom (背景除去 + studio 化) + Gemini (3 候補 hero 合成) を実行.

    cache 復元条件 (v2.2 HIGH-Codex-2 修正):
      1. force_regenerate=False
      2. out_base 配下に hero_W*.png ファイルが 1 つ以上 + _studio.png 存在
      3. manifest.json の source_url が現入力と一致 + pipeline_version 一致
         + compose_options (position/model) 一致 (board#9)

    上記すべて満たせば API 呼出 skip して既存ファイルから復元.
    一つでも不一致なら API 再実行 + manifest 更新.

    Args:
        source_url: 商品画像 source URL (https://...)
        out_base: 出力ディレクトリ (例 data/hero_candidates/{sku_or_eid}/)
        force_regenerate: True で cache 無視して再生成
        k: Gemini 生成候補数 (default 3)
        max_parallel: 並列度 (default 3)
        download_timeout: source download timeout (秒)
        position: プレート配置 ("auto" / "bottom_left" / "bottom_right", board#9)
        model: 合成モデル ("standard"=nano-banana / "pro"=nano-banana-pro, board#9)

    Returns:
        (candidates, studio_path).
        candidates: list[HeroCandidate]. 失敗時 [].
        studio_path: Path or None. Photoroom 出力ファイル.

    例外: 例外を呼出元に raise しない. 失敗時は ([], None) を返す.
    """
    out_base.mkdir(parents=True, exist_ok=True)
    compose_options = {"position": position, "model": model}

    # cache 復元判定 (HIGH-Codex-2 + board#9 compose_options)
    # board#9 2巡目 HIGH-A(a): 復元は glob ではなく manifest の hero list 基準。
    # glob だと旧設定 (standard 等) で生成した stale hero_W*.png が新設定の候補に
    # 紛れ込む (設定切替 + 部分失敗で plate 構成が変わるケース)。
    studio_path = out_base / "_studio.png"
    if (not force_regenerate
            and studio_path.exists()
            and _manifest_matches(out_base, source_url, compose_options)):
        # manifest から hero 情報を復元 (plate_id / score / reasoning)
        manifest = _read_manifest(out_base) or {}
        stage = manifest.get("stage_outputs", {})
        hero_files = [f for f in stage.get("hero", []) if isinstance(f, str)]
        cached_heros_meta = {
            h.get("filename"): h
            for h in stage.get("hero_meta", [])
            if isinstance(h, dict)
        }
        candidates: list[HeroCandidate] = []
        for fname in hero_files:
            p = out_base / fname
            if not p.exists():
                continue  # manifest 記載だが実ファイル欠落 → 候補から除外
            meta = cached_heros_meta.get(p.name, {})
            candidates.append(HeroCandidate(
                plate_id=str(meta.get("plate_id") or p.stem),
                score=float(meta.get("score") or 0.0),
                path=p,
                reasoning=str(meta.get("reasoning") or "(cached, no reasoning)"),
            ))
        if candidates:
            logger.info(
                f"hero cache 復元 (manifest 一致): {len(candidates)} 候補, out_base={out_base}"
            )
            return candidates, studio_path
        # manifest 一致だが hero 実体 0 件 → cache 破損扱いで生成経路へ

    # cache miss = source download → Photoroom → Gemini
    try:
        from monitor.image_composer_photoroom import compose_cover_with_photoroom
        from monitor.image_composer_gemini import generate_hero_candidates
    except ImportError as e:
        logger.warning(f"hero compose 必要 module import 失敗: {e}")
        return [], None

    # 1. source download
    source_path = out_base / "_source.jpg"
    try:
        with httpx.Client(timeout=download_timeout, follow_redirects=True) as client:
            r = client.get(source_url)
            r.raise_for_status()
            source_path.write_bytes(r.content)
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"hero source download 失敗 ({source_url}): {e}")
        return [], None

    # 2. Photoroom studio 化
    try:
        pr = compose_cover_with_photoroom(source_path)
    except Exception as e:  # noqa: BLE001
        # Photoroom SDK 想定外例外 (BaseException 含まないよう絞る)
        logger.warning(f"Photoroom 例外: {e}")
        return [], None
    if not pr.success or pr.image is None:
        logger.warning(f"Photoroom 失敗: {pr.error}")
        return [], None
    try:
        pr.image.save(studio_path)
    except OSError as e:
        logger.warning(f"_studio.png 保存失敗: {e}")
        return [], None

    # 3. Gemini 3 候補合成
    # board#9 2巡目 HIGH-A(a): 旧設定産の stale hero_W*.png を生成直前に掃除
    # (Photoroom 成功後 = 旧 cache を消しても新規生成が確実に走る位置)。
    # 残すと部分失敗時に新 manifest + 旧ファイルの混在 cache が出来る。
    for stale in out_base.glob("hero_W*.png"):
        try:
            stale.unlink()
        except OSError as e:
            logger.warning(f"stale hero 削除失敗 ({stale.name}): {e}")
    try:
        result = generate_hero_candidates(
            studio_product_path=studio_path,
            output_dir=out_base,
            k=k,
            max_parallel=max_parallel,
            position=position,
            model=model,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Gemini 合成例外: {e}")
        return [], studio_path

    candidates: list[HeroCandidate] = []
    hero_meta_for_manifest: list[dict] = []
    for c in result.candidates:
        if c.success and c.output_path:
            cand = HeroCandidate(
                plate_id=str(c.plate_id),
                score=float(c.score),
                path=Path(c.output_path),
                reasoning=str(c.reasoning or ""),
            )
            candidates.append(cand)
            hero_meta_for_manifest.append({
                "filename": cand.path.name,
                "plate_id": cand.plate_id,
                "score": cand.score,
                "reasoning": cand.reasoning,
            })

    # manifest 更新 (HIGH-Codex-2 source URL 紐付け + board#9 compose_options)
    # board#9 2巡目 HIGH-A(a): 候補 0 件 (全滅) の時は新設定で manifest を書かない。
    # 書くと次回の _manifest_matches が「一致」になり、ディスク残存の stale を
    # 新設定の cache として復元してしまう。失敗時は manifest 自体を無効化。
    if candidates:
        _write_manifest(out_base, source_url, {
            "hero": [c.path.name for c in candidates],
            "hero_meta": hero_meta_for_manifest,
        }, compose_options=compose_options)
    else:
        # hero キーのみ空に更新 (additional cache は巻き添え無効化しない =
        # Photoroom 再課金防止)。hero=[] なので次回復元は不成立 → 必ず再生成。
        existing = _read_manifest(out_base) or {}
        stage = existing.get("stage_outputs", {}) if isinstance(existing, dict) else {}
        stage["hero"] = []
        stage["hero_meta"] = []
        _write_manifest(out_base, source_url, stage, compose_options=compose_options)
        logger.warning(
            f"hero 合成 0 候補 (全滅): hero cache 無効化, source={source_url[:60]}"
        )

    logger.info(
        f"hero 合成完了: {len(candidates)} 候補, source={source_url[:60]}"
    )
    return candidates, studio_path


# ─────────────────────────────────────────────
# Public: 追加画像背景統一
# ─────────────────────────────────────────────

def unify_additional_backgrounds_cached(
    urls: list[str],
    out_base: Path,
    *,
    force_regenerate: bool = False,
    max_workers: int = 3,
    download_timeout: float = 30.0,
) -> list[AdditionalProcessed]:
    """Photoroom で複数画像を同一背景 (#c0c0c0 + depth) に統一.

    cache 復元条件 (v2.2 HIGH-Codex-2 修正):
      1. force_regenerate=False
      2. out_base 配下に _additional_NN.png が len(urls) 枚以上存在
      3. manifest.json の stage_outputs.additional に URL 順序が一致

    Args:
        urls: 加工対象の画像 URL list (hero source 以外)
        out_base: 出力ディレクトリ
        force_regenerate: True で cache 無視
        max_workers: 並列度 (default 3)
        download_timeout: download timeout (秒)

    Returns:
        list[AdditionalProcessed]. 失敗 URL は含まれない (caller が
        len(result) < len(urls) で部分失敗判定).

    例外: 呼出元に raise しない. 失敗時は [] を返す.
    """
    if not urls:
        return []

    out_base.mkdir(parents=True, exist_ok=True)

    # cache 復元判定 (HIGH-Codex-2)
    existing_additionals = sorted(out_base.glob("_additional_*.png"))
    manifest = _read_manifest(out_base)
    cached_additional_urls = (manifest or {}).get("stage_outputs", {}).get("additional_urls", [])
    if (not force_regenerate
            and len(existing_additionals) >= len(urls)
            and cached_additional_urls == list(urls)):
        # URL 順序まで一致 → 復元
        result: list[AdditionalProcessed] = []
        for idx, url in enumerate(urls):
            target = out_base / f"_additional_{idx:02d}.png"
            if target.exists():
                result.append(AdditionalProcessed(source_url=url, path=target))
        if len(result) >= len(urls):
            logger.info(
                f"additional cache 復元 (manifest 一致): {len(result)} 枚, out_base={out_base}"
            )
            return result

    try:
        from monitor.image_composer_photoroom import compose_cover_with_photoroom
    except ImportError as e:
        logger.warning(f"unify additional 必要 module import 失敗: {e}")
        return []

    # 並列で Photoroom 実行
    def _process_one(idx_url: tuple[int, str]) -> Optional[AdditionalProcessed]:
        idx, url = idx_url
        try:
            with httpx.Client(timeout=download_timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                src_bytes = resp.content
        except (httpx.HTTPError, OSError) as e:
            logger.warning(f"additional download 失敗 ({url}): {e}")
            return None
        try:
            pr = compose_cover_with_photoroom(src_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"additional Photoroom 例外 ({url}): {e}")
            return None
        if not pr.success or pr.image is None:
            logger.warning(f"additional Photoroom 失敗 ({url}): {pr.error}")
            return None
        target = out_base / f"_additional_{idx:02d}.png"
        try:
            pr.image.save(target)
        except OSError as e:
            logger.warning(f"_additional_{idx:02d}.png 保存失敗: {e}")
            return None
        return AdditionalProcessed(source_url=url, path=target)

    results: list[AdditionalProcessed] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_one, (i, u)): i for i, u in enumerate(urls)}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)

    # 元順序保持
    order = {url: i for i, url in enumerate(urls)}
    results.sort(key=lambda r: order.get(r.source_url, 999))

    # manifest 追記 (hero 情報は維持して additional を merge)
    existing_manifest = _read_manifest(out_base) or {}
    existing_stage = existing_manifest.get("stage_outputs", {}) if isinstance(existing_manifest, dict) else {}
    existing_stage["additional"] = [r.path.name for r in results]
    existing_stage["additional_urls"] = list(urls)
    # source_url は hero 由来. manifest があれば既存 source_url を尊重、
    # なければ最初の URL (caller responsibility) を入れない (空のままにする).
    source_url_for_manifest = existing_manifest.get("source_url", "")
    if source_url_for_manifest:
        _write_manifest(out_base, source_url_for_manifest, existing_stage)
    else:
        # hero 経由 cache が無いケース (additional のみ呼出 = 異例). manifest 書かない.
        logger.debug("additional 単独実行 (hero manifest 無し), manifest skip")

    logger.info(
        f"additional 統一完了: {len(results)}/{len(urls)} 枚成功, out_base={out_base}"
    )
    return results


# ─────────────────────────────────────────────
# Public: EPS upload
# ─────────────────────────────────────────────

def upload_to_eps_cached(
    paths: list[Path],
    *,
    max_workers: int = 3,
    use_cache: bool = True,
    config: Optional[dict] = None,
) -> EpsUploadOutcome:
    """eBay EPS upload を実行. content_sha256 cache 経由 (重複 upload 回避).

    HIGH-Codex-3 補足 (v2.2): 既存 ebay_eps_uploader.upload_images_parallel が
    内部で _file_sha256 ベースの DB cache を実装済. path だけでなく content hash
    でも cache hit/miss を判定する仕様 (false alarm に該当しない既存実装).

    本 wrapper は dataclass 化 + missing path の silent drop 防止 (Q0).

    Args:
        paths: upload 対象のローカル path list. 存在しない path は missing 扱い.
        max_workers: 並列度
        use_cache: True で DB cache 確認 (content_sha256)
        config: settings.json 相当 (credentials fallback)

    Returns:
        EpsUploadOutcome. success=True なら eps_urls に成功分のみ含まれる.
    """
    if not paths:
        return EpsUploadOutcome(success=False, failed=[("(no paths)", "empty input")])

    # missing path を事前 filter (Q0 silent drop 防止 = explicit failed に追加)
    existing_paths: list[Path] = []
    missing_failed: list[tuple[str, str]] = []
    for p in paths:
        if not isinstance(p, Path):
            p = Path(p)
        if p.exists():
            existing_paths.append(p)
        else:
            missing_failed.append((p.name, "ファイルが存在しません"))

    if not existing_paths:
        return EpsUploadOutcome(
            success=False,
            failed=missing_failed,
        )

    try:
        from monitor.ebay_eps_uploader import upload_images_parallel
    except ImportError as e:
        logger.warning(f"EPS uploader import 失敗: {e}")
        return EpsUploadOutcome(
            success=False,
            failed=missing_failed + [("(import)", str(e))],
        )

    results = upload_images_parallel(
        existing_paths,
        config=config,
        max_workers=max_workers,
        use_cache=use_cache,
    )

    eps_urls: list[str] = []
    failed: list[tuple[str, str]] = list(missing_failed)
    skipped_cache_hits = 0
    for path, res in zip(existing_paths, results):
        if res.success and res.eps_url:
            eps_urls.append(res.eps_url)
            # cache hit 判定: file_hash が存在し、かつ HTTP 実行が無かった場合
            # (現実装ではこの区別は EpsUploadResult から取れないので、ログレベルで判断不能)
            # → skipped_cache_hits は使わず eps_urls の本数のみ報告
        else:
            failed.append((path.name, res.error or "unknown"))

    success = len(eps_urls) > 0 and len(failed) == len(missing_failed)
    # all_success: missing なし AND upload 失敗なし
    return EpsUploadOutcome(
        success=success,
        eps_urls=eps_urls,
        failed=failed,
        skipped_cache_hits=skipped_cache_hits,
    )


# ─────────────────────────────────────────────
# Public: final picture URLs resolve
# ─────────────────────────────────────────────

def resolve_final_picture_urls(
    *,
    processed_eps_urls: list[str],
    selected_raw_urls: list[str],
    fallback_raw_urls: list[str],
    cap: int = 12,
) -> tuple[list[str], list[str]]:
    """ReviseItem PictureURL を確定する pure 関数.

    優先順位: processed > selected > fallback (append 順保持、dedupe、non-https 除外).
    cap (default 12, ReviseItem 仕様上限) で kept、それ以降は dropped.

    v2.2 HIGH-Codex-3 補足: cap=12 は ReviseItem 上限.
    AddFixedPriceItem は 24 件まで許容するが本関数は ReviseItem 向け.

    Returns:
        (kept, dropped). kept は cap 件まで, dropped は cap 超過分.
        UI は len(dropped) > 0 時に必須 warning 表示 (Q0).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for src_list in (processed_eps_urls, selected_raw_urls, fallback_raw_urls):
        for url in src_list:
            if not url or not isinstance(url, str):
                continue
            if not url.startswith("https://"):
                continue  # eBay は https のみ受付
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
    kept = ordered[:cap]
    dropped = ordered[cap:]
    return kept, dropped


__all__ = [
    "HeroCandidate",
    "AdditionalProcessed",
    "EpsUploadOutcome",
    "PIPELINE_VERSION",
    "PROMPT_VERSION",
    "compose_hero_candidates_cached",
    "unify_additional_backgrounds_cached",
    "upload_to_eps_cached",
    "resolve_final_picture_urls",
]
