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
import subprocess
import sys
from datetime import datetime, timezone, timedelta
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


def _build_search_url(site: str, keyword: str) -> str:
    """site + keyword から検索 URL を構築。"""
    kw = quote_plus(keyword.strip())
    if site == "mercari":
        return (
            f"https://jp.mercari.com/search?keyword={kw}"
            "&status=on_sale&sort=created_time&order=desc"
        )
    if site == "yahoo_auctions":
        return f"https://auctions.yahoo.co.jp/search/search?p={kw}&s1=new&o1=d"
    raise ValueError(f"unsupported site: {site}")


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

    # 簡易表示 (st.dataframe)
    rows = []
    for w in watches:
        pmin = w.get("price_min_jpy")
        pmax = w.get("price_max_jpy")
        if pmin is None and pmax is None:
            price_str = "(未設定 = 通知無効)"
        else:
            lo = f"¥{pmin:,}" if pmin is not None else "—"
            hi = f"¥{pmax:,}" if pmax is not None else "—"
            price_str = f"{lo} 〜 {hi}"
        rows.append({
            "id": w["id"],
            "site": w["site"],
            "keyword": w["keyword"],
            "price": price_str,
            "sentinel": "🛡️" if w.get("is_sentinel") else "",
            "active": "✅" if w["is_active"] else "❌",
            "last_crawl": _to_jst_str(w.get("last_crawled_at")),
            "last_error": (w.get("last_error") or "")[:60],
            "memo": (w.get("memo") or "")[:40],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

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

        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("💾 保存", key=f"kw_save_{sel_id}"):
                update_watch(
                    sel_id,
                    keyword=new_keyword.strip(),
                    price_min_jpy=None if pmin_unset else int(new_pmin),
                    price_max_jpy=None if pmax_unset else int(new_pmax),
                    memo=new_memo,
                    is_active=1 if new_active else 0,
                )
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
                st.dataframe(
                    [{
                        "detected_at": _to_jst_str(h["detected_at"]),
                        "title": (h.get("title") or "")[:50],
                        "price": f"¥{h['price_jpy']:,}" if h.get("price_jpy") else "—",
                        "in_range": "✅" if h["in_price_range"] else "—",
                        "discord": "📤" if h["discord_sent"] else "—",
                        "url": h["found_item_url"],
                    } for h in hits],
                    use_container_width=True,
                    hide_index=True,
                )


def _render_add_form() -> None:
    """3. 新規追加 form"""
    st.subheader("➕ 新規追加")
    with st.form("kw_add_form", clear_on_submit=True):
        c1, c2 = st.columns([1, 3])
        with c1:
            site = st.selectbox("サイト", ["mercari", "yahoo_auctions"],
                                 format_func=lambda x: "🛒 メルカリ" if x == "mercari" else "🔨 ヤフオク")
        with c2:
            keyword = st.text_input("キーワード", placeholder="例: Astell&Kern A&norma SR35")

        c3, c4, c5 = st.columns(3)
        with c3:
            pmin = st.number_input("下限 (¥、空欄=なし)", min_value=0, value=0, step=1000)
            pmin_unset = st.checkbox("下限なし", value=True)
        with c4:
            pmax = st.number_input("上限 (¥、空欄=なし)", min_value=0, value=0, step=1000)
            pmax_unset = st.checkbox("上限なし", value=False)
        with c5:
            st.caption("注: 両方なしだと通知無効 (履歴のみ)")

        memo = st.text_area("メモ (任意)", placeholder="採用後のアクション、注意点 等", height=80)

        submitted = st.form_submit_button("追加")
        if submitted:
            kw = keyword.strip()
            if not kw:
                st.error("キーワードは必須です")
                return
            try:
                url = _build_search_url(site, kw)
            except ValueError as e:
                st.error(str(e))
                return
            wid, new = add_watch(
                site=site,
                search_url=url,
                keyword=kw,
                price_min_jpy=None if pmin_unset else int(pmin),
                price_max_jpy=None if pmax_unset else int(pmax),
                memo=memo,
                source="manual",
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
    )
    rows = [r for r in exported if r["site"] in site_filter]
    st.caption(f"表示: {len(rows)} 件 (初回推奨: 50 件以下から開始)")

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
    st.markdown("## 🔔 キーワード新着監視 (W148)")
    st.caption(
        "メルカリ + ヤフオクで「狙っている型番が希望価格レンジで新規出品された瞬間」を Discord 通知。"
        "在庫監視 (守り) と別系統の発掘 (攻め) タスク。"
    )

    with st.container(border=True):
        _render_summary_section()

    with st.container(border=True):
        _render_watch_list()

    with st.container(border=True):
        _render_add_form()

    with st.container(border=True):
        _render_legacy_import()
