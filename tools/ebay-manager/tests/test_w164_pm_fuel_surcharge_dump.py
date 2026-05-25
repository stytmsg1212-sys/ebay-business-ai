"""W164-pm regression test: fuel_surcharge 抽出失敗時の raw HTML dump.

2026-05-25 19:00 health check で 5/25 03:40 / 08:39 の DHL/FedEx 抽出失敗を検出.
fix: 失敗時に raw HTML を data/tmp/fuel_surcharge_failure_<date>_<source>.html
に保存 (次回の手動デバッグ用、Q0 silent skip 防止).

code-reviewer HIGH-2 対応の regression coverage.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tasks.task_fuel_surcharge_check import _dump_failure_html


def test_dump_failure_html_creates_file(tmp_path, monkeypatch):
    """html 値が非 None なら dump path に書き込まれる."""
    monkeypatch.setattr("tasks.task_fuel_surcharge_check.BASE_DIR", tmp_path)
    _dump_failure_html("dhl", "<html>test content</html>")
    files = list((tmp_path / "data" / "tmp").glob("fuel_surcharge_failure_*_dhl.html"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "<html>test content</html>"


def test_dump_failure_html_skips_none(tmp_path, monkeypatch):
    """html=None なら何もしない (例外伝播しない)."""
    monkeypatch.setattr("tasks.task_fuel_surcharge_check.BASE_DIR", tmp_path)
    _dump_failure_html("dhl", None)
    assert not (tmp_path / "data" / "tmp").exists() or \
           not list((tmp_path / "data" / "tmp").glob("*.html"))


def test_dump_failure_html_isolated_per_source(tmp_path, monkeypatch):
    """fedex / dhl で別ファイル名 (互いに上書きしない)."""
    monkeypatch.setattr("tasks.task_fuel_surcharge_check.BASE_DIR", tmp_path)
    _dump_failure_html("dhl", "<dhl/>")
    _dump_failure_html("fedex", "<fedex/>")
    files = sorted((tmp_path / "data" / "tmp").glob("fuel_surcharge_failure_*.html"))
    assert len(files) == 2
    contents = {f.name: f.read_text(encoding="utf-8") for f in files}
    assert any("dhl" in name and v == "<dhl/>" for name, v in contents.items())
    assert any("fedex" in name and v == "<fedex/>" for name, v in contents.items())
