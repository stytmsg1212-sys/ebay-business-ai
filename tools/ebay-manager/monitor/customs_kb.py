#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化 KB (Knowledge Base).

Tier 1: config/customs_kb.json の手動登録 (即時 lookup)
Tier 2: Claude/Grok による動的 web 検索 (キャッシュ miss 時の fallback)
Tier 3: 検索結果を customs_kb_pending に queue → user 承認後 Tier 1 昇格

code-reviewer HIGH-8 対応: Tier 3 は承認制。Web 検索結果を無検証で KB に
書き込まない (HTS 誤記 = 関税増の直接原因のため).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_KB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "customs_kb.json"
)


@dataclass
class ManufacturerInfo:
    brand: str
    name: str                        # 表示名 (Distributor or Manufacturer)
    address: str
    tel: Optional[str] = None
    note: Optional[str] = None
    is_distributor: bool = True      # True: 日本代理店, False: 海外メーカー直
    categories: list[str] = field(default_factory=list)


@dataclass
class HTSInfo:
    category: str
    code: str
    description: str
    duty: str = ""
    ruling: Optional[str] = None


class CustomsKBError(Exception):
    pass


def load_kb(path: Optional[Path] = None) -> dict:
    """customs_kb.json を読んで dict を返す."""
    p = Path(path) if path else DEFAULT_KB_PATH
    if not p.exists():
        raise CustomsKBError(f"customs_kb.json not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def lookup_manufacturer(
    product_title: str, *, kb: Optional[dict] = None
) -> Optional[ManufacturerInfo]:
    """商品タイトルからメーカー/代理店情報を Tier 1 検索.

    ブランド keyword マッチで最初にヒットしたエントリを返す.
    None の場合は Tier 2 (web 検索) を呼ぶか manual 扱いにする.

    code-reviewer H-2 対応: Claude にこの dict を渡す時は allow-list 変数だけ
    展開する (任意コード実行を防ぐ).
    """
    kb = kb or load_kb()
    title_l = _norm(product_title)
    if not title_l:
        return None
    for brand, info in (kb.get("manufacturer") or {}).items():
        if not isinstance(info, dict):
            continue
        keywords = info.get("keywords") or [brand.lower()]
        for kw in keywords:
            if _norm(kw) in title_l:
                return ManufacturerInfo(
                    brand=brand,
                    name=info.get("distributor_name")
                         or info.get("manufacturer_name") or brand,
                    address=info.get("distributor_address")
                            or info.get("manufacturer_address") or "",
                    tel=info.get("distributor_tel") or info.get("manufacturer_tel"),
                    note=info.get("note"),
                    is_distributor="distributor_name" in info,
                    categories=list(info.get("categories") or []),
                )
    return None


def lookup_hts(
    product_title: str, *, categories: Optional[list[str]] = None,
    kb: Optional[dict] = None,
) -> Optional[HTSInfo]:
    """HTS コードを Tier 1 検索.

    優先順位:
      1. categories (manufacturer lookup で得た) がマッチするエントリ
      2. product_title に keyword が含まれるエントリ
      3. generic-electrical-apparatus (最終 fallback)
    """
    kb = kb or load_kb()
    hts_table = kb.get("hts") or {}
    title_l = _norm(product_title)

    # 1. categories マッチ
    if categories:
        cats_norm = {_norm(c) for c in categories}
        for cat, info in hts_table.items():
            if not isinstance(info, dict):
                continue
            if _norm(cat) in cats_norm:
                return _build_hts(cat, info)

    # 2. keyword マッチ
    if title_l:
        for cat, info in hts_table.items():
            if not isinstance(info, dict):
                continue
            for kw in info.get("keywords") or []:
                if _norm(kw) in title_l:
                    return _build_hts(cat, info)

    # 3. 汎用 fallback
    fallback = hts_table.get("generic-electrical-apparatus")
    if isinstance(fallback, dict):
        return _build_hts("generic-electrical-apparatus", fallback)
    return None


