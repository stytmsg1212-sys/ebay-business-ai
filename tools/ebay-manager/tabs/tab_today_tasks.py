"""本日の作業タブ (W292 / 2026-06-27) — UI 層 (段2).

設計書: .company/engineering/docs/2026-06-27-today-tasks-tab-design.md
モック:  .company/engineering/docs/2026-06-27-today-tasks-tab-mockup-final.html

データ層 (daily_task_db.py / database.py) は段1で実装済み。本ファイルはUI描画のみ。
listing 識別は ebay_item_id (sku-rules.md: SKU を一意キーにしない)。

デザイン再現方針 (2026-06-27 user「全く一緒にしてください」/ tab_research_duel と同流儀):
  - 確定モック (デザイン正本) の neumorphic box-shadow (raised / raised-sm / inset) /
    font / color / layout を `.tt-scope` ローカルトークンに逐語移植し、
    全体テーマ ui_themes.apply_neumorph_cream_theme の --nm-* に橋渡しする。
  - 立体カードは Streamlit ネイティブ widget (checkbox / button) を内包する必要があるため、
    `st.container(border=True)` (neumorph テーマで raised-sm の cream カード化) を土台にし、
    その内側に columns + st.markdown(unsafe_allow_html=True) で title/links/badges を描く。
  - user の不満点 (2026-06-27) を反映:
      1. 2部バー = ダークティール1枚帯 → クリーム neumorphic 立体カード2枚を横並び。
         🌅早朝=リサーチ導線 (ボタン3つ=リサーチ対戦/今日の発掘/ライバル調査)、
         🌙夜間=初期登録ゴール (X/10 大表示 + pips + chip)。
      2. 進捗リング = teal conic-gradient + neumorphic を cream 立体カード内に。
      3. チェックリスト = 各行を立体カード化 + 完了は打消線/グレーアウト + 完了区切り帯。
      4. ストリーク chip = 🔥 N 日連続 / 最高 M の数字付き。

機能配線 (壊さない / K2):
  - チェック = _on_change_chk callback + render 前 st.session_state[chk_key]=is_done
    (stale clobber 防止)。set_initial_registered + bump_db_version。
  - jump (登録画面へ) = pm_focus_eid + _w217a_cat_view("★ 毎日") + _w134_sel("商品管理") + rerun。
  - リサーチ jump = _w134_sel(ページ名) + _w217a_cat_view("⚲ リサーチ") + rerun
    (research タブは「⚲ リサーチ」グループ所属。group key も設定して view-sync を確実化)。
  - データ層 daily_task_db / _missing_badges import は不変。listing_gone 除外集計も保持。
"""
from __future__ import annotations

import datetime

import streamlit as st

# session_state プレフィクス — 他タブとの衝突回避
_SS = "today_"

# eBay item URL のひな形 (notifier.py / ebay_image_fetcher.py と同一)。
_EBAY_ITEM_URL = "https://www.ebay.com/itm/{eid}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 小物
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _esc(text) -> str:
    """HTML 埋め込み用の最小エスケープ (title / ラベルの < & > " を無害化)."""
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _md(html: str) -> None:
    """st.markdown(unsafe_allow_html=True) の共通 helper。

    Streamlit のマークダウンレンダラは行頭 4+ スペースをコードブロックと解釈する。
    HTML 文字列の各行を lstrip してインデントを除去してから渡すことで、
    三連クォート f-string 等で自然に入るインデントが PRE 化するのを防ぐ。
    """
    st.markdown(
        "\n".join(line.lstrip() for line in html.splitlines()),
        unsafe_allow_html=True,
    )


