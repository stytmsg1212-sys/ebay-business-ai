"""W115: 仕入先候補「採用」flow に写真反映機能統合 (案 A 別 button).

設計:
  - 採用 button (既存 app.py:2023): SKU 反映のみ (W112 1-click 維持).
  - 写真反映 button (新): 案 A により独立 button. 採用済 listing の写真を仕入先画像から
    Photoroom + Gemini compose で更新する.
  - K1 撤回適用: 個別出品タブ (`tab_individual_listing.py`) の hero compose は touch せず
    本 module に supplier 専用 helper を copy-paste で実装 (regression risk 排除).
  - H-3 silent skip 完全回避: 採用と写真反映を別 button 化したため、写真反映失敗時も
    DB 状態は採用済のまま、UI が status 表示で見える形.

Phase 3 Clarify 採用:
  - Q1 (a) auto scrape candidate_url (本 module 内で og:image 抽出)
  - Q2 (a) 復活候補のみ (caller 側で status='accepted' 判定済の前提)
  - Q3 (b) 写真失敗時も採用続行 + warning → 案 A で写真反映と採用が分離されたため、
    本 module の責務は写真反映のみ. 採用は既存 flow.
  - Q4 (a) 3 候補 inline radio 表示 (st.dialog 不採用、案 A K1 simple)

session_state prefix = 'sup_' (個別出品タブ '_il_' と分離、key 衝突回避).

H-6 Phase D verify (実機 user 在席で 1 件試行する DoD 11 step 抜粋):
  Phase 0 (前提): scheduler PID alive + Streamlit 8501 HTTP 200 + supplier_candidates に
    status='accepted' の 1 件を準備.
  Phase 1 (UI): MonoDeck 仕入先候補タブで「写真反映」 button 押下 → og:image 抽出成功 →
    Photoroom + Gemini で 3 候補表示 → 1 候補選択 → 「eBay に反映」 button 押下.
  Phase 2 (DB): api_call_log に新規 row 1 件 (operation='ebay_revise_item' 等)、
    eps_upload_cache に新規 row 1 件、is_batch=0 確認.
  Phase 3 (eBay): GetItem API で対象 ItemID の PictureDetails.PictureURL[0] が
    新 EPS URL (https://i.ebayimg.com/...) に更新されたことを実機検証.
  Phase 4 (回帰): 個別出品タブで通常出品 1 件、tab_individual_listing.py の hero compose
    が壊れていないことを確認 (本 module は copy-paste で独立、影響無を verify).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
import streamlit as st

logger = logging.getLogger(__name__)

# 個別出品タブと session_state を完全分離 (K2 surgical: 既存 flow 影響なし)
_SS = "sup_"


# ==========================================================================
# H-2: og:image 抽出 (auto scrape candidate_url)
# ==========================================================================

def fetch_supplier_image_url(candidate_url: str, timeout: float = 15.0) -> Optional[str]:
    """仕入先 URL (Mercari/Yahoo/PayPay 等) から商品画像 URL を og:image meta で抽出.

    Returns:
        画像 URL (https://) or None (取得失敗 / og:image 無し).

    エラー時は None を返して silent ではなく logger.warning で痕跡 (Q0 silent skip prevention).
    """
    if not candidate_url or not candidate_url.startswith(("http://", "https://")):
        logger.warning(f"fetch_supplier_image_url: invalid URL {candidate_url!r}")
        return None

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(
                candidate_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            r.raise_for_status()
    except Exception as e:
        logger.warning(f"fetch_supplier_image_url: HTTP failed {candidate_url}: {e}")
        return None

    html = r.text
    # og:image meta tag を抽出 (順序入れ替え robust に対応)
    import re
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            url = m.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith(("http://", "https://")):
                # 相対 URL は origin 補完
                from urllib.parse import urljoin
                url = urljoin(candidate_url, url)
            return url

    logger.warning(f"fetch_supplier_image_url: og:image not found in {candidate_url}")
    return None


def fetch_supplier_images_all(candidate_url: str, timeout: float = 15.0) -> list[str]:
    """W115 v2 (2026-05-10): 仕入先 URL から **全画像 URL** を取得 (max 10 枚).

    個別出品と同様の multi-image upload を実現するため、`scrape_supplier_url`
    (Yahoo/Mercari/PayPay 各 platform 別 scraper) を経由して全画像取得.

    Returns:
        画像 URL のリスト (最大 10 枚、index 0 が hero 候補). 取得失敗時 [].

    Fallback: scrape 失敗時 og:image (1 件) で best-effort.
    Q0 silent skip prevention: 失敗は logger.warning で痕跡保存.
    """
    if not candidate_url or not candidate_url.startswith(("http://", "https://")):
        logger.warning(f"fetch_supplier_images_all: invalid URL {candidate_url!r}")
        return []

    try:
        from monitor.supplier_scraper import scrape_supplier_url
        product = scrape_supplier_url(candidate_url, timeout_sec=int(timeout))
        if product.image_urls:
            return list(product.image_urls)
        if product.scrape_error:
            logger.warning(
                f"scrape_supplier_url failed for {candidate_url}: {product.scrape_error}"
            )
    except Exception as e:
        logger.warning(f"scrape_supplier_url exception for {candidate_url}: {e}")

    # fallback: og:image (legacy 1-image path)
    fallback = fetch_supplier_image_url(candidate_url, timeout=timeout)
    return [fallback] if fallback else []


# ==========================================================================
# Photoroom + Gemini hero compose (supplier 専用 copy / adapt)
# ==========================================================================

def _do_supplier_hero_compose(
    candidate_id: int, source_url: str, force_regenerate: bool = False,
    position: str = "auto", model: str = "standard",
    legacy_reuse_ok: bool = True,
) -> None:
    """Photoroom + Gemini で hero 候補 3 枚を生成. session_state 経由で UI に反映.

    既存合成結果 (data/hero_candidates/sup_<cid>/hero_W*.png) があれば force_regenerate=
    False で API skip. 課金保護.

    board#9 (2026-06-13): position/model 対応。manifest が無い経路のため、前回実行時の
    設定を out_base/_compose_opts.json に保存し、設定一致時のみ reuse する。legacy
    (opts ファイル無し) は legacy_reuse_ok=True (= picker が default の 1 枚目) かつ
    auto/standard 指定時のみ一致扱い (旧 cache は常に 1 枚目から生成されているため、
    picker で別画像を選んだ時に旧結果を silent 返却しない / reviewer HIGH-3)。
    """
    out_base = Path(f"data/hero_candidates/sup_{candidate_id}")
    out_base.mkdir(parents=True, exist_ok=True)

    sk_cands = f"{_SS}hero_candidates_{candidate_id}"
    sk_studio = f"{_SS}hero_studio_path_{candidate_id}"
    sk_source = f"{_SS}hero_source_url_{candidate_id}"
    sk_picked = f"{_SS}hero_selected_path_{candidate_id}"
    sk_used = f"{_SS}hero_used_{candidate_id}"

    opts = {"position": position, "model": model, "source_url": source_url}
    opts_path = out_base / "_compose_opts.json"

    # 既存合成結果 reuse (board#9: 設定一致時のみ)
    existing_heros = sorted(out_base.glob("hero_W*.png"))
    existing_studio = out_base / "_studio.png"
    if opts_path.exists():
        try:
            stored_opts = json.loads(opts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"_compose_opts.json 読込失敗 (sup_{candidate_id}): {e}")
            stored_opts = None
        opts_match = stored_opts == opts
    else:
        # legacy cache (board#9 以前 = 常に 1 枚目から生成): picker が default の
        # 1 枚目 (legacy_reuse_ok=True) かつ auto/standard の時のみ reuse 可
        opts_match = legacy_reuse_ok and position == "auto" and model == "standard"
    if not force_regenerate and existing_heros and existing_studio.exists() and opts_match:
        candidates = [{"name": p.stem, "path": str(p)} for p in existing_heros]
        st.session_state[sk_cands] = candidates
        st.session_state[sk_source] = source_url
        st.session_state[sk_studio] = str(existing_studio)
        st.session_state[sk_used] = opts
        st.success(
            f"既存合成結果 {len(candidates)} 候補を再使用 (API 課金 0)。"
            "再生成するには「再生成」ボタンを使用してください。"
        )
        return

    try:
        from monitor.image_composer_photoroom import compose_cover_with_photoroom
        from monitor.image_composer_gemini import generate_hero_candidates
    except Exception as e:
        st.error(f"モジュール import 失敗: {e}")
        return

    # 1. source download
    # expanded=True: 失敗時の st.error が折りたたみヘッダ裏に隠れる Q0 不可視化を防ぐ
    source_path = out_base / "_source.jpg"
    try:
        with st.status("画像をダウンロード中...", expanded=True) as _s:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                r = c.get(source_url)
                r.raise_for_status()
                source_path.write_bytes(r.content)
            _s.update(label=f"ダウンロード完了 ({len(r.content) // 1024} KB)", state="complete")
    except Exception as e:
        logger.warning(f"supplier hero source download 失敗 (sup_{candidate_id}): {e}")
        st.error(f"画像ダウンロード失敗: {e}")
        return

    # 2. Photoroom
    try:
        with st.status("Photoroom で studio 化中 (約 10 秒)...", expanded=True) as _s:
            pr = compose_cover_with_photoroom(source_path)
            if not pr.success:
                # Q0: state="error" でヘッダを赤表示にし、ログにも痕跡を残す
                # (Photoroom 402=プラン枯渇等の失敗が無痕跡で消えるのを防ぐ)。
                # 確立パターン (tab_individual_listing 2026-06-18) に合わせ、実原因
                # (残高/枚数枯渇が最多) を actionable に案内する。
                _s.update(label="Photoroom 失敗", state="error")
                logger.warning(f"supplier hero Photoroom 失敗 (sup_{candidate_id}): {pr.error}")
                st.error(
                    f"Photoroom 失敗: {pr.error}\n\n"
                    "考えられる原因:\n"
                    "1. Photoroom プランの枚数/残高切れ (402 'exhausted' が頻出原因) — "
                    "Photoroom dashboard でプランを確認/更新してください\n"
                    "2. PHOTOROOM_API_KEY が未設定 or 無効"
                )
                return
            studio_path = out_base / "_studio.png"
            pr.image.save(studio_path)
            st.session_state[sk_studio] = str(studio_path)
            _s.update(label="Photoroom 完了", state="complete")
    except Exception as e:
        logger.warning(f"supplier hero Photoroom 例外 (sup_{candidate_id}): {e}")
        st.error(f"Photoroom 例外: {e}")
        return

    # 3. Gemini 3 候補
    # board#9 2巡目 HIGH-A(b): 旧設定産の stale hero_W*.png を生成直前に掃除
    # (残すと失敗時に旧ファイル + 新 opts side-file の混在 cache が出来る)
    # 3巡目 M1: 旧 opts side-file も同時に無効化 — 生成成功後の opts 書込が
    # OSError で失敗しても「新設定産 hero + 旧設定 opts」の不整合 cache を残さない
    for stale in out_base.glob("hero_W*.png"):
        try:
            stale.unlink()
        except OSError as e:
            logger.warning(f"stale hero 削除失敗 (sup_{candidate_id}, {stale.name}): {e}")
    try:
        opts_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"旧 _compose_opts.json 削除失敗 (sup_{candidate_id}): {e}")
    try:
        with st.status("Gemini でプレート合成中 (約 30-40 秒, 3 候補並列)...", expanded=True) as _s:
            result = generate_hero_candidates(
                studio_product_path=studio_path,
                output_dir=out_base,
                k=3,
                max_parallel=3,
                position=position,
                model=model,
            )
            cands = []
            for c in result.candidates:
                if c.success and c.output_path:
                    cands.append({
                        "plate_id": c.plate_id,
                        "score": float(c.score),
                        "path": str(c.output_path),
                        "reasoning": c.reasoning,
                    })
            st.session_state[sk_cands] = cands
            st.session_state[sk_source] = source_url
            st.session_state[sk_picked] = None
            st.session_state[sk_used] = opts
            # board#9 2巡目 HIGH-A(b): side-file は成功 (候補 1 件以上) 時のみ書込。
            # 0 件で書くと次回 reuse 判定が「一致」になり stale を誤復元する。
            # 0 件は state="error" で正直に表示 (Q0 偽装成功防止)。
            if cands:
                _s.update(label=f"完了: {len(cands)} 候補生成", state="complete")
                try:
                    opts_path.write_text(
                        json.dumps(opts, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError as e:
                    logger.warning(f"_compose_opts.json 書込失敗 (sup_{candidate_id}): {e}")
            else:
                _s.update(label="合成失敗: 候補 0 件 (再生成してください)", state="error")
                try:
                    opts_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(f"_compose_opts.json 無効化失敗 (sup_{candidate_id}): {e}")
                logger.warning(f"supplier hero 合成 0 候補 (sup_{candidate_id})")
    except Exception as e:
        logger.warning(f"supplier hero Gemini 合成例外 (sup_{candidate_id}): {e}")
        st.error(f"Gemini 合成例外: {e}")
        return


# ==========================================================================
# W115 v2 (2026-05-10): hero 以外の画像を Photoroom で背景抜き処理
# ==========================================================================

def _do_supplier_additional_compose(
    candidate_id: int, additional_urls: list[str], force_regenerate: bool = False,
    legacy_reuse_ok: bool = True,
) -> None:
    """hero 以外の supplier 画像を Photoroom で背景抜き (Gemini なし).

    個別出品 `_do_process_additional_images` の supplier 版 (copy-paste、K2 surgical で
    既存 individual listing flow に touch せず).

    Cost protection: 既存 _additional_NN.png があれば API skip.
    Photoroom $0.02/枚 × N の課金抑制 (リトライ時 0).

    Returns: None (session_state[f'sup_additional_processed_{cid}'] に list[dict] 格納).
        各 dict: {'source_url': str, 'path': str (local PNG)}.

    Q0 silent skip prevention: 各 URL 失敗は logger.warning で痕跡保存、partial 結果返す.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not additional_urls:
        return

    out_base = Path(f"data/hero_candidates/sup_{candidate_id}")
    out_base.mkdir(parents=True, exist_ok=True)

    sk_proc = f"{_SS}additional_processed_{candidate_id}"

    # board#9 reviewer HIGH-1: _additional_NN.png は index ↔ URL の盲目ペアリングの
    # ため、生成時の URL list を side-file に保存し、完全一致時のみ reuse する。
    # picker で hero 変更 → additional 集合が変わった時に旧 PNG を誤帰属させない。
    add_urls_path = out_base / "_additional_urls.json"
    if add_urls_path.exists():
        try:
            stored_urls = json.loads(add_urls_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"_additional_urls.json 読込失敗 (sup_{candidate_id}): {e}")
            stored_urls = None
        urls_match = stored_urls == list(additional_urls)
    else:
        # legacy cache (side-file 無し = board#9 以前、常に all_urls[1:12] から生成):
        # picker が default (legacy_reuse_ok=True) なら集合構成は同一 = reuse 可
        urls_match = legacy_reuse_ok

    # Cost protection: 既存 _additional_*.png があり URL list 一致なら API skip
    existing = sorted(out_base.glob("_additional_*.png"))
    if (
        not force_regenerate
        and len(existing) >= len(additional_urls)
        and urls_match
    ):
        results = []
        for idx, url in enumerate(additional_urls):
            target = out_base / f"_additional_{idx:02d}.png"
            if target.exists():
                results.append({"source_url": url, "path": str(target)})
        if len(results) >= len(additional_urls):
            st.session_state[sk_proc] = results
            st.success(
                f"既存の追加画像 {len(results)} 枚を再使用 (Photoroom 課金 0)。"
            )
            return

    try:
        from monitor.image_composer_photoroom import compose_cover_with_photoroom
    except Exception as e:
        # H2 fix (2026-05-10 retrospective code-reviewer): import 失敗時も sk_proc=[] を設定.
        # auto-trigger 経路 (render_supplier_photo_apply_section) は
        # `additional_processed is None` で再実行判定するため、未設定のまま return すると
        # 無限 rerun ループ (Streamlit がブラウザ操作不能) になる.
        st.error(f"Photoroom モジュール import 失敗: {e}")
        st.session_state[sk_proc] = []
        return

    # board#9 2巡目 HIGH-A(c): 再生成前に旧 _additional_*.png を全削除。
    # 残すと部分失敗 index に旧 hero 構成時の別画像が残存し、新 URL list の
    # side-file と組み合わさって誤帰属 reuse → eBay へ誤画像アップロードに至る。
    for stale in existing:
        try:
            stale.unlink()
        except OSError as e:
            logger.warning(f"stale additional 削除失敗 (sup_{candidate_id}, {stale.name}): {e}")

    def _process_one(idx_url: tuple[int, str]) -> Optional[dict]:
        idx, url = idx_url
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                src_bytes = c.get(url).content
            r = compose_cover_with_photoroom(src_bytes)
            if not r.success or r.image is None:
                logger.warning(
                    f"additional photoroom failed (no image) for {url}: {r.error}"
                )
                return None
            out_path = out_base / f"_additional_{idx:02d}.png"
            r.image.save(out_path)
            return {"source_url": url, "path": str(out_path)}
        except Exception as e:
            logger.warning(f"additional photoroom exception for {url}: {e}")
            return None

    results: list[dict] = []
    with st.status(
        f"{len(additional_urls)} 枚を Photoroom で並列処理中 (~{len(additional_urls) * 5}秒)...",
        expanded=False,
    ) as _s:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_process_one, (i, u)): i
                for i, u in enumerate(additional_urls)
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                res = fut.result()
                if res:
                    results.append(res)
                _s.update(label=f"処理中 {done}/{len(additional_urls)}...")
        # 元順序を保持 (eBay の表示順 = upload 順)
        order = {u: i for i, u in enumerate(additional_urls)}
        results.sort(key=lambda r: order.get(r["source_url"], 999))
        _s.update(
            label=f"完了: {len(results)}/{len(additional_urls)} 枚処理成功",
            state="complete",
        )

    st.session_state[sk_proc] = results
    # board#9 reviewer HIGH-1: 生成時 URL list を永続化 (次回 reuse の照合元)。
    # 2巡目 HIGH-A(c): 完全成功時のみ書込。部分/全失敗で新 list を書くと
    # 欠落 index が次回 reuse 判定をすり抜けて誤帰属する。失敗時は旧 list も無効化。
    if len(results) == len(additional_urls):
        try:
            add_urls_path.write_text(
                json.dumps(list(additional_urls), ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"_additional_urls.json 書込失敗 (sup_{candidate_id}): {e}")
    else:
        try:
            add_urls_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"_additional_urls.json 無効化失敗 (sup_{candidate_id}): {e}")
        logger.warning(
            f"additional 部分失敗 (sup_{candidate_id}): "
            f"{len(results)}/{len(additional_urls)} 成功, side-file 無効化"
        )


