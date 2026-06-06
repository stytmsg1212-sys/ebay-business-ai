#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W226 (2026-06-06): 任意の EC サイト (Amazon / 楽天 / Yahoo!ショッピング /
ラクマ 等) から HTML 本文を取得するモジュール.

設計方針:
  - 仕入先フリマ (ヤフオク / メルカリ / PayPay) は monitor.supplier_scraper の
    専用 DOM パーサが既にあるため本モジュールは使わない。本モジュールは
    「専用パーサが無い汎用 EC サイト」を AI 解析 (ai_html_parser) に渡すための
    生 HTML を取得する役割に徹する。
  - httpx を primary (高速・軽量、SSR HTML はこれで十分なことが多い)。
  - anti-bot ページ (Robot Check / CAPTCHA) や本文不足を検知したら Playwright に
    escalation。Streamlit (Windows) 配下では asyncio SelectorEventLoop 制約で
    sync_playwright が直接動かないため、supplier_scraper と同じく subprocess に
    隔離して実行する。
  - 失敗時は例外を投げず (html, error) の tuple で返す (呼出側 UI を壊さない)。
    どちらか一方が必ず None。Q0: 取得失敗を silent に空文字へ畳まない。
"""
from __future__ import annotations

import logging
import os
import random
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# supplier_scraper と同じ subprocess 隔離判定を流用 (DRY)。
from monitor.supplier_scraper import _should_isolate_playwright

_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
)

# 本文がこれ未満なら JS 描画前の空シェル / anti-bot とみなし escalation する。
_MIN_BODY_CHARS = 1500


def _looks_blocked(html: Optional[str]) -> bool:
    """anti-bot ページ or 本文不足を判定。True なら Playwright escalation 対象。"""
    if not html or len(html) < _MIN_BODY_CHARS:
        return True
    low = html.lower()
    # W183 (2026-05-28 scrapers.py) と同じ Amazon anti-bot マーカー
    if "robot check" in low or "validatecaptcha" in low:
        return True
    if "captcha" in low and "enter the characters" in low:
        return True
    return False


def _fetch_httpx(url: str, timeout_sec: int) -> tuple[Optional[str], Optional[str]]:
    """httpx で HTML を取得。返値 (html, error)。"""
    import httpx

    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    }
    try:
        resp = httpx.get(
            url, headers=headers, timeout=timeout_sec, follow_redirects=True,
        )
    except httpx.TimeoutException:
        return None, "httpx_timeout"
    except Exception as e:  # noqa: BLE001
        return None, f"httpx_error: {type(e).__name__}: {e}"

    if resp.status_code == 404:
        return None, "http_404"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    return resp.text, None


def _fetch_playwright_inproc(
    url: str, timeout_sec: int,
) -> tuple[Optional[str], Optional[str]]:
    """sync_playwright で HTML を取得 (in-process)。返値 (html, error)。

    subprocess 隔離スクリプトからも呼ばれる (CLI / scheduler 文脈では直接)。
    """
    try:
        from playwright.sync_api import (
            sync_playwright, TimeoutError as PWTimeoutError,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"playwright_import_failed: {e}"

    timeout_ms = timeout_sec * 1000
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=random.choice(_USER_AGENTS),
                    locale="ja-JP",
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PWTimeoutError:
                    return None, "playwright_goto_timeout"
                # SPA 描画を少し待つ (本文が出てくるまで)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except PWTimeoutError:
                    pass
                html = page.content()
                return (html or None), (None if html else "playwright_empty")
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        return None, f"playwright_error: {type(e).__name__}: {e}"


def _fetch_playwright_subprocess(
    url: str, timeout_sec: int,
) -> tuple[Optional[str], Optional[str]]:
    """Python 子プロセスで Playwright fetch を実行し base64-JSON で結果受取。

    supplier_scraper._scrape_via_subprocess と同じく Streamlit の asyncio
    SelectorEventLoop 制約を回避する。HTML は大きく非 ASCII を含むため
    base64(utf-8) で受け渡す (stdout encoding 非依存)。
    """
    import json as _json
    import subprocess

    script = (
        "import json, base64, sys; "
        "from monitor.html_fetcher import _fetch_playwright_inproc; "
        "url, t = sys.argv[1], int(sys.argv[2]); "
        "html, err = _fetch_playwright_inproc(url, t); "
        "print(json.dumps({"
        "'html_b64': base64.b64encode(html.encode('utf-8')).decode('ascii') if html else None, "
        "'error': err}, ensure_ascii=True))"
    )
    env = dict(os.environ)
    env["EBAY_MANAGER_SCRAPE_SUBPROCESS"] = "0"  # 子では in-process 実行
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    project_root = str(Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, url, str(timeout_sec)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout_sec + 40, env=env, cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        return None, "subprocess_playwright_timeout"
    except Exception as e:  # noqa: BLE001
        return None, f"subprocess_launch_failed: {type(e).__name__}: {e}"

    if proc.returncode != 0:
        logger.warning(
            "playwright subprocess returncode=%s stderr=%r",
            proc.returncode, (proc.stderr or "")[:500],
        )
        return None, f"subprocess_returncode_{proc.returncode}"

    try:
        data = _json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        return None, f"subprocess_json_parse_failed: {e}"

    err = data.get("error")
    b64 = data.get("html_b64")
    if not b64:
        return None, err or "playwright_empty"
    try:
        import base64
        html = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return None, f"base64_decode_failed: {e}"
    return html, None


def _fetch_playwright(
    url: str, timeout_sec: int,
) -> tuple[Optional[str], Optional[str]]:
    """環境に応じて subprocess 隔離 or in-process で Playwright fetch。"""
    if _should_isolate_playwright():
        html, err = _fetch_playwright_subprocess(url, timeout_sec)
        if html is not None:
            return html, None
        return None, err
    return _fetch_playwright_inproc(url, timeout_sec)


def fetch_page_html(
    url: str, timeout_sec: int = 20, force_playwright: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """任意の EC サイトから HTML 本文を取得する。

    httpx を primary、anti-bot / 本文不足を検知したら Playwright に escalation。
    force_playwright=True なら httpx をスキップし最初から Playwright で取得する
    (httpx HTML が長いのに content-poor = JS 描画前シェル のサイト向け再取得経路。
    呼出側 resolver が AI 解析失敗を観測した時に使う)。

    Args:
        url: 取得対象 URL
        timeout_sec: httpx / Playwright タイムアウト (秒)
        force_playwright: True で httpx を飛ばし Playwright 直行

    Returns:
        (html, error)。成功時 (html, None)、失敗時 (None, error_message)。
        どちらか一方が必ず None。
    """
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return None, "invalid_url"

    url = url.strip()

    if force_playwright:
        html2, err2 = _fetch_playwright(url, timeout_sec)
        if html2 and not _looks_blocked(html2):
            return html2, None
        return None, err2 or "playwright_insufficient"

    # Step 1: httpx (高速)
    html, err = _fetch_httpx(url, timeout_sec)
    if html and not _looks_blocked(html):
        return html, None

    httpx_blocked = bool(html) and _looks_blocked(html)
    logger.debug(
        "httpx insufficient for %s (err=%s blocked=%s) -> playwright escalation",
        url, err, httpx_blocked,
    )

    # Step 2: Playwright escalation
    html2, err2 = _fetch_playwright(url, timeout_sec)
    if html2 and not _looks_blocked(html2):
        return html2, None

    # 両経路失敗 → 明示エラー (Q0: silent な空応答を作らない)
    if html2 and _looks_blocked(html2):
        return None, f"anti_bot_blocked (playwright body insufficient, httpx={err or 'blocked'})"
    return None, err2 or err or "fetch_failed"


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    _html, _err = fetch_page_html(args.url, timeout_sec=args.timeout)
    if _err:
        print(f"ERROR: {_err}")
    else:
        print(f"OK: {len(_html)} chars")
        print(_html[:800])
