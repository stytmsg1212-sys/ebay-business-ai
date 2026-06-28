#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""リサーチ対戦アリーナ (W286) タブ — オーナー × AI ブラインド・リサーチ採点 UI.

データ層は monitor.research_duel_db (新規 DB 操作はここで作らない / K2 surgical)。
描画 + 採点/picks をデータ層に配線するだけ (夜間タスク起動・学習実行は本タブの責務外)。

設計準拠 (デザイン正本 = 唯一の真実源):
  - 確定モック: .company/engineering/docs/2026-06-27-research-duel-mockup-v5.html
    = MonoDeck W261 Neumorphic Cream (地 #ede7da / accent ティール #0e4f4b /
      Inter 本文 + JetBrains Mono 数値 / ニューモーフィズム raised・inset 影)。
  - **高忠実度方針 (2026-06-28 全面 rework)**: モックの `<style>` ブロック (:root トークン /
    box-shadow raised・inset / color / font / @keyframes / グラデーション) を **値ごと逐語移植**
    し、`.rd-scope` 配下にスコープ注入する。提示用 chrome (topbar / 条件ティール帯 /
    列ティールヘッダ / カード枠 / VS+ペンギン / バッジ / スコアボード / 差分ボード) は
    `st.markdown(unsafe_allow_html=True)` で **モックの HTML を逐語再現** (クラス名もモックと同名)。
    操作が要る部分 (商品名/URL/利益 input・採点スライダー・確定/完了/承認ボタン・バックナンバー)
    のみ Streamlit widget を「操作パネル」に配置し、CSS でモックの見た目に寄せる。
  - キャラ (MONOペンギン): .company/engineering/docs/mono-penguin-mascot.png を base64 で
    HTML に埋め込み (Streamlit は相対 path 画像を描画しにくいため data: URI 化)。
    モック .mascot の `@keyframes bob` (上下 bob) + drop-shadow を逐語移植して動かす。
  - 期間は「YYYY/MM/DD 〜 MM/DD」の絶対日付。snapshot_json の window_start/window_end を
    最優先で読み、無ければ task_research_duel.compute_duel_window(jst_date, pattern) で
    後方算出する (harvest = 表示 = 単一真実源、user が朝のブラインドで同期間を再現可能)。
  - 既存タブ規約踏襲: render 関数は (s: dict) シグネチャ、DB 層は関数内 lazy import。
  - SKU をキーにしない (sku-rules: listing 識別は ebay_item_id)。本タブは表示のみで
    pick の title_ja で呼称し SKU を一切扱わない。
  - Q0 silent skip 禁止: 採点 (score<60 で理由必須) の ValueError は握り潰さず
    st.error で必ず可視化。完了の status 遷移失敗も同様。
  - ブラインドゲート: 自分の品を確定 (user_done / round status) するまで AI 5品を非表示。
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from functools import lru_cache
from html import escape as _html_escape
from pathlib import Path
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

# session_state プレフィクス (UI 衝突回避、tab_research_brain の _SS 流儀)
_SS = "rduel_"

# キャラ画像 (デザイン正本と同フォルダの確定アセット)。
# __file__ = <repo>/tools/ebay-manager/tabs/tab_research_duel.py
#   parents[0]=tabs / [1]=ebay-manager / [2]=tools / [3]=<repo root> (.company はここ直下)。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_DIR = _REPO_ROOT / ".company" / "engineering" / "docs"
_MASCOT_PNG = _DOCS_DIR / "mono-penguin-mascot.png"

# round status (research_duel_db と同値、import 失敗時の表示用に文字列定数で持つ)
_ST_AI_PENDING = "ai_pending"
_ST_AI_DONE = "ai_done"
_ST_USER_DONE = "user_done"
_ST_COMPLETED = "completed"
_ST_INVALIDATED = "invalidated"

_STATUS_JA = {
    _ST_AI_PENDING: "AI リサーチ待ち",
    _ST_AI_DONE: "採点待ち (AI 完了)",
    _ST_USER_DONE: "採点済 (完了待ち)",
    _ST_COMPLETED: "完了",
    _ST_INVALIDATED: "無効化",
}

_PATTERN_JA = {"new": "🆕 新着", "echo": "📈 2年前 (エコー)"}

# 失点採点で理由必須となる閾値 (research_duel_db._REASON_REQUIRED_BELOW と同値)。
_REASON_REQUIRED_BELOW = 60


# ────────────────────────────────────────────────────────────────────────────
# 小物
# ────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def _img_data_uri(path_str: str) -> Optional[str]:
    """png を data:image/png;base64,... に変換 (キャッシュ)。無ければ None。"""
    p = Path(path_str)
    try:
        raw = p.read_bytes()
    except OSError as e:  # noqa: BLE001 — 画像欠落は致命ではない (fallback 表示)
        logger.warning("[research_duel] mascot 画像読込失敗 %s: %s", p, e)
        return None
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _esc(text) -> str:
    """HTML 埋め込み用エスケープ (title/ラベルの < & > " を無害化)。"""
    return _html_escape("" if text is None else str(text), quote=True)


def _fmt_date(s_val) -> str:
    if not s_val:
        return "—"
    return str(s_val)[:10]


def _fmt_window(rnd: dict) -> str:
    """「YYYY/MM/DD 〜 MM/DD」の絶対日付期間 (凍結対象 sold 窓)。

    優先 1: snapshot_json の window_start/window_end (harvest 時に凍結した実窓)。
    優先 2: compute_duel_window(jst_date, pattern) で後方算出 (旧 round / snapshot 欠落)。
    どちらも構造上同じ窓を返すため単一真実源 (harvest = 表示)。
    """
    start_iso = end_iso = None
    # 1) snapshot から
    snap = rnd.get("snapshot_json")
    if snap:
        try:
            import json as _json
            b = _json.loads(snap)
            start_iso = b.get("window_start")
            end_iso = b.get("window_end")
        except Exception:  # noqa: BLE001 — snapshot 壊れても算出 fallback に流す
            start_iso = end_iso = None
    # 2) 後方算出 fallback
    if not (start_iso and end_iso):
        try:
            from tasks.task_research_duel import compute_duel_window
            start_iso, end_iso = compute_duel_window(
                str(rnd.get("jst_date") or "")[:10], rnd.get("pattern") or "new"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[research_duel] 期間算出失敗 round=%s: %s",
                           rnd.get("round_id"), e)
            return "—"
    try:
        s = datetime.fromisoformat(start_iso).strftime("%Y/%m/%d")
        e = datetime.fromisoformat(end_iso).strftime("%m/%d")
        return f"{s} 〜 {e}"
    except Exception:  # noqa: BLE001
        return f"{start_iso} 〜 {end_iso}"


def _grade_of(avg: Optional[float]) -> str:
    """平均点 → 判定ランク (モック gradeOf と同基準: S/A/B/C/D)。"""
    if avg is None:
        return "—"
    if avg >= 90:
        return "S"
    if avg >= 75:
        return "A"
    if avg >= 60:
        return "B"
    if avg >= 40:
        return "C"
    return "D"


def _score_class(score: Optional[int]) -> str:
    """採点値 → モック .num2 の色クラス (g=ok / m=warn / z=err)。"""
    if score is None:
        return "g"
    if score == 0:
        return "z"
    if score < _REASON_REQUIRED_BELOW:
        return "m"
    return "g"


