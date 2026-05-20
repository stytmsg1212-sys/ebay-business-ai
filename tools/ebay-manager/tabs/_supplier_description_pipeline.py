"""W148-X (2026-05-20 user 緊急要望): 仕入先候補「採用」後の description
生成+反映 pipeline.

個別出品の description 生成 (tab_individual_listing._do_generate) と同等の
処理を、既存 listing 向け ReviseItem 経路 (revise_item_description) で動かす。

flow:
    1. scrape_supplier_url(candidate_url) → ScrapedProduct
    2. classify_rank → RankClassification
    3. get_description_template (is_default 優先、無ければ先頭)
    4. generate_listing(product, reference=None, rank, template_body,
                        in_stock=False, config=settings) → ebay_description
    5. (UI 確認後) revise_item_description で eBay 反映

K1 Simplicity: reference listing は使わない (supplier_candidates 経路は既存
listing の置き換えなので別 reference は不要)、template は default を自動選択
(個別出品では user 選択だが本 flow は採用直後 quick path のため)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as _components

logger = logging.getLogger(__name__)

_SS = "sup_desc_pipeline_"


def generate_supplier_description(
    candidate_id: int,
    candidate_url: str,
    in_stock: bool = False,
    template_id: Optional[int] = None,
) -> dict:
    """仕入先 URL から description HTML を生成 (eBay 反映はしない、純生成のみ).

    Returns:
        {'success': bool,
         'description_html': str,    # 生成された HTML body
         'rank_code': str,           # 推定ランク (N/S/A/B/C/D/PO/As-Is)
         'title_en': str,            # Claude 生成 英語タイトル (preview 用)
         'message': str}
    """
    from monitor.supplier_scraper import scrape_supplier_url
    from monitor.rank_classifier import classify_rank
    from monitor.listing_generator import generate_listing
    from monitor.database import (
        get_description_templates, get_description_template,
    )

    # Step 1: scrape
    try:
        product = scrape_supplier_url(candidate_url, timeout_sec=15)
    except Exception as e:
        logger.exception("scrape_supplier_url failed cid=%s", candidate_id)
        return {
            'success': False,
            'message': f'スクレイプ失敗: {type(e).__name__}: {e}',
            'description_html': '', 'rank_code': '', 'title_en': '',
        }

    if not product or not getattr(product, 'title_ja', None):
        return {
            'success': False,
            'message': 'スクレイプ結果が空 (URL を再確認してください)',
            'description_html': '', 'rank_code': '', 'title_en': '',
        }

    # Step 2: rank classify
    try:
        rank = classify_rank(
            supplier_condition_ja=getattr(product, 'condition_ja', '') or '',
            supplier_description_ja=getattr(product, 'description_ja', None),
            supplier_title_ja=getattr(product, 'title_ja', None),
        )
    except Exception as e:
        logger.exception("classify_rank failed cid=%s", candidate_id)
        return {
            'success': False,
            'message': f'rank classify 失敗: {type(e).__name__}: {e}',
            'description_html': '', 'rank_code': '', 'title_en': '',
        }

    # Step 3: template (auto-select default、無ければ先頭)
    if template_id is None:
        try:
            templates = get_description_templates()
        except Exception as e:
            logger.exception("get_description_templates failed")
            return {
                'success': False,
                'message': f'description テンプレ取得失敗: {e}',
                'description_html': '', 'rank_code': rank.rank_code,
                'title_en': '',
            }
        if not templates:
            return {
                'success': False,
                'message': (
                    'description テンプレが未登録です。'
                    '個別出品タブの「テンプレート設定」で 1 件以上作成してください。'
                ),
                'description_html': '', 'rank_code': rank.rank_code,
                'title_en': '',
            }
        default_tpl = next(
            (t for t in templates if t.get('is_default')),
            templates[0],
        )
        template_id = default_tpl['id']

    try:
        tpl = get_description_template(int(template_id))
    except Exception as e:
        logger.exception("get_description_template failed id=%s", template_id)
        return {
            'success': False,
            'message': f'template id={template_id} 取得失敗: {e}',
            'description_html': '', 'rank_code': rank.rank_code,
            'title_en': '',
        }
    if not tpl:
        return {
            'success': False,
            'message': f'template id={template_id} が DB に存在しません',
            'description_html': '', 'rank_code': rank.rank_code,
            'title_en': '',
        }
    template_body = tpl.get('body') or ''

    # Step 4: settings.json load (handling/delivery 日付反映用)
    cfg_path = (
        Path(__file__).resolve().parent.parent
        / 'config' / 'schedule_config.json'
    )
    config: Optional[dict] = None
    if cfg_path.exists():
        try:
            config = json.loads(cfg_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("schedule_config.json 読込失敗 (continue): %s", e)

    # Step 5: generate_listing (reference=None で純粋に仕入先データから生成)
    try:
        gl = generate_listing(
            product=product,
            reference=None,
            rank=rank,
            template_body=template_body,
            in_stock=in_stock,
            config=config,
        )
    except Exception as e:
        logger.exception("generate_listing failed cid=%s", candidate_id)
        return {
            'success': False,
            'message': f'generate_listing 失敗: {type(e).__name__}: {e}',
            'description_html': '', 'rank_code': rank.rank_code,
            'title_en': '',
        }

    if getattr(gl, 'generate_error', None):
        return {
            'success': False,
            'message': f'Claude 生成エラー: {gl.generate_error}',
            'description_html': '', 'rank_code': rank.rank_code,
            'title_en': '',
        }

    desc = gl.ebay_description or ''
    if not desc.strip():
        return {
            'success': False,
            'message': '生成された description が空 (Claude 応答 or テンプレ展開に問題)',
            'description_html': '', 'rank_code': rank.rank_code,
            'title_en': getattr(gl, 'title_en', '') or '',
        }

    return {
        'success': True,
        'description_html': desc,
        'rank_code': rank.rank_code,
        'title_en': getattr(gl, 'title_en', '') or '',
        'message': (
            f'description 生成成功 (rank={rank.rank_code}, '
            f'{len(desc)} 文字)'
        ),
    }


def apply_description_to_ebay(
    ebay_item_id: str, description_html: str,
) -> dict:
    """生成済 description を eBay にReviseItem で反映.

    Returns: {'success': bool, 'message': str, 'description_len': int}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_client import revise_item_description

    creds = get_ebay_credentials()
    if not ebay_credentials_ok(creds):
        return {
            'success': False,
            'message': (
                'eBay credentials not configured '
                '(env var 設定 + OAuth 完了確認)'
            ),
            'description_len': len(description_html or ''),
        }

    return revise_item_description(
        item_id=ebay_item_id,
        description_html=description_html,
        app_id=creds['app_id'],
        dev_id=creds['dev_id'],
        cert_id=creds['cert_id'],
        user_token=creds['user_token'],
    )


