#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先候補 (探索結果の採用/不採用/反映) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "仕入先候補":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
同梱ヘルパー (app.py top-level から移動、単一タブ専用): _STATUS_JA
"""
from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
import streamlit as st

logger = logging.getLogger(__name__)


_STATUS_JA = {
    "pending": "未判定",
    "accepted": "採用済",
    "rejected": "不採用",
    "applied": "反映済",
}

# タブ密度化リファクタ A2 (2026-07-04): フィルタ行・閾値 expander・カード操作
# ボタンを 12px baseline に圧縮 (user 承認済み密度スペック: フォント12px /
# 行高22-28px)。Streamlit は `key=` 指定 widget の要素コンテナに
# `st-key-<key>` class を付与する仕様 (公式 docstring 明記、button.py/
# layouts.py 等で確認済) を利用し、このタブの widget key prefix だけを
# `[class*="st-key-<prefix>"]` で狙い撃ちする。汎用 data-testid セレクタ
# (`[data-testid="stButton"] button` 等) は使わない — このタブの render 中は
# 常時表示される app.py 側のページ切替ナビゲーションボタンにも波及して
# しまうため (K2 surgical、他 UI への影響ゼロを機械的に担保)。
_SUP_DENSITY_CSS = """
<style>
div[class*="st-key-sup_filter_status"] label,
div[class*="st-key-sup_filter_query"] label,
div[class*="st-key-sup_sort_order"] label {
  font-size:12px !important;
  margin-bottom:2px !important;
}
div[class*="st-key-sup_filter_status"] [data-baseweb="select"] > div,
div[class*="st-key-sup_sort_order"] [data-baseweb="select"] > div,
div[class*="st-key-sup_filter_query"] input {
  font-size:12px !important;
  min-height:30px !important;
}
div[class*="st-key-sup_threshold_expander"] summary {
  padding:4px 10px !important;
  min-height:26px !important;
}
div[class*="st-key-sup_threshold_expander"] summary p,
div[class*="st-key-sup_threshold_expander"] summary span {
  font-size:12px !important;
  line-height:22px !important;
  margin:0 !important;
}
div[class*="st-key-sup_threshold_expander"] [data-testid="stCaptionContainer"] p {
  font-size:11px !important;
  line-height:20px !important;
  margin:2px 0 !important;
}
div[class*="st-key-sup_new_listing_"] button,
div[class*="st-key-sup_accept_skuonly_"] button,
div[class*="st-key-sup_accept_editor_"] button,
div[class*="st-key-sup_reject_"] button,
div[class*="st-key-sup_accept_alt_confirm_"] button,
div[class*="st-key-sup_accept_alt_cancel_"] button,
div[class*="st-key-sup_more_"] button,
div[class*="st-key-sup_reload"] button,
div[class*="st-key-th_save"] button,
div[class*="st-key-th_recalc"] button {
  font-size:12px !important;
  padding:2px 10px !important;
  min-height:26px !important;
  line-height:22px !important;
}
</style>
"""


def render_supplier_candidates_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    import json
    from monitor.database import get_conn, get_supplier_candidates, update_supplier_candidate_status
    from tabs._adopt_candidate import adopt_candidate
    from typing import Optional

    # W314 Phase 4 (2026-07-03 性能設計書§7): 候補カード CSS (_CARD_CSS, 145行) を
    # このタブの全 render で 1 回だけ出す。旧実装はカード毎 (render_supplier_card_html
    # の既定 include_css=True) に同一 CSS 文字列を毎回同梱しており、候補件数ぶん
    # (最大 20+/タブ) 重複送信されていた。ここは st.fragment の外 (通常の top-level
    # render 経路) で毎回無条件に実行するため、Streamlit の要素ツリーから消える
    # リスクはない (session_state センチネルでの「1 セッション 1 回だけ」ゲートは
    # 使わない = fragment 部分 rerun でカード側が個別に再描画されても影響を受けない)。
    from tabs._supplier_card_html import _CARD_CSS
    st.markdown(_CARD_CSS, unsafe_allow_html=True)
    # タブ密度化リファクタ A2: フィルタ行/閾値 expander/操作ボタンの圧縮 CSS。
    st.markdown(_SUP_DENSITY_CSS, unsafe_allow_html=True)

    st.title("仕入先候補レビュー")
    st.caption(
        "Claude API が評価した仕入先候補を一覧。"
        "置き換え可能候補と別SKU出品機会を分けて表示します。"
    )

    # ── 最終実行日時 ──
    try:
        from datetime import datetime as _dt, timedelta as _td
        with get_conn() as _sup_conn:
            _sup_conn.row_factory = None
            _last_sup = _sup_conn.execute(
                "SELECT MAX(created_at) FROM supplier_candidates"
            ).fetchone()[0]
        if _last_sup:
            # "2026-04-23 02:44:24" 形式を parse
            try:
                _dt_obj = _dt.strptime(_last_sup, "%Y-%m-%d %H:%M:%S")
                _delta = _dt.now() - _dt_obj
                if _delta.total_seconds() < 3600:
                    _ago = f"{int(_delta.total_seconds()/60)} 分前"
                elif _delta.total_seconds() < 86400:
                    _ago = f"{int(_delta.total_seconds()/3600)} 時間前"
                else:
                    _ago = f"{int(_delta.total_seconds()/86400)} 日前"
                st.caption(f"**最終実行**: {_last_sup} ({_ago})")
            except Exception:
                st.caption(f"**最終実行**: {_last_sup}")
        else:
            st.caption("**最終実行**: データなし")
    except Exception as _e:
        logger.warning("仕入先候補 最終実行表示 失敗: %s", _e)

    # ── 閾値調整 (T6: Q5=C 手動ボタン、Q6=B タブ上部配置) ──
    import json as _th_json
    # W243: タブ分割後の parent.parent 修正 (旧 path は tabs/settings.json を指し、
    # 保存しても夜間 sweep が読む root settings.json に反映されない罠だった)
    _th_settings_path = Path(__file__).resolve().parent.parent / "settings.json"
    try:
        with open(_th_settings_path, encoding="utf-8") as _f:
            _th_settings = _th_json.load(_f)
    except Exception:
        _th_settings = {}
    _th_alt0 = int(_th_settings.get("supplier_alt0_score_threshold", 60))
    _th_alt1 = int(_th_settings.get("supplier_alt1_score_threshold", 20))

    # W212-supplier-card-cleanup (2026-06-04): 普段触らない探索閾値を expander
    # で折りたたみ降格 (表示・配置のみ、内部 slider / 保存 / 再計算 ロジックは
    # 不変. money-direct な DELETE FROM supplier_candidates は中で従来通り発火).
    with st.expander(
        "探索スコア閾値の調整 (THRESHOLD CONTROL)",
        expanded=False,
        key="sup_threshold_expander",
    ):
        st.caption(
            "新規探索時のスコア下限。緩和 (低い) → 候補多く拾う / "
            "厳格 (高い) → 精度重視。変更後「再計算実行」で既存候補にも適用。"
        )
        _th_c1, _th_c2, _th_c3 = st.columns([1.2, 1.2, 1.6])
        with _th_c1:
            _new_alt0 = st.select_slider(
                "置換候補 (alt=0) の下限",
                options=[20, 30, 40, 50, 60, 70, 80, 90],
                value=_th_alt0,
                key="th_alt0_slider",
                help="同一商品判定の確からしさ (Claude 評価)。60 が標準。",
            )
        with _th_c2:
            _new_alt1 = st.select_slider(
                "別SKU出品機会 (alt=1) の下限",
                options=[0, 10, 20, 30, 40, 50, 60],
                value=_th_alt1,
                key="th_alt1_slider",
                help="別SKU機会のスコア下限。20 で score<20 のゴミを除外。",
            )
        with _th_c3:
            st.write("")
            _th_b1, _th_b2 = st.columns(2)
            with _th_b1:
                if st.button("設定保存", key="th_save", width="stretch"):
                    _th_settings["supplier_alt0_score_threshold"] = int(_new_alt0)
                    _th_settings["supplier_alt1_score_threshold"] = int(_new_alt1)
                    try:
                        with open(_th_settings_path, "w", encoding="utf-8") as _f:
                            _th_json.dump(_th_settings, _f, indent=2, ensure_ascii=False)
                        st.success(f"保存: alt0≥{_new_alt0} / alt1≥{_new_alt1}")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"保存失敗: {_e}")
            with _th_b2:
                if st.button("再計算実行", key="th_recalc", type="primary", width="stretch",
                             help="既存 DB を新閾値で再判定。下限以下の候補を物理削除、"
                                  "残る候補も profit_jpy を最新計算で更新 (rejected/applied は保護)"):
                    try:
                        from scripts.recalc_supplier_candidate_profits import recalc_all
                        # 現在保存中の閾値を使って再計算 (ボタン押下前に保存推奨)
                        with st.status("既存候補を再計算中...", expanded=False) as _st_rc:
                            _res_rc = recalc_all(dry_run=False)
                            _st_rc.update(
                                label=f"完了: 削除 {_res_rc.get('price_none_deleted', 0) + _res_rc.get('now_unprofitable_deleted', 0)}件 / "
                                      f"利益更新 {_res_rc.get('still_profitable', 0)}件",
                                state="complete",
                            )
                        # 低 score の候補も削除 (alt0 閾値 + alt1 閾値に基づく)
                        with get_conn() as _c_rc:
                            _low_score_deleted = _c_rc.execute(
                                """DELETE FROM supplier_candidates
                                   WHERE status NOT IN ('rejected','applied')
                                     AND (
                                       (COALESCE(alt_listing_possible,0) = 0
                                        AND COALESCE(match_score, 0) < ?)
                                       OR
                                       (COALESCE(alt_listing_possible,0) = 1
                                        AND COALESCE(match_score, 0) < ?)
                                     )""",
                                (int(_new_alt0), int(_new_alt1)),
                            ).rowcount
                            _c_rc.commit()
                        st.success(
                            f"再計算完了。低スコア削除 {_low_score_deleted}件 + "
                            f"利益ベース削除 {_res_rc.get('price_none_deleted', 0) + _res_rc.get('now_unprofitable_deleted', 0)}件"
                        )
                    except Exception as _e:
                        st.error(f"再計算エラー: {_e}")

    # ── フィルタ ──
    # 依頼ボード#15 (2026-06-12): SKU 完全一致検索 → 商品名/SKU 部分一致検索に変更
    # + 並び順 selectbox 追加 (利益順=従来 DB order / 新着順 / 一致度順)。
    # 旧 UI は「謎の SKU 完全一致のみ・ソート不可」で履歴から商品を探せなかった。
    _sup_f1, _sup_f2, _sup_f3, _sup_f4 = st.columns([2, 3, 2, 1], gap="small")
    with _sup_f1:
        _sup_filter_status = st.selectbox(
            "ステータス",
            options=["pending", "accepted", "rejected", "applied", "すべて"],
            format_func=lambda x: _STATUS_JA.get(x, x),
            index=0,
            key="sup_filter_status",
        )
    with _sup_f2:
        _sup_query = st.text_input(
            "商品名 / SKU で検索（部分一致・スペース区切りで AND・空欄で全件）",
            value="",
            key="sup_filter_query",
        )
    with _sup_f3:
        _SUP_SORT_JA = {
            "profit": "利益順 (高→低)",
            "newest": "新着順 (新→旧)",
            "score": "一致度順 (高→低)",
        }
        _sup_sort = st.selectbox(
            "並び順",
            options=["profit", "newest", "score"],
            format_func=lambda x: _SUP_SORT_JA.get(x, x),
            index=0,
            key="sup_sort_order",
        )
    with _sup_f4:
        st.write("")  # spacer
        if st.button("再読込", key="sup_reload"):
            st.rerun()

    _sup_all = get_supplier_candidates(
        status=None if _sup_filter_status == "すべて" else _sup_filter_status,
    )

    # W115 v2 root fix (2026-05-10): 履歴 tab は status filter から独立 fetch.
    # 旧挙動: status filter default='pending' で _sup_all=pending only → _sup_history=[] (常に 0 件、UX 不能)
    # 新挙動: 履歴は検索 filter のみ尊重して rejected+applied を独立 fetch.
    # status filter は actionable 3 tab (revive/replace/altlist) のみに適用.
    _sup_history_raw = (
        get_supplier_candidates(status="rejected")
        + get_supplier_candidates(status="applied")
    )

    # 商品名 / SKU 部分一致検索 (依頼ボード#15)。candidate_title (仕入先側商品名) +
    # 親 listing の eBay title + sku を対象に、スペース区切り全トークン AND・大小文字無視。
    # NFKC 正規化で全角英数 (「ＳＯＮＹ」等、Yahoo/メルカリ由来タイトルに頻出) も
    # 半角入力で hit させる (code-reviewer MED-2 2026-06-12)。
    _sup_query_norm = unicodedata.normalize("NFKC", _sup_query.strip()).lower()
    if _sup_query_norm:
        _sup_tokens = _sup_query_norm.split()

        # eBay 側タイトルも検索対象 (code-reviewer MED-1: user は英語 eBay タイトルで
        # 探すことがあり、仕入先タイトルは日本語主体のため取りこぼす)。
        # ebay_item_id → title の一括 map (IN 句 1 クエリ、N+1 回避)。
        _q_eids = list({
            r.get("ebay_item_id")
            for r in (_sup_all + _sup_history_raw)
            if r.get("ebay_item_id")
        })
        _sup_title_map: dict[str, str] = {}
        if _q_eids:
            from monitor.database import get_conn as _q_conn
            with _q_conn() as _q_cc:
                _q_ph = ",".join("?" * len(_q_eids))
                for _trow in _q_cc.execute(
                    f"SELECT ebay_item_id, title FROM ebay_listings "
                    f"WHERE ebay_item_id IN ({_q_ph})",
                    _q_eids,
                ).fetchall():
                    _sup_title_map[_trow["ebay_item_id"]] = _trow["title"] or ""

        def _sup_match_query(row: dict) -> bool:
            hay = unicodedata.normalize(
                "NFKC",
                f"{row.get('candidate_title') or ''} {row.get('sku') or ''} "
                f"{_sup_title_map.get(row.get('ebay_item_id') or '', '')}",
            ).lower()
            return all(tok in hay for tok in _sup_tokens)

        _sup_all = [r for r in _sup_all if _sup_match_query(r)]
        _sup_history_raw = [r for r in _sup_history_raw if _sup_match_query(r)]

    # 並び順 (依頼ボード#15)。"profit" は DB 既定 order (profit DESC → score DESC →
    # created_at DESC、NULL 末尾) をそのまま使う。以降の 4 区分 partition は
    # 順序保存 loop なので、ここで並べ替えれば全サブタブに反映される。
    if _sup_sort == "newest":
        _sup_all.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        _sup_history_raw.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    elif _sup_sort == "score":
        # match_score NULL は末尾 (-1 扱い)
        _sup_all.sort(
            key=lambda r: r.get("match_score") if r.get("match_score") is not None else -1,
            reverse=True,
        )
        _sup_history_raw.sort(
            key=lambda r: r.get("match_score") if r.get("match_score") is not None else -1,
            reverse=True,
        )

    # 親 listing の source_status + quantity_ebay + is_ended をまとめて取得 (N+1 回避)
    # 2026-04-24: qty_ebay も取得して「復活候補」(qty=0) と「置換候補」(qty≥1) を分離
    # W68 Iteration 2: SKU 集約 → ebay_item_id 集約に変更 (sku-rules.md 準拠).
    # 同 SKU 多 listing (有在庫プール 8 SKU で 107 listings) で dict[sku] が衝突する事故防止.
    # W115 v2 (2026-05-10): _sup_history_raw の eids も含めて parent metadata を完全化
    # (履歴行の「仕入先復活警告」表示を担保).
    # W314 Phase 4 (2026-07-03): current_price / ebay_image_url も同じ一括 SELECT に
    # 相乗り (性能設計書§7 N+1 解消)。旧実装はカード毎 (_render_candidate_card ループ内)
    # に get_ebay_listing_by_item_id(eid) = SELECT * FROM ebay_listings を個別発行して
    # おり、候補件数ぶん (最大 20+) の DB 接続開閉+全列 SELECT が発生していた。
    _sup_parent_status: dict[str, str] = {}
    _sup_parent_qty: dict[str, int] = {}
    _sup_parent_ended: dict[str, int] = {}
    _sup_parent_listing: dict[str, dict] = {}
    _sup_eids = list({
        r.get("ebay_item_id")
        for r in (_sup_all + _sup_history_raw)
        if r.get("ebay_item_id")
    })
    if _sup_eids:
        from monitor.database import get_conn as _sup_conn
        with _sup_conn() as _sup_cc:
            _ph = ",".join("?" * len(_sup_eids))
            for _srow in _sup_cc.execute(
                f"""SELECT ebay_item_id, source_status, is_ended, quantity_ebay,
                           current_price, ebay_image_url, title
                    FROM ebay_listings WHERE ebay_item_id IN ({_ph})""",
                _sup_eids,
            ).fetchall():
                _ended = int(_srow["is_ended"] or 0)
                _sup_parent_status[_srow["ebay_item_id"]] = (
                    (_srow["source_status"] or "") if not _ended else "ended"
                )
                _sup_parent_qty[_srow["ebay_item_id"]] = int(_srow["quantity_ebay"] or 0)
                _sup_parent_ended[_srow["ebay_item_id"]] = _ended
                _sup_parent_listing[_srow["ebay_item_id"]] = {
                    "current_price": _srow["current_price"],
                    "ebay_image_url": _srow["ebay_image_url"],
                    # 依頼ボード #50 (2026-07-04): カード左ペイン (eBay 側) に
                    # 現行タイトルを表示するため追加 (表示のみ、money-direct 無関係).
                    "title": _srow["title"],
                }

    # 4 区分に分離 (2026-04-24 rev2):
    #   revive: eBay qty=0 + alt=0 + status IN (pending,accepted) → 復活候補 (actionable 最優先)
    #   replace: eBay qty≥1 + alt=0 + status IN (pending,accepted) → 置換候補 (actionable)
    #   altlist: alt=1 + status IN (pending,accepted) → 別SKU出品機会 (actionable)
    #   history: status IN (rejected,applied) → 履歴 (過去の判断、参照用)
    #
    # actionable = pending or accepted (まだ action が残っている)
    # history    = rejected (user 不採用済) or applied (既に反映済)
    #   ↳ これらを actionable 3 tab に混ぜると「URL 失効した古い候補」で誤解を招く
    _sup_revive: list[dict] = []
    _sup_replace: list[dict] = []
    _sup_altlist: list[dict] = []
    _sup_history: list[dict] = []
    # W115 v2 (2026-05-10): _sup_all の rejected/applied は skip (履歴は独立 fetch).
    # status filter='rejected'/'applied' 時に _sup_all と _sup_history_raw が重複しないよう
    # actionable loop は明示 status guard で history を除外.
    # W182 (2026-05-28): 在庫 gate 結果が 'unavailable' / 'not_found' の候補は
    # actionable 3 tab から除外 (sold_out 商品の誤提案を防ぐ恒久対策).
    # 旧 candidate (v54 migration 前) は availability_status=NULL なので影響なし.
    _w182_excluded = 0
    for _r in _sup_all:
        _qty_r = _sup_parent_qty.get(_r.get("ebay_item_id") or "", -1)
        _is_alt_r = bool(_r.get("alt_listing_possible"))
        _st_r = (_r.get("status") or "pending").lower()
        if _st_r in ("rejected", "applied"):
            continue  # 履歴は _sup_history_raw から独立 populate
        _avail_st_r = (_r.get("availability_status") or "").lower()
        if _avail_st_r in ("unavailable", "not_found"):
            _w182_excluded += 1
            continue
        if _is_alt_r:
            _sup_altlist.append(_r)
        elif _qty_r == 0:
            _sup_revive.append(_r)
        else:
            _sup_replace.append(_r)

    # 履歴 populate (status filter 非依存、auto_rejected=1 はノイズ抑止 / FINDING 8 2026-05-05)
    for _r in _sup_history_raw:
        _st_r = (_r.get("status") or "").lower()
        if _st_r == "rejected" and int(_r.get("auto_rejected") or 0) == 1:
            continue
        _sup_history.append(_r)

    _actionable_total = len(_sup_revive) + len(_sup_replace) + len(_sup_altlist)
    st.markdown(
        f"**actionable**: {_actionable_total}件 "
        f"(復活 {len(_sup_revive)} / 置換 {len(_sup_replace)} / 別SKU {len(_sup_altlist)})　"
        f"**履歴 (rejected/applied)**: {len(_sup_history)}件 "
        f"<span style='color:#8d927f;font-size:11px;'>"
        f"※ 履歴は status filter 非依存</span>",
        unsafe_allow_html=True,
    )

    @st.fragment
    def _render_candidate_card(row: dict, context: str):
        """1候補の表示＋操作ボタン。context='replace' or 'altlist' で色分け。

        2026-05-25 W174-pm user 報告: 採用/不採用 button 押下後 st.rerun() で
        st.tabs() が tab 0 (復活候補) にリセットされ、user が操作中のタブから
        移動してしまう UX バグ。@st.fragment で button rerun を fragment scope
        に限定し、親の st.tabs() 状態を維持する (同 codebase _render_oos_block
        と同 pattern).

        scope 設計 (code-reviewer HIGH-2 対応):
        - 不採用 (L5305 付近): default fragment scope = タブ維持優先
          + session_state `_sup_rejected_{cid}` hide フラグで「処理済」caption
        - 採用 (L5293 付近): `st.rerun(scope="app")` で full rerun =
          採用直後の photo prompt / desc prompt section (L5314+) を表示優先
          (採用は元から status='applied' で履歴タブに candidate 移動 = タブ
          維持は副次的、photo prompt 表示が UX 仕様 W112+W148-X)
        """
        cid = row["id"]
        # W174-pm H-1: 不採用直後の hide caption (fragment scope で card 自体は
        # 残るため、user に「処理されました」明示しないと「効いてない」と感じる)
        if st.session_state.get(f"_sup_rejected_{cid}"):
            st.caption(
                f"✓ 不採用にしました (cid={cid})。"
                "次回画面更新で履歴タブに移動します。"
            )
            return
        # W174-pm 別SKU出品機会 (alt_only) 採用後の hide caption
        # 個別出品タブで pre-fill 済を user に明示 + 次アクション誘導
        if st.session_state.get(f"_sup_il_prefilled_{cid}"):
            st.caption(
                f"✓ 採用 → 「個別出品」タブで仕入先 URL pre-fill 済 (cid={cid})。"
                "タブを切り替えて続行してください。"
            )
            return
        # W112 H-3 (2026-05-08 retrospective): 前回 click のメッセージを表示
        # (rerun 後に消えないよう session_state 経由で持ち越し).
        for _lvl, _msg in st.session_state.pop(f"_sup_msgs_{cid}", []):
            getattr(st, _lvl, st.info)(_msg)
        # W212-supplier-card-cleanup: カード描画用のうち後続採用フローでも参照
        # する 5 変数のみ残置. platform / price / reasoning / alt_note /
        # junk_flag / profitable はヘルパ内へ移送 (K2 Surgical).
        score = row.get("match_score") or 0
        title = row.get("candidate_title") or "(タイトル未取得)"
        url = row.get("candidate_url", "")
        status = row.get("status", "pending")
        # W212-supplier-card-cleanup (2026-06-04): カード HTML を純関数ヘルパに分離.
        # caller 側で DB / settings 依存値 (eBay USD price / JPY 換算 / profit_jpy /
        # parent_status) を取得してヘルパに渡す. ヘルパは DB アクセスを行わない.
        # money-direct path (採用/不採用/ReviseItem) は不変、純粋に表示整理のみ.
        # 2026-04-26: eBay 出品額 (USD + JPY 換算) 表示。user 要望対応.
        # ebay_item_id から ebay_listings.current_price を引いて利益判断と並列表示.
        # W314 Phase 4 (2026-07-03): N+1 解消。旧実装はカード毎に
        # get_ebay_listing_by_item_id(SELECT * FROM ebay_listings) を個別発行して
        # いたが、caller (render_supplier_candidates_tab) が既に _sup_parent_status
        # 等と同じ一括 SELECT で current_price / ebay_image_url を取得済のため、
        # そのキャッシュ dict から引く (sku-rules: ebay_item_id キー、DB 再アクセスなし)。
        _ebay_price_usd: Optional[float] = None
        _ebay_listing = _sup_parent_listing.get(row.get("ebay_item_id") or "")
        if _ebay_listing:
            _ebay_price_usd = _ebay_listing.get("current_price")
        # 為替レートで JPY 換算 (settings の exchange_rate 使用、無ければ 150 fallback)
        try:
            _fx = float(s.get("exchange_rate") or 150)
        except (TypeError, ValueError):
            _fx = 150.0
        _ebay_price_jpy = (
            int(_ebay_price_usd * _fx) if _ebay_price_usd else None
        )
        # 仕入先復活警告判定用の親 status (caller 側で N+1 回避済の dict から取得)
        _parent_ss = _sup_parent_status.get(row.get("ebay_item_id") or "", "")

        from tabs._supplier_card_html import render_supplier_card_html
        # W258/Phase-B (2026-06-11): eBay 画像 + 仕入先画像を比較カードに渡す。
        # ebay_image_url: _sup_parent_listing (caller の一括 SELECT) から
        # (W314 Phase 4 で get_ebay_listing_by_item_id 個別呼出から統合).
        # candidate_image_url: supplier_candidates 行の列 (v71 migration 後) から。
        _ebay_img_url: Optional[str] = (
            _ebay_listing.get("ebay_image_url") if _ebay_listing else None
        )
        _cand_img_url: Optional[str] = row.get("candidate_image_url")
        # 2026-06-11 user 要望: 還付抜き利益も併記。DB の profit_jpy は還付込み
        # (calculate の profit_with_refund = profit + 消費税還付 + ポイント還元) なので、
        # 還付抜き = profit_jpy - 仕入×税率/(1+税率) - 仕入×ポイント率 で表示用に導出
        # (両項とも仕入価格と現 settings から決定的に逆算可、DB 列追加不要)。
        _profit_incl = row.get("profit_jpy")
        _cand_price_jpy = row.get("candidate_price_jpy")
        _profit_excl: Optional[float] = None
        if _profit_incl is not None and _cand_price_jpy and _cand_price_jpy > 0:
            try:
                _tax = float(s.get("consumption_tax_rate") or 10) / 100
                _pt = float(s.get("point_reward_rate") or 0) / 100
                _profit_excl = (
                    float(_profit_incl)
                    - float(_cand_price_jpy) * _tax / (1 + _tax)
                    - float(_cand_price_jpy) * _pt
                )
            except (TypeError, ValueError):
                _profit_excl = None
        st.markdown(
            render_supplier_card_html(
                row=row,
                ebay_price_usd=_ebay_price_usd,
                ebay_price_jpy=_ebay_price_jpy,
                profit_jpy=_profit_incl,
                parent_status=_parent_ss,
                ebay_image_url=_ebay_img_url,
                candidate_image_url=_cand_img_url,
                profit_excl_refund_jpy=_profit_excl,
                # W314 Phase 4: CSS はタブ先頭で 1 回だけ出力済み (_CARD_CSS 直接注入)。
                include_css=False,
                # 依頼ボード #50 (2026-07-04): 左ペイン (eBay 側) の現行タイトル.
                ebay_title=(_ebay_listing.get("title") if _ebay_listing else None),
            ),
            unsafe_allow_html=True,
        )

        # 履歴タブは参照専用 (操作ボタン非表示).
        # ただし W115 (2026-05-10): status='applied' は「📷 写真反映」 button のみ例外的に表示.
        # 経緯: W112 (5/8) 1-click 化で status は pending→applied 直行、accepted は遷移しない.
        # 案 A 別 button 設計を維持しつつ、applied 後のリトロアクティブ操作 path を提供.
        if context == "history":
            if status == "applied":
                _photo_key = f"history_photo_open_{cid}"
                if st.button(
                    "📷 写真反映",
                    key=f"history_btn_photo_{cid}",
                    help=(
                        "仕入先画像から Photoroom + Gemini で hero 合成 → "
                        "EPS upload → ReviseItem PictureDetails で eBay 反映"
                    ),
                ):
                    st.session_state[_photo_key] = (
                        not st.session_state.get(_photo_key, False)
                    )
                if st.session_state.get(_photo_key, False):
                    # 2026-05-20 Codex HIGH: 採用直後 followup (画面上部) で同 cid が
                    # 既に表示されている場合、ここで再 render すると
                    # `render_supplier_photo_apply_section` 内の widget key
                    # (sup_*_{cid}) が重複し Streamlit duplicate-key エラーで
                    # 画面破綻。W314 Phase 2 S6 (2026-07-03): followup 側は
                    # 統一パネル (render_finishing_panel) に一本化され「はい/いいえ」
                    # プロンプトを経由せず cid が followup 対象である間は常時
                    # render_supplier_photo_apply_section を内包する。そのため
                    # 判定基準を _sup_photo_open_inline_ 単独から、followup 側の
                    # _followup_cids 収集条件 (4 flag いずれか True) と同一に更新。
                    _followup_active = any(
                        st.session_state.get(f"{_p}{cid}")
                        for _p in (
                            "_sup_photo_prompt_", "_sup_photo_open_inline_",
                            "_sup_desc_prompt_", "_sup_desc_open_inline_",
                        )
                    )
                    if _followup_active:
                        st.caption(
                            "⚠️ この候補は採用直後の商品仕上げパネル (画面上部) で"
                            "既に表示されています。そちらで操作してください。"
                        )
                    else:
                        from tabs._supplier_photo_pipeline import (
                            render_supplier_photo_apply_section,
                        )
                        render_supplier_photo_apply_section(
                            candidate_id=cid,
                            candidate_url=url,
                            ebay_item_id=row.get("ebay_item_id") or "",
                            candidate_title=title,
                        )
            return

        # 2026-05-08 W112 (UX 1-click 化) + 2026-05-09 W112 retrospective fix (H-1〜H-5):
        # 採用ボタン = accept + apply (eBay ReviseItem) + qty 復元 (revive のみ) 一気通貫.
        #   H-1: bare except → 限定例外 + logger.exception で痕跡保存 (Q0 silent skip 防止)
        #   H-2: st.rerun() 後に明示 return (Streamlit 仕様変更時の防御)
        #   H-3: メッセージは session_state 経由で rerun 越しに表示 (rerun で消失する UX 退化防止)
        #   H-5: session_state lock で重複 click 防止
        # #35/#36 (2026-06-28): 全パターンで「個別出品で新規」「採用」「不採用」の 3 ボタン常時表示。
        # alt_only (score<60 + alt=1) の候補: apply (SKU 書換) は task 側でブロックされるため、
        # 「採用」は accept_supplier_candidate のみ (status='accepted' 記録) に留める。
        # 「個別出品で新規」は全パターンで il_pending_supplier_url prefill を行う。
        alt_only = (score < 60) and bool(row.get("alt_listing_possible"))
        if alt_only:
            st.caption(
                "別SKU出品機会: 「個別出品で新規」で「個別出品」タブに仕入先 URL を pre-fill (SKU 書換なし)、"
                "「採用」は確認の上 現 listing の SKU をこの候補 URL に書き換えて eBay 反映します"
            )

        _lock_key = f"_sup_lock_{cid}"
        _processing = st.session_state.get(_lock_key, False)

        # user フィードバック #1 (2026-07-03): 採用ボタンを 3 択化したため、
        # ボタン数が 3 → 4 になった。個別出品 / SKUのみ / 編集あり / 不採用 の
        # 4 連レイアウト (K1: 列数のみ変更)。カード幅が狭い時は wrap するが
        # streamlit columns はそのまま横並び表示するため、ボタン文言を短めに
        # 保つ (「編集あり」等)。
        _btn_cols = st.columns(4)
        with _btn_cols[0]:
            # 「個別出品で新規」ボタン: 全パターン常時表示。
            # 仕入先 URL を「個別出品」タブの URL 欄に pre-fill する。
            # status は 'accepted' に変更して採用済み記録も同時に行う。
            if st.button(
                "個別出品",
                key=f"sup_new_listing_{context}_{cid}",
                help="仕入先 URL を「個別出品」タブの URL 欄に pre-fill (status='accepted' 印付け)",
            ):
                try:
                    update_supplier_candidate_status(cid, "accepted")
                    # 個別出品タブの URL prefill (依頼ボード#28 修正 2026-06-17):
                    # 個別出品タブ (_SS="il_") は widget 生成前に
                    # `il_pending_supplier_url` (pending seed) を読んで input 欄へ
                    # 反映する設計 (tab_individual_listing._render_step1_urls L464-469)。
                    # 旧 `il_supplier_url` (pending_ 欠落) はどこからも読まれず prefill
                    # が常にスキップされていた = 本依頼の真因。pending seed キーに統一。
                    st.session_state["il_pending_supplier_url"] = url
                    st.session_state[f"_sup_il_prefilled_{cid}"] = True
                    st.toast(
                        f"「個別出品」タブに仕入先 URL を pre-fill しました "
                        f"(cid={cid})。タブを切り替えて続行してください。",
                        icon="✓",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("new_listing prefill failed cid=%s", cid)
                    st.session_state[f"_sup_msgs_{cid}"] = [
                        ("error", f"採用記録失敗 (cid={cid})。詳細はログ確認。"),
                    ]
                st.rerun(scope="fragment")
                return  # H-2
        # user フィードバック #1 (2026-07-03): 「採用」ボタンを 2 択化
        # (SKUのみ / 編集あり)。alt_only の 2 段確認は「choice を引き継ぐ」形で
        # 押されたボタン (SKUのみ/編集あり) の open_editor 値を session_state に
        # 保存してから warning へ進む (K1: 確定側で再選択させない)。
        _confirm_key = f"_sup_confirm_alt_adopt_{cid}"
        _confirm_choice_key = f"_sup_confirm_alt_choice_{cid}"

        def _do_adopt(open_editor: bool, allow_alt_override: bool = False) -> None:
            """通常 採用 path (revive / replace / alt-override) の共通 handler.

            実行部 (accept→apply→followup フラグ set→qty 復元) は
            tabs._adopt_candidate.adopt_candidate に単一化済 (W314 Phase 3 T1)。
            open_editor は #1 (2026-07-03) の 3 択化に対応した委譲。
            """
            st.session_state[_lock_key] = True
            _msgs: list[tuple[str, str]] = []
            _eid = row.get("ebay_item_id") or ""
            try:
                _cfg_path = (
                    Path(__file__).resolve().parent.parent
                    / "config" / "schedule_config.json"
                )
                _cfg = {}
                _cfg_load_ok = True
                if _cfg_path.exists():
                    try:
                        _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as _e:
                        logger.exception(
                            "schedule_config.json 読込失敗 cid=%s", cid,
                        )
                        _msgs.append(("error", f"schedule_config.json 読込失敗: {_e}"))
                        _cfg_load_ok = False
                if _cfg_load_ok:
                    with st.spinner("仕入先の在庫を確認中..."):
                        res = adopt_candidate(
                            cid, _cfg, source_tab="supplier",
                            allow_alt_override=allow_alt_override,
                            open_editor=open_editor,
                        )
                    if not res.get("success"):
                        logger.error(
                            "supplier adopt failed cid=%s eid=%s stage=%s msg=%s "
                            "override=%s open_editor=%s",
                            cid, _eid, res.get("stage"), res.get("message"),
                            allow_alt_override, open_editor,
                        )
                        if res.get("stage") == "apply":
                            _msgs.append((
                                "error",
                                f"eBay 反映失敗: {res.get('message') or 'apply エラー'}",
                            ))
                        else:
                            _msgs.append((
                                "error",
                                res.get("message") or "採用に失敗しました",
                            ))
                    else:
                        _base_msg = res.get("message") or (
                            "別SKU手動override採用→eBay反映 成功"
                            if allow_alt_override
                            else "採用→eBay 反映 成功"
                        )
                        _suffix = (
                            "（SKUのみ、編集パネル非展開）" if not open_editor else ""
                        )
                        _msgs.append(("success", f"{_base_msg}{_suffix}"))
                        if res.get("qty_restore_message"):
                            _msgs.append((
                                "success" if res.get("qty_restore_ok") else "error",
                                res["qty_restore_message"],
                            ))
            except Exception:  # noqa: BLE001 — H-A: 想定外例外も logger + UI msg で必ず痕跡残す
                logger.exception(
                    "supplier accept/apply 想定外例外 cid=%s eid=%s override=%s open_editor=%s",
                    cid, _eid, allow_alt_override, open_editor,
                )
                _msgs.append((
                    "error",
                    f"想定外エラーが発生しました (cid={cid}). "
                    "詳細はログ確認、手動で在庫/SKU を確認してください。",
                ))
            finally:
                st.session_state[_lock_key] = False
                st.session_state[f"_sup_msgs_{cid}"] = _msgs

        # ── alt_only 2 段確認モード: 選択済み choice を保持して警告 + 確定/やめる ──
        # confirm フラグが立っている時は button 行を出さず、warning + 確定/やめる を
        # フル幅で描画する (旧: btn_cols[1] 内に押し込んでいたが SKUのみ/編集あり
        # 分離で幅が足りなくなるため上に引き出し、K1)。
        if _processing:
            st.caption("⏳ 処理中... (二度押し防止)")
            return  # button 行は描画しない
        if alt_only and st.session_state.get(_confirm_key):
            _choice = st.session_state.get(_confirm_choice_key, "editor")
            _choice_label = "SKUのみ" if _choice == "skuonly" else "編集あり"
            st.warning(
                f"⚠️ 別SKU候補です。現 listing の SKU をこの候補 URL に書き換えて"
                f" eBay に反映します。別商品の可能性があるため、正しい商品か確認して"
                f"ください。（採用モード: **{_choice_label}**）"
            )
            _conf_c1, _conf_c2 = st.columns(2)
            with _conf_c1:
                if st.button(
                    f"確定（{_choice_label} で書換採用）",
                    key=f"sup_accept_alt_confirm_{context}_{cid}",
                    type="primary",
                ):
                    st.session_state[_confirm_key] = False
                    st.session_state.pop(_confirm_choice_key, None)
                    _do_adopt(
                        open_editor=(_choice == "editor"),
                        allow_alt_override=True,
                    )
                    st.rerun(scope="app")
                    return  # H-2
            with _conf_c2:
                if st.button(
                    "やめる",
                    key=f"sup_accept_alt_cancel_{context}_{cid}",
                ):
                    st.session_state[_confirm_key] = False
                    st.session_state.pop(_confirm_choice_key, None)
                    st.rerun(scope="fragment")
                    return  # H-2
            return  # 確認モード中は個別出品/採用/不採用ボタンを重ねて出さない

        with _btn_cols[1]:
            # 「採用 (SKUのみ)」: adopt + eBay 反映のみ、followup パネル非展開
            if st.button(
                "SKUのみ",
                key=f"sup_accept_skuonly_{context}_{cid}",
                help=(
                    "採用 (SKUのみ): 別SKU候補は 1 度確認を挟んでから SKU 書換で反映"
                    if alt_only
                    else "採用 (SKUのみ): SKU 切替のみ、編集パネルは開かない。"
                    " 仕入先を差し替えるだけで済ませたい時。"
                ),
            ):
                if alt_only:
                    st.session_state[_confirm_key] = True
                    st.session_state[_confirm_choice_key] = "skuonly"
                    st.rerun(scope="fragment")
                    return  # H-2
                _do_adopt(open_editor=False)
                st.rerun(scope="app")
                return  # H-2
        with _btn_cols[2]:
            # 「採用 (編集あり)」: adopt + followup パネル展開 (従来動作)
            if st.button(
                "編集あり",
                key=f"sup_accept_editor_{context}_{cid}",
                type="primary",
                help=(
                    "採用 (編集あり): 別SKU候補は 1 度確認を挟んでから書換 + パネル展開"
                    if alt_only
                    else "採用 (編集あり): SKU 書換 + 商品仕上げパネル展開"
                    " (タイトル/画像/ランク/数量 を続けて編集する時)。"
                ),
            ):
                if alt_only:
                    st.session_state[_confirm_key] = True
                    st.session_state[_confirm_choice_key] = "editor"
                    st.rerun(scope="fragment")
                    return  # H-2
                _do_adopt(open_editor=True)
                st.rerun(scope="app")
                return  # H-2
        with _btn_cols[3]:
            # 2026-06-11 不採用高速化: on_click コールバックは fragment 再実行の前に
            # 走るため、DB 更新 + hide フラグを先に済ませて 1 往復で関数冒頭の早期
            # return (caption 表示) に直行する。旧 if st.button + st.rerun(scope=
            # "fragment") は 2 往復 (実測 2.4s) だった。fragment 内 widget の
            # interaction は fragment-scope rerun のままなのでタブ維持も不変 (W174-pm)。
            def _on_reject(cid_: int = cid) -> None:
                # 不採用の DB 更新も例外吸収せず痕跡残す (Surface B 対称性 / Q0 silent skip 防止)
                try:
                    update_supplier_candidate_status(cid_, "rejected")
                    # W174-pm H-1: fragment scope で card 自体が消えないので
                    # hide フラグを立てて関数冒頭の早期 return path で caption 表示
                    st.session_state[f"_sup_rejected_{cid_}"] = True
                except Exception:  # noqa: BLE001
                    logger.exception("supplier reject failed cid=%s", cid_)
                    st.session_state[f"_sup_msgs_{cid_}"] = [
                        ("error", f"不採用記録に失敗しました (cid={cid_}). 詳細はログ確認."),
                    ]

            st.button("不採用", key=f"sup_reject_{context}_{cid}", on_click=_on_reject)

    @st.fragment
    def _render_card_page(rows: list[dict], context: str, page_key: str) -> None:
        """W258 Phase D (2026-06-11): カードリストを初期 10 件 + さらに表示に
        ページング。旧 [:30] 一括描画は 4 タブ合計 ~80 枚で初期 4.4s かかっていた。
        fragment スコープなので「さらに表示」押下でも st.tabs のタブ位置維持
        (W174-pm と同 pattern)。page_key は履歴タブで rejected/applied の 2 リストを
        区別するため context と別引数 (widget key 衝突防止)。

        依頼ボード #50 (2026-07-04): タブ密度化 A2 (カード 2 枚横並び) は
        「2 つ別のものを横に並べる形」が見づらいと user 差し戻し。1 カラム
        縦積みに戻す (密度はカード内部の左右ペイン分割 [``sc-split``] で確保)。
        件数/ページングロジックは不変、配置のみ変更。"""
        _key = f"_sup_shown_{page_key}"
        _shown = st.session_state.setdefault(_key, 10)
        for _row in rows[:_shown]:
            _render_candidate_card(_row, context=context)
        _remain = len(rows) - _shown
        if _remain > 0:
            def _show_more(k: str = _key) -> None:
                st.session_state[k] = st.session_state.get(k, 10) + 20
            st.button(
                f"さらに 20 件表示 (残り {_remain} 件)",
                key=f"sup_more_{page_key}",
                on_click=_show_more,
            )

    # ── 採用後フォローアップ欄 (写真/description prompt) ──
    # 2026-06-12 依頼ボード#11: inline ブロックを tabs/_supplier_followup_section.py
    # へ移設 (在庫監視タブと共有)。経緯コメント (2026-05-20 W115 / 2026-06-11
    # バグ3/4 / 依頼ボード#12 行き先通知) は移設先に保持。
    from tabs._supplier_followup_section import render_supplier_followup_section
    if render_supplier_followup_section(source_tab="supplier"):
        st.markdown("---")

    # ── サブタブ 4 分割 (2026-04-24 rev2) ──
    # 復活候補 = 最優先: eBay 在庫0 商品に仕入先が見つかった → 採用で qty 自動復元
    # 置換候補 = eBay 在庫≥1 商品の SKU 書換
    # 別SKU出品機会 = alt=1 (新規出品検討)
    # 履歴 = rejected / applied (過去判断の参照用)
    _tab_revive, _tab_replace, _tab_altlist, _tab_history = st.tabs([
        f"復活候補 ({len(_sup_revive)})",
        f"置換候補 ({len(_sup_replace)})",
        f"別SKU出品機会 ({len(_sup_altlist)})",
        f"履歴 ({len(_sup_history)})",
    ])

    with _tab_revive:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#b35a2e;text-transform:uppercase;">'
            '復 活 候 補 キ ュ ー</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "**eBay 在庫 0 商品**に対して仕入先が見つかった候補。"
            "採用すると SKU が新仕入先 URL ベースに書き換わり、**eBay 在庫も自動で 1 に復元** されます。"
            " (Q3=A, 採用→revise_item_sku+revise_inventory_quantity(1) 連続実行)"
        )
        if not _sup_revive:
            st.info(
                "復活候補はありません。eBay 在庫 0 の商品に新仕入先が見つかればここに表示されます。"
            )
        else:
            _render_card_page(_sup_revive, "revive", "revive")

    with _tab_replace:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#5f6557;text-transform:uppercase;">'
            '置 換 候 補 キ ュ ー</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "eBay 在庫≥1 の商品で、Claude が同一商品 (または置換可能) と判定した仕入先候補。"
            " 採用で SKU が新仕入先 URL ベースに書き換わります (在庫は既にあるので qty 復元なし)。"
        )
        if not _sup_replace:
            if not _sup_all:
                st.info(
                    "候補がまだ生成されていません。"
                    "在庫切れSKUが検出されると Pattern 1（即時探索）"
                    "または朝バッチ Pattern 2（一括探索）で自動生成されます。"
                    "「手動実行」タブから task_inventory_check → task_supplier_candidate_search の順で実行可能です。"
                )
            else:
                st.info(
                    "actionable な置換候補はありません (rejected/applied は履歴タブへ移動しました)。"
                )
        else:
            _render_card_page(_sup_replace, "replace", "replace")

    with _tab_altlist:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#8b7355;text-transform:uppercase;">'
            '別 S K U 出 品 機 会</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "既存 SKU とは別物だが「別 SKU で新規出品する価値あり」と Claude が判定した商材。"
            " 現 listing の置換には使えない。新規出品フロー (個別出品タブ) で活用してください。"
        )
        if not _sup_altlist:
            st.info("別SKU出品機会は現在の条件では見つかりません。")
        else:
            _render_card_page(_sup_altlist, "altlist", "altlist")

    with _tab_history:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#6b6b6b;text-transform:uppercase;">'
            '履 歴 （不 採 用 / 反 映 済）</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "過去の判断ログ (rejected = 不採用にしたもの / applied = 既に反映済)。"
            " URL は時間経過で失効している場合があります。"
            " 参照専用、アクションは他サブタブで実施してください。"
        )
        if not _sup_history:
            st.info("履歴はまだありません。")
        else:
            # rejected と applied を分けて表示
            _rej = [r for r in _sup_history if (r.get("status") or "").lower() == "rejected"]
            _app = [r for r in _sup_history if (r.get("status") or "").lower() == "applied"]
            if _rej:
                st.markdown(f"#### 不採用 ({len(_rej)}件)")
                _render_card_page(_rej, "history", "history_rej")
            if _app:
                st.markdown(f"#### 反映済 ({len(_app)}件)")
                _render_card_page(_app, "history", "history_app")