# ────────────────────────────────────────────────────────────────────────────
# CSS 注入 (モック v5 の <style> を逐語移植、.rd-scope 配下にスコープ)
# ────────────────────────────────────────────────────────────────────────────
def _inject_css() -> None:
    """対戦アリーナ専用 CSS。デザイン正本 v5 mockup の `<style>` を **値ごと逐語移植**。

    モックの :root トークン (--bg / --teal / --raised / --inset / --r 等) を `.rd-scope`
    のローカル変数として **モックのハードコード値そのまま** 定義する (全体テーマ --nm-*
    が将来ずれても忠実度が崩れないよう、値を固定で持つ)。クラス名・box-shadow・color・
    font-size/weight・@keyframes・グラデーションはモックと同一にする。
    Streamlit 既存 UI を壊さないよう全セレクタを `.rd-scope` 配下に限定。
    """
    st.markdown(
        """<style>
        /* ===== モック :root トークンを逐語移植 (値は不変)。
           de-scope: 旧 `.rd-scope` ローカル変数 → `:root` グローバル変数。
           Streamlit が chrome の st.markdown を .rd-scope と別 container に割るため、
           descendant scope `.foo` だと chrome に当たらない。よって全
           セレクタを distinctive class の global selector に de-scope し、変数を
           document 全体で解決可能にする (CSS の値・@keyframes は一切変更しない)。 ===== */
        :root{
            --bg:#ede7da; --bg-deep:#e4dcca; --surface:#f2ecdf; --surface-hi:#f9f5eb;
            --sh-d:rgba(166,150,121,0.50); --sh-l:rgba(255,255,255,0.90);
            --teal:#0e4f4b; --teal-hi:#156a63; --teal-deep:#0a3d3a; --teal-soft:rgba(14,79,75,0.10);
            --text:#2a2e2a; --text-2:#5f6557; --text-3:#8d927f;
            --ok:#2e7d5b; --warn:#b8860b; --err:#a8341b;
            --raised:5px 5px 12px var(--sh-d), -5px -5px 12px var(--sh-l);
            --raised-sm:3px 3px 7px var(--sh-d), -3px -3px 7px var(--sh-l);
            --inset:inset 3px 3px 7px var(--sh-d), inset -3px -3px 7px var(--sh-l);
            --r:16px; --r-sm:10px;
            --f-body:'Inter','Noto Sans JP','Hiragino Kaku Gothic ProN','Yu Gothic UI',sans-serif;
            --f-num:'JetBrains Mono','Consolas',monospace;
        }
        /* base (font/line-height/box-sizing/link) を chrome root + 子孫へ適用 */
        .topbar,.cond,.arena,.board,.ctrlhead,
        .topbar *,.cond *,.arena *,.board *,.ctrlhead *{box-sizing:border-box}
        .topbar,.cond,.arena,.board{font-family:var(--f-body);color:var(--text);line-height:1.55}
        .num{font-family:var(--f-num);font-variant-numeric:tabular-nums}
        .topbar a,.cond a,.arena a,.board a{color:var(--teal);text-decoration:none}

        /* ===== topbar (.topbar) ===== */
        .topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:2px 0 18px}
        .brand{display:flex;align-items:center;gap:13px}
        .logo{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;
            font-size:22px;color:#fff;background:var(--teal);box-shadow:var(--raised-sm)}
        .brand h1{font-size:21px;margin:0;font-weight:700;letter-spacing:.2px;color:var(--text)}
        .brand p{margin:2px 0 0;color:var(--text-2);font-size:12.5px;font-weight:500}
        .spacer{flex:1}
        .chip{display:inline-flex;align-items:center;gap:8px;background:var(--surface);
            border-radius:999px;padding:8px 15px;font-size:13px;font-weight:600;
            box-shadow:var(--raised-sm);color:var(--text-2)}
        .chip .dot{width:8px;height:8px;border-radius:50%;background:var(--teal)}
        .chip b{color:var(--teal);font-family:var(--f-num)}

        /* ===== 条件ヘッダー (.cond) — 濃ティール立体バンド (逐語) ===== */
        .cond{position:relative;overflow:hidden;border-radius:var(--r);padding:20px 22px;
            margin-bottom:20px;color:#f3efe6;
            background:linear-gradient(120deg,var(--teal-deep),var(--teal) 70%,var(--teal-hi));
            box-shadow:var(--raised)}
        .cond .row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:relative;z-index:1}
        .cond .daybadge{background:rgba(255,255,255,.14);border:1.5px solid rgba(255,255,255,.3);
            border-radius:14px;padding:9px 15px;text-align:center;min-width:88px}
        .cond .daybadge .k{font-size:10px;color:#f3efe6;letter-spacing:2px;font-weight:700;text-transform:uppercase}
        .cond .daybadge .v{font-size:24px;font-weight:700;line-height:1.1;font-family:var(--f-num)}
        .cond .pat{font-size:13px;font-weight:700;background:#f9f5eb;color:var(--teal-deep);
            border-radius:999px;padding:6px 15px}
        .cond .meta{display:flex;gap:24px;flex-wrap:wrap}
        .cond .meta .k{font-size:10px;color:#f3efe6;font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
        .cond .meta .v{font-size:15px;font-weight:600;margin-top:3px}
        .cond .catbtn{display:inline-flex;align-items:center;gap:6px;background:#f9f5eb;
            color:var(--teal-deep);border-radius:10px;padding:8px 13px;font-size:12.5px;font-weight:700}
        .cond .freeze{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.16);
            border:1.5px solid rgba(255,255,255,.4);border-radius:999px;padding:5px 12px;
            font-size:11px;font-weight:600;margin-left:6px}
        .cond .cycle{display:flex;gap:8px;margin-top:15px;align-items:center;position:relative;z-index:1;flex-wrap:wrap}
        .cond .cycle .seg{font-size:11px;opacity:.9;font-weight:600;margin-right:3px}
        .cond .cycle i{width:13px;height:13px;border-radius:50%;background:rgba(255,255,255,.28);display:inline-block}
        .cond .cycle i.on{background:#f9f5eb;box-shadow:0 0 0 4px rgba(255,255,255,.18)}
        .cond .cycle i.done{background:rgba(255,255,255,.6)}

        /* ===== アリーナ 3 列 (.arena) ===== */
        .arena{display:grid;grid-template-columns:1fr 170px 1fr;gap:16px;align-items:start}
        .col{background:var(--surface);border-radius:var(--r);box-shadow:var(--raised);overflow:hidden;position:relative}
        .col h2{margin:0;padding:15px 18px;font-size:15px;display:flex;align-items:center;gap:10px;color:#f3efe6;font-weight:700}
        .col.user h2{background:var(--teal)}
        .col.ai h2{background:var(--teal-deep)}
        .col h2 .who{font-size:20px}
        .col h2 .count{margin-left:auto;font-size:11px;font-weight:600;background:rgba(255,255,255,.18);padding:4px 11px;border-radius:999px}
        .cards{padding:14px}

        /* ===== カード (.card) — neumorphic raised (逐語) ===== */
        .card{background:var(--surface-hi);border-radius:14px;padding:14px;margin:12px 4px;
            box-shadow:var(--raised-sm);transition:.18s;position:relative}
        .card:hover{transform:translateY(-2px);box-shadow:var(--raised)}
        .card .rank{position:absolute;top:-9px;left:-9px;width:28px;height:28px;border-radius:50%;
            display:grid;place-items:center;font-weight:700;font-size:12px;color:#fff;
            font-family:var(--f-num);box-shadow:var(--raised-sm)}
        .user .card .rank{background:var(--teal)}
        .ai .card .rank{background:var(--teal-deep)}
        .card .title{font-weight:700;font-size:14px;line-height:1.4;margin:2px 0 9px;padding-left:22px}
        .links{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:8px;align-items:center}
        .link{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:600;
            border-radius:8px;padding:5px 9px;background:var(--bg-deep);box-shadow:var(--inset);color:var(--teal)}
        .profit{display:inline-flex;align-items:center;gap:5px;font-weight:700;font-size:12.5px;
            font-family:var(--f-num);background:var(--bg-deep);color:var(--warn);border-radius:8px;
            padding:4px 10px;box-shadow:var(--inset)}
        .flag{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:600;
            color:var(--warn);background:rgba(184,134,11,.12);border:1px solid rgba(184,134,11,.35);border-radius:7px;padding:3px 8px}
        .why{margin-top:9px;background:var(--bg-deep);border-radius:10px;padding:9px 11px;
            font-size:12.5px;color:var(--text-2);line-height:1.5;box-shadow:var(--inset)}
        .why b{color:var(--text)}
        .tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
        .tag{font-size:10.5px;font-weight:600;border-radius:999px;padding:3px 10px}
        .tag.cat{background:rgba(168,52,27,.12);color:var(--err);border:1px solid rgba(168,52,27,.3)}
        .tag.risk{background:rgba(184,134,11,.12);color:var(--warn);border:1px solid rgba(184,134,11,.3)}
        .tag.vero{background:var(--teal-soft);color:var(--teal);border:1px solid rgba(14,79,75,.25)}
        .emptyrow{text-align:center;color:var(--text-3);font-size:12px;padding:6px 0 4px;font-weight:600}

        /* ===== scorebox (.scorebox) — inset 凹み + 採点トラック (逐語) ===== */
        .scorebox{margin-top:11px;background:var(--bg-deep);border-radius:12px;padding:11px 12px;box-shadow:var(--inset)}
        .scorebox .top{display:flex;align-items:center;gap:8px;margin-bottom:8px}
        .scorebox .lab{font-size:10.5px;color:var(--text-3);font-weight:700;letter-spacing:1px;text-transform:uppercase}
        .scorebox .num2{margin-left:auto;font-weight:700;font-size:20px;font-family:var(--f-num)}
        .scorebox .num2.g{color:var(--ok)}
        .scorebox .num2.m{color:var(--warn)}
        .scorebox .num2.z{color:var(--err)}
        /* 採点トラック (静的表示: モック .slider のグラデ + inset を踏襲、実操作は下の widget) */
        .scoretrack{position:relative;width:100%;height:10px;border-radius:999px;
            background:linear-gradient(90deg,var(--ok),var(--warn),var(--err));box-shadow:var(--inset)}
        .scoretrack .knob{position:absolute;top:50%;width:20px;height:20px;border-radius:50%;
            background:var(--surface-hi);border:4px solid var(--teal);box-shadow:var(--raised-sm);
            transform:translate(-50%,-50%)}
        .card.ojama{box-shadow:var(--inset);background:var(--bg-deep)}
        .card.ojama .title{color:var(--text-3)}
        .ojama-badge{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;font-weight:700;
            color:#fff;background:var(--text-3);border-radius:999px;padding:3px 10px;margin-left:6px}

        /* ===== VS 列 (.vs) — ペンギン bob アニメ (逐語) ===== */
        .vs{display:flex;flex-direction:column;align-items:center;gap:10px;padding-top:4px;position:sticky;top:14px}
        .bubble{background:var(--surface-hi);border-radius:13px;padding:8px 12px;font-size:11.5px;
            font-weight:600;color:var(--teal-deep);box-shadow:var(--raised-sm);position:relative;
            text-align:center;line-height:1.4}
        .bubble::after{content:"";position:absolute;bottom:-7px;left:50%;transform:translateX(-50%);
            border:7px solid transparent;border-top-color:var(--surface-hi)}
        .mascot{width:158px;max-width:100%;filter:drop-shadow(5px 8px 10px var(--sh-d));
            animation:rdbob 2.6s ease-in-out infinite}
        .mascot-fallback{width:148px;height:148px;border-radius:var(--r);display:grid;place-items:center;
            text-align:center;font-size:12px;color:var(--text-3);font-weight:700;background:var(--surface);
            box-shadow:var(--inset);padding:10px}
        @keyframes rdbob{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
        @media (prefers-reduced-motion: reduce){.mascot{animation:none}}
        .vstag{font-family:var(--f-num);font-weight:700;font-size:18px;color:#fff;background:var(--teal);
            width:48px;height:48px;border-radius:50%;display:grid;place-items:center;
            box-shadow:var(--raised-sm),0 0 0 3px var(--surface);margin-top:-2px}
        .gate{background:var(--surface-hi);border-radius:14px;padding:12px;text-align:center;
            box-shadow:var(--raised-sm);width:100%}
        .gate .ico{font-size:22px}
        .gate p{margin:6px 0 4px;font-size:11px;color:var(--text-2);line-height:1.5;font-weight:500}

        /* AI 列ロック (ブラインド) — モック .lockmask の凹みマスク踏襲 */
        .lockmask{border-radius:14px;padding:34px 14px;text-align:center;background:var(--bg-deep);
            box-shadow:var(--inset);color:var(--teal-deep);font-weight:700;margin:12px 4px}
        .lockmask .ico{font-size:34px;display:block;margin-bottom:6px}
        .lockmask small{font-weight:500;color:var(--text-3)}

        /* ===== スコアボード (.board / .ring / .kpi / .diffbox / .roi) — 逐語 ===== */
        .board{display:grid;grid-template-columns:250px 1fr;gap:16px;margin-top:22px}
        .panel{background:var(--surface);border-radius:var(--r);box-shadow:var(--raised);padding:18px}
        .panel h3{margin:0 0 12px;font-size:13.5px;display:flex;align-items:center;gap:8px;font-weight:700;color:var(--text)}
        .ring{width:160px;height:160px;border-radius:50%;margin:6px auto 10px;position:relative;
            display:grid;place-items:center;
            background:conic-gradient(var(--teal) calc(var(--p,0)*1%), var(--bg-deep) 0);box-shadow:var(--raised-sm)}
        .ring::before{content:"";position:absolute;width:118px;height:118px;border-radius:50%;
            background:var(--surface);box-shadow:var(--inset)}
        .ring .n{position:relative;text-align:center}
        .ring .n b{font-size:32px;font-weight:700;font-family:var(--f-num);color:var(--text)}
        .ring .n b small{font-size:14px;color:var(--text-3)}
        .ring .n .gd{display:inline-block;margin-top:2px;font-size:12px;font-weight:700;color:#fff;
            background:var(--teal);border-radius:999px;padding:2px 14px}
        .kpis{display:flex;gap:10px;justify-content:center;margin-top:8px}
        .kpi{text-align:center;background:var(--bg-deep);border-radius:12px;padding:8px 12px;box-shadow:var(--inset)}
        .kpi b{display:block;font-size:17px;font-weight:700;font-family:var(--f-num)}
        .kpi span{font-size:10px;color:var(--text-3);font-weight:600}
        .diffgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
        .diffbox{border-radius:12px;padding:12px;background:var(--surface-hi);box-shadow:var(--raised-sm)}
        .diffbox .h{font-size:11.5px;font-weight:700;margin-bottom:8px}
        .diffbox.miss .h{color:var(--err)}
        .diffbox.extra .h{color:var(--teal-deep)}
        .diffbox ul{margin:0;padding:0}
        .diffbox li{font-size:12px;margin:5px 0;list-style:none;padding-left:14px;position:relative;line-height:1.45;color:var(--text)}
        .diffbox li::before{content:"\\25CF";position:absolute;left:0;color:var(--teal);font-size:8px;top:4px}
        .diffbox i{color:var(--text-3)}
        .roi{margin-top:16px}
        .roibar{display:flex;align-items:center;gap:10px;font-size:12px;font-weight:600;margin-bottom:9px}
        .roibar .lab{width:104px;color:var(--text-3)}
        .roibar .track{flex:1;height:18px;background:var(--bg-deep);border-radius:999px;overflow:hidden;box-shadow:var(--inset)}
        .roibar .fill{height:100%;border-radius:999px;display:flex;align-items:center;justify-content:flex-end;
            padding-right:8px;color:#fff;font-size:11px;font-family:var(--f-num)}
        .roibar.arena .fill{background:var(--teal-deep)}
        .roibar.real .fill{background:var(--ok)}
        .roi .note{font-size:11px;color:var(--text-2);margin-top:6px;line-height:1.5;
            background:var(--bg-deep);border-radius:10px;padding:9px 11px;box-shadow:var(--inset)}
        .roi .note b{color:var(--text)}

        /* レスポンシブ (モック media query 踏襲) */
        @media(max-width:880px){.arena{grid-template-columns:1fr}.board,.diffgrid{grid-template-columns:1fr}}

        /* ===== 操作パネル: Streamlit widget をモック寄せ (見た目だけ補正) ===== */
        .ctrlhead{display:flex;align-items:center;gap:8px;font-weight:700;font-size:13px;
            color:#f3efe6;background:var(--teal);border-radius:var(--r) var(--r) 0 0;padding:11px 16px;
            box-shadow:var(--raised-sm);margin:4px 0 0}
        .ctrlhead.ai{background:var(--teal-deep)}

        /* ===== 条件帯 / ティール背景文字色: global override 対策の統一ワイルドカード化 ===== */
        /* ティール背景コンテナ + 全子孫を明色強制 */
        .cond,.cond *,.col h2,.col h2 *,.ctrlhead,.ctrlhead *{color:#f3efe6 !important}
        /* teal 円ロゴ・VS・grade pill は白 (.topbar .logo はトップバーの ◉ ロゴ) */
        .topbar .logo,.vstag,.ring .n .gd{color:#fff !important}
        /* クリーム背景 pill はワイルドカードで明色化されないよう濃ティール文字に戻す */
        .cond .pat,.cond .pat *,.cond .catbtn,.cond .catbtn *{color:var(--teal-deep) !important}
        /* ロックマスク本文・錠アイコンは濃ティール (small は既存 var(--text-3) を維持) */
        .lockmask,.lockmask .ico{color:var(--teal-deep) !important}
        </style>""",
        unsafe_allow_html=True,
    )


