"""依頼ボード #39 Phase A 実機 0 点 fb 対応 (2026-07-03):
`tasks.task_keyword_watch_crawl._send_discord_for_hit` が DASHBOARD 通知センター
向けに **意味のある title/body/link_target/link_ref** を choke point
(`notifiers.notification_center.record_and_maybe_send`) 経由で渡していること
を静的検証 + monkeypatch で確認する。

対象 fb:
- 旧: title = "🔔 キーワード新着 (🔨 ヤフオク) hit_id=3825743" (内部 ID のみ)
- 新: title = "🔨 <商品タイトル>"
      body  = "¥<価格> | キーワード: <kw> | <サイト名>"
      link_target = "keyword"
      link_ref = 商品 URL
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _StubHit:
    """CrawlHit スタブ (task_keyword_watch_crawl._send_discord_for_hit 引数)."""
    def __init__(self, title, url, price_jpy, image_url=None):
        self.title = title
        self.url = url
        self.price_jpy = price_jpy
        self.image_url = image_url


# ---------------------------------------------------------------------------
# monkeypatch: record_and_maybe_send の呼び出し引数を検証
# ---------------------------------------------------------------------------

def test_send_discord_for_hit_routes_meaningful_title_via_choke_point():
    """record_and_maybe_send に意味のある title/body/link_target/link_ref が渡ること。"""
    from tasks.task_keyword_watch_crawl import _send_discord_for_hit

    watch = {
        'id': 42,
        'site': 'yahoo',
        'keyword': 'ライカ M6',
        'memo': 'JP seller only',
        'price_min_jpy': 10000,
        'price_max_jpy': 50000,
        'ebay_item_id': '123456789012',
        '_ebay_price': 350.0,
        '_ebay_title': 'Leica M6 Classic Rangefinder Camera',
    }
    hit = _StubHit(
        title='Leica M6 Classic レンジファインダー カメラ',
        url='https://auctions.yahoo.co.jp/xxx/yyy',
        price_jpy=30000,
        image_url='https://example.com/x.jpg',
    )

    called = {}

    def fake_record(**kwargs):
        called.update(kwargs)
        return {"notification_id": 100, "discord_sent": True, "gated": False,
                "deduped": False, "severity_bypassed": False}

    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        ok = _send_discord_for_hit("https://example.com/webhook", watch, hit, hit_id=3825743)

    assert ok is True
    # 意味のある title (商品名が主、内部 ID なし)
    assert 'hit_id=' not in called['title']
    assert 'Leica M6' in called['title']
    # 絵文字接頭辞
    assert called['title'].startswith('🔨 '), f"title should start with 🔨: {called['title']!r}"
    # body に価格 + キーワード + サイト名
    assert '¥30,000' in called['body']
    assert 'キーワード: ライカ M6' in called['body']
    assert 'ヤフオク' in called['body']
    # link_target / link_ref
    assert called['link_target'] == 'keyword'
    assert called['link_ref'] == 'https://auctions.yahoo.co.jp/xxx/yyy'
    # embed は Discord POST 用に渡っている (embed['title'] は既存の「新着: 商品名」形式)
    assert called['embed'] is not None
    assert 'Leica M6' in called['embed']['title']


def test_send_discord_for_hit_mercari_uses_shopping_emoji():
    """メルカリ hit は 🛒 絵文字接頭辞。"""
    from tasks.task_keyword_watch_crawl import _send_discord_for_hit

    watch = {
        'id': 7, 'site': 'mercari', 'keyword': 'ローランド JD-XA',
        'memo': '', 'price_min_jpy': None, 'price_max_jpy': None,
        'ebay_item_id': None,
    }
    hit = _StubHit(
        title='Roland JD-XA シンセサイザー',
        url='https://jp.mercari.com/item/m12345',
        price_jpy=80000,
    )
    called = {}
    def fake_record(**kwargs):
        called.update(kwargs)
        return {"discord_sent": True}
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        _send_discord_for_hit("https://example.com/webhook", watch, hit, hit_id=1)
    assert called['title'].startswith('🛒 ')
    assert 'メルカリ' in called['body']


def test_send_discord_for_hit_missing_price_shows_placeholder():
    """price_jpy=None でも body に「(価格不明)」が入る (silent 空文字にしない)。"""
    from tasks.task_keyword_watch_crawl import _send_discord_for_hit

    watch = {
        'id': 1, 'site': 'yahoo', 'keyword': 'テスト',
        'memo': '', 'price_min_jpy': None, 'price_max_jpy': None,
    }
    hit = _StubHit(title='テスト商品', url='https://y.example/x', price_jpy=None)
    called = {}
    def fake_record(**kwargs):
        called.update(kwargs)
        return {"discord_sent": True}
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        _send_discord_for_hit("https://example.com/webhook", watch, hit, hit_id=1)
    assert '(価格不明)' in called['body']


def test_send_discord_for_hit_untitled_hit_fallback():
    """hit.title 空でも title fallback「(タイトル未取得)」で意味不明ベル絵文字だけの通知を防ぐ。"""
    from tasks.task_keyword_watch_crawl import _send_discord_for_hit

    watch = {
        'id': 1, 'site': 'yahoo', 'keyword': 'テスト',
        'memo': '', 'price_min_jpy': None, 'price_max_jpy': None,
    }
    hit = _StubHit(title='', url='https://y.example/x', price_jpy=1000)
    called = {}
    def fake_record(**kwargs):
        called.update(kwargs)
        return {"discord_sent": True}
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        _send_discord_for_hit("https://example.com/webhook", watch, hit, hit_id=1)
    assert '(タイトル未取得)' in called['title']


# ---------------------------------------------------------------------------
# 静的検証: 旧 "hit_id=" 内部 ID 文字列が title 生成経路に残っていないこと
# ---------------------------------------------------------------------------

def test_keyword_crawl_source_no_hit_id_in_notification_title():
    """通知 title 生成に hit_id を埋め込んでいないこと (logger.exception の
    デバッグ文脈で hit_id を残すのは正常なので guard は record_and_maybe_send
    呼び出しの title kwarg に限定)。"""
    src = (PROJECT_ROOT / "tasks" / "task_keyword_watch_crawl.py").read_text(encoding="utf-8")
    # record_and_maybe_send を choke point 経由で使っていること
    assert "record_and_maybe_send" in src
    # AST で record_and_maybe_send 呼び出しの title kwarg に hit_id が入っていないこと。
    # コメント/docstring 内の説明文は AST では拾わないので安全に検証できる。
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "record_and_maybe_send":
            for kw in node.keywords:
                if kw.arg == "title":
                    seg = ast.get_source_segment(src, kw.value) or ""
                    assert "hit_id" not in seg, (
                        f"record_and_maybe_send(title=...) に hit_id が入っている: {seg!r}"
                    )
                    checked += 1
    assert checked >= 1, "record_and_maybe_send(title=...) 呼び出しが見つからない"


# ---------------------------------------------------------------------------
# discord_notifier.send_message: embed['title'] が title fallback で最優先
# ---------------------------------------------------------------------------

def test_discord_notifier_prefers_embed_title_over_message():
    """DiscordNotifier.send_message: embed['title'] が空でない場合、record 側 title に採用。

    これで「hit_id=... のような内部 ID 文字列が content に来ても、embed['title']
    (意味のある商品名) が優先される」ことを保証する。
    """
    from notifiers.discord_notifier import DiscordNotifier

    captured = {}
    def fake_record(category, severity, title, body, **kwargs):
        captured.update(dict(
            category=category, severity=severity, title=title, body=body, **kwargs))
        return {"notification_id": 1, "discord_sent": True, "gated": False,
                "deduped": False, "severity_bypassed": False}

    n = DiscordNotifier("https://example.com/webhook", bypass_env=True,
                        category="keyword")
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        ok = n.send_message(
            "🔔 内部 hit_id=3825743",
            embed={"title": "🔨 ヤフオク 新着: Leica M6 Classic",
                    "description": "詳細説明"},
        )
    assert ok is True
    assert 'Leica M6' in captured['title']
    assert 'hit_id=' not in captured['title']


def test_discord_notifier_fallback_uses_embed_description_when_no_title():
    """embed['title'] 空 → embed['description'] 先頭 80 字を title に採用。"""
    from notifiers.discord_notifier import DiscordNotifier

    captured = {}
    def fake_record(category, severity, title, body, **kwargs):
        captured.update(dict(title=title, body=body))
        return {"discord_sent": True}

    n = DiscordNotifier("https://example.com/webhook", bypass_env=True,
                        category="system")
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        n.send_message("", embed={"description": "検知: 在庫切れ 3 件"})
    assert '検知' in captured['title']


def test_discord_notifier_final_fallback_no_title():
    """すべて空でも '(no title)' を確実に入れて INSERT 失敗させない。"""
    from notifiers.discord_notifier import DiscordNotifier

    captured = {}
    def fake_record(category, severity, title, body, **kwargs):
        captured.update(dict(title=title))
        return {"discord_sent": True}

    n = DiscordNotifier("https://example.com/webhook", bypass_env=True,
                        category="default")
    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        n.send_message("", embed={})
    assert captured['title'] == '(no title)'


def test_py_compile_all_touched_files():
    import py_compile
    for rel in [
        "tabs/_notification_center_html.py",
        "tabs/tab_dashboard.py",
        "notifiers/discord_notifier.py",
        "tasks/task_keyword_watch_crawl.py",
    ]:
        py_compile.compile(str(PROJECT_ROOT / rel), doraise=True)
