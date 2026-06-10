#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 FIX-E: research_search_cli + _search_freemarket subprocess 化 unit test.

テスト対象:
  monitor.research_search_cli (新規 CLI エントリポイント)
  monitor.research_poc._search_freemarket (subprocess 呼び出しに変更)

方針:
  - research_search_cli は純粋な CLI ロジック (platform 不正 / search 関数 mock) のみ。
  - _search_freemarket は subprocess.run を mock して: returncode!=0 / ok:false /
    正常 JSON の 3 パターンを確認 (実 Playwright は不要)。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from monitor import research_poc


# ============================================================================
# research_search_cli 単体テスト
# ============================================================================

class TestResearchSearchCli:
    """monitor.research_search_cli._run の単体テスト."""

    def test_unknown_platform_returns_ok_false(self):
        """未知の platform は ok:false を返す."""
        from monitor.research_search_cli import _run
        result = _run("unknown_platform", 5, "test")
        assert result["ok"] is False
        assert "unknown platform" in result["error"]

    def test_mercari_ok(self, monkeypatch):
        """mercari: search_mercari の結果が hits に変換される."""
        from monitor.research_search_cli import _run

        fake_hit = MagicMock()
        fake_hit.url = "https://mercari.com/item/x"
        fake_hit.title = "SONY WH-1000XM4"
        fake_hit.price_jpy = 25000
        fake_hit.image_url = "https://img.example.com/x.jpg"

        with patch("monitor.mercari_search.search_mercari", return_value=[fake_hit]):
            result = _run("mercari", 3, "WH-1000XM4")

        assert result["ok"] is True
        assert len(result["hits"]) == 1
        hit = result["hits"][0]
        assert hit["url"] == "https://mercari.com/item/x"
        assert hit["title"] == "SONY WH-1000XM4"
        assert hit["price_jpy"] == 25000
        assert hit["image_url"] == "https://img.example.com/x.jpg"

    def test_yahoo_ok(self, monkeypatch):
        """yahoo_auctions: search_yahoo の結果が hits に変換される."""
        from monitor.research_search_cli import _run

        fake_hit = MagicMock()
        fake_hit.url = "https://auctions.yahoo.co.jp/item/y"
        fake_hit.title = "ATH-M50x"
        fake_hit.price_jpy = 8000
        fake_hit.image_url = None

        with patch("monitor.yahoo_search.search_yahoo", return_value=[fake_hit]):
            result = _run("yahoo_auctions", 3, "ATH-M50x")

        assert result["ok"] is True
        assert len(result["hits"]) == 1
        assert result["hits"][0]["image_url"] is None

    def test_paypay_ok(self, monkeypatch):
        """paypay_furima: search_paypay の結果が hits に変換される."""
        from monitor.research_search_cli import _run

        fake_hit = MagicMock()
        fake_hit.url = "https://paypayfleamarket.yahoo.co.jp/item/z"
        fake_hit.title = "Bose QC45"
        fake_hit.price_jpy = 18000
        fake_hit.image_url = "https://img.example.com/z.jpg"

        with patch("monitor.paypay_search.search_paypay", return_value=[fake_hit]):
            result = _run("paypay_furima", 3, "Bose QC45")

        assert result["ok"] is True
        assert len(result["hits"]) == 1

    def test_search_exception_propagates_from_run(self):
        """_run は例外を握りつぶさず伝播する (main 側で catch して ok:false に変換)."""
        from monitor.research_search_cli import _run

        with patch(
            "monitor.mercari_search.search_mercari",
            side_effect=RuntimeError("playwright died"),
        ):
            with pytest.raises(RuntimeError, match="playwright died"):
                _run("mercari", 3, "SONY")


# ============================================================================
# _search_freemarket subprocess 呼び出しテスト
# ============================================================================

class TestSearchFreemarketSubprocess:
    """_search_freemarket の subprocess 呼び出し挙動テスト."""

    def _make_completed_proc(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> MagicMock:
        proc = MagicMock(spec=subprocess.CompletedProcess)
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    def test_returncode_nonzero_raises(self):
        """subprocess が非 0 exit → RuntimeError."""
        fake_proc = self._make_completed_proc(
            returncode=1, stderr="playwright error detail"
        )
        with patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(RuntimeError, match="search subprocess failed"):
                research_poc._search_freemarket("mercari", "WH-1000XM4", 3)

    def test_ok_false_raises(self):
        """subprocess が ok:false を返す → RuntimeError."""
        payload = json.dumps({"ok": False, "error": "unknown platform: 'bad'"})
        fake_proc = self._make_completed_proc(returncode=0, stdout=payload)
        with patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(RuntimeError, match="search subprocess error"):
                research_poc._search_freemarket("bad", "keyword", 3)

    def test_ok_true_returns_hits(self):
        """正常 JSON → FreemarketHit リストが返る."""
        hits_data = [
            {
                "url": "https://mercari.com/item/abc",
                "title": "SONY WH-1000XM4 美品",
                "price_jpy": 22000,
                "image_url": "https://img.example.com/abc.jpg",
            }
        ]
        payload = json.dumps({"ok": True, "hits": hits_data})
        fake_proc = self._make_completed_proc(returncode=0, stdout=payload)
        with patch("subprocess.run", return_value=fake_proc):
            result = research_poc._search_freemarket("mercari", "WH-1000XM4", 3)

        assert len(result) == 1
        hit = result[0]
        assert isinstance(hit, research_poc.FreemarketHit)
        assert hit.source_platform == "mercari"
        assert hit.url == "https://mercari.com/item/abc"
        assert hit.title == "SONY WH-1000XM4 美品"
        assert hit.price_jpy == 22000
        assert hit.image_url == "https://img.example.com/abc.jpg"

    def test_ok_true_empty_hits(self):
        """正常 JSON でヒット 0 件 → 空リスト (例外なし)."""
        payload = json.dumps({"ok": True, "hits": []})
        fake_proc = self._make_completed_proc(returncode=0, stdout=payload)
        with patch("subprocess.run", return_value=fake_proc):
            result = research_poc._search_freemarket("yahoo_auctions", "ATH-M50x", 3)

        assert result == []

    def test_invalid_json_propagates(self):
        """subprocess が壊れた JSON を出力 → JSONDecodeError が伝播する."""
        fake_proc = self._make_completed_proc(returncode=0, stdout="NOT_JSON")
        with patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(json.JSONDecodeError):
                research_poc._search_freemarket("mercari", "WH-1000XM4", 3)

    def test_timeout_propagates(self):
        """subprocess.TimeoutExpired が伝播する."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=180),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                research_poc._search_freemarket("mercari", "WH-1000XM4", 3)

    def test_subprocess_called_with_correct_args(self):
        """subprocess.run に正しい引数が渡される."""
        import sys

        payload = json.dumps({"ok": True, "hits": []})
        fake_proc = self._make_completed_proc(returncode=0, stdout=payload)
        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            research_poc._search_freemarket("paypay_furima", "Bose QC45", 7)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1:] == ["-m", "monitor.research_search_cli", "paypay_furima", "7"]
        kwargs = call_args[1]
        assert kwargs["input"] == "Bose QC45"
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["timeout"] == 180
