#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
description_templates テーブルへの v4 テンプレ初期投入タスク (W9 Phase 5)

app.py の起動時 (init_db の直後) に一度だけ呼ぶ。既に何か1件でも
description_templates に登録されていれば no-op。

テンプレ本文の取得優先順:
  1. `.company/ebay-knowledge/topics/listing-description-template.md` の
     ```html ～ ``` ブロックを抽出
  2. 読取失敗時はハードコードされた fallback テンプレ (最小構成) を投入

これにより、ユーザー環境でテンプレ markdown が消えていてもアプリは
UI が立ち上がる (完全壊滅を避ける)。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional

# pythonw gotcha ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

logger = logging.getLogger(__name__)

# v4 テンプレ markdown の既定パス (プロジェクトルートからの相対)
_DEFAULT_TEMPLATE_MD = (
    Path(__file__).resolve().parent.parent.parent.parent
    / ".company"
    / "ebay-knowledge"
    / "topics"
    / "listing-description-template.md"
)

# 投入するテンプレ名 (UNIQUE 制約の対象)
_SEED_TEMPLATE_NAME = "MonoHonpo v4 (default)"

# フォールバック用最小テンプレ。markdown が読めない環境でも UI を止めない。
# listing_generator.render_description の 14 種 placeholder を全て含む。
_FALLBACK_TEMPLATE_BODY = """<div class="mh-wrap {{mode_class}}" style="font-family:Arial,sans-serif;max-width:860px;margin:0 auto;padding:20px;background:#f6f2ea;color:#1a1817;">
  <div style="text-align:center;padding:24px 0;border-bottom:1px solid #d8cdb5;">
    <div style="font-size:11px;letter-spacing:8px;color:#a8341b;margin-bottom:14px;">S H I P P E D  F R O M  J A P A N</div>
    <h1 style="font-size:28px;margin:0 0 10px;">{{product_name}}</h1>
    <p style="font-style:italic;color:#6b6157;margin:0;">{{product_sub}}</p>
  </div>
  <div style="padding:24px 0;text-align:center;border-bottom:1px solid #d8cdb5;">
    <div style="font-size:11px;letter-spacing:6px;color:#6b6157;text-transform:uppercase;">CONDITION RANK</div>
    <div style="font-size:36px;font-weight:600;margin:10px 0;">{{rank}}</div>
    <div style="font-size:16px;margin-bottom:4px;">{{rank_label}}</div>
    <div style="font-size:13px;color:#6b6157;">{{rank_jp}}</div>
    <div style="max-width:520px;margin:16px auto 0;padding:12px 16px;background:#fbf9f3;border-left:2px solid #1a1817;text-align:left;font-style:italic;">{{quick_notes}}</div>
  </div>
  <div style="padding:20px 0;">
    <h2 style="font-size:20px;margin-bottom:12px;">In the box</h2>
    <div>{{includes_rows}}</div>
  </div>
  <div style="padding:20px 0;">
    <h2 style="font-size:20px;margin-bottom:12px;">Specifications</h2>
    <table style="width:100%;border-collapse:collapse;">{{specs_rows}}</table>
  </div>
  <div style="padding:16px;background:#fbf9f3;border:1px solid #d8cdb5;margin:16px 0;">{{spec_strip_rows}}</div>
  <div style="padding:24px;background:#1a1817;color:#f6f2ea;margin:16px 0;">
    <h3 style="font-size:18px;margin-bottom:12px;">Shipping &amp; handling</h3>
    <p><strong>Origin:</strong> {{shipping_origin}}</p>
    <p><strong>Carrier:</strong> {{shipping_carrier}}</p>
    <p><strong>Handling:</strong> Ships within {{shipping_handling}}</p>
    <p><strong>Delivery (US):</strong> {{shipping_delivery_us}}</p>
    <p><strong>Packaging:</strong> {{shipping_packaging}}</p>
    <p><strong>Notes:</strong> {{shipping_notes}}</p>
  </div>
</div>"""


def _extract_html_block(md_text: str) -> Optional[str]:
    """markdown から最初の ```html ～ ``` ブロックを抽出する。"""
    if not md_text:
        return None
    m = re.search(r'```html\s*\n(.*?)\n```', md_text, flags=re.DOTALL)
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def _load_v4_template_body(md_path: Optional[Path] = None) -> str:
    """listing-description-template.md から v4 HTML 本文を読み出す。

    読取失敗時はハードコード fallback を返す。
    """
    path = md_path or _DEFAULT_TEMPLATE_MD
    try:
        if path.exists():
            md = path.read_text(encoding='utf-8')
            extracted = _extract_html_block(md)
            if extracted:
                return extracted
            logger.warning(
                f"v4 template markdown found but no ```html``` block: {path}"
            )
        else:
            logger.warning(f"v4 template markdown not found: {path}")
    except OSError as e:
        logger.warning(f"v4 template read failed ({path}): {e}")
    return _FALLBACK_TEMPLATE_BODY


def seed_v4_template_if_needed(md_path: Optional[Path] = None) -> Optional[int]:
    """description_templates が空なら v4 テンプレを 1件 INSERT する。

    Returns:
        投入した template_id (新規) / None (既存データありで skip)
    """
    # DB 関数は遅延 import (本モジュールを pytest が import する時点で
    # database.py が load されるとマイグレーションが走る副作用を避けるため、
    # 呼出し側が init_db 済みであることを前提にしない)。
    from monitor.database import (
        get_description_templates,
        save_description_template,
    )

    try:
        existing = get_description_templates()
    except Exception as e:  # noqa: BLE001 — UI を絶対に止めない
        logger.warning(f"seed_v4: get_description_templates failed: {e}")
        return None

    if existing:
        logger.debug(
            f"seed_v4: templates already exist ({len(existing)} rows), skipping"
        )
        return None

    body = _load_v4_template_body(md_path)
    try:
        new_id = save_description_template(
            name=_SEED_TEMPLATE_NAME,
            body=body,
            is_default=True,
        )
        logger.info(
            f"seed_v4: inserted template id={new_id} name={_SEED_TEMPLATE_NAME!r} "
            f"len={len(body)}"
        )
        return new_id
    except Exception as e:  # noqa: BLE001
        logger.warning(f"seed_v4: save failed: {e}")
        return None


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    tid = seed_v4_template_if_needed()
    if tid is None:
        print("no-op (existing rows or error)")
    else:
        print(f"inserted id={tid}")
