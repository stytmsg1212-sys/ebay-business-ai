"""
燃料サーチャージ週次自動取得タスク
FedEx/DHLの公式サイトから最新の燃料サーチャージ%を取得し、
settings.json に反映する。取得失敗時はDiscordへ通知。

データソース:
- FedEx: https://www.fedex.com/ja-jp/shipping/surcharges.html (bot対策あり)
- DHL: https://mydhl.express.dhl/jp/ja/ship/surcharges.html

失敗した側はsettings.jsonの既存値を維持する（片側だけ更新でも良い）
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows CP932対策: 親プロセスが utf8_console 未import でも安全に自己適用
_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
try:
    import utf8_console  # noqa: F401
except Exception:
    pass

import httpx

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = BASE_DIR / "settings.json"

FEDEX_URL = "https://www.fedex.com/ja-jp/shipping/surcharges.html"
DHL_URL = "https://mydhl.express.dhl/jp/ja/ship/surcharges.html"

# bot対策回避のため通常ブラウザのUAを使う
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _fetch_html(url: str, timeout: float = 20.0) -> tuple[Optional[str], Optional[str]]:
    """HTMLを取得。成功 (html, None)、失敗 (None, error_msg)"""
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text, None
    except Exception as e:
        return None, f"取得失敗: {type(e).__name__}: {e}"


def _extract_dhl_surcharge(html: str) -> Optional[float]:
    """
    DHLページから現在の週の燃料サーチャージ%を抽出。

    戦略: ページには「週範囲+%値」の表が並ぶ。今日の日付を含む週範囲を特定して、
    その行の%値を取る。日付テーブル以外の%値（ガソリン価格帯の上限など）を
    誤検出しないよう、日付パターンと%値が近接する箇所だけ拾う。
    """
    today = datetime.now()
    today_md = (today.month, today.day)

    # パターン: 「4月27日～5月3日」「2026年4月27日～5月3日」「4/27〜5/3」
    # 月日範囲 + %値 が近接するブロックを探す
    date_range_patterns = [
        # 4月27日～5月3日: 48.00%
        re.compile(
            r'(\d{1,2})月\s*(\d{1,2})日[～〜\-–]+\s*(?:(\d{1,2})月\s*)?(\d{1,2})日'
            r'.{0,80}?(\d{2,3}\.\d{1,2})\s*%',
            re.DOTALL,
        ),
    ]

    candidates: list[tuple[tuple[int, int], tuple[int, int], float]] = []
    for pat in date_range_patterns:
        for m in pat.finditer(html):
            try:
                sm = int(m.group(1))
                sd = int(m.group(2))
                em_mo = int(m.group(3)) if m.group(3) else sm
                ed = int(m.group(4))
                pct = float(m.group(5))
                if not (20 <= pct <= 60):
                    continue
                candidates.append(((sm, sd), (em_mo, ed), pct))
            except (ValueError, TypeError):
                continue

    if not candidates:
        return None

    # 今日の日付を含む範囲を最優先
    def in_range(today_md, start_md, end_md):
        # 年跨ぎは考慮しない（燃料サーチャージは週次更新なので非該当）
        return start_md <= today_md <= end_md

    in_range_hits = [pct for (s, e, pct) in candidates if in_range(today_md, s, e)]
    if in_range_hits:
        # 複数一致なら最大（保守的）
        return max(in_range_hits)

    # 含まれる範囲がない場合: 最も未来に近い開始日の行を採用（=来週分が先取り表示されているケース）
    future_hits = [(s, pct) for (s, e, pct) in candidates if s >= today_md]
    if future_hits:
        future_hits.sort(key=lambda x: x[0])
        return future_hits[0][1]

    # それでもなければNone（大雑把な最大値は返さない方が安全）
    return None


def _extract_fedex_surcharge(html: str) -> Optional[float]:
    """
    FedExページから燃料サーチャージ%を抽出。
    FedExはbot対策が厳しく、HTMLが取れても値が埋め込まれていない可能性が高い。
    """
    pct_matches = re.findall(r'(\d{1,3}\.\d{1,2})\s*%', html)
    if not pct_matches:
        return None
    candidates = [float(p) for p in pct_matches if 20 <= float(p) <= 60]
    if not candidates:
        return None
    return max(candidates)


def _send_discord_notification(webhook_url: str, message: str) -> None:
    """Discordに通知（失敗しても黙って続行）"""
    if not webhook_url:
        return
    try:
        httpx.post(webhook_url, json={"content": message}, timeout=10)
    except Exception as e:
        logger.warning(f"Discord通知失敗: {e}")


def run_fuel_surcharge_check(config: dict) -> dict:
    """
    メイン処理: FedEx/DHLから取得 → settings.json 更新 → 変更検知で通知

    Returns:
        {
            'success': bool,
            'fedex_rate': float or None,
            'dhl_rate': float or None,
            'fedex_error': str or None,
            'dhl_error': str or None,
            'changed': bool,
        }
    """
    # 現在の設定を読み込む
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        settings = json.load(f)

    old_fedex = float(settings.get('fuel_surcharge_fedex', 0))
    old_dhl = float(settings.get('fuel_surcharge_dhl', 0))

    result = {
        'success': False,
        'fedex_rate': None,
        'dhl_rate': None,
        'fedex_error': None,
        'dhl_error': None,
        'changed': False,
    }

    # ─── DHL ───
    logger.info(f"DHL燃料サーチャージ取得: {DHL_URL}")
    dhl_html, dhl_err = _fetch_html(DHL_URL)
    if dhl_err:
        result['dhl_error'] = dhl_err
        logger.warning(f"DHL取得失敗: {dhl_err}")
    else:
        dhl_rate = _extract_dhl_surcharge(dhl_html)
        if dhl_rate is None:
            result['dhl_error'] = "HTMLから燃料サーチャージ値を抽出できませんでした"
            logger.warning(result['dhl_error'])
        else:
            result['dhl_rate'] = dhl_rate
            logger.info(f"DHL: {dhl_rate}% (旧値 {old_dhl}%)")

    # ─── FedEx ───
    logger.info(f"FedEx燃料サーチャージ取得: {FEDEX_URL}")
    fedex_html, fedex_err = _fetch_html(FEDEX_URL)
    if fedex_err:
        result['fedex_error'] = fedex_err
        logger.warning(f"FedEx取得失敗: {fedex_err}")
    else:
        fedex_rate = _extract_fedex_surcharge(fedex_html)
        if fedex_rate is None:
            result['fedex_error'] = "HTMLから燃料サーチャージ値を抽出できませんでした（bot対策の可能性）"
            logger.warning(result['fedex_error'])
        else:
            result['fedex_rate'] = fedex_rate
            logger.info(f"FedEx: {fedex_rate}% (旧値 {old_fedex}%)")

    # settings.json 更新
    # 安全策: 旧値との差が5pt以上ある場合は自動反映せず、ユーザー確認を促す
    AUTO_APPLY_THRESHOLD_PT = 5.0
    now_iso = datetime.now().isoformat(timespec='seconds')
    changed = False
    rejected_large_change: list[str] = []

    def _apply_if_safe(carrier: str, new_rate: float, old_rate: float, key: str) -> bool:
        delta = abs(new_rate - old_rate)
        if delta < 0.01:
            return False  # 変動なし
        if delta >= AUTO_APPLY_THRESHOLD_PT:
            # 大きすぎる変動は自動反映せず通知だけ
            rejected_large_change.append(
                f"{carrier}: {old_rate}% → {new_rate}% (差{delta:.2f}pt、自動反映はスキップ)"
            )
            logger.warning(f"{carrier} 大幅変動のため自動反映スキップ: {old_rate}→{new_rate}")
            return False
        settings[key] = round(new_rate, 2)
        return True

    if result['dhl_rate'] is not None:
        if _apply_if_safe('DHL', result['dhl_rate'], old_dhl, 'fuel_surcharge_dhl'):
            changed = True
    if result['fedex_rate'] is not None:
        if _apply_if_safe('FedEx', result['fedex_rate'], old_fedex, 'fuel_surcharge_fedex'):
            changed = True

    # 最終更新日時は取得成功時のみ更新（反映スキップでも「チェック実施済み」として）
    if result['dhl_rate'] is not None or result['fedex_rate'] is not None:
        settings['fuel_surcharge_last_updated'] = now_iso
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        logger.info(f"settings.json 更新完了 (changed={changed})")

    result['rejected_large_change'] = rejected_large_change

    result['changed'] = changed
    # 少なくとも片方が成功すればsuccess
    result['success'] = result['dhl_rate'] is not None or result['fedex_rate'] is not None

    # Discord通知（失敗 or 大幅変動時）
    webhook = (config.get('discord', {}) or {}).get('webhook_url') or settings.get('discord_webhook_url', '')
    notify_msgs = []

    if result['fedex_error']:
        notify_msgs.append(f":warning: FedEx燃料サーチャージ取得失敗\n{result['fedex_error']}\n手動確認: {FEDEX_URL}")
    if result['dhl_error']:
        notify_msgs.append(f":warning: DHL燃料サーチャージ取得失敗\n{result['dhl_error']}\n手動確認: {DHL_URL}")

    # 大幅変動（5pt以上）は自動反映していないので、手動確認を促す
    for msg in rejected_large_change:
        notify_msgs.append(
            f":chart_with_upwards_trend: 燃料サーチャージ大幅変動検知（要手動確認）\n{msg}\n"
            f"FedEx: {FEDEX_URL}\nDHL: {DHL_URL}\n"
            f"eBay Manager 設定タブから手動で値を更新してください"
        )

    if notify_msgs:
        _send_discord_notification(webhook, "\n\n".join(notify_msgs))

    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    import sys
    config_path = BASE_DIR / 'config' / 'schedule_config.json'
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)
    result = run_fuel_surcharge_check(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['success'] else 1)
