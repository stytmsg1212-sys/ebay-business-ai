#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W158 (2026-05-23): 画像加工パイプライン共通 UI section.

個別出品 / 商品管理 / 仕入先候補 3 タブから同じ関数を呼んで使う Streamlit
section. prefix 引数で session_state key を namespace 化.

提供 API:
  - render_image_pipeline_section(...): 3 タブ共通の section 描画
  - clear_pipeline_keys(prefix): caller 側で source 変更時に呼ぶ cascade clear

設計書: .company/engineering/docs/2026-05-23-W158-image-pipeline-shared.md (v2.2)

Codex GPT-5.5 review fix (v2.2):
  - HIGH-Codex-1: in-flight lock (連打 / Streamlit rerun 重複課金防止)
  - HIGH-Codex-5: credentials guard (Photoroom / Gemini / eBay の 3 種を check)
  - HIGH-Codex-7: section hide でなく実行ボタン disable + checklist 表示
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

import streamlit as st

from monitor.image_pipeline_shared import (
    HeroCandidate,
    AdditionalProcessed,
    EpsUploadOutcome,
    compose_hero_candidates_cached,
    unify_additional_backgrounds_cached,
    upload_to_eps_cached,
    resolve_final_picture_urls,
)

logger = logging.getLogger(__name__)

# session_state key suffix (caller の prefix の後に付く)
_SUFFIX_HERO_CANDIDATES = "hero_candidates"
_SUFFIX_HERO_SELECTED = "hero_selected_path"
_SUFFIX_HERO_STUDIO = "hero_studio_path"
_SUFFIX_HERO_SOURCE = "hero_source_url"
_SUFFIX_ADDITIONAL = "additional_processed"
_SUFFIX_PROCESSED_URLS = "processed_image_urls"
_SUFFIX_LAST_APPLY = "last_apply_result"
_SUFFIX_LOCK_PREFIX = "lock_"  # 各 stage の lock key: f"{prefix}lock_{stage}"

# stage 種別 (in-flight lock key 用)
STAGE_HERO = "hero"
STAGE_ADDITIONAL = "additional"
STAGE_EPS = "eps"
STAGE_APPLY = "apply"


# ─────────────────────────────────────────────
# Credentials check (HIGH-Codex-5)
# ─────────────────────────────────────────────

def _check_credentials() -> dict:
    """Photoroom / Gemini / eBay の 3 種 credentials check.

    Returns: {'photoroom': bool, 'gemini': bool, 'ebay': bool}
    """
    photoroom_ok = bool(os.environ.get("PHOTOROOM_API_KEY"))
    gemini_ok = bool(os.environ.get("FAL_KEY") and os.environ.get("GOOGLE_API_KEY"))
    try:
        from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
        ebay_ok = ebay_credentials_ok(get_ebay_credentials())
    except Exception:  # noqa: BLE001
        ebay_ok = False
    return {"photoroom": photoroom_ok, "gemini": gemini_ok, "ebay": ebay_ok}


# ─────────────────────────────────────────────
# in-flight lock (HIGH-Codex-1)
# ─────────────────────────────────────────────

def _lock_key(prefix: str, stage: str) -> str:
    return f"{prefix}{_SUFFIX_LOCK_PREFIX}{stage}"


def _is_locked(prefix: str, stage: str) -> bool:
    return bool(st.session_state.get(_lock_key(prefix, stage), False))


def _acquire_lock(prefix: str, stage: str) -> bool:
    """lock 取得. すでにロックされていたら False (caller skip)."""
    key = _lock_key(prefix, stage)
    if st.session_state.get(key, False):
        return False
    st.session_state[key] = True
    return True


def _release_lock(prefix: str, stage: str) -> None:
    st.session_state[_lock_key(prefix, stage)] = False


# ─────────────────────────────────────────────
# Public: cascade clear (caller 責任 / HIGH-1 二重作用回避)
# ─────────────────────────────────────────────

