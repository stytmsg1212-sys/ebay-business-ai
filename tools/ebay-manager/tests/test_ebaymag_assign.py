"""assign_product (REST PUT 版) の回帰テスト。

2026-06-22 W284: 商品割当が GraphQL productSave(no-op) → REST PUT に判明。
PUT エラー / HTTP200+body エラー / read-back 不一致 / global ID 正規化 /
merge 消失検知 を固定化 (code-reviewer 要求)。
"""
import pytest

from monitor import ebaymag_assign as A
from monitor.ebaymag_assign import assign_product, assert_no_vanish, AssignError


class FakePage:
    def __init__(self, put_res):
        self._put_res = put_res

    def evaluate(self, js, args):
        return self._put_res

    def wait_for_timeout(self, ms):
        pass


def _patch(monkeypatch, ship_profile_id):
    """list_profiles (snapshot 用) と gql (read-back 用) を差し替え。"""
    profs = [{"id": "1", "title": "MAG_x", "numberOfProducts": 0}]
    monkeypatch.setattr(A, "list_profiles", lambda pg, first=200: profs)
    monkeypatch.setattr(
        A, "gql",
        lambda pg, op, q, v: {"product": {"shippingProfileId": ship_profile_id}})


def test_put_http_error_raises(monkeypatch):
    _patch(monkeypatch, "1")
    pg = FakePage({"status": 500, "body": "server error"})
    with pytest.raises(AssignError, match="HTTP 500"):
        assign_product(pg, "p1", "1")


def test_put_200_body_error_raises(monkeypatch):
    """HIGH-2: HTTP 200 でも body に success:false / errors があれば停止。"""
    _patch(monkeypatch, "1")
    pg = FakePage({"status": 200, "body": {"success": False, "errors": ["nope"]}})
    with pytest.raises(AssignError, match="body error"):
        assign_product(pg, "p1", "1")


def test_readback_mismatch_raises(monkeypatch):
    """read-back の shippingProfileId が policy_id と不一致なら停止 (偽成功防止)。"""
    _patch(monkeypatch, "999")
    pg = FakePage({"status": 200, "body": {}})
    with pytest.raises(AssignError, match="read-back NG"):
        assign_product(pg, "p1", "1")


def test_readback_global_id_normalized(monkeypatch):
    """HIGH-1: read-back が 'Profile:1' 形式でも policy_id '1' と一致判定 (例外なし)。"""
    _patch(monkeypatch, "Profile:1")
    pg = FakePage({"status": 200, "body": {}})
    assign_product(pg, "p1", "1")  # 例外が出なければ PASS


def test_readback_raw_numeric_ok(monkeypatch):
    """実機形式 (raw 数値文字列) で正常割当 (例外なし)。"""
    _patch(monkeypatch, "1")
    pg = FakePage({"status": 200, "body": {}})
    assign_product(pg, "p1", "1")


def test_assert_no_vanish_detects_merge():
    before = {"1": {"title": "DDP_2-3kg"}, "2": {"title": "DDP_6-8kg"}}
    after = {"2": {"title": "DDP_6-8kg"}}
    with pytest.raises(AssignError, match="消失"):
        assert_no_vanish(before, after)
