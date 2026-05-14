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

    </style>
    """, unsafe_allow_html=True)


# 後方互換: 以前 apply_jarvis_theme / apply_custom_styling を呼んでいる箇所があれば
# 新テーマに置き換える。これで app.py の既存 import を変更せず切替可能。
def apply_jarvis_theme():
    apply_dark_paper_theme()


def apply_custom_styling():
    apply_dark_paper_theme()