# ────────────────────────────────────────────────────────────────────────────
# HTML 部品ビルダ (純文字列、Streamlit に依存しない / テスト容易)
# ────────────────────────────────────────────────────────────────────────────
def _build_topbar_html(rnd: dict, rounds_total: int) -> str:
    """ブランド + カテゴリ / 通算 R チップ (モック .topbar 逐語)。"""
    cat_label = rnd.get("category_label") or "全カテゴリ"
    return (
        '<div class="topbar">'
        '<div class="brand"><div class="logo">◉</div>'
        '<div><h1>リサーチ対戦アリーナ</h1>'
        '<p>あなた × MONOペンギン — 同じ条件でリサーチ。採点で AI が育つ</p></div></div>'
        '<div class="spacer"></div>'
        f'<div class="chip"><span class="dot"></span>{_esc(cat_label)}</div>'
        '<div class="chip"><span class="dot" style="background:var(--warn)"></span>'
        f'通算 <b>&nbsp;{int(rounds_total)}</b>&nbsp;R</div>'
        '</div>'
    )


def _build_condition_html(rnd: dict) -> str:
    """Day N / pattern / 絶対日付期間 / カテゴリ / 状態 の濃ティール条件帯 (モック .cond 逐語)。"""
    pat = (rnd.get("pattern") or "").lower()
    pat_ja = _PATTERN_JA.get(pat, pat or "—")
    cat_label = rnd.get("category_label") or (
        f"カテゴリ #{rnd.get('category_id')}" if rnd.get("category_id") else "全カテゴリ"
    )
    window = _fmt_window(rnd)
    status = rnd.get("status") or _ST_AI_PENDING
    status_ja = _STATUS_JA.get(status, status)

    # cycle index は snapshot から (無ければ Round 番号表示)
    day_of_cycle = None
    snap = rnd.get("snapshot_json")
    if snap:
        try:
            import json as _json
            day_of_cycle = _json.loads(snap).get("cycle_index")
        except Exception:  # noqa: BLE001
            day_of_cycle = None

    if isinstance(day_of_cycle, int):
        day_html = (
            f'<div class="v">{int(day_of_cycle) + 1}'
            f'<span style="font-size:13px;opacity:.7">/6</span></div>'
        )
        day_k = "Day"
    else:
        day_html = f'<div class="v">#{_esc(rnd.get("round_id", "?"))}</div>'
        day_k = "Round"

    # cycle ドット (snapshot に day があれば 6 個中の現在位置を点灯、モック .cycle 逐語)
    cycle_html = ""
    if isinstance(day_of_cycle, int):
        idx = day_of_cycle % 6
        new_dots = "".join(
            f'<i class="{"on" if i == idx else ("done" if i < idx else "")}"></i>'
            for i in range(3)
        )
        echo_dots = "".join(
            f'<i class="{"on" if (i + 3) == idx else ("done" if (i + 3) < idx else "")}"'
            f' style="opacity:.55"></i>'
            for i in range(3)
        )
        cycle_html = (
            '<div class="cycle"><span class="seg">6日サイクル</span>'
            f'{new_dots}'
            '<span class="seg" style="margin-left:8px">→ 2年前</span>'
            f'{echo_dots}'
            '<span style="margin-left:auto;font-size:11.5px;opacity:.9;font-weight:600">'
            'あなた=朝6:00 ブラインド ／ MONOペンギン=前夜 自動 ✅</span></div>'
        )

    return (
        '<div class="cond"><div class="row">'
        f'<div class="daybadge"><div class="k">{day_k}</div>{day_html}</div>'
        f'<span class="pat">{_esc(pat_ja)}</span>'
        '<div class="meta">'
        '<div><div class="k">固定期間（絶対・凍結）</div>'
        f'<div class="v num">{_esc(window)}</div></div>'
        '<div><div class="k">カテゴリ</div>'
        f'<div class="v">{_esc(cat_label)}</div></div>'
        '</div>'
        f'<span class="catbtn">▶ {_esc(status_ja)}</span>'
        '<span class="freeze">🧊 スナップショット凍結済</span>'
        f'</div>{cycle_html}</div>'
    )