# ==========================================================================
# H-1 連携 + H-4 EPS orphan handling
# ==========================================================================

def _upload_eps_and_revise(
    candidate_id: int,
    ebay_item_id: str,
    hero_local_path: str,
    additional_paths: Optional[list[str]] = None,
) -> dict:
    """W115 H-1+H-4 + v2 (2026-05-10): EPS upload → revise_item_pictures で eBay 反映.

    v2: hero に加えて additional 画像 (背景抜きのみ、Photoroom 処理済) も同時 upload.
    eBay PictureDetails 上限 12 枚 (`revise_item_pictures` で hard cap).

    Args:
        hero_local_path: Photoroom + Gemini で合成した hero 画像 (1 枚目 = Gallery).
        additional_paths: hero 以外の Photoroom 処理済画像 list. None or [] なら hero のみ.

    Returns:
        {'success': bool, 'message': str, 'eps_url': str | None}.
        eps_url は hero の EPS URL (caller/UI で表示用).

    H-4 (EPS upload 成功 + ReviseItem 失敗時 orphan):
      - EPS は upload_images_parallel(use_cache=True) の DB cache (eps_upload_cache) に
        永続記録される (file hash → EPS URL のマップ).
      - ReviseItem 失敗時、EPS storage は eBay 側に残るが DB cache のおかげで再試行コスト 0.
      - 「課金消費 + listing 未更新」状態は本関数戻り値 success=False で caller に明示surface.

    Partial-success: additional N/M 枚が EPS upload 失敗しても、成功分 + hero で
    ReviseItem 続行 (個別出品 _upload_processed_to_eps_sync L919-925 と同パターン).
    Q0 silent skip prevention: 失敗枚数は message に明示 surface.
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_eps_uploader import upload_images_parallel
    from monitor.ebay_client import revise_item_pictures

    creds = get_ebay_credentials()
    if not ebay_credentials_ok(creds):
        return {
            'success': False,
            'message': 'eBay credentials not configured (env var 設定 + OAuth 完了確認)',
            'eps_url': None,
        }

    # Step 1: 全画像 list 構築 (hero index=0 を保証、eBay max 12 で truncate)
    all_paths: list[Path] = [Path(hero_local_path)]
    if additional_paths:
        for p in additional_paths:
            all_paths.append(Path(p))
    if len(all_paths) > 12:
        # eBay 制約 silent drop 防止 (Q0). caller への truncate 通知は message に含める.
        truncated_count = len(all_paths) - 12
        all_paths = all_paths[:12]
    else:
        truncated_count = 0

    # Step 2: EPS upload (use_cache=True で eps_upload_cache に永続記録、再試行で課金 0).
    try:
        eps_results = upload_images_parallel(
            all_paths,
            use_cache=True,
            max_workers=3,
        )
    except Exception as e:
        return {
            'success': False,
            'message': f'EPS upload exception: {type(e).__name__}: {e}',
            'eps_url': None,
        }

    # H3 fix (2026-05-10 retrospective code-reviewer): len mismatch を Q0 surface.
    # upload_images_parallel が all_paths と異なる長さ list を返した場合、silent に
    # additional 数を miscount (zip で短い側に切捨) し「反映成功 N 枚」が嘘になる.
    if len(eps_results) != len(all_paths):
        return {
            'success': False,
            'message': (
                f'EPS results count mismatch: got {len(eps_results)} but expected '
                f'{len(all_paths)}. upload_images_parallel internal bug の可能性、'
                f'再試行前に upstream を確認してください.'
            ),
            'eps_url': None,
        }

    # Step 3: 結果集計 (hero 必須、additional は partial OK)
    if not eps_results or not eps_results[0].success:
        # hero 失敗 → abort (additional だけでは Gallery hero が存在しない)
        return {
            'success': False,
            'message': (
                f'hero EPS upload failed: '
                f'{eps_results[0].error if eps_results else "no result"}'
            ),
            'eps_url': None,
        }

    hero_url = eps_results[0].eps_url
    eps_urls: list[str] = [hero_url]
    failed_additional: list[str] = []
    for r, p in zip(eps_results[1:], all_paths[1:]):
        if r.success and r.eps_url:
            eps_urls.append(r.eps_url)
        else:
            failed_additional.append(f"{p.name}: {r.error or 'unknown'}")

    # Step 4: ReviseItem PictureDetails (W115 H-1)
    revise_result = revise_item_pictures(
        item_id=ebay_item_id,
        picture_urls=eps_urls,
        app_id=creds['app_id'],
        dev_id=creds['dev_id'],
        cert_id=creds['cert_id'],
        user_token=creds['user_token'],
    )

    if not revise_result['success']:
        # H-4: EPS は upload 済 ($0.02 × N + DB cache 残存)、eBay listing は未更新.
        return {
            'success': False,
            'message': (
                f'EPS upload OK ({len(eps_urls)} 枚) だが ReviseItem 失敗: '
                f'{revise_result["message"]}. EPS は eps_upload_cache に永続記録済、'
                f'再試行時は cache hit で課金 0.'
            ),
            'eps_url': hero_url,
        }

    msg = (
        f'eBay 反映成功: 商品ID {ebay_item_id} の写真 {len(eps_urls)} 枚更新 '
        f'(メイン画像 + 追加画像 {len(eps_urls) - 1} 枚)'
    )
    if failed_additional:
        msg += f' / Photoroom/EPS 失敗 {len(failed_additional)} 枚 skip'
    if truncated_count > 0:
        msg += f' / eBay 12 枚上限で {truncated_count} 枚 truncate'

    return {
        'success': True,
        'message': msg,
        'eps_url': hero_url,
    }


# ==========================================================================
# Render UI section
# ==========================================================================

def _render_mode1_ai_compose(
    candidate_id: int, candidate_url: str, ebay_item_id: str, candidate_title: str
) -> None:
    """① AI 合成 (従来フロー、W314 S2 でモード分岐前と完全同一実装).

    Args:
        candidate_id: supplier_candidates.id (session_state key 用)
        candidate_url: 仕入先 URL (og:image 抽出元)
        ebay_item_id: 反映先 eBay 出品 ID
        candidate_title: 表示用商品名 (UI 短縮)

    flow:
        1. 仕入先画像 URL 抽出 (og:image)
        2. Photoroom + Gemini で 3 候補合成
        3. inline radio で 1 候補選択
        4. 「eBay に反映」button → EPS upload + ReviseItem
        5. 結果 success/failure を st.success/error で表示
    """
    sk_source = f"{_SS}hero_source_url_{candidate_id}"
    sk_cands = f"{_SS}hero_candidates_{candidate_id}"
    sk_picked = f"{_SS}hero_selected_path_{candidate_id}"
    sk_apply_result = f"{_SS}apply_result_{candidate_id}"
    # W115 v2 (2026-05-10): multi-image 対応
    sk_all_urls = f"{_SS}all_image_urls_{candidate_id}"
    sk_additional_proc = f"{_SS}additional_processed_{candidate_id}"

    with st.container(border=True):
        st.markdown(
            f'<div style="font-size:11px;color:#8d927f;letter-spacing:2px;'
            f'margin:8px 0 6px;">写 真 反 映 　 — 　 '
            f'候補 #{candidate_id} → 商品ID {ebay_item_id}</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"対象商品: {candidate_title[:60]}")

        # Step 1: 全画像 fetch (multi-image scrape、lazy: 初回のみ)
        all_urls = st.session_state.get(sk_all_urls)
        if all_urls is None:
            with st.spinner("仕入先 URL から全画像を抽出中 (Yahoo/Mercari/PayPay 対応)..."):
                all_urls = fetch_supplier_images_all(candidate_url)
            st.session_state[sk_all_urls] = all_urls
        # W314 実機 E2E 発覚バグ (2026-07-03): 空 list `[]` を fetch がキャッシュ済の状態で
        # 2 回目 render (fragment rerun) すると、旧 `if not all_urls:` guard が `if all_urls
        # is None:` 分岐の内側にあったため cached-empty で skip され、直後の
        # `all_urls[src_idx]` (L665 相当) で `IndexError: list index out of range` が
        # 発生した (Mode② L1226 は既に外側に置いていた = intended pattern)。guard を
        # 外側へ引き上げて cached-empty も cached-None (初回) も同じ経路で graceful return。
        if not all_urls:
            st.error(
                "画像が取得できません (scrape_supplier_url 失敗 + og:image meta も無し)。"
                "仕入先 URL を確認するか、対応サイト (Mercari/Yahoo/PayPay) を確認してください。"
            )
            return

        # board#9: 合成元 picker + 位置/モデル選択 (共有部品)
        from tabs._image_pipeline_ui import (
            COMPOSE_COST_LABELS,
            hero_source_index,
            render_compose_options,
            render_hero_source_picker,
        )

        kp = f"{_SS}c{candidate_id}_"
        src_idx = hero_source_index(kp, all_urls)
        source_url = all_urls[src_idx]
        # additional = hero 以外の先頭 11 枚 (eBay max 12 = hero 1 + additional 11)
        additional_urls = [u for i, u in enumerate(all_urls) if i != src_idx][:11]

        # reviewer HIGH-2: picker での合成元変更を検知したら下流 stage を cascade clear
        # (旧 hero 候補 / 採用 / additional / 反映結果が stale なまま残ると、誤画像を
        # 「課金0」表示や旧結果のまま eBay に反映し得る)
        prev_source = st.session_state.get(sk_source)
        if prev_source and prev_source != source_url:
            st.info("合成元画像が変わったため前回の合成候補・追加画像・反映結果は破棄されました。")
            for k in (sk_cands, sk_picked, sk_additional_proc, sk_apply_result):
                st.session_state.pop(k, None)
        st.session_state[sk_source] = source_url
        st.caption(
            f"取得画像: 全 {len(all_urls)} 枚 "
            f"(hero 候補 1 + additional {len(additional_urls)} 枚) / source: {source_url[:60]}"
        )

        render_hero_source_picker(kp, all_urls)
        position, model = render_compose_options(kp)
        cost = COMPOSE_COST_LABELS.get(model, "$0.14")

        # Step 2: hero 合成 button
        cands = st.session_state.get(sk_cands) or []

        # 正直な課金ラベル (Q0 UI 版): 設定/合成元が前回実行時と異なるなら再課金を明示
        used = st.session_state.get(f"{_SS}hero_used_{candidate_id}")
        opts_changed = (
            bool(cands)
            and used is not None
            and used != {"position": position, "model": model, "source_url": source_url}
        )
        if opts_changed:
            st.caption(
                "⚠️ 合成設定または合成元が前回実行時と異なります。"
                "実行すると新しい設定で再生成されます (再課金)。"
            )

        _b1, _b2, _b3 = st.columns([1.4, 1.4, 4])
        with _b1:
            label = "プレート合成実行" if (not cands or opts_changed) else "再使用 (課金0)"
            if st.button(label, key=f"{_SS}btn_compose_{candidate_id}", type="primary"):
                _do_supplier_hero_compose(
                    candidate_id, source_url, force_regenerate=False,
                    position=position, model=model,
                    legacy_reuse_ok=(src_idx == 0),
                )
                st.rerun()
        with _b2:
            if st.button(
                f"再生成 ({cost})", key=f"{_SS}btn_regen_{candidate_id}",
                help="既存合成結果を破棄して Photoroom + Gemini で再合成",
            ):
                _do_supplier_hero_compose(
                    candidate_id, source_url, force_regenerate=True,
                    position=position, model=model,
                )
                st.rerun()
        with _b3:
            if cands:
                st.caption(f"{len(cands)} 候補生成済")

        if not cands:
            return

        # Step 3: 3 候補 inline radio (Q4 (a) K1 simple)
        st.markdown("**3 候補から 1 枚選択してください**")
        cols = st.columns(len(cands))
        picked = st.session_state.get(sk_picked)
        for idx, cand in enumerate(cands):
            with cols[idx]:
                cpath = str(cand.get("path") or "")
                try:
                    st.image(cpath, use_container_width=True)
                except Exception:
                    st.caption(f"(画像読込失敗) {cpath}")
                is_picked = (picked == cpath)
                btn_label = "採用中" if is_picked else "採用"
                st.caption(
                    f"**#{idx+1} [{cand.get('plate_id')}]** "
                    f"score={cand.get('score', 0):.0f}"
                )
                if st.button(
                    btn_label,
                    key=f"{_SS}btn_pick_{candidate_id}_{idx}",
                    type="primary" if is_picked else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[sk_picked] = cpath
                    st.rerun()

        # Step 4: hero 選択後 → 自動 additional Photoroom (背景抜き、Gemini なし)
        if not picked:
            return

        additional_processed = st.session_state.get(sk_additional_proc)
        if additional_urls and additional_processed is None:
            # auto-trigger (cache hit なら課金 0、新規なら $0.02 × N)
            _do_supplier_additional_compose(
                candidate_id, additional_urls, legacy_reuse_ok=(src_idx == 0),
            )
            st.rerun()

        # Step 4.5: additional preview (画像処理済の場合のみ)
        if additional_processed:
            st.markdown(
                f"**追加画像 {len(additional_processed)} 枚 (背景抜き処理済)**"
            )
            preview_cols = st.columns(min(len(additional_processed), 5))
            for idx, item in enumerate(additional_processed[:5]):
                with preview_cols[idx]:
                    try:
                        st.image(item.get("path", ""), use_container_width=True)
                    except Exception:
                        st.caption(f"(表示失敗) #{idx+2}")
                    st.caption(f"#{idx+2}")
            if len(additional_processed) > 5:
                st.caption(f"... 他 {len(additional_processed) - 5} 枚")
            # 再生成 button (additional のみ、Photoroom $0.02 × N)
            cost_regen = len(additional_urls) * 0.02
            if st.button(
                f"追加画像の再生成 (${cost_regen:.2f})",
                key=f"{_SS}btn_regen_additional_{candidate_id}",
                help="既存の背景抜き画像を破棄して Photoroom で再処理します",
            ):
                _do_supplier_additional_compose(
                    candidate_id, additional_urls, force_regenerate=True
                )
                st.rerun()
        elif additional_urls and additional_processed == []:
            st.warning(
                f"追加画像 {len(additional_urls)} 枚すべての背景処理に失敗しました。"
                "メイン画像のみで eBay 反映可能です (下のボタンから実行)。"
            )

        # Step 5: eBay 反映 button (hero 選択 + additional 処理完了 で表示)
        st.markdown("---")
        additional_paths_for_upload = [
            item["path"] for item in (additional_processed or [])
        ]
        _ec1, _ec2 = st.columns([2, 5])
        with _ec1:
            total_to_upload = 1 + len(additional_paths_for_upload)
            if st.button(
                f"📷 eBay に反映 ({total_to_upload} 枚)",
                key=f"{_SS}btn_apply_{candidate_id}",
                type="primary",
                help=(
                    f"メイン画像 ({picked.split(chr(92))[-1]}) + 追加画像 "
                    f"{len(additional_paths_for_upload)} 枚を eBay にアップロードして "
                    f"商品の写真を更新します"
                ),
            ):
                with st.spinner(
                    f"画像 {total_to_upload} 枚を eBay にアップロード中..."
                ):
                    result = _upload_eps_and_revise(
                        candidate_id,
                        ebay_item_id,
                        picked,
                        additional_paths=additional_paths_for_upload,
                    )
                st.session_state[sk_apply_result] = result
                st.rerun()
        with _ec2:
            st.caption(
                f"メイン画像: {picked.split(chr(92))[-1]} / 追加画像: "
                f"{len(additional_paths_for_upload)} 枚"
            )

        # Step 5: 結果表示 (H-3 silent skip 防止: 失敗時も明示 surface)
        result = st.session_state.get(sk_apply_result)
        if result:
            if result['success']:
                st.success(result['message'])
                if result.get('eps_url'):
                    st.caption(f"EPS URL: {result['eps_url']}")
            else:
                st.error(result['message'])
                if result.get('eps_url'):
                    st.caption(
                        f"EPS は upload 済 ({result['eps_url']}) — "
                        "ReviseItem 再試行時は cache hit で課金 0"
                    )


# ==========================================================================
# W314 Phase 1 S2 (2026-07-03): 画像 3 モード共通部品
#   ① AI 合成 (従来, _render_mode1_ai_compose) / ② そのまま採用 / ③ メイン差し替え
# 設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §4/§6
# ==========================================================================

MODE_AI_COMPOSE = "① AI 合成 (従来)"
MODE_AS_IS = "② 仕入先画像をそのまま使う"
MODE_MAIN_REPLACE = "③ メイン 1 枚だけ差し替え"

# 確定判断1 (設計書 §0): 500px 未満は警告バッジのみ、ブロックしない (強行可)
MIN_RESOLUTION_PX = 500


def check_image_resolution(path: Path) -> Optional[tuple[int, int]]:
    """画像ファイルの解像度 (width, height) を返す. 読込失敗時は None.

    Q0 silent skip prevention: 失敗は logger.warning で痕跡保存.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return (int(im.width), int(im.height))
    except Exception as e:
        logger.warning(f"check_image_resolution 失敗 ({path}): {e}")
        return None


