#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14 customs_template_loader unit tests."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from monitor.customs_template_loader import (
    ALLOWED_VARIABLES, TemplateInfo,
    _parse_front_matter, load_templates, render_template, select_template,
)


def _make_template(tmpdir: Path, name: str, meta: str, body: str) -> Path:
    p = tmpdir / name
    p.write_text(f"---\n{meta}\n---\n{body}", encoding="utf-8")
    return p


def test_parse_front_matter_inline_list():
    text = (
        "---\n"
        "name: x\n"
        "when_to_use: [\"alpha\", \"beta\"]\n"
        "priority: 3\n"
        "---\n"
        "body text"
    )
    meta, body = _parse_front_matter(text)
    assert meta["name"] == "x"
    assert meta["when_to_use"] == ["alpha", "beta"]
    assert meta["priority"] == "3"
    assert "body text" in body


def test_parse_front_matter_block_list():
    text = (
        "---\n"
        "name: y\n"
        "when_to_use:\n"
        "  - first\n"
        "  - second\n"
        "carrier: fedex\n"
        "---\n"
        "hello"
    )
    meta, body = _parse_front_matter(text)
    assert meta["when_to_use"] == ["first", "second"]
    assert meta["carrier"] == "fedex"


def test_load_production_templates():
    """実環境のテンプレディレクトリから 4 件以上読めること."""
    templates = load_templates()
    # fedex-basic / fedex-tsca / fedex-ja-domestic / dhl-basic / ups-basic
    assert len(templates) >= 4
    carriers = {t.carrier for t in templates}
    assert carriers >= {"fedex", "dhl", "ups"}


def test_select_template_by_carrier_and_keyword(tmp_path):
    _make_template(
        tmp_path, "a.md",
        'name: a\ncarrier: fedex\npriority: 2\nwhen_to_use: ["generic"]',
        "body A",
    )
    _make_template(
        tmp_path, "b.md",
        'name: b\ncarrier: fedex\npriority: 5\nwhen_to_use: ["TSCA"]',
        "body B",
    )
    _make_template(
        tmp_path, "c.md",
        'name: c\ncarrier: dhl\npriority: 1\nwhen_to_use: ["generic"]',
        "body C",
    )
    templates = load_templates(tmp_path)
    # FedEx + "TSCA" キーワード → b
    picked = select_template(
        carrier="fedex", request_keywords=["TSCA"], templates=templates,
    )
    assert picked.name == "b"
    # FedEx + マッチ無し → priority 最小 (a)
    picked2 = select_template(
        carrier="fedex", request_keywords=["unknown"], templates=templates,
    )
    assert picked2.name == "a"
    # DHL → c
    picked3 = select_template(carrier="dhl", templates=templates)
    assert picked3.name == "c"
    # UPS 不在 → None
    assert select_template(carrier="ups", templates=templates) is None


def test_render_template_allowed_variables():
    t = TemplateInfo(
        name="x", carrier="fedex", body="Hi {{tracking_number}} from {{manufacturer_name}}",
    )
    out = render_template(t, {
        "tracking_number": "12345",
        "manufacturer_name": "SKT Co.",
    })
    assert "Hi 12345 from SKT Co." == out


def test_render_template_disallowed_var_ignored():
    t = TemplateInfo(name="x", carrier="fedex", body="{{malicious_code}} {{tracking_number}}")
    out = render_template(t, {
        "malicious_code": "rm -rf /",      # allow-list 外
        "tracking_number": "999",
    })
    # 不許可変数は展開されず {{var}} のまま
    assert "{{malicious_code}}" in out
    assert "999" in out
    assert "rm -rf" not in out


def test_allowed_variables_includes_critical_keys():
    for k in ("tracking_number", "manufacturer_name", "hts_code"):
        assert k in ALLOWED_VARIABLES
