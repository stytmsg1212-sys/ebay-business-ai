"""採用後フォローアップ欄 (写真/description プロンプト) の共有 render。

2026-06-12 依頼ボード#11: 在庫監視タブの採用 (チェックボックス+一括実行) でも
仕入先候補タブと同じ「写真も反映する？ / description も生成する？」欄を
展開してほしい、という user 要望。tab_supplier_candidates.py に inline で
あった followup ブロック (2026-05-20 W115/W148-X 由来) を本モジュールへ
そのまま移設し、両タブから呼ぶ (2 箇所目の実利用 = 複製回避の抽出)。

呼び出し側の責務:
  - 採用 (apply_supplier_candidate 成功) 時に session_state へ
    `_sup_photo_prompt_{cid}` / `_sup_desc_prompt_{cid}` = True を set
    (meta `_sup_photo_meta_{cid}` は任意 — 無ければ本 render が DB 補完)
  - ページ本文の先頭付近で render_supplier_followup_section(source_tab=...) を呼び、
    True が返ったら区切り線等を描く
    (source_tab は "inventory"|"supplier" — 統一「商品仕上げパネル」
    (tabs/_finishing_panel.py) のコンテンツ既定開閉に使われる、W314 Phase 2 S6)

W314 Phase 2 S6 (2026-07-03): 写真反映 (📷 プロンプト) + タイトル編集小節は
統一「商品仕上げパネル」(`render_finishing_panel`) に一本化された。
description の AI 生成プロンプト (📝) のみ本モジュールに残る
(パネルには AI 生成機能が無いため)。
"""
from __future__ import annotations

import logging

import streamlit as st

logger = logging.getLogger(__name__)


def _close_supplier_followup(cid: int) -> None:
    """採用後フォローアップ欄を商品単位で完全クローズ (バグ3 root fix).

    実装は tabs._supplier_followup_state.close_supplier_followup_state に委譲。
    st.session_state を渡す thin wrapper。
    """
    from tabs._supplier_followup_state import close_supplier_followup_state
    close_supplier_followup_state(st.session_state, cid)


def _render_followup_title_subsection(cid: int, eid: str) -> None:
    """採用後フォローアップ欄の「✏️ タイトルも直す」サブセクション (W314 Phase 1 S3).

    仕入先候補「採用」後にタイトルも直したい (今回要望の核心) に対応。
    現行タイトルを ebay_listings から取得して text_input に初期表示、
    変更があれば「eBay へ反映」ボタンが活性化する (dirty-flag、
    tab_product_management.py の title 編集パターンを踏襲)。
    """
    from tabs._supplier_followup_state import (
        apply_followup_title_to_ebay,
        detect_origin_risk_words,
        title_is_dirty,
    )

    _title_initial = ""
    if eid:
        try:
            from monitor.database import get_conn
            with get_conn() as conn:
                _row = conn.execute(
                    "SELECT title FROM ebay_listings WHERE ebay_item_id=?", (eid,),
                ).fetchone()
            _title_initial = (_row[0] if _row else "") or ""
        except Exception:  # noqa: BLE001 -- UI 補完経路なので例外で section 壊さない
            logger.exception("followup タイトル取得失敗 cid=%s eid=%s", cid, eid)

    with st.expander("✏️ タイトルも直す", expanded=False):
        if not eid or not _title_initial:
            st.caption("listing 情報が取得できないため、ここではタイトルを編集できません。")
            return

        _new_title = st.text_input(
            "商品タイトル (eBay Title / 80 文字以内)",
            value=_title_initial,
            max_chars=80,
            key=f"_sup_title_{cid}",
            help="変更後に「eBay へ反映」を押すと eBay Title を更新します (変更した時のみ)。",
        )
        _new_title = (_new_title or "").strip()
        st.caption(f"{len(_new_title)}/80 文字")

        _risk_words = detect_origin_risk_words(_new_title)
        if _risk_words:
            st.warning(
                "⚠️ 原産国を示唆する表現が含まれています "
                f"({', '.join(_risk_words)})。eBay 出品文への原産国記載は"
                "関税リスクがあるため、記載しないことを推奨します。"
            )

        _dirty = title_is_dirty(_new_title, _title_initial)
        if st.button(
            "📤 eBay へ反映",
            key=f"_sup_title_apply_{cid}",
            type="primary",
            disabled=not _dirty,
        ):
            _result = apply_followup_title_to_ebay(
                eid, _new_title, _title_initial,
                source_tab="followup", candidate_id=cid,
            )
            if _result.get("success"):
                st.success(_result.get("message") or "タイトルを更新しました")
            else:
                st.error(_result.get("message") or "タイトル更新に失敗しました")