def is_low_resolution(width: int, height: int, min_px: int = MIN_RESOLUTION_PX) -> bool:
    """幅 or 高さのいずれかが min_px 未満なら低解像度 (確定判断1: 警告のみ・強行可)."""
    return width < min_px or height < min_px


def _upgrade_to_https(url: str) -> str:
    """`http://` を `https://` に昇格 (i.ebayimg.com 等は両対応). それ以外は素通し.

    F6: 既存 GetItem 応答が http:// URL を返すケースがあり、そのまま ReviseItem に
    送ると eBay 側で reject される (https 必須). i.ebayimg.com / ebayimg.com は
    https でも同じリソースが返るため機械的に昇格しても副作用なし.
    """
    if not isinstance(url, str) or not url:
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def build_main_replace_picture_urls(
    new_main_url: str, existing_urls: list[str], *, cap: int = 12,
) -> tuple[list[str], list[str], list[str]]:
    """③ メイン差し替え: `[new_main] + existing[1:]` を構築 (eBay 12 枚上限, dedupe 済).

    PictureDetails は ReviseItem で全置換のみ (差分更新不可) のため、既存の
    2 枚目以降を明示的に再送して保持する (設計書 §4-③).

    W314 S2 codex review 対応:
      - F5 dedupe: 順序保持で重複除去 (new_main が existing 内にある場合も 1 枠に).
        cap 12 の実効枚数を減らさない (F5 と 12 枚上限の相互作用の意図).
      - F6 http→https 昇格: existing 側 URL の `http://` は `https://` に昇格.
        i.ebayimg.com は両対応のため機械的に昇格しても副作用なし.
      - F4 fail-closed: 昇格後も非 https が残る URL は **silent 除外せず invalid に集計**.
        caller は len(invalid) > 0 時に反映を中断する (silent 除外 = 画像消失事故).

    Returns:
        (kept, dropped, invalid).
          - kept: 反映対象 (cap 件まで、dedupe + https 昇格済).
          - dropped: cap 超過分 (dedupe 後の cap 位置以降).
          - invalid: 昇格しても https にならなかった URL (mailto: 等).
            **caller は len(invalid) > 0 なら全体を中断する** (Q0 silent skip 防止).
    """
    if not new_main_url:
        return [], [], []
    # 1. combined 構築 (new_main は必ず先頭、existing[1:] を後続)
    combined_raw: list[str] = [new_main_url] + [
        u for u in (existing_urls or [])[1:] if u
    ]
    # 2. F6: http → https 昇格
    combined_upgraded = [_upgrade_to_https(u) for u in combined_raw]
    # 3. F4: 非 https が残ったら invalid に集計 (silent 除外禁止)
    invalid = [u for u in combined_upgraded if not u.startswith("https://")]
    # 4. F5: 順序保持で dedupe
    seen: set[str] = set()
    deduped: list[str] = []
    for u in combined_upgraded:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    # 5. cap 適用 (dedupe 後の実効枚数で 12 上限を判定 = F5 意図)
    kept = deduped[:cap]
    dropped = deduped[cap:]
    return kept, dropped, invalid


