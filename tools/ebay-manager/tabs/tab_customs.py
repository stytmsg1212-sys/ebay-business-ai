#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通関対応 (FedEx/DHL/UPS 通関情報提出ドラフト生成・送信) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "通関対応":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
"""
from __future__ import annotations

import logging
from pathlib import Path
import streamlit as st

logger = logging.getLogger(__name__)


def render_customs_tab() -> None:
    import json as _json_cust
    from monitor.database import get_conn as _cust_conn

    st.title("通関対応")
    st.caption(
        "FedEx / DHL / UPS からの通関情報提出要求を自動検知し、英文ドラフトを生成。"
        " 内容を確認の上、送信ボタンで Gmail API 経由で送信します。"
    )

    # ── 最新情報取得ボタン ──
    # daily_scheduler の 06:10 朝バッチを待たずに、user 操作で即時 Gmail から
    # 通関メールを再取得する. 過去 7 日範囲を再 scan, 新規のみ DB に追加 (gmail_id UNIQUE).
    _refresh_col_a, _refresh_col_b = st.columns([1, 4])
    with _refresh_col_a:
        _refresh_btn = st.button(
            "🔄 最新情報取得",
            type="primary",
            key="customs_refresh_btn",
            help="Gmail から最新の通関メールを取得 (過去 7 日)。約 1-3 分。",
        )
    with _refresh_col_b:
        st.caption(
            "前回自動取得: 毎朝 06:10 / 手動実行可能"
        )
    if _refresh_btn:
        with st.spinner("Gmail から通関メール取得中... (1-3 分)"):
            try:
                from tasks.task_customs_check import run_customs_check
                import json as _json_refresh
                import io as _io_refresh
                with _io_refresh.open(
                    "config/schedule_config.json", encoding="utf-8"
                ) as _scf:
                    _refresh_cfg = _json_refresh.load(_scf)
                _r = run_customs_check(_refresh_cfg, days=7)
                if _r.get("success"):
                    st.success(
                        f"取得完了: 検知 {_r.get('detected', 0)} 件、"
                        f"ドラフト生成 {_r.get('drafted', 0)} 件、"
                        f"手動対応要 {_r.get('manual', 0)} 件"
                    )
                else:
                    st.error(f"取得失敗: {_r.get('message','unknown')}")
            except Exception as _re:
                st.error(f"エラー: {type(_re).__name__}: {_re}")
            st.rerun()

    # ── Subtabs ──
    _cust_tab_pending, _cust_tab_sent, _cust_tab_manual, _cust_tab_kb = st.tabs([
        "要対応", "送信済み", "手動対応要", "KB承認",
    ])

    # ── 要対応 (drafted) ──
    with _cust_tab_pending:
        with _cust_conn() as _cc:
            _pending = [dict(r) for r in _cc.execute(
                """SELECT * FROM customs_requests
                   WHERE status IN ('drafted', 'drafted_no_photo',
                                     'drafted_in_gmail')
                   ORDER BY deadline ASC NULLS LAST, detected_at DESC"""
            ).fetchall()]
        st.markdown(f"**要対応: {len(_pending)} 件**")
        if not _pending:
            st.info(
                "現在、要対応の通関案件はありません。"
                "毎朝 06:10 に FedEx/DHL/UPS から新規案件を自動検知します。"
            )
        for _req in _pending:
            _deadline = _req.get("deadline") or ""
            _days_left = ""
            if _deadline:
                try:
                    from datetime import datetime as _dt_cust
                    _d = _dt_cust.strptime(_deadline, "%Y-%m-%d")
                    _diff = (_d - _dt_cust.now()).days
                    _days_left = f" (残 {_diff} 日)" if _diff >= 0 else f" (期限 {abs(_diff)} 日超過)"
                except ValueError:
                    pass
            _deadline_color = "#ff6060" if "超過" in _days_left else (
                "#ffa84a" if "残 0" in _days_left or "残 1" in _days_left
                else "#cccccc"
            )
            _header = (
                f"**[{_req['carrier'].upper()}]** "
                f"TRK# {_req.get('tracking_number') or '(不明)'}  "
                f":green[{_req.get('product_title') or '(商品未特定)'}]"
            )
            st.markdown(
                f'<div style="border-left:3px solid {_deadline_color};padding:6px 12px;'
                f'margin:6px 0;background:rgba(166,150,121,0.06);">'
                f'{_header}<br>'
                f'<span style="color:{_deadline_color};font-size:12px;">'
                f'期限: {_deadline or "不明"}{_days_left}</span>  '
                f'<span style="color:#8d927f;font-size:11px;">'
                f'| 検知: {_req.get("detected_at","")[:16]}</span>'
                f'</div>', unsafe_allow_html=True)
            with st.expander(f"ドラフト内容を確認 (#{_req['id']})", expanded=False):
                # 送信先
                try:
                    _recips = _json_cust.loads(_req.get("draft_recipients") or "{}")
                except Exception:
                    _recips = {}
                _to_list = _recips.get("to") or []
                _cc_list = _recips.get("cc") or []
                st.markdown(
                    f"**TO**: :red[**{', '.join(_to_list) if _to_list else '(未解決)'}**]  "
                    f"**CC**: {', '.join(_cc_list) if _cc_list else '(なし)'}"
                )
                st.markdown(f"**Subject**: `{_req.get('draft_subject','')}`")
                st.markdown("**Body** (英文):")
                st.code(_req.get("draft_body") or "", language="text")
                # 添付
                try:
                    _photos = _json_cust.loads(_req.get("attached_photos") or "[]")
                except Exception:
                    _photos = []
                if _photos:
                    st.markdown(f"**添付写真**: {len(_photos)} 枚")
                    _pc = st.columns(min(len(_photos), 5))
                    for _i, _p in enumerate(_photos[:5]):
                        try:
                            _pc[_i].image(_p, width=120)
                        except Exception:
                            _pc[_i].caption(Path(_p).name)
                else:
                    st.warning("添付写真なし (status: drafted_no_photo) — 送信前に user が手動で追加する必要があります")
                # 補助情報
                _warnings = _req.get("error_msg")
                if _warnings:
                    st.warning(f"注意: {_warnings}")
                try:
                    _kb = _json_cust.loads(_req.get("kb_hits") or "{}")
                    st.caption(
                        f"メーカー KB: {_kb.get('manufacturer') or '(未解決)'} "
                        f"/ HTS: {_kb.get('hts') or '(未解決)'} "
                        f"/ テンプレ: {_req.get('template_used') or '(なし)'}"
                    )
                except Exception as _kb_e:
                    logger.debug("通関 KB 表示 skip: %s", _kb_e)

                # ── W14ext (2026-04-25): 「送信準備」= Gmail 下書き作成 ──
                # フロー:
                #   status='drafted' or 'drafted_no_photo' (Gmail 未保存):
                #     → 「送信準備」ボタンで Gmail draft 作成
                #     → status='drafted_in_gmail'
                #   status='drafted_in_gmail' (Gmail に下書き保存済):
                #     → 「Gmail で確認」リンク + 「送信」ボタン (赤、2 段階確認)
                #     → 送信完了で status='sent'
                _existing_status = _req.get("status") or ""
                _draft_gmail_id = (_req.get("draft_gmail_id") or "").strip()

                # ── 「✋ 対応済み (手動)」ボタン (全 status 共通) ──
                # MONO Deck 経由で送らず Gmail 直接 or 別手段で対応した場合に
                # 要対応リストから外す.
                # status='sent' + gmail_sent_id=NULL + error_msg にマーカーで記録.
                # 送信済みサブタブには「(手動対応)」ラベル付きで表示される.
                _manual_done_key = f"customs_manual_done_confirm_{_req['id']}"
                if st.session_state.get(_manual_done_key):
                    st.warning("**確認**: この案件を「対応済み」として要対応から外します。")
                    _md_c1, _md_c2 = st.columns(2)
                    with _md_c1:
                        if st.button(
                            "確定",
                            key=f"customs_manual_done_go_{_req['id']}",
                            type="primary",
                        ):
                            from monitor.database import get_conn as _gc_md
                            from datetime import datetime as _dt_md
                            with _gc_md() as _c_md:
                                _c_md.execute(
                                    "UPDATE customs_requests SET status='sent', "
                                    "sent_at=CURRENT_TIMESTAMP, gmail_sent_id=NULL, "
                                    "draft_gmail_id=NULL, draft_lock_at=NULL, "
                                    "error_msg=COALESCE(error_msg,'') || ? "
                                    "WHERE id=?",
                                    (" [manually-handled outside MONO Deck "
                                     f"({_dt_md.now().strftime('%Y-%m-%d %H:%M')})]",
                                     _req["id"]),
                                )
                            st.session_state[_manual_done_key] = False
                            st.success("対応済みにしました")
                            st.rerun()
                    with _md_c2:
                        if st.button(
                            "キャンセル",
                            key=f"customs_manual_done_cancel_{_req['id']}",
                        ):
                            st.session_state[_manual_done_key] = False
                            st.rerun()
                else:
                    if st.button(
                        "✋ 対応済み (手動・要対応から外す)",
                        key=f"customs_manual_done_btn_{_req['id']}",
                        help="Gmail 直接や電話など MONO Deck 外で対応済みの場合"
                             "、このボタンで要対応から外せます。",
                    ):
                        st.session_state[_manual_done_key] = True
                        st.rerun()

                if _existing_status in ("drafted", "drafted_no_photo"):
                    # 段階 1: Gmail 下書きをまだ作っていない → 「送信準備」表示
                    if st.button(
                        "送信準備 (Gmail 下書きとして保存)",
                        key=f"customs_send_prep_{_req['id']}",
                        type="primary",
                    ):
                        try:
                            from monitor.customs_gmail_sender import (
                                CustomsSendBlocked, CustomsSendFailed,
                                create_customs_draft,
                            )
                            import json as _json_prep
                            import io as _io_prep
                            with _io_prep.open(
                                "config/schedule_config.json", encoding="utf-8"
                            ) as _scf:
                                _prep_cfg = _json_prep.load(_scf)
                            _dr = create_customs_draft(
                                _req["id"], config=_prep_cfg,
                            )
                            if _dr.success:
                                st.success(
                                    f"Gmail 下書きを{_dr.action}しました "
                                    f"(draft id: {_dr.draft_gmail_id[:16]}...)"
                                )
                                st.rerun()
                            else:
                                st.error(f"下書き作成失敗: {_dr.error}")
                        except CustomsSendBlocked as _e:
                            st.warning(f"ブロック: {_e}")
                        except CustomsSendFailed as _e:
                            st.error(f"Gmail API 失敗: {_e}")
                        except Exception as _e:
                            st.error(f"予期せぬエラー: {type(_e).__name__}: {_e}")
                elif _existing_status == "drafted_in_gmail":
                    # 段階 2: Gmail 下書き保存済 → 確認 + 送信ボタン
                    _gmail_draft_url = (
                        f"https://mail.google.com/mail/u/0/#drafts/{_draft_gmail_id}"
                        if _draft_gmail_id else ""
                    )
                    st.success("✓ Gmail に下書き保存済み")
                    if _gmail_draft_url:
                        st.markdown(
                            f'<a href="{_gmail_draft_url}" target="_blank" '
                            f'style="color:#156a63;">📧 Gmail で内容を確認する</a>',
                            unsafe_allow_html=True,
                        )

                    _confirm_key = f"customs_send_confirm_{_req['id']}"
                    if not st.session_state.get(_confirm_key):
                        _btn_cols = st.columns(2)
                        with _btn_cols[0]:
                            if st.button(
                                "🔴 送信",
                                key=f"customs_send_go_{_req['id']}",
                                type="primary",
                            ):
                                st.session_state[_confirm_key] = True
                                st.rerun()
                        with _btn_cols[1]:
                            if st.button(
                                "下書きを更新",
                                key=f"customs_draft_update_{_req['id']}",
                                help="本文や宛先を変えた場合は再度「送信準備」で Gmail 下書きを再作成",
                            ):
                                # H-2 対応: draft_gmail_id を NULL にして強制 create.
                                # H-X2 対応: draft_lock_at もクリアして即時再「送信準備」可
                                # 古い Gmail 下書きは Gmail UI で user が削除可能.
                                from monitor.database import get_conn as _gc
                                with _gc() as _c:
                                    _c.execute(
                                        "UPDATE customs_requests SET "
                                        "status='drafted', draft_gmail_id=NULL, "
                                        "draft_lock_at=NULL "
                                        "WHERE id=? AND status='drafted_in_gmail'",
                                        (_req["id"],),
                                    )
                                st.info(
                                    "ステータスを drafted に戻しました。"
                                    "Gmail 上の旧下書きが残っている場合は手動で削除してください。"
                                )
                                st.rerun()
                    else:
                        st.error(
                            "**最終確認** — Gmail で内容を確認しましたか？"
                            "送信すると撤回できません。"
                        )
                        _bc1, _bc2 = st.columns(2)
                        with _bc1:
                            if st.button(
                                "**本当に送信**",
                                key=f"customs_send_final_{_req['id']}",
                                type="primary",
                            ):
                                try:
                                    from monitor.customs_gmail_sender import (
                                        CustomsSendBlocked, CustomsSendFailed,
                                        send_customs_reply,
                                    )
                                    import json as _json_send
                                    import io as _io_send
                                    with _io_send.open(
                                        "config/schedule_config.json",
                                        encoding="utf-8",
                                    ) as _scf:
                                        _send_cfg = _json_send.load(_scf)
                                    _r = send_customs_reply(
                                        _req["id"], config=_send_cfg,
                                    )
                                    if _r.success:
                                        st.success(
                                            f"送信成功: Gmail ID {_r.gmail_sent_id}"
                                        )
                                    else:
                                        st.error(f"送信失敗: {_r.error}")
                                except CustomsSendBlocked as _e:
                                    st.warning(f"送信ブロック: {_e}")
                                except CustomsSendFailed as _e:
                                    st.error(f"Gmail API 失敗: {_e}")
                                except Exception as _e:
                                    st.error(
                                        f"予期せぬエラー: {type(_e).__name__}: {_e}"
                                    )
                                st.session_state[_confirm_key] = False
                                st.rerun()
                        with _bc2:
                            if st.button(
                                "キャンセル",
                                key=f"customs_send_cancel_{_req['id']}",
                            ):
                                st.session_state[_confirm_key] = False
                                st.rerun()

    # ── 送信済み ──
    with _cust_tab_sent:
        with _cust_conn() as _cc:
            _sent = [dict(r) for r in _cc.execute(
                """SELECT id, carrier, tracking_number, product_title,
                          draft_subject, sent_at, gmail_sent_id, error_msg
                   FROM customs_requests
                   WHERE status = 'sent'
                   ORDER BY sent_at DESC LIMIT 50"""
            ).fetchall()]
        st.markdown(f"**送信済み: {len(_sent)} 件**")
        if not _sent:
            st.info("まだ送信済み案件はありません")
        for _s in _sent:
            # 手動対応マーカー判別 (gmail_sent_id NULL かつ error_msg に marker)
            _is_manual = (
                not _s.get("gmail_sent_id")
                and "manually-handled" in (_s.get("error_msg") or "")
            )
            _label = "✋ 手動対応" if _is_manual else "✓ 送信済"
            _detail = (
                "MONO Deck 経由ではない (user 手動対応)"
                if _is_manual
                else f"Gmail ID: {_s.get('gmail_sent_id')}"
            )
            st.markdown(
                f'- **{_label}** [{_s["carrier"].upper()}] '
                f'TRK# {_s.get("tracking_number") or "?"}  '
                f'{_s.get("product_title") or ""}  \n'
                f'  :gray[完了: {(_s.get("sent_at") or "")[:16]} / {_detail}]'
            )

    # ── 手動対応要 (manual / failed) ──
    with _cust_tab_manual:
        with _cust_conn() as _cc:
            _manual = [dict(r) for r in _cc.execute(
                """SELECT * FROM customs_requests
                   WHERE status IN ('manual', 'failed')
                   ORDER BY detected_at DESC LIMIT 50"""
            ).fetchall()]
        st.markdown(f"**手動対応要 / 失敗: {len(_manual)} 件**")
        if not _manual:
            st.info("手動対応が必要な案件はありません")
        for _m in _manual:
            st.markdown(
                f'- [{_m["carrier"].upper()}] status={_m["status"]} '
                f'TRK# {_m.get("tracking_number") or "?"}  \n'
                f'  :orange[{_m.get("error_msg","")[:200]}]'
            )

    # ── KB 承認 (customs_kb_pending) ──
    with _cust_tab_kb:
        st.markdown("**承認待ち KB エントリ** (Tier 2/3 web 検索結果)")
        try:
            from monitor.customs_kb import (
                approve_kb_entry, list_pending_kb, reject_kb_entry,
            )
            _kb_pending = list_pending_kb()
        except Exception as _e:
            _kb_pending = []
            st.warning(f"KB pending load failed: {_e}")
        if not _kb_pending:
            st.info("承認待ちエントリはありません")
        for _kp in _kb_pending:
            with st.expander(
                f"[{_kp['kind']}] {_kp['brand_or_category']} ({_kp['created_at'][:10]})"
            ):
                try:
                    _pj = _json_cust.loads(_kp.get("proposed_json") or "{}")
                    st.json(_pj)
                except Exception:
                    st.code(_kp.get("proposed_json") or "")
                if _kp.get("source_url"):
                    st.caption(f"Source: {_kp['source_url']}")
                _akc1, _akc2 = st.columns(2)
                with _akc1:
                    if st.button(
                        "承認 (KB 昇格)", key=f"kb_approve_{_kp['id']}",
                        type="primary",
                    ):
                        if approve_kb_entry(_kp["id"]):
                            st.success("承認 → customs_kb.json に追加")
                            st.rerun()
                        else:
                            st.error("承認失敗")
                with _akc2:
                    if st.button("却下", key=f"kb_reject_{_kp['id']}"):
                        if reject_kb_entry(_kp["id"]):
                            st.info("却下しました")
                            st.rerun()
