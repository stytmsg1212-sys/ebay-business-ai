#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化: テンプレ library ローダ

`.company/daily-operations/customs-templates/*.md` から YAML front matter 付き
テンプレを読み込み、carrier + キーワードマッチで選択.

code-reviewer H-2 対応:
  - テンプレ内 `{{変数}}` は allow-list 方式で限定 (任意コード実行を防ぐ)
  - 変数展開は Python 側の deterministic な str.replace のみ (Jinja2 等の
    評価エンジンを介さない)
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = (
    _BASE_DIR.parent.parent / ".company" / "daily-operations" / "customs-templates"
)


# テンプレ内で展開を許可する変数名 (allow-list、H-2)
ALLOWED_VARIABLES = frozenset({
    "tracking_number",
    "carrier_case_cc",
    "sender_osv_email",
    "product_description_en",
    "product_description_ja",
    "product_end_use_en",
    "product_end_use_ja",
    "manufacturer_name",
    "manufacturer_address",
    "manufacturer_address_short",
    "manufacturer_tel_optional",
    "hts_code",
    "hts_description",
    "hts_ruling_optional",
    "photo_attachment_note",
    "composition_en",
    "composition_ja",
    "japan_cs_contact_name",
    "recipient",
    "ship_date",
})


@dataclass
class TemplateInfo:
    name: str
    carrier: str                     # 'fedex' / 'dhl' / 'ups'
    when_to_use: list[str] = field(default_factory=list)
    priority: int = 99
    version: str = "1.0"
    body: str = ""                   # front matter 除去後の本文
    path: Path = None
    content_hash: str = ""           # SHA256 (audit 用)


_FRONT_MATTER_RE = re.compile(r"^---\n(.+?)\n---\n(.*)$", re.S)


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """YAML front matter を軽量パース (PyYAML なしでも動くシンプル実装).

    サポート: key: value, key: [v1, v2], key: \\n  - v1\\n  - v2
    """
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    header, body = m.group(1), m.group(2)
    meta: dict = {}
    current_list_key: Optional[str] = None
    for line in header.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - "):
            if current_list_key:
                meta.setdefault(current_list_key, []).append(line[4:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if not v:
                current_list_key = k
                meta[k] = []
            else:
                current_list_key = None
                # inline list
                if v.startswith("[") and v.endswith("]"):
                    items = [s.strip().strip('"\'') for s in v[1:-1].split(",") if s.strip()]
                    meta[k] = items
                else:
                    meta[k] = v.strip('"\'')
    return meta, body


def load_templates(directory: Optional[Path] = None) -> list[TemplateInfo]:
    """指定ディレクトリ下の *.md を全て読み込む."""
    d = Path(directory) if directory else _TEMPLATES_DIR
    if not d.exists():
        logger.warning(f"customs templates dir not found: {d}")
        return []
    results: list[TemplateInfo] = []
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"failed to read {p}: {e}")
            continue
        meta, body = _parse_front_matter(text)
        carrier = (meta.get("carrier") or "").lower()
        if carrier not in ("fedex", "dhl", "ups"):
            logger.debug(f"skipping template {p.name}: invalid carrier={carrier}")
            continue
        try:
            priority = int(meta.get("priority", 99))
        except (TypeError, ValueError):
            priority = 99
        results.append(TemplateInfo(
            name=str(meta.get("name") or p.stem),
            carrier=carrier,
            when_to_use=list(meta.get("when_to_use") or []),
            priority=priority,
            version=str(meta.get("version") or "1.0"),
            body=body,
            path=p,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        ))
    return results


def select_template(
    *,
    carrier: str,
    request_keywords: Optional[list[str]] = None,
    templates: Optional[list[TemplateInfo]] = None,
) -> Optional[TemplateInfo]:
    """carrier + キーワードマッチでテンプレを 1 件選択.

    優先順位:
      1. carrier マッチ + request_keywords が when_to_use の要素に含まれる最高 priority
      2. carrier マッチのみの最高 priority (fallback)
      3. None
    """
    candidates = [
        t for t in (templates or load_templates())
        if t.carrier == (carrier or "").lower()
    ]
    if not candidates:
        return None

    # 1. keyword マッチ
    if request_keywords:
        kw_lower = [k.lower() for k in request_keywords if k]
        scored: list[tuple[int, TemplateInfo]] = []
        for t in candidates:
            score = 0
            for use_case in t.when_to_use:
                if any(k in use_case.lower() for k in kw_lower):
                    score += 1
            if score > 0:
                scored.append((score, t))
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1].priority))
            return scored[0][1]

    # 2. fallback: priority 最小 (数字小さい = 優先高)
    candidates.sort(key=lambda t: t.priority)
    return candidates[0]


def render_template(template: TemplateInfo, variables: dict) -> str:
    """テンプレ本文に {{var}} を展開.

    code-reviewer H-2 対応:
      - ALLOWED_VARIABLES に含まれる変数のみ展開
      - 不明変数は `{{var}}` のまま残す (draft_generator が later に検知可)
      - Jinja2 等の式エンジンは使わない (任意コード実行を防ぐ)
    """
    rendered = template.body
    for key, value in variables.items():
        if key not in ALLOWED_VARIABLES:
            logger.warning(f"disallowed template variable: {key} (ignored)")
            continue
        placeholder = "{{" + key + "}}"
        rendered = rendered.replace(placeholder, str(value or ""))
    return rendered


__all__ = [
    "TemplateInfo", "ALLOWED_VARIABLES",
    "load_templates", "select_template", "render_template",
]
