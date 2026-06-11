#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W258/Phase-B: supplier_candidates.candidate_image_url backfill (one-shot).

対象: supplier_candidates WHERE status='pending' AND candidate_image_url IS NULL
各 candidate_url ページから og:image メタタグを正規表現で取得して保存する。

取得方法:
  - httpx + UA ヘッダで HTML を GET
  - og:image メタタグから URL を抽出 (正規表現)
  - メルカリ: https://static.mercdn.net/...
  - ヤフオク: https://auctions.c.yimg.jp/...
  (2026-06-11 実測確認済み: Referer なしでも 200 + image/jpeg を返す)

Q2 6-step:
  1. 対象件数を SELECT で確認
  2. snapshot: data/backup_candidate_images_YYYYMMDD_HHMMSS.json
  3. --apply なしは dry-run 既定
  4. 1 件試行 → 検証 → 残り全件
  5. init_db 非接触 (one-shot)

使い方:
  python scripts/backfill_candidate_images_2026_06_11.py           # dry-run
  python scripts/backfill_candidate_images_2026_06_11.py --apply   # 実書込

ドメイン毎 2 秒 sleep で rate limit 遵守。
取得失敗は WARN ログで明示して続行 (Q0: silent skip 禁止)。
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.database import get_conn  # noqa: E402

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE2 = re.compile(
    r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)


def _extract_og_image(html: str) -> str | None:
    """og:image メタタグから URL を抽出する。"""
    m = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE2.search(html)
    return m.group(1) if m else None


def _fetch_og_image(url: str, timeout: int = 15) -> str | None:
    """httpx で candidate_url の og:image を取得。失敗時は None。"""
    try:
        import httpx
    except ImportError:
        print("[WARN] httpx not installed. pip install httpx")
        return None
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _UA},
            follow_redirects=True,
            timeout=timeout,
        )
        r.raise_for_status()
        return _extract_og_image(r.text)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] fetch failed: {url!r} -> {e}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="supplier_candidates.candidate_image_url backfill"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="実際に UPDATE を実行する (既定は dry-run)"
    )
    args = parser.parse_args()
    dry_run = not args.apply

    # Step 1: 対象行
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, candidate_url
               FROM supplier_candidates
               WHERE status='pending' AND candidate_image_url IS NULL
               ORDER BY id"""
        ).fetchall()

    if not rows:
        print("[backfill] 対象行なし。処理スキップ。")
        return

    print(f"[backfill] 対象: {len(rows)} 件 (status=pending, candidate_image_url=NULL)")

    # Step 2: snapshot
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = PROJECT_ROOT / "data" / f"backup_candidate_images_{ts}.json"
    snapshot = [{"id": r[0], "candidate_url": r[1]} for r in rows]
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[backfill] snapshot 保存: {backup_path}")

    for r in rows[:5]:
        print(f"  id={r[0]}  url={r[1]!r}")
    if len(rows) > 5:
        print(f"  ... (他 {len(rows) - 5} 件)")

    if dry_run:
        print("[backfill] dry-run モード: 書込なし。--apply で実書込。")
        return

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Step 3: 1 件試行 → 検証
    first_id, first_url = rows[0]
    first_img = _fetch_og_image(first_url)
    print(f"[backfill] 1件試行: id={first_id}  img={first_img!r}")
    if first_img:
        with get_conn() as conn:
            conn.execute(
                """UPDATE supplier_candidates
                   SET candidate_image_url=?, candidate_image_fetched_at=?
                   WHERE id=?""",
                (first_img, now_utc, first_id),
            )
        with get_conn() as conn:
            check = conn.execute(
                "SELECT candidate_image_url FROM supplier_candidates WHERE id=?",
                (first_id,),
            ).fetchone()
        actual = check[0] if check else None
        print(f"[backfill] 1件検証: id={first_id}  DB値={actual!r}")
        if actual != first_img:
            print(f"[backfill] ERROR: DB 値が期待値と不一致。中断。")
            sys.exit(1)
    else:
        print(f"[backfill] WARN: id={first_id} 画像取得失敗 (続行)")

    # Step 4: 残り全件 (ドメイン毎 2 秒 sleep)
    _last_domain: str = urlparse(first_url).netloc
    ok_count = 1 if first_img else 0
    fail_count = 0 if first_img else 1

    for cid, curl in rows[1:]:
        domain = urlparse(curl).netloc
        if domain == _last_domain:
            time.sleep(2)
        else:
            _last_domain = domain
            time.sleep(1)

        img_url = _fetch_og_image(curl)
        if img_url:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE supplier_candidates
                       SET candidate_image_url=?, candidate_image_fetched_at=?
                       WHERE id=? AND candidate_image_url IS NULL""",
                    (img_url, now_utc, cid),
                )
            ok_count += 1
        else:
            print(f"[backfill] WARN: id={cid} 画像取得失敗 (URL={curl!r})")
            fail_count += 1

    print(
        f"[backfill] 完了: 成功={ok_count} / 失敗={fail_count} / 合計={len(rows)}"
    )


if __name__ == "__main__":
    main()
