#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在庫監視 (要対応/監視リスト/サイト設定) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "在庫監視":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
同梱ヘルパー (app.py top-level から移動、単一タブ専用): _cd_supply_risk, rank_to_stars, _render_inventory_summary_html
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


@st.cache_data(ttl=3, show_spinner=False)
def _cd_supply_risk(db_version: int):
    from monitor.database import get_ebay_listings_supply_risk
    return get_ebay_listings_supply_risk()


def rank_to_stars(rank: str) -> str:
    """Convert rank (S-E) to star representation"""
    rank_stars = {
        'S': "SSSSS TOP PRIORITY",
        'A': "AAAA",
        'B': "BBB",
        'C': "CC",
        'D': "D",
        'E': "-",
    }
    return rank_stars.get(rank, "???")


def _render_inventory_summary_html(
    total_risk: int,
    oos_n: int,
    pnf_n: int,
    last_checked_str: str,
) -> str:
    """在庫監視「要対応」サブタブの先頭サマリバー (純関数, 表示のみ).

    K1 Simplicity: DB アクセス禁止 (呼出側で集計済の値を受ける).
    K2 Surgical: 表示のみ、money-direct logic に一切触れない.

    引数:
      total_risk: 要対応件数 (oos_n + pnf_n)。0 → 緑、>0 → 赤.
      oos_n: 在庫切れ件数.
      pnf_n: ページ消失件数.
      last_checked_str: 最終チェック時刻表示文字列 (例 "2026-06-04 02:35:21 (3 時間前)" や
                        "データなし"). 呼出側で format 済を受ける.

    戻り値: <div> 1 枚の HTML 文字列 (`st.markdown(..., unsafe_allow_html=True)` 用).
    """
    if total_risk == 0:
        bar_color = "#2e7d5b"     # 緑系
        bar_bg = "rgba(46,125,91,0.10)"
        count_color = "#2e7d5b"
        head_label = "要対応"
    else:
        bar_color = "rgba(255,90,90,0.85)"     # 赤系
        bar_bg = "rgba(255,90,90,0.06)"
        count_color = "rgba(255,140,140,0.95)"
        head_label = "要対応"

    last_checked_safe = html.escape(last_checked_str or "")

    return (
        f'<div style="border-left:4px solid {bar_color};background:{bar_bg};'
        f'padding:10px 14px;margin-bottom:10px;border-radius:4px;">'
        f'<div style="display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;">'
        f'<span style="font-size:13px;color:#8d927f;">{head_label}</span>'
        f'<span style="font-size:26px;font-weight:700;color:{count_color};">{int(total_risk)}件</span>'
        f'<span style="font-size:12px;color:#5f6557;">'
        f'在庫切れ <b>{int(oos_n)}</b> ・ ページ消失 <b>{int(pnf_n)}</b>'
        f'</span>'
        f'<span style="font-size:12px;color:#8d927f;margin-left:auto;">'
        f'最終チェック: {last_checked_safe}'
        f'</span>'
        f'</div></div>'
    )


