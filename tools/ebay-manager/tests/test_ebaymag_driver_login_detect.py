"""ebaymag_driver: ログインリダイレクト検知の回帰テスト (2026-06-29).

_open_panel_and_check_itm がセッション切れ時に res.error へ
適切なメッセージをセットして None を返すことを固定化。
"""
import monitor.ebaymag_driver as D
from monitor.ebaymag_driver import _open_panel_and_check_itm, EbaymagResult


def test_login_redirect_sets_session_error(monkeypatch):
    """ログインページへのリダイレクト → res.error にセッション切れメッセージ + None 返却."""
    login_info = {
        "url": "https://ebaymag.com/login?redirect_to=%2Fstock%3FproductId%3D718746500",
        "title": "",
        "itm": None,
        "hasAction": False,
        "head": "",
    }
    monkeypatch.setattr(D, "_read_panel", lambda page, url: login_info)

    res = EbaymagResult()
    result = _open_panel_and_check_itm(
        None, "https://ebaymag.com/stock?productId=718746500", "356371379534", res
    )

    assert result is None, "ログインリダイレクト時は None を返すべき"
    assert res.error is not None, "res.error がセットされていない"
    assert "セッション切れ" in res.error, f"セッション切れメッセージが含まれない: {res.error!r}"
    assert "9222" in res.error, f"CDP ポート番号 9222 が含まれない: {res.error!r}"


def test_normal_stock_url_not_flagged_as_login(monkeypatch):
    """通常の stock URL は login リダイレクト扱いしない (誤検知防止)."""
    normal_info = {
        "url": "https://ebaymag.com/stock?productId=718746500",
        "title": "Test Product",
        "itm": "356371379534",
        "hasAction": True,
        "head": "",
    }
    monkeypatch.setattr(D, "_read_panel", lambda page, url: normal_info)

    res = EbaymagResult()
    result = _open_panel_and_check_itm(
        None, "https://ebaymag.com/stock?productId=718746500", "356371379534", res
    )

    assert result is not None, "正常 URL では None を返してはいけない"
    assert res.error is None, f"正常時に res.error がセットされた: {res.error!r}"


def test_itm_mismatch_returns_none_and_sets_error(monkeypatch):
    """T3: itm が expected と不一致 → None / res.error に「itm 照合 NG」を含む (誤商品防止)."""
    wrong_itm_info = {
        "url": "https://ebaymag.com/stock?productId=718746500",
        "title": "Wrong Product",
        "itm": "999999999999",
        "hasAction": True,
        "head": "",
    }
    monkeypatch.setattr(D, "_read_panel", lambda page, url: wrong_itm_info)

    res = EbaymagResult()
    result = _open_panel_and_check_itm(
        None, "https://ebaymag.com/stock?productId=718746500", "356371379534", res
    )

    assert result is None, "itm 不一致時は None を返すべき"
    assert res.error is not None, "res.error がセットされていない"
    assert "itm 照合 NG" in res.error, f"「itm 照合 NG」が含まれない: {res.error!r}"
