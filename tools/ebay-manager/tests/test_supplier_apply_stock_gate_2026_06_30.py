"""在庫ゲート unit test (2026-06-30).

テスト対象: tasks/task_supplier_apply.apply_supplier_candidate
  - check_candidate_availability の結果に基づいて ReviseItem をブロック/通過する。

カバレッジ:
  1. available   → ReviseItem が呼ばれ success=True
  2. unavailable → ReviseItem が呼ばれない, success=False, "売り切れ" message,
                   候補 status は accepted のまま (rejected にしない)
  3. not_found   → 同上
  4. unknown     → ReviseItem が呼ばれる (続行)
  5. override=True + unavailable → それでもブロック (allow_alt_override と独立)

前提:
  - conftest.py の _isolate_monitor_db により本番 DB に触れない。
  - 実 HTTP は発火しない (check_candidate_availability を monkeypatch)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tasks.task_supplier_apply import apply_supplier_candidate  # noqa: E402


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def _make_candidate(
    *,
    cid: int = 99,
    status: str = "accepted",
    match_score: int = 80,
    alt_listing_possible: int = 0,
    ebay_item_id: str = "111222333444",
    sku: str = "ebayyh_p9000001",
    candidate_url: str = "https://example.com/item/9000001",
) -> dict:
    return {
        "id": cid,
        "status": status,
        "match_score": match_score,
        "alt_listing_possible": alt_listing_possible,
        "ebay_item_id": ebay_item_id,
        "sku": sku,
        "candidate_url": candidate_url,
        "candidate_title": "テスト商品",
    }


def _make_listing(*, is_ended: int = 0) -> dict:
    return {
        "ebay_item_id": "111222333444",
        "is_ended": is_ended,
        "ended_at": None,
        "ended_reason": None,
    }


def _make_avail(status: str, signal: str = "test-signal") -> dict:
    return {"status": status, "signal": signal, "checked_at": "2026-06-30T00:00:00+00:00"}


def _full_mocks(avail_status: str, avail_signal: str = "test-signal"):
    """在庫ゲート以外のすべての依存を通過させるパッチ群を返す context manager stack。"""
    return (
        patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
              return_value=_make_candidate()),
        patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
              return_value=_make_listing()),
        patch("tasks.task_supplier_apply.url_to_sku",
              return_value="ebayyh_p9000001"),
        patch("tasks.task_supplier_apply.get_ebay_credentials",
              return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}),
        patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True),
        patch("tasks.task_supplier_apply.check_candidate_availability",
              return_value=_make_avail(avail_status, avail_signal)),
    )


def _revise_ok_mocks():
    """ReviseItem 成功 + DB 追従のパッチ群。"""
    _cm = MagicMock()
    _cm.__enter__ = MagicMock(return_value=MagicMock())
    _cm.__exit__ = MagicMock(return_value=False)
    return (
        patch("tasks.task_supplier_apply.revise_item_sku",
              return_value={"success": True, "message": "OK"}),
        patch("tasks.task_supplier_apply.update_ebay_listing_sku"),
        patch("tasks.task_supplier_apply.upsert_item"),
        patch("tasks.task_supplier_apply.update_supplier_candidate_status"),
        patch("tasks.task_supplier_apply.get_conn", return_value=_cm),
    )


# ---------------------------------------------------------------------------
# 1. available → ReviseItem 呼ばれる, success=True
# ---------------------------------------------------------------------------

class TestStockGateAvailable(unittest.TestCase):
    """status=available は通過して ReviseItem が実行される。"""

    def test_available_executes_revise_and_returns_success(self):
        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate()), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail("available")) as mock_avail, \
             patch("tasks.task_supplier_apply.revise_item_sku",
                   return_value={"success": True, "message": "OK"}) as mock_revise, \
             patch("tasks.task_supplier_apply.update_ebay_listing_sku"), \
             patch("tasks.task_supplier_apply.upsert_item"), \
             patch("tasks.task_supplier_apply.update_supplier_candidate_status"), \
             patch("tasks.task_supplier_apply.get_conn") as mock_conn:

            _cm = MagicMock()
            _cm.__enter__ = MagicMock(return_value=MagicMock())
            _cm.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value = _cm

            result = apply_supplier_candidate(candidate_id=99, config={})

        self.assertTrue(result["success"], f"expected success: {result}")
        mock_revise.assert_called_once()


# ---------------------------------------------------------------------------
# 2. unavailable → ReviseItem 呼ばれない, success=False, 売り切れ, status据置
# ---------------------------------------------------------------------------

class TestStockGateUnavailable(unittest.TestCase):
    """status=unavailable は ReviseItem をブロックし success=False を返す。"""

    def _run(self, avail_status: str, signal: str = "sold-out") -> tuple[dict, MagicMock]:
        mock_revise = MagicMock()
        mock_update_status = MagicMock()

        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate(status="accepted")), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail(avail_status, signal)), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise), \
             patch("tasks.task_supplier_apply.update_supplier_candidate_status", mock_update_status):

            result = apply_supplier_candidate(candidate_id=99, config={})

        return result, mock_revise, mock_update_status

    def test_unavailable_returns_failure(self):
        result, _, _ = self._run("unavailable")
        self.assertFalse(result["success"])
        self.assertIn("売り切れ", result["message"])

    def test_unavailable_revise_not_called(self):
        _, mock_revise, _ = self._run("unavailable")
        mock_revise.assert_not_called()

    def test_unavailable_signal_in_message(self):
        result, _, _ = self._run("unavailable", signal="在庫なし")
        self.assertIn("在庫なし", result["message"])

    def test_unavailable_candidate_status_not_changed(self):
        """候補 status は accepted のまま据置 (rejected/applied にしない)。"""
        _, _, mock_update_status = self._run("unavailable")
        # update_supplier_candidate_status が呼ばれていない = status 変更なし
        mock_update_status.assert_not_called()

    def test_not_found_returns_failure(self):
        result, _, _ = self._run("not_found", signal="HTTP 404")
        self.assertFalse(result["success"])
        self.assertIn("売り切れ", result["message"])

    def test_not_found_revise_not_called(self):
        _, mock_revise, _ = self._run("not_found")
        mock_revise.assert_not_called()

    def test_not_found_candidate_status_not_changed(self):
        _, _, mock_update_status = self._run("not_found")
        mock_update_status.assert_not_called()


# ---------------------------------------------------------------------------
# 4. unknown → ReviseItem が呼ばれる (続行)
# ---------------------------------------------------------------------------

class TestStockGateUnknown(unittest.TestCase):
    """status=unknown は判定保留で ReviseItem を続行する。"""

    def test_unknown_executes_revise(self):
        mock_revise = MagicMock(return_value={"success": True, "message": "OK"})
        mock_update_status = MagicMock()

        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate()), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail("unknown", "httpx timeout")), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise), \
             patch("tasks.task_supplier_apply.update_ebay_listing_sku"), \
             patch("tasks.task_supplier_apply.upsert_item"), \
             patch("tasks.task_supplier_apply.update_supplier_candidate_status",
                   mock_update_status), \
             patch("tasks.task_supplier_apply.get_conn") as mock_conn:

            _cm = MagicMock()
            _cm.__enter__ = MagicMock(return_value=MagicMock())
            _cm.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value = _cm

            result = apply_supplier_candidate(candidate_id=99, config={})

        mock_revise.assert_called_once()
        self.assertTrue(result["success"], f"expected success for unknown: {result}")
        # applied に更新される
        mock_update_status.assert_called_once_with(99, "applied")


# ---------------------------------------------------------------------------
# 5. override=True + unavailable → ブロックされる (allow_alt_override と独立)
# ---------------------------------------------------------------------------

class TestStockGateWithAltOverride(unittest.TestCase):
    """allow_alt_override=True でも unavailable はブロックされる。"""

    def test_override_true_unavailable_still_blocked(self):
        mock_revise = MagicMock()

        # alt_only 候補 (score<60, alt=1) で override=True + unavailable
        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate(match_score=30, alt_listing_possible=1)), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail("unavailable", "sold out")), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise):

            result = apply_supplier_candidate(
                candidate_id=99, config={}, allow_alt_override=True
            )

        self.assertFalse(result["success"])
        self.assertIn("売り切れ", result["message"])
        mock_revise.assert_not_called()

    def test_override_true_not_found_still_blocked(self):
        mock_revise = MagicMock()

        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate(match_score=30, alt_listing_possible=1)), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail("not_found", "HTTP 404")), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise):

            result = apply_supplier_candidate(
                candidate_id=99, config={}, allow_alt_override=True
            )

        self.assertFalse(result["success"])
        self.assertIn("売り切れ", result["message"])
        mock_revise.assert_not_called()

    def test_override_true_available_executes_revise(self):
        """override=True + available → ReviseItem が実行される。"""
        mock_revise = MagicMock(return_value={"success": True, "message": "OK"})
        mock_update_status = MagicMock()

        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate(match_score=30, alt_listing_possible=1)), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   return_value=_make_avail("available")), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise), \
             patch("tasks.task_supplier_apply.update_ebay_listing_sku"), \
             patch("tasks.task_supplier_apply.upsert_item"), \
             patch("tasks.task_supplier_apply.update_supplier_candidate_status",
                   mock_update_status), \
             patch("tasks.task_supplier_apply.get_conn") as mock_conn:

            _cm = MagicMock()
            _cm.__enter__ = MagicMock(return_value=MagicMock())
            _cm.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value = _cm

            result = apply_supplier_candidate(
                candidate_id=99, config={}, allow_alt_override=True
            )

        mock_revise.assert_called_once()
        self.assertTrue(result["success"], f"expected success: {result}")


# ---------------------------------------------------------------------------
# 6. check_candidate_availability が例外 → unknown 扱いで続行, ReviseItem 呼ばれる
# ---------------------------------------------------------------------------

class TestStockGateException(unittest.TestCase):
    """check_candidate_availability が想定外例外を raise しても crash せず続行する。"""

    def test_exception_treated_as_unknown_and_revise_called(self):
        mock_revise = MagicMock(return_value={"success": True, "message": "OK"})
        mock_update_status = MagicMock()

        _cm = MagicMock()
        _cm.__enter__ = MagicMock(return_value=MagicMock())
        _cm.__exit__ = MagicMock(return_value=False)

        with patch("tasks.task_supplier_apply.get_supplier_candidate_by_id",
                   return_value=_make_candidate()), \
             patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id",
                   return_value=_make_listing()), \
             patch("tasks.task_supplier_apply.url_to_sku",
                   return_value="ebayyh_p9000001"), \
             patch("tasks.task_supplier_apply.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}), \
             patch("tasks.task_supplier_apply.ebay_credentials_ok", return_value=True), \
             patch("tasks.task_supplier_apply.check_candidate_availability",
                   side_effect=RuntimeError("scraper internal error")), \
             patch("tasks.task_supplier_apply.revise_item_sku", mock_revise), \
             patch("tasks.task_supplier_apply.update_ebay_listing_sku"), \
             patch("tasks.task_supplier_apply.upsert_item"), \
             patch("tasks.task_supplier_apply.update_supplier_candidate_status",
                   mock_update_status), \
             patch("tasks.task_supplier_apply.get_conn", return_value=_cm):

            result = apply_supplier_candidate(candidate_id=99, config={})

        # 例外 → unknown 経路 → ReviseItem が呼ばれ続行
        mock_revise.assert_called_once()
        self.assertTrue(result["success"], f"expected success (unknown fallback): {result}")
        # applied に遷移する
        mock_update_status.assert_called_with(99, "applied")


if __name__ == "__main__":
    unittest.main()