def _build_user_card_html(p: dict) -> str:
    """ユーザー 1 品の表示カード (モック .col.user .card 逐語)。"""
    rank = p.get("rank") or "?"
    title = p.get("title_ja") or "(無題)"
    profit = p.get("profit_jpy_user")
    why = (p.get("why_md") or "").strip()
    ebay_url = p.get("ebay_url")
    supplier_url = p.get("supplier_url")

    links = []
    if ebay_url:
        links.append(f'<a class="link" href="{_esc(ebay_url)}" target="_blank" rel="noopener">eBay ↗</a>')
    else:
        links.append('<span class="link">eBay</span>')
    if supplier_url:
        links.append(f'<a class="link" href="{_esc(supplier_url)}" target="_blank" rel="noopener">仕入先 ↗</a>')
    else:
        links.append('<span class="link">仕入先</span>')
    if profit:
        links.append(f'<span class="profit">¥{int(profit):,}</span>')
    links_html = "".join(links)

    why_html = (
        f'<div class="why"><b>なぜ選んだか：</b>{_esc(why)}</div>' if why else ""
    )
    return (
        '<div class="card">'
        f'<span class="rank">{_esc(rank)}</span>'
        f'<div class="title">{_esc(title)}</div>'
        f'<div class="links">{links_html}</div>'
        f'{why_html}'
        '</div>'
    )


