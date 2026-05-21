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

# 2026-05-21 ランク手動 override UI 用 (個別出品 tab_individual_listing と同セット)。
# 重複だが K1 (3 回出てから共通化) 範囲、tab_individual との結合を作らない方が
# 安全 (UI モジュール間の隠れ依存防止)。
_RANK_CHOICES: tuple[str, ...] = ('N', 'S', 'A', 'B', 'C', 'D', 'PO', 'As-Is')
_RANK_LABEL_HINTS: dict[str, str] = {
    "N":     "N — 新品未開封 (シュリンク付き)",
    "S":     "S — 新品同様 (開封済・未使用)",
    "A":     "A — 美品 (小傷、全機能動作)",
    "B":     "B — 並品 (目立つ使用痕、全機能動作)",
    "C":     "C — 使用感あり (強い使用痕、全機能動作)",
    "D":     "D — 難あり (機能限定で動作)",
    "PO":    "PO — 通電のみ (動作未確認)",
    "As-Is": "As-Is — 未確認 / 部品取り (無保証)",
}


def prefetch_supplier_product_and_rank(
    candidate_id: int, candidate_url: str,
) -> dict:
    """2026-05-21 user 要望: section open 時に scrape + rank classify を自動
    実行して結果を返す。UI 層が session_state にキャッシュし rerun でも
    再実行しない (~10-15s のコストを 1 回のみ支払う)。

    Returns:
        {'success': bool,
         'product': ScrapedProduct or None,
         'rank_code': str,        # Claude 推定 (失敗時 '')
         'rank_label': str,
         'rank_confidence': float,
         'rank_reasoning': str,
         'message': str}
    """
    from monitor.supplier_scraper import scrape_supplier_url
    from monitor.rank_classifier import classify_rank

    out = {
        'success': False, 'product': None,
        'rank_code': '', 'rank_label': '', 'rank_confidence': 0.0,
        'rank_reasoning': '', 'message': '',
    }
    try:
        product = scrape_supplier_url(candidate_url, timeout_sec=15)
    except Exception as e:
        logger.exception("prefetch scrape failed cid=%s", candidate_id)
        out['message'] = f'スクレイプ失敗: {type(e).__name__}: {e}'
        return out
    if not product or not getattr(product, 'title_ja', None):
        out['message'] = 'スクレイプ結果が空 (URL を再確認してください)'
        return out

    try:
        rank = classify_rank(
            supplier_condition_ja=getattr(product, 'condition_ja', '') or '',
            supplier_description_ja=getattr(product, 'description_ja', None),
            supplier_title_ja=getattr(product, 'title_ja', None),
        )
    except Exception as e:
        logger.exception("prefetch rank failed cid=%s", candidate_id)
        # scrape は成功しているので product だけでも返す (rank は手動入力可)
        out['product'] = product
        out['message'] = f'rank classify 失敗 (手動指定で続行可): {type(e).__name__}: {e}'
        return out

    out.update({
        'success': True,
        'product': product,
        'rank_code': rank.rank_code,
        'rank_label': getattr(rank, 'rank_label', '') or '',
        'rank_confidence': float(getattr(rank, 'confidence', 0.0) or 0.0),
        'rank_reasoning': getattr(rank, 'reasoning', '') or '',
        'message': (
            f'スクレイプ + 自動ランク推定 完了 (ランク={rank.rank_code}, '
            f'confidence={float(getattr(rank, "confidence", 0.0)):.2f})'
        ),
    })
    return out


