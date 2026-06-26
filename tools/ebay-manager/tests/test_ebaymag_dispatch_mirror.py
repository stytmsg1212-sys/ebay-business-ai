"""ebaymag_dispatch_mirror の純関数 + money-direct ガード回帰テスト (2026-06-26)。

twin_value_complete の leak 防止 (未生成サイト/不一致値/$0/通貨違いで hold) と
build_twin_index の series 厳密 parse・両twin欠落検出を固定する。
"""
import pytest

from monitor import ebaymag_dispatch_mirror as M


# ---------- parse_mag / sku_expected_series ----------

def test_parse_mag_valid():
    assert M.parse_mag("MAG_5-6kg_7day") == ("5-6kg", "7day")
    assert M.parse_mag("MAG_0-0.5kg_1day") == ("0-0.5kg", "1day")


def test_parse_mag_invalid():
    assert M.parse_mag("DDP_1-2kg_$030_7day / X / JP") is None
    assert M.parse_mag("MAG_5-6kg_2day") is None  # 想定外サフィックス
    assert M.parse_mag("") is None


def test_sku_expected_series():
    assert M.sku_expected_series("stock:01") == "1day"
    assert M.sku_expected_series("stock") == "1day"
    assert M.sku_expected_series("ebayyh_p123") == "7day"
    assert M.sku_expected_series(None) is None
    assert M.sku_expected_series("weird") is None


# ---------- build_twin_index ----------

def _prof(pid, title):
    return {"id": pid, "title": title}


def test_build_twin_index_links_twins():
    profs = [_prof("100", "MAG_5-6kg_7day"), _prof("101", "MAG_5-6kg_1day"),
             _prof("200", "DDP_x / JP")]  # 非MAGは無視
    idx = M.build_twin_index(profs)
    assert idx["100"]["twin_id"] == "101"
    assert idx["101"]["twin_id"] == "100"
    assert "200" not in idx


def test_build_twin_index_missing_twin_raises():
    profs = [_prof("100", "MAG_5-6kg_7day")]  # 1day twin が無い
    with pytest.raises(M.MirrorError, match="twin"):
        M.build_twin_index(profs)


def test_build_twin_index_bad_suffix_raises():
    # MAG_ 始まりだが {1day,7day} でない = 黙って無視せず明示エラー (Q0)
    profs = [_prof("100", "MAG_5-6kg_7day"), _prof("101", "MAG_5-6kg_1day"),
             _prof("102", "MAG_5-6kg_2day")]
    with pytest.raises(M.MirrorError, match="series suffix"):
        M.build_twin_index(profs)


# ---------- twin_value_complete (money-direct leak ガード) ----------

# canonical: Europe=$3 / Australia=$5 / Canada=$3 (US=$0)
CANON = {"band": "x", "tab_values": {"US": 0, "Europe": 3, "Australia": 5, "Canada": 3}}
FX = {"GBP": 0.75, "EUR": 0.87, "AUD": 1.4, "CAD": 1.4}
# 期待現地: UK=ceil(3*.75)=3GBP, DE/FR/IT/ES=ceil(3*.87)=3EUR, AU=ceil(5*1.4)=7AUD, CA=ceil(3*1.4)=5CAD
_EXPECT = {3: (3, "GBP"), 77: (3, "EUR"), 71: (3, "EUR"), 101: (3, "EUR"),
           186: (3, "EUR"), 15: (7, "AUD"), 2: (5, "CAD")}


def _ep(sid, val, cur):
    return {"siteId": sid, "id": f"e{sid}",
            "payload": {"shippingOptions": [
                {"optionType": "DOMESTIC",
                 "shippingServices": [{"shippingCost": {"value": val, "currency": cur}}]}]}}


class _FakePage:
    pass


def _patch(monkeypatch, eps):
    monkeypatch.setattr(M, "build_canonical_policy", lambda band: CANON)
    monkeypatch.setattr(M.G, "read_profile",
                        lambda pg, pid: {"shippingEbayProfiles": eps})


def _all_correct_eps():
    return [_ep(sid, v, c) for sid, (v, c) in _EXPECT.items()]


def test_complete_all_sites_correct(monkeypatch):
    _patch(monkeypatch, _all_correct_eps())
    ok, reason = M.twin_value_complete(_FakePage(), "tid", "x", FX)
    assert ok is True and reason is None


def test_hold_on_missing_site(monkeypatch):
    eps = _all_correct_eps()[:-1]  # Canada(2) を欠落
    _patch(monkeypatch, eps)
    ok, reason = M.twin_value_complete(_FakePage(), "tid", "x", FX)
    assert ok is False and "未生成" in reason


def test_hold_on_zero_cost(monkeypatch):
    eps = _all_correct_eps()
    eps[0] = _ep(3, 0, "GBP")  # UK を $0 に
    _patch(monkeypatch, eps)
    ok, reason = M.twin_value_complete(_FakePage(), "tid", "x", FX)
    assert ok is False and "UK" in reason


def test_hold_on_value_mismatch(monkeypatch):
    eps = _all_correct_eps()
    eps[5] = _ep(15, 99, "AUD")  # AU を過大に
    _patch(monkeypatch, eps)
    ok, reason = M.twin_value_complete(_FakePage(), "tid", "x", FX)
    assert ok is False and "AU" in reason


def test_hold_on_wrong_currency(monkeypatch):
    eps = _all_correct_eps()
    eps[1] = _ep(77, 3, "USD")  # DE 値一致だが通貨違い
    _patch(monkeypatch, eps)
    ok, reason = M.twin_value_complete(_FakePage(), "tid", "x", FX)
    assert ok is False and "DE" in reason