def _build_hts(category: str, info: dict) -> HTSInfo:
    return HTSInfo(
        category=category,
        code=str(info.get("code", "")),
        description=str(info.get("description", "")),
        duty=str(info.get("duty", "")),
        ruling=info.get("ruling"),
    )


# ─────────────────────────────────────────────
# Tier 3: 承認待ち queue (H-8 対応)
# ─────────────────────────────────────────────

def propose_kb_entry(
    *,
    kind: str,                           # 'manufacturer' or 'hts'
    brand_or_category: str,
    proposed_json: dict,
    source_url: Optional[str] = None,
    detected_from_customs_request_id: Optional[int] = None,
) -> int:
    """Tier 2 Web 検索結果を customs_kb_pending に queue.

    user が MONO Deck UI で「承認」を押すまで Tier 1 には昇格しない.
    同じ brand_or_category が既に proposed なら重複 queue しない (idempotent).
    """
    if kind not in ("manufacturer", "hts"):
        raise CustomsKBError(f"invalid kind: {kind}")
    from monitor.database import get_conn
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM customs_kb_pending "
            "WHERE kind = ? AND brand_or_category = ? AND status = 'proposed'",
            (kind, brand_or_category),
        ).fetchone()
        if existing:
            return int(existing[0])
        cur = conn.execute(
            """INSERT INTO customs_kb_pending
               (kind, brand_or_category, proposed_json, source_url,
                status, detected_from_customs_request_id)
               VALUES (?, ?, ?, ?, 'proposed', ?)""",
            (kind, brand_or_category,
             json.dumps(proposed_json, ensure_ascii=False),
             source_url, detected_from_customs_request_id),
        )
        return int(cur.lastrowid)


def approve_kb_entry(pending_id: int, *, kb_path: Optional[Path] = None) -> bool:
    """Tier 3 エントリを承認し Tier 1 (customs_kb.json) に昇格.

    user が MONO Deck UI から呼び出す想定.
    """
    from monitor.database import get_conn
    p = Path(kb_path) if kb_path else DEFAULT_KB_PATH
    with get_conn() as conn:
        row = conn.execute(
            "SELECT kind, brand_or_category, proposed_json FROM customs_kb_pending "
            "WHERE id = ? AND status = 'proposed'",
            (pending_id,),
        ).fetchone()
        if not row:
            return False
        kind, brand, proposed = row["kind"], row["brand_or_category"], row["proposed_json"]
        try:
            proposed_obj = json.loads(proposed)
        except json.JSONDecodeError:
            logger.error(f"pending {pending_id}: proposed_json invalid")
            return False

        # customs_kb.json に追記
        kb = load_kb(p)
        section = kb.setdefault(kind, {})
        section[brand] = proposed_obj
        p.write_text(
            json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        conn.execute(
            "UPDATE customs_kb_pending SET status='approved', "
            "reviewed_at=CURRENT_TIMESTAMP WHERE id = ?",
            (pending_id,),
        )
    logger.info(f"KB approved: {kind}/{brand} (pending_id={pending_id})")
    return True


def reject_kb_entry(pending_id: int) -> bool:
    from monitor.database import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE customs_kb_pending SET status='rejected', "
            "reviewed_at=CURRENT_TIMESTAMP WHERE id = ? AND status='proposed'",
            (pending_id,),
        )
        return cur.rowcount > 0


def list_pending_kb() -> list[dict]:
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, kind, brand_or_category, proposed_json, source_url, "
            "       created_at "
            "FROM customs_kb_pending WHERE status='proposed' "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "ManufacturerInfo", "HTSInfo", "CustomsKBError",
    "load_kb", "lookup_manufacturer", "lookup_hts",
    "propose_kb_entry", "approve_kb_entry", "reject_kb_entry", "list_pending_kb",
]