def render_supplier_description_section(
    candidate_id: int,
    candidate_url: str,
    ebay_item_id: str,
    candidate_title: str,
) -> None:
    """採用直後 prompt 経由で開かれる description 生成+反映 UI section.

    flow:
        1. 「📝 生成」ボタン → spinner (scrape→rank→Claude) → preview
        2. preview 表示 → 「✅ eBay に反映」 → ReviseItem → success/error
        3. 「🔄 再生成」 / 「✖ 閉じる」 で session_state クリア

    K1: 単一テンプレ (default) 自動選択。手動選択が要れば個別出品タブで生成
    してから手動コピペ運用 (本 quick path の主旨は「採用直後に最小操作で
    description を更新する」)。
    """
    sk_result = f"{_SS}gen_result_{candidate_id}"
    sk_apply_result = f"{_SS}apply_result_{candidate_id}"

    with st.container(border=True):
        st.markdown(
            f'<div style="font-size:11px;color:rgba(180,255,200,0.7);'
            f'letter-spacing:2px;margin:8px 0 6px;">'
            f'description 反 映 　 — 　 候補 #{candidate_id} → '
            f'商品ID {ebay_item_id}</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"対象商品: {candidate_title[:60]}")

        gen_result = st.session_state.get(sk_result)

        # Step 1: 未生成 → 生成ボタン (Codex 2026-05-20 MEDIUM 対応: in-flight
        # lock で double-trigger による Claude 二重課金を物理防止。既存 採用
        # button の _sup_lock_{cid} と同型パターン)。
        sk_gen_lock = f"{_SS}gen_lock_{candidate_id}"
        _is_generating = bool(st.session_state.get(sk_gen_lock, False))
        if not gen_result:
            cols = st.columns([1.8, 4])
            with cols[0]:
                if _is_generating:
                    st.caption("⏳ 生成処理中... (二度押し防止)")
                elif st.button(
                    "📝 description を生成",
                    key=f"{_SS}btn_gen_{candidate_id}",
                    type="primary",
                ):
                    st.session_state[sk_gen_lock] = True
                    try:
                        with st.spinner(
                            "仕入先 URL スクレイプ → ランク推定 → Claude 生成 "
                            "(~30-60 秒)..."
                        ):
                            res = generate_supplier_description(
                                candidate_id=candidate_id,
                                candidate_url=candidate_url,
                                in_stock=False,  # supplier_candidate は無在庫前提
                            )
                        st.session_state[sk_result] = res
                    finally:
                        st.session_state[sk_gen_lock] = False
                    st.rerun()
            with cols[1]:
                st.caption(
                    "(個別出品と同じ Claude パイプラインで description を生成。"
                    "テンプレは default を自動選択)"
                )
            return

        # Step 2: 生成失敗 → エラー + 再試行
        if not gen_result.get('success'):
            st.error(f"❌ 生成失敗: {gen_result.get('message')}")
            if st.button(
                "🔄 再試行", key=f"{_SS}btn_retry_{candidate_id}",
            ):
                if sk_result in st.session_state:
                    del st.session_state[sk_result]
                st.rerun()
            return

        # Step 3: 生成成功 → preview + apply UI
        desc = gen_result.get('description_html') or ''
        st.success(
            f"✅ 生成成功 — rank={gen_result.get('rank_code')} / "
            f"title_en='{(gen_result.get('title_en') or '')[:60]}' / "
            f"description {len(desc)} 文字"
        )

        with st.expander("▼ description プレビュー (HTML レンダリング)", expanded=True):
            try:
                _components.html(desc, height=400, scrolling=True)
            except Exception as e:
                st.error(f"プレビュー描画失敗: {e}")
                st.code(desc[:2000], language='html')

        # apply 結果 (前回 click 後の永続表示)
        apply_result = st.session_state.get(sk_apply_result)
        if apply_result:
            if apply_result.get('success'):
                st.success(f"✅ {apply_result.get('message')}")
            else:
                st.error(f"❌ {apply_result.get('message')}")

        cols2 = st.columns([1.5, 1.5, 4])
        with cols2[0]:
            if st.button(
                "✅ eBay に反映",
                key=f"{_SS}btn_apply_{candidate_id}",
                type="primary",
                disabled=bool(apply_result and apply_result.get('success')),
            ):
                with st.spinner("eBay ReviseItem 実行中..."):
                    ar = apply_description_to_ebay(ebay_item_id, desc)
                st.session_state[sk_apply_result] = ar
                st.rerun()
        with cols2[1]:
            if st.button(
                "🔄 再生成", key=f"{_SS}btn_regen_{candidate_id}",
            ):
                for k in (sk_result, sk_apply_result):
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