def _supplier_url(sku) -> str:
    """SKU → 仕入先 URL (無在庫 ebay**_***** のみ。失敗時は空文字)。

    sku-rules.md 公認の用途 2 (無在庫 SKU 変換 → 仕入先候補 URL)。
    """
    if not sku:
        return ""
    try:
        from monitor.database import build_source_url
        return build_source_url(str(sku)) or ""
    except Exception:  # noqa: BLE001 — URL 導出失敗は致命でない (リンク非表示で続行)
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CSS (確定モックの値を .tt-scope ローカルトークンに逐語移植)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _inject_css() -> None:
    """本タブ専用 custom CSS (確定モックの box-shadow / font / color / layout を移植)。

    色・影は全体テーマ (--nm-*) を参照しつつ、モックと同じ short 名で .tt-scope に
    再定義して逐語移植する (tab_research_duel の .rd-scope 流儀)。
    """
    st.markdown(
        """<style>
        /* ===== ローカル別名: mockup の :root トークン名 → 全体テーマ --nm-* に橋渡し。
           de-scope: 旧 `.tt-scope` ローカル変数 → `:root` グローバル変数。
           Streamlit が chrome の st.markdown を .tt-scope と別 container に割るため、
           descendant scope `.foo` だと chrome に当たらず var() も解決できない。よって
           変数を document 全体で解決可能にし、全セレクタを distinctive class の
           global selector に de-scope する (値は一切変更しない)。 ===== */
        :root{
            --bg:var(--nm-bg,#ede7da); --bg-deep:var(--nm-bg-deep,#e4dcca);
            --surface:var(--nm-surface,#f2ecdf); --surface-hi:var(--nm-surface-hi,#f9f5eb);
            --sh-d:var(--nm-shadow-d,rgba(166,150,121,0.50));
            --sh-l:var(--nm-shadow-l,rgba(255,255,255,0.90));
            --teal:var(--nm-teal,#0e4f4b); --teal-hi:var(--nm-teal-hi,#156a63);
            --teal-deep:var(--nm-teal-deep,#0a3d3a); --teal-soft:var(--nm-teal-soft,rgba(14,79,75,0.10));
            --text:var(--nm-text,#2a2e2a); --text-2:var(--nm-text-2,#5f6557); --text-3:var(--nm-text-3,#8d927f);
            --ok:var(--nm-ok,#2e7d5b); --warn:var(--nm-warn,#b8860b); --err:var(--nm-err,#a8341b);
            --raised:5px 5px 12px var(--sh-d), -5px -5px 12px var(--sh-l);
            --raised-sm:3px 3px 7px var(--sh-d), -3px -3px 7px var(--sh-l);
            --inset:inset 3px 3px 7px var(--sh-d), inset -3px -3px 7px var(--sh-l);
            --r:16px; --r-sm:10px;
            --f-body:var(--f-body,'Inter','Noto Sans JP',sans-serif);
            --f-num:var(--f-num,'JetBrains Mono','Consolas',monospace);
        }
        .tt-topbar,.tt-cond,.tt-panel,.tt-listhead,.tt-doneband,.tt-cele,
        .tt-topbar *,.tt-cond *,.tt-panel *,.tt-listhead *,.tt-doneband *,.tt-cele *{box-sizing:border-box}
        .num{font-family:var(--f-num);font-variant-numeric:tabular-nums}

        /* ===== topbar ===== */
        .tt-topbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:2px 0 18px}
        .tt-brand{display:flex;align-items:center;gap:13px}
        .tt-logo{width:46px;height:46px;border-radius:12px;display:grid;place-items:center;
            font-size:22px;color:#fff;background:var(--teal);box-shadow:var(--raised-sm)}
        .tt-brand h1{font-size:21px;margin:0;font-weight:700;letter-spacing:.2px;color:var(--text)}
        .tt-brand p{margin:2px 0 0;color:var(--text-2);font-size:12.5px;font-weight:500}
        .tt-spacer{flex:1}
        .tt-chip{display:inline-flex;align-items:center;gap:8px;background:var(--surface);
            border-radius:999px;padding:8px 15px;font-size:13px;font-weight:600;
            box-shadow:var(--raised-sm);color:var(--text-2)}
        .tt-chip .dot{width:8px;height:8px;border-radius:50%;background:var(--teal)}
        .tt-chip b{color:var(--teal);font-family:var(--f-num)}
        .tt-chip.streak{color:var(--warn)}
        .tt-chip.streak .dot{background:var(--warn)}
        .tt-chip.streak b{color:var(--warn)}
        .tt-chip.date b{color:var(--text-2)}
        .tt-chip.summary{color:var(--teal)}
        .tt-chip.summary .dot{background:var(--teal)}
        .tt-chip.summary b{color:var(--teal)}

        /* ===== 2部コンディションバー = ダークティール gradient 帯 (mockup .cond 逐語) ===== */
        .tt-cond{position:relative;overflow:hidden;border-radius:var(--r);margin-bottom:18px;
            color:#f3efe6;background:linear-gradient(120deg,var(--teal-deep),var(--teal) 70%,var(--teal-hi));
            box-shadow:var(--raised)}
        .tt-cond-inner{display:grid;grid-template-columns:1fr 1px 1.2fr;gap:0;align-items:stretch;
            position:relative;z-index:1}
        .tt-part{padding:20px 24px}
        .tt-cond-vbar{width:1px;background:rgba(255,255,255,.22);align-self:stretch}
        .tt-part-label{font-size:10px;color:#f3efe6;font-weight:700;letter-spacing:1.5px;
            text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px}
        .tt-part-lead{font-size:16px;font-weight:700;margin-bottom:12px;line-height:1.3;color:#f3efe6}
        .tt-part-sub{font-size:11.5px;color:#f3efe6;opacity:.95;font-weight:500;margin-top:4px;line-height:1.5}

        /* リサーチ部のジャンプボタン (mockup .gobtns / .gobtn 逐語) */
        .tt-gobtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:2px}
        .tt-gobtn{display:inline-flex;align-items:center;gap:6px;background:#f9f5eb;
            color:var(--teal-deep);border-radius:10px;padding:9px 14px;font-size:12.5px;
            font-weight:700;box-shadow:0 2px 0 rgba(0,0,0,.08);cursor:pointer;border:0;
            transition:.15s;text-decoration:none}
        .tt-gobtn:hover{transform:translateY(-1px);box-shadow:0 3px 0 rgba(0,0,0,.1)}

        /* 夜間部のゴール表示 (mockup .goal-row / .goal-big / .pips / .nowbadge 逐語) */
        .tt-goal-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:10px}
        .tt-goal-big{font-size:32px;font-weight:700;font-family:var(--f-num);line-height:1}
        .tt-goal-big small{font-size:15px;color:#f3efe6;opacity:.92}
        .tt-nowbadge{font-size:11px;font-weight:700;background:rgba(255,255,255,.16);
            border:1.5px solid rgba(255,255,255,.34);border-radius:999px;padding:5px 12px;
            white-space:nowrap}
        .tt-goal-pips-row{display:flex;align-items:center;gap:12px}
        .tt-pips{display:flex;gap:5px;flex-wrap:wrap}
        .tt-pips i{width:13px;height:13px;border-radius:50%;background:rgba(255,255,255,.26);
            display:inline-block}
        .tt-pips i.done{background:#f9f5eb;box-shadow:0 0 0 2.5px rgba(255,255,255,.18)}
        .tt-pips-label{font-size:11px;color:#f3efe6;opacity:.95;font-weight:600;font-family:var(--f-num)}

        @media(max-width:780px){
            .tt-cond-inner{grid-template-columns:1fr}
            .tt-cond-vbar{display:none}
            .tt-part{padding:16px 18px}
        }

        /* ===== 進捗リング パネル (cream 立体カード + teal conic-gradient)
           タブ密度化リファクタ B1 (2026-07-04): リング縮小 + .tt-hint はタイトル
           tooltip 化 (常時表示テキストの行を消す、値は下の h3 title= に移動)。===== */
        .tt-panel{background:var(--surface);border-radius:var(--r);box-shadow:var(--raised);padding:14px}
        .tt-panel h3{margin:0 0 10px;font-size:12px;display:flex;align-items:center;gap:6px;
            font-weight:700;justify-content:center;color:var(--text)}
        .tt-ringwrap{text-align:center}
        .tt-ring{width:128px;height:128px;border-radius:50%;margin:4px auto 10px;position:relative;
            display:grid;place-items:center;box-shadow:var(--raised-sm)}
        .tt-ring::before{content:"";position:absolute;width:92px;height:92px;border-radius:50%;
            background:var(--surface);box-shadow:var(--inset)}
        .tt-ring .n{position:relative;text-align:center;z-index:1}
        .tt-ring .n b{font-size:26px;font-weight:700;font-family:var(--f-num);color:var(--text)}
        .tt-ring .n b small{font-size:12px;color:var(--text-3)}
        .tt-ring .n .gd{display:inline-block;margin-top:2px;font-size:11px;font-weight:700;color:#fff;
            border-radius:999px;padding:1px 11px}
        .tt-kpis{display:flex;gap:8px;justify-content:center;margin-top:8px}
        .tt-kpi{text-align:center;background:var(--bg-deep);border-radius:10px;padding:6px 10px;
            box-shadow:var(--inset)}
        .tt-kpi b{display:block;font-size:15px;font-weight:700;font-family:var(--f-num)}
        .tt-kpi span{font-size:10px;color:var(--text-3);font-weight:600}

        /* ===== チェックリスト 見出し ===== */
        .tt-listhead{display:flex;align-items:center;gap:10px;margin:0 0 8px;flex-wrap:wrap}
        .tt-listhead h2{margin:0;font-size:14px;font-weight:700;color:var(--text)}
        .tt-listhead .sub{font-size:11px;color:var(--text-2);font-weight:500}
        .tt-listhead .count{margin-left:auto;font-size:11px;font-weight:700;background:var(--teal-soft);
            color:var(--teal);border-radius:999px;padding:4px 10px}
        .tt-doneband{font-size:11px;font-weight:600;color:var(--text-3);text-align:center;
            margin:6px 0 8px;letter-spacing:.5px}

        /* ===== タスク 1 行圧縮 (title + links + metrics + badges を単一 flex 行に) ===== */
        .tt-row1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;line-height:24px}
        .tt-row1 .tt-title{margin:0;flex:0 1 auto;font-size:12px}

        /* ===== task カード本文 (st.container(border) を土台にした内部装飾) ===== */
        /* タイトル */
        .tt-title{font-weight:700;font-size:14px;line-height:1.4;margin:1px 0 8px;color:var(--text)}
        .tt-title.done{color:var(--text-3);text-decoration:line-through}
        /* リンク/メトリクス行 */
        .tt-links{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
        .tt-link{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:600;
            border-radius:8px;padding:5px 9px;background:var(--bg-deep);box-shadow:var(--inset);
            color:var(--teal) !important;border-bottom:0 !important}
        .tt-metric{display:inline-flex;align-items:center;gap:5px;font-weight:700;font-size:12px;
            font-family:var(--f-num);background:var(--bg-deep);border-radius:8px;padding:4px 9px;
            box-shadow:var(--inset)}
        .tt-metric.sold{color:var(--ok)}
        .tt-metric.zero{color:var(--err)}
        /* 欠落バッジ */
        .tt-gaps{display:flex;gap:5px;flex-wrap:wrap;margin-top:2px}
        .tt-gap{font-size:10.5px;font-weight:600;border-radius:999px;padding:3px 10px}
        .tt-gap.rival{background:rgba(168,52,27,.12);color:var(--err);border:1px solid rgba(168,52,27,.30)}
        .tt-gap.miss{background:rgba(184,134,11,.12);color:var(--warn);border:1px solid rgba(184,134,11,.34)}
        .tt-gap.ok{background:rgba(46,125,91,.12);color:var(--ok);border:1px solid rgba(46,125,91,.30)}
        /* ===== チェックリストカードの立体化 (Fugu HIGH-1) =====
           ui_themes のグローバル `[data-testid="stVerticalBlock"] >
           div[data-testid="stVerticalBlockBorderWrapper"]` (詳細度 0,2,1, !important)
           が st.container(border=True) を flat 化する経路があるため、型タグ `div` を
           足して詳細度 0,2,2 に上げ raised-sm 浮きを担保する (mockup .task = 浮きカード)。
           値 (--raised-sm) は全体テーマの既存 raised-sm トークンと同値。 */
        div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]{
            box-shadow:var(--raised-sm) !important;background:var(--surface) !important;
            border-radius:var(--r-sm) !important}
        /* 完了行はカード全体を沈ませる (mockup .task.done = inset/グレーアウト)。
           .tt-row-done を前置して詳細度 0,3,2 = 上の浮きルール(0,2,2)を確実に上書き。 */
        .tt-row-done div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"],
        .tt-row-done [data-testid="stVerticalBlockBorderWrapper"]{
            box-shadow:var(--inset) !important;background:var(--bg-deep) !important;opacity:.72}
        /* 削除済み */
        .tt-gone{opacity:.5;font-style:italic;font-size:12px;color:var(--text-3)}

        /* ===== 完了セレブレーション (ダークティール gradient のアクセントカード) ===== */
        .tt-cele{position:relative;overflow:hidden;border-radius:var(--r);padding:28px 22px;
            text-align:center;color:#f3efe6;
            background:linear-gradient(120deg,var(--teal-deep),var(--teal) 72%,var(--teal-hi));
            box-shadow:var(--raised);margin-top:26px}
        .tt-cele .big{font-size:26px;font-weight:800;letter-spacing:.3px}
        .tt-cele .sub{font-size:13px;opacity:.9;margin-top:6px;font-weight:500}
        .tt-cele .streaknew{display:inline-flex;align-items:center;gap:8px;margin-top:16px;
            background:rgba(255,255,255,.15);border:1.5px solid rgba(255,255,255,.36);
            border-radius:999px;padding:8px 20px;font-size:14px;font-weight:700}
        .tt-cele .celesub{margin-top:8px;font-size:12px;opacity:.75;font-weight:500}

        /* ===== 2部バー文字色: global theme override 対策 (高詳細度 + !important) ===== */
        .tt-cond,.tt-cond *{color:#f3efe6 !important;opacity:1 !important}
        .tt-cond .tt-gobtn,.tt-cond .tt-gobtn *{color:var(--teal-deep) !important}
        /* topbar teal 円ロゴ (📋) を明色強制 */
        .tt-logo{color:#fff !important}
        /* ===== ティール背景上テキスト明色化 (global --nm-text-2 override 対策) ===== */
        .tt-cele .big,
        .tt-cele .sub,
        .tt-cele .streaknew,
        .tt-cele .celesub{color:#f3efe6 !important}
        .tt-ring .n .gd{color:#fff !important}
        </style>""",
        unsafe_allow_html=True,
    )