def _build_ai_card_html(p: dict) -> str:
    """AI 1 品の採点カード表示 HTML (モック .col.ai .card + .scorebox 逐語)。

    rank / title / リンク / 利益(あれば) / 選定理由 / 採点バッジ + 採点トラック を立体描画。
    実スライダー操作は下の操作パネル (Streamlit widget) で行う。score=0 は .ojama 化。
    """
    rank = p.get("rank") or "?"
    title = p.get("title_ja") or "(タイトル未取得)"
    cur_score = p.get("user_score")
    fb = (p.get("user_fb_md") or "").strip()

    # rc_id 由来の利益 / URL は本タブで複製しない (真値は rc_id 側 / Fugu A2)。
    # 表示は title + 採点に集中。why_md は AI pick には無いため、採点済なら失点理由を表示。
    badge_cls = _score_class(cur_score)
    badge_val = cur_score if cur_score is not None else "—"

    # 採点トラック (knob 位置 = 現在値%、未採点は中庸 70 を薄表示)
    knob_pct = int(cur_score) if cur_score is not None else 70
    knob_opacity = "1" if cur_score is not None else ".45"

    # links: ai pick は ebay_item_id があれば eBay リンク化 (仕入先は rc 側 / 省略)
    eid = p.get("ebay_item_id")
    if eid:
        link_html = (
            f'<a class="link" href="https://www.ebay.com/itm/{_esc(eid)}" '
            'target="_blank" rel="noopener">eBay ↗</a>'
        )
    else:
        link_html = '<span class="link">eBay</span>'

    # 失点理由 (採点済 < 60) を why 風に表示
    why_html = ""
    if cur_score is not None and int(cur_score) < _REASON_REQUIRED_BELOW and fb:
        why_html = f'<div class="why"><b>失点理由：</b>{_esc(fb)}</div>'
    elif fb:
        why_html = f'<div class="why"><b>メモ：</b>{_esc(fb)}</div>'

    ojama_cls = " ojama" if cur_score == 0 else ""
    ojama_badge = (
        ' <span class="ojama-badge">⬛ 0点・除外</span>' if cur_score == 0 else ""
    )

    return (
        f'<div class="card{ojama_cls}">'
        f'<span class="rank">{_esc(rank)}</span>'
        f'<div class="title">{_esc(title)}{ojama_badge}</div>'
        f'<div class="links">{link_html}</div>'
        f'{why_html}'
        '<div class="scorebox">'
        '<div class="top"><span class="lab">あなたの採点</span>'
        f'<span class="num2 {badge_cls}">{_esc(badge_val)}</span></div>'
        '<div class="scoretrack">'
        f'<span class="knob" style="left:{knob_pct}%;opacity:{knob_opacity}"></span></div>'
        '</div>'
        '</div>'
    )


def _build_vs_html(blind: bool) -> str:
    """speech バブル + MONOペンギン (bob アニメ) + VS + ゲート (モック .vs 逐語)。"""
    uri = _img_data_uri(str(_MASCOT_PNG))
    if uri:
        mascot_html = f'<img class="mascot" src="{uri}" alt="MONOペンギン">'
    else:
        mascot_html = (
            '<div class="mascot-fallback">🐧 MONOペンギン<br>'
            '<small>(mono-penguin-mascot.png)</small></div>'
        )
    if blind:
        gate_html = (
            '<div class="gate"><div class="ico">🙈</div>'
            '<p>ブラインド維持中。<br>自分の品を確定すると<br>'
            '結果が公開され採点できます。</p></div>'
        )
    else:
        gate_html = (
            '<div class="gate"><div class="ico">✍️</div>'
            '<p>公開！下の操作パネルで<br>0〜100 採点してね</p></div>'
        )
    return (
        '<div class="vs">'
        '<div class="bubble">5品さがして<br>きたよ！採点して〜</div>'
        f'{mascot_html}'
        '<div class="vstag">VS</div>'
        f'{gate_html}</div>'
    )


def _build_arena_html(
    rnd: dict, user_picks: list[dict], ai_picks: list[dict], blind: bool
) -> str:
    """3 列アリーナ全体 (左=あなた / 中=VS+ペンギン / 右=AI) を逐語 HTML で構築。"""
    # 左: ユーザー列
    n_user = len([p for p in user_picks if (p.get("title_ja") or "").strip()])
    user_cards = "".join(
        _build_user_card_html(p)
        for p in user_picks
        if (p.get("title_ja") or "").strip()
    )
    if not user_cards:
        user_cards = (
            '<div class="emptyrow">＋ まだ品が未入力です'
            '（下の操作パネルで 1〜5 品を入力）</div>'
        )
    elif n_user < 5:
        user_cards += (
            f'<div class="emptyrow">＋ 残り{5 - n_user}枠は任意（1〜5品でOK）</div>'
        )
    user_col = (
        '<section class="col user">'
        f'<h2><span class="who">🧑</span> あなたのリサーチ '
        f'<span class="count">{n_user} / 5 品</span></h2>'
        f'<div class="cards">{user_cards}</div></section>'
    )

    # 中: VS 列
    vs_col = _build_vs_html(blind)

    # 右: AI 列
    if blind:
        ai_count = "🔒 非公開"
        ai_body = (
            '<div class="lockmask"><span class="ico">🔒</span>'
            '確定するまで非公開<br><small>（アンカリング防止）</small></div>'
        )
    else:
        ai_count = f"{len(ai_picks)} 品・採点して！"
        if ai_picks:
            ai_body = "".join(_build_ai_card_html(p) for p in ai_picks)
        else:
            ai_body = (
                '<div class="emptyrow">AI の品がまだ保存されていません'
                '（夜間タスク待ち）</div>'
            )
    ai_col = (
        '<section class="col ai">'
        f'<h2><span class="who">🐧</span> MONOペンギンのリサーチ '
        f'<span class="count">{_esc(ai_count)}</span></h2>'
        f'<div class="cards">{ai_body}</div></section>'
    )

    return f'<div class="arena">{user_col}{vs_col}{ai_col}</div>'


