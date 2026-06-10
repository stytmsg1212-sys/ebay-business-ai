"""Promoted Listings 2% 登録失敗の Discord 通知 回帰テスト (2026-06-08).

出典: 6/4-6/8 にeBayトークン破損で REST API が Access denied → 個別出品時の
2% 自動登録が UI 表示だけで静かに失敗し続け、user が気付けなかった事故。
再発防止に enroll 失敗時 Discord 通知を追加 (tab_individual_listing._notify_pls_enroll_failure)。

検証: 失敗時にメッセージ送信される / item_id・理由が含まれる / 通知自体の失敗は
握り潰して出品フローに影響させない (Q0)。
"""
import sys
import types

import tabs.tab_individual_listing as til


class _CapturingNotifier:
    sent = []

    def __init__(self, webhook_url, *, bypass_env=False):
        self.webhook_url = webhook_url

    def send_message(self, message, embed=None):
        _CapturingNotifier.sent.append(message)
        return True


class _RaisingNotifier:
    def __init__(self, webhook_url, *, bypass_env=False):
        pass

    def send_message(self, message, embed=None):
        raise RuntimeError("discord down")


def _install_notifier(monkeypatch, cls):
    """notifiers.discord_notifier.DiscordNotifier を差し替える (lazy import 対策)。"""
    mod = types.ModuleType("notifiers.discord_notifier")
    mod.DiscordNotifier = cls
    monkeypatch.setitem(sys.modules, "notifiers.discord_notifier", mod)


def test_notify_sends_with_item_id_and_reason(monkeypatch):
    _CapturingNotifier.sent = []
    _install_notifier(monkeypatch, _CapturingNotifier)
    til._notify_pls_enroll_failure(
        {"discord": {"webhook_url": "https://x/y"}},
        ebay_item_id="357414236596",
        errors=["1100: Access denied"],
    )
    assert len(_CapturingNotifier.sent) == 1
    msg = _CapturingNotifier.sent[0]
    assert "357414236596" in msg
    assert "Access denied" in msg
    assert "Promoted Listings" in msg


def test_notify_swallows_send_exception(monkeypatch):
    """通知の送信失敗は例外を伝播させない (出品フロー本体を壊さない / Q0)。"""
    _install_notifier(monkeypatch, _RaisingNotifier)
    # 例外が漏れないこと
    til._notify_pls_enroll_failure(
        {"discord": {}},
        ebay_item_id="123",
        errors=["boom"],
    )


def test_notify_handles_empty_errors(monkeypatch):
    _CapturingNotifier.sent = []
    _install_notifier(monkeypatch, _CapturingNotifier)
    til._notify_pls_enroll_failure({}, ebay_item_id="999", errors=[])
    assert len(_CapturingNotifier.sent) == 1
    assert "999" in _CapturingNotifier.sent[0]


def test_enroll_failure_is_wired_to_notify():
    """配線保証 (AST): enroll の失敗/例外パスから _notify_pls_enroll_failure が
    呼ばれること。今回の事故本質は「失敗が UI 表示だけで通知に繋がっていなかった」点
    なので、呼び出しが2箇所以上存在することを静的に固定する (else分岐 + except分岐)。"""
    import ast
    import inspect

    src = inspect.getsource(til)
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_notify_pls_enroll_failure"
    ]
    assert len(calls) >= 2, (
        f"_notify_pls_enroll_failure の呼び出しが {len(calls)} 箇所 "
        "(失敗分岐 + 例外分岐の2箇所必須)"
    )
