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


def _run_candidate_search_in_subprocess(
    ebay_item_id: str, sku: str, discovered_via: str, timeout_sec: int = 600
) -> dict:
    """run_supplier_candidate_search を別プロセスで実行 (board#19 root cause 修正).

    Streamlit プロセスは tornado 互換のため WindowsSelectorEventLoopPolicy が
    プロセス全体に設定され、Windows の SelectorEventLoop は asyncio subprocess
    非対応 → Playwright sync API がどの thread でも NotImplementedError で即死し、
    search_* 内の except Exception が握りつぶして空リストを返す
    = UI 即時探索が常に「偽の市場 0 件」になっていた (W228 FIX-E と同根)。
    フレッシュな子プロセスは既定の ProactorEventLoop で Playwright が正常動作する。

    Q0: 環境エラー (起動失敗 / timeout / 出力解析不能) は success=False で返し、
    「市場で類似商品が見つかりませんでした」(success=True + found=0) と必ず区別する。

    Returns: run_supplier_candidate_search と同形の dict。
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    import sys as _sys

    _base = Path(__file__).resolve().parent.parent
    _flags = _sp.CREATE_NO_WINDOW if _sys.platform == "win32" else 0
    try:
        proc = _sp.run(
            [_sys.executable, "-m", "tasks.supplier_search_cli",
             ebay_item_id, discovered_via],
            input=sku + "\n",
            capture_output=True, text=True, encoding="utf-8",
            # reviewer MEDIUM-2 (2026-06-13): 子の stderr encoding は親 env 継承に
            # 依存させず明示 (house パターン: supplier_scraper.py L430 と同)。
            # errors="replace" で万一の混入 byte でも reader thread を死なせない。
            errors="replace",
            env={**_os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            timeout=timeout_sec, cwd=str(_base), creationflags=_flags,
        )
    except _sp.TimeoutExpired:
        logger.warning("supplier_search_cli timeout eid=%s timeout=%ss",
                       ebay_item_id, timeout_sec)
        return {"success": False, "found": 0, "persisted": 0,
                "message": f"探索プロセス timeout ({timeout_sec}秒) — 環境エラー (市場0件ではありません)"}
    except OSError as e:
        logger.warning("supplier_search_cli 起動失敗 eid=%s err=%r", ebay_item_id, e)
        return {"success": False, "found": 0, "persisted": 0,
                "message": f"探索プロセス起動失敗: {e}"}

    if proc.returncode != 0:
        # reviewer MEDIUM-1 (2026-06-13): UI message は揮発 (session_state) のため
        # 子の traceback 末尾を必ず log に恒久記録 (board#19 の診断経路を閉じない)
        logger.warning("supplier_search_cli 異常終了 eid=%s exit=%s stderr_tail=%s",
                       ebay_item_id, proc.returncode, (proc.stderr or "")[-2000:])
        return {"success": False, "found": 0, "persisted": 0,
                "message": f"探索プロセス異常終了 (exit={proc.returncode}): "
                           f"{(proc.stderr or '')[-200:]}"}

    # stdout から RESULT_JSON: マーカー行を後方探索 (pipeline の print/log 汚染耐性)
    _marker = "RESULT_JSON:"
    payload = None
    for _line in reversed((proc.stdout or "").splitlines()):
        if _line.startswith(_marker):
            try:
                payload = _json.loads(_line[len(_marker):])
            except _json.JSONDecodeError:
                payload = None
            break
    if payload is None:
        logger.warning("supplier_search_cli 出力解析失敗 eid=%s stdout_tail=%s",
                       ebay_item_id, (proc.stdout or "")[-2000:])
        return {"success": False, "found": 0, "persisted": 0,
                "message": f"探索プロセス出力解析失敗: {(proc.stdout or '')[-200:]}"}
    if not payload.get("ok"):
        logger.warning("supplier_search_cli 内部エラー eid=%s error=%s",
                       ebay_item_id, payload.get("error"))
        return {"success": False, "found": 0, "persisted": 0,
                "message": f"探索プロセス内エラー: {payload.get('error')}"}
    return payload.get("result") or {
        "success": False, "found": 0, "persisted": 0,
        "message": "探索プロセスが空の結果を返しました",
    }


def render_inventory_monitor_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    import json
    from monitor.database import add_check_log, add_item_manual, build_source_url, delete_item, delete_site_config, find_site_config_by_sku, get_active_items, get_all_items, get_conn, get_prev_status, get_recent_logs, get_site_configs, save_site_config, set_ebay_listing_risk_confirmed, update_ebay_listing_quantity, update_item_status, update_supplier_candidate_status, upsert_item
    from monitor.notifier import send_unavailable_alert
    from monitor.scrapers import check_item_by_config, check_items_batch, prepare_batch_items
    from ui_cache import bump_db_version, get_db_version

    # 依頼ボード#11 (2026-06-12): 採用バッチ成功後の写真/description フォロー
    # アップ欄を在庫監視タブでも展開 (仕入先候補タブと共通 section)。
    # _process_apply 成功時に立てた _sup_photo_prompt_/_sup_desc_prompt_ を
    # ここで描画 (バッチ末尾の st.rerun 後にタブ最上部へ出る)。
    from tabs._supplier_followup_section import render_supplier_followup_section
    if render_supplier_followup_section(source_tab="inventory"):
        st.markdown("---")

    # W314 Phase 3 T2 (2026-07-03): 「供給リスク (自動検知)」と「手動監視」の
    # 2 概念が同じ内側タブ群に同居し混同されやすい。ラベル + 1 行説明で明示分離
    # (内側タブ構造自体は維持、K1)。
    monitor_tab_risk, monitor_tab1, monitor_tab2 = st.tabs(
        ["⚠ 供給リスク (自動検知)", "👁 手動監視 (自分で登録)", "サイト設定"]
    )

    # ---------- 要対応（仕入先在庫リスク） ----------
    with monitor_tab_risk:
        st.caption(
            "ebay_listings の在庫チェック結果から自動検知。仕入先で購入できない"
            "のに eBay 在庫が残っている商品を一覧表示します。"
        )
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

        # 依頼ボード#18: 採用/在庫0/様子見 操作の結果通知 (rerun 跨ぎ queue を pop して表示)
        for _kind, _msg in st.session_state.pop("_inv_action_notice", []):
            {"success": st.success, "error": st.error, "info": st.info}.get(_kind, st.info)(_msg)

        # 2026-06-05 user 要望: Yahoo 24h 再出品猶予 (W100 grace) の独立「再出品待ち」
        # 表示セクションを削除。Yahoo 終了/売切も要対応/確認不可の一覧に出す。
        # 依頼ボード#20 (2026-06-14): grace 自体は撤廃されておらず検索タイミングは健在
        # (落札なし終了=24h猶予 / 売切=即時)。独立セクションは作らず、要対応一覧の各カード内に
        # 「オークション終了（落札者なし）」バナーとして区別表示する (_render_oos_block 内)。

        def _notice(kind: str, msg: str) -> None:
            """rerun を跨いで表示する通知 queue (タブ冒頭で pop して表示)."""
            st.session_state.setdefault("_inv_action_notice", []).append((kind, msg))

        def _adopt_and_apply(
            cand: dict, item: dict, is_pending: bool = True,
            open_editor: bool = True,
        ) -> bool:
            """仕入先候補を採用 → eBay へ SKU 反映まで 1 操作で実行 (依頼ボード#18).

            実行部 (accept→apply→followup フラグ set→qty 復元) は
            tabs._adopt_candidate.adopt_candidate に単一化済 (W314 Phase 3 T1、
            仕入先候補タブの 1 クリック採用と同一の共有実装)。本関数はタブ固有の
            UI 前処理 (eid mismatch ログ / 認証事前チェック) と通知 queue への
            変換のみを担う。
            通知は _inv_action_notice queue 経由 (呼出側が st.rerun(scope="app"))。

            open_editor (2026-07-03 user フィードバック #1):
              True (default) = 従来動作 (adopt + パネル展開)
              False = SKUのみ切替 (followup パネルは開かない)

            注: _ebay_creds / _cfg は後方の共通スコープ定義を closure 参照
            (呼出は render 時の fragment 内 = 定義後なので未定義参照にならない)。
            """
            from tabs._adopt_candidate import adopt_candidate

            cid = cand["id"]
            # reviewer HIGH-1: SKU 反映 (apply_supplier_candidate) は候補行の
            # ebay_item_id に対して走るため、followup meta の eid も候補行を正とする
            # (カード側 item と食い違えば写真/description が別 listing に飛ぶ)。
            eid = cand.get("ebay_item_id") or item["ebay_item_id"]
            if cand.get("ebay_item_id") and cand["ebay_item_id"] != item["ebay_item_id"]:
                logger.warning(
                    "adopt eid mismatch card=%s cand=%s cid=%s",
                    item["ebay_item_id"], cand["ebay_item_id"], cid)
            title_s = (item.get("title") or "")[:40]
            if not all(_ebay_creds.values()):
                _notice("error", "eBay API認証情報が未設定です（設定タブ参照）")
                return False
            with st.spinner("仕入先の在庫を確認中..."):
                res = adopt_candidate(
                    cid, _cfg, source_tab="inventory", is_pending=is_pending,
                    open_editor=open_editor,
                )
            if not res.get("success"):
                logger.error(
                    "adopt failed (inventory tab) cid=%s eid=%s stage=%s msg=%s",
                    cid, eid, res.get("stage"), res.get("message"))
                if res.get("stage") == "apply":
                    _notice("error",
                            f"{title_s}: eBay 反映に失敗しました — "
                            f"{res.get('message') or 'apply エラー'}。"
                            f"候補は採用済みのまま残るので「反映」ボタンで再試行できます。")
                else:
                    _notice("error",
                            f"{title_s}: 採用に失敗しました — "
                            f"{res.get('message') or 'accept失敗'}")
                return False
            _suffix = "（SKUのみ、編集パネル非展開）" if not open_editor else ""
            _notice("success", f"{title_s}: 採用 → eBay SKU 反映 完了{_suffix}")
            if res.get("qty_restore_message"):
                _notice(
                    "success" if res.get("qty_restore_ok") else "error",
                    res["qty_restore_message"],
                )
            return True

        # --- 在庫切れ（インライン候補表示版） ---
        def _on_reject_inv(cid: int, title: str) -> None:
            """不採用ボタン (on_click): fragment 再実行前に DB 更新 + hide フラグ.

            仕入先候補タブの不採用と同パターン (1 往復で高速)。fragment rerun では
            外側の候補 query が再実行されないため、_inv_rejected_{cid} フラグで
            該当候補カードを非表示化する (依頼ボード#18 / 2026-06-13)。
            """
            try:
                update_supplier_candidate_status(cid, "rejected")
                st.session_state[f"_inv_rejected_{cid}"] = True
            except Exception as _e:
                logger.exception("supplier reject failed (inventory tab) cid=%s", cid)
                _notice("error", f"{title}: 不採用の記録に失敗しました — {_e}")

        @st.fragment
        def _render_oos_block(
            item: dict,
            cands: list[dict],
            section_key: str,
            alt_only_count_by_eid: dict[str, int],
        ) -> None:
            """
            1 商品分のブロックを描画（OOS情報 + 仕入先候補 up to 3件をインライン）。

            依頼ボード#18 (2026-06-13) 全面簡素化:
            - 候補あり: 「採用」1 クリックで accept → apply (eBay SKU 反映) まで即実行、
              「不採用」は on_click で DB 更新 + hide フラグ (仕入先候補タブと同パターン)
            - 候補なし: 「はい、在庫を0にする」「いいえ、このまま様子見」の 2 ボタン
            - 旧 UI (確認チェック / SKU・在庫直接編集 / 一括実行) は撤去 (git 履歴参照)

            引数:
              item: oos_items の 1 要素
              cands: 対象商品の仕入先候補 list[dict]（status IN ('pending','accepted','applied')、
                     score降順、最大3件）
              section_key: widget key プレフィクス（例 "oos"）
              alt_only_count_by_eid: ebay_item_id 毎の alt_listing_possible=1 候補数 dict.
                cands=0 件 + alt_only>0 のとき「探索済 (置換候補なし)」caption を出す.
                2026-05-05 NameError バグ fix で引数化 (旧: 別関数のローカル参照でクラッシュ).
                2026-06-13 reviewer HIGH-1 fix で OOS/PNF 両経路とも eid キーに統一.
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

            with st.container(border=True):
                # --- ヘッダ行: Item ID / タイトル / 価格 / ランク / 在庫 ---
                _h1, _h2, _h3, _h4, _h5 = st.columns([1.6, 5.0, 0.9, 0.9, 0.9])
                with _h1:
                    st.markdown(
                        f'<div style="font-family:var(--font-mono,monospace);'
                        f'font-size:12px;color:#2a2e2a;padding-top:6px;">'
                        f'<a href="https://www.ebay.com/itm/{html.escape(eid)}" target="_blank" '
                        f'style="color:#156a63;text-decoration:none;">{html.escape(eid)}</a></div>',
                        unsafe_allow_html=True,
                    )
                with _h2:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">'
                        f'{html.escape(title)}</div>',
                        unsafe_allow_html=True,
                    )
                with _h3:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">{price_str}</div>',
                        unsafe_allow_html=True,
                    )
                with _h4:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">ランク{rank}</div>',
                        unsafe_allow_html=True,
                    )
                with _h5:
                    st.markdown(
                        f'<div style="font-size:12px;color:#2a2e2a;padding-top:6px;">在庫{qty_orig}</div>',
                        unsafe_allow_html=True,
                    )

                # --- 情報行: SKU(表示のみ) / 仕入先 / 仕入先URL ---
                _e1, _e2, _e3 = st.columns([3.6, 1.8, 2.4])
                with _e1:
                    st.markdown(
                        f'<div style="font-size:11px;color:#8d927f;padding-top:4px;">'
                        f'SKU: {html.escape(sku_orig) if sku_orig else "-"}</div>',
                        unsafe_allow_html=True,
                    )
                with _e2:
                    st.markdown(
                        f'<div style="font-size:11px;color:#8d927f;padding-top:4px;">'
                        f'仕入先: {html.escape(source)}</div>',
                        unsafe_allow_html=True,
                    )
                with _e3:
                    if source_url:
                        st.markdown(
                            f'<div style="padding-top:2px;">'
                            f'<a href="{html.escape(source_url, quote=True)}" target="_blank" '
                            f'style="color:#156a63;font-size:12px;">仕入先URLを開く</a></div>',
                            unsafe_allow_html=True,
                        )

                # W314 Phase 3 T3 (2026-07-03): 商品管理タブへの導線 (W292 jump 流儀)。
                # 供給リスクカードから直接タイトル/画像/ランクを編集したい時の近道。
                if st.button(
                    "📝 商品管理で開く",
                    key=f"{section_key}_goto_pm_{eid}",
                    help="この商品を商品管理タブで開いて詳細を確認・編集します",
                ):
                    st.session_state["pm_focus_eid"] = eid
                    st.session_state["_w134_sel"] = "商品管理"
                    st.session_state["_w217a_cat_view"] = "★ 毎日"
                    # W174-pm と同方針: fragment 内の st.rerun() は明示 scope 指定が
                    # 確実。商品管理タブへ完全ナビゲートするため app scope。
                    st.rerun(scope="app")

                # --- 依頼ボード#20 (2026-06-14): ヤフオク「落札者なし終了」は
                # 売り切れと区別して「オークション終了（再出品待ち）」と明示。
                # 24h 猶予中 (yahoo_grace_until 未来) = 再出品の可能性を待って
                # 仕入先探索を保留中であることを user に伝える。売り切れ(落札済)は
                # grace を張らず即時再検索のため、このバナーは出ない。
                if item.get("auction_ended_grace"):
                    _grace_disp = item.get("yahoo_grace_until") or ""
                    try:
                        from datetime import datetime as _dtg, timedelta as _tdg
                        _g = _dtg.strptime(_grace_disp, "%Y-%m-%d %H:%M:%S") + _tdg(hours=9)
                        _grace_disp = _g.strftime("%Y-%m-%d %H:%M") + " (JST)"
                    except Exception:
                        pass
                    st.markdown(
                        f'<div style="border-left:4px solid #b8860b;'
                        f'background:rgba(184,134,11,0.08);padding:8px 12px;'
                        f'margin:4px 0;border-radius:4px;font-size:12px;color:#7a5a0a;">'
                        f'🔔 <b>オークション終了（落札者なし）</b> — 再出品される可能性が'
                        f'あるため、{html.escape(_grace_disp)} 以降に自動で仕入先を'
                        f'再確認します。今すぐ販売停止するなら下の「在庫を0にする」を'
                        f'押してください。'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                # 依頼ボード#20 再対応 (2026-06-15): 「採用→再OOS」区別バナー。
                # 過去に採用した仕入先 (1 点物の多い yahoo/mercari) が再び売切れた
                # 正当な再OOS。システム不具合ではなく新規イベントである旨を伝え、
                # 「次の仕入先を探索」で別候補をワンクリック再探索できるようにする。
                if item.get("prev_adopted_at"):
                    _PLAT_JP = {"yahoo_auctions": "ヤフオク", "mercari": "メルカリ",
                                "paypay_furima": "PayPayフリマ"}
                    _plat_disp = _PLAT_JP.get(
                        item.get("prev_adopted_platform") or "",
                        item.get("prev_adopted_platform") or "仕入先")

                    def _jst_date(_raw: str) -> str:
                        # user_action_at / source_out_of_stock_since は UTC 保存
                        # (CURRENT_TIMESTAMP)。JST 日付 (MM-DD) へ変換して表示。
                        try:
                            from datetime import datetime as _dd, timedelta as _td2
                            return (_dd.strptime(_raw[:19], "%Y-%m-%d %H:%M:%S")
                                    + _td2(hours=9)).strftime("%m-%d")
                        except Exception:
                            return (_raw or "")[:10]

                    _adopt_d = _jst_date(item.get("prev_adopted_at") or "")
                    _oos_raw = item.get("source_out_of_stock_since")
                    _oos_d = _jst_date(_oos_raw) if _oos_raw else "?"
                    st.markdown(
                        f'<div style="border-left:4px solid #2e7d5b;'
                        f'background:rgba(46,125,91,0.08);padding:8px 12px;margin:4px 0;'
                        f'border-radius:4px;font-size:12px;color:#1f5a3f;">'
                        f'🔁 <b>採用済みでしたが再び在庫切れ</b> — '
                        f'採用 {html.escape(_adopt_d)} → 在庫切れ {html.escape(_oos_d)}'
                        f'（{html.escape(_plat_disp)}）。採用した仕入先がまた売り切れ'
                        f'ました。下の「次の仕入先を探索」で別候補を探すか、販売停止'
                        f'できます。'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    def _do_resrch() -> None:
                        """『次の仕入先を探索』: 採用先が再OOSした商品の次候補を即時
                        再探索 (board#20)。既存 per-card 探索と同じ subprocess 経路。
                        結果/進行は _inv_resrch_* キーで本バナー内に表示する。"""
                        import threading as _th_r
                        from streamlit.runtime.scriptrunner import (
                            add_script_run_ctx as _ax_r,
                            get_script_run_ctx as _gx_r,
                        )
                        _fk = f"_inv_resrch_fired_{eid}"
                        _rk = f"_inv_resrch_result_{eid}"

                        def _bg(eid=eid, sku=sku_orig, fk=_fk, rk=_rk):
                            try:
                                r = _run_candidate_search_in_subprocess(
                                    ebay_item_id=eid, sku=sku,
                                    discovered_via="ui_reoos_resrch")
                                st.session_state[rk] = {
                                    "ok": bool(r.get("success")),
                                    "msg": r.get("message") or "",
                                    "persisted": int(r.get("persisted") or 0),
                                    "alt": int(r.get("persisted_alt") or 0),
                                }
                            except Exception as _e:
                                st.session_state[rk] = {"ok": False, "msg": f"例外: {_e}"}
                            finally:
                                st.session_state[fk] = False

                        # flag は start() より前に立てる (爆速完了 thread の finally で
                        # 先に False になる race 防止、既存 HIGH-1 と同パターン)
                        st.session_state[_fk] = True
                        st.session_state.pop(_rk, None)
                        _t = _th_r.Thread(target=_bg, daemon=True)
                        _ax_r(_t, _gx_r())
                        _t.start()

                    _rs_fired = st.session_state.get(f"_inv_resrch_fired_{eid}", False)
                    _rs_result = st.session_state.get(f"_inv_resrch_result_{eid}")
                    if _rs_fired:
                        st.caption("次の仕入先を探索中…（1〜2分・終わったらページ更新）")
                    else:
                        _rb1, _rb2 = st.columns([2, 4])
                        with _rb1:
                            if sku_orig and st.button(
                                "次の仕入先を探索",
                                key=f"{section_key}_resrch_{eid}",
                                width="stretch",
                                help="この出品の別の仕入先候補を今すぐ探索します"
                                     "（1〜2分・バックグラウンド）",
                            ):
                                _do_resrch()
                                st.rerun(scope="app")
                        if _rs_result is not None:
                            if _rs_result.get("ok"):
                                _rp = int(_rs_result.get("persisted") or 0)
                                _ra = int(_rs_result.get("alt") or 0)
                                _rmain = max(0, _rp - _ra)
                                if _rmain > 0:
                                    st.caption(
                                        f"探索完了 — 新規候補 {_rmain}件"
                                        f"（ページ更新で上に表示されます）")
                                elif _ra > 0:
                                    st.caption(
                                        f"探索完了 — 別SKU出品機会 {_ra}件のみ"
                                        f"（このカードには出ません。仕入先候補タブで確認）")
                                else:
                                    st.caption(
                                        "探索完了 — 新規候補なし"
                                        "（基準未満／既存と同一／利益不足）")
                            else:
                                st.caption(
                                    f"探索失敗 — {(_rs_result.get('msg') or '')[:60]}")

                st.markdown("---")

                # --- 候補部 (依頼ボード#18: 採用/不採用 1 クリック即実行) ---
                # 不採用 on_click で _inv_rejected_{cid} が立った候補は非表示
                # (fragment 再実行では外側 query が走らないため flag でフィルタ)
                _visible_cands = [
                    _c for _c in cands
                    if not st.session_state.get(f"_inv_rejected_{_c['id']}", False)
                ]
                _had_rejected_now = len(_visible_cands) < len(cands)
                # 採用/反映できる候補 (pending=採用可 / accepted=反映可)。
                # applied のみ (= 採用済みだが採用先が再OOS) は actionable 0 件 →
                # 候補ゼロと同様に終端アクション (在庫0/様子見) を出して操作可能にする
                # (依頼ボード#20 再対応: 旧実装は applied 行で _visible_cands 非空となり
                #  終端ボタンが出ず、再OOS商品がカード上で操作不能に詰まっていた)。
                _actionable_cands = [
                    _c for _c in _visible_cands
                    if (_c.get("status") or "pending") in ("pending", "accepted")
                ]
                if _visible_cands:
                    st.markdown(
                        f'<div style="font-size:11px;color:#8d927f;margin-bottom:4px;">'
                        f'仕入先候補 {len(_visible_cands)}件（score降順／上位最大3件）</div>',
                        unsafe_allow_html=True,
                    )

                    for _c in _visible_cands:
                        _cid = _c["id"]
                        _score = _c.get("match_score") or 0
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

                        # user フィードバック #1 (2026-07-03): 採用ボタンを 3 択化した
                        # ため、btn 列を 2.2 → 3.0 に広げて「SKUのみ/編集あり/不採用」
                        # の 3 連ボタンを収める (info 5.6 → 5.0 で相殺、link 1.2 は不変)。
                        _info_col, _link_col, _btn_col = st.columns([5.0, 1.2, 3.0])
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
                                f' <span style="color:#5f6557;">{html.escape(_plat)}</span>'
                                f' <span style="color:#2a2e2a;">{html.escape(_ttl)}</span>'
                                f' <span style="color:#5f6557;">{_price_str}</span>'
                                f'{_profit_str}'
                                f'{_status_badge}'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            # W258/Phase-B (2026-06-11): eBay × 仕入先 画像比較カード。
                            # money-direct path (checkbox/button) は不変、画像表示のみ追加。
                            _oos_ebay_img = item.get("ebay_image_url") or ""
                            _oos_cand_img = _c.get("candidate_image_url") or ""
                            if _oos_ebay_img or _oos_cand_img:
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
                                # W314 Phase 4: CSS はループ手前で 1 回だけ出力済み
                                # (_oos_card_css)。ここでは div のみ送出。
                                st.markdown(
                                    _oos_imgpair,
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
                            if _status == "pending":
                                # user フィードバック #1 (2026-07-03): 採用ボタンを
                                # 3 択化。SKUのみ (open_editor=False) / 編集あり
                                # (open_editor=True) / 不採用。旧「採用」1 択は撤去。
                                _lock_key = f"_inv_lock_{_cid}"
                                _locked = bool(st.session_state.get(_lock_key, False))
                                _b1, _b2, _b3 = st.columns(3)
                                with _b1:
                                    if st.button(
                                        "SKUのみ",
                                        key=f"{section_key}_btn_adopt_skuonly_{_cid}",
                                        width="stretch",
                                        disabled=_locked,
                                        help="採用 (SKU切替のみ、編集パネルは開かない)。"
                                             " 仕入先を差し替えるだけで済ませたい時。",
                                    ):
                                        st.session_state[_lock_key] = True
                                        try:
                                            _adopt_and_apply(
                                                _c, item, is_pending=True,
                                                open_editor=False,
                                            )
                                        finally:
                                            st.session_state[_lock_key] = False
                                        st.rerun(scope="app")
                                with _b2:
                                    if st.button(
                                        "編集あり",
                                        key=f"{section_key}_btn_adopt_editor_{_cid}",
                                        type="primary", width="stretch",
                                        disabled=_locked,
                                        help="採用 + 商品仕上げパネル展開 (タイトル/画像/"
                                             "ランク/数量 を続けて編集する時)。",
                                    ):
                                        st.session_state[_lock_key] = True
                                        try:
                                            _adopt_and_apply(
                                                _c, item, is_pending=True,
                                                open_editor=True,
                                            )
                                        finally:
                                            st.session_state[_lock_key] = False
                                        st.rerun(scope="app")
                                with _b3:
                                    st.button(
                                        "不採用",
                                        key=f"{section_key}_btn_reject_{_cid}",
                                        width="stretch",
                                        on_click=_on_reject_inv,
                                        args=(_cid, title),
                                        help="不採用（ユーザー判断として学習データに記録）",
                                    )
                            elif _status == "accepted":
                                # 採用済 (旧一括 UI で accept のみ成功し apply 未了の
                                # 残骸を含む)。単一「反映」ボタンで eBay 反映を再試行。
                                # 旧「反映する」checkbox + 「📷 写真反映」button は撤去
                                # (user 報告の「謎の設定ボタン」の正体。写真/description
                                # は採用成功後にタブ最上部の followup 欄で案内される)。
                                if st.button(
                                    "反映",
                                    key=f"{section_key}_btn_apply_{_cid}",
                                    type="primary", width="stretch",
                                    help="採用済の候補を eBay に反映 (ReviseItem で SKU 書換)",
                                ):
                                    _adopt_and_apply(_c, item, is_pending=False)
                                    st.rerun(scope="app")
                            elif _status == "applied":
                                st.caption("✓ 反映済み")
                # 終端アクション: 採用/反映できる候補が無い (候補ゼロ or applied-only
                # 再OOS) 商品は 在庫0/様子見 を出して操作可能にする (依頼ボード#20 再対応:
                # 旧実装は applied 行で _visible_cands 非空 → else に入らず終端ボタンが出ず、
                # 採用先が再OOSした商品がカード上で操作不能に詰まっていた)。
                if not _actionable_cands:
                    # 状況 caption は候補ゼロのときのみ (applied-only は上の再OOSバナーで説明済)。
                    if not _visible_cands:
                        if _had_rejected_now:
                            st.caption("表示中の候補はすべて不採用にしました。")
                        else:
                            # 候補0件 (置換用) → ただし alt-only 候補があれば「探索済」と区別表示.
                            # 2026-05-05 修正: Baccarat case (探索済 7 件すべて alt のみ) で
                            # 「候補未探索」と誤表示してた事象の修正. user 認識ミスを防ぐ.
                            # 2026-06-13 reviewer HIGH-1 fix: OOS/PNF 両経路とも eid キー統一
                            # (旧 sku fallback は dead code 化したため撤去)。
                            _alt_n = alt_only_count_by_eid.get(eid, 0)
                            if _alt_n > 0:
                                st.caption(
                                    f"仕入先候補は探索済みです（{_alt_n} 件見つかりましたが、"
                                    f"すべて『別商品の出品候補』として分類されており、この出品の"
                                    f"置き換えには使えません）。別商品として出品する検討は"
                                    f"『別SKU出品機会』タブで行ってください。"
                                )
                            elif item.get("auction_ended_grace"):
                                # 依頼ボード#20 (2026-06-14): 落札なし終了は再出品待ちで
                                # 探索を 24h 猶予中。「候補未探索」と誤表示すると user が
                                # 探索漏れと誤解するため grace 中である旨を明示する。
                                st.caption(
                                    "オークション終了（落札者なし）のため仕入先探索は"
                                    "再出品待ちで 24h 猶予中。上記の予定時刻以降に自動で"
                                    "再探索されます（在庫が復活すれば候補に表示）。"
                                )
                            else:
                                st.caption(
                                    "候補未探索（次回02:30 Pattern 2バッチで自動探索、"
                                    "または下部の探索ボタンで個別起動）"
                                )

                    # --- 依頼ボード#18: 候補がない場合は 在庫0化 はい/いいえ 2 ボタン ---
                    st.markdown(
                        '<div style="font-size:12px;color:#5f6557;margin:6px 0 2px;">'
                        '仕入先で購入できない状態です。eBay の在庫を 0 にして'
                        '販売停止しますか?</div>',
                        unsafe_allow_html=True,
                    )
                    _z1, _z2, _z3 = st.columns([1.8, 1.8, 2.8])
                    with _z1:
                        if st.button(
                            "はい、在庫を0にする",
                            key=f"{section_key}_qty0_{eid}",
                            type="primary", width="stretch",
                            help="eBay の在庫数を 0 に変更して販売停止します (listing は残ります)",
                        ):
                            if not all(_ebay_creds.values()):
                                _notice("error", "eBay API認証情報が未設定です（設定タブ参照）")
                            else:
                                from monitor.ebay_client import (
                                    revise_inventory_quantity as _rev_q0,
                                )
                                try:
                                    _qres0 = _rev_q0(eid, 0, **_ebay_creds)
                                except (RuntimeError, ConnectionError,
                                        TimeoutError, OSError) as _qe0:
                                    logger.exception(
                                        "qty0 exception (inventory tab) eid=%s", eid)
                                    _notice("error", f"{title}: 在庫0化中に例外 — {_qe0}")
                                else:
                                    if _qres0.get("success"):
                                        update_ebay_listing_quantity(eid, 0)
                                        bump_db_version()  # W134 Step2: 在庫変更後 read-cache 無効化
                                        _notice("success",
                                                f"{title}: eBay 在庫を 0 にしました（販売停止）")
                                    else:
                                        logger.error(
                                            "qty0 api_failed (inventory tab) eid=%s msg=%s",
                                            eid, _qres0.get("message"))
                                        _notice("error",
                                                f"{title}: 在庫0化に失敗 — "
                                                f"{_qres0.get('message') or 'error'}")
                            st.rerun(scope="app")
                    with _z2:
                        if st.button(
                            "いいえ、このまま様子見",
                            key=f"{section_key}_keep_{eid}",
                            width="stretch",
                            help="eBay 在庫はそのまま。確認済みとして要対応一覧から外します",
                        ):
                            set_ebay_listing_risk_confirmed(eid, 1)
                            bump_db_version()  # W134 Step2: risk 変更後 read-cache 無効化
                            _notice("info",
                                    f"{title}: 様子見にしました"
                                    f"（eBay 在庫はそのまま、一覧から外れます）")
                            st.rerun(scope="app")

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
            # [1] listing ごとの候補を1回だけ一括取得（N+1 SQL 回避）
            # 依頼ボード#18 reviewer HIGH-1 fix (2026-06-13): 旧 sku キー取得は
            # sku-rules 違反 (同一 sku 共有 listing で別 listing の候補がカードに
            # 混入 → 採用時の followup meta eid が誤 listing を指し写真/description
            # が別 listing に反映される経路)。PNF 側 HIGH-4 fix と対称の
            # ebay_item_id キーに張り替え (v56 で ebay_item_id NOT NULL 済)。
            from monitor.database import get_conn as _oos_conn
            _oos_eid_list = [_it["ebay_item_id"] for _it in oos_items if _it.get("ebay_item_id")]
            _cand_by_eid: dict[str, list[dict]] = {}
            # 2026-05-05 追加 (CRITICAL fix): alt-only candidate 数を OOS section でも別取得.
            # _render_oos_block の caption 分岐で参照される. 過去の bug fix で別関数の
            # ローカル変数を参照してて NameError クラッシュしていた事故予防.
            _oos_alt_only_count: dict[str, int] = {}
            if _oos_eid_list:
                with _oos_conn() as _cc:
                    _ph = ",".join("?" * len(_oos_eid_list))
                    for _r in _cc.execute(
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
                        _oos_eid_list,
                    ).fetchall():
                        _cand_by_eid.setdefault(_r["ebay_item_id"], []).append(dict(_r))
                    # alt-only candidate 数も別途取得 (Baccarat case 対策、eid キー)
                    for _r in _cc.execute(
                        f"""SELECT ebay_item_id, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE ebay_item_id IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY ebay_item_id""",
                        _oos_eid_list,
                    ).fetchall():
                        _oos_alt_only_count[_r["ebay_item_id"]] = _r["alt_n"]

            st.caption(
                "各商品のすぐ下に仕入先候補(上位3件)を表示。「採用」を押すとその場で eBay へ "
                "SKU 反映まで実行されます。候補がない商品は「在庫を0にする」で販売停止できます。"
            )

            # W314 Phase 4 (2026-07-03 性能設計書§7): _CARD_CSS (145行/仕入先候補
            # カードと共有) を OOS 一覧の全 render で 1 回だけ出す。旧実装は
            # _render_oos_block (@st.fragment、商品毎に個別インスタンス) の内部で
            # 画像比較ブロックがある商品ごとに毎回 _CARD_CSS を同梱していた
            # (最大 oos_items 件ぶん重複)。ここは fragment の外 (通常の top-level
            # render 経路) で無条件に実行するため、要素ツリーから消えるリスクはない
            # (tab_supplier_candidates.py と同一パターン)。
            if oos_items:
                from tabs._supplier_card_html import _CARD_CSS as _oos_card_css
                st.markdown(_oos_card_css, unsafe_allow_html=True)

            # [3] st.fragment 方式 (依頼ボード#18 / 2026-06-13 全面簡素化):
            # 各商品ブロックは @st.fragment 化。候補ありは「採用」「不採用」の
            # 1 クリック即実行 (仕入先候補タブと同パターン)、候補なしは
            # 在庫0化の はい/いいえ 2 ボタン。一括実行・確認チェック・
            # SKU/qty 直接編集 UI は撤去 (旧 UI は git 履歴参照)。
            for _item in oos_items:
                _eid_val = _item.get("ebay_item_id") or ""
                _cands_for_eid = _cand_by_eid.get(_eid_val, [])[:3]
                _render_oos_block(_item, _cands_for_eid, "oos", _oos_alt_only_count)

            # --- form 外: 候補未探索 listing の即時探索（form内では button 使えないため分離） ---
            _missing_skus = [
                _it for _it in oos_items
                if not _cand_by_eid.get(_it.get("ebay_item_id") or "", [])
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
                        import threading as _th_bulk
                        from streamlit.runtime.scriptrunner import (
                            add_script_run_ctx as _add_ctx_bulk,
                            get_script_run_ctx as _get_ctx_bulk,
                        )
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

                        def _bg_bulk_search(targets=_targets,
                                            flag_key=_bulk_search_flag,
                                            result_key=_bulk_search_result):
                            ok_count = 0
                            ng_count = 0
                            persisted_sum = 0
                            persisted_alt_sum = 0
                            errors = []
                            try:
                                for eid, sku in targets:
                                    try:
                                        # board#19 (2026-06-13): 直接呼出は Streamlit
                                        # プロセス内で Playwright 起動不可 → 偽 found=0。
                                        # subprocess 経由で実探索する (helper docstring 参照)
                                        r = _run_candidate_search_in_subprocess(
                                            ebay_item_id=eid, sku=sku,
                                            discovered_via="ui_bulk_search",
                                        )
                                        if r.get("success"):
                                            ok_count += 1
                                            persisted_sum += int(r.get("persisted") or 0)
                                            persisted_alt_sum += int(r.get("persisted_alt") or 0)
                                        else:
                                            ng_count += 1
                                            errors.append(f"{sku}: {(r.get('message') or 'fail')[:40]}")
                                    except Exception as e:
                                        ng_count += 1
                                        errors.append(f"{sku}: 例外 {str(e)[:40]}")
                            finally:
                                st.session_state[result_key] = {
                                    "ok": ok_count, "ng": ng_count,
                                    "total": len(targets),
                                    "persisted": persisted_sum,
                                    "persisted_alt": persisted_alt_sum,
                                    "errors": errors[:10],  # 最初の10件のみ保持
                                }
                                st.session_state[flag_key] = False

                        if not _targets:
                            st.warning("探索対象がありません (SKU 未設定の商品のみ)。")
                        else:
                            # board#19 修正 (2026-06-13): raw thread の st.session_state 書込は
                            # ScriptRunContext 不在で global mock に fallback し実 session に
                            # 届かない (= flag が永遠に下りず「実行中…」stuck) → ctx を移植。
                            # HIGH-1: flag/result の準備は start() より前 (爆速完了 thread の
                            # finally と race して flag を上書きしないため)。
                            st.session_state[_bulk_search_flag] = True
                            st.session_state.pop(_bulk_search_result, None)  # 前回結果をクリア
                            _bulk_result = None  # ローカルも同期 (同 run 下部での stale 表示防止)
                            _t_bulk = _th_bulk.Thread(target=_bg_bulk_search, daemon=True)
                            _add_ctx_bulk(_t_bulk, _get_ctx_bulk())
                            _t_bulk.start()
                            st.success(
                                f"{len(_targets)}件 の一括探索を開始しました。"
                                f"数分後にページを更新すると結果が表示されます。"
                            )

                # 一括探索の結果表示
                if _bulk_result is not None and not _bulk_in_progress:
                    # board#19 MEDIUM-1 (2026-06-13): 「成功」= 探索完了であって候補保存ではない
                    # → 新規保存件数を必ず併記 (0件でも「候補が出ない」と誤解させない)
                    _bp = int(_bulk_result.get("persisted") or 0)
                    _bp_alt = int(_bulk_result.get("persisted_alt") or 0)
                    _bp_main = max(0, _bp - _bp_alt)
                    _saved_txt = f"新規候補 {_bp_main}件"
                    if _bp_alt:
                        _saved_txt += f" + 別SKU出品機会 {_bp_alt}件"
                    if _bulk_result["ng"] == 0:
                        if _bp > 0:
                            st.success(
                                f"一括探索完了: {_bulk_result['ok']}/{_bulk_result['total']} 件"
                                f"探索 — {_saved_txt} 保存"
                            )
                        else:
                            st.info(
                                f"一括探索完了: {_bulk_result['ok']}/{_bulk_result['total']} 件"
                                f"探索 — 新規候補なし (基準未満・既存と同一・利益不足のみ)"
                            )
                    else:
                        st.warning(
                            f"一括探索完了: 探索 {_bulk_result['ok']}件 ({_saved_txt}) / "
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
                            _name_m = f"{_title_m} ({_sku_m2})" if _title_m else _sku_m2
                            if st.session_state.get(_flag_k, False):
                                st.caption(f"{_name_m}: 探索実行中…（1〜2分・終わったらページ更新）")
                            elif _last_result is not None and _last_result.get("ok"):
                                # board#19 (2026-06-13): 成功でも persisted=0 だと無反応に見えた
                                # → 結果内訳を必ず表示 + 再探索ボタン併設
                                # HIGH-2: alt_listing_possible=1 行はこのカード一覧に出ない
                                # (alt=0 filter) ため、main/alt を区別して虚偽約束を防ぐ
                                _p_cnt = int(_last_result.get("persisted") or 0)
                                _alt_cnt = int(_last_result.get("alt") or 0)
                                _main_cnt = max(0, _p_cnt - _alt_cnt)
                                if _main_cnt > 0:
                                    _alt_sfx = f"／別SKU出品機会 {_alt_cnt}件" if _alt_cnt else ""
                                    st.caption(
                                        f"{_name_m}: 探索完了 — 新規候補 {_main_cnt} 件保存"
                                        f"（ページ更新で上に表示されます）{_alt_sfx}"
                                    )
                                elif _alt_cnt > 0:
                                    st.caption(
                                        f"{_name_m}: 探索完了 — 別SKU出品機会として {_alt_cnt} 件保存"
                                        f"（このカードには出ません。仕入先候補タブで確認）"
                                    )
                                else:
                                    _parts = []
                                    if _last_result.get("low"):
                                        _parts.append(f"類似度基準未満 {_last_result['low']}件")
                                    if _last_result.get("existing"):
                                        _parts.append(
                                            f"既存/不採用済みと同一 {_last_result['existing']}件"
                                        )
                                    if _last_result.get("unprofitable"):
                                        _parts.append(f"利益不足 {_last_result['unprofitable']}件")
                                    _found_cnt = int(_last_result.get("found") or 0)
                                    _detail = "・".join(_parts)
                                    if _found_cnt == 0:
                                        _summary = "市場で類似商品が見つかりませんでした"
                                    elif _detail:
                                        _summary = f"{_found_cnt}件 見つかりましたが {_detail}"
                                    else:
                                        _summary = f"{_found_cnt}件 見つかりましたが保存基準外"
                                    st.caption(
                                        f"{_name_m}: 探索完了 — 新規候補なし（{_summary}）"
                                    )
                                if st.button(
                                    f"再探索 {_sku_m2}",
                                    key=f"oos_search_again_{_eid_m}",
                                    help=f"{_title_m}: 結果表示をクリアして「探索」ボタンを再表示します",
                                    width="stretch",
                                ):
                                    st.session_state.pop(_result_k_display, None)
                                    st.rerun()
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
                                import threading as _th
                                from streamlit.runtime.scriptrunner import (
                                    add_script_run_ctx as _add_ctx,
                                    get_script_run_ctx as _get_ctx,
                                )
                                _sku_tgt = _sku_m2
                                _eid_tgt = _eid_m
                                # 2026-04-20 修正 (HIGH-1): silent failure 対策
                                # 旧: except Exception: pass + flag_k 残置 → 失敗 SKU が再試行不能
                                # 新: 例外も結果も session_state に保存、flag_k は finally で必ず下ろす
                                # board#19 (2026-06-13): 上記は ctx 無し thread のため mock 行きで
                                # 機能していなかった → add_script_run_ctx で実 session に結線
                                _result_k = f"_oos_search_result_{_eid_m}"
                                def _bg_cs(eid=_eid_tgt, sku=_sku_tgt,
                                           result_key=_result_k, flag_key=_flag_k):
                                    try:
                                        # board#19 (2026-06-13): 直接呼出は Streamlit
                                        # プロセス内で Playwright 起動不可 → 偽 found=0。
                                        # subprocess 経由で実探索する (helper docstring 参照)
                                        r = _run_candidate_search_in_subprocess(
                                            ebay_item_id=eid, sku=sku,
                                            discovered_via="ui_on_demand",
                                        )
                                        st.session_state[result_key] = {
                                            "ok": bool(r.get("success")),
                                            "msg": r.get("message") or "",
                                            "found": int(r.get("found") or 0),
                                            "persisted": int(r.get("persisted") or 0),
                                            "alt": int(r.get("persisted_alt") or 0),
                                            "low": int(r.get("skipped_low_score") or 0),
                                            "existing": int(r.get("skipped_existing") or 0),
                                            "unprofitable": int(r.get("skipped_unprofitable") or 0),
                                        }
                                    except Exception as e:
                                        st.session_state[result_key] = {
                                            "ok": False, "msg": f"例外: {e}",
                                        }
                                    finally:
                                        st.session_state[flag_key] = False
                                # HIGH-1 (2026-06-13): flag は start() より前に立てる
                                # (爆速完了 thread の finally: flag=False を後から True で
                                # 上書きすると恒久 stuck になる race 防止)
                                st.session_state[_flag_k] = True
                                _t_cs = _th.Thread(target=_bg_cs, daemon=True)
                                _add_ctx(_t_cs, _get_ctx())
                                _t_cs.start()
                                st.success(f"{_sku_m2}: 探索開始。数分後にページ更新してください。")

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
                "各商品のすぐ下に仕入先候補(上位3件)を表示。「採用」を押すとその場で eBay へ "
                "SKU 反映まで実行されます。候補がない商品は「在庫を0にする」で販売停止できます。"
            )

            for _item in pnf_items:
                _pnf_eid_val = _item.get("ebay_item_id") or ""
                _cands_for_eid = _pnf_cand_by_eid.get(_pnf_eid_val, [])[:3]
                _render_oos_block(_item, _cands_for_eid, "pnf", _pnf_alt_only_count)
        else:
            st.success("確認不可の商品はありません。")

        # --- 状態不明 (依頼ボード#17 C / 2026-06-12) ---
        # 旧実装は『不明』『エラー』を 2 バケツ (在庫切れ/確認不可) のどちらにも
        # 入れず silent drop → 9 件が要対応 UI からも探索からも見えず滞留していた。
        # 俯瞰テーブルで可視化のみ (在庫判定が未確定のため一括操作 UI は付けない)。
        unk_items = risk_data.get("status_unknown", [])
        if unk_items:
            st.markdown("---")
            st.markdown(f"### 状態不明 ({len(unk_items)}件)")
            st.caption(
                "仕入先ページの在庫状態を自動判定できなかった商品です。"
                "仕入先URLを開いて実状態を確認し、右の欄に結果を記載して「保存」してください。"
                "保存すると要対応一覧へ正しく振り分けられます "
                "(ページ形式が変わった場合はサイト設定の見直しが必要です)。"
            )
            # 依頼ボード#21 (2026-06-14): 状態不明を read-only 表 → 手動判定入力欄に変更。
            # user が仕入先を実見した結果 (在庫有/在庫無/ページなし) を記載 → 保存で
            # source_status に反映 (update_ebay_listing_status が在庫有時の risk_confirmed
            # リセットも処理)。在庫有→要対応から解消 / 在庫無→在庫切れ / ページなし→確認不可。
            from monitor.database import update_ebay_listing_status as _upd_unk_status
            _UNK_PLACEHOLDER = "（選択してください）"
            _UNK_CHOICES = [_UNK_PLACEHOLDER, "在庫有", "在庫無", "ページなし"]
            for _u in unk_items:
                _u_eid = _u.get("ebay_item_id") or ""
                _u_title = (_u.get("title") or "")[:70]
                _u_url = _u.get("source_url") or ""
                _u_last = str(_u.get("source_last_checked") or "")[:16]
                with st.container(border=True):
                    _uc1, _uc2, _uc3 = st.columns([4.2, 2.0, 2.0])
                    with _uc1:
                        st.markdown(
                            f'<div style="font-size:12px;color:#2a2e2a;">'
                            f'{html.escape(_u_title)}</div>'
                            f'<div style="font-size:11px;color:#8d927f;">'
                            f'現状態: {html.escape(_u.get("source_status") or "不明")}'
                            f' ・ eBay在庫 {_u.get("quantity_ebay")}'
                            f' ・ 最終チェック {html.escape(_u_last)}</div>',
                            unsafe_allow_html=True,
                        )
                        if _u_url:
                            st.markdown(
                                f'<a href="{html.escape(_u_url, quote=True)}" target="_blank" '
                                f'style="color:#156a63;font-size:12px;">仕入先URLを開いて確認</a>',
                                unsafe_allow_html=True,
                            )
                    with _uc2:
                        _u_choice = st.selectbox(
                            "実状態を記載",
                            _UNK_CHOICES,
                            key=f"unk_status_{_u_eid}",
                            label_visibility="collapsed",
                        )
                    with _uc3:
                        if st.button(
                            "保存", key=f"unk_save_{_u_eid}",
                            type="primary", width="stretch",
                            help="記載した実状態を保存して要対応一覧へ振り分けます",
                        ):
                            if _u_choice == _UNK_PLACEHOLDER:
                                _notice("error",
                                        f"{_u_title}: 実状態を選択してください")
                            else:
                                try:
                                    _upd_unk_status(_u_eid, _u_choice)
                                    bump_db_version()  # 状態変更後 read-cache 無効化
                                    _notice("success",
                                            f"{_u_title}: 状態を「{_u_choice}」として保存しました")
                                except Exception as _ue:
                                    logger.exception(
                                        "状態不明 手動保存失敗 eid=%s", _u_eid)
                                    _notice("error",
                                            f"{_u_title}: 保存に失敗しました — {_ue}")
                            st.rerun(scope="app")

    # ---------- 監視リスト (手動監視) ----------
    with monitor_tab1:
        st.caption(
            "monitored_items に自分で登録した URL の在庫監視です。"
            "「⚠ 供給リスク」タブの自動検知とは別の仕組みです。"
        )
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