def clear_pipeline_keys(prefix: str) -> None:
    """caller 側で source URL 変更時に呼ぶ. shared 内 全 key を一括クリア.

    in-flight lock も解除 (異常終了からの復帰経路).
    """
    keys = [
        _SUFFIX_HERO_CANDIDATES, _SUFFIX_HERO_SELECTED, _SUFFIX_HERO_STUDIO,
        _SUFFIX_HERO_SOURCE, _SUFFIX_ADDITIONAL, _SUFFIX_PROCESSED_URLS,
        _SUFFIX_LAST_APPLY,
    ]
    for suffix in keys:
        st.session_state.pop(f"{prefix}{suffix}", None)
    # locks
    for stage in (STAGE_HERO, STAGE_ADDITIONAL, STAGE_EPS, STAGE_APPLY):
        st.session_state.pop(_lock_key(prefix, stage), None)


# ─────────────────────────────────────────────
# Public: main UI section
# ─────────────────────────────────────────────

def render_image_pipeline_section(
    *,
    prefix: str,
    source_urls: list[str],
    sku_hint: str,
    ebay_item_id: Optional[str] = None,
    description_html: Optional[str] = None,
    on_apply_image: Optional[Callable[[list[str]], dict]] = None,
    on_apply_description: Optional[Callable[[str], dict]] = None,
    on_apply_both: Optional[Callable[[str, list[str]], dict]] = None,
) -> None:
    """3 タブ共通の画像加工 + 反映 section.

    Args:
        prefix: session_state namespace prefix (例 "pm_url_direct_{eid}_w158_")
        source_urls: 加工対象の raw URL list (source_urls[0] = hero, [1:] = additional)
        sku_hint: out_base path 用 (data/hero_candidates/{sku_hint}/)
        ebay_item_id: 反映ボタン有効化条件 (None なら apply 系 button 非表示)
        description_html: description 反映 button 有効化条件
        on_apply_image: callback(picture_urls: list[str]) -> dict
        on_apply_description: callback(description_html: str) -> dict
        on_apply_both: callback(description_html, picture_urls) -> dict

    flow:
      Step A: credentials check (足りなければ checklist 表示で button disable)
      Step B: hero 合成 (3 候補) + user 採用
      Step C: 2 枚目以降の背景統一
      Step D: EPS upload
      Step E: 反映 3 button (image-only / description-only / both)
    """
    creds = _check_credentials()

    if not source_urls:
        st.info("加工対象の画像 URL がありません.")
        return

    # ── Step A: credentials checklist (HIGH-Codex-7) ──
    if not (creds["photoroom"] and creds["gemini"] and creds["ebay"]):
        missing = []
        if not creds["photoroom"]:
            missing.append("PHOTOROOM_API_KEY")
        if not creds["gemini"]:
            missing.append("FAL_KEY / GOOGLE_API_KEY")
        if not creds["ebay"]:
            missing.append("eBay OAuth (settings.json or env)")
        st.warning(
            "⚠️ 画像加工に必要な認証情報が未設定です: " + ", ".join(missing)
            + "\n\n環境変数 (.env) 設定後にこのページを再読込してください."
        )
        return

    # source_urls[0] = hero source
    hero_source_url = source_urls[0]
    additional_source_urls = list(source_urls[1:]) if len(source_urls) > 1 else []

    # out_base (caller の sku_hint を使う、空なら eid or temp fallback)
    out_base_name = sku_hint.strip() or (f"eid_{ebay_item_id}" if ebay_item_id else f"temp_{int(time.time())}")
    # path traversal 防止 (sku_hint に .. 含めない)
    out_base_name = out_base_name.replace("..", "_").replace("/", "_").replace("\\", "_")
    out_base = Path("data/hero_candidates") / out_base_name

    st.markdown(
        '<div style="font-size:11px;color:rgba(180,220,255,0.55);'
        'letter-spacing:2px;margin:8px 0 6px;">'
        '画 像 加 工 　 + 　 e B a y 反 映</div>',
        unsafe_allow_html=True,
    )

    # ── Step B: hero compose ──
    _render_step_b_hero_compose(
        prefix=prefix,
        hero_source_url=hero_source_url,
        out_base=out_base,
    )

    hero_selected = st.session_state.get(f"{prefix}{_SUFFIX_HERO_SELECTED}")

    # ── Step C: additional backgrounds ──
    if hero_selected and additional_source_urls:
        _render_step_c_additional(
            prefix=prefix,
            additional_urls=additional_source_urls,
            out_base=out_base,
        )

    # ── Step D: EPS upload ──
    if hero_selected:
        _render_step_d_eps_upload(
            prefix=prefix,
            additional_count=len(additional_source_urls),
        )

    # ── Step E: 反映ボタン 3 種 ──
    if ebay_item_id:
        _render_step_e_apply_buttons(
            prefix=prefix,
            ebay_item_id=ebay_item_id,
            description_html=description_html,
            on_apply_image=on_apply_image,
            on_apply_description=on_apply_description,
            on_apply_both=on_apply_both,
        )


