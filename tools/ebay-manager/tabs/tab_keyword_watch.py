"""W148 — キーワード新着監視タブ (AlertCrawler 移植)。

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md (v2.2)

3 セクション構成:
  1. 巡回サマリ + センチネル初期化 + 今すぐ巡回
  2. ウォッチ一覧 (編集/削除/履歴)
  3. 追加 form
  4. AlertCrawler legacy 取込 UI
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import streamlit as st

from monitor.keyword_watch_db import (
    add_watch,
    list_watches,
    update_watch,
    delete_watch,
    get_recent_hits,
    get_watch_stats,
    init_default_sentinels,
)
from ui_cache import bump_db_version

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

# タブ密度化リファクタ B2 (2026-07-04): このタブ配下だけに効くスコープ CSS
# (st.container(key="kw_watch_root") 内側 div に付く class="st-key-kw_watch_root"
# を掴む。他タブ / app.py のグローバル密度 CSS には触れない = K2 surgical、
# tab_request_board.py の _DENSITY_CSS パターンを踏襲)。
# user 承認済み密度スペック: フォント 12px / 行高 22-28px / 全幅引き伸ばし禁止 /
# 2 カラム詰め込み可 / 常時 caption → tooltip / 赤は要対応のみ / expander 既定閉。
_KW_DENSITY_CSS = """<style>
div[class*="st-key-kw_watch_root"] [data-testid="stMarkdownContainer"] p,
div[class*="st-key-kw_watch_root"] [data-testid="stCaptionContainer"] p {
    font-size: 12px !important;
    line-height: 24px !important;
    margin: 2px 0 !important;
}
div[class*="st-key-kw_watch_root"] [data-testid="stExpander"] details > summary {
    padding: 3px 8px !important;
    min-height: 24px !important;
}
div[class*="st-key-kw_watch_root"] [data-testid="stExpander"] summary p,
div[class*="st-key-kw_watch_root"] [data-testid="stExpander"] summary span {
    font-size: 12px !important;
    line-height: 22px !important;
    margin: 0 !important;
}
div[class*="st-key-kw_watch_root"] [data-testid="stButton"] > button,
div[class*="st-key-kw_watch_root"] [data-testid="stFormSubmitButton"] > button {
    min-height: 28px !important;
    padding: 3px 10px !important;
    font-size: 12px !important;
    line-height: 22px !important;
}
div[class*="st-key-kw_watch_root"] [data-testid="stTextInput"] input,
div[class*="st-key-kw_watch_root"] [data-testid="stTextArea"] textarea,
div[class*="st-key-kw_watch_root"] [data-testid="stNumberInput"] input,
div[class*="st-key-kw_watch_root"] [data-testid="stSelectbox"] div[role="combobox"] {
    font-size: 12px !important;
}
div[class*="st-key-kw_watch_root"] [data-testid="stCheckbox"] label p {
    font-size: 12px !important;
}
</style>"""


def _to_jst_str(ts: Optional[str]) -> str:
    """UTC TIMESTAMP 文字列を JST 表示。"""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts else \
            datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return ts


def _build_search_url(
    site: str,
    keyword: str,
    price_min_jpy: Optional[int] = None,
    price_max_jpy: Optional[int] = None,
) -> str:
    """site + keyword (+ 価格下限) から検索 URL を構築。新着順ソート固定。

    価格 param 名 (2026-06-01 実サイト live 検証で裏取り):
      - mercari: price_min / (price_max は焼かない)
      - yahoo_auctions: aucminprice / (aucmaxprice は焼かない)

    W206 (2026-06-01): **上限は URL に焼かない (通知ゲート専用)**。
      取得段階で上限を URL filter すると履歴 (`keyword_watch_hits`) に
      上限超過品の記録が残らず後で見返せない。下限は「明らかに対象外の安物」
      を取得段階で物理排除する一方、上限は `_check_price_range` で通知ゲート
      にのみ作用させる方針 (watch.price_max_jpy をそのまま使う)。
    sort=created_time&order=desc / s1=new&o1=d (新着順) は維持必須。

    price_min_jpy が None または 0 の時のみ price_min param を省略 (0 = 未設定扱い)。
    price_max_jpy はシグネチャに残すが本関数では参照しない (呼出側互換性のため)。
    """
    _ = price_max_jpy  # 通知ゲート専用、URL には焼かない (W206)
    kw = quote_plus(keyword.strip())
    if site == "mercari":
        url = (
            f"https://jp.mercari.com/search?keyword={kw}"
            "&status=on_sale&sort=created_time&order=desc"
        )
        if price_min_jpy:
            url += f"&price_min={int(price_min_jpy)}"
        return url
    if site == "yahoo_auctions":
        url = f"https://auctions.yahoo.co.jp/search/search?p={kw}&s1=new&o1=d"
        if price_min_jpy:
            url += f"&aucminprice={int(price_min_jpy)}"
        return url
    raise ValueError(f"unsupported site: {site}")


def _compute_watch_update(
    target: dict,
    kw_new: str,
    pmin_val: Optional[int],
    pmax_val: Optional[int],
    memo: str,
    is_active: bool,
    ebay_item_id: Optional[str] = None,
) -> dict:
    """編集保存時の update_fields を計算する純関数 (UI から分離してテスト可能化)。

    - keyword / 価格レンジが変わったら search_url を再生成して含める
      (巡回 URL が古い filter のまま残る既存バグの修正)。
    - sentinel watch は search_url を固定保護する設計のため、keyword / 価格の
      変更は禁止 (ValueError)。黙ってドロップすると user の編集意図を silent skip
      するため (Q0)、明示的に例外を投げて UI 側で error 表示させる。memo / active
      のみ変更可。
    - W206: ebay_item_id は任意メタ (None クリア可)。URL に影響しないため
      changed 判定 (URL 再生成トリガ) には含めない。
    """
    changed = (
        kw_new != target["keyword"]
        or pmin_val != target.get("price_min_jpy")
        or pmax_val != target.get("price_max_jpy")
    )
    if target.get("is_sentinel") and changed:
        raise ValueError(
            "sentinel watch はキーワード/価格を編集できません (メモ・active のみ変更可)"
        )
    fields = dict(
        keyword=kw_new,
        price_min_jpy=pmin_val,
        price_max_jpy=pmax_val,
        memo=memo,
        is_active=1 if is_active else 0,
        ebay_item_id=ebay_item_id,
    )
    if changed:
        fields["search_url"] = _build_search_url(
            target["site"], kw_new, pmin_val, pmax_val
        )
    return fields


def _get_last_hit_at_map(watch_ids: list[int]) -> dict[int, str]:
    """watch_id → 最新 detected_at (UTC 文字列) を一括取得 (N+1 回避、読み取り専用)。

    「最終ヒット」列の density 化 (B2) で新規追加。keyword_watch_hits への
    GROUP BY 集計のみ (書込みなし)、watch_id は SKU ではないため sku-rules 対象外。
    """
    if not watch_ids:
        return {}
    from monitor.database import get_conn

    placeholders = ",".join("?" for _ in watch_ids)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT watch_id, MAX(detected_at) AS last_hit FROM keyword_watch_hits "
            f"WHERE watch_id IN ({placeholders}) GROUP BY watch_id",
            watch_ids,
        ).fetchall()
    return {r["watch_id"]: r["last_hit"] for r in rows}


def _render_hit_row_html(hit: dict, site: str) -> str:
    """1 hit を通知センター調のコンパクト 1 行 HTML に整形する純関数。

    レイアウト: 商品名 — 価格 | サイト ・時刻 (`_notification_center_html.py` の
    1 行フォーマットを踏襲、このタブ専用に別 CSS scope で定義)。
    """
    title = (hit.get("title") or "").strip() or "(タイトル不明)"
    title_disp = title if len(title) <= 50 else title[:49] + "…"
    price_disp = f"¥{hit['price_jpy']:,}" if hit.get("price_jpy") else "—"
    time_disp = _to_jst_str(hit.get("detected_at"))
    site_label = {"mercari": "🛒メルカリ", "yahoo_auctions": "🔨ヤフオク"}.get(site, site)
    range_icon = " ✅" if hit.get("in_price_range") else ""
    discord_icon = " 📤" if hit.get("discord_sent") else ""
    url = hit.get("found_item_url") or ""

    title_html = (
        f'<a href="{escape(url)}" target="_blank" rel="noopener" '
        f'style="color:#2a2e2a;text-decoration:none;font-weight:600;">'
        f'{escape(title_disp)}</a>' if url else f'<span style="font-weight:600;">{escape(title_disp)}</span>'
    )
    return (
        '<div style="font-size:12px;line-height:24px;padding:2px 8px;'
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
        'border-bottom:1px solid rgba(0,0,0,0.06);">'
        f'{title_html}'
        f'<span style="color:#8d927f;"> — </span>{escape(price_disp)}'
        f'<span style="color:#8d927f;"> | </span>{escape(site_label)}'
        f'<span style="color:#8d927f;"> ・</span>{escape(time_disp)}'
        f'{range_icon}{discord_icon}'
        '</div>'
    )


def _render_summary_section() -> None:
    """1. 巡回サマリ + センチネル初期化 + 今すぐ巡回"""
    stats = get_watch_stats()

    # sentinel 未登録サイトの警告 (Codex 3回目 MEDIUM、UI 側 redundant 表示)
    watches = list_watches(active_only=True)
    watched_sites = {w["site"] for w in watches}
    sentinel_sites = {w["site"] for w in watches if w.get("is_sentinel")}
    orphan_sites = sorted(watched_sites - sentinel_sites)
    if orphan_sites:
        st.warning(
            f"⚠️ センチネル未登録: {orphan_sites}。"
            "DOM 変更/bot ban の自動検知が無効です。"
            "下の「センチネル初期化」ボタンを実行推奨。"
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("active 件数", stats["active"])
    c2.metric("センチネル", stats["sentinel_active"])
    c3.metric("24h hits", stats["hits_24h"])
    c4.metric("未通知 (in_range)", stats["unnotified_in_range"])

    st.caption(f"最終巡回時刻: {_to_jst_str(stats['last_crawl_at'])}")

    c5, c6 = st.columns(2)
    with c5:
        if st.button("🛡️ センチネル初期化", help="各サイトに DOM 健康センチネルを 1 件登録 (既登録は skip)"):
            n = init_default_sentinels()
            bump_db_version()
            st.success(f"センチネル {n} 件を新規登録しました (既登録は skip)")
            st.rerun()
    with c6:
        if st.button("🚀 今すぐ巡回", help="全 active watch を即時巡回 (1-5 分かかる場合あり)"):
            with st.spinner("巡回中… (Playwright で別プロセス起動)"):
                _run_crawl_now()
            st.rerun()


def _run_crawl_now() -> None:
    """UI「今すぐ巡回」: subprocess 起動 (Streamlit script は main thread だが、
    巡回中に UI が止まらないよう subprocess に統一)。"""
    cwd = Path(__file__).resolve().parent.parent  # tools/ebay-manager
    try:
        res = subprocess.run(
            [sys.executable, "-m", "tasks.task_keyword_watch_crawl"],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,  # UI 経路はやや長めに
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0:
            try:
                r = json.loads((res.stdout or "").strip().splitlines()[-1])
                st.success(f"巡回完了: {r.get('message', '')}")
            except Exception:
                st.success(f"巡回完了: {(res.stdout or '')[:200]}")
        else:
            st.error(
                f"巡回失敗 (exit={res.returncode}): "
                f"{(res.stderr or '')[:300]}"
            )
    except subprocess.TimeoutExpired:
        st.error("巡回 timeout (900s 超過)")
    except Exception as e:
        st.error(f"巡回起動失敗: {type(e).__name__}: {e}")


def _render_watch_list() -> None:
    """2. ウォッチ一覧"""
    st.subheader("📋 ウォッチ一覧")
    watches = list_watches(active_only=False)
    if not watches:
        st.info("まだ登録がありません。下の「新規追加」または「AlertCrawler 取込」を使ってください。")
        return

    # 依頼ボード#24: リサーチ承認で自動登録された監視を区別表示する。
    # research_candidates.watch_ids_json から由来を導出 (スキーマ変更なし)。
    try:
        from monitor.research_candidates_db import get_research_watch_ids
        research_ids = get_research_watch_ids()
    except Exception as e:  # noqa: BLE001 — 由来表示は付加情報、失敗で一覧を止めない
        logger.warning("get_research_watch_ids 失敗: %s", e)
        research_ids = set()

    if research_ids:
        only_research = st.checkbox(
            f"🔬 リサーチ由来のみ表示 ({len(research_ids)} 件)",
            value=False, key="kw_filter_research_only",
        )
        if only_research:
            watches = [w for w in watches if w["id"] in research_ids]

    # B2 密度化 (2026-07-04): 1 watch 1 行に圧縮 (キーワード/サイト/希望価格/
    # 最終ヒット)。由来・sentinel はキーワード先頭の絵文字に統合、item_id/memo は
    # 下の「編集/削除」で参照可能 (機能は不変、常時表示のみ間引き)。
    # 状態列は 停止中/エラー/正常 を 1 セルに集約 (last_error を隠さない = Q0)。
    last_hit_map = _get_last_hit_at_map([w["id"] for w in watches])
    rows = []
    for w in watches:
        pmin = w.get("price_min_jpy")
        pmax = w.get("price_max_jpy")
        if pmin is None and pmax is None:
            price_str = "(未設定=通知無効)"
        else:
            lo = f"¥{pmin:,}" if pmin is not None else "—"
            hi = f"¥{pmax:,}" if pmax is not None else "—"
            price_str = f"{lo}〜{hi}"
        if not w["is_active"]:
            state = "❌停止中"
        elif w.get("last_error"):
            state = f"⚠️{(w['last_error'] or '')[:20]}"
        else:
            state = "✅"
        prefix = ("🛡️" if w.get("is_sentinel") else "") + ("🔬" if w["id"] in research_ids else "")
        keyword_disp = f"{prefix} {w['keyword']}".strip() if prefix else w["keyword"]
        rows.append({
            "id": w["id"],
            "状態": state,
            "site": "🛒メルカリ" if w["site"] == "mercari" else "🔨ヤフオク",
            "keyword": keyword_disp,
            "希望価格": price_str,
            "最終ヒット": _to_jst_str(last_hit_map.get(w["id"])),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True, row_height=26)

    with st.expander("🛠 編集 / 削除", expanded=False):
        ids = [w["id"] for w in watches]
        labels = {
            w["id"]: f"#{w['id']} [{w['site']}] {w['keyword'][:30]}"
            for w in watches
        }
        sel_id = st.selectbox(
            "対象 watch", ids,
            format_func=lambda x: labels[x],
            key="kw_edit_target",
        )
        target = next(w for w in watches if w["id"] == sel_id)

        c1, c2, c3 = st.columns(3)
        with c1:
            new_keyword = st.text_input("キーワード", value=target["keyword"],
                                         key=f"kw_kw_{sel_id}")
        with c2:
            new_pmin = st.number_input(
                "下限 (¥)", min_value=0,
                value=int(target["price_min_jpy"] or 0),
                key=f"kw_pmin_{sel_id}",
            )
            pmin_unset = st.checkbox("下限なし", value=(target["price_min_jpy"] is None),
                                      key=f"kw_pmin_none_{sel_id}")
        with c3:
            new_pmax = st.number_input(
                "上限 (¥)", min_value=0,
                value=int(target["price_max_jpy"] or 0),
                key=f"kw_pmax_{sel_id}",
            )
            pmax_unset = st.checkbox("上限なし", value=(target["price_max_jpy"] is None),
                                      key=f"kw_pmax_none_{sel_id}")
        new_memo = st.text_area("メモ", value=target.get("memo") or "",
                                 key=f"kw_memo_{sel_id}", height=80)
        new_active = st.checkbox("active", value=bool(target["is_active"]),
                                  key=f"kw_active_{sel_id}")
        # W206: 任意 eBay Item ID 紐付け (Discord 通知 embed 拡充用)
        new_item_id_raw = st.text_input(
            "eBay Item ID (任意)",
            value=target.get("ebay_item_id") or "",
            key=f"kw_iid_{sel_id}",
            help="紐づく自社 eBay 出品の Item ID。空欄でクリア。",
        )

        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("💾 保存", key=f"kw_save_{sel_id}"):
                kw_new = new_keyword.strip()
                # 0 は「下限/上限なし」と同義 (_build_search_url が 0 を param 省略する
                # ため、DB 表現も None に揃えて URL と整合させる)。
                pmin_val = None if (pmin_unset or int(new_pmin) == 0) else int(new_pmin)
                pmax_val = None if (pmax_unset or int(new_pmax) == 0) else int(new_pmax)
                new_item_id = new_item_id_raw.strip() or None
                if not kw_new:
                    st.error("キーワードは必須です")
                    return
                try:
                    update_fields = _compute_watch_update(
                        target, kw_new, pmin_val, pmax_val, new_memo, new_active,
                        ebay_item_id=new_item_id,
                    )
                except ValueError as e:
                    # sentinel の keyword/価格編集禁止、または未対応 site
                    st.error(str(e))
                    return
                try:
                    update_watch(sel_id, **update_fields)
                except sqlite3.IntegrityError:
                    # UNIQUE(site, search_url) 衝突 = 同 site で同条件の watch が既存
                    st.error(
                        "更新失敗: 同じサイトで同じ検索条件 (キーワード・価格) の "
                        "watch が既に存在します"
                    )
                    return
                except Exception as e:
                    # DB ロック等の別原因まで「重複」と誤誘導しない (Q0)
                    st.error(f"更新失敗: {type(e).__name__}: {e}")
                    return
                bump_db_version()
                st.success(f"#{sel_id} を更新しました")
                st.rerun()
        with cc2:
            confirm = st.checkbox(f"#{sel_id} 削除確認", key=f"kw_del_confirm_{sel_id}")
            if st.button("🗑️ 削除 (取消不可)", key=f"kw_del_{sel_id}", disabled=not confirm):
                delete_watch(sel_id)
                bump_db_version()
                st.success(f"#{sel_id} を削除しました")
                st.rerun()

        if st.button("📜 直近 hits 20 件", key=f"kw_hist_{sel_id}"):
            hits = get_recent_hits(sel_id, limit=20)
            if not hits:
                st.info("hits なし")
            else:
                # B2 密度化: 通知センターの 1 行形式
                # (商品名 — 価格 | サイト ・時刻) に合わせてコンパクト化。
                st.markdown(
                    "".join(_render_hit_row_html(h, target["site"]) for h in hits),
                    unsafe_allow_html=True,
                )


def _render_add_form() -> None:
    """3. 新規追加 form"""
    st.subheader("➕ 新規追加")
    # 2026-06-02 (user 要望): 追加後も入力を保持 (clear_on_submit=False)。
    # 同一商品をメルカリ→ヤフオクと続けて登録する際、文言を残したまま「サイト」だけ
    # 切り替えて「追加」を押せば済むようにする (別 site + 別 URL なので dedup も通る)。
    with st.form("kw_add_form", clear_on_submit=False):
        # B2 密度化 (2026-07-04): site/keyword/価格 2 種を 1 行 (4 カラム) に圧縮。
        # 「両方なしだと通知無効」の説明は追加ボタンの tooltip へ移動 (常時 caption 廃止)。
        c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
        with c1:
            site = st.selectbox("サイト", ["mercari", "yahoo_auctions"],
                                 format_func=lambda x: "🛒 メルカリ" if x == "mercari" else "🔨 ヤフオク")
        with c2:
            keyword = st.text_input("キーワード", placeholder="例: Astell&Kern A&norma SR35")
        with c3:
            pmin = st.number_input("下限 (¥)", min_value=0, value=0, step=1000)
            pmin_unset = st.checkbox("下限なし", value=True)
        with c4:
            pmax = st.number_input("上限 (¥)", min_value=0, value=0, step=1000)
            pmax_unset = st.checkbox("上限なし", value=False)

        c5, c6 = st.columns([3, 2])
        with c5:
            memo = st.text_area("メモ (任意)", placeholder="採用後のアクション、注意点 等", height=60)
        with c6:
            # W206: 任意 eBay Item ID 紐付け (Discord 通知 embed 拡充用)
            item_id_raw = st.text_input(
                "eBay Item ID (任意)",
                placeholder="例: 358505733121",
                help="紐づく自社 eBay 出品の Item ID。通知時に eBay 販売価格を併記。",
            )

        submitted = st.form_submit_button(
            "追加",
            help="下限・上限を両方「なし」のままだと通知は無効になります (ヒット履歴には記録)。",
        )
        if submitted:
            kw = keyword.strip()
            if not kw:
                st.error("キーワードは必須です")
                return
            pmin_val = None if pmin_unset else int(pmin)
            pmax_val = None if pmax_unset else int(pmax)
            iid_val = item_id_raw.strip() or None
            try:
                url = _build_search_url(site, kw, pmin_val, pmax_val)
            except ValueError as e:
                st.error(str(e))
                return
            wid, new = add_watch(
                site=site,
                search_url=url,
                keyword=kw,
                price_min_jpy=pmin_val,
                price_max_jpy=pmax_val,
                memo=memo,
                source="manual",
                ebay_item_id=iid_val,
            )
            bump_db_version()
            if new:
                st.success(f"#{wid} を追加しました")
            else:
                st.info(f"#{wid} は既に登録済みです (同 site + 同 URL)")
            st.rerun()


def _render_legacy_import() -> None:
    """4. AlertCrawler legacy 取込 UI"""
    st.subheader("📦 AlertCrawler legacy 取込 (450 件)")

    default_src = r"C:\Users\gucch\Desktop\work\EBAY\EBAY\AlertCrawler\data.db"
    src_path = st.text_input("data.db path", value=default_src, key="kw_legacy_src")
    out_path = Path(__file__).resolve().parent.parent / "data" / "alertcrawler_legacy_export.json"

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 dump 再生成", help="src の data.db を読んで JSON に dump"):
            cwd = Path(__file__).resolve().parent.parent
            try:
                res = subprocess.run(
                    [sys.executable, "scripts/import_alertcrawler_legacy.py",
                     "--src", src_path, "--out", str(out_path)],
                    cwd=str(cwd),
                    capture_output=True, text=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if res.returncode == 0:
                    st.success(f"dump 完了: {(res.stdout or '')[:300]}")
                else:
                    st.error(f"dump 失敗: {(res.stderr or '')[:300]}")
            except Exception as e:
                st.error(f"起動失敗: {type(e).__name__}: {e}")

    if not out_path.exists():
        st.info("まず「dump 再生成」を押してください。")
        return

    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"JSON 読込失敗: {e}")
        return

    exported = data.get("exported", [])
    skipped = data.get("skipped", [])
    st.caption(
        f"total={data.get('total_rows')} / exported={len(exported)} / "
        f"skipped={len(skipped)}"
    )
    if not exported:
        return

    # filter by site
    site_filter = st.multiselect(
        "サイト絞込", ["mercari", "yahoo_auctions"],
        default=["mercari", "yahoo_auctions"],
        help="初回登録は 50 件以下からの少数運用を推奨",
    )
    rows = [r for r in exported if r["site"] in site_filter]
    st.caption(f"表示: {len(rows)} 件")

    # st.data_editor でチェック付き
    edited = st.data_editor(
        rows,
        column_config={
            "selected": st.column_config.CheckboxColumn("選択"),
            "search_url": st.column_config.LinkColumn("URL", width="small"),
            "dataC_raw": None,  # hide
        },
        column_order=["selected", "legacy_id", "site", "keyword",
                      "price_min_jpy", "price_max_jpy", "legacy_added_at",
                      "search_url"],
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="kw_legacy_editor",
    )

    selected = [r for r in edited if r.get("selected")]
    st.caption(f"選択中: {len(selected)} 件")
    if st.button("📥 選択分を登録", disabled=not selected):
        added = 0
        existed = 0
        for r in selected:
            _, new = add_watch(
                site=r["site"],
                search_url=r["search_url"],
                keyword=r["keyword"],
                price_min_jpy=r.get("price_min_jpy"),
                price_max_jpy=r.get("price_max_jpy"),
                memo=f"[AlertCrawler legacy #{r['legacy_id']} added {r.get('legacy_added_at') or ''}]",
                source="imported_alertcrawler",
            )
            if new:
                added += 1
            else:
                existed += 1
        bump_db_version()
        st.success(f"登録 {added} 件 (既登録 skip {existed} 件)")
        st.rerun()


def render_keyword_watch_tab() -> None:
    """W148 キーワード新着監視タブ entry。"""
    # B2 密度化 (2026-07-04): 常時表示の説明 caption → subheader の hover tooltip。
    with st.container(key="kw_watch_root"):
        st.markdown(_KW_DENSITY_CSS, unsafe_allow_html=True)
        st.subheader(
            "🔔 キーワード新着監視 (W148)",
            help=(
                "メルカリ + ヤフオクで「狙っている型番が希望価格レンジで新規出品された瞬間」を"
                "Discord 通知。在庫監視 (守り) と別系統の発掘 (攻め) タスク。"
            ),
        )

        with st.container(border=True):
            _render_summary_section()

        with st.container(border=True):
            _render_watch_list()

        with st.container(border=True):
            _render_add_form()

        with st.container(border=True):
            _render_legacy_import()