# タブ密度化リファクタ B1 (2026-07-04): ジャンプボタン群 (🌅早朝の部 導線 3 個 +
# 各タスク行の「📝登録画面へ」) を 12px baseline に小型化。widget key は全て
# "today_" prefix (grep 済・他タブと非衝突確認済) なので tab_supplier_candidates
# A2 の per-widget-type 分割は不要 (K1)。
_TT_DENSITY_CSS = """
<style>
div[class*="st-key-today_goto_"] button,
div[class*="st-key-today_jump_"] button {
  font-size:12px !important;
  padding:2px 10px !important;
  min-height:26px !important;
  line-height:22px !important;
}
</style>
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# topbar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_topbar(streak: dict, done: int, total: int) -> None:
    """タイトル + 🔥 streak chip (数字付き) + 日付 + 本日 X/total サマリ."""
    today_str = datetime.date.today().strftime("%Y/%m/%d")
    cur = int(streak.get("current_streak", 0) or 0)
    best = int(streak.get("best_streak", 0) or 0)

    _md(
        f"""<div class="tt-topbar">
          <div class="tt-brand">
            <div class="tt-logo">📋</div>
            <div>
              <h1>午後の作業</h1>
              <p>毎日10件だけ。初期登録をベイビーステップで消化 — 売れ筋・未整備 DESC</p>
            </div>
          </div>
          <div class="tt-spacer"></div>
          <div class="tt-chip streak">
            <span class="dot"></span>
            🔥 &nbsp;<b>{cur}</b>&nbsp;日連続 &nbsp;/&nbsp; 最高 <b>{best}</b>
          </div>
          <div class="tt-chip date">
            <span class="dot" style="background:var(--text-3)"></span>
            <b>{today_str}</b>&nbsp;(JST)
          </div>
          <div class="tt-chip summary">
            <span class="dot"></span>
            本日 <b>{done}</b>&nbsp;/&nbsp;<b>{total}</b>
          </div>
        </div>"""
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2部コンディションバー (cream 立体カード 2枚)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_condition_bar(done: int, total: int) -> None:
    """🌅 早朝の部 / 🌙 夜間の部 の 2 部コンディションバー (cream neumorphic カード 2枚)。

    🌅 早朝の部: リサーチタブへのジャンプボタン 3 個 (リサーチ対戦 / 今日の発掘 / ライバル調査)。
        ボタンは st.button → _w134_sel + _w217a_cat_view("⚲ リサーチ") を書いて st.rerun()。
    🌙 夜間の部: 初期登録 10 件ゴール X/total の進捗表示 (本タブの主役)。

    HTML は「左カード=ラベル+リード+ボタン枠placeholder+sub / 右カード=ゴール表示」を
    grid で描き、左カードの直後に実 st.button をぶら下げる (Streamlit widget は markdown
    内に置けないため、カード見た目は CSS、操作は native widget の二層構成)。
    """
    # 🌙 pips (cream 沈み = 未 / teal 浮き = 済)
    pips_html = "".join(
        f'<i class="{"done" if i < done else ""}"></i>' for i in range(total)
    )
    dots_label = "●" * done + "○" * max(0, total - done)

    _md(
        f"""<div class="tt-cond">
          <div class="tt-cond-inner">

            <div class="tt-part">
              <div class="tt-part-label">🌅 早朝の部 — リサーチ</div>
              <div class="tt-part-lead">新しい仕入れの種をさがす</div>
              <div class="tt-gobtns">
                <span class="tt-gobtn">⚔️ 午前の作業へ →</span>
                <span class="tt-gobtn">⛏️ 今日の発掘へ →</span>
                <span class="tt-gobtn">📊 ライバル調査 →</span>
              </div>
              <div class="tt-part-sub">朝のうちに回しておく。夜間の部とは独立。</div>
            </div>

            <div class="tt-cond-vbar"></div>

            <div class="tt-part">
              <div class="tt-part-label">🌙 夜間の部 — 商品管理 初期登録</div>
              <div class="tt-goal-row">
                <div class="tt-goal-big">{done}<small>/{total}</small></div>
                <span class="tt-nowbadge">いま夜間の部 🌙</span>
              </div>
              <div class="tt-goal-pips-row">
                <div class="tt-pips">{pips_html}</div>
                <span class="tt-pips-label">{_esc(dots_label)} {done}/{total}</span>
              </div>
              <div class="tt-part-sub">今日のゴール：<b style="opacity:1">{total} 件</b>（未済が少ない日はその実数）。<br>
                欠落バッジを埋めながら消し込む。</div>
            </div>

          </div>
        </div>"""
    )

    # 🌅 リサーチ導線ボタン 3 個 (左カードの直下に native widget で配置)。
    # routing contract: _w134_sel(ページ名) + _w217a_cat_view("⚲ リサーチ") を両方設定。
    # research タブは「⚲ リサーチ」グループ所属 → group key も set して view-sync を確実化。
    btn_c1, btn_c2, btn_c3, _spacer = st.columns([1, 1, 1, 2])
    with btn_c1:
        if st.button("⚔️ 午前の作業へ →", key=f"{_SS}goto_duel", use_container_width=True):
            st.session_state["_w134_sel"] = "午前の作業"
            st.session_state["_w217a_cat_view"] = "★ 毎日"
            st.rerun()
    with btn_c2:
        if st.button("⛏️ 今日の発掘へ →", key=f"{_SS}goto_discovery", use_container_width=True):
            st.session_state["_w134_sel"] = "今日の発掘"
            st.session_state["_w217a_cat_view"] = "⚲ リサーチ"
            st.rerun()
    with btn_c3:
        if st.button("📊 ライバル調査へ →", key=f"{_SS}goto_rival", use_container_width=True):
            st.session_state["_w134_sel"] = "ライバルセラー監視"
            st.session_state["_w217a_cat_view"] = "⚲ リサーチ"
            st.rerun()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W322 (2026-07-05): AI店長 夕方digest 「今夜の価格対応候補」
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_evening_digest() -> None:
    """夕方 refresh (19:30) が抽出した「今夜の価格対応候補」を1行形式で表示.

    monitor.evening_digest.get_evening_price_candidates() を Discord 通知
    (tasks/task_evening_refresh.py) と共有 (同一抽出クエリ、データソース不一致防止)。
    """
    try:
        from monitor.evening_digest import get_evening_price_candidates
        candidates = get_evening_price_candidates()
    except Exception as _e:  # noqa: BLE001 — 本セクション表示失敗でタブ全体を落とさない
        st.caption(f"🤖 今夜の価格対応候補: 取得エラー ({_e})")
        return

    with st.container(border=True):
        st.markdown(f"**🤖 今夜の価格対応候補 ({len(candidates)}件)**")
        if not candidates:
            st.caption("本日は対応候補なし")
        else:
            for c in candidates:
                st.caption(c.get("line", ""))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 進捗リング (cream 立体カード + teal conic-gradient)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_progress_ring(done: int, total: int) -> None:
    """conic-gradient で X/total の進捗リングを cream 立体カード内に描画 (HTML/CSS、JS 不要)."""
    pct = round(done / total * 100) if total > 0 else 0
    all_done = done == total and total > 0
    ring_fill = "#d4a017" if all_done else "var(--teal)"  # 全完了で金色リング
    left = total - done
    label = "完了 🎉" if left <= 0 else f"あと{left}件"
    gd_bg = "#d4a017" if all_done else "var(--teal)"

    _hint = (
        f"チェックを入れると「初期登録済み」フラグが立ちます。"
        f"{total}件消し込むと今日は完了 — 連続記録が伸びます"
    )
    ring_html = f"""<div class="tt-panel tt-ringwrap">
      <h3 title="{_hint}">🎯 今日の進捗 <span style="opacity:.6;font-size:10px;font-weight:400">ℹ️</span></h3>
      <div class="tt-ring" style="background:conic-gradient({ring_fill} {pct}%, var(--bg-deep) 0)">
        <div class="n">
          <b>{done}<small>/{total}</small></b>
          <div class="gd" style="background:{gd_bg}">{label}</div>
        </div>
      </div>
      <div class="tt-kpis">
        <div class="tt-kpi"><b style="color:var(--ok)">{done}</b><span>完了</span></div>
        <div class="tt-kpi"><b style="color:var(--teal)">{left if left >= 0 else 0}</b><span>のこり</span></div>
      </div>
    </div>"""
    _md(ring_html)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 欠落バッジ (設計書 §5 に準拠)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# データ層の _missing_badges を単一定義として使う (MED-2 / drift 防止)。
# import 失敗時(テスト環境等)のみ最小 fallback を使用。
try:
    from monitor.daily_task_db import _missing_badges as _missing_badges
except Exception:  # pragma: no cover
    def _missing_badges(t: dict) -> list[str]:  # type: ignore[misc]
        out: list[str] = []
        if not (t.get("competitor_count") or 0):
            out.append("ライバル未登録")
        if not t.get("purchase_yen"):
            out.append("仕入¥未")
        if not t.get("weight_g"):
            out.append("重量未")
        if not (t.get("length_cm") and t.get("width_cm") and t.get("height_cm")):
            out.append("寸法未")
        if not t.get("lp_breakeven_usd"):
            out.append("損益分岐未")
        return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# タスク行 (1 件) — st.container(border) を土台にした立体カード
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_task_row(t: dict, idx: int) -> None:
    """1 件: チェック + タイトル + eBay/仕入先リンク + sold/競合 + 欠落バッジ + 登録画面へ。

    立体カード = st.container(border=True) (neumorph テーマで raised-sm の cream カード化)。
    完了行は親に .tt-row-done を付け、CSS で border-wrapper を inset/グレーアウトさせる。

    チェック on/off で on_change callback が DB に書込 → bump_db_version() → st.rerun()。

    Stale-state clobber 防止 (W292 bug fix):
      Streamlit は key の session_state が存在すると value= を無視するため、
      毎 render の checkbox 描画直前に DB 真値を session_state へ同期する。
      これで「外部で reg が変わった → 次の rerun で session_state が DB 値に追従」
      「ユーザの toggle は on_change callback が先に DB へ書き込む → 直後の同期で整合」
      の両方を保証する。
    """
    eid: str = t.get("ebay_item_id", "")
    title: str = t.get("title") or "(タイトル不明)"
    sold: int = int(t.get("sold") or 0)
    competitor_count: int = int(t.get("competitor_count") or 0)
    is_done: bool = bool(t.get("initial_registered"))
    is_gone: bool = bool(t.get("listing_gone"))
    badges = _missing_badges(t)

    chk_key = f"{_SS}chk_{eid}"

    # ── DB 真値を widget に強制同期 (stale-state clobber 防止) ──────────────
    st.session_state[chk_key] = is_done

    def _on_change_chk(item_eid: str) -> None:
        """チェック toggle 時の DB 書込 callback."""
        new_val: bool = bool(st.session_state.get(f"{_SS}chk_{item_eid}", False))
        try:
            from monitor.database import set_initial_registered
            set_initial_registered(item_eid, new_val)
            from ui_cache import bump_db_version
            bump_db_version()
        except Exception as _ce:  # noqa: BLE001 — DB 失敗は握り潰さず可視化 (Q0)
            st.error(f"DB 更新エラー: {_ce}")

    # 完了行はラッパに .tt-row-done を付与 (CSS で border-wrapper を沈ませる)。
    # 削除済みは触れない (チェック不可)。
    if is_done and not is_gone:
        st.markdown('<div class="tt-row-done">', unsafe_allow_html=True)

    with st.container(border=True):
        col_chk, col_body, col_btn = st.columns([1, 10, 3], vertical_alignment="center")

        with col_chk:
            st.checkbox(
                "完了",
                key=chk_key,
                on_change=_on_change_chk,
                args=(eid,),
                label_visibility="collapsed",
                disabled=is_gone,
            )

        with col_body:
            if is_gone:
                st.markdown(
                    f'<span class="tt-gone">#{idx} {_esc(title)} — 削除済み</span>',
                    unsafe_allow_html=True,
                )
            else:
                # タブ密度化リファクタ B1 (2026-07-04): タイトル/リンク/メトリクス/
                # 欠落バッジを単一 .tt-row1 flex 行にまとめ「1タスク1行」に圧縮
                # (旧: 3 個の st.markdown = 3 行、機能は不変)。
                title_cls = "tt-title done" if is_done else "tt-title"
                row_parts = [f'<span class="{title_cls}">{_esc(title)}</span>']

                # eBay / 仕入先 リンク + sold / 競合 メトリクス
                ebay_url = _EBAY_ITEM_URL.format(eid=_esc(eid)) if eid else ""
                sup_url = _supplier_url(t.get("sku"))
                sold_cls = "sold" if sold > 0 else "zero"
                rival_cls = "zero" if competitor_count == 0 else "sold"
                if ebay_url:
                    row_parts.append(
                        f'<a class="tt-link" href="{ebay_url}" target="_blank" '
                        f'rel="noopener">eBay ↗</a>'
                    )
                if sup_url:
                    row_parts.append(
                        f'<a class="tt-link" href="{_esc(sup_url)}" target="_blank" '
                        f'rel="noopener">仕入先 ↗</a>'
                    )
                row_parts.append(f'<span class="tt-metric {sold_cls}">Sold {sold}</span>')
                row_parts.append(
                    f'<span class="tt-metric {rival_cls}">競合 {competitor_count}</span>'
                )

                # 欠落バッジ (完了後は非表示 = 視覚ノイズ削減)
                if is_done:
                    pass
                elif badges:
                    for b in badges:
                        css_cls = "rival" if b == "ライバル未登録" else "miss"
                        pfx = "⚠ " if b == "ライバル未登録" else ""
                        row_parts.append(
                            f'<span class="tt-gap {css_cls}">{pfx}{_esc(b)}</span>'
                        )
                else:
                    row_parts.append('<span class="tt-gap ok">✅ 物理属性 完備</span>')

                st.markdown(
                    f'<div class="tt-row1">{"".join(row_parts)}</div>',
                    unsafe_allow_html=True,
                )

        with col_btn:
            btn_disabled = is_done or is_gone
            if st.button(
                "📝 登録画面へ",
                key=f"{_SS}jump_{eid}",
                disabled=btn_disabled,
                use_container_width=True,
            ):
                # §8.2(a) jump フック: pm_focus_eid を seed して商品管理へ遷移
                st.session_state["pm_focus_eid"] = eid
                st.session_state["_w134_sel"] = "商品管理"
                st.session_state["_w217a_cat_view"] = "★ 毎日"  # 商品管理は★毎日グループ
                st.rerun()

    if is_done and not is_gone:
        st.markdown("</div>", unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# セレブレーション
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_celebration(streak: dict) -> None:
    """10件 all_done 時のセレブレーション表示 (ダークティール gradient アクセントカード)."""
    cur = int(streak.get("current_streak", 0) or 0)
    best = int(streak.get("best_streak", 0) or 0)
    gap_to_best = best - cur
    best_msg = (
        f"🏆 自己ベスト更新！ {cur} 日連続"
        if gap_to_best <= 0
        else f"🔥 {cur} 日連続達成！ — 自己ベストまであと {gap_to_best} 日"
    )
    st.balloons()
    _md(
        f"""<div class="tt-cele">
          <div class="big">🎉 本日の初期登録、完了！</div>
          <div class="sub">すべて消し込みました。お疲れさまでした 🌙</div>
          <div class="streaknew">{best_msg}</div>
          <div class="celesub">明日も売れ筋上位がここに並びます</div>
        </div>"""
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メインエントリ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_today_tasks_tab(s: dict) -> None:
    """本タブ本体 (app.py dispatch から呼出)。引数 s = st.session_state.settings (既存規約)。"""
    _inject_css()
    st.markdown(_TT_DENSITY_CSS, unsafe_allow_html=True)
    # neumorphic scope を開く (本タブの custom HTML を .tt-scope 配下に置く)
    st.markdown('<div class="tt-scope">', unsafe_allow_html=True)

    # 1. データ層 lazy import (失敗を st.error で可視化 — Q0)
    try:
        from monitor.daily_task_db import (
            get_today_tasks_with_status,
            get_streak,
            bump_streak_on_completion,
        )
    except Exception as _import_err:  # noqa: BLE001
        st.error(f"データ層 import 失敗 (monitor.daily_task_db): {_import_err}")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # 2. データ取得
    try:
        status = get_today_tasks_with_status()
    except Exception as _fetch_err:  # noqa: BLE001
        st.error(f"本日タスク取得エラー: {_fetch_err}")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    tasks: list[dict] = status["tasks"]
    done: int = status["done"]
    total: int = status["total"]
    all_done: bool = status["all_done"]

    streak = get_streak()

    # 3. all_done なら streak bump (冪等 — 同日 2 度目以降は helper 側で吸収)
    if all_done:
        streak = bump_streak_on_completion()

    # 4. 描画: topbar + 2部コンディションバー + AI店長 夕方digest (W322)
    _render_topbar(streak, done, total)
    _render_condition_bar(done, total)
    _render_evening_digest()

    # タスクが空 = 未登録 listing がゼロ (total=0)
    if not tasks:
        cur_streak = int(streak.get("current_streak", 0) or 0)
        st.success("✨ 本日の初期登録対象はありません。")
        if cur_streak > 0:
            st.caption(f"現在 {cur_streak} 日連続 🔥")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # 5. メインレイアウト: 進捗リング (左) + チェックリスト (右)
    ring_col, list_col = st.columns([1, 2], gap="medium")

    with ring_col:
        _render_progress_ring(done, total)

    with list_col:
        # リストヘッダ
        _md(
            f"""<div class="tt-listhead">
              <h2>本日の{total}件</h2>
              <span class="sub">売れ筋・未整備 DESC で固定</span>
              <span class="count">{done} / {total} 済</span>
            </div>"""
        )

        # 未済 → 完了済み → 削除済み の順で描画 (mockup: 未済を上、完了を下にまとめる)
        done_tasks = [t for t in tasks if t.get("initial_registered") and not t.get("listing_gone")]
        todo_tasks = [t for t in tasks if not t.get("initial_registered") and not t.get("listing_gone")]
        gone_tasks = [t for t in tasks if t.get("listing_gone")]

        # 未済を先に表示
        for idx, t in enumerate(todo_tasks, start=1):
            _render_task_row(t, idx)

        # 完了区切り帯 (mockup .doneband: 「— ここまで完了 X件 / これから Y件 —」)
        if done_tasks:
            st.markdown(
                f'<div class="tt-doneband">— 完了 {len(done_tasks)} 件 / '
                f'これから {len(todo_tasks)} 件 —</div>',
                unsafe_allow_html=True,
            )
            for idx, t in enumerate(done_tasks, start=len(todo_tasks) + 1):
                _render_task_row(t, idx)

        if gone_tasks:
            st.markdown(
                '<div class="tt-doneband">— 削除済み (当日スナップショットで保持) ↓ —</div>',
                unsafe_allow_html=True,
            )
            for idx, t in enumerate(gone_tasks, start=len(todo_tasks) + len(done_tasks) + 1):
                _render_task_row(t, idx)

    # 6. 全完了セレブレーション
    if all_done:
        _render_celebration(streak)

    st.markdown("</div>", unsafe_allow_html=True)