def _download_image_to(url: str, dest: Path, timeout: float = 30.0) -> Optional[Path]:
    """URL の画像をローカルにダウンロード. 成功時 Path、失敗時 None.

    Q0 silent skip prevention: 失敗は logger.warning で痕跡保存.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as e:
        logger.warning(f"_download_image_to 失敗 ({url} -> {dest}): {e}")
        return None


def _log_content_change_images_safe(
    ebay_item_id: str,
    before_urls: Optional[list[str]],
    after_urls: Optional[list[str]],
    *,
    source_tab: str,
    candidate_id: Optional[int],
    success: bool,
    ebay_ack: Optional[str] = None,
) -> None:
    """listing_content_change_log (W314 並行実装) への画像変更ログ書込.

    monitor.listing_content_change_log は本タスクと並行実装中のため、未整備
    (ImportError) でも UI を落とさず no-op fallback する (Q0: 痕跡は
    logger.warning に残す、統合後は実体が入って自動的に記録が始まる).
    """
    try:
        from monitor.listing_content_change_log import log_content_change
    except ImportError as e:
        logger.warning(
            f"listing_content_change_log 未整備のため画像変更ログを skip "
            f"(eid={ebay_item_id}): {e}"
        )
        return
    try:
        log_content_change(
            ebay_item_id=ebay_item_id,
            field="images",
            before_value=json.dumps(before_urls or [], ensure_ascii=False),
            after_value=json.dumps(after_urls or [], ensure_ascii=False),
            source_tab=source_tab,
            candidate_id=candidate_id,
            success=success,
            ebay_ack=ebay_ack,
        )
    except Exception as e:  # noqa: BLE001 log 契約の実装差異で例外多様、UI は落とさない
        logger.warning(f"log_content_change 呼出失敗 (images, eid={ebay_item_id}): {e}")


def _upload_full_list_and_revise(ebay_item_id: str, local_paths: list[str]) -> dict:
    """② そのまま採用: 選択画像を EPS 化して PictureDetails を全置換.

    Returns:
        {'success': bool, 'message': str, 'picture_urls': list[str]}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_eps_uploader import upload_images_parallel
    from monitor.ebay_client import revise_item_pictures
    from monitor.image_pipeline_shared import resolve_final_picture_urls

    creds = get_ebay_credentials()
    if not ebay_credentials_ok(creds):
        return {
            'success': False,
            'message': 'eBay credentials not configured (env var 設定 + OAuth 完了確認)',
            'picture_urls': [],
        }
    if not local_paths:
        return {'success': False, 'message': 'アップロード対象画像がありません', 'picture_urls': []}

    paths = [Path(p) for p in local_paths]
    try:
        eps_results = upload_images_parallel(paths, use_cache=True, max_workers=3)
    except Exception as e:
        return {
            'success': False,
            'message': f'EPS upload exception: {type(e).__name__}: {e}',
            'picture_urls': [],
        }

    if len(eps_results) != len(paths):
        return {
            'success': False,
            'message': (
                f'EPS results count mismatch: got {len(eps_results)} but expected '
                f'{len(paths)}. upload_images_parallel internal bug の可能性.'
            ),
            'picture_urls': [],
        }

    eps_urls = [r.eps_url for r in eps_results if r.success and r.eps_url]
    failed_count = len(paths) - len(eps_urls)
    if not eps_urls:
        return {
            'success': False,
            'message': f'EPS upload 全滅 ({len(paths)} 枚)',
            'picture_urls': [],
        }

    kept, dropped = resolve_final_picture_urls(
        processed_eps_urls=eps_urls, selected_raw_urls=[], fallback_raw_urls=[], cap=12,
    )
    if not kept:
        return {
            'success': False,
            'message': 'EPS URL が有効な https URL ではありません',
            'picture_urls': [],
        }

    revise_result = revise_item_pictures(
        item_id=ebay_item_id, picture_urls=kept,
        app_id=creds['app_id'], dev_id=creds['dev_id'], cert_id=creds['cert_id'],
        user_token=creds['user_token'],
    )
    if not revise_result['success']:
        return {
            'success': False,
            'message': (
                f'EPS upload OK ({len(kept)} 枚) だが ReviseItem 失敗: '
                f'{revise_result["message"]}. EPS は eps_upload_cache に永続記録済、'
                f'再試行時は cache hit で課金 0.'
            ),
            'picture_urls': kept,
        }

    msg = f'eBay 反映成功: ItemID {ebay_item_id} の写真 {len(kept)} 枚で全置換'
    if failed_count:
        msg += f' / EPS 失敗 {failed_count} 枚 skip'
    if dropped:
        msg += f' / eBay 12 枚上限で {len(dropped)} 枚 truncate'
    return {'success': True, 'message': msg, 'picture_urls': kept}


