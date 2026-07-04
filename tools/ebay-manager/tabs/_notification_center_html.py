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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as _esc
from typing import Optional


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