def generate_supplier_description(
    candidate_id: int,
    candidate_url: str,
    in_stock: bool = False,
    template_id: Optional[int] = None,
    prefetched_product=None,
    rank_override_code: Optional[str] = None,
) -> dict:
    """仕入先 URL から description HTML を生成 (eBay 反映はしない、純生成のみ).

    2026-05-21 user 要望: prefetched_product / rank_override_code を受け取り
    section open 時の事前取得結果を再利用 + user 手動 rank 上書き対応。
    両方 None なら旧挙動 (内部で scrape + auto-classify)。

    Returns:
        {'success': bool,
         'description_html': str,    # 生成された HTML body
         'rank_code': str,           # 使用したランク (override or 自動)
         'title_en': str,            # Claude 生成 英語タイトル (preview 用)
         'message': str}
    """
    from monitor.supplier_scraper import scrape_supplier_url
    from monitor.rank_classifier import classify_rank, _build_result
    from monitor.listing_generator import generate_listing
    from monitor.database import (
        get_description_templates, get_description_template,
    )

    # Step 1: scrape (prefetched があれば再利用)
    product = prefetched_product
    if product is None:
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

    # Step 2: rank (override > auto-classify)
    if rank_override_code and rank_override_code in _RANK_CHOICES:
        # user 手動指定 → RankClassification を組み立て (confidence=1.0, manual reasoning)
        try:
            rank = _build_result(
                rank_override_code,
                confidence=1.0,
                reasoning='manual override (user 指定)',
            )
        except Exception as e:
            logger.exception("manual rank build failed cid=%s", candidate_id)
            return {
                'success': False,
                'message': f'manual rank build 失敗: {type(e).__name__}: {e}',
                'description_html': '', 'rank_code': '', 'title_en': '',
            }
    else:
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
    sk_prefetch = f"{_SS}prefetch_{candidate_id}"          # 2026-05-21: scrape+rank キャッシュ
    sk_rank_override = f"{_SS}rank_override_{candidate_id}"  # 2026-05-21: user 手動 rank

    with st.container(border=True):
        st.markdown(
            f'<div style="font-size:11px;color:rgba(180,255,200,0.7);'
            f'letter-spacing:2px;margin:8px 0 6px;">'
            f'description 反 映 　 — 　 候補 #{candidate_id} → '
            f'商品ID {ebay_item_id}</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"対象商品: {candidate_title[:60]}")

        # 2026-05-21 user 要望: section 展開時に自動 scrape + rank classify を実行
        # → 結果を session_state にキャッシュ (rerun で再実行しない、~10-15s/回)。
        prefetch = st.session_state.get(sk_prefetch)
        if prefetch is None:
            with st.spinner(
                "仕入先 URL からスクレイプ + Claude Haiku ランク推定 中 (~10-15秒)..."
            ):
                prefetch = prefetch_supplier_product_and_rank(
                    candidate_id, candidate_url,
                )
            st.session_state[sk_prefetch] = prefetch
            st.rerun()  # rank UI を 1 回目 render で確実に出すため再 render

        if not prefetch.get('success') and not prefetch.get('product'):
            # scrape 自体が失敗 (product 取れず) → 再試行のみ可
            st.error(f"❌ {prefetch.get('message') or 'prefetch 失敗'}")
            if st.button(
                "🔄 prefetch 再試行", key=f"{_SS}btn_prefetch_retry_{candidate_id}",
            ):
                if sk_prefetch in st.session_state:
                    del st.session_state[sk_prefetch]
                st.rerun()
            return

        # ── ランク UI (個別出品同様: Claude 推定 default + selectbox 上書き可) ──
        auto_rank = prefetch.get('rank_code') or ''
        auto_conf = prefetch.get('rank_confidence') or 0.0
        auto_reasoning = prefetch.get('rank_reasoning') or ''
        if auto_rank:
            st.success(
                f"🔍 自動推定ランク: **{auto_rank}** "
                f"({_RANK_LABEL_HINTS.get(auto_rank, auto_rank)}) / "
                f"confidence {auto_conf:.0%}"
            )
            if auto_reasoning:
                with st.expander("Claude 判定理由を見る", expanded=False):
                    st.caption(auto_reasoning)
        else:
            st.warning(
                f"⚠️ 自動ランク推定失敗: {prefetch.get('message') or '不明'}。"
                f"下のセレクトで手動指定してください。"
            )

        # default index: Claude 自動 (index 0) or 既存 session 値
        _rank_options = ["(Claude 自動推定を使う)"] + list(_RANK_CHOICES)
        _cur_override = st.session_state.get(sk_rank_override) or ""
        _default_idx = 0
        if _cur_override in _RANK_CHOICES:
            _default_idx = _rank_options.index(_cur_override)

        def _fmt_rank(i: int) -> str:
            opt = _rank_options[i]
            if opt == "(Claude 自動推定を使う)":
                return f"{opt} = {auto_rank or '推定失敗'}"
            return _RANK_LABEL_HINTS.get(opt, opt)

        _rank_sel = st.selectbox(
            "ランク (手動上書き可能、未指定なら Claude 推定を使用)",
            options=list(range(len(_rank_options))),
            format_func=_fmt_rank,
            index=_default_idx,
            key=f"{_SS}sel_rank_{candidate_id}",
        )
        _rank_override_chosen = (
            _rank_options[_rank_sel]
            if _rank_sel > 0 else ''
        )
        st.session_state[sk_rank_override] = _rank_override_chosen
        # 実際に generate で使う rank (override > auto)
        _effective_rank = _rank_override_chosen or auto_rank

        gen_result = st.session_state.get(sk_result)
        sk_gen_lock = f"{_SS}gen_lock_{candidate_id}"
        _is_generating = bool(st.session_state.get(sk_gen_lock, False))

        # Step 1: 未生成 → 生成ボタン (in-flight lock で Claude 二重課金防止)
        if not gen_result:
            if not _effective_rank:
                st.error("❌ ランクが決まっていません (Claude 推定失敗 + 手動指定なし)")
                return
            cols = st.columns([1.8, 4])
            with cols[0]:
                if _is_generating:
                    st.caption("⏳ 生成処理中... (二度押し防止)")
                elif st.button(
                    f"📝 description を生成 (rank={_effective_rank})",
                    key=f"{_SS}btn_gen_{candidate_id}",
                    type="primary",
                ):
                    st.session_state[sk_gen_lock] = True
                    try:
                        with st.spinner(
                            f"Claude Sonnet で description 生成中 "
                            f"(rank={_effective_rank}, ~30-60 秒)..."
                        ):
                            res = generate_supplier_description(
                                candidate_id=candidate_id,
                                candidate_url=candidate_url,
                                in_stock=False,
                                prefetched_product=prefetch.get('product'),
                                rank_override_code=_effective_rank,
                            )
                        st.session_state[sk_result] = res
                    finally:
                        st.session_state[sk_gen_lock] = False
                    st.rerun()
            with cols[1]:
                st.caption(
                    "(個別出品と同じ Claude パイプラインで description を生成。"
                    "テンプレは default を自動選択。ランクは上のセレクトで上書き可)"
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
