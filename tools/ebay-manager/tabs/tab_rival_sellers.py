"""W#3 ライバルセラー新規出品モニター タブ。

セクション:
  1. 登録セラー一覧 + JP判定状態 + 最終チェック日時
  2. セラー手動登録フォーム
  3. 「今すぐチェック」ボタン
  4. 検知済み listing 一覧

監視対象は日本セラーのみ (JP未確認は警告、自動採用しない)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))


def _to_jst(ts: Optional[str]) -> str:
    """UTC TIMESTAMP → JST 文字列。"""
    if not ts:
        return "—"
    try:
        if "T" in ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return ts


def render_rival_sellers_tab(config: dict) -> None:
    """ライバルセラー監視タブ本体。"""
    st.subheader("ライバルセラー新規出品モニター")
    st.caption(
        "日本の優良eBayセラー(5-15名)を手動登録→新規出品を差分検知→AI評価→Discord通知。"
        " 監視対象は日本セラーのみ (JP未確認は警告)。購入・値下げは一切しない。"
    )

    try:
        from monitor.rival_seller_monitor import (
            list_monitored_sellers,
            add_monitored_seller,
            toggle_seller_active,
            delete_monitored_seller,
            get_recent_detections,
        )
    except ImportError as e:
        st.error(f"rival_seller_monitor import エラー: {e}")
        return

    # ── セクション 1: 登録セラー一覧 ───────────────────────────────────
    st.markdown("### 登録セラー一覧")
    sellers = list_monitored_sellers(active_only=False)

    if not sellers:
        st.info("登録済みセラーがありません。下の「セラーを登録」フォームから追加してください。")
    else:
        for seller in sellers:
            sid = seller["seller_id"]
            label = seller.get("seller_label") or sid
            is_active = bool(seller.get("is_active", 1))
            is_jp = bool(seller.get("is_jp_verified", 0))
            added = _to_jst(seller.get("added_at"))
            last_checked = _to_jst(seller.get("last_checked_at"))
            db_id = seller["id"]

            jp_badge = "JP確認済" if is_jp else "JP未確認"
            jp_color = "green" if is_jp else "orange"
            active_badge = "監視中" if is_active else "停止中"

            with st.expander(
                f"{label} ({sid}) | :{jp_color}[{jp_badge}] | {active_badge}",
                expanded=False,
            ):
                col_info, col_btn = st.columns([3, 1])
                with col_info:
                    st.markdown(
                        f"- **eBay seller_id**: `{sid}`\n"
                        f"- **JP判定**: {jp_badge}\n"
                        f"- **登録日時**: {added}\n"
                        f"- **最終チェック**: {last_checked}"
                    )
                    if not is_jp:
                        st.warning(
                            "JP未確認: Browse APIでitem_location=JPを確認できませんでした。"
                            " セラーIDが正確か、または出品ゼロでないかを確認してください。"
                        )

                with col_btn:
                    if is_active:
                        if st.button(
                            "停止",
                            key=f"deact_{db_id}",
                            use_container_width=True,
                        ):
                            toggle_seller_active(db_id, False)
                            st.success(f"{sid} を停止しました")
                            st.rerun()
                    else:
                        if st.button(
                            "再開",
                            key=f"act_{db_id}",
                            use_container_width=True,
                        ):
                            toggle_seller_active(db_id, True)
                            st.success(f"{sid} を再開しました")
                            st.rerun()

                    if st.button(
                        "削除",
                        key=f"del_{db_id}",
                        use_container_width=True,
                        type="secondary",
                    ):
                        delete_monitored_seller(db_id)
                        st.success(f"{sid} を削除しました")
                        st.rerun()

    # ── セクション 2: セラー登録フォーム ──────────────────────────────
    st.markdown("---")
    st.markdown("### セラーを登録")
    st.caption(
        "eBayの seller_id (ユーザー名) を入力。登録時にBrowse APIでJP判定を試みます。"
        " JP未確認でも登録は可能ですが、監視は自己責任で行ってください。"
    )

    with st.form("rival_seller_add_form", clear_on_submit=True):
        new_sid = st.text_input(
            "eBay seller_id (ユーザー名)",
            placeholder="例: seller_japan_audio",
            help="eBay 出品者のユーザー名 (seller_id) を入力",
        )
        new_label = st.text_input(
            "ラベル (任意)",
            placeholder="例: オーディオ専門セラー A",
            help="管理用の表示名。空欄なら seller_id をそのまま使用",
        )
        submitted = st.form_submit_button("登録", type="primary")

    if submitted:
        if not new_sid or not new_sid.strip():
            st.error("seller_id を入力してください")
        else:
            sid_clean = new_sid.strip()
            with st.spinner(f"{sid_clean} のJP判定中..."):
                try:
                    db_id, inserted, is_jp = add_monitored_seller(
                        seller_id=sid_clean,
                        label=new_label.strip() if new_label else "",
                    )
                    if inserted:
                        if is_jp:
                            st.success(f"{sid_clean} を登録しました (JP確認済)")
                        else:
                            st.warning(
                                f"{sid_clean} を登録しました (JP未確認)。"
                                " Browse APIでJP出品を確認できませんでした。"
                                " セラーIDが正しいか確認してください。"
                            )
                    else:
                        st.info(f"{sid_clean} は既に登録済みです (id={db_id})")
                    st.rerun()
                except Exception as e:
                    st.error(f"登録エラー: {e}")

    # ── セクション 3: 今すぐチェック ───────────────────────────────────
    st.markdown("---")
    st.markdown("### 今すぐチェック")
    st.caption(
        "全 active セラーの新規出品を今すぐ確認します。"
        " スケジューラは毎日 02:00 に自動実行します。"
    )

    if st.button("今すぐチェック実行", type="primary", key="rival_run_now"):
        active_sellers = list_monitored_sellers(active_only=True)
        if not active_sellers:
            st.warning("active なセラーがありません。先にセラーを登録してください。")
        else:
            with st.spinner(f"{len(active_sellers)} セラーをチェック中..."):
                try:
                    from tasks.task_rival_seller_sweep import run_rival_seller_sweep_task
                    result = run_rival_seller_sweep_task(config, scheduled_hour=0)
                    if result.get("success"):
                        st.success(
                            f"チェック完了: "
                            f"sellers={result.get('sellers_checked', 0)} "
                            f"/ 新規={result.get('total_new', 0)}件 "
                            f"/ Discord通知={result.get('total_notified', 0)}件"
                        )
                    else:
                        st.error(f"チェック失敗: {result.get('message', '不明')}")
                    if result.get("errors"):
                        st.warning("エラー詳細:\n" + "\n".join(result["errors"][:5]))
                except Exception as e:
                    st.error(f"チェック実行エラー: {e}")

    # ── セクション 4: 検知済み listing 一覧 ───────────────────────────
    st.markdown("---")
    st.markdown("### 検知済み新規出品")

    detections = get_recent_detections(limit=100)
    if not detections:
        st.info("まだ検知された listing はありません。「今すぐチェック」を実行してください。")
    else:
        # フィルタ
        sellers_in_detections = sorted(
            {d["seller_id"] for d in detections}
        )
        sel_filter = st.selectbox(
            "セラーでフィルタ",
            options=["全て"] + sellers_in_detections,
            key="rival_det_filter",
        )
        show_notified_only = st.checkbox(
            "Discord通知済みのみ表示",
            value=False,
            key="rival_notified_filter",
        )

        rows = detections
        if sel_filter != "全て":
            rows = [r for r in rows if r["seller_id"] == sel_filter]
        if show_notified_only:
            rows = [r for r in rows if r.get("notified", 0) == 1]

        st.caption(f"{len(rows)} 件表示")

        for det in rows[:50]:
            ebay_id = det["ebay_item_id"]
            title = det.get("title") or "(タイトル不明)"
            price = det.get("price_usd")
            price_str = f"${price:.2f}" if price is not None else "—"
            first_seen = _to_jst(det.get("first_seen_at"))
            notified = bool(det.get("notified", 0))
            score = det.get("eval_score")
            reason = det.get("eval_reason") or ""
            seller_id = det.get("seller_id", "")

            notified_badge = "通知済" if notified else "未通知"
            notified_color = "green" if notified else "gray"
            score_str = f"AI評価:{score}/100" if score is not None else ""

            with st.expander(
                f"[{seller_id}] {title[:60]} | {price_str} | "
                f":{notified_color}[{notified_badge}] {score_str}",
                expanded=False,
            ):
                st.markdown(
                    f"- **タイトル**: {title}\n"
                    f"- **価格**: {price_str}\n"
                    f"- **セラー**: `{seller_id}`\n"
                    f"- **eBay Item ID**: `{ebay_id}`\n"
                    f"- **初検知**: {first_seen}\n"
                    f"- **AI評価**: {score_str}\n"
                    f"- **評価理由**: {reason[:200] if reason else '—'}"
                )
                ebay_url = f"https://www.ebay.com/itm/{ebay_id}"
                st.markdown(f"[eBayで見る]({ebay_url})")