# ─────────────────────────────────────────────
# Step B: hero compose UI
# ─────────────────────────────────────────────

def _render_step_b_hero_compose(*, prefix: str, hero_source_url: str, out_base: Path) -> None:
    """1 枚目の hero 合成 (Photoroom + Gemini 3 候補)."""
    sk_cands = f"{prefix}{_SUFFIX_HERO_CANDIDATES}"
    sk_selected = f"{prefix}{_SUFFIX_HERO_SELECTED}"
    sk_source = f"{prefix}{_SUFFIX_HERO_SOURCE}"

    # source URL 変化検知 → 候補 cascade clear
    last_source = st.session_state.get(sk_source)
    if last_source and last_source != hero_source_url:
        st.info("source 画像が変わったため前回の合成候補は破棄されました。再生成してください。")
        st.session_state.pop(sk_cands, None)
        st.session_state.pop(sk_selected, None)

    candidates_raw = st.session_state.get(sk_cands) or []
    # session_state は dict 形 (HeroCandidate.to_dict() の result)
    candidates = candidates_raw

    with st.container(border=True):
        st.caption(
            f"Step B: ロゴプレート合成 (Photoroom + Gemini 3 候補, 約 $0.14 = 21 円). "
            f"source: {hero_source_url[:80]}"
        )
        cols = st.columns([1.3, 1.4, 1.3, 4])
        with cols[0]:
            locked = _is_locked(prefix, STAGE_HERO)
            label = "プレート合成実行" if not candidates else "再使用 (課金0)"
            if st.button(label, key=f"{prefix}btn_hero_compose", type="primary", disabled=locked):
                _do_hero_compose(prefix, hero_source_url, out_base, force_regenerate=False)
                st.rerun()
        with cols[1]:
            locked = _is_locked(prefix, STAGE_HERO)
            if st.button("再生成 ($0.14)", key=f"{prefix}btn_hero_regen", disabled=locked):
                _do_hero_compose(prefix, hero_source_url, out_base, force_regenerate=True)
                st.rerun()
        with cols[2]:
            if candidates and st.button("候補クリア", key=f"{prefix}btn_hero_clear"):
                st.session_state.pop(sk_cands, None)
                st.session_state.pop(sk_selected, None)
                st.rerun()
        with cols[3]:
            if candidates:
                st.caption(f"{len(candidates)} 候補 / source: {hero_source_url[:60]}...")

        if not candidates:
            return

        # 候補を横並びで表示
        st.markdown("**3 候補から 1 枚選択してください**")
        sel_path = st.session_state.get(sk_selected)
        cand_cols = st.columns(len(candidates))
        for idx, cand in enumerate(candidates):
            with cand_cols[idx]:
                cpath = str(cand.get("path") or "")
                try:
                    st.image(cpath, use_container_width=True)
                except Exception:  # noqa: BLE001
                    st.caption(f"(画像読込失敗) {cpath}")
                is_picked = (sel_path == cpath)
                st.caption(
                    f"**#{idx+1} [{cand.get('plate_id')}]** score={float(cand.get('score') or 0):.0f}"
                )
                btn_label = "採用中" if is_picked else "採用"
                if st.button(
                    btn_label, key=f"{prefix}btn_hero_pick_{idx}",
                    type="primary" if is_picked else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[sk_selected] = cpath
                    st.rerun()


def _do_hero_compose(prefix: str, source_url: str, out_base: Path, *, force_regenerate: bool) -> None:
    """hero compose を実行して session_state に保存."""
    if not _acquire_lock(prefix, STAGE_HERO):
        st.warning("hero 合成は実行中です. 完了をお待ちください.")
        return
    try:
        with st.status(
            "Photoroom + Gemini で 3 候補生成中 (~40 秒)..." if force_regenerate
            else "既存合成結果を確認中...", expanded=False,
        ) as _s:
            candidates, studio = compose_hero_candidates_cached(
                source_url, out_base, force_regenerate=force_regenerate,
            )
            if not candidates:
                _s.update(label="hero 合成失敗 (logger 参照)", state="error")
                st.error("hero 合成失敗. PHOTOROOM_API_KEY / FAL_KEY / GOOGLE_API_KEY 設定を確認.")
                return
            st.session_state[f"{prefix}{_SUFFIX_HERO_CANDIDATES}"] = [c.to_dict() for c in candidates]
            st.session_state[f"{prefix}{_SUFFIX_HERO_SOURCE}"] = source_url
            if studio:
                st.session_state[f"{prefix}{_SUFFIX_HERO_STUDIO}"] = str(studio)
            st.session_state.pop(f"{prefix}{_SUFFIX_HERO_SELECTED}", None)
            _s.update(label=f"完了: {len(candidates)} 候補生成", state="complete")
    finally:
        _release_lock(prefix, STAGE_HERO)


# ─────────────────────────────────────────────
# Step C: additional backgrounds
# ─────────────────────────────────────────────

def _render_step_c_additional(*, prefix: str, additional_urls: list[str], out_base: Path) -> None:
    sk_add = f"{prefix}{_SUFFIX_ADDITIONAL}"
    processed = st.session_state.get(sk_add)
    cost = len(additional_urls) * 0.02

    with st.container(border=True):
        st.caption(
            f"Step C: 2 枚目以降を背景グレー統一 ({len(additional_urls)} 枚, "
            f"約 ${cost:.2f} = {int(cost * 150)} 円)"
        )
        cols = st.columns([1.4, 1.4, 1.2, 4])
        with cols[0]:
            locked = _is_locked(prefix, STAGE_ADDITIONAL)
            label = "統一処理実行" if not processed else "再使用 (課金0)"
            if st.button(label, key=f"{prefix}btn_add_unify", type="primary", disabled=locked):
                _do_additional_unify(prefix, additional_urls, out_base, force_regenerate=False)
                st.rerun()
        with cols[1]:
            locked = _is_locked(prefix, STAGE_ADDITIONAL)
            if st.button(f"再生成 (${cost:.2f})", key=f"{prefix}btn_add_regen", disabled=locked):
                _do_additional_unify(prefix, additional_urls, out_base, force_regenerate=True)
                st.rerun()
        with cols[2]:
            if processed and st.button("結果クリア", key=f"{prefix}btn_add_clear"):
                st.session_state.pop(sk_add, None)
                st.rerun()
        with cols[3]:
            if processed:
                st.caption(f"{len(processed)}/{len(additional_urls)} 枚処理済")

        if not processed:
            return

        # サムネ表示
        n = len(processed)
        per_row = min(n, 5)
        for i in range(0, n, per_row):
            batch = processed[i:i + per_row]
            row_cols = st.columns(per_row)
            for j, item in enumerate(batch):
                with row_cols[j]:
                    p = item.get("path") or ""
                    try:
                        st.image(p, use_container_width=True)
                    except Exception:  # noqa: BLE001
                        st.caption(f"(表示失敗) {p}")
                    st.caption(f"#{i + j + 2}")


def _do_additional_unify(prefix: str, urls: list[str], out_base: Path, *, force_regenerate: bool) -> None:
    if not _acquire_lock(prefix, STAGE_ADDITIONAL):
        st.warning("背景統一は実行中です. 完了をお待ちください.")
        return
    try:
        with st.status(
            f"{len(urls)} 枚を Photoroom で並列処理中...", expanded=False,
        ) as _s:
            results = unify_additional_backgrounds_cached(
                urls, out_base, force_regenerate=force_regenerate,
            )
            st.session_state[f"{prefix}{_SUFFIX_ADDITIONAL}"] = [r.to_dict() for r in results]
            if len(results) < len(urls):
                _s.update(
                    label=f"部分完了: {len(results)}/{len(urls)} 枚成功",
                    state="complete",
                )
            else:
                _s.update(label=f"完了: {len(results)} 枚処理成功", state="complete")
    finally:
        _release_lock(prefix, STAGE_ADDITIONAL)


# ─────────────────────────────────────────────
# Step D: EPS upload
# ─────────────────────────────────────────────

def _render_step_d_eps_upload(*, prefix: str, additional_count: int) -> None:
    sk_eps = f"{prefix}{_SUFFIX_PROCESSED_URLS}"
    sk_hero = f"{prefix}{_SUFFIX_HERO_SELECTED}"
    sk_add = f"{prefix}{_SUFFIX_ADDITIONAL}"

    hero = st.session_state.get(sk_hero)
    additional = st.session_state.get(sk_add) or []
    uploaded = st.session_state.get(sk_eps) or []

    if not hero and not additional:
        return

    total_local = (1 if hero else 0) + len(additional)

    with st.container(border=True):
        st.caption(
            f"Step D: eBay EPS upload ({total_local} 枚, 課金 0, 重複は cache 経由 skip)"
        )
        cols = st.columns([1.5, 1.2, 4])
        with cols[0]:
            locked = _is_locked(prefix, STAGE_EPS)
            label = "EPS アップロード実行" if not uploaded else "再アップロード"
            if st.button(label, key=f"{prefix}btn_eps_upload", type="primary", disabled=locked):
                _do_eps_upload(prefix)
                st.rerun()
        with cols[1]:
            if uploaded and st.button("結果クリア", key=f"{prefix}btn_eps_clear"):
                st.session_state.pop(sk_eps, None)
                st.rerun()
        with cols[2]:
            if uploaded:
                st.caption(f"{len(uploaded)}/{total_local} 枚アップロード済")

        if uploaded:
            st.markdown("**公開 URL (eBay 反映で使われます)**")
            for i, u in enumerate(uploaded[:5]):
                st.caption(f"#{i+1}  {u}")
            if len(uploaded) > 5:
                st.caption(f"... 他 {len(uploaded) - 5} 枚")


def _do_eps_upload(prefix: str) -> None:
    if not _acquire_lock(prefix, STAGE_EPS):
        st.warning("EPS upload は実行中です.")
        return
    try:
        hero = st.session_state.get(f"{prefix}{_SUFFIX_HERO_SELECTED}")
        additional = st.session_state.get(f"{prefix}{_SUFFIX_ADDITIONAL}") or []

        paths: list[Path] = []
        if hero:
            paths.append(Path(hero))
        for item in additional:
            p = item.get("path") if isinstance(item, dict) else None
            if p:
                paths.append(Path(p))

        if not paths:
            st.error("EPS upload 対象がありません.")
            return

        with st.status(
            f"{len(paths)} 枚を eBay EPS にアップロード中...", expanded=False,
        ) as _s:
            outcome: EpsUploadOutcome = upload_to_eps_cached(paths)
            st.session_state[f"{prefix}{_SUFFIX_PROCESSED_URLS}"] = list(outcome.eps_urls)
            if outcome.success:
                _s.update(
                    label=f"完了: {len(outcome.eps_urls)} 枚 upload 成功",
                    state="complete",
                )
            else:
                _s.update(
                    label=(
                        f"部分完了: {len(outcome.eps_urls)}/{len(paths)} 枚 upload, "
                        f"{len(outcome.failed)} 件失敗"
                    ),
                    state="error" if not outcome.eps_urls else "complete",
                )
                if outcome.failed:
                    st.error(
                        "失敗: " + ", ".join(f"{f[0]}: {f[1]}" for f in outcome.failed[:3])
                        + (" ..." if len(outcome.failed) > 3 else "")
                    )
    finally:
        _release_lock(prefix, STAGE_EPS)


# ─────────────────────────────────────────────
# Step E: 反映ボタン 3 種
# ─────────────────────────────────────────────

def _render_step_e_apply_buttons(
    *,
    prefix: str,
    ebay_item_id: str,
    description_html: Optional[str],
    on_apply_image: Optional[Callable[[list[str]], dict]],
    on_apply_description: Optional[Callable[[str], dict]],
    on_apply_both: Optional[Callable[[str, list[str]], dict]],
) -> None:
    sk_eps = f"{prefix}{_SUFFIX_PROCESSED_URLS}"
    sk_apply = f"{prefix}{_SUFFIX_LAST_APPLY}"

    processed_urls = st.session_state.get(sk_eps) or []
    has_image = len(processed_urls) > 0
    has_desc = bool((description_html or "").strip())

    # PictureURL cap warning (HIGH-3)
    final_kept, final_dropped = resolve_final_picture_urls(
        processed_eps_urls=processed_urls,
        selected_raw_urls=[], fallback_raw_urls=[], cap=12,
    )
    if final_dropped:
        st.warning(
            f"⚠️ {len(final_dropped)} 枚は ReviseItem 上限 (12 枚) 超過のため反映されません. "
            f"(AddFixedPriceItem の 24 枚と異なる)"
        )

    with st.container(border=True):
        st.markdown("**Step E: eBay に反映**")
        cols = st.columns(3)
        with cols[0]:
            locked = _is_locked(prefix, STAGE_APPLY)
            disabled = not (has_image and on_apply_image) or locked
            if st.button(
                "📷 画像だけ反映",
                key=f"{prefix}btn_apply_image",
                disabled=disabled,
                use_container_width=True,
            ):
                if on_apply_image:
                    _do_apply(prefix, lambda: on_apply_image(final_kept))
                    st.rerun()
        with cols[1]:
            locked = _is_locked(prefix, STAGE_APPLY)
            disabled = not (has_desc and on_apply_description) or locked
            if st.button(
                "📝 説明文だけ反映",
                key=f"{prefix}btn_apply_desc",
                disabled=disabled,
                use_container_width=True,
            ):
                if on_apply_description:
                    _do_apply(prefix, lambda: on_apply_description(description_html or ""))
                    st.rerun()
        with cols[2]:
            locked = _is_locked(prefix, STAGE_APPLY)
            disabled = not (has_image and has_desc and on_apply_both) or locked
            if st.button(
                "✅ 両方反映 (説明文 → 画像 の順)",
                key=f"{prefix}btn_apply_both",
                type="primary",
                disabled=disabled,
                use_container_width=True,
            ):
                if on_apply_both:
                    _do_apply(prefix, lambda: on_apply_both(description_html or "", final_kept))
                    st.rerun()

        # 結果表示 (永続)
        result = st.session_state.get(sk_apply)
        if result:
            _render_apply_result(result)


def _do_apply(prefix: str, callback: Callable[[], dict]) -> None:
    if not _acquire_lock(prefix, STAGE_APPLY):
        st.warning("反映処理は実行中です.")
        return
    try:
        with st.status("eBay ReviseItem 実行中...", expanded=False) as _s:
            try:
                result = callback()
            except Exception as e:  # noqa: BLE001
                logger.exception("apply callback 例外")
                result = {"success": False, "message": f"callback 例外: {type(e).__name__}: {e}"}
            st.session_state[f"{prefix}{_SUFFIX_LAST_APPLY}"] = result
            if result.get("success"):
                _s.update(label="✅ 反映成功", state="complete")
            else:
                _s.update(label=f"❌ 反映失敗: {result.get('message', '')[:80]}", state="error")
    finally:
        _release_lock(prefix, STAGE_APPLY)


def _render_apply_result(result: dict) -> None:
    """W158 sequencer の戻値を per-step 表示 (HIGH-Codex-4 Q0 透明性)."""
    success = result.get("success")
    updated = result.get("updated") or {}
    attempted = result.get("attempted") or {}
    skipped_reason = result.get("skipped_reason")

    if success:
        st.success(result.get("message", "✅ 反映成功"))
    else:
        # per-step status: description / pictures
        if attempted.get("description"):
            if updated.get("description"):
                st.markdown("- ✅ 説明文反映済")
            else:
                desc_r = result.get("description_result") or {}
                st.markdown(f"- ❌ 説明文反映失敗: {desc_r.get('message') or 'unknown'}")
        if attempted.get("pictures"):
            if updated.get("pictures"):
                st.markdown("- ✅ 画像反映済")
            else:
                pic_r = result.get("pictures_result") or {}
                st.markdown(f"- ❌ 画像反映失敗: {pic_r.get('message') or 'unknown'}")
        else:
            if skipped_reason == "description_failed_early_return":
                st.markdown(
                    "- ⏭️ 画像反映は **未実行** (説明文先実行 → 失敗で stop). "
                    "説明文を修正後、「📷 画像だけ反映」 button で再実行可能."
                )
        if skipped_reason and skipped_reason not in ("description_failed_early_return",):
            st.caption(f"skipped_reason: {skipped_reason}")


__all__ = [
    "render_image_pipeline_section",
    "clear_pipeline_keys",
    "STAGE_HERO",
    "STAGE_ADDITIONAL",
    "STAGE_EPS",
    "STAGE_APPLY",
]
