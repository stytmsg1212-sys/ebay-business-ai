"""W148 — キーワード新着監視 task (メルカリ / ヤフオク を巡回 → Discord 通知)。

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md (v2.2)
DB: monitor/database.py v46 (keyword_watches / keyword_watch_hits)

呼出経路:
  (A) cron 2h: daily_scheduler._run_keyword_watch_crawl → subprocess で本ファイル __main__ 起動
      (Playwright sync_playwright を APScheduler worker thread から物理分離)
  (B) UI「今すぐ巡回」: Streamlit script (main thread) から run_keyword_watch_crawl(config) 直接呼出

Q0 偽装成功防止: 必ず dict を返す (例外時も success=False + message + 部分集計を含む dict)。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from monitor.keyword_watch_db import (
    list_watches,
    record_hit_claim,
    mark_hit_notified,
    update_watch_last_crawled,
    get_unnotified_in_range_hits,
    claim_hit_for_resend,
    release_hit_resend_claim,
    get_ebay_prices_for_item_ids,
)

logger = logging.getLogger(__name__)


def _check_price_range(
    price: Optional[int],
    pmin: Optional[int],
    pmax: Optional[int],
) -> bool:
    """価格レンジ判定。
    - price=None: 通知しない (誤発火防止)
    - 両方 NULL: 通知しない (§15-Q1 = 「価格レンジ未設定 = 通知無効」)
    - 範囲外: 通知しない"""
    if price is None:
        return False
    if pmin is None and pmax is None:
        return False
    if pmin is not None and price < pmin:
        return False
    if pmax is not None and price > pmax:
        return False
    return True


def _format_range(pmin: Optional[int], pmax: Optional[int]) -> str:
    if pmin is None and pmax is None:
        return "(未設定)"
    lo = f"¥{pmin:,}" if pmin is not None else "下限なし"
    hi = f"¥{pmax:,}" if pmax is not None else "上限なし"
    return f"{lo}〜{hi}"


def _send_discord_for_hit(webhook: str, watch: dict, hit, hit_id: int) -> bool:
    """価格レンジ合致 hit を Discord webhook へ送信。

    Codex HIGH-3 (a): 1 回失敗 → 1s backoff → 1 回 retry. webhook 5xx /
    TLS hiccup / 一過性 network 障害で機会 lost を防ぐ. 恒久障害は (b) の
    crawl 末尾 resend pass + 次回 crawl の resend で救済する.
    """
    if not webhook:
        return False
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook)
        site_label = '🛒 メルカリ' if watch['site'] == 'mercari' else '🔨 ヤフオク'
        price_str = f"¥{hit.price_jpy:,}" if hit.price_jpy else "(価格不明)"
        range_str = _format_range(watch.get('price_min_jpy'), watch.get('price_max_jpy'))
        title = (hit.title or "")[:80]
        fields = [
            {'name': '価格', 'value': f"{price_str}  (希望 {range_str})", 'inline': True},
            {'name': 'キーワード', 'value': (watch.get('keyword') or '')[:80], 'inline': True},
            {'name': 'メモ', 'value': (watch.get('memo') or '—')[:200], 'inline': False},
        ]
        # W206: watch に紐付け済 eBay Item ID / 販売価格 (USD) を embed に追加。
        # ebay_item_id / _ebay_price は呼出側 (run_keyword_watch_crawl) が
        # batch helper で事前注入する (N+1 回避)。USD のみ表示 (JPY 併記しない)。
        ebay_item_id = watch.get('ebay_item_id')
        if ebay_item_id:
            fields.append({
                'name': 'eBay Item ID',
                'value': str(ebay_item_id),
                'inline': True,
            })
        ebay_price = watch.get('_ebay_price')
        if ebay_price is not None:
            fields.append({
                'name': 'eBay 販売価格',
                'value': f"${ebay_price:,.2f}",
                'inline': True,
            })
        embed = {
            'title': f"{site_label} 新着: {title}",
            'url': hit.url,
            'color': 3066993 if watch['site'] == 'mercari' else 15105570,
            'fields': fields,
        }
        if hit.image_url:
            embed['image'] = {'url': hit.image_url}
        content = f"🔔 キーワード新着 ({site_label}) hit_id={hit_id}"
        if notifier.send_message(content, embed=embed):
            return True
        time.sleep(1.0)  # backoff
        return bool(notifier.send_message(content, embed=embed))
    except Exception:
        logger.exception(f"W148 Discord send failed (watch_id={watch.get('id')}, hit_id={hit_id})")
        return False


def _send_discord_site_health(webhook: str, site: str, msg: str) -> bool:
    if not webhook:
        return False
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook)
        return bool(notifier.send_message(msg))
    except Exception:
        logger.exception(f"W148 site_health Discord send failed (site={site})")
        return False


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    config = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    # subprocess 起動 (daily_scheduler._run_keyword_watch_crawl) のため親の注入済 config を
    # 引き継げない。2026-05-25 .env 移行 (commit 8473103) で空になった webhook をここでも
    # in-memory 復元しないと通知ガードが silent skip する (code-reviewer HIGH-1 2026-05-29)。
    from notifiers.discord_notifier import inject_webhook_into_config
    return inject_webhook_into_config(config)


def run_keyword_watch_crawl(config: dict) -> dict:
    """W148 メイン処理。必ず dict を返す (Q0 偽装成功防止)。

    Returns:
        {success: bool, message: str, watches_crawled: int, new_hits: int,
         in_range_hits: int, errors: int, discord_sent: int,
         dom_rot_suspected: int, dom_rot_orphan_sites: list[str]}
    """
    summary = {
        "success": False,
        "message": "",
        "watches_crawled": 0,
        "new_hits": 0,
        "in_range_hits": 0,
        "errors": 0,
        "discord_sent": 0,
        "dom_rot_suspected": 0,
        "dom_rot_orphan_sites": [],
    }
    try:
        cfg = (config.get('tasks_enabled', {}) or {}).get('keyword_watch_crawl', {}) or {}
        if not cfg.get('enabled', True):
            return {**summary, "success": True, "message": "disabled"}

        webhook = (config.get('discord', {}) or {}).get('webhook_url') or ""
        watches = list_watches(active_only=True)
        max_per_run = int(cfg.get('max_watches_per_run', 30))
        sleep_sec = float(cfg.get('sleep_between_watches_sec', 4))

        # sentinel は必ず含める (site_health 集計の為)、残り枠を古い順 normal で埋める
        sentinels = [w for w in watches if w.get('is_sentinel')]
        non_sent = [w for w in watches if not w.get('is_sentinel')]
        watches = sentinels + non_sent[:max(0, max_per_run - len(sentinels))]

        # W206: 紐付け済 eBay listing の current_price を batch 取得 (N+1 回避)。
        # 各 watch dict に `_ebay_price` (Optional[float]) を注入し、
        # _send_discord_for_hit が embed に USD 価格を併記する。
        # ebay_item_id 未設定の watch は dict に key 自体が入らない (= embed 省略)。
        try:
            _ebay_prices = get_ebay_prices_for_item_ids(
                [w.get('ebay_item_id') for w in watches]
            )
        except Exception:
            # 価格取得失敗は通知 embed の付加情報なので主処理続行 (Q0: logger 痕跡化)
            logger.exception(
                "W148 get_ebay_prices_for_item_ids failed (embed の eBay 販売価格を省略)"
            )
            _ebay_prices = {}
        for w in watches:
            iid = w.get('ebay_item_id')
            if iid and iid in _ebay_prices:
                w['_ebay_price'] = _ebay_prices[iid]

        # サイト別 sentinel 集計 (v2.1 HIGH-B: per-watch 連続0件は alert fatigue)
        site_health: dict[str, dict[str, int]] = {}

        for w in watches:
            hits = []
            err = None
            try:
                # Codex HIGH-1: AlertCrawler 移植 watch は legacy URL に min/max/category_id/
                # 除外語が焼かれている。w['search_url'] を直接 page.goto に渡すことで
                # keyword 単独からの汎用 URL 再構築 (= URL filter 全消失) を回避する。
                if w['site'] == 'mercari':
                    from monitor.mercari_search import search_mercari
                    hits = search_mercari(
                        w['keyword'], max_results=10, headless=True,
                        search_url=w.get('search_url'),
                    )
                elif w['site'] == 'yahoo_auctions':
                    from monitor.yahoo_search import search_yahoo
                    hits = search_yahoo(
                        w['keyword'], max_results=10, headless=True,
                        search_url=w.get('search_url'),
                    )
                else:
                    logger.warning(f"W148 unsupported site: {w['site']} (watch_id={w['id']})")
                    update_watch_last_crawled(w['id'], error=f"unsupported site: {w['site']}")
                    continue
            except Exception as e:
                err = f"crawl error: {type(e).__name__}: {e}"
                summary["errors"] += 1
                logger.exception(f"W148 per-watch crawl failed (watch_id={w['id']})")

            # sentinel watch の結果を集計 (per-watch alert はしない)
            # Codex HIGH-2: exception path (browser launch fail / chromium OOM /
            # network outage) も site_health に sentinel_error として計上し、
            # all-error / mixed (zero+error == total) でも Discord 発火 = Q0 silent skip 防止.
            if w.get('is_sentinel'):
                st = site_health.setdefault(
                    w['site'],
                    {'sentinel_total': 0, 'sentinel_zero': 0, 'sentinel_error': 0},
                )
                st['sentinel_total'] += 1
                if err is not None:
                    st['sentinel_error'] += 1
                elif not hits:
                    st['sentinel_zero'] += 1

            for h in hits:
                in_range = _check_price_range(
                    h.price_jpy,
                    w.get('price_min_jpy'),
                    w.get('price_max_jpy'),
                )
                # sentinel は通知対象でない (price レンジ未設定で in_range=False になるが明示)
                if w.get('is_sentinel'):
                    in_range = False

                hit_id = record_hit_claim(
                    watch_id=w['id'],
                    found_item_url=h.url,
                    title=h.title or "",
                    price_jpy=h.price_jpy,
                    image_url=h.image_url,
                    in_price_range=in_range,
                )
                if hit_id is None:
                    continue  # 既知 URL (二重防止)
                summary["new_hits"] += 1
                if in_range:
                    summary["in_range_hits"] += 1
                    ok = _send_discord_for_hit(webhook, w, h, hit_id)
                    if ok:
                        mark_hit_notified(hit_id)
                        summary["discord_sent"] += 1

            update_watch_last_crawled(w['id'], error=err)
            summary["watches_crawled"] += 1
            time.sleep(sleep_sec)

        # site-level DOM/ban センチネルチェック (run 終了時)
        # Codex HIGH-2: zero + error の合計が total に達した場合も DOM rot として扱う
        # (= browser crash / network outage で全 sentinel が exception → 検知ゼロを防ぐ).
        for site, st in site_health.items():
            zero = st.get('sentinel_zero', 0)
            error = st.get('sentinel_error', 0)
            if st['sentinel_total'] > 0 and (zero + error) == st['sentinel_total']:
                if error and zero:
                    cause = f"全センチネル ({st['sentinel_total']}件) 異常: 0 件 {zero} / 例外 {error}"
                elif error:
                    cause = f"全センチネル ({st['sentinel_total']}件) 例外 = 巡回 crash"
                else:
                    cause = f"全センチネル ({st['sentinel_total']}件) が 0 件"
                msg = (
                    f"[W148 警告] {site}: {cause} = "
                    "DOM 変更 or bot ban の可能性。selector / user_agent / IP を点検してください。"
                )
                logger.warning(msg)
                _send_discord_site_health(webhook, site, msg)
                summary["dom_rot_suspected"] += 1

        # sentinel 未登録サイトの silent gap 防止 (Codex 3回目 MEDIUM + 4回目 HIGH-2)
        # Discord も発火させる (logger.warning だけだと R-11 視認不能 = Q0 silent skip).
        if cfg.get('sentinel_health_check_enabled', True):
            watched_sites = {w['site'] for w in watches}
            sentinel_sites = {s for s, st in site_health.items() if st['sentinel_total'] > 0}
            orphan_sites = sorted(watched_sites - sentinel_sites)
            if orphan_sites:
                msg = (
                    f"[W148 注意] sentinel 未登録サイト: {orphan_sites}。"
                    "DOM 変更/ban 自動検知が無効。UI「センチネル初期化」を実行推奨。"
                )
                logger.warning(msg)
                for site in orphan_sites:
                    _send_discord_site_health(webhook, site, msg)
                summary["dom_rot_orphan_sites"] = orphan_sites

        # Codex HIGH-3 (b): resend pass.
        # webhook 5xx / TLS hiccup で discord_sent=0 のまま残った in-range hit を
        # crawl 末尾で救済再送. _send_discord_for_hit 内の (a) 1 回 retry は
        # 一過性障害向け、本 pass は前回 crawl で永久 lost になりかけた hit の
        # 機会救済 (Section 232 派生品の高 value 機会を守る money-direct 経路).
        resend_pass_failed = False
        if cfg.get('webhook_resend_pass_enabled', True):
            try:
                unsent = get_unnotified_in_range_hits(
                    days=cfg.get('webhook_resend_days', 7),
                    limit=cfg.get('webhook_resend_limit', 200),
                )
            except Exception as e:
                # Codex 2 周目 HIGH-A: DB 例外を握りつぶし success=True にすると
                # recovery 経路自体が黙って死ぬ = Q0 偽装成功. R-11 視認可能化必須.
                logger.exception("W148 resend pass: get_unnotified_in_range_hits failed")
                summary["errors"] += 1
                resend_pass_failed = True
                _send_discord_site_health(
                    webhook, "resend_pass",
                    f"[W148 警告] resend pass DB error: {type(e).__name__}: {e} "
                    "= webhook 救済機構停止. monitor.db / migration v46 状態を点検."
                )
                unsent = []
            # W206: resend pass も batch で eBay 価格取得 (N+1 回避)。
            try:
                _resend_prices = get_ebay_prices_for_item_ids(
                    [u.get('ebay_item_id') for u in unsent]
                )
            except Exception:
                logger.exception(
                    "W148 resend pass: get_ebay_prices_for_item_ids failed "
                    "(embed の eBay 販売価格を省略)"
                )
                _resend_prices = {}
            resent = 0
            for u in unsent:
                # Codex 2 周目 HIGH-B: UI 巡回 + cron 巡回 並行で同 hit が
                # 二重 Discord に飛ぶ race を atomic claim で防ぐ.
                # claim_hit_for_resend = UPDATE ... WHERE discord_sent=0 で rowcount=1 のみ送信権獲得.
                if not claim_hit_for_resend(u['hit_id']):
                    continue  # 他 process が既に claim 済 → skip
                class _ResendHit:
                    pass
                rh = _ResendHit()
                rh.url = u['found_item_url']
                rh.title = u.get('title') or ''
                rh.price_jpy = u.get('price_jpy')
                rh.image_url = u.get('image_url')
                watch_view = {
                    'id': u['watch_id'],
                    'site': u['site'],
                    'keyword': u.get('keyword') or '',
                    'memo': u.get('memo') or '',
                    'price_min_jpy': u.get('price_min_jpy'),
                    'price_max_jpy': u.get('price_max_jpy'),
                    'ebay_item_id': u.get('ebay_item_id'),
                }
                _u_iid = u.get('ebay_item_id')
                if _u_iid and _u_iid in _resend_prices:
                    watch_view['_ebay_price'] = _resend_prices[_u_iid]
                if _send_discord_for_hit(webhook, watch_view, rh, u['hit_id']):
                    summary['discord_sent'] += 1
                    resent += 1
                else:
                    # 送信失敗 = claim を巻き戻して次回 crawl に retry させる
                    release_hit_resend_claim(u['hit_id'])
            if resent:
                logger.info(f"W148 resend pass: {resent}/{len(unsent)} 件救済送信")
            summary['discord_resent'] = resent

        # Codex 2 周目 HIGH-A: resend pass DB error (recovery 機構自体停止) のみ
        # success=False に倒す. per-watch errors は既存仕様で許容 (Discord で
        # DOM rot / orphan / sentinel error は site_health 経路で警告済).
        summary["success"] = not resend_pass_failed
        summary["message"] = (
            f"crawled={summary['watches_crawled']} new={summary['new_hits']} "
            f"in_range={summary['in_range_hits']} discord={summary['discord_sent']} "
            f"resent={summary.get('discord_resent', 0)} "
            f"err={summary['errors']} dom_rot={summary['dom_rot_suspected']} "
            f"orphan={len(summary['dom_rot_orphan_sites'])}"
            + (" RESEND_PASS_FAILED" if resend_pass_failed else "")
        )
        return summary
    except Exception as e:
        logger.exception("W148 run_keyword_watch_crawl top-level failure")
        summary["message"] = f"top-level failure: {type(e).__name__}: {e}"
        return summary  # success=False


if __name__ == "__main__":
    # subprocess entry: cron が `python -m tasks.task_keyword_watch_crawl` で起動。
    # config を読み込み、run_keyword_watch_crawl を呼び、結果を stdout JSON で出力。
    # success に応じて exit code (0/1) を返す = scheduler の _run_isolated_task が
    # exit code で started/completed/failed を task_execution_log に記録できる。
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    res = run_keyword_watch_crawl(_load_config())
    print(json.dumps(res, ensure_ascii=False, default=str))
    sys.exit(0 if res.get("success") else 1)