def render_inventory_monitor_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    import json
    from monitor.database import add_check_log, add_item_manual, build_source_url, delete_item, delete_site_config, find_site_config_by_sku, get_active_items, get_all_items, get_conn, get_prev_status, get_recent_logs, get_site_configs, save_site_config, set_ebay_listing_risk_confirmed, update_ebay_listing_quantity, update_ebay_listing_sku, update_item_status, update_supplier_candidate_status, upsert_item
    from monitor.notifier import send_unavailable_alert
    from monitor.scrapers import check_item_by_config, check_items_batch, prepare_batch_items
    from sku_mapping_manager import url_to_sku
    from tasks.task_supplier_apply import accept_supplier_candidate, apply_supplier_candidate
    from ui_cache import bump_db_version, get_db_version

    # 依頼ボード#11 (2026-06-12): 採用バッチ成功後の写真/description フォロー
    # アップ欄を在庫監視タブでも展開 (仕入先候補タブと共通 section)。
    # _process_apply 成功時に立てた _sup_photo_prompt_/_sup_desc_prompt_ を
    # ここで描画 (バッチ末尾の st.rerun 後にタブ最上部へ出る)。
    from tabs._supplier_followup_section import render_supplier_followup_section
    if render_supplier_followup_section():
        st.markdown("---")

    monitor_tab_risk, monitor_tab1, monitor_tab2 = st.tabs(["要対応", "監視リスト", "サイト設定"])

    # ---------- 要対応（仕入先在庫リスク） ----------
    with monitor_tab_risk:
        risk_data = _cd_supply_risk(get_db_version())
        oos_items = risk_data["out_of_stock"]
        pnf_items = risk_data["page_not_found"]
        total_risk = len(oos_items) + len(pnf_items)

        # 最終チェック時刻を文字列化 (旧 caption 群の format 維持、表示先のみサマリバーへ集約)
        _last_checked_str = "データなし"
        try:
            from datetime import datetime as _dt_inv
            with get_conn() as _inv_conn:
                _inv_conn.row_factory = None
                _last_inv = _inv_conn.execute(
                    "SELECT MAX(source_last_checked) FROM ebay_listings WHERE source_last_checked IS NOT NULL"
                ).fetchone()[0]
            if _last_inv:
                # 2つの format を許容: "2026-04-23T15:39:00.671816" (ISO) or "2026-04-23 15:39:00"
                _parse_ok = False
                for _fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        _dt_inv_obj = _dt_inv.strptime(_last_inv, _fmt)
                        _parse_ok = True
                        break
                    except Exception:
                        continue
                if _parse_ok:
                    _delta = _dt_inv.now() - _dt_inv_obj
                    if _delta.total_seconds() < 3600:
                        _ago = f"{int(_delta.total_seconds()/60)} 分前"
                    elif _delta.total_seconds() < 86400:
                        _ago = f"{int(_delta.total_seconds()/3600)} 時間前"
                    else:
                        _ago = f"{int(_delta.total_seconds()/86400)} 日前"
                    _last_checked_str = f"{_dt_inv_obj.strftime('%Y-%m-%d %H:%M:%S')} ({_ago})"
                else:
                    _last_checked_str = str(_last_inv)
        except Exception as _e:
            logger.warning("在庫リスク 最終チェック表示 失敗: %s", _e)

        st.markdown(
            _render_inventory_summary_html(
                total_risk=total_risk,
                oos_n=len(oos_items),
                pnf_n=len(pnf_items),
                last_checked_str=_last_checked_str,
            ),
            unsafe_allow_html=True,
        )
        st.caption("eBay在庫が1以上あるのに、仕入先で購入できない商品です。出品停止または仕入先変更を検討してください。")

        # 2026-06-05 user 要望: Yahoo 24h 再出品猶予 (W100 grace) 撤廃により
        # 「再出品待ち」表示セクションを削除。Yahoo 終了/売切も即 OOS 扱い (要対応/確認不可に出る)。

        def _render_risk_table_with_actions(items: list[dict], section_key: str) -> None:
            """要対応商品を編集可能テーブルで表示（二段階チェック方式・DB永続化）+ 候補URL最大3件併記"""
            import pandas as pd
            from monitor.database import get_conn as _risk_conn

            # 元データを保持（変更検出用）
            orig_data = {item["ebay_item_id"]: {"sku": item.get("sku") or "", "qty": item["quantity_ebay"]} for item in items}

            # risk_confirmed はDBから取得済み
            confirmed_ids = {item["ebay_item_id"] for item in items if item.get("risk_confirmed", 0) == 1}

            # SKU 毎に supplier_candidates 上位3件（pending/accepted）を取得
            # 2026-04-24 Q2=iii: 在庫監視は「仕入先在庫0→置換 or 在庫0化」の動線なので
            # alt_listing_possible=1 (別SKU出品機会) は非表示にする (本来の目的と逸れるため)
            _sku_list = [it.get("sku") or "" for it in items if it.get("sku")]
            _cand_by_sku: dict[str, list[dict]] = {}
            # 2026-05-05 追加: 探索済だが alt-only (= 全 candidate が alt_listing_possible=1) の SKU
            # を識別して、UI で「候補未探索」と誤表示する bug を防ぐ. Baccarat case 対策.
            _alt_only_count_by_sku: dict[str, int] = {}
            if _sku_list:
                with _risk_conn() as _rc:
                    _ph = ",".join("?" * len(_sku_list))
                    for _r in _rc.execute(
                        f"""SELECT sku, candidate_url, match_score, status, alt_listing_possible
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 0
                            ORDER BY sku, match_score DESC""",
                        _sku_list,
                    ).fetchall():
                        _cand_by_sku.setdefault(_r["sku"], []).append(dict(_r))
                    # alt-only candidates の数も別途取得 (UI caption 分岐用)
                    for _r in _rc.execute(
                        f"""SELECT sku, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY sku""",
                        _sku_list,
                    ).fetchall():
                        _alt_only_count_by_sku[_r["sku"]] = _r["alt_n"]

            def _cand_url(sku: str, idx: int) -> str:
                """sku の候補 idx 番目の URL を返す。無ければ空文字。"""
                cands = _cand_by_sku.get(sku, [])
                if idx >= len(cands):
                    return ""
                return cands[idx].get("candidate_url") or ""

            def _cand_count_label(sku: str) -> str:
                n = len(_cand_by_sku.get(sku, []))
                if n == 0:
                    # 2026-05-05 修正: alt-only 候補があれば「探索済」と区別表示
                    # (PNF / OOS の両 section で同一ロジック、Baccarat case 対策).
                    alt_n = _alt_only_count_by_sku.get(sku, 0)
                    if alt_n > 0:
                        return f"探索済(別出品機会{alt_n})"
                    return "未探索"
                return f"{min(n,3)}/{n}件"

            df = pd.DataFrame([
                {
                    "状態": "● 確認済" if item["ebay_item_id"] in confirmed_ids else "○ 未確認",
                    "確認": item["ebay_item_id"] in confirmed_ids,
                    "Item ID": item["ebay_item_id"],
                    "eBay": f"https://www.ebay.com/itm/{item['ebay_item_id']}" if item["ebay_item_id"] else "",
                    "SKU": item.get("sku") or "",
                    "価格": f"${item['current_price']:.2f}" if item["current_price"] else "-",
                    "在庫": int(item["quantity_ebay"]),
                    "ランク": item["rank"] or "-",
                    "仕入先": item["source"] or "-",
                    "仕入先URL": item.get("source_url") or "",
                    "候補": _cand_count_label(item.get("sku") or ""),
                    "候補URL1": _cand_url(item.get("sku") or "", 0),
                    "候補URL2": _cand_url(item.get("sku") or "", 1),
                    "候補URL3": _cand_url(item.get("sku") or "", 2),
                    "タイトル": (item["title"] or "")[:55],
                }
                for item in items
            ])

            # st.form で囲み、ボタン押下まで中間rerunを防ぐ
            with st.form(key=f"risk_form_{section_key}"):
                edited_df = st.data_editor(
                    df,
                    column_config={
                        "状態": st.column_config.TextColumn("状態", width="small"),
                        "確認": st.column_config.CheckboxColumn("確認", width="small"),
                        "Item ID": st.column_config.TextColumn("Item ID", width="medium"),
                        "eBay": st.column_config.LinkColumn("eBay", display_text="開く", width="small"),
                        "SKU": st.column_config.TextColumn("SKU", width="medium"),
                        "在庫": st.column_config.NumberColumn("在庫", min_value=0, step=1, width="small"),
                        "仕入先URL": st.column_config.LinkColumn("仕入先URL", display_text="リンク", width="small"),
                        "候補": st.column_config.TextColumn("候補", width="small",
                                                          help="Pattern 1 async / Pattern 2 batch が探索した候補数"),
                        "候補URL1": st.column_config.LinkColumn("候補1", display_text="▸1", width="small"),
                        "候補URL2": st.column_config.LinkColumn("候補2", display_text="▸2", width="small"),
                        "候補URL3": st.column_config.LinkColumn("候補3", display_text="▸3", width="small"),
                    },
                    disabled=["状態", "Item ID", "eBay", "価格", "ランク", "仕入先", "仕入先URL",
                              "候補", "候補URL1", "候補URL2", "候補URL3", "タイトル"],
                    hide_index=True,
                    width="stretch",
                    key=f"risk_table_{section_key}",
                )
                submitted = st.form_submit_button("確認済みをeBayに反映", type="primary")

            # --- 集計表示 ---
            total = len(items)
            confirmed_count = len(confirmed_ids)
            remaining = total - confirmed_count
            st.caption(f"全{total}件 | ● 確認済: {confirmed_count}件 | 残り: {remaining}件")

            # --- 二段階目：フォーム送信時にeBayに反映 ---
            if submitted:
                checked_rows = edited_df[edited_df["確認"]]
                if checked_rows.empty:
                    st.warning("チェックが入っている商品がありません")
                else:
                    sync_targets = []
                    no_change_ids = []
                    for _, row in checked_rows.iterrows():
                        eid = row["Item ID"]
                        if eid in confirmed_ids:
                            continue  # 既に確認済みはスキップ
                        orig = orig_data.get(eid, {})
                        new_sku = row["SKU"] if row["SKU"] != orig.get("sku", "") else None
                        new_qty = int(row["在庫"]) if int(row["在庫"]) != int(orig.get("qty", 0)) else None
                        if new_sku is not None or new_qty is not None:
                            sync_targets.append({"ebay_item_id": eid, "new_sku": new_sku, "new_qty": new_qty})
                        else:
                            no_change_ids.append(eid)

                    new_checked = len(sync_targets) + len(no_change_ids)
                    if new_checked == 0:
                        st.success("チェック済みの全商品は確認済みです")
                    else:
                        change_msg = f"変更あり: {len(sync_targets)}件" if sync_targets else ""
                        keep_msg = f"現状維持: {len(no_change_ids)}件" if no_change_ids else ""
                        st.info(f"{new_checked}件を処理中（{', '.join(filter(None, [change_msg, keep_msg]))}）")

                        # 現状維持 → DBに確認済みフラグを立てる
                        for eid in no_change_ids:
                            set_ebay_listing_risk_confirmed(eid, 1)

                        # 変更あり → eBay APIに送信してからDBに確認済みフラグ
                        if sync_targets:
                            ebay_creds = {
                                'app_id': s.get("ebay_app_id", ""),
                                'dev_id': s.get("ebay_dev_id", ""),
                                'cert_id': s.get("ebay_cert_id", ""),
                                'user_token': s.get("ebay_user_token", ""),
                            }
                            if not all(ebay_creds.values()):
                                st.error("eBay API認証情報が未設定です（設定タブ参照）")
                            else:
                                from monitor.ebay_client import revise_inventory_quantity, revise_item_sku
                                progress = st.progress(0)
                                for i, ch in enumerate(sync_targets):
                                    eid = ch["ebay_item_id"]
                                    ok = True
                                    if ch["new_qty"] is not None:
                                        r = revise_inventory_quantity(eid, ch["new_qty"], **ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_quantity(eid, ch["new_qty"])
                                            st.success(f"{eid}: 在庫 → {ch['new_qty']}")
                                        else:
                                            st.error(f"{eid}: 在庫 → {r['message']}")
                                            ok = False
                                    if ch["new_sku"] is not None:
                                        r = revise_item_sku(eid, ch["new_sku"], **ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_sku(eid, ch["new_sku"])
                                            st.success(f"{eid}: SKU → {ch['new_sku']}")
                                        else:
                                            st.error(f"{eid}: SKU → {r['message']}")
                                            ok = False
                                    if ok:
                                        set_ebay_listing_risk_confirmed(eid, 1)
                                    progress.progress((i + 1) / len(sync_targets))

                        bump_db_version()  # W134 Step2: 在庫/SKU/risk 一括変更後 read-cache 無効化
                        st.rerun()  # Reload to update status column

        # --- 在庫切れ（インライン候補表示版） ---
        def _on_adopt_check_change(cid: int, eid: str, url: str, section_key: str):
            """採用チェック ON 時に仕入先 URL を SKU 変換して SKU 欄 session_state に反映.

            2026-04-24 要件 Q3=A: ユーザーが採用チェックを入れた瞬間に UI の SKU 欄が
            新仕入先ベースの SKU に自動更新される。確認後に一括実行で eBay 反映。
            OFF にしても SKU 欄は自動復元しない (ユーザーが手動編集した可能性を尊重)。
            """
            adopt_key = f"{section_key}_adopt_{cid}"
            sku_key = f"{section_key}_sku_{eid}"
            if st.session_state.get(adopt_key):
                try:
                    new_sku = url_to_sku(url)
                    if new_sku:
                        st.session_state[sku_key] = new_sku
                except Exception:
                    pass  # url_to_sku 失敗時は何もしない

        @st.fragment
        def _render_oos_block(
            item: dict,
            cands: list[dict],
            section_key: str,
            alt_only_count_by_sku: dict[str, int],
        ) -> None:
            """
            1 SKU分のブロックを描画（OOS情報 + 仕入先候補 up to 3件をインライン）。

            2026-04-24 st.fragment 化 (Q3=A 要件):
            - 採用 checkbox の on_change コールバックで SKU 欄を自動更新
            - fragment ごとに独立 rerun, 他商品には影響しない (爆速維持)
            - st.form は解体、一括実行は単純ボタンで全 session_state を読んで処理

            引数:
              item: oos_items の 1 要素
              cands: 対象SKUの仕入先候補 list[dict]（status IN ('pending','accepted','applied')、
                     score降順、最大3件）
              section_key: widget key プレフィクス（例 "oos"）
              alt_only_count_by_sku: SKU 毎の alt_listing_possible=1 候補数 dict.
                cands=0 件 + alt_only>0 のとき「探索済 (置換候補なし)」caption を出す.
                2026-05-05 NameError バグ fix で引数化 (旧: 別関数のローカル参照でクラッシュ).
            """
            eid = item["ebay_item_id"]
            sku_orig = item.get("sku") or ""
            qty_orig = int(item.get("quantity_ebay") or 0)
            price = item.get("current_price")
            price_str = f"${price:.2f}" if price else "-"
            rank = item.get("rank") or "-"
            source = item.get("source") or "-"
            source_url = item.get("source_url") or ""
            title = (item.get("title") or "")[:80]
            confirmed_now = bool(item.get("risk_confirmed"))

            with st.container(border=True):
                # --- ヘッダ行: 確認 / Item ID / タイトル / 価格 / ランク ---
                _h1, _h2, _h3, _h4, _h5 = st.columns([0.9, 1.6, 5.0, 0.9, 0.8])
                with _h1:
                    st.checkbox(
                        "確認", key=f"{section_key}_confirm_{eid}",
                        value=confirmed_now,
                    )
                with _h2:
                    st.markdown(
                        f'<div style="font-family:var(--font-mono,monospace);'
                        f'font-size:12px;color:#2a2e2a;padding-top:6px;">'
                        f'<a href="https://www.ebay.com/itm/{html.escape(eid)}" target="_blank" '
                        f'style="color:#156a63;text-decoration:none;">{html.escape(eid)}</a></div>',
                        unsafe_allow_html=True,
                    )
                with _h3:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">'
                        f'{html.escape(title)}</div>',
                        unsafe_allow_html=True,
                    )
                with _h4:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">{price_str}</div>',
                        unsafe_allow_html=True,
                    )
                with _h5:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">ランク{rank}</div>',
                        unsafe_allow_html=True,
                    )

                # --- 編集行: SKU / 在庫 / 仕入先URL ---
                _e1, _e2, _e3, _e4 = st.columns([3.0, 1.2, 1.6, 2.0])
                with _e1:
                    st.text_input(
                        "SKU", value=sku_orig,
                        key=f"{section_key}_sku_{eid}",
                        label_visibility="collapsed",
                    )
                with _e2:
                    st.number_input(
                        "在庫", min_value=0, step=1, value=qty_orig,
                        key=f"{section_key}_qty_{eid}",
                        label_visibility="collapsed",
                    )
                with _e3:
                    st.markdown(
                        f'<div style="font-size:11px;color:#8d927f;padding-top:8px;">'
                        f'仕入先: {html.escape(source)}</div>',
                        unsafe_allow_html=True,
                    )
                with _e4:
                    if source_url:
                        st.markdown(
                            f'<div style="padding-top:6px;">'
                            f'<a href="{html.escape(source_url, quote=True)}" target="_blank" '
                            f'style="color:#156a63;font-size:12px;">仕入先URLを開く</a></div>',
                            unsafe_allow_html=True,
                        )

                st.markdown("---")

                # --- 候補部（form-safe: 全て checkbox、submit で batch 処理） ---
                if cands:
                    _total_for_sku = len(cands)
                    st.markdown(
                        f'<div style="font-size:11px;color:#8d927f;margin-bottom:4px;">'
                        f'仕入先候補 {_total_for_sku}件（score降順／上位最大3件）／'
                        f'下部「一括実行」で採用チェック済みURLをSKUに反映</div>',
                        unsafe_allow_html=True,
                    )

                    for _c in cands:
                        _cid = _c["id"]
                        _score = _c.get("match_score") or 0
                        _is_alt = bool(_c.get("alt_listing_possible")) and _score < 60
                        _score_color = (
                            "#2e7d5b" if _score >= 80
                            else "#b8860b" if _score >= 60
                            else "rgba(200,150,220,0.85)"
                        )
                        _plat = _c.get("source_platform") or "?"
                        _ttl = (_c.get("candidate_title") or "")[:50]
                        _price_jpy = _c.get("candidate_price_jpy")
                        _price_str = f"仕入 ¥{_price_jpy:,}" if _price_jpy else "仕入 ¥?"
                        _url = _c.get("candidate_url") or ""
                        _status = _c.get("status") or "pending"
                        _type_label = "別出品機会" if _is_alt else "置換候補"

                        # 利益 inline (Interstellar amber)
                        _profit_jpy_v = _c.get("profit_jpy")
                        _profit_str = ""
                        if _profit_jpy_v is not None and _price_jpy and _price_jpy > 0:
                            _rate_v = (_profit_jpy_v / _price_jpy) * 100
                            _profit_str = (
                                f' <span style="color:#b35a2e;font-weight:600;">'
                                f'利益 +¥{int(_profit_jpy_v):,} ({_rate_v:.0f}%)</span>'
                            )

                        # W100 (2026-05-06): 旧「採用後 24h 猶予」UI 削除.
                        # 新仕様 (Phase 3) は inventory_check 側で grace を管理し、
                        # 採用→反映フローは即時実行 (猶予なし).

                        _info_col, _link_col, _btn_col = st.columns([5.6, 1.2, 2.2])
                        with _info_col:
                            _status_badge = ""
                            if _status == "accepted":
                                _status_badge = (
                                    '<span style="color:#2e7d5b;'
                                    'font-size:10px;margin-left:6px;">[採用済]</span>'
                                )
                            elif _status == "applied":
                                _status_badge = (
                                    '<span style="color:#156a63;'
                                    'font-size:10px;margin-left:6px;">[反映済]</span>'
                                )
                            # W100: grace UI 廃止
                            _grace_html = ""
                            # score を pill バッジ化 (背景=_score_color、文字=濃色).
                            # 表示のみ変更、_score_color 算出ロジック (≥80/≥60/未満) は不変.
                            _score_badge = (
                                f'<span style="display:inline-block;background:{_score_color};'
                                f'color:#0e1626;font-weight:700;font-size:11px;padding:1px 8px;'
                                f'border-radius:10px;line-height:1.5;">score {_score}</span>'
                            )
                            st.markdown(
                                f'<div style="border-left:2px solid {_score_color};padding:4px 10px;'
                                f'background:rgba(166,150,121,0.06);font-size:12px;">'
                                f'{_score_badge}'
                                f' <span style="color:#8d927f;font-size:10px;">[{_type_label}]</span>'
                                f' <span style="color:#5f6557;">{html.escape(_plat)}</span>'
                                f' <span style="color:#2a2e2a;">{html.escape(_ttl)}</span>'
                                f' <span style="color:#5f6557;">{_price_str}</span>'
                                f'{_profit_str}'
                                f'{_status_badge}{_grace_html}'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            # W258/Phase-B (2026-06-11): eBay × 仕入先 画像比較カード。
                            # money-direct path (checkbox/button) は不変、画像表示のみ追加。
                            _oos_ebay_img = item.get("ebay_image_url") or ""
                            _oos_cand_img = _c.get("candidate_image_url") or ""
                            if _oos_ebay_img or _oos_cand_img:
                                from tabs._supplier_card_html import _CARD_CSS
                                from html import escape as _hesc
                                _oos_price = item.get("current_price")

                                def _imgcell(img_url: str, caption: str) -> str:
                                    if img_url:
                                        return (
                                            f'<div class="sc-imgpair-cell">'
                                            f'<a href="{_hesc(img_url)}" target="_blank" rel="noopener">'
                                            f'<img src="{_hesc(img_url)}" alt="{_hesc(caption)}" loading="lazy">'
                                            f'</a>'
                                            f'<div class="sc-imgpair-caption">{_hesc(caption)}</div>'
                                            f'</div>'
                                        )
                                    return (
                                        f'<div class="sc-imgpair-cell">'
                                        f'<div class="sc-imgpair-placeholder">画像未取得</div>'
                                        f'<div class="sc-imgpair-caption">{_hesc(caption)}</div>'
                                        f'</div>'
                                    )

                                _ebay_cap = f"eBay ${_oos_price:.2f}" if _oos_price else "eBay"
                                _cand_cap = f"¥{_price_jpy:,}" if _price_jpy else "仕入先"
                                _oos_imgpair = (
                                    f'<div class="sc-imgpair">'
                                    f'{_imgcell(_oos_ebay_img, _ebay_cap)}'
                                    f'{_imgcell(_oos_cand_img, _cand_cap)}'
                                    f'</div>'
                                )
                                st.markdown(
                                    _CARD_CSS + _oos_imgpair,
                                    unsafe_allow_html=True,
                                )
                        with _link_col:
                            if _url:
                                st.markdown(
                                    f'<a href="{html.escape(_url, quote=True)}" target="_blank" '
                                    f'style="color:#156a63;font-size:12px;">[商品開く]</a>',
                                    unsafe_allow_html=True,
                                )
                        with _btn_col:
                            _alt_only = _is_alt

                            if _status == "pending":
                                _b1, _b2 = st.columns(2)
                                with _b1:
                                    st.checkbox(
                                        "採用", key=f"{section_key}_adopt_{_cid}",
                                        disabled=_alt_only,
                                        on_change=_on_adopt_check_change,
                                        args=(_cid, eid, _url, section_key),
                                        help=(
                                            "別SKU出品機会のためSKU置換は不適（新規出品フロー向け）"
                                            if _alt_only
                                            else "採用: SKU欄が自動で仕入先URLベースに更新されます。"
                                            "確認後に下部「一括実行」でeBayに反映。"
                                        ),
                                    )
                                with _b2:
                                    st.checkbox(
                                        "不採用", key=f"{section_key}_reject_{_cid}",
                                        help=(
                                            "不採用にする（別出品機会も却下、学習データに記録）"
                                            if _alt_only
                                            else "不採用（ユーザー判断として学習データに記録）"
                                        ),
                                    )
                            elif _status == "accepted":
                                # W100 (2026-05-06): 旧「ヤフオク 24h 猶予」廃止により
                                # _disabled 条件削除. 採用済はいつでも反映可能.
                                st.checkbox(
                                    "反映する",
                                    key=f"{section_key}_applyck_{_cid}",
                                    help="採用済。submit 時に ReviseItem でSKU反映",
                                )
                                # W115: 写真反映 button (案 A 別 button、SKU と独立).
                                _photo_key = f"{section_key}_photo_open_{_cid}"
                                if st.button(
                                    "📷 写真反映",
                                    key=f"{section_key}_btn_photo_{_cid}",
                                    help=(
                                        "仕入先画像から Photoroom + Gemini で hero 合成 → "
                                        "EPS upload → ReviseItem PictureDetails で eBay 反映"
                                    ),
                                ):
                                    st.session_state[_photo_key] = (
                                        not st.session_state.get(_photo_key, False)
                                    )
                            elif _status == "applied":
                                st.caption("完了")

                        # W115: status='accepted' で「写真反映」展開時は section render.
                        # _btn_col 外に置くことで横幅を活かして 3 候補表示.
                        if _status == "accepted" and st.session_state.get(
                            f"{section_key}_photo_open_{_cid}", False
                        ):
                            from tabs._supplier_photo_pipeline import (
                                render_supplier_photo_apply_section,
                            )
                            render_supplier_photo_apply_section(
                                candidate_id=_cid,
                                candidate_url=_url,
                                ebay_item_id=eid,
                                candidate_title=_ttl,
                            )
                else:
                    # 候補0件 (置換用) → ただし alt-only 候補があれば「探索済」と区別表示.
                    # 2026-05-05 修正: Baccarat case (探索済 7 件すべて alt のみ) で
                    # 「候補未探索」と誤表示してた事象の修正. user 認識ミスを防ぐ.
                    # HIGH-1' fix: PNF 経路は eid キー dict、OOS 経路は sku キー dict。
                    # eid は数字文字列 / sku は stock*/ebay* prefix でキー空間が衝突しない
                    # ため、eid 優先 lookup + sku fallback で両経路を単一式で吸収する。
                    _alt_n = alt_only_count_by_sku.get(eid) or alt_only_count_by_sku.get(sku_orig, 0)
                    if _alt_n > 0:
                        st.caption(
                            f"仕入先候補は探索済みです（{_alt_n} 件見つかりましたが、"
                            f"すべて『別商品の出品候補』として分類されており、この出品の"
                            f"置き換えには使えません）。別商品として出品する検討は"
                            f"『別SKU出品機会』タブで行ってください。"
                        )
                    else:
                        st.caption(
                            "候補未探索（次回02:30 Pattern 2バッチで自動探索、"
                            "または form 下部の「未探索SKUの即時探索」で個別起動）"
                        )

        # [共通] 認証情報と config を OOS / PNF 両セクションより前に準備 (HIGH-1 fix)
        # oos_items が 0 件でも pnf_items が存在すると PNF ハンドラが参照するため、
        # どちらの if ブロックにも属さない共通スコープで定義する。
        _ebay_creds = {
            'app_id': s.get("ebay_app_id", ""),
            'dev_id': s.get("ebay_dev_id", ""),
            'cert_id': s.get("ebay_cert_id", ""),
            'user_token': s.get("ebay_user_token", ""),
        }
        _cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
        _cfg = {}
        if _cfg_path.exists():
            try:
                _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as _cfg_err:
                logger.warning("schedule_config.json 読込失敗 (空 config で続行): %s", _cfg_err)

        st.markdown(f"### 仕入先在庫切れ ({len(oos_items)}件)")
        if oos_items:
            # [1] SKU ごとの候補を1回だけ一括取得（N+1 SQL 回避）
            from monitor.database import get_conn as _oos_conn
            _sku_list = [_it.get("sku") for _it in oos_items if _it.get("sku")]
            _cand_by_sku: dict[str, list[dict]] = {}
            # 2026-05-05 追加 (CRITICAL fix): alt-only candidate 数を OOS section でも別取得.
            # _render_oos_block の caption 分岐で参照される. 過去の bug fix で別関数の
            # ローカル変数を参照してて NameError クラッシュしていた事故予防.
            _oos_alt_only_count: dict[str, int] = {}
            if _sku_list:
                with _oos_conn() as _cc:
                    _ph = ",".join("?" * len(_sku_list))
                    for _r in _cc.execute(
                        f"""SELECT id, sku, ebay_item_id, candidate_url, candidate_title,
                                   candidate_price_jpy, match_score, source_platform,
                                   status, alt_listing_possible,
                                   profit_jpy, profitable,
                                   candidate_image_url
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted','applied')
                              AND COALESCE(alt_listing_possible, 0) = 0
                            ORDER BY sku,
                                     (profit_jpy IS NULL), profit_jpy DESC,
                                     match_score DESC""",
                        _sku_list,
                    ).fetchall():
                        _cand_by_sku.setdefault(_r["sku"], []).append(dict(_r))
                    # alt-only candidate 数も別途取得 (Baccarat case 対策)
                    for _r in _cc.execute(
                        f"""SELECT sku, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY sku""",
                        _sku_list,
                    ).fetchall():
                        _oos_alt_only_count[_r["sku"]] = _r["alt_n"]

            st.caption(
                "各商品のすぐ下に仕入先候補(上位3件)を表示。候補に「採用」「不採用」をチェック → "
                "下部の「一括実行」ボタンで eBay API への反映 (ReviseItem) と DB 更新が同時に走ります。"
                " 採用は SKU 書換 / 不採用は学習データに記録。SKU・在庫数の直接編集は「確認」チェック必須。"
            )

            # [3] st.fragment 方式 (2026-04-24 Q3=A 対応):
            # 各商品ブロックは @st.fragment 化され、採用 checkbox の on_change で
            # SKU 欄が即時更新される (他商品には影響せず爆速維持)。
            # 一括実行ボタンは通常ボタン (form_submit_button ではない)、
            # 押下時に全 session_state を読み取って batch 処理する。
            for _item in oos_items:
                _sku_val = _item.get("sku") or ""
                _cands_for_sku = _cand_by_sku.get(_sku_val, [])[:3]
                _render_oos_block(_item, _cands_for_sku, "oos", _oos_alt_only_count)

            st.divider()
            st.caption(
                "採用チェック → SKU 欄が仕入先URLベースに自動更新されます。"
                " 確認後、下のボタンで eBay 反映・DB 更新を一括処理。"
                " 採用 mutex: 同一SKU内で複数 ON の場合は score 最上位を採用、他は不採用扱い。"
            )

            _submitted = st.button(
                "選択した採用/不採用/編集を一括実行（eBay・DB）",
                key="oos_batch_submit",
                type="primary", width="stretch",
            )

            # --- form 外: submit 時のバッチ処理 ---
            if _submitted:
                # 処理の順序:
                #   1) 不採用 (DB のみ、高速)
                #   2) 採用 (pending → accept + apply、accepted→apply のみ、eBay ReviseItem で URL→SKU 設定)
                #   3) 手動SKU/qty編集 (確認✓ かつ widget state vs 元データ diff)
                #   4) 確認済みフラグ更新
                adoptions_pending: list[int] = []     # pending 候補で 採用 checked
                adoptions_accepted: list[int] = []    # accepted 候補で 反映する checked
                rejections: list[int] = []
                _cid_to_eid: dict[int, str] = {}
                _cid_to_score: dict[int, int] = {}
                _eid_to_item: dict[str, dict] = {
                    _it["ebay_item_id"]: _it for _it in oos_items
                }
                # SKU ごとの 採用 候補リスト（mutex 用: 複数 ON なら上位score を採用）
                _sku_adopt_candidates: dict[str, list[int]] = {}

                for _it in oos_items:
                    _sku_for_cand = _it.get("sku") or ""
                    for _c in _cand_by_sku.get(_sku_for_cand, [])[:3]:
                        _cid_scan = _c["id"]
                        _cid_to_eid[_cid_scan] = _it["ebay_item_id"]
                        _cid_to_score[_cid_scan] = _c.get("match_score") or 0
                        if _c.get("status") == "pending":
                            if st.session_state.get(f"oos_adopt_{_cid_scan}", False):
                                _sku_adopt_candidates.setdefault(_sku_for_cand, []).append(_cid_scan)
                            elif st.session_state.get(f"oos_reject_{_cid_scan}", False):
                                rejections.append(_cid_scan)
                        elif _c.get("status") == "accepted":
                            if st.session_state.get(f"oos_applyck_{_cid_scan}", False):
                                adoptions_accepted.append(_cid_scan)

                # Mutex 適用: 同一SKU内で複数 採用 ON → score 最上位のみ採用、他は不採用扱い
                for _sku_m, _cids_m in _sku_adopt_candidates.items():
                    if len(_cids_m) == 1:
                        adoptions_pending.append(_cids_m[0])
                    else:
                        _cids_sorted = sorted(_cids_m, key=lambda x: _cid_to_score.get(x, 0), reverse=True)
                        _winner = _cids_sorted[0]
                        _losers = _cids_sorted[1:]
                        adoptions_pending.append(_winner)
                        rejections.extend(_losers)
                        st.warning(
                            f"SKU {_sku_m}: 採用チェックが {len(_cids_m)} 件ONでしたが、"
                            f"SKU置換は1つのみ可。score最上位(cid={_winner})を採用、他は不採用として処理します。"
                        )

                # 手動 SKU/qty 編集の収集
                sync_targets: list[dict] = []
                no_change_ids: list[str] = []
                for _it in oos_items:
                    eid = _it["ebay_item_id"]
                    is_checked = bool(st.session_state.get(f"oos_confirm_{eid}", False))
                    if not is_checked:
                        continue
                    cur_sku = st.session_state.get(f"oos_sku_{eid}", _it.get("sku") or "")
                    cur_qty = int(st.session_state.get(f"oos_qty_{eid}", _it["quantity_ebay"]))
                    orig_sku = _it.get("sku") or ""
                    orig_qty = int(_it["quantity_ebay"])
                    new_sku = cur_sku if cur_sku != orig_sku else None
                    new_qty = cur_qty if cur_qty != orig_qty else None
                    if new_sku is not None or new_qty is not None:
                        sync_targets.append(
                            {"ebay_item_id": eid, "new_sku": new_sku, "new_qty": new_qty}
                        )
                    else:
                        no_change_ids.append(eid)

                total_adoptions = len(adoptions_pending) + len(adoptions_accepted)
                total_actions = total_adoptions + len(rejections) + len(sync_targets) + len(no_change_ids)
                if total_actions == 0:
                    st.warning("チェックが入っている未処理商品がありません")
                else:
                    st.info(
                        f"処理中: 採用 {total_adoptions}件 "
                        f"(pending→apply {len(adoptions_pending)} + accepted→apply {len(adoptions_accepted)})"
                        f" / 不採用 {len(rejections)}件 / 変更あり {len(sync_targets)}件 "
                        f"/ 現状維持 {len(no_change_ids)}件"
                    )

                    # 1) 不採用 (DB のみ)
                    for _cid in rejections:
                        try:
                            update_supplier_candidate_status(_cid, "rejected")
                        except Exception as _e:
                            st.error(f"不採用 cid={_cid}: {_e}")
                    if rejections:
                        st.success(f"不採用 {len(rejections)}件 を記録しました")

                    # 2) 採用処理（pending→accept+apply と accepted→apply-only）
                    adopt_succeeded_eids: set[str] = set()
                    if total_adoptions > 0:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import revise_inventory_quantity as _rev_qty

                            def _process_apply(_cid_p: int, _is_pending: bool) -> None:
                                # W114 (2026-05-09): Surface A の挙動を Surface B (W112 retrospective fix 後)
                                # に統一. 旧 Surface A は apply 失敗を `st.info("採用済（反映保留）")` で
                                # 表示 = Q0 偽装成功境界 + qty 復元 logger 痕跡なし = silent skip 境界
                                # → 全部 specific exception + logger + st.error に修正.
                                _eid_applied = _cid_to_eid.get(_cid_p, "?")
                                if _is_pending:
                                    _res_a = accept_supplier_candidate(_cid_p)
                                    if not _res_a.get("success"):
                                        logger.error(
                                            "supplier accept failed (Surface A) cid=%s eid=%s msg=%s",
                                            _cid_p, _eid_applied, _res_a.get("message"),
                                        )
                                        st.error(
                                            f"採用 cid={_cid_p}: "
                                            f"{_res_a.get('message') or 'accept失敗'}"
                                        )
                                        return
                                _res_b = apply_supplier_candidate(_cid_p, _cfg)
                                if not _res_b.get("success"):
                                    # W114: 旧 `st.info("採用済（反映保留）")` の Q0 偽装成功境界を fix.
                                    # apply 失敗は明確に error として表示 + logger 痕跡保存.
                                    logger.error(
                                        "supplier apply failed (Surface A) cid=%s eid=%s msg=%s",
                                        _cid_p, _eid_applied, _res_b.get("message"),
                                    )
                                    st.error(
                                        f"{_eid_applied}: eBay 反映失敗: "
                                        f"{_res_b.get('message') or 'apply エラー'} (cid={_cid_p})"
                                    )
                                    return
                                st.success(
                                    f"{_eid_applied}: 採用→SKU設定 成功 "
                                    f"({_res_b.get('message') or 'applied'})"
                                )
                                adopt_succeeded_eids.add(_eid_applied)
                                # 依頼ボード#11 (2026-06-12): 仕入先候補タブと同様、
                                # 採用成功後に写真/description 生成プロンプトを展開。
                                # meta (url/eid/title) は render 側が DB 補完するため
                                # フラグのみ set (バッチ末尾 st.rerun → タブ先頭に表示)
                                st.session_state[f"_sup_photo_prompt_{_cid_p}"] = True
                                st.session_state[f"_sup_desc_prompt_{_cid_p}"] = True
                                _it_adopted = _eid_to_item.get(_eid_applied)
                                if not _it_adopted:
                                    return
                                try:
                                    _cur_qty_a = int(_it_adopted.get("quantity_ebay") or 0)
                                except (TypeError, ValueError):
                                    _cur_qty_a = 0
                                if _cur_qty_a != 0:
                                    return  # qty 既に >=1 = 復元不要
                                # W114: qty 復元の specific exception + logger.exception
                                try:
                                    _qres = _rev_qty(_eid_applied, 1, **_ebay_creds)
                                except (RuntimeError, ConnectionError, TimeoutError, OSError) as _qe:
                                    logger.exception(
                                        "qty restore exception (Surface A) cid=%s eid=%s",
                                        _cid_p, _eid_applied,
                                    )
                                    st.error(
                                        f"{_eid_applied}: SKU 書換成功だが qty 復元中に例外 "
                                        f"({_qe}). 手動で在庫を 1 に戻してください (cid={_cid_p})."
                                    )
                                    return
                                if _qres.get("success"):
                                    update_ebay_listing_quantity(_eid_applied, 1)
                                    st.success(
                                        f"{_eid_applied}: 在庫 0 → 1 自動復元"
                                    )
                                else:
                                    logger.error(
                                        "qty restore api_failed (Surface A) cid=%s eid=%s msg=%s",
                                        _cid_p, _eid_applied, _qres.get("message"),
                                    )
                                    st.error(
                                        f"{_eid_applied}: SKU 書換成功だが qty 復元失敗 "
                                        f"({_qres.get('message') or 'error'}). "
                                        f"手動で在庫を 1 に戻してください (cid={_cid_p})."
                                    )

                            for _cid in adoptions_pending:
                                _process_apply(_cid, _is_pending=True)
                            for _cid in adoptions_accepted:
                                _process_apply(_cid, _is_pending=False)

                    # 3) 現状維持 → DBに確認済みフラグ
                    for eid in no_change_ids:
                        set_ebay_listing_risk_confirmed(eid, 1)

                    # 4) 変更あり → eBay APIに送信 (採用済 eid は SKU 上書きをスキップ)
                    if sync_targets:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import (
                                revise_inventory_quantity, revise_item_sku,
                            )
                            progress = st.progress(0)
                            for i, ch in enumerate(sync_targets):
                                eid = ch["ebay_item_id"]
                                ok = True
                                if ch["new_qty"] is not None:
                                    r = revise_inventory_quantity(eid, ch["new_qty"], **_ebay_creds)
                                    if r["success"]:
                                        update_ebay_listing_quantity(eid, ch["new_qty"])
                                        st.success(f"{eid}: 在庫 → {ch['new_qty']}")
                                    else:
                                        st.error(f"{eid}: 在庫 → {r['message']}")
                                        ok = False
                                if ch["new_sku"] is not None:
                                    if eid in adopt_succeeded_eids:
                                        st.info(
                                            f"{eid}: SKU手動編集は採用反映と競合のためスキップ"
                                        )
                                    else:
                                        r = revise_item_sku(eid, ch["new_sku"], **_ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_sku(eid, ch["new_sku"])
                                            st.success(f"{eid}: SKU → {ch['new_sku']}")
                                        else:
                                            st.error(f"{eid}: SKU → {r['message']}")
                                            ok = False
                                if ok:
                                    set_ebay_listing_risk_confirmed(eid, 1)
                                progress.progress((i + 1) / len(sync_targets))
                    bump_db_version()  # W134 Step2: 在庫/SKU/risk 一括変更後 read-cache 無効化
                    st.rerun()

            # --- form 外: 候補未探索 SKU の即時探索（form内では button 使えないため分離） ---
            _missing_skus = [
                _it for _it in oos_items
                if not _cand_by_sku.get(_it.get("sku") or "", [])
            ]
            if _missing_skus:
                st.divider()
                st.markdown(
                    f'<div style="font-size:12px;color:#5f6557;margin-bottom:6px;">'
                    f'候補未探索 {len(_missing_skus)}件（次回02:30 Pattern 2バッチで max 50件自動探索／'
                    f'下記ボタンで個別または一括即時探索）'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # --- 一括探索ボタン ---
                _bulk_search_flag = "_oos_bulk_search_fired"
                _bulk_search_result = "_oos_bulk_search_result"
                _bulk_in_progress = st.session_state.get(_bulk_search_flag, False)
                _bulk_result = st.session_state.get(_bulk_search_result)

                _bc1, _bc2 = st.columns([3, 1])
                with _bc1:
                    _bulk_limit = st.number_input(
                        "一括探索する件数 (古い順・qty=0優先)",
                        min_value=1, max_value=len(_missing_skus),
                        value=min(50, len(_missing_skus)),
                        step=10,
                        key="oos_bulk_search_limit",
                        help=f"Claude API 1件あたり ~5秒 + スクレイプ ~30秒。{len(_missing_skus)}件全てで約 {len(_missing_skus) * 35 // 60} 分",
                    )
                with _bc2:
                    if _bulk_in_progress:
                        st.caption("一括探索 実行中…")
                    elif st.button(
                        "一括探索 開始", key="oos_bulk_search_btn",
                        type="primary", width="stretch",
                        help="未探索SKUを上限件数まで即時バックグラウンド探索。完了まで数分〜数十分",
                    ):
                        from tasks.task_supplier_candidate_search import (
                            run_supplier_candidate_search as _run_cs_bulk,
                        )
                        import threading as _th_bulk
                        # 古い順・qty=0優先で N 件選出
                        _sorted_missing = sorted(
                            _missing_skus,
                            key=lambda x: (
                                int(x.get("quantity_ebay") or 0),
                                x.get("source_out_of_stock_since") or "9999",
                            ),
                        )
                        _targets = [
                            (it["ebay_item_id"], it.get("sku") or "")
                            for it in _sorted_missing[:int(_bulk_limit)]
                            if it.get("sku")
                        ]

                        def _bg_bulk_search(targets=_targets, cfg=_cfg,
                                            flag_key=_bulk_search_flag,
                                            result_key=_bulk_search_result):
                            ok_count = 0
                            ng_count = 0
                            errors = []
                            try:
                                for eid, sku in targets:
                                    try:
                                        r = _run_cs_bulk(
                                            ebay_item_id=eid, sku=sku, config=cfg,
                                            discovered_via="ui_bulk_search",
                                        )
                                        if r.get("success"):
                                            ok_count += 1
                                        else:
                                            ng_count += 1
                                            errors.append(f"{sku}: {r.get('message', 'fail')[:40]}")
                                    except Exception as e:
                                        ng_count += 1
                                        errors.append(f"{sku}: 例外 {str(e)[:40]}")
                            finally:
                                st.session_state[result_key] = {
                                    "ok": ok_count, "ng": ng_count,
                                    "total": len(targets),
                                    "errors": errors[:10],  # 最初の10件のみ保持
                                }
                                st.session_state[flag_key] = False

                        _th_bulk.Thread(target=_bg_bulk_search, daemon=True).start()
                        st.session_state[_bulk_search_flag] = True
                        st.session_state.pop(_bulk_search_result, None)  # 前回結果をクリア
                        st.success(
                            f"{len(_targets)}件 の一括探索を開始しました。"
                            f"数分後にページを更新すると候補が表示されます。"
                        )

                # 一括探索の結果表示
                if _bulk_result is not None and not _bulk_in_progress:
                    if _bulk_result["ng"] == 0:
                        st.success(
                            f"一括探索完了: {_bulk_result['ok']}/{_bulk_result['total']} 件"
                            f" 成功"
                        )
                    else:
                        st.warning(
                            f"一括探索完了: 成功 {_bulk_result['ok']}件 / "
                            f"失敗 {_bulk_result['ng']}件 (詳細は下記)"
                        )
                        for _err in _bulk_result.get("errors", []):
                            st.caption(f"  × {_err}")

                _cols_per_row = 3
                for _i_start in range(0, len(_missing_skus), _cols_per_row):
                    _cols_m = st.columns(_cols_per_row)
                    for _j, _it_m in enumerate(_missing_skus[_i_start:_i_start + _cols_per_row]):
                        with _cols_m[_j]:
                            _eid_m = _it_m["ebay_item_id"]
                            _sku_m2 = _it_m.get("sku") or ""
                            _title_m = (_it_m.get("title") or "")[:30]
                            _flag_k = f"_oos_search_fired_{_eid_m}"
                            _result_k_display = f"_oos_search_result_{_eid_m}"
                            _last_result = st.session_state.get(_result_k_display)
                            if st.session_state.get(_flag_k, False):
                                st.caption(f"{_sku_m2}: 探索実行中…")
                            elif _last_result is not None and not _last_result.get("ok"):
                                # 直近の探索失敗を表示、再試行ボタン併設
                                st.caption(
                                    f"{_sku_m2}: 前回失敗 — {_last_result.get('msg', '')[:60]}"
                                )
                                if _sku_m2 and st.button(
                                    f"再試行 {_sku_m2}",
                                    key=f"oos_search_retry_{_eid_m}",
                                    help=f"{_title_m} の候補探索を再試行",
                                    width="stretch",
                                ):
                                    # 失敗結果をクリアして再実行フローへフォールスルー
                                    st.session_state.pop(_result_k_display, None)
                                    st.rerun()
                            elif _sku_m2 and st.button(
                                f"探索 {_sku_m2}",
                                key=f"oos_search_outside_{_eid_m}",
                                help=f"{_title_m} の候補を即時探索（1〜2分・バックグラウンド）",
                                width="stretch",
                            ):
                                from tasks.task_supplier_candidate_search import (
                                    run_supplier_candidate_search as _run_cs,
                                )
                                import threading as _th
                                _sku_tgt = _sku_m2
                                _eid_tgt = _eid_m
                                # 2026-04-20 修正 (HIGH-1): silent failure 対策
                                # 旧: except Exception: pass + flag_k 残置 → 失敗 SKU が再試行不能
                                # 新: 例外も結果も session_state に保存、flag_k は finally で必ず下ろす
                                _result_k = f"_oos_search_result_{_eid_m}"
                                def _bg_cs(eid=_eid_tgt, sku=_sku_tgt, cfg=_cfg,
                                           result_key=_result_k, flag_key=_flag_k):
                                    try:
                                        r = _run_cs(
                                            ebay_item_id=eid, sku=sku, config=cfg,
                                            discovered_via="ui_on_demand",
                                        )
                                        st.session_state[result_key] = {
                                            "ok": bool(r.get("success")),
                                            "msg": r.get("message") or "",
                                        }
                                    except Exception as e:
                                        st.session_state[result_key] = {
                                            "ok": False, "msg": f"例外: {e}",
                                        }
                                    finally:
                                        st.session_state[flag_key] = False
                                _th.Thread(target=_bg_cs, daemon=True).start()
                                st.session_state[_flag_k] = True
                                st.success(f"{_sku_m2}: 探索開始。数分後にページ更新してください。")

            # --- form 外: 一括在庫0 ---
            st.divider()
            _bulk_confirm = st.checkbox(
                f"上記 {len(oos_items)}件 の在庫を一括で0にする",
                key="bulk_confirm_oos",
            )
            if _bulk_confirm:
                st.warning(f"{len(oos_items)}件 のeBay在庫を全て0に変更します。")
                if st.button("一括在庫0実行", key="bulk_qty0_oos", type="primary"):
                    if not all(_ebay_creds.values()):
                        st.error("eBay API認証情報が未設定です（設定タブ参照）")
                    else:
                        from monitor.ebay_client import revise_inventory_quantity
                        with st.status(
                            f"{len(oos_items)}件の在庫を0に変更中...", expanded=True,
                        ) as status:
                            success_count = 0
                            fail_count = 0
                            for i, item in enumerate(oos_items):
                                st.write(f"▸ [{i+1}/{len(oos_items)}] {item['ebay_item_id']}")
                                result = revise_inventory_quantity(
                                    item["ebay_item_id"], 0, **_ebay_creds
                                )
                                if result['success']:
                                    update_ebay_listing_quantity(item["ebay_item_id"], 0)
                                    bump_db_version()  # W134 Step2: 在庫0化後 read-cache 無効化
                                    # 業務ロジック: 一括在庫0 実行 = RISK 解消 (販売停止)。
                                    # risk_confirmed は自動セットしない。必要な listing は
                                    # user が個別に「確認」チェックで手動追跡。
                                    success_count += 1
                                else:
                                    fail_count += 1
                            if fail_count == 0:
                                status.update(
                                    label=f"完了 — {success_count}件の在庫を0に変更",
                                    state="complete",
                                )
                            else:
                                status.update(
                                    label=f"完了 — 成功 {success_count}件 / 失敗 {fail_count}件",
                                    state="complete",
                                )
                        st.rerun()
        else:
            st.success("仕入先在庫切れの商品はありません。")

        st.divider()

        # --- 確認不可 (W252: 「仕入先在庫切れ」と同じ表示に統一) ---
        # user 指示: 確認不可 = 仕入先在庫切れとして処理してよい。表示レイヤーを統一。
        # DB の source_status 値 (not_found) は書き換えない (表示マッピングのみ)。
        st.markdown(f"### 仕入先在庫切れ（確認不可）({len(pnf_items)}件)")
        st.caption("仕入先ページが削除済みまたは確認不可。「仕入先在庫切れ」と同様に出品停止または仕入先変更を検討してください。")
        if pnf_items:
            # OOS と同じ表示フロー (_render_oos_block + 一括実行ボタン)
            # HIGH-4 fix: SKU 規約違反を修正。supplier_candidates は migration v56 で
            # ebay_item_id NOT NULL + UNIQUE(ebay_item_id, candidate_url) 化済み。
            # WHERE ebay_item_id IN (...) で取得し、dict キーも ebay_item_id に統一。
            from monitor.database import get_conn as _pnf_conn
            _pnf_eid_list = [_it["ebay_item_id"] for _it in pnf_items if _it.get("ebay_item_id")]
            _pnf_cand_by_eid: dict[str, list[dict]] = {}
            _pnf_alt_only_count: dict[str, int] = {}
            if _pnf_eid_list:
                with _pnf_conn() as _pcc:
                    _ph = ",".join("?" * len(_pnf_eid_list))
                    for _r in _pcc.execute(
                        f"""SELECT id, sku, ebay_item_id, candidate_url, candidate_title,
                                   candidate_price_jpy, match_score, source_platform,
                                   status, alt_listing_possible,
                                   profit_jpy, profitable,
                                   candidate_image_url
                            FROM supplier_candidates
                            WHERE ebay_item_id IN ({_ph})
                              AND status IN ('pending','accepted','applied')
                              AND COALESCE(alt_listing_possible, 0) = 0
                            ORDER BY ebay_item_id,
                                     (profit_jpy IS NULL), profit_jpy DESC,
                                     match_score DESC""",
                        _pnf_eid_list,
                    ).fetchall():
                        _pnf_cand_by_eid.setdefault(_r["ebay_item_id"], []).append(dict(_r))
                    # HIGH-1' fix: eid キーのまま格納 (sku 橋渡しは同一 sku 共有 listing で
                    # 後勝ち上書き → 誤 caption。_render_oos_block 側で eid 優先 lookup)
                    for _r in _pcc.execute(
                        f"""SELECT ebay_item_id, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE ebay_item_id IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY ebay_item_id""",
                        _pnf_eid_list,
                    ).fetchall():
                        _pnf_alt_only_count[_r["ebay_item_id"]] = _r["alt_n"]

            st.caption(
                "各商品のすぐ下に仕入先候補(上位3件)を表示。候補に「採用」「不採用」をチェック → "
                "下部の「一括実行」ボタンで eBay API への反映と DB 更新が同時に走ります。"
            )

            for _item in pnf_items:
                _pnf_eid_val = _item.get("ebay_item_id") or ""
                _cands_for_eid = _pnf_cand_by_eid.get(_pnf_eid_val, [])[:3]
                _render_oos_block(_item, _cands_for_eid, "pnf", _pnf_alt_only_count)

            st.divider()
            st.caption(
                "採用チェック → SKU 欄が仕入先URLベースに自動更新されます。"
                " 確認後、下のボタンで eBay 反映・DB 更新を一括処理。"
            )

            _pnf_submitted = st.button(
                "選択した採用/不採用/編集を一括実行（eBay・DB）",
                key="pnf_batch_submit",
                type="primary", width="stretch",
            )

            if _pnf_submitted:
                adoptions_pending_pnf: list[int] = []
                adoptions_accepted_pnf: list[int] = []
                rejections_pnf: list[int] = []
                _cid_to_eid_pnf: dict[int, str] = {}
                _cid_to_score_pnf: dict[int, int] = {}
                _eid_to_item_pnf: dict[str, dict] = {
                    _it["ebay_item_id"]: _it for _it in pnf_items
                }
                # HIGH-4 fix: mutex dict キーを ebay_item_id に統一
                _eid_adopt_candidates_pnf: dict[str, list[int]] = {}

                for _it in pnf_items:
                    _eid_for_cand = _it["ebay_item_id"]
                    for _c in _pnf_cand_by_eid.get(_eid_for_cand, [])[:3]:
                        _cid_scan = _c["id"]
                        # HIGH-4 fix: 候補レコードの ebay_item_id 列から直接引く
                        _cid_to_eid_pnf[_cid_scan] = _c["ebay_item_id"]
                        _cid_to_score_pnf[_cid_scan] = _c.get("match_score") or 0
                        if _c.get("status") == "pending":
                            if st.session_state.get(f"pnf_adopt_{_cid_scan}", False):
                                _eid_adopt_candidates_pnf.setdefault(_eid_for_cand, []).append(_cid_scan)
                            elif st.session_state.get(f"pnf_reject_{_cid_scan}", False):
                                rejections_pnf.append(_cid_scan)
                        elif _c.get("status") == "accepted":
                            if st.session_state.get(f"pnf_applyck_{_cid_scan}", False):
                                adoptions_accepted_pnf.append(_cid_scan)

                for _eid_m, _cids_m in _eid_adopt_candidates_pnf.items():
                    if len(_cids_m) == 1:
                        adoptions_pending_pnf.append(_cids_m[0])
                    else:
                        _cids_sorted = sorted(_cids_m, key=lambda x: _cid_to_score_pnf.get(x, 0), reverse=True)
                        _winner = _cids_sorted[0]
                        _losers = _cids_sorted[1:]
                        adoptions_pending_pnf.append(_winner)
                        rejections_pnf.extend(_losers)

                sync_targets_pnf: list[dict] = []
                no_change_ids_pnf: list[str] = []
                for _it in pnf_items:
                    eid = _it["ebay_item_id"]
                    is_checked = bool(st.session_state.get(f"pnf_confirm_{eid}", False))
                    if not is_checked:
                        continue
                    cur_sku = st.session_state.get(f"pnf_sku_{eid}", _it.get("sku") or "")
                    cur_qty = int(st.session_state.get(f"pnf_qty_{eid}", _it["quantity_ebay"]))
                    orig_sku = _it.get("sku") or ""
                    orig_qty = int(_it["quantity_ebay"])
                    new_sku = cur_sku if cur_sku != orig_sku else None
                    new_qty = cur_qty if cur_qty != orig_qty else None
                    if new_sku is not None or new_qty is not None:
                        sync_targets_pnf.append(
                            {"ebay_item_id": eid, "new_sku": new_sku, "new_qty": new_qty}
                        )
                    else:
                        no_change_ids_pnf.append(eid)

                total_adoptions_pnf = len(adoptions_pending_pnf) + len(adoptions_accepted_pnf)
                total_actions_pnf = total_adoptions_pnf + len(rejections_pnf) + len(sync_targets_pnf) + len(no_change_ids_pnf)
                if total_actions_pnf == 0:
                    st.warning("チェックが入っている未処理商品がありません")
                else:
                    st.info(
                        f"処理中: 採用 {total_adoptions_pnf}件 / 不採用 {len(rejections_pnf)}件 "
                        f"/ 変更あり {len(sync_targets_pnf)}件 / 現状維持 {len(no_change_ids_pnf)}件"
                    )

                    for _cid in rejections_pnf:
                        try:
                            update_supplier_candidate_status(_cid, "rejected")
                        except Exception as _e:
                            logger.error("pnf_batch: 不採用記録失敗 cid=%s: %s", _cid, _e)
                            st.error(f"不採用 cid={_cid}: {_e}")
                    if rejections_pnf:
                        st.success(f"不採用 {len(rejections_pnf)}件 を記録しました")

                    adopt_succeeded_eids_pnf: set[str] = set()
                    if total_adoptions_pnf > 0:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import revise_inventory_quantity as _rev_qty_pnf

                            def _process_apply_pnf(_cid_p: int, _is_pending: bool) -> None:
                                _eid_applied = _cid_to_eid_pnf.get(_cid_p, "?")
                                if _is_pending:
                                    _res_a = accept_supplier_candidate(_cid_p)
                                    if not _res_a.get("success"):
                                        _msg_a = _res_a.get('message') or 'accept失敗'
                                        logger.error("pnf_batch: 採用失敗 cid=%s eid=%s: %s", _cid_p, _eid_applied, _msg_a)
                                        st.error(f"採用 cid={_cid_p}: {_msg_a}")
                                        return
                                _res_b = apply_supplier_candidate(_cid_p, _cfg)
                                if not _res_b.get("success"):
                                    _msg_b = _res_b.get('message') or 'apply エラー'
                                    logger.error("pnf_batch: eBay 反映失敗 eid=%s cid=%s: %s", _eid_applied, _cid_p, _msg_b)
                                    st.error(f"{_eid_applied}: eBay 反映失敗: {_msg_b} (cid={_cid_p})")
                                    return
                                st.success(f"{_eid_applied}: 採用→SKU設定 成功 ({_res_b.get('message') or 'applied'})")
                                adopt_succeeded_eids_pnf.add(_eid_applied)
                                # 依頼ボード#11 (2026-06-12): OOS 経路と同様、採用成功後に
                                # 写真/description 生成プロンプトを展開 (meta は DB 補完)
                                st.session_state[f"_sup_photo_prompt_{_cid_p}"] = True
                                st.session_state[f"_sup_desc_prompt_{_cid_p}"] = True
                                _it_adopted = _eid_to_item_pnf.get(_eid_applied)
                                if not _it_adopted:
                                    return
                                try:
                                    _cur_qty_a = int(_it_adopted.get("quantity_ebay") or 0)
                                except (TypeError, ValueError):
                                    _cur_qty_a = 0
                                if _cur_qty_a != 0:
                                    return
                                try:
                                    _qres = _rev_qty_pnf(_eid_applied, 1, **_ebay_creds)
                                except (RuntimeError, ConnectionError, TimeoutError, OSError) as _qe:
                                    logger.exception("pnf_batch: qty 復元中に例外 eid=%s cid=%s", _eid_applied, _cid_p)
                                    st.error(f"{_eid_applied}: qty 復元中に例外 ({_qe}). 手動で在庫を 1 に戻してください (cid={_cid_p}).")
                                    return
                                if _qres.get("success"):
                                    update_ebay_listing_quantity(_eid_applied, 1)
                                    st.success(f"{_eid_applied}: 在庫 0 → 1 自動復元")
                                else:
                                    _msg_q = _qres.get('message') or 'error'
                                    logger.error("pnf_batch: qty 復元失敗 eid=%s cid=%s: %s", _eid_applied, _cid_p, _msg_q)
                                    st.error(f"{_eid_applied}: qty 復元失敗 ({_msg_q}). 手動で在庫を 1 に戻してください (cid={_cid_p}).")

                            for _cid in adoptions_pending_pnf:
                                _process_apply_pnf(_cid, _is_pending=True)
                            for _cid in adoptions_accepted_pnf:
                                _process_apply_pnf(_cid, _is_pending=False)

                    for eid in no_change_ids_pnf:
                        set_ebay_listing_risk_confirmed(eid, 1)

                    if sync_targets_pnf:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import (
                                revise_inventory_quantity, revise_item_sku,
                            )
                            progress = st.progress(0)
                            for i, ch in enumerate(sync_targets_pnf):
                                eid = ch["ebay_item_id"]
                                ok = True
                                if ch["new_qty"] is not None:
                                    r = revise_inventory_quantity(eid, ch["new_qty"], **_ebay_creds)
                                    if r["success"]:
                                        update_ebay_listing_quantity(eid, ch["new_qty"])
                                        st.success(f"{eid}: 在庫 → {ch['new_qty']}")
                                    else:
                                        logger.error("pnf_batch: qty 変更失敗 eid=%s: %s", eid, r['message'])
                                        st.error(f"{eid}: 在庫 → {r['message']}")
                                        ok = False
                                if ch["new_sku"] is not None:
                                    if eid in adopt_succeeded_eids_pnf:
                                        st.info(f"{eid}: SKU手動編集は採用反映と競合のためスキップ")
                                    else:
                                        r = revise_item_sku(eid, ch["new_sku"], **_ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_sku(eid, ch["new_sku"])
                                            st.success(f"{eid}: SKU → {ch['new_sku']}")
                                        else:
                                            logger.error("pnf_batch: SKU 変更失敗 eid=%s: %s", eid, r['message'])
                                            st.error(f"{eid}: SKU → {r['message']}")
                                            ok = False
                                if ok:
                                    set_ebay_listing_risk_confirmed(eid, 1)
                                progress.progress((i + 1) / len(sync_targets_pnf))
                    bump_db_version()
                    st.rerun()
        else:
            st.success("確認不可の商品はありません。")

    # ---------- 監視リスト ----------
    with monitor_tab1:
        h1, h2, h3 = st.columns([3, 1, 1])
        with h1:
            st.subheader("監視中アイテム")
        with h2:
            if st.button("eBay同期"):
                app_id = s.get("ebay_app_id", "")
                dev_id = s.get("ebay_dev_id", "")
                cert_id = s.get("ebay_cert_id", "")
                user_token = s.get("ebay_user_token", "")
                if not all([app_id, dev_id, cert_id, user_token]):
                    st.error("eBay API認証情報が未設定です（設定タブ参照）")
                else:
                    with st.spinner("eBay APIから取得中..."):
                        try:
                            from monitor.ebay_client import get_active_listings, filter_items_with_sku
                            listings = get_active_listings(app_id, dev_id, cert_id, user_token)
                            sku_items = filter_items_with_sku(listings)
                            synced, skipped = 0, 0
                            for item in sku_items:
                                cfg = find_site_config_by_sku(item["sku"])
                                if cfg:
                                    upsert_item(sku=item["sku"], ebay_item_id=item["item_id"], title=item["title"])
                                    synced += 1
                                else:
                                    skipped += 1
                            st.success(f"同期完了: {synced}件 / スキップ: {skipped}件（変換URL未設定）")
                            st.rerun()
                        except Exception as e:
                            st.error(f"同期エラー: {e}")
        with h3:
            if st.button("▶ 全件チェック", type="primary"):
                items = get_active_items()
                if not items:
                    st.warning("監視中のアイテムがありません。")
                else:
                    configs = get_site_configs()
                    configs_by_prefix = {c["convert_url"]: c for c in configs}
                    batch = prepare_batch_items(items, configs_by_prefix)
                    if not batch:
                        st.warning("チェック可能なアイテムがありません。")
                    else:
                        with st.spinner(f"{len(batch)}件をバッチチェック中...（ブラウザ再利用で高速化）"):
                            try:
                                results = check_items_batch(batch)
                            except Exception as e:
                                st.error(f"チェックエラー: {e}")
                                results = {}
                        webhook = s.get("discord_webhook_url", "")
                        items_by_id = {item["id"]: item for item in items}
                        notified = 0
                        for item_id, status in results.items():
                            item = items_by_id.get(item_id)
                            if not item:
                                continue
                            prev = get_prev_status(item_id)
                            update_item_status(item_id, status)
                            discord_sent = False
                            if status in ("unavailable", "not_found") and prev not in ("unavailable", "not_found"):
                                item["last_status"] = status
                                discord_sent = send_unavailable_alert(webhook, item)
                                if discord_sent:
                                    notified += 1
                            add_check_log(item_id, status, discord_sent)
                        st.success(f"全件チェック完了: {len(results)}件チェック / {notified}件通知")
                        st.rerun()

        # 手動追加フォーム
        _show_manual_add = st.checkbox("手動登録（eBay SKU）", key="chk_manual_add")
        if _show_manual_add:
            mc1, mc2, mc3 = st.columns([2, 2, 1])
            with mc1:
                new_sku = st.text_input(
                    "eBay SKU（カスタムラベル）",
                    placeholder="例: ebayme_m12345678",
                    key="new_sku",
                )
            with mc2:
                new_title = st.text_input("メモ（任意）", key="new_title")
            with mc3:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("追加", key="add_item"):
                    if new_sku:
                        cfg = find_site_config_by_sku(new_sku)
                        url = build_source_url(new_sku)
                        if not cfg:
                            st.error("対応する変換URLが見つかりません。サイト設定タブで確認してください。")
                        else:
                            add_item_manual(sku=new_sku, title=new_title)
                            st.success(f"追加: {new_sku} → {url}")
                            st.rerun()
                    else:
                        st.error("SKUを入力してください。")

        st.divider()

        # アイテム一覧テーブル
        STATUS_EMOJI = {
            "available": "●", "unavailable": "○",
            "not_found": "◆", "error": "[!]", "unknown": "[?]",
        }
        STATUS_LABEL = {
            "available": "在庫あり", "unavailable": "在庫切れ",
            "not_found": "ページなし", "error": "エラー", "unknown": "未チェック",
        }

        all_items = get_all_items()
        if not all_items:
            st.info("監視アイテムがありません。eBay同期か手動追加で登録してください。\n\nSKUフォーマット例: `ebayme_m12345678` / `ebayrm_abc123` / `ebayh_xyz789`")
        else:
            # W222-E (2026-06-05 user 要望): per-row columns+divider の冗長レイアウトを
            # 確認不可一覧と同様のコンパクトな表 (st.dataframe) に変更。一覧は表で俯瞰し、
            # 個別操作 (チェック/在庫0/削除) は下の selectbox で対象を選んで実行する。
            import pandas as pd

            _status_filter = st.multiselect(
                "状態フィルタ",
                options=list(STATUS_LABEL.values()),
                default=[],
                key="inv_watch_status_filter",
                help="未選択 = 全件表示。在庫切れ/ページなし だけ見たい時に絞り込み。",
            )
            _label_to_status = {v: k for k, v in STATUS_LABEL.items()}
            _wanted = {_label_to_status[v] for v in _status_filter} if _status_filter else None

            _rows = []
            _items_by_key = {}
            for item in all_items:
                status = item.get("last_status", "unknown")
                if _wanted is not None and status not in _wanted:
                    continue
                ebay_item_id = item.get("ebay_item_id", "") or ""
                sku = item.get("sku", "") or ""
                _key = sku or ebay_item_id or str(item.get("id"))
                _items_by_key[_key] = item
                _rows.append({
                    "状態": f'{STATUS_EMOJI.get(status, "[?]")} {STATUS_LABEL.get(status, status)}',
                    "Item ID": ebay_item_id or "-",
                    "eBay": f"https://www.ebay.com/itm/{ebay_item_id}" if ebay_item_id else "",
                    "SKU": sku,
                    "メモ": (item.get("title") or "")[:40],
                    "仕入元URL": item.get("source_url", "") or "",
                    "最終確認": str(item.get("last_check") or "")[:16],
                })

            st.caption(f"監視 {len(_rows)} 件" + (f" / 全 {len(all_items)} 件中" if _wanted else ""))
            st.dataframe(
                pd.DataFrame(_rows),
                hide_index=True,
                width="stretch",
                height=460,
                column_config={
                    "状態": st.column_config.TextColumn("状態", width="small"),
                    "Item ID": st.column_config.TextColumn("Item ID", width="small"),
                    "eBay": st.column_config.LinkColumn("eBay", display_text="開く", width="small"),
                    "SKU": st.column_config.TextColumn("SKU", width="medium"),
                    "メモ": st.column_config.TextColumn("メモ", width="medium"),
                    "仕入元URL": st.column_config.LinkColumn("仕入元", display_text="リンク", width="small"),
                    "最終確認": st.column_config.TextColumn("最終確認", width="small"),
                },
            )

            # 個別操作 (対象を選んで チェック / 在庫0 / 削除)
            st.caption("個別操作:")
            _act_label = st.selectbox(
                "操作対象",
                options=["—"] + [
                    f"{k} — {(_items_by_key[k].get('title') or '')[:30]}"
                    for k in _items_by_key
                ],
                key="inv_watch_action_target",
                label_visibility="collapsed",
            )
            if _act_label != "—":
                _sel_key = _act_label.split(" — ")[0]
                _it = _items_by_key.get(_sel_key)
                if _it:
                    _sku = _it.get("sku", "") or ""
                    _src = _it.get("source_url", "") or ""
                    _eid = _it.get("ebay_item_id", "") or ""
                    _ac1, _ac2, _ac3 = st.columns(3)
                    with _ac1:
                        if st.button("▶ 今すぐチェック", key=f"chk_{_it['id']}"):
                            cfg = find_site_config_by_sku(_sku)
                            if cfg and _src:
                                with st.spinner("チェック中..."):
                                    new_status = check_item_by_config(_it, cfg)
                                update_item_status(_it["id"], new_status)
                                add_check_log(_it["id"], new_status)
                                st.rerun()
                            else:
                                st.error("サイト設定が見つかりません")
                    with _ac2:
                        if st.button("eBay在庫を0に", key=f"qty0_{_it['id']}"):
                            if not _eid:
                                st.error("Item IDなし")
                            else:
                                ebay_creds = {
                                    'app_id': s.get("ebay_app_id", ""),
                                    'dev_id': s.get("ebay_dev_id", ""),
                                    'cert_id': s.get("ebay_cert_id", ""),
                                    'user_token': s.get("ebay_user_token", ""),
                                }
                                if not all(ebay_creds.values()):
                                    st.error("eBay API未設定")
                                else:
                                    from monitor.ebay_client import revise_inventory_quantity
                                    result = revise_inventory_quantity(_eid, 0, **ebay_creds)
                                    if result['success']:
                                        update_ebay_listing_quantity(_eid, 0)
                                        bump_db_version()  # W134 Step2: 在庫0化後 read-cache 無効化
                                        st.success(f"{_eid} qty set to 0")
                                        st.rerun()
                                    else:
                                        st.error(result['message'])
                    with _ac3:
                        if st.button("削除", key=f"del_{_it['id']}"):
                            delete_item(_it["id"])
                            st.rerun()

        # チェック履歴
        _show_check_logs = st.checkbox("チェックログ（直近20件）", key="chk_check_logs")
        if _show_check_logs:
            logs = get_recent_logs(20)
            if logs:
                log_data = [{
                    "時刻": str(log["checked_at"])[:16],
                    "SKU": log.get("sku", ""),
                    "状態": STATUS_EMOJI.get(log["status"], "[?]") + " " + STATUS_LABEL.get(log["status"], log["status"]),
                    "Discord": "[OK]" if log["discord_sent"] else "-",
                } for log in logs]
                st.dataframe(pd.DataFrame(log_data), hide_index=True, width="stretch")
            else:
                st.info("履歴がありません。")

    # ---------- サイト設定 ----------
    with monitor_tab2:
        st.subheader("サイト別設定")
        st.caption("変換URLはeBay SKUのプレフィックス。例: `ebayme_` → メルカリ `https://jp.mercari.com/item/m` + 商品ID")

        configs = get_site_configs()

        # 設定テーブル（編集用）
        col_headers = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 2, 1.2, 0.5])
        labels = ["サイト名", "販売中1", "販売中2", "売切テキスト", "ページなしテキスト", "変換URL", "共通URL（仕入元ベースURL）", "有効", "削除"]
        for col, label in zip(col_headers, labels):
            col.markdown(f"**{label}**")
        st.divider()

        for cfg in configs:
            row = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 2, 1.2, 0.5])
            cfg_id = cfg["id"]

            updated = dict(cfg)
            updated["site_name"] = row[0].text_input("サイト名", value=cfg["site_name"], key=f"sn_{cfg_id}", label_visibility="collapsed")
            updated["in_stock_text1"] = row[1].text_input("在庫テキスト1", value=cfg.get("in_stock_text1", ""), key=f"is1_{cfg_id}", label_visibility="collapsed")
            updated["in_stock_text2"] = row[2].text_input("在庫テキスト2", value=cfg.get("in_stock_text2", ""), key=f"is2_{cfg_id}", label_visibility="collapsed")
            updated["sold_out_text"] = row[3].text_input("売切テキスト", value=cfg.get("sold_out_text", ""), key=f"so_{cfg_id}", label_visibility="collapsed")
            updated["no_page_text"] = row[4].text_input("ページなしテキスト", value=cfg.get("no_page_text", ""), key=f"np_{cfg_id}", label_visibility="collapsed")
            updated["convert_url"] = row[5].text_input("変換URL", value=cfg.get("convert_url", ""), key=f"cu_{cfg_id}", label_visibility="collapsed")
            updated["common_url"] = row[6].text_input("共通URL", value=cfg.get("common_url", ""), key=f"comu_{cfg_id}", label_visibility="collapsed")
            updated["is_active"] = int(row[7].checkbox("有効", value=bool(cfg.get("is_active", 1)), key=f"act_{cfg_id}", label_visibility="collapsed"))

            if row[8].button("DEL", key=f"del_cfg_{cfg_id}"):
                delete_site_config(cfg_id)
                st.rerun()

            # 変更があれば自動保存（保存ボタンで一括保存）
            st.session_state[f"cfg_edit_{cfg_id}"] = updated

        st.divider()
        save_col, add_col = st.columns([1, 3])
        with save_col:
            if st.button("設定を保存", type="primary", key="save_site_cfg"):
                for cfg in configs:
                    cfg_id = cfg["id"]
                    edited = st.session_state.get(f"cfg_edit_{cfg_id}")
                    if edited:
                        save_site_config(edited)
                st.success("サイト設定を保存しました。")
                st.rerun()

        with add_col:
            _show_site_add = st.checkbox("サイト新規追加", key="chk_site_add")
            if _show_site_add:
                nc1, nc2, nc3, nc4 = st.columns(4)
                new_cfg_name = nc1.text_input("サイト名", key="new_cfg_name")
                new_cfg_convert = nc2.text_input("変換URL（例: ebayXX_）", key="new_cfg_convert")
                new_cfg_common = nc3.text_input("共通URL", key="new_cfg_common")
                new_cfg_instock = nc4.text_input("販売中テキスト", key="new_cfg_instock")
                if st.button("追加", key="add_cfg"):
                    if new_cfg_name and new_cfg_convert:
                        save_site_config({
                            "site_name": new_cfg_name,
                            "convert_url": new_cfg_convert,
                            "common_url": new_cfg_common,
                            "in_stock_text1": new_cfg_instock,
                            "in_stock_text2": "",
                            "sold_out_text": "",
                            "no_page_text": "",
                        })
                        st.success(f"追加しました: {new_cfg_name}")
                        st.rerun()
                    else:
                        st.error("サイト名と変換URLは必須です。")
