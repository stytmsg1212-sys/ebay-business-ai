"""set_values の read-back 検証回帰テスト (W284, 2026-06-22)。

canary で「正しく設定できているのに毎回 FAIL」する偽の失敗 (shippingCost が dict
形式なのに int 直比較) を修正。dict 形式の value/currency 照合、DOMESTIC service の
対称取得、excludedCountries 封じ込め検証 (自国以外全除外) を固定する。
"""
import pytest

from monitor import ebaymag_assign as A

UNIVERSE = ["CA", "US", "GB"]  # CA managed なら excl 期待 = US,GB = 2 = len-1
CANON = {"US": 0, "Europe": 0, "Australia": 0, "Canada": 3}  # CA usd=3
FX = {"CAD": 1.4141}  # ceil(3*1.4141)=5 CAD


def _ep(site_id, cost, excl):
    return {"siteId": site_id, "id": f"e{site_id}",
            "payload": {
                "shippingOptions": [
                    {"optionType": "DOMESTIC",
                     "shippingServices": [{"shippingServiceCode": "X", "shippingCost": cost}]}],
                "excludedCountries": excl}}


class _FakePage:
    def wait_for_timeout(self, _ms):
        pass


def _setup(monkeypatch, rb_ep):
    """書込前 prof (CA ep) と read-back prof を順に返すよう read_profile 等を差替え。"""
    write_prof = {"title": "MAG_2-3kg_7day", "color": 0, "dispatchTime": 7,
                  "returnsWithin": None, "returnsPaidByBuyer": False,
                  "excludedCountries": [], "country": None, "city": None, "postalCode": None,
                  "tariffs": [], "shippingEbayProfiles": [_ep(2, None, [])]}
    rb_prof = {"title": "MAG_2-3kg_7day", "shippingEbayProfiles": [rb_ep]}
    seq = iter([write_prof, rb_prof])
    monkeypatch.setattr(A, "read_profile", lambda pg, pid: next(seq))
    monkeypatch.setattr(A, "list_profiles",
                        lambda pg, first=200: [{"id": "p1", "title": "MAG_2-3kg_7day",
                                                "numberOfProducts": 1}])
    monkeypatch.setattr(A, "gql",
                        lambda *a, **k: {"upsertProfile": {"success": True,
                                                           "profile": {"id": "p1"}}})
    monkeypatch.setattr(A, "domestic_service_code", lambda ep: "X")


def test_readback_dict_and_excl_pass(monkeypatch):
    """dict 形式 cost + 自国以外全除外 → PASS (例外なし)。"""
    _setup(monkeypatch, _ep(2, {"value": 5, "currency": "CAD"}, ["US", "GB"]))
    A.set_values(_FakePage(), "p1", "2-3kg", FX, CANON, UNIVERSE)


def test_readback_wrong_currency_fails(monkeypatch):
    """value 一致でも currency 不一致は AssignError。"""
    _setup(monkeypatch, _ep(2, {"value": 5, "currency": "USD"}, ["US", "GB"]))
    with pytest.raises(A.AssignError, match="送料"):
        A.set_values(_FakePage(), "p1", "2-3kg", FX, CANON, UNIVERSE)


def test_readback_wrong_value_fails(monkeypatch):
    """value 不一致は AssignError (偽成功防止)。"""
    _setup(monkeypatch, _ep(2, {"value": 99, "currency": "CAD"}, ["US", "GB"]))
    with pytest.raises(A.AssignError, match="送料"):
        A.set_values(_FakePage(), "p1", "2-3kg", FX, CANON, UNIVERSE)


def test_readback_excl_count_wrong_fails(monkeypatch):
    """excludedCountries 件数不足 (配送国封じ込め漏れ) は AssignError。"""
    _setup(monkeypatch, _ep(2, {"value": 5, "currency": "CAD"}, ["US"]))
    with pytest.raises(A.AssignError, match="excludedCountries"):
        A.set_values(_FakePage(), "p1", "2-3kg", FX, CANON, UNIVERSE)


def test_readback_self_country_excluded_fails(monkeypatch):
    """自国 (CA) が除外リストに入る = 配送不能の誤り → AssignError。"""
    _setup(monkeypatch, _ep(2, {"value": 5, "currency": "CAD"}, ["CA", "US"]))
    with pytest.raises(A.AssignError, match="excludedCountries"):
        A.set_values(_FakePage(), "p1", "2-3kg", FX, CANON, UNIVERSE)
