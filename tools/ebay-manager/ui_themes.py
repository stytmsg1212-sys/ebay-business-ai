"""MonoHonpo Dark Paper theme — brand-v3 "09 Manager UI" 準拠 (2026-04-23)

設計源泉: design/monohonpo-brand-v3.html セクション 09
配色 (wabi-sabi editorial):
  background: ink #1a1817 / card #24201c / topbar #14110f
  accent: shu #a8341b (vermilion) / sage #6b7a5c / brass #8b7355
  text: paper-hi #fbf9f3 / paper-edge #d8cdb5 / ink-4 #9a8f82

数字フォントは JetBrains Mono (tabular、可読性優先)、
display は Cormorant Garamond、body は Inter。
"""
import streamlit as st


def apply_dark_paper_theme():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

    :root {
        /* Paper (light surfaces) */
        --paper: #f6f2ea;
        --paper-hi: #fbf9f3;
        --paper-lo: #ece4d3;
        --paper-edge: #d8cdb5;
        --paper-edge-soft: #e5ddca;

        /* Ink (dark surfaces) */
        --ink: #1a1817;
        --ink-2: #3a332c;
        --ink-3: #6b6157;
        --ink-4: #9a8f82;
        --ink-5: #bcb2a3;

        /* Accent */
        --shu: #a8341b;
        --shu-dim: #8a2b17;
        --shu-hi: #c54a2c;
        --brass: #8b7355;
        --sage: #6b7a5c;

        /* Surface elevations (brand-v3 mgr) */
        --surface-top: #14110f;
        --surface-base: #1a1817;
        --surface-card: #24201c;
        --surface-row: #1e1a17;

        /* Status */
        --green-ok: #6b7a5c;
        --amber: #c89b2a;

        /* Fonts */
        --f-display: 'Cormorant Garamond', 'Noto Serif JP', serif;
        --f-body: 'Inter', 'Segoe UI', sans-serif;
        --f-mono: 'JetBrains Mono', 'Consolas', monospace;
        --f-num: 'JetBrains Mono', 'Consolas', monospace;
    }

    /* ────── Base ────── */
    [data-testid="stAppViewContainer"] {
        background: var(--surface-base) !important;
    }
    .main { background: transparent !important; }

    [data-testid="stSidebar"] {
        background: var(--surface-top) !important;
        border-right: 1px solid var(--ink-2) !important;
    }

    /* ────── Typography ────── */
    html, body, [data-testid="stAppViewContainer"] {
        color: var(--paper-hi) !important;
        font-family: var(--f-body) !important;
        font-size: 14px !important;
        line-height: 1.55 !important;
    }

    h1, h2, h3, h4, h5, h6 {
        font-family: var(--f-display) !important;
        color: var(--paper-hi) !important;
        font-weight: 500 !important;
        letter-spacing: 0.3px !important;
    }
    h1 { font-size: 28px !important; font-weight: 500 !important; }
    h2 { font-size: 22px !important; font-weight: 500 !important; border: none !important; padding: 0 !important; }
    h3 { font-size: 18px !important; font-weight: 500 !important; }
    h4 {
        font-family: var(--f-mono) !important;
        font-size: 11px !important; font-weight: 500 !important;
        color: var(--paper-edge) !important;
        letter-spacing: 2.5px !important; text-transform: uppercase !important;
    }

    p, span, li, label, .stMarkdown, div {
        font-family: var(--f-body) !important;
        color: var(--paper-edge) !important;
    }
    strong, b { color: var(--paper-hi) !important; font-weight: 600 !important; }

    /* Numbers use JetBrains Mono for tabular legibility */
    .stMetric [data-testid="stMetricValue"],
    [data-testid="stMetricValue"],
    .stNumberInput input,
    .stTextInput input[type="number"] {
        font-family: var(--f-num) !important;
        font-variant-numeric: tabular-nums !important;
        letter-spacing: 0 !important;
    }

    /* ────── Tabs (brand-v3 mgr-tabs) ────── */
    .stTabs [data-baseweb="tab-list"] {
        background: var(--surface-top) !important;
        border-bottom: 1px solid var(--ink-2) !important;
        gap: 0 !important; padding: 0 !important;
    }
    .stTabs [data-baseweb="tab-list"] button {
        background: transparent !important;
        border: 0 !important;
        border-right: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
        color: var(--ink-4) !important;
        font-family: var(--f-mono) !important;
        font-weight: 500 !important; font-size: 11px !important;
        letter-spacing: 2.5px !important; text-transform: uppercase !important;
        padding: 14px 20px !important; margin: 0 !important;
        box-shadow: none !important; transition: all 0.25s !important;
    }
    .stTabs [data-baseweb="tab-list"] button:hover {
        color: var(--paper-edge) !important;
        background: var(--surface-row) !important;
        transform: none !important;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        color: var(--paper-hi) !important;
        background: var(--surface-base) !important;
        position: relative !important;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"]::before {
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: var(--shu);
    }

    /* ────── Buttons (brand-v3 filter + pill) ────── */
    .stButton button, .stFormSubmitButton button {
        font-family: var(--f-mono) !important;
        background: transparent !important;
        border: 1px solid var(--ink-2) !important;
        color: var(--paper-edge) !important;
        padding: 7px 14px !important;
        border-radius: 2px !important;
        font-weight: 500 !important;
        font-size: 10px !important;
        letter-spacing: 2px !important;
        text-transform: uppercase !important;
        box-shadow: none !important;
        animation: none !important;
        transition: all 0.2s !important;
    }
    .stButton button:hover, .stFormSubmitButton button:hover {
        background: var(--ink-2) !important;
        color: var(--paper-hi) !important;
        border-color: var(--ink-3) !important;
        box-shadow: none !important;
        transform: none !important;
    }
    .stButton button[kind="primary"],
    .stButton button[type="primary"],
    .stFormSubmitButton button[kind="primary"] {
        background: var(--shu) !important;
        border: 1px solid var(--shu) !important;
        color: var(--paper-hi) !important;
    }
    .stButton button[kind="primary"]:hover,
    .stFormSubmitButton button[kind="primary"]:hover {
        background: var(--shu-hi) !important;
        border-color: var(--shu-hi) !important;
    }

    /* ────── Inputs ────── */
    .stTextInput input, .stNumberInput input,
    .stSelectbox select, .stTextArea textarea,
    .stDateInput input, .stTimeInput input {
        font-family: var(--f-body) !important;
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
        color: var(--paper-hi) !important;
        border-radius: 2px !important;
        font-size: 13px !important;
    }
    .stTextInput input:focus, .stNumberInput input:focus,
    .stSelectbox select:focus, .stTextArea textarea:focus {
        border-color: var(--shu) !important;
        box-shadow: 0 0 0 1px var(--shu) !important;
        outline: none !important;
    }
    /* Selectbox dropdown */
    [data-baseweb="select"] > div {
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
        border-radius: 2px !important;
        color: var(--paper-hi) !important;
    }
    [data-baseweb="popover"] {
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
    }
    [data-baseweb="menu"] li {
        background: var(--surface-card) !important;
        color: var(--paper-edge) !important;
        font-family: var(--f-body) !important;
    }
    [data-baseweb="menu"] li:hover {
        background: var(--surface-row) !important;
        color: var(--paper-hi) !important;
    }

    /* ────── Metrics (brand-v3 .kpi) ────── */
    [data-testid="metric-container"],
    [data-testid="stMetric"] {
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
        padding: 18px 16px 14px !important;
        box-shadow: none !important;
        animation: none !important;
        position: relative !important;
    }
    [data-testid="metric-container"]::before,
    [data-testid="stMetric"]::before {
        content: '';
        position: absolute;
        top: 0; left: 0;
        width: 2px; height: 22px;
        background: var(--shu);
    }
    [data-testid="metric-container"] label,
    [data-testid="stMetric"] label {
        font-family: var(--f-mono) !important;
        color: var(--ink-4) !important;
        font-size: 9px !important;
        font-weight: 500 !important;
        letter-spacing: 2px !important;
        text-transform: uppercase !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"],
    [data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-family: var(--f-num) !important;
        color: var(--paper-hi) !important;
        font-size: 28px !important;
        font-weight: 600 !important;
        font-variant-numeric: tabular-nums !important;
        line-height: 1.1 !important;
        text-shadow: none !important;
    }
    [data-testid="stMetricDelta"] {
        font-family: var(--f-mono) !important;
        font-size: 10px !important;
        letter-spacing: 1px !important;
    }

    /* ────── Containers / Expander ────── */
    [data-testid="stExpander"] {
        border: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
        background: var(--surface-card) !important;
        box-shadow: none !important;
    }
    [data-testid="stExpander"] summary {
        font-family: var(--f-mono) !important;
        color: var(--paper-edge) !important;
        font-weight: 500 !important;
        font-size: 11px !important;
        letter-spacing: 1.5px !important;
        text-transform: uppercase !important;
        padding: 12px 16px !important;
    }
    [data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
        background: var(--surface-card) !important;
        box-shadow: none !important;
    }

    /* Hide Material Icons text fallback */
    @font-face {
        font-family: 'Material Symbols Rounded';
        src: local('__disabled__');
    }
    .material-symbols-rounded {
        display: none !important;
    }

    /* W258 (2026-06-11): Material icon 生テキスト露出の根治。
       Streamlit 1.56 は icon を [data-testid="stIconMaterial"] に "check" 等の
       生テキストで描画する。フォント無効化だけでは生テキストが露出し、ボタン幅で
       先頭が欠けて "heck"/"ieck" に見える。要素ごと非表示 + expander caret は
       ::before の unicode 三角で代替 (tab_product_management.py の実績ある局所 fix の
       グローバル昇格)。 */
    [data-testid="stIconMaterial"] {
        display: none !important;
    }
    [data-testid="stExpander"] details summary::before {
        content: '▶';
        display: inline-block;
        margin-right: 0.6em;
        font-size: 0.85em;
        font-weight: 700;
        transition: transform 0.15s;
    }
    [data-testid="stExpander"] details[open] summary::before {
        content: '▼';
    }

    /* ────── Alerts (brand-v3 pill) ────── */
    .stSuccess {
        border: 1px solid var(--sage) !important;
        background: rgba(107,122,92,0.12) !important;
        border-radius: 2px !important;
        color: var(--paper-hi) !important;
    }
    .stError {
        border: 1px solid var(--shu) !important;
        background: rgba(168,52,27,0.12) !important;
        border-radius: 2px !important;
        color: var(--paper-hi) !important;
    }
    .stWarning {
        border: 1px solid var(--amber) !important;
        background: rgba(200,155,42,0.12) !important;
        border-radius: 2px !important;
        color: var(--paper-hi) !important;
    }
    .stInfo {
        border: 1px solid var(--brass) !important;
        background: rgba(139,115,85,0.12) !important;
        border-radius: 2px !important;
        color: var(--paper-hi) !important;
    }

    /* ────── Dividers ────── */
    hr {
        border: 0 !important;
        height: 1px !important;
        background: var(--ink-2) !important;
        margin: 20px 0 !important;
        box-shadow: none !important;
    }

    /* ────── Scrollbar ────── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: var(--surface-top); }
    ::-webkit-scrollbar-thumb { background: var(--ink-2); border-radius: 0; }
    ::-webkit-scrollbar-thumb:hover { background: var(--ink-3); }

    /* ────── Links / Code ────── */
    a {
        color: var(--shu-hi) !important;
        text-decoration: none !important;
        border-bottom: 1px solid transparent !important;
    }
    a:hover {
        color: var(--paper-hi) !important;
        border-bottom-color: var(--shu-hi) !important;
    }
    code {
        font-family: var(--f-mono) !important;
        background: var(--surface-top) !important;
        border: 1px solid var(--ink-2) !important;
        color: var(--paper-edge) !important;
        padding: 1px 6px !important;
        border-radius: 2px !important;
        font-size: 12px !important;
    }

    /* ────── Checkbox / Radio ────── */
    .stCheckbox label span,
    .stRadio label span {
        font-family: var(--f-body) !important;
        font-weight: 400 !important;
        font-size: 13px !important;
        color: var(--paper-edge) !important;
    }
    .stCheckbox input:checked + div,
    .stRadio input:checked + div {
        background: var(--shu) !important;
        border-color: var(--shu) !important;
    }

    /* ────── Data tables ────── */
    [data-testid="stDataFrame"] {
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
        box-shadow: none !important;
    }
    [data-testid="stDataFrame"] table {
        font-family: var(--f-body) !important;
        color: var(--paper-edge) !important;
    }
    [data-testid="stDataFrame"] table thead th {
        font-family: var(--f-mono) !important;
        font-size: 9px !important;
        letter-spacing: 1.5px !important;
        text-transform: uppercase !important;
        color: var(--ink-4) !important;
        background: var(--surface-row) !important;
        border-bottom: 1px solid var(--ink-2) !important;
    }
    [data-testid="stDataFrame"] table tbody td {
        font-family: var(--f-body) !important;
        font-size: 12px !important;
        color: var(--paper-edge) !important;
        border-bottom: 1px solid var(--ink-2) !important;
    }
    /* numeric cells inherit tabular mono */
    [data-testid="stDataFrame"] table tbody td[class*="num"],
    [data-testid="stDataFrame"] .col_heading {
        font-variant-numeric: tabular-nums !important;
    }

    /* ────── Caption / small text ────── */
    [data-testid="stCaption"], .caption {
        font-family: var(--f-mono) !important;
        font-size: 10px !important;
        color: var(--ink-4) !important;
        letter-spacing: 1.5px !important;
    }

    /* ────── Status box (st.status) ────── */
    [data-testid="stStatusWidget"] {
        background: var(--surface-card) !important;
        border: 1px solid var(--ink-2) !important;
        border-radius: 0 !important;
    }

    /* ────── Tag / Pill emulation via markdown ────── */
    code.pill-shu { background: var(--shu) !important; color: var(--paper-hi) !important; border: 0 !important; }
    code.pill-ok { background: transparent !important; color: var(--green-ok) !important; border: 1px solid var(--green-ok) !important; }
    code.pill-sage { background: var(--sage) !important; color: var(--paper-hi) !important; border: 0 !important; }
    code.pill-brass { background: var(--brass) !important; color: var(--paper-hi) !important; border: 0 !important; }

    /* ────── W258 Mobile layer (2026-06-11) ────── */
    @media (max-width: 640px) {
        /* タッチターゲット 44px (iOS HIG)。デスクトップ密度 CSS (app.py 34px) を上書くため
           詳細度を body 前置で 1 段上げる (cascade 順で app.py が後勝ちするため)。 */
        body [data-testid="stButton"] > button,
        body [data-testid="stDownloadButton"] > button,
        body [data-testid="stFormSubmitButton"] > button {
            min-height: 44px !important;
            font-size: 14px !important;
            padding: 8px 14px !important;
        }
        body [data-testid="stTextInput"] input,
        body [data-testid="stNumberInput"] input,
        body [data-testid="stSelectbox"] div[role="combobox"] {
            min-height: 44px !important;
            font-size: 15px !important;
        }
        body .main .block-container {
            padding-left: 0.6rem !important;
            padding-right: 0.6rem !important;
        }
        body [data-testid="stDataFrame"] {
            overflow-x: auto !important;
        }
    }

    </style>
    """, unsafe_allow_html=True)


def apply_neumorph_cream_theme():
    """W261 Neumorphic Cream theme (2026-06-11) — design system 画像準拠

    配色: クリーム地 #ede7da / 深緑ティール #0e4f4b / JetBrains Mono 数値フォント
    ニューモーフィズム: raised / inset 二値の box-shadow で奥行き表現
    """
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

    :root {
        /* W261 Neumorphic Cream (2026-06-11) — design system 画像準拠 */
        --nm-bg: #ede7da;          /* base layer (クリーム地) */
        --nm-bg-deep: #e4dcca;     /* inset/沈み面 */
        --nm-surface: #f2ecdf;     /* raised card */
        --nm-surface-hi: #f9f5eb;  /* hover/最上面 */
        --nm-shadow-d: rgba(166,150,121,0.50);  /* 右下の暗影 */
        --nm-shadow-l: rgba(255,255,255,0.90);  /* 左上の光 */
        --nm-teal: #0e4f4b;        /* primary accent (深緑ティール) */
        --nm-teal-hi: #156a63;     /* hover */
        --nm-teal-deep: #0a3d3a;
        --nm-teal-soft: rgba(14,79,75,0.10);
        --nm-text: #2a2e2a;
        --nm-text-2: #5f6557;
        --nm-text-3: #8d927f;
        --nm-ok: #2e7d5b;
        --nm-warn: #b8860b;
        --nm-err: #a8341b;
        --nm-radius-sm: 8px;
        --nm-radius: 12px;
        --nm-radius-lg: 16px;
        --nm-shadow-raised: 5px 5px 12px var(--nm-shadow-d), -5px -5px 12px var(--nm-shadow-l);
        --nm-shadow-raised-sm: 3px 3px 7px var(--nm-shadow-d), -3px -3px 7px var(--nm-shadow-l);
        --nm-shadow-inset: inset 3px 3px 7px var(--nm-shadow-d), inset -3px -3px 7px var(--nm-shadow-l);
        --f-body: 'Inter', 'Segoe UI', sans-serif;
        --f-mono: 'JetBrains Mono', 'Consolas', monospace;
        --f-num: 'JetBrains Mono', 'Consolas', monospace;
    }

    /* ────── Base ────── */
    [data-testid="stAppViewContainer"] {
        background: var(--nm-bg) !important;
    }
    .main { background: transparent !important; }

    [data-testid="stSidebar"] {
        background: var(--nm-surface) !important;
        border-right: 1px solid rgba(166,150,121,0.25) !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }

    /* ────── Page fade-in ────── */
    @keyframes nmFade {
        from { opacity: 0; transform: translateY(6px); }
        to   { opacity: 1; transform: none; }
    }
    .main .block-container {
        animation: nmFade .35s ease-out;
    }
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation: none !important;
            transition: none !important;
        }
    }

    /* ────── Typography ────── */
    html, body, [data-testid="stAppViewContainer"] {
        color: var(--nm-text) !important;
        font-family: var(--f-body) !important;
        font-size: 14px !important;
        line-height: 1.55 !important;
    }

    h1, h2, h3, h5, h6 {
        font-family: var(--f-body) !important;
        color: var(--nm-text) !important;
        font-weight: 600 !important;
        letter-spacing: 0.2px !important;
    }
    h1 { font-size: 26px !important; font-weight: 700 !important; }
    h2 { font-size: 20px !important; font-weight: 600 !important; border: none !important; padding: 0 !important; }
    h3 { font-size: 16px !important; font-weight: 600 !important; }
    h4 {
        font-family: var(--f-body) !important;
        font-size: 11px !important; font-weight: 700 !important;
        color: var(--nm-text) !important;
        letter-spacing: 2px !important; text-transform: uppercase !important;
    }

    p, span, li, label, .stMarkdown, div {
        font-family: var(--f-body) !important;
    }
    /* 文字色の既定は inline style で色指定が無い要素のみに適用する。
       :not([style*="color"]) が無いと、各タブが意図して付けた
       ステータス色 (利益の緑 / 警告の琥珀 / エラーの赤 等) まで
       一律 --nm-text-2 に潰れて色の階層が消える (W261-fix 2026-06-11)。 */
    /* :where() で詳細度を 0,0,1 に据え置く (:not 直書きだと属性セレクタ分
       0,1,1 に上がり、下の `button p { inherit }` 0,0,2 を逆転してしまう)。 */
    p:where(:not([style*="color"])), span:where(:not([style*="color"])),
    li:where(:not([style*="color"])), label:where(:not([style*="color"])),
    .stMarkdown, div:where(:not([style*="color"])) {
        color: var(--nm-text-2) !important;
    }
    strong, b { color: var(--nm-text) !important; font-weight: 600 !important; }
    /* 色付きブロック内の太字は親の色を継承 (例: 緑の「効果」行の <b>) */
    [style*="color"] strong, [style*="color"] b { color: inherit !important; }

    /* ボタン内テキストは button 要素の color (primary=白) を継承させる。
       上の全 div/p/span 文字色ルールがティール塗りボタンの白文字を
       --nm-text-2 で潰すのを防ぐ (W261 実機検証で発覚)。 */
    button p, button span, button div {
        color: inherit !important;
    }

    /* Numbers use JetBrains Mono for tabular legibility */
    .stMetric [data-testid="stMetricValue"],
    [data-testid="stMetricValue"],
    .stNumberInput input,
    .stTextInput input[type="number"] {
        font-family: var(--f-num) !important;
        font-variant-numeric: tabular-nums !important;
        letter-spacing: 0 !important;
    }

    /* ────── Tabs ──────
       ピル型チップ化 (W261-fix 2026-06-11): 旧・下線式は本文と見分けが
       つかないと user 指摘 → 上部ナビと同じ「浮き出るボタン」文法に統一。 */
    .stTabs [data-baseweb="tab-list"] {
        background: transparent !important;
        border-bottom: 0 !important;
        gap: 8px !important; padding: 2px 2px 10px 2px !important;
    }
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {
        display: none !important;
    }
    .stTabs [data-baseweb="tab-list"] button {
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: 999px !important;
        color: var(--nm-text-2) !important;
        font-family: var(--f-body) !important;
        font-weight: 600 !important; font-size: 13px !important;
        letter-spacing: 0.3px !important; text-transform: none !important;
        padding: 7px 18px !important; margin: 0 !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
        cursor: pointer !important;
        transition: box-shadow .18s ease, transform .18s ease, color .18s, background .18s !important;
    }
    .stTabs [data-baseweb="tab-list"] button:hover {
        color: var(--nm-teal) !important;
        transform: translateY(-1px) !important;
        box-shadow: var(--nm-shadow-raised) !important;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        background: var(--nm-teal) !important;
        border-color: var(--nm-teal) !important;
        color: #ffffff !important;
        box-shadow: inset 2px 2px 5px rgba(0,0,0,0.25),
                    2px 2px 6px rgba(166,150,121,0.35) !important;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"]:hover {
        transform: none !important;
    }

    /* ────── Buttons ────── */
    .stButton button, .stFormSubmitButton button {
        font-family: var(--f-body) !important;
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        color: var(--nm-text-2) !important;
        padding: 7px 14px !important;
        border-radius: var(--nm-radius-sm) !important;
        font-weight: 600 !important;
        font-size: 12px !important;
        letter-spacing: 0 !important;
        text-transform: none !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
        animation: none !important;
        transition: box-shadow .18s ease, transform .18s ease, color .18s, background .18s !important;
    }
    .stButton button:hover, .stFormSubmitButton button:hover {
        background: var(--nm-surface-hi) !important;
        color: var(--nm-teal) !important;
        border-color: rgba(166,150,121,0.25) !important;
        box-shadow: var(--nm-shadow-raised) !important;
        transform: translateY(-1px) !important;
    }
    .stButton button:active, .stFormSubmitButton button:active {
        box-shadow: var(--nm-shadow-inset) !important;
        transform: translateY(0) !important;
    }
    .stButton button[kind="primary"],
    .stButton button[kind="primaryFormSubmit"],
    .stButton button[type="primary"],
    .stFormSubmitButton button[kind="primary"],
    .stFormSubmitButton button[kind="primaryFormSubmit"] {
        background: var(--nm-teal) !important;
        border: 1px solid var(--nm-teal) !important;
        color: #fff !important;
        border-radius: 999px !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }
    .stButton button[kind="primary"]:hover,
    .stButton button[kind="primaryFormSubmit"]:hover,
    .stFormSubmitButton button[kind="primary"]:hover,
    .stFormSubmitButton button[kind="primaryFormSubmit"]:hover {
        background: var(--nm-teal-hi) !important;
        border-color: var(--nm-teal-hi) !important;
        transform: translateY(-1px) !important;
    }
    .stButton button[kind="primary"]:active,
    .stButton button[kind="primaryFormSubmit"]:active,
    .stFormSubmitButton button[kind="primary"]:active,
    .stFormSubmitButton button[kind="primaryFormSubmit"]:active {
        box-shadow: var(--nm-shadow-inset) !important;
        transform: translateY(0) !important;
    }

    /* ────── Inputs ────── */
    .stTextInput input, .stNumberInput input,
    .stSelectbox select, .stTextArea textarea,
    .stDateInput input, .stTimeInput input {
        font-family: var(--f-body) !important;
        background: var(--nm-bg-deep) !important;
        border: none !important;
        color: var(--nm-text) !important;
        border-radius: 10px !important;
        font-size: 13px !important;
        box-shadow: var(--nm-shadow-inset) !important;
    }
    .stTextInput input:focus, .stNumberInput input:focus,
    .stSelectbox select:focus, .stTextArea textarea:focus {
        box-shadow: var(--nm-shadow-inset), 0 0 0 2px var(--nm-teal-soft) !important;
        outline: 1px solid var(--nm-teal) !important;
    }
    /* Selectbox dropdown */
    [data-baseweb="select"] > div {
        background: var(--nm-bg-deep) !important;
        border: none !important;
        border-radius: 10px !important;
        color: var(--nm-text) !important;
        box-shadow: var(--nm-shadow-inset) !important;
    }
    [data-baseweb="popover"] {
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius) !important;
        box-shadow: var(--nm-shadow-raised) !important;
    }
    [data-baseweb="menu"] li {
        background: var(--nm-surface) !important;
        color: var(--nm-text-2) !important;
        font-family: var(--f-body) !important;
    }
    [data-baseweb="menu"] li:hover {
        background: var(--nm-surface-hi) !important;
        color: var(--nm-teal) !important;
    }

    /* ────── Metrics ────── */
    [data-testid="metric-container"],
    [data-testid="stMetric"] {
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius-lg) !important;
        padding: 18px 16px 14px !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
        animation: none !important;
        position: relative !important;
    }
    [data-testid="metric-container"]::before,
    [data-testid="stMetric"]::before {
        content: '';
        position: absolute;
        top: 14px; left: 16px;
        width: 8px; height: 8px;
        border-radius: 50%;
        background: var(--nm-teal);
    }
    [data-testid="metric-container"] label,
    [data-testid="stMetric"] label {
        font-family: var(--f-body) !important;
        color: var(--nm-text-3) !important;
        font-size: 10px !important;
        font-weight: 600 !important;
        letter-spacing: 1.5px !important;
        text-transform: uppercase !important;
        padding-left: 14px !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"],
    [data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-family: var(--f-num) !important;
        color: var(--nm-text) !important;
        font-size: 28px !important;
        font-weight: 600 !important;
        font-variant-numeric: tabular-nums !important;
        line-height: 1.1 !important;
        text-shadow: none !important;
    }
    [data-testid="stMetricDelta"] {
        font-family: var(--f-mono) !important;
        font-size: 10px !important;
        letter-spacing: 1px !important;
    }

    /* ────── Containers / Expander ────── */
    [data-testid="stExpander"] {
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius-lg) !important;
        background: var(--nm-surface) !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }
    [data-testid="stExpander"] summary {
        font-family: var(--f-body) !important;
        color: var(--nm-text-2) !important;
        font-weight: 600 !important;
        font-size: 13px !important;
        letter-spacing: 0.3px !important;
        text-transform: none !important;
        padding: 12px 16px !important;
    }
    [data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius-lg) !important;
        background: var(--nm-surface) !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }

    /* Hide Material Icons text fallback */
    @font-face {
        font-family: 'Material Symbols Rounded';
        src: local('__disabled__');
    }
    .material-symbols-rounded {
        display: none !important;
    }

    /* W258 (2026-06-11): Material icon 生テキスト露出の根治。
       [data-testid="stIconMaterial"] 要素ごと非表示 + expander caret は
       ::before の unicode 三角で代替。 */
    [data-testid="stIconMaterial"] {
        display: none !important;
    }
    [data-testid="stExpander"] details summary::before {
        content: '▶';
        display: inline-block;
        margin-right: 0.6em;
        font-size: 0.85em;
        font-weight: 700;
        transition: transform 0.15s;
    }
    [data-testid="stExpander"] details[open] summary::before {
        content: '▼';
    }

    /* ────── Alerts ────── */
    .stSuccess {
        border: 1px solid var(--nm-ok) !important;
        background: rgba(46,125,91,0.12) !important;
        border-radius: var(--nm-radius) !important;
        color: var(--nm-text) !important;
    }
    .stError {
        border: 1px solid var(--nm-err) !important;
        background: rgba(168,52,27,0.12) !important;
        border-radius: var(--nm-radius) !important;
        color: var(--nm-text) !important;
    }
    .stWarning {
        border: 1px solid var(--nm-warn) !important;
        background: rgba(184,134,11,0.12) !important;
        border-radius: var(--nm-radius) !important;
        color: var(--nm-text) !important;
    }
    .stInfo {
        border: 1px solid var(--nm-teal) !important;
        background: rgba(14,79,75,0.10) !important;
        border-radius: var(--nm-radius) !important;
        color: var(--nm-text) !important;
    }

    /* ────── Dividers ────── */
    hr {
        border: 0 !important;
        height: 1px !important;
        background: rgba(166,150,121,0.35) !important;
        margin: 20px 0 !important;
        box-shadow: none !important;
    }

    /* ────── Scrollbar ────── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: var(--nm-bg-deep); border-radius: 999px; }
    ::-webkit-scrollbar-thumb { background: rgba(166,150,121,0.6); border-radius: 999px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(166,150,121,0.85); }

    /* ────── Links / Code ────── */
    a {
        color: var(--nm-teal) !important;
        text-decoration: none !important;
        border-bottom: 1px solid transparent !important;
    }
    a:hover {
        color: var(--nm-teal-hi) !important;
        border-bottom-color: var(--nm-teal-hi) !important;
    }
    code {
        font-family: var(--f-mono) !important;
        background: var(--nm-bg-deep) !important;
        border: 1px solid rgba(166,150,121,0.30) !important;
        color: var(--nm-text-2) !important;
        padding: 1px 6px !important;
        border-radius: var(--nm-radius-sm) !important;
        font-size: 12px !important;
    }

    /* ────── Checkbox / Radio ────── */
    .stCheckbox label span,
    .stRadio label span {
        font-family: var(--f-body) !important;
        font-weight: 400 !important;
        font-size: 13px !important;
        color: var(--nm-text-2) !important;
    }
    /* チェック ON の塗りは config.toml primaryColor (#0e4f4b) に任せる。
       旧 `input:checked + div` は実 DOM (span=箱 / input / div=ラベル文字)
       でラベル文字コンテナに誤マッチし、文字背景をティールに塗り潰して
       読めなくなる (W261-fix 2026-06-11 user 報告) ため削除。
       誤マッチの保険として、ラベル文字 div の背景を常に透明に固定する。 */
    .stCheckbox label > div:last-child,
    .stRadio label > div:last-child {
        background: transparent !important;
    }

    /* ────── Data tables ────── */
    [data-testid="stDataFrame"] {
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius) !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }
    [data-testid="stDataFrame"] table {
        font-family: var(--f-body) !important;
        color: var(--nm-text-2) !important;
    }
    [data-testid="stDataFrame"] table thead th {
        font-family: var(--f-mono) !important;
        font-size: 9px !important;
        letter-spacing: 1.5px !important;
        text-transform: uppercase !important;
        color: var(--nm-text-3) !important;
        background: var(--nm-bg-deep) !important;
        border-bottom: 1px solid rgba(166,150,121,0.25) !important;
    }
    [data-testid="stDataFrame"] table tbody td {
        font-family: var(--f-body) !important;
        font-size: 12px !important;
        color: var(--nm-text-2) !important;
        border-bottom: 1px solid rgba(166,150,121,0.15) !important;
    }
    /* numeric cells inherit tabular mono */
    [data-testid="stDataFrame"] table tbody td[class*="num"],
    [data-testid="stDataFrame"] .col_heading {
        font-variant-numeric: tabular-nums !important;
    }

    /* ────── Caption / small text ────── */
    [data-testid="stCaption"], .caption {
        font-family: var(--f-mono) !important;
        font-size: 10px !important;
        color: var(--nm-text-3) !important;
        letter-spacing: 1.5px !important;
    }

    /* ────── Status box (st.status) ────── */
    [data-testid="stStatusWidget"] {
        background: var(--nm-surface) !important;
        border: 1px solid rgba(166,150,121,0.25) !important;
        border-radius: var(--nm-radius) !important;
        box-shadow: var(--nm-shadow-raised-sm) !important;
    }

    /* ────── Tag / Pill emulation via markdown ────── */
    code.pill-shu { background: var(--nm-err) !important; color: #fff !important; border: 0 !important; }
    code.pill-ok { background: transparent !important; color: var(--nm-ok) !important; border: 1px solid var(--nm-ok) !important; }
    code.pill-sage { background: var(--nm-teal) !important; color: #fff !important; border: 0 !important; }
    code.pill-brass { background: var(--nm-warn) !important; color: #fff !important; border: 0 !important; }

    /* ────── W258 Mobile layer ────── */
    @media (max-width: 640px) {
        /* タッチターゲット 44px (iOS HIG)。デスクトップ密度 CSS (app.py 34px) を上書くため
           詳細度を body 前置で 1 段上げる。 */
        body [data-testid="stButton"] > button,
        body [data-testid="stDownloadButton"] > button,
        body [data-testid="stFormSubmitButton"] > button {
            min-height: 44px !important;
            font-size: 14px !important;
            padding: 8px 14px !important;
        }
        body [data-testid="stTextInput"] input,
        body [data-testid="stNumberInput"] input,
        body [data-testid="stSelectbox"] div[role="combobox"] {
            min-height: 44px !important;
            font-size: 15px !important;
        }
        body .main .block-container {
            padding-left: 0.6rem !important;
            padding-right: 0.6rem !important;
        }
        body [data-testid="stDataFrame"] {
            overflow-x: auto !important;
        }
    }

    </style>
    """, unsafe_allow_html=True)


# 後方互換: 以前 apply_jarvis_theme / apply_custom_styling を呼んでいる箇所があれば
# 新テーマに置き換える。これで app.py の既存 import を変更せず切替可能。
def apply_jarvis_theme():
    apply_neumorph_cream_theme()


def apply_custom_styling():
    apply_neumorph_cream_theme()
