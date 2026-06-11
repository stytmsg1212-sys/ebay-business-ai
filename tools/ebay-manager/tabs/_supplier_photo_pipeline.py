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
    candidate_id: int, source_url: str, force_regenerate: bool = False
) -> None:
    """Photoroom + Gemini で hero 候補 3 枚を生成. session_state 経由で UI に反映.

    既存合成結果 (data/hero_candidates/sup_<cid>/hero_W*.png) があれば force_regenerate=
    False で API skip. 課金保護.
    """
    out_base = Path(f"data/hero_candidates/sup_{candidate_id}")
    out_base.mkdir(parents=True, exist_ok=True)

    sk_cands = f"{_SS}hero_candidates_{candidate_id}"
    sk_studio = f"{_SS}hero_studio_path_{candidate_id}"
    sk_source = f"{_SS}hero_source_url_{candidate_id}"
    sk_picked = f"{_SS}hero_selected_path_{candidate_id}"

    # 既存合成結果 reuse
    existing_heros = sorted(out_base.glob("hero_W*.png"))
    existing_studio = out_base / "_studio.png"
    if not force_regenerate and existing_heros and existing_studio.exists():
        candidates = [{"name": p.stem, "path": str(p)} for p in existing_heros]
        st.session_state[sk_cands] = candidates
        st.session_state[sk_source] = source_url
        st.session_state[sk_studio] = str(existing_studio)
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
    source_path = out_base / "_source.jpg"
    try:
        with st.status("画像をダウンロード中...", expanded=False) as _s:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                r = c.get(source_url)
                r.raise_for_status()
                source_path.write_bytes(r.content)
            _s.update(label=f"ダウンロード完了 ({len(r.content) // 1024} KB)", state="complete")
    except Exception as e:
        st.error(f"画像ダウンロード失敗: {e}")
        return

    # 2. Photoroom
    try:
        with st.status("Photoroom で studio 化中 (約 10 秒)...", expanded=False) as _s:
            pr = compose_cover_with_photoroom(source_path)
            if not pr.success:
                st.error(f"Photoroom 失敗: {pr.error}")
                return
            studio_path = out_base / "_studio.png"
            pr.image.save(studio_path)
            st.session_state[sk_studio] = str(studio_path)
            _s.update(label="Photoroom 完了", state="complete")
    except Exception as e:
        st.error(f"Photoroom 例外: {e}")
        return

    # 3. Gemini 3 候補
    try:
        with st.status("Gemini でプレート合成中 (約 30-40 秒, 3 候補並列)...", expanded=False) as _s:
            result = generate_hero_candidates(
                studio_product_path=studio_path,
                output_dir=out_base,
                k=3,
                max_parallel=3,
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
            _s.update(label=f"完了: {len(cands)} 候補生成", state="complete")
    except Exception as e:
        st.error(f"Gemini 合成例外: {e}")
        return


# ==========================================================================
# W115 v2 (2026-05-10): hero 以外の画像を Photoroom で背景抜き処理
# ==========================================================================

def _do_supplier_additional_compose(
    candidate_id: int, additional_urls: list[str], force_regenerate: bool = False
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

    # Cost protection: 既存 _additional_*.png があれば API skip
    existing = sorted(out_base.glob("_additional_*.png"))
    if not force_regenerate and len(existing) >= len(additional_urls):
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

def render_supplier_photo_apply_section(
    candidate_id: int, candidate_url: str, ebay_item_id: str, candidate_title: str
) -> None:
    """app.py から呼出される写真反映セクション.

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
            if not all_urls:
                st.error(
                    "画像が取得できません (scrape_supplier_url 失敗 + og:image meta も無し)。"
                    "仕入先 URL を確認するか、対応サイト (Mercari/Yahoo/PayPay) を確認してください。"
                )
                return

        # hero source = image_urls[0]、additional = [1:11] (eBay max 12 = hero 1 + additional 11)
        source_url = all_urls[0]
        additional_urls = all_urls[1:12]
        st.session_state[sk_source] = source_url
        st.caption(
            f"取得画像: 全 {len(all_urls)} 枚 "
            f"(hero 候補 1 + additional {len(additional_urls)} 枚) / source: {source_url[:60]}"
        )

        # Step 2: hero 合成 button
        cands = st.session_state.get(sk_cands) or []
        _b1, _b2, _b3 = st.columns([1.4, 1.4, 4])
        with _b1:
            label = "プレート合成実行" if not cands else "再使用 (課金0)"
            if st.button(label, key=f"{_SS}btn_compose_{candidate_id}", type="primary"):
                _do_supplier_hero_compose(candidate_id, source_url, force_regenerate=False)
                st.rerun()
        with _b2:
            if st.button(
                "再生成 ($0.14)", key=f"{_SS}btn_regen_{candidate_id}",
                help="既存合成結果を破棄して Photoroom + Gemini で再合成",
            ):
                _do_supplier_hero_compose(candidate_id, source_url, force_regenerate=True)
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
            _do_supplier_additional_compose(candidate_id, additional_urls)
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
