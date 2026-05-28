"""W148-Y (2026-05-20 user 緊急要望): 仕入先候補で既に売切/終了している
ものを auto_rejected=1 で排除する one-shot script.

経緯: 仕入先候補 (supplier_candidates) には Phase 1 リサーチ時点では在庫
ありだったが、その後売切/オークション終了になった候補が多数残存。user が
「不採用」を押すと user 判断扱いになり Phase 1 学習を歪める懸念
(monitor/claude_evaluator.py L301 `auto_rejected=1 は除外 = ユーザー判断のみ`)。

本 script は status IN ('pending', 'accepted') の候補を httpx で取得し、
inventory_checker_http.py の検出ルール (売り切れました / このオークションは終了)
にマッチしたら `status='rejected', auto_rejected=1` で更新する。

設計:
- httpx 軽量。Mercari/Yahoo は server-rendered HTML 内 (or static fallback)
  に売切テキストを持つことが多いので httpx で十分なはず。
- bot 検知 (403/captcha) は skip (status 更新しない、次回再試行)。
- timeout/network エラーも skip (= 在庫不明扱い、安全側)。
- progress 表示。最終 summary を JSON で stdout 出力。
- DB 更新は trans 内 commit (途中中断でも書込済分は反映)。

呼出: python -m scripts.check_supplier_candidates_oos_2026_05_20

Q0 silent skip 防止: 各候補ごとに in_stock/out_of_stock/uncertain のいずれかに
分類して log 残す (sample 出力 + summary 集計)。失敗は uncertain 扱い。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor.db"

# inventory_checker_http.py の検出ルールを継承 (実証済 production パターン)
# Codex 2026-05-20 HIGH 対応: mercari の "SOLD" は raw HTML 全体マッチで
# embedded JSON / 関連商品 / script tag に紛れる false-positive リスクが
# あるため除外。「売り切れました」のみ (inventory_checker_http.py:51 と整合)。
DETECTION = {
    "mercari": {
        "in_stock": ["購入手続きへ"],
        "out_of_stock": ["売り切れました"],
        "not_found": ["ページが見つかりません"],
    },
    "yahoo_auctions": {
        "in_stock": ["入札する", "今すぐ落札"],
        "out_of_stock": ["このオークションは終了", "落札されました"],
        "not_found": ["このオークションは存在しません"],
    },
    "paypay": {
        "in_stock": ["購入手続きへ"],
        "out_of_stock": ["関連商品をアプリで探す"],
        "not_found": ["この商品は存在しません"],
    },
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def detect_platform(source_platform: str, url: str) -> str:
    """source_platform 列 + URL から検出 platform key を決定."""
    sp = (source_platform or "").lower()
    if sp in DETECTION:
        return sp
    if "mercari" in url:
        return "mercari"
    if "auctions.yahoo" in url:
        return "yahoo_auctions"
    if "paypayfleamarket" in url or "paypay" in url:
        return "paypay"
    return "unknown"


def check_url_inventory(
    url: str, platform: str, timeout: float = 10.0,
) -> dict:
    """1 URL の在庫を判定。

    Returns:
        {'classification': 'in_stock' | 'out_of_stock' | 'not_found' |
                           'uncertain' | 'blocked' | 'error',
         'signal': str,   # マッチしたテキスト or エラー理由
         'http_status': Optional[int]}
    """
    rules = DETECTION.get(platform)
    if not rules:
        return {
            'classification': 'uncertain',
            'signal': f'unsupported_platform: {platform}',
            'http_status': None,
        }
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.9"},
        ) as client:
            r = client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return {
            'classification': 'error', 'signal': f'http error: {e}',
            'http_status': None,
        }

    http_status = r.status_code

    if http_status in (403, 429):
        return {
            'classification': 'blocked',
            'signal': f'http {http_status} (bot 検知 / rate limit、後で再試行)',
            'http_status': http_status,
        }
    if http_status == 404:
        return {
            'classification': 'not_found',
            'signal': f'http 404',
            'http_status': http_status,
        }
    if http_status >= 500:
        return {
            'classification': 'error',
            'signal': f'http {http_status}',
            'http_status': http_status,
        }
    if http_status != 200:
        return {
            'classification': 'uncertain',
            'signal': f'http {http_status}',
            'http_status': http_status,
        }

    html = r.text or ""

    # 優先順: not_found > out_of_stock > in_stock。
    # (not_found のシグナルは「ページが消えた」= 確実に取扱不能。
    #  out_of_stock を先に判定すると not_found ページに OOS テキストが
    #  紛れていた場合に誤分類するため not_found を先に。)
    for sig in rules.get("not_found", []):
        if sig in html:
            return {
                'classification': 'not_found', 'signal': sig,
                'http_status': http_status,
            }
    for sig in rules.get("out_of_stock", []):
        if sig in html:
            return {
                'classification': 'out_of_stock', 'signal': sig,
                'http_status': http_status,
            }
    for sig in rules.get("in_stock", []):
        if sig in html:
            return {
                'classification': 'in_stock', 'signal': sig,
                'http_status': http_status,
            }
    # どのシグナルもマッチせず → uncertain (HTML が JS-render or 形式変更)
    return {
        'classification': 'uncertain',
        'signal': 'no signal matched (JS-rendered or layout change)',
        'http_status': http_status,
    }


def main() -> int:
    if sys.platform == 'win32':
        if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dry-run", action="store_true",
        help="DB を更新せず classification のみ実行 (sample audit 用)",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="先頭 N 件だけ処理 (sample mode、default=全件)",
    )
    p.add_argument(
        "--include-accepted", action="store_true",
        help="W182 (2026-05-28): accepted も対象に含める (default=pending のみ). "
             "user 明示承認後の候補でも sold_out 確認したい時用.",
    )
    args = p.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Codex 2026-05-20 MEDIUM 対応: accepted は user が明示承認した候補なので
    # default では auto-reject 対象から除外 (既存 cleanup_stale_supplier_candidates も
    # accepted は user 判断尊重で除外). W182 で --include-accepted フラグ追加.
    if args.include_accepted:
        status_clause = "status IN ('pending', 'accepted')"
        status_label = "pending + accepted"
    else:
        status_clause = "status = 'pending'"
        status_label = "pending のみ"
    rows = conn.execute(
        f"SELECT id, candidate_url, source_platform, status, candidate_title "
        f"FROM supplier_candidates "
        f"WHERE {status_clause} "
        f"  AND candidate_url IS NOT NULL AND candidate_url != '' "
        f"ORDER BY id" + (f" LIMIT {int(args.limit)}" if args.limit else "")
    ).fetchall()

    total = len(rows)
    mode = "DRY-RUN" if args.dry_run else "PRODUCTION"
    logger.info(
        f"対象 supplier_candidates: {total} 件 ({status_label}) [{mode}]"
    )

    summary = {
        'total': total,
        'in_stock': 0,
        'out_of_stock': 0,
        'not_found': 0,
        'uncertain': 0,
        'blocked': 0,
        'error': 0,
        'auto_rejected_oos': 0,    # 実際に DB UPDATE した件数
        'auto_rejected_404': 0,
        'platforms': {},
        'samples_oos': [],          # 排除サンプル (確認用、最大 10)
        'samples_uncertain': [],    # 不明サンプル (再試行 trigger 用、最大 5)
    }

    for i, row in enumerate(rows, 1):
        cid = row['id']
        url = row['candidate_url']
        sp = row['source_platform']
        ttl = (row['candidate_title'] or '')[:50]
        platform = detect_platform(sp, url)

        result = check_url_inventory(url, platform)
        cls = result['classification']
        summary[cls] = summary.get(cls, 0) + 1

        if platform not in summary['platforms']:
            summary['platforms'][platform] = {'total': 0, 'oos': 0}
        summary['platforms'][platform]['total'] += 1

        # Codex 2026-05-20 LOW 対応: 各候補ごとの classification 痕跡を残す
        # (Q0 silent skip 防止、後から audit 可能化)。
        logger.info(
            f"  cid={cid} platform={platform} cls={cls} signal={result['signal'][:60]!r} "
            f"title={ttl!r}"
        )

        # 排除条件: out_of_stock or not_found = 「もはや取扱不能」確定
        if cls in ('out_of_stock', 'not_found'):
            # W182 HIGH-1 fix (2026-05-28 code-reviewer): UPDATE WHERE 句の status を
            # SELECT 時の status_clause と整合させる。旧コードは UPDATE が
            # status='pending' 固定で、--include-accepted 指定時に accepted な
            # sold_out 行が UPDATE 0 件影響だが summary では reject 済とカウント =
            # Q0 偽装成功違反。修正: SELECT で hit した status 集合に対して UPDATE
            # を発行し、rowcount で実 update を verify。0 行影響時は summary も
            # カウントしない (silent skip 防止)。
            actually_updated = False
            if not args.dry_run:
                cur = conn.execute(
                    f"UPDATE supplier_candidates "
                    f"SET status='rejected', auto_rejected=1, "
                    f"    user_action_at=CURRENT_TIMESTAMP "
                    f"WHERE id=? AND {status_clause}",
                    (cid,),
                )
                actually_updated = (cur.rowcount or 0) > 0
                if not actually_updated:
                    logger.warning(
                        f"  cid={cid} UPDATE rowcount=0 (status changed between "
                        f"SELECT and UPDATE? 集計から除外)"
                    )
                conn.commit()
            else:
                # dry-run は SELECT 結果ベースで集計 (実 update なし)
                actually_updated = True
            if actually_updated:
                summary['platforms'][platform]['oos'] += 1
                if cls == 'out_of_stock':
                    summary['auto_rejected_oos'] += 1
                else:
                    summary['auto_rejected_404'] += 1
                if len(summary['samples_oos']) < 10:
                    summary['samples_oos'].append({
                        'cid': cid, 'platform': platform, 'classification': cls,
                        'signal': result['signal'], 'title': ttl,
                    })

        if cls == 'uncertain' and len(summary['samples_uncertain']) < 5:
            summary['samples_uncertain'].append({
                'cid': cid, 'platform': platform,
                'signal': result['signal'], 'title': ttl,
            })

        # progress (10件ごと)
        if i % 10 == 0 or i == total:
            logger.info(
                f"progress {i}/{total}: "
                f"in_stock={summary['in_stock']} "
                f"oos={summary['out_of_stock']} 404={summary['not_found']} "
                f"uncertain={summary['uncertain']} "
                f"blocked={summary['blocked']} err={summary['error']} "
                f"→ auto_rejected={summary['auto_rejected_oos'] + summary['auto_rejected_404']}"
            )

        # 礼儀正しい sleep (bot 検知回避)
        time.sleep(0.8)

    conn.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