def render_supplier_followup_section(source_tab: str) -> bool:
    """採用後の写真/description フォローアップ欄を描画する。

    session_state の `_sup_photo_prompt_` / `_sup_photo_open_inline_` /
    `_sup_desc_prompt_` / `_sup_desc_open_inline_` 各 namespace から
    アクティブな candidate id を収集し、cid 単位の統合ブロックを描画。

    Args:
        source_tab: 呼出元タブ ("inventory"|"supplier")。統一「商品仕上げパネル」
            (`render_finishing_panel`) へそのまま渡し、コンテンツ既定開閉に使う
            (W314 Phase 2 S6)。

    Returns:
        1 件以上描画したら True (呼び出し側が区切り線を引く判断に使う)。
    """
    # ── 2026-05-20 user 緊急要望: 採用後の写真反映 prompt (タブ非依存) ──
    # 採用 button (W112 1-click) が status='applied' に遷移させると候補は
    # 履歴タブに移動するため、user は元のタブを見ていて写真反映ボタンに
    # 気付かない (= 「採用して終わり」になる)。セクション最上部に prompt
    # を出してから、はい押下で個別出品同様のプレート選択フローを inline
    # 展開する (履歴タブへ移動不要)。
    #
    # 2026-06-11 バグ3/4 修正: 2 つの浮動コンテナを cid 単位の統合ブロックに再構成。
    # - バグ3: 完了ボタン1つで cid スコープの session_state を全消し → 欄が消える
    # - バグ4: cid ごとに独立ブロックで sorted → 別商品の干渉がない

    # アクティブな cid を photo / desc 両 namespace から収集
    _followup_cids: set[int] = set()
    for _k in list(st.session_state.keys()):
        if _k.startswith("_sup_photo_prompt_") and st.session_state.get(_k):
            try:
                _followup_cids.add(int(_k.replace("_sup_photo_prompt_", "")))
            except ValueError:
                pass
        elif _k.startswith("_sup_photo_open_inline_") and st.session_state.get(_k):
            try:
                _followup_cids.add(int(_k.replace("_sup_photo_open_inline_", "")))
            except ValueError:
                pass
        elif _k.startswith("_sup_desc_prompt_") and st.session_state.get(_k):
            try:
                _followup_cids.add(int(_k.replace("_sup_desc_prompt_", "")))
            except ValueError:
                pass
        elif _k.startswith("_sup_desc_open_inline_") and st.session_state.get(_k):
            try:
                _followup_cids.add(int(_k.replace("_sup_desc_open_inline_", "")))
            except ValueError:
                pass

    # 2026-06-12 依頼ボード#12: 写真/desc 両方「後でやる」でフォローアップ欄ごと
    # 無言で消え「商品が全消失した」ように見えた → 行き先を 1 回だけ通知
    for _ln in st.session_state.pop("_sup_followup_later_notice", []):
        st.info(
            f"✅ {_ln.get('title') or '(タイトル不明)'} "
            f"(item {_ln.get('eid') or '不明'}) の採用は完了済みです。"
            f"候補は仕入先候補タブの『履歴』に移動しました (一覧から消えるのは正常です)。"
            f"写真反映は仕入先候補タブ『履歴』の「📷 写真反映」、"
            f"description 生成は商品管理タブからいつでもできます。"
        )

    # 同一 listing (ebay_item_id) に候補 2 件以上が同時 followup アクティブになると
    # render_finishing_panel が同じ eid で 2 回呼ばれ、pf_{eid}_* widget key が
    # 重複して StreamlitDuplicateElementKey で followup 全体がクラッシュする
    # (1 listing に複数候補は正常データ。code-reviewer HIGH1 / 2026-07-03)。
    # 対策 = eid 単位で先着 cid のみ描画、後続 cid は caption で明示スキップ (Q0)。
    # 「対応を完了」でフラグが閉じれば次 rerun で次の cid が自然に表示される。
    _seen_eids: set[str] = set()
    _skipped_by_eid: dict[str, list[int]] = {}

    for _fcid in sorted(_followup_cids):
        _fmeta: dict = st.session_state.get(f"_sup_photo_meta_{_fcid}") or {}
        # meta が session_state にない場合は DB から補完 (2026-05-25 防御強化を継承)
        if not _fmeta.get("url"):
            try:
                from monitor.database import get_conn
                with get_conn() as _conn:
                    _row = _conn.execute(
                        "SELECT candidate_url, ebay_item_id, candidate_title "
                        "FROM supplier_candidates WHERE id=?", (_fcid,),
                    ).fetchone()
            except Exception:  # noqa: BLE001 — UI 補完経路なので例外で section 壊さない
                logger.exception("followup meta DB 補完失敗 cid=%s", _fcid)
                _row = None
            if _row and _row[0]:
                _fmeta = {
                    "url": _row[0],
                    "eid": _fmeta.get("eid") or (_row[1] or ""),
                    "title": _fmeta.get("title") or (_row[2] or ""),
                }
                st.session_state[f"_sup_photo_meta_{_fcid}"] = _fmeta

        _f_ttl = (_fmeta.get("title") or "")[:60]
        _f_eid = _fmeta.get("eid") or ""
        _f_url = _fmeta.get("url") or ""

        # HIGH1 fix: 同一 eid 重複描画抑止 (widget key 衝突防止)。
        # eid が空 (meta 補完失敗) は cid が識別子として機能するため衝突しない = 描画する。
        if _f_eid and _f_eid in _seen_eids:
            _skipped_by_eid.setdefault(_f_eid, []).append(_fcid)
            continue
        if _f_eid:
            _seen_eids.add(_f_eid)

        with st.container(border=True):
            st.markdown(
                f'<div style="font-size:11px;color:#b8860b;'
                f'letter-spacing:2px;margin:0 0 8px;">'
                f'採 用 後 フ ォ ロ ー ア ッ プ &nbsp;—&nbsp; '
                f'{_f_ttl} (item {_f_eid})</div>',
                unsafe_allow_html=True,
            )

            # ── 統一パネル (W314 Phase 2 S6): タイトル/画像/ランク/数量を集約 ──
            # 旧「タイトルサブセクション」(_render_followup_title_subsection、S3) と
            # 「写真サブセクション」の Step1 プロンプト/Step2 inline 展開は、この
            # 単一呼出しに統合された (_render_followup_title_subsection 自体は
            # 削除せず温存、パネルのタイトル欄と重複するため followup からは呼ばない)。
            # render_supplier_photo_apply_section はパネル内部からのみ呼ばれる
            # (followup 側で直接呼ばないことで widget key 二重描画を防止)。
            from tabs._finishing_panel import render_finishing_panel
            render_finishing_panel(
                _f_eid, None, candidate_id=_fcid, candidate_url=_f_url,
                source_tab=source_tab,
            )

            # ── description サブセクション ──
            if st.session_state.get(f"_sup_desc_prompt_{_fcid}"):
                # Step 1: prompt
                st.warning(
                    f"📝 採用しました ({_f_ttl} / item {_f_eid})。"
                    f"仕入先 URL から description (HTML 本文) も生成して反映しますか？ "
                    f"(個別出品と同じ Claude パイプライン、~30-60 秒)"
                )
                _dpc = st.columns([1.6, 1.4, 5])
                with _dpc[0]:
                    if st.button(
                        "📝 はい、description も生成",
                        key=f"_sup_desc_yes_{_fcid}", type="primary",
                    ):
                        st.session_state[f"_sup_desc_open_inline_{_fcid}"] = True
                        st.session_state[f"_sup_desc_prompt_{_fcid}"] = False
                        st.rerun()
                with _dpc[1]:
                    if st.button(
                        "いいえ、後でやる",
                        key=f"_sup_desc_no_{_fcid}",
                    ):
                        st.session_state[f"_sup_desc_prompt_{_fcid}"] = False
                        # 依頼ボード#12: photo 側も非アクティブなら欄ごと消える
                        # → 行き先通知を queue
                        if not st.session_state.get(
                            f"_sup_photo_prompt_{_fcid}"
                        ) and not st.session_state.get(
                            f"_sup_photo_open_inline_{_fcid}"
                        ):
                            st.session_state.setdefault(
                                "_sup_followup_later_notice", []
                            ).append({"title": _f_ttl, "eid": _f_eid})
                        st.rerun()

            elif st.session_state.get(f"_sup_desc_open_inline_{_fcid}"):
                # Step 2: opened
                if not _f_url:
                    st.error(
                        f"cid={_fcid}: URL 情報不足 + DB lookup 失敗 → "
                        f"採用やり直しで再 prompt 発生"
                    )
                else:
                    st.markdown(
                        f"**▼ description 反映: {_f_ttl} (item {_f_eid})**"
                    )
                    from tabs._supplier_description_pipeline import (
                        render_supplier_description_section,
                    )
                    # 2026-06-11: close_flag_key 廃止 (✖閉じる 削除、閉じる動線は
                    # 下のフッタ「この商品の対応を完了」に一本化)
                    render_supplier_description_section(
                        candidate_id=_fcid,
                        candidate_url=_f_url,
                        ebay_item_id=_f_eid,
                        candidate_title=_f_ttl,
                    )

            # ── フッタ: 完了ボタン (バグ3 fix: このボタン 1 つで cid 全消し) ──
            st.markdown("---")
            if st.button(
                "この商品の対応を完了 (欄を閉じる)",
                key=f"_sup_followup_done_{_fcid}",
                type="primary",
            ):
                _close_supplier_followup(_fcid)
                st.rerun()

    # HIGH1 fix: eid 単位で先着 cid のみ描画したため、後続 cid は silent 消失させず
    # ユーザーに「上のパネル完了後に表示されます」と明示 (Q0)。
    for _eid, _skipped_cids in _skipped_by_eid.items():
        _cid_list = ", ".join(f"#{c}" for c in _skipped_cids)
        st.caption(
            f"ℹ️ 同一商品 (item {_eid}) の別候補 {_cid_list} は、"
            f"上のパネルで「対応を完了」した後に順次表示されます。"
        )

    return bool(_followup_cids)
