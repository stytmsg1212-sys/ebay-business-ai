"""依頼ボード #39 Phase A S4 (2026-07-03): DASHBOARD 通知センター UI 純関数 test.

対象: tabs/_notification_center_html.py (純描画部)
- severity 別色分け (info/warn/error/unknown)
- カテゴリ絵文字 / 日本語ラベル (既知 8 種 + fallback)
- link_target → ナビ遷移先マップ (keyword/inventory/rival/system/research のみ
  対象、order/action_required/pricing は None = DASHBOARD 内表示のみ)
- 相対時刻表示 (たった今/N分前/N時間前/N日前/M-D 絶対表示)
- is_within_days (7日既読フィルタの判定関数)
- render_notification_row_html の HTML escape (XSS 防御) / include_css toggle /
  未読 class / discord_sent バッジ

streamlit の実 render (widget 描画) は ScriptRunContext が無いと実行できないため、
純関数部分のみを直接呼んで検証する (_supplier_card_html テストと同方針)。
tab_dashboard.py の ImportError fallback は AST/ソース静的検証で守る。
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tabs._notification_center_html import (
    CATEGORY_EMOJI,
    LINK_TARGET_MAP,
    category_emoji,
    category_label_ja,
    get_nav_target,
    is_within_days,
    relative_time_jst,
    render_notification_row_html,
    severity_color,
)


# ---------------------------------------------------------------------------
# severity color
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("severity, expected", [
    # S1 whitelist (NOTIFICATION_SEVERITIES) の 4 値
    ("info", "#5f6557"),
    ("warning", "#b8860b"),
    ("error", "#a8341b"),
    ("critical", "#a8341b"),
    # 大文字小文字を無視
    ("WARNING", "#b8860b"),
    ("Critical", "#a8341b"),
    # 空文字 / 未知値は info 相当 (default)
    ("", "#5f6557"),
    ("unknown_severity", "#5f6557"),
    # 旧キー "warn" は誤り (S1 の whitelist は "warning") — default に落ちる
    ("warn", "#5f6557"),
])
def test_severity_color(severity, expected):
    assert severity_color(severity) == expected


def test_severity_color_covers_s1_whitelist():
    """S1 (monitor.notification_log_db.NOTIFICATION_SEVERITIES) の 4 値
    (info/warning/error/critical) を全て非-default 色でカバーしていること。

    whitelist と食い違うと左ボーダーが info グレーに degrade して重大度の
    視覚差が消える (統合レビュー MED 2026-07-03)。
    """
    from tabs._notification_center_html import SEVERITY_COLOR, _DEFAULT_SEVERITY_COLOR
    from monitor.notification_log_db import NOTIFICATION_SEVERITIES

    for sev in NOTIFICATION_SEVERITIES:
        assert sev in SEVERITY_COLOR, f"severity {sev!r} が SEVERITY_COLOR に無い"
    # warning / error / critical は info の default とは別色 (視覚差確保)
    assert SEVERITY_COLOR["warning"] != _DEFAULT_SEVERITY_COLOR
    assert SEVERITY_COLOR["error"] != _DEFAULT_SEVERITY_COLOR
    assert SEVERITY_COLOR["critical"] != _DEFAULT_SEVERITY_COLOR


# ---------------------------------------------------------------------------
# category emoji / label (8 種 + fallback)
# ---------------------------------------------------------------------------

def test_category_emoji_all_known_categories():
    # S1 whitelist (NOTIFICATION_CATEGORIES) の 9 値全てに絵文字マップがある。
    expected = {
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
    for cat, emoji in expected.items():
        assert category_emoji(cat) == emoji
    assert set(CATEGORY_EMOJI.keys()) == set(expected.keys())


def test_category_emoji_fallback_unknown():
    # 未知 / 空 は default 相当 (🔖) — 「その他」カテゴリと視覚一致。
    # ベル (🔔) は keyword カテゴリ専用にする (絵文字だけの意味不明行を防ぐ実機 fb)。
    assert category_emoji("nonexistent_category") == "🔖"
    assert category_emoji("") == "🔖"
    assert category_emoji(None) == "🔖"


def test_category_label_ja_known_and_fallback():
    assert category_label_ja("order") == "注文"
    assert category_label_ja("inventory") == "在庫"
    # default (S1 whitelist カテゴリ) は「その他」— 実機 fb「default (1)」根治
    assert category_label_ja("default") == "その他"
    # 未知カテゴリはカテゴリ文字列そのまま (完全なフォールバック消失を防ぐ)
    assert category_label_ja("mystery_cat") == "mystery_cat"
    # 空文字は「その他」
    assert category_label_ja("") == "その他"
    assert category_label_ja(None) == "その他"


# ---------------------------------------------------------------------------
# link_target → ナビ遷移先マップ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("link_target, expected", [
    ("keyword", ("キーワード新着監視", "⚲ リサーチ")),
    ("inventory", ("在庫監視", "★ 毎日")),
    ("rival", ("最安値チェック", "⚲ リサーチ")),
    ("system", ("システム運用", "⛭ 運用")),
    ("research", ("リサーチ脳", "⚲ リサーチ")),
])
def test_get_nav_target_mapped_categories(link_target, expected):
    assert get_nav_target(link_target) == expected


@pytest.mark.parametrize("link_target", ["order", "action_required", "pricing", "", "unknown"])
def test_get_nav_target_unmapped_stays_none(link_target):
    """order/action_required/pricing は専用タブが無いため None (開くボタン非表示)。"""
    assert get_nav_target(link_target) is None


def test_link_target_map_only_has_5_entries():
    # order / action_required / pricing の 3 カテゴリは意図的に対象外
    assert len(LINK_TARGET_MAP) == 5


# ---------------------------------------------------------------------------
# 相対時刻表示
# ---------------------------------------------------------------------------

def test_relative_time_just_now():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    created = "2026-07-03 09:59:45"  # 15秒前
    assert relative_time_jst(created, now_utc=now) == "たった今"


def test_relative_time_minutes_ago():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    created = "2026-07-03 09:45:00"  # 15分前
    assert relative_time_jst(created, now_utc=now) == "15分前"


def test_relative_time_hours_ago():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    created = "2026-07-03 05:00:00"  # 5時間前
    assert relative_time_jst(created, now_utc=now) == "5時間前"


def test_relative_time_yesterday_jst_day_boundary():
    """JST 基準で 1 日前 (前日) は「昨日」。"""
    # JST 2026-07-03 12:00 (UTC 2026-07-03 03:00) 時点で
    # JST 2026-07-02 10:00 (UTC 2026-07-02 01:00) は前日 → 「昨日」
    now = datetime(2026, 7, 3, 3, 0, 0, tzinfo=timezone.utc)
    created = "2026-07-02 01:00:00"
    assert relative_time_jst(created, now_utc=now) == "昨日"


def test_relative_time_days_ago():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    created = "2026-06-30 10:00:00"  # 3日前 (JST 日跨ぎ)
    assert relative_time_jst(created, now_utc=now) == "3日前"


def test_relative_time_over_7days_shows_absolute_jst_date():
    now = datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)
    created = "2026-06-20 00:00:00"  # 20日前 → JST 絶対表示
    result = relative_time_jst(created, now_utc=now)
    assert result == "6/20"


def test_relative_time_unparseable_returns_empty():
    assert relative_time_jst("") == ""
    assert relative_time_jst("not-a-date") == ""
    assert relative_time_jst(None) == ""


def test_relative_time_future_clamped_to_just_now():
    """feed/DB 側の時計ズレで未来日付が来ても例外にならず 'たった今' 扱い。"""
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    created = "2026-07-03 10:05:00"  # 未来 (5分先)
    assert relative_time_jst(created, now_utc=now) == "たった今"


# ---------------------------------------------------------------------------
# is_within_days (既読7日フィルタ)
# ---------------------------------------------------------------------------

def test_is_within_days_true_for_recent():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    assert is_within_days("2026-06-28 10:00:00", 7, now_utc=now) is True


def test_is_within_days_false_for_old():
    now = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    assert is_within_days("2026-06-01 10:00:00", 7, now_utc=now) is False


def test_is_within_days_unparseable_returns_false():
    assert is_within_days("", 7) is False
    assert is_within_days("garbage", 7) is False


# ---------------------------------------------------------------------------
# render_notification_row_html
# ---------------------------------------------------------------------------

def _base_notif(**overrides) -> dict:
    row = {
        "id": 1,
        "category": "keyword",
        "severity": "warning",
        "title": "テスト通知",
        "body": "本文テキスト",
        "link_target": "keyword",
        "link_ref": None,
        "discord_sent": 0,
        "created_at": "2026-07-03 09:00:00",
        "read_at": None,
    }
    row.update(overrides)
    return row


def test_render_row_returns_compact_1line_with_expected_parts():
    """コンパクト 1 行レイアウト: nc-line クラス + タイトル + 補足 + 時刻。"""
    now = datetime(2026, 7, 3, 9, 30, 0, tzinfo=timezone.utc)
    html = render_notification_row_html(_base_notif(), now_utc=now)
    assert isinstance(html, str)
    assert 'class="nc-line nc-unread"' in html  # read_at=None → 未読
    assert "テスト通知" in html
    assert "本文テキスト" in html
    assert "30分前" in html
    # ベル単独行 (旧レイアウト) の残骸が入っていないこと
    assert 'class="nc-row' not in html
    # 「Discord 通知済」表示は廃止済 (2026-07-03 fb)
    assert "Discord 通知済" not in html


def test_render_row_include_css_toggle():
    html_with_css = render_notification_row_html(_base_notif(), include_css=True)
    html_without_css = render_notification_row_html(_base_notif(), include_css=False)
    assert "<style>" in html_with_css
    assert ".nc-line" in html_with_css
    assert "<style>" not in html_without_css


def test_render_row_read_notif_has_read_class_not_unread():
    """既読は nc-line nc-read クラス (nc-unread ではない)。"""
    html = render_notification_row_html(_base_notif(read_at="2026-07-03 09:10:00"))
    assert 'class="nc-line nc-read"' in html
    assert "nc-unread" not in html


def test_render_row_xss_escape():
    malicious = _base_notif(
        title="<script>alert(1)</script>",
        body="<img src=x onerror=alert(2)>",
    )
    html = render_notification_row_html(malicious)
    assert "<script>" not in html
    assert "<img src=x" not in html  # 実タグとして注入されていないこと
    assert "&lt;script&gt;" in html


def _extract_span(html: str, cls: str) -> str:
    """<span class="cls">...</span> の中身を抽出 (visible 部分だけを検査するため、
    2026-07-04 仕上げで追加された line div の title 属性 (フル文字列 hover) を
    アサーションから除外する)。"""
    import re
    m = re.search(rf'<span class="{cls}">(.*?)</span>', html, re.DOTALL)
    return m.group(1) if m else ""


def test_render_row_title_truncated_over_45_chars():
    """タイトル > 45 字は … で省略 (2026-07-04 仕上げで 60→45 に強化。実機 QA で
    3-4 行折り返しが発生したため CSS の nowrap+ellipsis に加えて文字数上限も
    強めに truncate。フル文字列は line div の title 属性で hover 表示)。"""
    long_title = "あ" * 100
    html = render_notification_row_html(_base_notif(title=long_title))
    title_visible = _extract_span(html, "nc-title")
    # 44 文字 + … で 45 文字 (nc-title の overflow を単独行で確実に収める)
    assert "あ" * 44 in title_visible
    assert "あ" * 100 not in title_visible
    assert "…" in title_visible


def test_render_row_body_truncated_over_60_chars():
    """補足 (body) > 60 字は … で省略 (2026-07-04 仕上げで 80→60 に強化)。"""
    long_body = "本文" + ("あ" * 200)
    html = render_notification_row_html(_base_notif(body=long_body))
    body_visible = _extract_span(html, "nc-sub")
    assert "…" in body_visible
    # 100 文字は入りきらない (visible span 内で)
    assert ("あ" * 100) not in body_visible


def test_render_row_no_discord_sent_badge_regardless_of_flag():
    """『Discord 通知済』行は廃止 (実機 fb ノイズ)。discord_sent=1 でも非表示。"""
    html_sent = render_notification_row_html(_base_notif(discord_sent=1))
    html_not_sent = render_notification_row_html(_base_notif(discord_sent=0))
    assert "Discord 通知済" not in html_sent
    assert "Discord 通知済" not in html_not_sent


def test_render_row_severity_accent_color():
    html_error = render_notification_row_html(_base_notif(severity="error"))
    assert "#a8341b" in html_error
    html_warning = render_notification_row_html(_base_notif(severity="warning"))
    assert "#b8860b" in html_warning
    html_critical = render_notification_row_html(_base_notif(severity="critical"))
    assert "#a8341b" in html_critical


def test_render_row_empty_title_fallback_to_category_label():
    """title 空でも 1 行レイアウトが崩れないよう、カテゴリ日本語名 fallback。"""
    html = render_notification_row_html(_base_notif(title=""))
    # keyword カテゴリ → 「キーワード新着」
    assert "キーワード新着" in html


# ---------------------------------------------------------------------------
# tab_dashboard.py: ImportError fallback (静的検証)
# ---------------------------------------------------------------------------

def test_tab_dashboard_notification_center_has_importerror_fallback():
    """notification_log_db 未整備時に「準備中」caption で fallback する try/except
    (ImportError, sqlite3.Error) が render_dashboard_tab 内に存在すること。"""
    src_path = PROJECT_ROOT / "tabs" / "tab_dashboard.py"
    source = src_path.read_text(encoding="utf-8")
    assert "from monitor.notification_log_db import" in source
    assert "except (ImportError, sqlite3.Error)" in source
    assert "準備中" in source

    tree = ast.parse(source, filename=str(src_path))
    found_try_with_notification_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            body_src = ast.get_source_segment(source, node) or ""
            if "notification_log_db" in body_src and "ImportError" in body_src:
                found_try_with_notification_import = True
                break
    assert found_try_with_notification_import, (
        "notification_log_db import を含む try/except ImportError ブロックが見つからない"
    )


def test_tab_dashboard_py_compiles():
    import py_compile
    py_compile.compile(
        str(PROJECT_ROOT / "tabs" / "tab_dashboard.py"), doraise=True
    )


def test_notification_center_html_py_compiles():
    import py_compile
    py_compile.compile(
        str(PROJECT_ROOT / "tabs" / "_notification_center_html.py"), doraise=True
    )


# ---------------------------------------------------------------------------
# app.py: _BADGE_MAP に DASHBOARD が追加されていること (静的検証)
# ---------------------------------------------------------------------------

def test_app_py_badge_map_has_dashboard_entry():
    src_path = PROJECT_ROOT / "app.py"
    source = src_path.read_text(encoding="utf-8")
    assert '"DASHBOARD": "notifications_unread"' in source
    # 既存の .get(_badge_key, 0) 流儀 (KeyError 非依存) が維持されていること
    assert "_nav_badges.get(_badge_key, 0)" in source
