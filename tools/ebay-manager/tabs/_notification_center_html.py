"""通知センター HTML レンダラ (純関数、DB アクセスなし)。

依頼ボード #39 Phase A S4 (2026-07-03): DASHBOARD 通知センター UI の表示部品。
``_supplier_card_html.py`` の純描画パターン (row dict → HTML 文字列、DB access なし、
``include_css`` flag で <style> 重複回避) を踏襲する。

S1 (``monitor/notification_log_db.py``) の実装確定:
- ``NOTIFICATION_SEVERITIES = {"info", "warning", "error", "critical"}``
- ``NOTIFICATION_CATEGORIES = {"inventory", "order", "rival", "keyword", "research",
   "pricing", "system", "action_required", "default"}``
- ``created_at`` は SQL ``datetime('now')`` (UTC naive、``sqlite-timezone.md`` 準拠)

改修 (2026-07-03 実機 0 点フィードバック):
- 1 通知 = **コンパクト 1 行** (~30px、左ボーダー3px + 絵文字 + 太字タイトル + 補足
  グレー + 右寄せ時刻)。ベル単独行・「Discord 通知済」表示は廃止。
- ボタン (開く / ✓既読) は Streamlit 側で同一行右端に小型配置 (``st.columns``)。
- 相対時刻は「たった今 / N分前 / N時間前 / 昨日 / N日前 / M/D」形式 (「昨日」を追加)。

DASHBOARD 磨き込み (2026-07-04、依頼ボード #39 差し戻し対応):
- ``humanize_notification_text()``: DB 保存済みの古い通知 (発行側の文言修正が
  間に合わなかった過去分) に残る内部変数露出 ("W153 truncation" / "processed=868,
  issues=818" / "max_listings_per_run=30" 等) を **render 時**に業務語へ変換する
  層。発行側 (tasks/) の文言修正は過去 DB レコードには反映されないため、表示側
  でも既知パターンを吸収する。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import escape as _esc
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# カテゴリ / severity マッピング (S1 API 契約の値に対応)
# ---------------------------------------------------------------------------

CATEGORY_EMOJI: dict[str, str] = {
    "order": "📦",
    "action_required": "🚨",
    "keyword": "🔔",
    "rival": "⚔",
    "system": "⚙",
    "inventory": "📉",
    "research": "🔍",
    "pricing": "💲",
    "default": "🔖",
}
_DEFAULT_EMOJI = "🔖"

# S1 whitelist の 9 カテゴリを日本語見出しにマップ。実機 fb「default (1)」問題を根治。
CATEGORY_LABEL_JA: dict[str, str] = {
    "order": "注文",
    "action_required": "要対応",
    "keyword": "キーワード新着",
    "rival": "競合/最安値",
    "system": "システム運用",
    "inventory": "在庫",
    "research": "リサーチ",
    "pricing": "価格",
    "default": "その他",
}

SEVERITY_COLOR: dict[str, str] = {
    # S1 whitelist (monitor.notification_log_db.NOTIFICATION_SEVERITIES) と
    # 完全一致で保持: {"info", "warning", "error", "critical"}。
    # キーが whitelist と食い違うと左ボーダーが info グレーに degrade して
    # 重大度の視覚差が消える。
    "info": "#5f6557",
    "warning": "#b8860b",
    "error": "#a8341b",
    "critical": "#a8341b",
}
_DEFAULT_SEVERITY_COLOR = "#5f6557"

# link_target(category 相当) → ナビ遷移先 (ページ名, カテゴリグループ名)。
# app.py `_W134_GROUPS` (2026-07-02 リネーム後) 準拠。
# order / action_required / pricing / default は対応する専用タブが無いため
# マップ対象外 (None = 「開く」ボタン非表示、DASHBOARD 内表示のみ)。
LINK_TARGET_MAP: dict[str, tuple[str, str]] = {
    "keyword": ("キーワード新着監視", "⚲ リサーチ"),
    "inventory": ("在庫監視", "★ 毎日"),
    "rival": ("最安値チェック", "⚲ リサーチ"),
    "system": ("システム運用", "⛭ 運用"),
    "research": ("リサーチ脳", "⚲ リサーチ"),
}


def get_nav_target(link_target: str) -> Optional[tuple[str, str]]:
    """link_target 値 → (ページ名, グループ名)。マップ外は None。"""
    return LINK_TARGET_MAP.get((link_target or "").strip())


def category_emoji(category: str) -> str:
    return CATEGORY_EMOJI.get((category or "").strip(), _DEFAULT_EMOJI)


def category_label_ja(category: str) -> str:
    c = (category or "").strip()
    if not c:
        return "その他"
    return CATEGORY_LABEL_JA.get(c, c)


def severity_color(severity: str) -> str:
    return SEVERITY_COLOR.get((severity or "").strip().lower(), _DEFAULT_SEVERITY_COLOR)


# ---------------------------------------------------------------------------
# 時刻表示 (UTC created_at → JST 相対/絶対)
# ---------------------------------------------------------------------------

def _parse_utc(ts: str) -> Optional[datetime]:
    """SQLite TIMESTAMP 文字列 (UTC naive、SQL ``datetime('now')`` 由来) を
    aware(UTC) datetime に変換。parse 不能は None (呼び出し側で degrade)。"""
    if not ts:
        return None
    s = str(ts).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def relative_time_jst(created_at: str, now_utc: Optional[datetime] = None) -> str:
    """created_at (UTC 文字列) → JST 基準の相対表示。

    'たった今' / 'N分前' / 'N時間前' / '昨日' (JST 日跨ぎ 1 日以内) /
    'N日前' (7 日未満) / 'M/D' (JST 絶対、7 日以上)。
    parse 不能時は空文字列。
    """
    dt = _parse_utc(created_at)
    if dt is None:
        return ""
    now = now_utc or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "たった今"
    if secs < 3600:
        return f"{int(secs // 60)}分前"
    if secs < 86400:
        return f"{int(secs // 3600)}時間前"
    # JST 基準の日跨ぎ判定 (「昨日」表示)
    jst = timezone(timedelta(hours=9))
    dt_jst_date = dt.astimezone(jst).date()
    now_jst_date = now.astimezone(jst).date()
    day_diff = (now_jst_date - dt_jst_date).days
    if day_diff == 1:
        return "昨日"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}日前"
    dt_jst = dt.astimezone(jst)
    return f"{dt_jst.month}/{dt_jst.day}"


def is_within_days(created_at: str, days: int, now_utc: Optional[datetime] = None) -> bool:
    """created_at (UTC 文字列) が現在から ``days`` 日以内かどうか。parse 不能時は False。"""
    dt = _parse_utc(created_at)
    if dt is None:
        return False
    now = now_utc or datetime.now(timezone.utc)
    return 0 <= (now - dt).total_seconds() <= days * 86400


# ---------------------------------------------------------------------------
# 内部変数露出 → 業務語変換 (render 時 humanize、2026-07-04 依頼ボード #39 差戻し)
# ---------------------------------------------------------------------------
#
# notification_log の実データ棚卸し (2026-07-04 SELECT) で頻出した内部ジャーゴン
# 露出パターン: "W153 truncation" / "W153 新規ライバル検出" / "W301 rival_classify
# 要確認 (processed=N, issues=M)" / "(W139)" 系の W 番号 suffix。発行側 (tasks/)
# の文言修正は既存 DB レコードには遡及しないため、表示側で吸収する。

# 既知パターン: (正規表現, title→(new_title, new_body|None) ビルダー)。
# new_body=None は「本文は fallback jargon-strip のみ適用 (既存本文は保持)」を
# 意味する (W301 のように detail 本文全体が内部ログのため空にしたい場合は
# ビルダーが "" を返す)。
_KNOWN_NOTIFICATION_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], tuple[str, Optional[str]]]]] = [
    (
        re.compile(r"W301 rival_classify 要確認\s*\(processed=(\d+),\s*issues=(\d+)\)"),
        lambda m: (
            f"AI店長: {m.group(2)} 件が要確認判定 (Shadow 運用中・対応不要)",
            "",
        ),
    ),
    (
        re.compile(
            r"W153 truncation:?\s*監視 ON listing が (\d+) 件あり "
            r"max_listings_per_run=(\d+) を超えています。?\s*今回 (\d+) 件 skip"
        ),
        lambda m: (
            f"最安値チェック: 監視対象 {m.group(1)} 件が処理上限 {m.group(2)} 件を超過、"
            f"今回 {m.group(3)} 件が未処理",
            "商品管理タブで監視 ON の件数を絞ると解消します (対応不要でも可)",
        ),
    ),
    (
        re.compile(r"W153 新規ライバル検出\s*\((\d+) listings\)"),
        lambda m: (
            f"新規ライバル出品を検出 ({m.group(1)} 件)",
            # 2026-07-04 diff 差戻し: 姉妹パターン (W301/W153 truncation) と同様に
            # body="" で情報を捨てる (タイトルで情報完結)。旧 None は fallback へ
            # 委譲していたが、絵文字プレフィックス始まりの本文で `^W\d+` アンカーが
            # 効かず生ログが漏れ表示される不具合の直接原因になっていた。
            "",
        ),
    ),
]

# fallback: 未知パターンの W 番号 / 変数=値 トークンだけを機械的に剥がす。
# 文字列中の任意位置ではなく「先頭の 'W123 '」「'(W123)' / '(W123-foo)'」に限定して
# マッチさせる (real DB の全既知例がこの 2 形のいずれかで、商品タイトル中の型番
# 例: "DSC-W800" 等を誤って破壊しないための安全側マージン)。
_W_NUM_PAREN_RE = re.compile(r"\(W\d{2,4}(?:-[A-Za-z0-9]+)?\)")
_W_NUM_PREFIX_RE = re.compile(r"^W\d{2,4}\s+")
# "processed=868" 等の identifier=数値 トークン。値の直後が英数字/./- でない
# ことを要求し ("band=1-2kg" のような複合値の部分破壊を防ぐ)。
_VAR_EQ_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*=[+-]?\d+(?:\.\d+)?(?![\w.-])")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_DANGLE_CHARS = " \t-:・,、"

# 2026-07-04 差戻し 2 段目: 絵文字/記号プレフィックスを剥がしてから W 番号アンカー
# を効かせるための前段クリーナ。real DB では発行側 (tasks/) がタイトルに絵文字を
# 直付けする慣例 ("🎯 W153 …" / "⚠️ W153 …" / "🔔 …") のため、単純な `^W\d+`
# アンカーだけでは頭文字が絵文字/変体記号のケースを取りこぼす。
# 対象文字: 各種 emoji (Unicode の絵文字ブロック) / 全角/半角空白 / 太字マーカー
# 残骸 (`*` は _strip_md で除去済のはずだが二重防御) / 変体セレクタ (U+FE0E/FE0F)
# / 主要な記号系絵文字プレフィックス (⚠ ⚔ 等)。
# 先頭デコレーション文字クラス。`W\d+` に到達するまでの skip 用 (単独では消さない、
# lookahead で W 番号が続くときに限り剥がす — 「🛒 商品が売れました」等の legitimate
# 絵文字プレフィックスを不必要に破壊しない安全側)。
_DECOR_CLASS = (
    r"["
    r"\U0001F300-\U0001FAFF"  # 主要 emoji 平面
    r"☀-➿"          # 記号絵文字 (⚠ ⚔ ⛭ ★ ☆ 等 = ☀-➿ 相当)
    r"︎️"           # 異体字セレクタ (VS15/VS16)
    r"‍"                 # ZWJ
    r"\s·・:*_\-"             # 空白/中黒/コロン/記号
    r"]"
)
# 「先頭デコレーション + W\d」→ W\d 露出。W が続かない場合は無変更。
_LEADING_DECOR_BEFORE_W_RE = re.compile(rf"^{_DECOR_CLASS}+(?=W\d)")
# "(2 listings)" / "(3 listings)" — 英語トークンを業務語に置換 (発行側 tasks/ 修正
# が過去分に届かない DB レコード対策)。数字は保持。
_LISTINGS_PAREN_RE = re.compile(r"\((\d+)\s+listings?\)", re.IGNORECASE)


def _strip_internal_jargon(s: str) -> str:
    """未知パターン向け fallback: W 番号 / 変数=値 トークンを除去して整形。

    2026-07-04 差戻し 2 段目:
    - 先頭の絵文字/記号を一旦剥がしてから `^W\\d+` を適用 → 絵文字プレフィックス
      始まり ("🎯 W153 ...") でも W 番号が確実に取れる。剥がした prefix は破棄
      (通知1行に絵文字を残しても情報密度は上がらない)。
    - `(N listings)` → `(N 件)` 置換で英語トークンの残存も業務語化。
    """
    if not s:
        return s
    # 1. 先頭デコレーション + W\d\+ の隣接時のみ剥がす (絵文字が W 番号を隠している
    #    ケース救済、legitimate emoji-only prefix は不変)。
    s = _LEADING_DECOR_BEFORE_W_RE.sub("", s, count=1)
    # 2. `(N listings)` → `(N 件)` に変換 (既知パターン外の英語トークン救済)。
    s = _LISTINGS_PAREN_RE.sub(lambda m: f"({m.group(1)} 件)", s)
    # 3. W 番号 / 変数=値 の除去。
    s = _W_NUM_PAREN_RE.sub("", s)
    s = _W_NUM_PREFIX_RE.sub("", s)
    s = _VAR_EQ_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    return s.strip(_DANGLE_CHARS).strip()


def humanize_notification_text(category: str, title: str, body: str) -> tuple[str, str]:
    """内部変数露出タイトル/本文を業務語 + 対応要否へ変換する (render 時)。

    Args:
        category: notif["category"] (現状マッチングには未使用、将来のカテゴリ限定
            パターン追加に備えて引数として保持)。
        title: markdown 装飾除去済のタイトル。
        body: markdown 装飾除去済の本文。

    Returns:
        (title, body) — 既知パターンにマッチすればビジネス向け文言に置換、
        マッチしなければ fallback (W 番号 / 変数=値 トークンのみ除去) を適用。
    """
    for pattern, builder in _KNOWN_NOTIFICATION_PATTERNS:
        m = pattern.search(title)
        if not m and body:
            m = pattern.search(body)
        if m:
            new_title, new_body = builder(m)
            if new_body is None:
                new_body = _strip_internal_jargon(body)
            return new_title, new_body
    return _strip_internal_jargon(title), _strip_internal_jargon(body)


# ---------------------------------------------------------------------------
# コンパクト 1 行 HTML
# ---------------------------------------------------------------------------

_NC_CSS = """
<style>
.nc-line{
  display:flex;
  align-items:center;
  flex-wrap:nowrap;
  gap:8px;
  padding:5px 10px;
  margin:2px 0;
  background:#f2ecdf;
  border-radius:0 4px 4px 0;
  border:1px solid rgba(14,79,75,0.10);
  border-left:3px solid var(--nc-accent, rgba(14,79,75,0.35));
  font-size:12px;
  line-height:1.3;
  min-height:24px;
  max-height:26px;
  overflow:hidden;
  white-space:nowrap;
}
.nc-line.nc-unread{background:#ede7da;}
.nc-line.nc-read{opacity:0.62;}
.nc-emoji{font-size:14px;flex-shrink:0;line-height:1;}
.nc-title{
  font-weight:600;
  color:#2a2e2a;
  max-width:32ch;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
  flex-shrink:1;
  min-width:0;
}
.nc-sep{color:#8d927f;flex-shrink:0;}
.nc-sub{
  color:#5f6557;
  font-size:11px;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
  flex:1 1 auto;
  min-width:0;
}
.nc-time{
  color:#8d927f;
  font-size:11px;
  margin-left:auto;
  padding-left:8px;
  flex-shrink:0;
  font-family:'JetBrains Mono','Consolas',monospace;
  white-space:nowrap;
}
</style>
"""

# タイトル/補足の最大長 (仕上げ 2026-07-04: 実機 QA で 3-4 行折り返しが確認された
# ため、CSS の nowrap+ellipsis に加えて文字数上限も強めに truncate。フル文字列は
# `title` 属性で hover 表示するため情報は失わない)。
_TITLE_MAX = 45
_BODY_MAX = 60


def render_notification_row_html(
    notif: dict,
    now_utc: Optional[datetime] = None,
    include_css: bool = False,
) -> str:
    """1 通知行の HTML 文字列 (コンパクト 1 行) を返す。

    Args:
        notif: ``get_notifications()`` の 1 dict (id/category/severity/title/body/
            link_target/link_ref/discord_sent/created_at/read_at)。
        now_utc: テスト用に現在時刻を固定注入 (省略時 ``datetime.now(UTC)``)。
        include_css: True で ``<style>`` を同梱 (``_supplier_card_html`` の
            ``include_css`` パターン踏襲、既定 False)。

    レイアウト:
        [severity 左ボーダー] 絵文字 太字タイトル — 補足(グレー) [右寄せ 時刻]

    ボタン (開く / ✓既読) は本関数の外で ``st.columns`` により同一 Streamlit
    row の右端に小型配置する (HTML には含めない)。
    """
    category = notif.get("category") or ""
    severity = notif.get("severity") or "info"
    title = (notif.get("title") or "").strip()
    body = (notif.get("body") or "").strip()
    created_at = notif.get("created_at") or ""
    is_unread = notif.get("read_at") in (None, "")

    emoji = category_emoji(category)
    color = severity_color(severity)
    rel = relative_time_jst(created_at, now_utc)

    # 仕上げ 2026-07-04: 実機で `**` マーカーがリテラル表示 + title と body が同文で
    # 「title — body」として二重表示されていた。以下 2 点で正規化:
    #   1. markdown 装飾マーカー (`**`, `__`, backtick) を strip して純テキスト化。
    #      1 行 HTML レンダラー経路のため markdown はそもそも parse されない = リテラル
    #      露出のみが害になる (Q7 情報密度: 装飾記号の視覚ノイズを排除)。
    #   2. body が title と同一 / title で前方一致 / title の部分文字列の場合は body を
    #      空扱いにして "— " セパレータごと消す (実機二重表示根治)。
    def _strip_md(s: str) -> str:
        if not s:
            return s
        return (
            s.replace("**", "")
             .replace("__", "")
             .replace("`", "")
             .strip()
        )
    title = _strip_md(title)
    body = _strip_md(body)
    # 2026-07-04 依頼ボード #39 差戻し: DB 保存済みの古い通知が内部変数露出の
    # まま表示され続ける問題を render 時変換で根治 (発行側修正は過去分に遡及しない)。
    title, body = humanize_notification_text(category, title, body)
    if body and title:
        if body == title or body.startswith(title) or title.startswith(body) or body in title:
            body = ""

    # タイトル文字数制限 (…で省略)。空タイトルは 1 行レイアウトが崩れるため、
    # カテゴリ日本語名で埋める (S1 は非空 title を必須としているため通常はここに
    # 到達しない)。
    if not title:
        title = category_label_ja(category)
    title_disp = title if len(title) <= _TITLE_MAX else (title[: _TITLE_MAX - 1] + "…")
    body_disp = body if len(body) <= _BODY_MAX else (body[: _BODY_MAX - 1] + "…")

    line_cls = "nc-line nc-unread" if is_unread else "nc-line nc-read"
    sep_html = '<span class="nc-sep">—</span>' if body_disp else ""
    sub_html = (
        f'<span class="nc-sub">{_esc(body_disp)}</span>' if body_disp else ""
    )
    time_html = f'<span class="nc-time">{_esc(rel)}</span>' if rel else ""

    # hover 用のフル文字列 tooltip (truncate で消えた情報を復元)。
    _tt_parts = [title]
    if body:
        _tt_parts.append(body)
    _tt = _esc(" — ".join(p for p in _tt_parts if p))
    line_html = (
        f'<div class="{line_cls}" style="--nc-accent:{color};" title="{_tt}">'
        f'<span class="nc-emoji">{emoji}</span>'
        f'<span class="nc-title">{_esc(title_disp)}</span>'
        f'{sep_html}{sub_html}{time_html}'
        f'</div>'
    )
    return (_NC_CSS if include_css else "") + line_html
