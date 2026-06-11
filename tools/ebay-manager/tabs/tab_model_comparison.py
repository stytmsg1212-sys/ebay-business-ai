#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""モデル比較 (Supplier A/B Test) タブ (W221 Tier2 抽出、2026-06-04)。

app.py L6106-6333 から body をそのまま移植。挙動不変 (K2 surgical)。
Opus 4.7 vs Sonnet 4.6 の supplier 評価 A/B test 結果を listing 別に並列比較。
"""
from __future__ import annotations

import streamlit as st


def render_model_comparison_tab() -> None:
    """Supplier 評価モデル比較 (A/B test 結果) タブ."""
    st.markdown("# モデル比較 (Supplier A/B Test)")
    st.caption(
        "Opus 4.7 vs Sonnet 4.6 で同じ scrape 結果を独立評価し、判定と精度を比較。"
        "past_judgments / knowledge は両モデルとも bypass (test_mode=True)。"
    )

    from monitor.database import get_conn as _get_conn

    with _get_conn() as _conn:
        _runs = _conn.execute(
            "SELECT run_id, MIN(created_at) AS started, COUNT(*) AS rows "
            "FROM supplier_ab_test_runs GROUP BY run_id ORDER BY started DESC"
        ).fetchall()

    if not _runs:
        st.info(
            "A/B test 結果なし。"
            "`python scripts/run_supplier_ab_test_2026_05_01.py` を実行してください。"
        )
    else:
        _run_options = [r["run_id"] for r in _runs]
        _selected_run = st.selectbox(
            "Run ID を選択",
            _run_options,
            format_func=lambda x: f"{x}  ({next((r['started'] for r in _runs if r['run_id']==x), '?')}, "
                                  f"{next((r['rows'] for r in _runs if r['run_id']==x), 0)} rows)",
        )

        # === Summary metrics ===
        with _get_conn() as _conn:
            _summary = _conn.execute(
                "SELECT model, COUNT(*) AS calls, "
                "  SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS cache_hits, "
                "  SUM(cost_usd) AS total_cost, AVG(cost_usd) AS avg_cost, "
                "  AVG(duration_ms) AS avg_dur, "
                "  AVG(match_score) AS avg_score "
                "FROM supplier_ab_test_runs WHERE run_id=? GROUP BY model",
                (_selected_run,),
            ).fetchall()
            _listing_count = _conn.execute(
                "SELECT COUNT(DISTINCT ebay_item_id) FROM supplier_ab_test_runs WHERE run_id=?",
                (_selected_run,),
            ).fetchone()[0]
            _cand_count = _conn.execute(
                "SELECT COUNT(DISTINCT ebay_item_id || '_' || candidate_index) "
                "FROM supplier_ab_test_runs WHERE run_id=?",
                (_selected_run,),
            ).fetchone()[0]

        st.markdown("## サマリ")
        st.caption(
            f"対象 listings: {_listing_count} 件 / 候補総数: {_cand_count} 件 / "
            f"評価合計: {sum(s['calls'] for s in _summary)} 件 (= 候補数 × 2 model)"
        )
        _summary_rows = []
        for _s in _summary:
            _model_label = (
                "Opus 4.7" if "opus" in _s["model"].lower()
                else "Sonnet 4.6" if "sonnet" in _s["model"].lower()
                else _s["model"]
            )
            _hit_rate = (_s["cache_hits"] / _s["calls"] * 100) if _s["calls"] else 0
            _summary_rows.append({
                "Model": _model_label,
                "calls": _s["calls"],
                "cache hit": f"{_s['cache_hits']}/{_s['calls']} ({_hit_rate:.0f}%)",
                "total cost": f"${_s['total_cost']:.4f}",
                "avg / call": f"${_s['avg_cost']:.5f}",
                "avg duration": f"{_s['avg_dur']/1000:.1f}s" if _s['avg_dur'] else "—",
                "avg score": f"{_s['avg_score']:.1f}" if _s['avg_score'] is not None else "—",
            })
        st.table(_summary_rows)

        # === 判定一致率 ===
        with _get_conn() as _conn:
            _agreement = _conn.execute(
                """
                SELECT
                  COUNT(*) AS total_pairs,
                  SUM(CASE WHEN ABS(opus_score - sonnet_score) <= 10 THEN 1 ELSE 0 END) AS within_10,
                  SUM(CASE WHEN ABS(opus_score - sonnet_score) <= 20 THEN 1 ELSE 0 END) AS within_20,
                  AVG(ABS(opus_score - sonnet_score)) AS avg_diff,
                  SUM(CASE
                    WHEN (opus_score >= 60) = (sonnet_score >= 60) THEN 1 ELSE 0
                  END) AS verdict_agree
                FROM (
                  SELECT
                    ebay_item_id, candidate_index,
                    MAX(CASE WHEN model='claude-opus-4-7' THEN match_score END) AS opus_score,
                    MAX(CASE WHEN model='claude-sonnet-4-6' THEN match_score END) AS sonnet_score
                  FROM supplier_ab_test_runs
                  WHERE run_id=?
                  GROUP BY ebay_item_id, candidate_index
                  HAVING opus_score IS NOT NULL AND sonnet_score IS NOT NULL
                )
                """,
                (_selected_run,),
            ).fetchone()

        st.markdown("## 判定一致統計")
        if _agreement and _agreement["total_pairs"]:
            _t = _agreement["total_pairs"]
            _v = _agreement["verdict_agree"] or 0
            _w10 = _agreement["within_10"] or 0
            _w20 = _agreement["within_20"] or 0
            _avg_d = _agreement["avg_diff"] or 0
            st.markdown(
                f"- 候補ペア総数: **{_t}**\n"
                f"- score 差 ±10 以内: **{_w10}/{_t} ({_w10/_t*100:.0f}%)**\n"
                f"- score 差 ±20 以内: **{_w20}/{_t} ({_w20/_t*100:.0f}%)**\n"
                f"- 平均 score 差: **{_avg_d:.1f}** 点\n"
                f"- 判定一致 (採用60+/不採用60-): **{_v}/{_t} ({_v/_t*100:.0f}%)**"
            )
        else:
            st.caption("候補ペアなし。")

        # === Per-listing 並列カード ===
        st.markdown("## 商品別 比較カード")
        with _get_conn() as _conn:
            _listings = _conn.execute(
                "SELECT DISTINCT ebay_item_id, ebay_title, ebay_sku "
                "FROM supplier_ab_test_runs WHERE run_id=? "
                "ORDER BY ebay_sku",
                (_selected_run,),
            ).fetchall()

        for _li in _listings:
            st.markdown("---")
            st.markdown(f"### {_li['ebay_title']}")
            st.caption(f"ItemID: `{_li['ebay_item_id']}` / SKU: `{_li['ebay_sku']}`")

            with _get_conn() as _conn:
                _cands = _conn.execute(
                    "SELECT candidate_index, candidate_title, candidate_url, "
                    "  candidate_price_jpy, candidate_platform, "
                    "  model, match_score, reasoning, alt_listing_possible, "
                    "  junk_likely_untested, alt_listing_note, "
                    "  cost_usd, cache_read_tokens, duration_ms, error "
                    "FROM supplier_ab_test_runs "
                    "WHERE run_id=? AND ebay_item_id=? "
                    "ORDER BY candidate_index, model",
                    (_selected_run, _li["ebay_item_id"]),
                ).fetchall()

            _by_cand: dict[int, dict[str, dict]] = {}
            for _c in _cands:
                _by_cand.setdefault(_c["candidate_index"], {})[_c["model"]] = dict(_c)

            if not _by_cand:
                st.caption("候補なし (scrape 0 件 or 全エラー)")
                continue

            for _idx in sorted(_by_cand.keys()):
                _opus = _by_cand[_idx].get("claude-opus-4-7")
                _sonnet = _by_cand[_idx].get("claude-sonnet-4-6")
                _cand_title = (_opus or _sonnet)["candidate_title"]
                _cand_url = (_opus or _sonnet)["candidate_url"]
                _cand_price = (_opus or _sonnet)["candidate_price_jpy"]
                _cand_plat = (_opus or _sonnet)["candidate_platform"]

                # diff 強調: score 差 > 20 = 不一致 marker
                _diff = abs((_opus["match_score"] if _opus else 0) - (_sonnet["match_score"] if _sonnet else 0))
                _verdict_diff = (
                    _opus and _sonnet and
                    ((_opus["match_score"] >= 60) != (_sonnet["match_score"] >= 60))
                )
                _diff_badge = ""
                if _verdict_diff:
                    _diff_badge = ' <span style="color:#ff6464;font-weight:600;font-size:11px;background:rgba(200,80,80,0.2);padding:2px 8px;border-radius:3px;">採用判定が不一致</span>'
                elif _diff > 20:
                    _diff_badge = f' <span style="color:#b8860b;font-weight:600;font-size:11px;background:rgba(180,150,40,0.18);padding:2px 8px;border-radius:3px;">score 差 {_diff} 点</span>'

                # _cand_price が None (= scraper で価格取得失敗) でも format crash しないよう
                # 明示 fallback. 2026-05-05 修正.
                _price_disp = f"¥{_cand_price:,}" if _cand_price is not None else "¥-"
                st.markdown(
                    f"**候補 #{_idx}** [{_cand_plat}]　"
                    f"<a href='{_cand_url}' target='_blank' style='color:#156a63;'>{_cand_title}</a>　"
                    f"<span style='color:#8d927f;'>{_price_disp}</span>"
                    f"{_diff_badge}",
                    unsafe_allow_html=True,
                )

                _col1, _col2 = st.columns(2)
                for _col, _row, _label, _color in [
                    (_col1, _opus,   "Opus 4.7",   "rgba(196,128,255,0.95)"),
                    (_col2, _sonnet, "Sonnet 4.6", "#156a63"),
                ]:
                    with _col:
                        if not _row:
                            st.caption(f"{_label}: 評価データなし")
                            continue
                        if _row.get("error"):
                            st.markdown(
                                f"<span style='color:{_color};font-weight:600;'>{_label}</span>: "
                                f"<span style='color:#a8341b;'>ERROR — {_row['error']}</span>",
                                unsafe_allow_html=True,
                            )
                            continue
                        _score = _row["match_score"]
                        _score_color = (
                            "#2e7d5b" if _score >= 80
                            else "#b8860b" if _score >= 60
                            else "rgba(255,128,128,0.9)"
                        )
                        _flags = []
                        if _row.get("alt_listing_possible"):
                            _flags.append("alt_listing")
                        if _row.get("junk_likely_untested"):
                            _flags.append("junk_untested")
                        _flag_html = (
                            f' <span style="color:#8d927f;font-size:10px;">[{",".join(_flags)}]</span>'
                            if _flags else ""
                        )
                        _cache_marker = "✓" if (_row.get("cache_read_tokens") or 0) > 0 else "—"
                        st.markdown(
                            f"<span style='color:{_color};font-weight:600;font-size:13px;'>{_label}</span>　"
                            f"<span style='color:{_score_color};font-size:18px;font-weight:700;'>{_score}</span>"
                            f"{_flag_html}　"
                            f"<span style='color:#8d927f;font-size:11px;'>"
                            f"${_row['cost_usd']:.5f} / {_row['duration_ms']/1000:.1f}s / cache {_cache_marker}"
                            f"</span>",
                            unsafe_allow_html=True,
                        )
                        st.caption(f"判定理由: {_row['reasoning']}")
                        if _row.get("alt_listing_note"):
                            st.caption(f"alt_listing_note: {_row['alt_listing_note']}")