def _build_scoreboard_html(scores: list[int]) -> str:
    """スコアボード (.panel .ring + .kpi) を逐語 HTML で構築。採点 0 件なら案内。"""
    if not scores:
        return (
            '<div class="panel" style="text-align:center">'
            '<h3 style="justify-content:center">🏅 今日のスコア</h3>'
            '<div class="emptyrow">まだ採点がありません。'
            'AI 各品を 0-100 で採点すると集計されます。</div></div>'
        )
    avg = round(sum(scores) / len(scores), 1)
    avg_int = int(round(avg))
    zero_rate = round(sum(1 for x in scores if x == 0) / len(scores) * 100)
    nonzero = sorted(x for x in scores if x > 0)
    median = nonzero[(len(nonzero) - 1) // 2] if nonzero else 0
    grade = _grade_of(avg)
    # avg 整数表示 (モック ring .n b は整数)
    avg_disp = str(avg_int)
    return (
        '<div class="panel" style="text-align:center">'
        '<h3 style="justify-content:center">🏅 今日のスコア</h3>'
        f'<div class="ring" style="--p:{avg_int}">'
        f'<div class="n"><b>{avg_disp}<small>/100</small></b>'
        f'<div class="gd">判定 {_esc(grade)}</div></div></div>'
        '<div class="kpis">'
        f'<div class="kpi"><b style="color:var(--err)">{zero_rate}%</b>'
        '<span>致命傷率(0点)</span></div>'
        f'<div class="kpi"><b style="color:var(--ok)">{median}</b>'
        '<span>中央値</span></div>'
        '</div></div>'
    )


def _build_diffboard_html(picks: dict) -> str:
    """差分ボード (.panel .diffgrid) を逐語 HTML で構築。"""
    ai = picks.get("ai", [])
    user = picks.get("user", [])
    scored = [p for p in ai if p.get("user_score") is not None]
    low = [p for p in scored if int(p.get("user_score") or 0) < _REASON_REQUIRED_BELOW]

    # miss: ユーザーが選んだ品 (ペンギンが逃した可能性)
    if user:
        miss_items = "".join(
            f'<li>{_esc((p.get("title_ja") or "(無題)"))}'
            + (
                f' — <i>{_esc((p.get("why_md") or "").strip()[:60])}</i>'
                if (p.get("why_md") or "").strip()
                else ""
            )
            + "</li>"
            for p in user
        )
    else:
        miss_items = '<li><i>（まだ自分の品を確定していません）</i></li>'

    # extra: ペンギンが選んで低得点/0点
    if low:
        extra_items = "".join(
            f'<li>{_esc((p.get("title_ja") or "(無題)"))} '
            f'({int(p.get("user_score") or 0)}) — '
            f'<i>{_esc((p.get("user_fb_md") or "").strip()[:60])}</i></li>'
            for p in low
        )
    elif scored:
        extra_items = '<li><i>低得点はありません（全品 60 以上）</i></li>'
    else:
        extra_items = '<li><i>（まだ AI を採点していません）</i></li>'

    return (
        '<div class="panel">'
        '<h3>🔍 差分ボード（学びの宝）</h3>'
        '<div class="diffgrid">'
        '<div class="diffbox miss"><div class="h">😣 あなたが選び ペンギンが逃した</div>'
        f'<ul>{miss_items}</ul></div>'
        '<div class="diffbox extra"><div class="h">🐧 ペンギンが選び 低得点/0点</div>'
        f'<ul>{extra_items}</ul></div>'
        '</div>'
        '<div class="roi"><h3 style="margin-top:16px">💎 アリーナ点 vs 実売却（閉ループ ROI）</h3>'
        '<div class="note">💡 採点は主観の代理指標。'
        '<b>本当の正解は「採用→出品→売れて利益が出た」事実</b>。'
        '乖離で採点インフレを検知。</div></div>'
        '</div>'
    )


# ────────────────────────────────────────────────────────────────────────────
# 操作パネル (Streamlit widget): ユーザー picks 入力
# ────────────────────────────────────────────────────────────────────────────
def _render_user_pick_form(rnd: dict, existing: list[dict], save_user_picks) -> None:
    """オーナー 1-5 品の入力フォーム (確定でブラインド解除)。

    確定 (save_user_picks) すると round status を user_done へ進め、AI 採点を解禁。
    既存 user picks があれば編集用に prefill。
    """
    round_id = rnd["round_id"]
    st.markdown(
        '<div class="ctrlhead">🧑 あなたのリサーチを入力 / 確定</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "今日のカテゴリ条件で見つけた品を 1〜5 品入力 (時間が無ければ少なくてよい)。"
        "「確定」を押すと MONOペンギン の品が公開され採点に進めます (アンカリング防止)。"
    )

    # 既存 picks を rank → dict にして prefill default 化
    _by_rank = {int(p.get("rank") or 0): p for p in existing}

    with st.form(key=f"{_SS}user_form_{round_id}"):
        for rank in range(1, 6):
            _ex = _by_rank.get(rank, {})
            with st.container(border=True):
                _c1, _c2 = st.columns([3, 1])
                with _c1:
                    st.text_input(
                        f"{rank}. 商品名",
                        value=_ex.get("title_ja") or "",
                        key=f"{_SS}u_title_{round_id}_{rank}",
                        placeholder="例: HIOKI 3280-10F AC クランプメーター",
                    )
                with _c2:
                    st.number_input(
                        "想定利益 (¥)",
                        value=int(_ex.get("profit_jpy_user") or 0),
                        step=100,
                        key=f"{_SS}u_profit_{round_id}_{rank}",
                    )
                _u1, _u2 = st.columns(2)
                with _u1:
                    st.text_input(
                        "eBay URL (任意)",
                        value=_ex.get("ebay_url") or "",
                        key=f"{_SS}u_ebay_{round_id}_{rank}",
                    )
                with _u2:
                    st.text_input(
                        "仕入先 URL (任意)",
                        value=_ex.get("supplier_url") or "",
                        key=f"{_SS}u_sup_{round_id}_{rank}",
                    )
                st.text_area(
                    "なぜこれを選んだか (学習の核)",
                    value=_ex.get("why_md") or "",
                    key=f"{_SS}u_why_{round_id}_{rank}",
                    height=70,
                    placeholder="例: 校正不要クランプは安定需要。状態B・ヤフオク¥3,000台で実用十分。",
                )
        _submit = st.form_submit_button(
            "自分の品を確定して採点へ進む", type="primary", use_container_width=True
        )

    if _submit:
        # フォーム入力を収集。title が空の rank は除外 (1-5 品、最低 1 品必須)。
        picks: list[dict] = []
        for rank in range(1, 6):
            _title = (st.session_state.get(f"{_SS}u_title_{round_id}_{rank}") or "").strip()
            if not _title:
                continue
            picks.append({
                "rank": rank,
                "title_ja": _title,
                "ebay_url": (st.session_state.get(f"{_SS}u_ebay_{round_id}_{rank}") or "").strip() or None,
                "supplier_url": (st.session_state.get(f"{_SS}u_sup_{round_id}_{rank}") or "").strip() or None,
                "profit_jpy_user": int(st.session_state.get(f"{_SS}u_profit_{round_id}_{rank}") or 0),
                "why_md": (st.session_state.get(f"{_SS}u_why_{round_id}_{rank}") or "").strip() or None,
            })
        if not picks:
            st.error("最低 1 品は商品名を入力してください。")
            return
        try:
            save_user_picks(round_id, picks)
            # status を user_done へ前進 (ai_done からのみ許容)。既に user_done/
            # completed なら no-op (再編集の保存)。遷移エラーは可視化 (Q0)。
            from monitor.research_duel_db import (
                update_round_status as _upd, can_transition as _can,
            )
            cur_status = rnd.get("status")
            if cur_status == _ST_AI_DONE and _can(_ST_AI_DONE, _ST_USER_DONE):
                _upd(round_id, _ST_USER_DONE)
            st.toast(f"{len(picks)} 品を確定しました。AI の品を公開します。", icon="✅")
            st.rerun()
        except ValueError as e:
            st.error(f"確定失敗: {e}")
        except Exception as e:  # noqa: BLE001
            logger.exception("save_user_picks failed round_id=%s", round_id)
            st.error(f"確定失敗 (詳細はログ): {e}")


# ────────────────────────────────────────────────────────────────────────────
# 操作パネル (Streamlit widget): AI picks 採点
# ────────────────────────────────────────────────────────────────────────────
def _render_ai_scoring_panel(
    rnd: dict, ai_picks: list[dict], score_ai_pick, blind: bool
) -> None:
    """AI 採点パネル (ブラインド時は案内のみ / 解禁時は各品スライダー)。"""
    round_id = rnd["round_id"]
    st.markdown(
        '<div class="ctrlhead ai">🐧 MONOペンギンの品を採点</div>',
        unsafe_allow_html=True,
    )
    if blind:
        st.info(
            "アンカリング防止のため、自分の品を確定するまで AI の品は採点できません。"
            "上の操作パネルで 1〜5 品入力し「確定」を押してください。"
        )
        return
    if not ai_picks:
        st.info("AI の品がまだ保存されていません (夜間タスクが AI リサーチを実行すると表示)。")
        return
    st.caption(
        f"各品を 0〜100 で採点。出品不可と判断したら 0 点。"
        f"{_REASON_REQUIRED_BELOW} 点未満は失点理由が必須 (学習 signal)。"
    )
    for p in ai_picks:
        _render_ai_score_widget(round_id, p, score_ai_pick)


def _render_ai_score_widget(round_id: int, p: dict, score_ai_pick) -> None:
    """AI 1 品の採点 widget (スライダー + 失点理由 + 保存)。表示カードは上の HTML 側。"""
    pick_id = p["id"]
    rank = p.get("rank") or "?"
    title = p.get("title_ja") or "(タイトル未取得)"
    cur_score = p.get("user_score")
    cur_fb = p.get("user_fb_md") or ""

    with st.container(border=True):
        st.markdown(f"**#{_esc(rank)}　{_esc(title)}**")
        # 0-100 スライダー (採点済なら現値 default、未採点は 70 中庸 default)
        _slider_key = f"{_SS}score_{round_id}_{pick_id}"
        _score = st.slider(
            "採点 (0-100)",
            min_value=0,
            max_value=100,
            value=int(cur_score) if cur_score is not None else 70,
            key=_slider_key,
            label_visibility="collapsed",
        )
        # 失点 (score<60) のみ理由欄を出す。0点(出品不可)も理由必須。
        _fb_key = f"{_SS}fb_{round_id}_{pick_id}"
        if _score < _REASON_REQUIRED_BELOW:
            _fb_val = st.text_area(
                f"失点理由 (必須 — {_REASON_REQUIRED_BELOW}点未満)",
                value=cur_fb,
                key=_fb_key,
                height=70,
                placeholder="例: カテゴリ外+薄利のため除外 / 返品リスク(旧式HDD) / VeRO 該当",
            )
        else:
            # 60点以上でも任意でメモを残せる (既存 fb があれば prefill)
            _fb_val = st.text_input(
                "メモ (任意)",
                value=cur_fb,
                key=_fb_key,
                placeholder="加点理由など (任意)",
            )

        if st.button(
            "採点を保存", key=f"{_SS}save_score_{round_id}_{pick_id}", type="primary"
        ):
            try:
                ok = score_ai_pick(
                    pick_id,
                    user_score=int(_score),
                    user_fb_md=(_fb_val.strip() or None),
                )
                if ok:
                    st.toast(f"#{rank} を {_score} 点で採点しました。", icon="✅")
                    st.rerun()
                else:
                    st.error(f"採点対象が見つかりません (pick_id={pick_id})。")
            except ValueError as e:
                # score<60 で理由未入力時など (Q0: 握り潰さず可視化)
                st.error(f"採点できません: {e}")
            except Exception as e:  # noqa: BLE001
                logger.exception("score_ai_pick failed pick_id=%s", pick_id)
                st.error(f"採点保存失敗 (詳細はログ): {e}")


# ────────────────────────────────────────────────────────────────────────────
# 完了ボタン (学習トリガー)
# ────────────────────────────────────────────────────────────────────────────
def _render_complete_button(rnd: dict, ai_picks: list[dict]) -> None:
    """完了ボタン: status→completed + 集計保存 + 学習トリガー (別モジュール)。"""
    round_id = rnd["round_id"]
    status = rnd.get("status")
    st.markdown("---")

    scored_n = sum(1 for p in ai_picks if p.get("user_score") is not None)
    total_n = len(ai_picks)

    if status == _ST_COMPLETED:
        st.success("この対戦は完了済みです (深層学習トリガー実行済)。")
        return

    if status != _ST_USER_DONE:
        st.info("自分の品を確定すると「今日の対戦を完了する」ボタンが有効になります。")
        return

    if scored_n < total_n:
        st.caption(
            f"採点進捗: {scored_n}/{total_n} 品。全品採点すると判定が安定します "
            "(未採点でも完了は可能)。"
        )

    if st.button(
        "📚 今日の対戦を完了する (MONOペンギンが学習)",
        key=f"{_SS}complete_{round_id}",
        type="primary",
        use_container_width=True,
    ):
        # 1) 集計をデータ層で保存 (平均/致命傷率/中央値)。status はここで進めない (HIGH-1 fix)。
        try:
            from monitor.research_duel_db import compute_and_save_round_scores
            agg = compute_and_save_round_scores(round_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("compute_and_save_round_scores failed round_id=%s", round_id)
            st.error(f"集計処理失敗 (詳細はログ): {e}")
            return

        # 2) 学習トリガー。run_completion_learning が user_done → completed 遷移を所有する。
        #    ImportError (モジュール不在) 時のみ tab が completed へ前進する (fallback)。
        try:
            from monitor.research_duel_learning import run_completion_learning
            with st.status("MONOペンギンが深層学習中...", expanded=True) as _ls:
                _res = run_completion_learning(round_id)
                _ls.update(label="学習完了", state="complete")
            if not (isinstance(_res, dict) and _res.get("completed")):
                # Opus 失敗: round は user_done のまま (再実行可能)。偽装成功にしない (Q0)。
                reason = _res.get("reason", "不明なエラー") if isinstance(_res, dict) else "不明なエラー"
                st.warning(
                    f"Opus 学習に失敗しました (再実行可能)。採点・集計は保存済みです。\n理由: {reason}"
                )
                return
            st.success(
                f"対戦を完了しました。平均 {agg.get('avg')}/100 "
                f"(致命傷率 {agg.get('zero_rate')} / 中央値 {agg.get('median')})。"
            )
            if isinstance(_res, dict) and _res.get("summary_md"):
                st.markdown(_res["summary_md"])
        except ImportError:
            # 学習モジュール不在 = fallback: tab が completed へ前進する (HIGH-1: 唯一の例外)。
            try:
                from monitor.research_duel_db import update_round_status
                update_round_status(round_id, _ST_COMPLETED)
            except ValueError as e:
                st.error(f"完了処理失敗: {e}")
                return
            st.success(
                f"対戦を完了しました (学習モジュール準備中)。平均 {agg.get('avg')}/100 "
                f"(致命傷率 {agg.get('zero_rate')} / 中央値 {agg.get('median')})。\n"
                "採点と集計は保存済みです。"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("run_completion_learning failed round_id=%s", round_id)
            st.warning(f"学習トリガーは失敗しましたが採点・集計は保存済みです: {e}")
            return
        st.rerun()


# ────────────────────────────────────────────────────────────────────────────
# バックナンバー (round 選択)
# ────────────────────────────────────────────────────────────────────────────
def _select_round(list_rounds, get_round) -> Optional[tuple[dict, int]]:
    """バックナンバーから対戦 round を選択 (なければ None)。

    返り値: (選択 round dict, 総 round 数)。既定は最新 round。
    """
    try:
        rounds = list_rounds(limit=60)
    except Exception as e:  # noqa: BLE001
        st.error(f"対戦一覧の取得に失敗しました: {e}")
        return None

    if not rounds:
        st.info(
            "まだ対戦ラウンドがありません。"
            "夜間タスク (AI リサーチ) が round を作成すると、ここで採点できます。"
        )
        return None

    total = len(rounds)

    # ラベル: 日付 / pattern / カテゴリ / 状態
    def _label(r: dict) -> str:
        _pat = _PATTERN_JA.get((r.get("pattern") or "").lower(), r.get("pattern") or "?")
        _cat = r.get("category_label") or (
            f"#{r.get('category_id')}" if r.get("category_id") else "全"
        )
        _stj = _STATUS_JA.get(r.get("status"), r.get("status") or "")
        return f"#{r.get('round_id')} {_fmt_date(r.get('jst_date'))} {_pat} / {_cat} [{_stj}]"

    _id_to_round = {int(r["round_id"]): r for r in rounds}
    _ids = list(_id_to_round.keys())

    _cols = st.columns([4, 1])
    with _cols[0]:
        _sel_id = st.selectbox(
            "🗓️ 対戦ラウンド (バックナンバー)",
            options=_ids,
            format_func=lambda rid: _label(_id_to_round[rid]),
            index=0,
            key=f"{_SS}round_select",
        )
    with _cols[1]:
        st.write("")
        if st.button("再読込", key=f"{_SS}reload"):
            st.rerun()

    # 選択 round の最新状態を get_round で取り直す (一覧は古い可能性)
    try:
        fresh = get_round(int(_sel_id))
    except Exception as e:  # noqa: BLE001
        st.error(f"対戦の取得に失敗しました: {e}")
        return (_id_to_round.get(int(_sel_id)), total)
    return (fresh or _id_to_round.get(int(_sel_id)), total)


# ────────────────────────────────────────────────────────────────────────────
# メインエントリポイント
# ────────────────────────────────────────────────────────────────────────────
def render_research_duel_tab(s: dict) -> None:
    """リサーチ対戦アリーナ タブ本体. app.py の dispatch から呼ばれる.

    引数 s = settings dict (既存タブ規約に合わせて受け取るが、本タブは表示のみで
    為替レート等の設定値には現状依存しない / K1 simplicity)。

    描画方針 (2026-06-28 高忠実度 rework):
      1. モック CSS を .rd-scope に逐語注入。
      2. 提示用 chrome (topbar / 条件帯 / アリーナ 3 列 / スコアボード / 差分ボード) を
         逐語 HTML で 1 ブロック描画 (動的データ差し込み)。
      3. その下に「操作パネル」(Streamlit widget): ユーザー入力 / AI 採点 / 完了。
    """
    # DB 層は関数内 lazy import (tab_supplier_candidates 流儀)。
    try:
        from monitor.research_duel_db import (
            list_rounds,
            get_round,
            get_round_picks,
            save_user_picks,
            score_ai_pick,
        )
    except Exception as e:  # noqa: BLE001
        st.error(f"リサーチ対戦アリーナ データ層の読込に失敗しました: {e}")
        logger.exception("research_duel_db import failed")
        return

    _inject_css()
    # neumorphic scope を開く (本タブの custom HTML を .rd-scope 配下に置く)
    st.markdown('<div class="rd-scope">', unsafe_allow_html=True)

    # ── round 選択 (バックナンバー) ──
    _sel = _select_round(list_rounds, get_round)
    if not _sel:
        st.markdown("</div>", unsafe_allow_html=True)
        return
    rnd, rounds_total = _sel
    if not rnd:
        st.markdown("</div>", unsafe_allow_html=True)
        return

    round_id = int(rnd["round_id"])
    status = rnd.get("status") or _ST_AI_PENDING

    # ── picks 取得 ──
    try:
        picks = get_round_picks(round_id)
    except Exception as e:  # noqa: BLE001
        st.error(f"picks の取得に失敗しました: {e}")
        logger.exception("get_round_picks failed round_id=%s", round_id)
        st.markdown("</div>", unsafe_allow_html=True)
        return
    ai_picks = picks.get("ai", [])
    user_picks = picks.get("user", [])

    # ブラインドゲート: 自分の品を確定 (user_done 以降) するまで AI 採点を隠す。
    # ai_pending = AI リサーチ未了なので、そもそも採点対象が無い。
    _blind = status in (_ST_AI_PENDING, _ST_AI_DONE)

    if status == _ST_AI_PENDING:
        st.warning(
            "MONOペンギン (AI) のリサーチがまだ完了していません "
            "(夜間タスク待ち)。先に自分の品を入力しておけます。"
        )

    # ── 提示用 chrome を逐語 HTML で 1 ブロック描画 ──
    _chrome = (
        _build_topbar_html(rnd, rounds_total)
        + _build_condition_html(rnd)
        + _build_arena_html(rnd, user_picks, ai_picks, _blind)
    )
    if not _blind:
        _scores = [
            int(p["user_score"]) for p in ai_picks if p.get("user_score") is not None
        ]
        _chrome += (
            '<div class="board">'
            + _build_scoreboard_html(_scores)
            + _build_diffboard_html(picks)
            + '</div>'
        )
    st.markdown(_chrome, unsafe_allow_html=True)

    # ── 操作パネル (Streamlit widget) ──
    st.markdown("---")
    st.markdown(
        '<div style="font-weight:700;font-size:14px;color:var(--text);margin:4px 0 6px">'
        '⚙️ 操作パネル</div>',
        unsafe_allow_html=True,
    )
    _col_user, _col_ai = st.columns([1, 1])
    with _col_user:
        _render_user_pick_form(rnd, user_picks, save_user_picks)
    with _col_ai:
        _render_ai_scoring_panel(rnd, ai_picks, score_ai_pick, _blind)

    # 採点解禁後のみ完了ボタン
    if not _blind:
        _render_complete_button(rnd, ai_picks)

    st.markdown("</div>", unsafe_allow_html=True)