def _upload_single_and_revise_main(
    ebay_item_id: str, new_main_source: str, kind: str, existing_urls: list[str],
) -> dict:
    """③ メイン差し替え: 新メイン 1 枚を EPS 化し `[new_main] + existing[1:]` で ReviseItem.

    Args:
        new_main_source: kind='local' ならローカル合成済ファイルパス、
            kind='remote' なら仕入先の画像 URL (内部で DL してから EPS upload).
        existing_urls: GetItem で取得済の現行 PictureURL 全件. 呼び出し元
            (`_render_mode3_main_replace`) で空でないこと確認済の前提だが、
            本関数でも二重防御する (Q0: 既存画像消失リスクを絶対に取らない).

    Returns:
        {'success': bool, 'message': str, 'picture_urls': list[str]}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_eps_uploader import upload_images_parallel
    from monitor.ebay_client import revise_item_pictures

    creds = get_ebay_credentials()
    if not ebay_credentials_ok(creds):
        return {
            'success': False,
            'message': 'eBay credentials not configured (env var 設定 + OAuth 完了確認)',
            'picture_urls': [],
        }
    if not existing_urls:
        return {
            'success': False,
            'message': '現行画像一覧が空のため反映を中断しました (既存画像消失リスク回避)',
            'picture_urls': [],
        }

    if kind == "local":
        local_path = Path(new_main_source)
        if not local_path.exists():
            return {
                'success': False,
                'message': f'ローカル画像が見つかりません: {local_path}',
                'picture_urls': [],
            }
    else:
        tmp_dir = Path("data/hero_candidates/_mode3_tmp")
        dest = tmp_dir / f"{ebay_item_id}_new_main.jpg"
        local_path = _download_image_to(new_main_source, dest)
        if not local_path:
            return {
                'success': False,
                'message': f'新メイン画像のダウンロード失敗: {new_main_source}',
                'picture_urls': [],
            }

    # W314 S2 review H1 fix: モード② と同じ eps_upload_cache 経由に統一。
    # `upload_image_to_eps` 直呼びは cache 非経由のため、ReviseItem 失敗 → 再クリック
    # で同一画像を再課金 upload してしまう。また ① で EPS 化済の合成 hero を ③ で
    # 再利用する経路 (mode3 picker で AI 合成候補を選ぶ場合) では確実に二重 upload
    # になる。`upload_images_parallel(..., use_cache=True)` で file hash → EPS URL の
    # DB cache (`eps_upload_cache`) を経由し重複課金を防ぐ (max_workers=1: 単一画像)。
    _eps_results = upload_images_parallel(
        [local_path], use_cache=True, max_workers=1,
    )
    eps_result = _eps_results[0] if _eps_results else None
    if not eps_result or not eps_result.success or not eps_result.eps_url:
        return {
            'success': False,
            'message': (
                f'新メイン画像の EPS upload 失敗: '
                f'{getattr(eps_result, "error", "no result")}'
            ),
            'picture_urls': [],
        }

    kept, dropped, invalid = build_main_replace_picture_urls(
        eps_result.eps_url, existing_urls, cap=12,
    )
    # F4 fail-closed: 昇格しても非 https が残る URL がある場合、silent 除外せず
    # 全体を中断 (silent 除外 = 画像消失事故 = Q0 違反).
    if invalid:
        return {
            'success': False,
            'message': (
                f'既存画像に https 化できない URL が {len(invalid)} 件あります '
                f'(反映中断・画像消失防止): {invalid[:3]}'
                + (f' ... 他 {len(invalid) - 3} 件' if len(invalid) > 3 else '')
            ),
            'picture_urls': [],
        }
    if not kept:
        return {'success': False, 'message': '差し替え後の画像リストが空です', 'picture_urls': []}

    revise_result = revise_item_pictures(
        item_id=ebay_item_id, picture_urls=kept,
        app_id=creds['app_id'], dev_id=creds['dev_id'], cert_id=creds['cert_id'],
        user_token=creds['user_token'],
    )
    if not revise_result['success']:
        return {
            'success': False,
            'message': (
                f'新メイン EPS upload OK ({eps_result.eps_url}) だが ReviseItem 失敗: '
                f'{revise_result["message"]}'
            ),
            'picture_urls': kept,
        }

    msg = f'eBay 反映成功: メイン画像を差し替え ({len(kept)} 枚、うち既存保持 {len(kept) - 1} 枚)'
    if dropped:
        msg += f' / eBay 12 枚上限で {len(dropped)} 枚 truncate'
    return {'success': True, 'message': msg, 'picture_urls': kept}


def _render_mode2_as_is(
    candidate_id: int, candidate_url: str, ebay_item_id: str, candidate_title: str
) -> None:
    """② 仕入先画像をそのまま採用 (AI 合成なし、EPS 経由で eBay 全置換).

    hotlink 直渡し不可 (仕入先側 URL 消滅対策) のため、選択画像は必ず一旦
    ローカル DL → EPS upload を経由する (設計書 §4-②).
    """
    sk_all_urls = f"{_SS}asis_all_urls_{candidate_id}"
    sk_selected = f"{_SS}asis_selected_{candidate_id}"
    sk_downloaded = f"{_SS}asis_downloaded_{candidate_id}"
    sk_apply_result = f"{_SS}asis_apply_result_{candidate_id}"

    with st.container(border=True):
        st.caption(f"② そのまま採用: 対象商品 {candidate_title[:60]} / item {ebay_item_id}")

        all_urls = st.session_state.get(sk_all_urls)
        if all_urls is None:
            with st.spinner("仕入先 URL から全画像を抽出中 (Yahoo/Mercari/PayPay 対応)..."):
                all_urls = fetch_supplier_images_all(candidate_url)
            st.session_state[sk_all_urls] = all_urls
        if not all_urls:
            st.error(
                "仕入先画像が取得できません (scrape_supplier_url 失敗 + og:image meta も無し)。"
                "仕入先 URL を確認してください。"
            )
            return

        st.markdown("**採用する画像を選択してください (チェックボックス、既定は 1 枚目)**")
        selected_flags: dict = st.session_state.get(sk_selected) or {}
        per_row = 5
        for i in range(0, len(all_urls), per_row):
            cols = st.columns(per_row)
            for j, url in enumerate(all_urls[i:i + per_row]):
                idx = i + j
                with cols[j]:
                    try:
                        st.image(url, use_container_width=True)
                    except Exception:  # noqa: BLE001
                        st.caption(f"(表示失敗) #{idx+1}")
                    checked = st.checkbox(
                        f"#{idx+1} を採用", value=selected_flags.get(idx, idx == 0),
                        key=f"{_SS}asis_chk_{candidate_id}_{idx}",
                    )
                    selected_flags[idx] = checked
        st.session_state[sk_selected] = selected_flags

        chosen_idx = sorted(i for i, v in selected_flags.items() if v)
        if not chosen_idx:
            st.info("1 枚以上選択してください。")
            return
        chosen_urls = [all_urls[i] for i in chosen_idx]

        if st.button(
            "選択画像をダウンロードして解像度確認",
            key=f"{_SS}asis_btn_dl_{candidate_id}", type="primary",
        ):
            out_dir = Path(f"data/hero_candidates/sup_{candidate_id}/asis")
            downloaded: dict = {}
            with st.spinner(f"{len(chosen_urls)} 枚ダウンロード中..."):
                for i, url in zip(chosen_idx, chosen_urls):
                    dest = out_dir / f"raw_{i:02d}.jpg"
                    path = _download_image_to(url, dest)
                    reso = check_image_resolution(path) if path else None
                    downloaded[i] = {
                        "url": url,
                        "path": str(path) if path else None,
                        "reso": reso,
                    }
            st.session_state[sk_downloaded] = downloaded
            st.rerun()

        downloaded = st.session_state.get(sk_downloaded)
        if not downloaded:
            return
        if set(downloaded.keys()) != set(chosen_idx):
            st.warning("選択が変更されました。「選択画像をダウンロードして解像度確認」を再実行してください。")
            return

        st.markdown("**ダウンロード結果**")
        ok_paths: list[str] = []
        for i in chosen_idx:
            info = downloaded.get(i) or {}
            path = info.get("path")
            reso = info.get("reso")
            if not path:
                st.caption(f"#{i+1}: ⚠ ダウンロード失敗 (対象から除外されます)")
                continue
            if reso and is_low_resolution(*reso):
                st.caption(
                    f"#{i+1}: ⚠ 低解像度 {reso[0]}x{reso[1]} "
                    f"({MIN_RESOLUTION_PX}px 未満、そのまま反映可)"
                )
            elif reso:
                st.caption(f"#{i+1}: OK ({reso[0]}x{reso[1]})")
            else:
                st.caption(f"#{i+1}: OK (解像度不明)")
            ok_paths.append(path)

        if not ok_paths:
            st.error("有効な画像がありません。")
            return

        st.markdown("---")
        if st.button(
            f"📷 この {len(ok_paths)} 枚で eBay 画像を全置換",
            key=f"{_SS}asis_btn_apply_{candidate_id}", type="primary",
        ):
            from monitor.ebay_image_fetcher import get_all_ebay_image_urls
            before_urls = get_all_ebay_image_urls(ebay_item_id)  # ログ用ベストエフォート
            # W314 S2 codex review F9: before 取得失敗時は silent にせず UI 注記 + log
            # (mode② は revise 側の重み付けが「全置換 = before 空でも本命処理継続」でよい
            # ため反映は中断しない、が「記録できていない」事実は user に見える形にする).
            if not before_urls:
                logger.warning(
                    f"mode② before_urls 取得失敗 (eid={ebay_item_id}). "
                    f"監査ログ before_value は [] で記録し反映は続行."
                )
                st.info(
                    "変更前画像の記録を取得できませんでした (Ack≠Success / API 失敗)。"
                    "監査ログの変更前は空で記録され、反映処理は続行します。"
                )
            with st.spinner("EPS アップロード + eBay 反映中..."):
                result = _upload_full_list_and_revise(ebay_item_id, ok_paths)
            after_urls = result.get("picture_urls") if result.get("success") else []
            _log_content_change_images_safe(
                ebay_item_id, before_urls, after_urls,
                source_tab="supplier_candidates", candidate_id=candidate_id,
                success=bool(result.get("success")), ebay_ack=result.get("message"),
            )
            st.session_state[sk_apply_result] = result
            st.rerun()

        result = st.session_state.get(sk_apply_result)
        if result:
            if result['success']:
                st.success(result['message'])
            else:
                st.error(result['message'])


def _render_mode3_main_replace(
    candidate_id: int, candidate_url: str, ebay_item_id: str, candidate_title: str
) -> None:
    """③ メイン 1 枚だけ差し替え. `[new_main] + existing[1:]` で eBay に再送.

    現行配列 (GetItem) の取得失敗時は反映を中断してエラー表示する
    (設計書 §4-③: 既存画像を消すリスクを絶対に取らない).
    """
    sk_current = f"{_SS}mr_current_{candidate_id}"
    sk_supplier_urls = f"{_SS}mr_supplier_urls_{candidate_id}"
    sk_picked = f"{_SS}mr_picked_{candidate_id}"
    sk_picked_kind = f"{_SS}mr_picked_kind_{candidate_id}"
    sk_apply_result = f"{_SS}mr_apply_result_{candidate_id}"
    # ① モードで合成済の hero 候補があれば新メイン候補として再利用 (session_state key 共有)
    sk_ai_cands = f"{_SS}hero_candidates_{candidate_id}"

    with st.container(border=True):
        st.caption(f"③ メイン差し替え: 対象商品 {candidate_title[:60]} / item {ebay_item_id}")

        current = st.session_state.get(sk_current)
        if current is None:
            from monitor.ebay_image_fetcher import get_all_ebay_image_urls
            with st.spinner("eBay の現行画像一覧を取得中 (GetItem)..."):
                current = get_all_ebay_image_urls(ebay_item_id)
            st.session_state[sk_current] = current

        if not current:
            st.error(
                "eBay の現行画像一覧を取得できませんでした (GetItem 失敗)。"
                "既存画像を消すリスクがあるため反映は中断します。"
                "credentials / ItemID を確認してから再試行してください。"
            )
            if st.button("再取得", key=f"{_SS}mr_btn_retry_{candidate_id}"):
                st.session_state.pop(sk_current, None)
                st.rerun()
            return

        st.markdown(f"**現行画像 ({len(current)} 枚、1 枚目がメイン = 差し替え対象)**")
        cur_cols = st.columns(min(len(current), 6))
        for i, url in enumerate(current[:6]):
            with cur_cols[i]:
                try:
                    st.image(url, use_container_width=True)
                except Exception:  # noqa: BLE001
                    st.caption("(表示失敗)")
                st.caption("🎯 現行メイン" if i == 0 else f"#{i+1}")
        if len(current) > 6:
            st.caption(f"... 他 {len(current) - 6} 枚")

        supplier_urls = st.session_state.get(sk_supplier_urls)
        if supplier_urls is None:
            with st.spinner("仕入先 URL から画像を抽出中..."):
                supplier_urls = fetch_supplier_images_all(candidate_url)
            st.session_state[sk_supplier_urls] = supplier_urls

        ai_cands = st.session_state.get(sk_ai_cands) or []
        candidates: list[dict] = []
        for c in ai_cands:
            p = c.get("path")
            if p:
                candidates.append(
                    {"label": f"AI合成 [{c.get('plate_id')}]", "path": p, "kind": "local"}
                )
        for i, u in enumerate(supplier_urls or []):
            candidates.append({"label": f"仕入先 raw #{i+1}", "path": u, "kind": "remote"})

        if not candidates:
            st.warning(
                "新メイン候補がありません (仕入先画像取得失敗、①モードでの合成も未実施)。"
            )
            return

        st.markdown("**新しいメイン画像を選択してください**")
        picked = st.session_state.get(sk_picked)
        per_row = 5
        for i in range(0, len(candidates), per_row):
            cols = st.columns(per_row)
            for j, cand in enumerate(candidates[i:i + per_row]):
                idx = i + j
                with cols[j]:
                    try:
                        st.image(cand["path"], use_container_width=True)
                    except Exception:  # noqa: BLE001
                        st.caption("(表示失敗)")
                    is_picked = (picked == cand["path"])
                    st.caption(cand["label"])
                    if st.button(
                        "採用中" if is_picked else "選択",
                        key=f"{_SS}mr_btn_pick_{candidate_id}_{idx}",
                        type="primary" if is_picked else "secondary",
                        use_container_width=True,
                    ):
                        st.session_state[sk_picked] = cand["path"]
                        st.session_state[sk_picked_kind] = cand["kind"]
                        st.rerun()

        if not picked:
            return

        kind = st.session_state.get(sk_picked_kind) or "remote"
        st.markdown("---")
        if st.button(
            "📷 このメイン画像で差し替え",
            key=f"{_SS}mr_btn_apply_{candidate_id}", type="primary",
        ):
            # W314 S2 codex review F1/F2: apply 直前に fresh GetItem を取り直して
            # ベースラインにする (sk_current は render 開始時の snapshot なので、
            # モード②等で他 tab から画像更新された場合に stale)。取得失敗/空なら
            # 中断 (既存画像消失リスク回避、fail-closed).
            from monitor.ebay_image_fetcher import get_all_ebay_image_urls
            with st.spinner("現行画像を eBay から再取得中 (GetItem)..."):
                fresh_current = get_all_ebay_image_urls(ebay_item_id)
            if not fresh_current:
                fail = {
                    'success': False,
                    'message': (
                        'apply 直前の GetItem 再取得に失敗しました '
                        '(Ack≠Success / API 失敗 / 空). 既存画像消失リスクがあるため '
                        '反映を中断しました. しばらく待って再試行してください.'
                    ),
                    'picture_urls': [],
                }
                _log_content_change_images_safe(
                    ebay_item_id, current, [],
                    source_tab="supplier_candidates", candidate_id=candidate_id,
                    success=False, ebay_ack=fail['message'],
                )
                st.session_state[sk_apply_result] = fail
                st.rerun()
            with st.spinner("EPS アップロード + eBay 反映中..."):
                result = _upload_single_and_revise_main(
                    ebay_item_id, picked, kind, fresh_current,
                )
            after_urls = result.get("picture_urls") if result.get("success") else []
            # F2: 監査ログの before_value は fresh_current (revise 実行時の真の現行)
            _log_content_change_images_safe(
                ebay_item_id, fresh_current, after_urls,
                source_tab="supplier_candidates", candidate_id=candidate_id,
                success=bool(result.get("success")), ebay_ack=result.get("message"),
            )
            st.session_state[sk_apply_result] = result
            # W314 S2 review M1: 成功時に現行画像キャッシュを無効化。
            # 次回 render 時に GetItem で再取得させ、古い配列が表示継続するのを防ぐ。
            if result.get("success"):
                st.session_state.pop(sk_current, None)
            st.rerun()

        result = st.session_state.get(sk_apply_result)
        if result:
            if result['success']:
                st.success(result['message'])
            else:
                st.error(result['message'])


def render_supplier_photo_apply_section(
    candidate_id: int, candidate_url: str, ebay_item_id: str, candidate_title: str
) -> None:
    """app.py / 仕入先候補タブから呼出される写真反映セクション (W314 S2: 3 モード).

    Args:
        candidate_id: supplier_candidates.id (session_state key 用)
        candidate_url: 仕入先 URL (og:image / 全画像抽出元)
        ebay_item_id: 反映先 eBay 出品 ID
        candidate_title: 表示用商品名 (UI 短縮)

    3 モード (既定 = ① 従来フロー無改変):
        ① AI 合成 (従来): `_render_mode1_ai_compose` (完全既存実装)
        ② 仕入先画像をそのまま使う: `_render_mode2_as_is` (EPS 経由で全置換)
        ③ メイン 1 枚だけ差し替え: `_render_mode3_main_replace`
            (`[new_main] + existing[1:]` で ReviseItem 再送)
    """
    sk_mode = f"{_SS}photo_mode_{candidate_id}"
    mode = st.radio(
        "反映モード",
        [MODE_AI_COMPOSE, MODE_AS_IS, MODE_MAIN_REPLACE],
        key=sk_mode,
        horizontal=True,
    )
    if mode == MODE_AS_IS:
        _render_mode2_as_is(candidate_id, candidate_url, ebay_item_id, candidate_title)
    elif mode == MODE_MAIN_REPLACE:
        _render_mode3_main_replace(candidate_id, candidate_url, ebay_item_id, candidate_title)
    else:
        _render_mode1_ai_compose(candidate_id, candidate_url, ebay_item_id, candidate_title)
