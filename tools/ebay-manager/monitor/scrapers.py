"""
仕入元サイトの在庫チェック（Playwright + httpx フォールバック）
URLに直接アクセスして在庫テキストを検出する方式
"""
import asyncio
import logging
import random
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# ---- httpx ベースのチェック（高速・軽量） ----

def _check_with_httpx(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> Optional[str]:
    """httpx で HTML を取得しキーワード検索。判定不能なら None。"""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        if resp.status_code == 404:
            return "not_found"
        if resp.status_code != 200:
            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
            return None

        html = resp.text
        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    except httpx.TimeoutException:
        logger.debug(f"httpx timeout: {url}")
        return None
    except Exception as e:
        logger.debug(f"httpx error: {url}: {e}")
        return None


# ---- 判定ロジック ----

def _detect_status_single(
    content: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
    strict: bool = False,
) -> Optional[str]:
    """
    単一テキスト（HTML or rendered text）から判定。判定不能なら None。
    strict=True: 在庫有と在庫無が同時検出された場合は None（SPA対策）
    """
    active_np = [t for t in no_page_texts if t]
    active_so = [t for t in sold_out_texts if t]
    active_is = [t for t in in_stock_texts if t]

    found_np = any(t in content for t in active_np)
    found_so = any(t in content for t in active_so)
    found_is = active_is and any(t in content for t in active_is)

    # SPA対策: 在庫有・在庫無・ページなしが全て見つかる場合はJSテンプレート混入
    if strict and sum([found_np, found_so, found_is]) >= 2:
        logger.debug("Ambiguous detection (SPA?) - deferring to Playwright")
        return None

    if found_np:
        return "not_found"
    if found_so:
        return "unavailable"
    if found_is:
        return "available"
    return None


def _detect_status(
    content: str,
    rendered_text: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """レンダリング済みテキスト優先→HTML フォールバック。"""
    # Step 1: レンダリング済みテキスト（ユーザーに見える内容）で判定
    result = _detect_status_single(rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 2: HTML全体で判定（JSで動的生成される要素もカバー）
    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 3: strict無しで再判定（1つでも見つかれば判定）
    result = _detect_status_single(content + "\n" + rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
    return result or "unknown"


# ---- Playwright バッチチェック（ブラウザ再利用） ----

async def _check_urls_batch_async(
    items: list[dict],
    headless: bool = True,
    use_chrome: bool = False,
) -> dict[int, str]:
    """
    複数URLを1つのブラウザインスタンスで順次チェック。
    items: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    launch_opts = {
        "headless": headless,
        "args": ["--disable-http2", "--disable-blink-features=AutomationControlled"],
    }
    if use_chrome:
        launch_opts["channel"] = "chrome"

    browser = None
    try:
        async with async_playwright() as p:
            # ブラウザ起動のリトライ（最大3回）
            for launch_attempt in range(3):
                try:
                    browser = await p.chromium.launch(**launch_opts)
                    break
                except Exception as e:
                    logger.debug(f"Browser launch attempt {launch_attempt + 1} failed: {e}")
                    if launch_attempt < 2:
                        await asyncio.sleep(2)
                    else:
                        raise

            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()

            for item in items:
                item_id = item["id"]
                url = item["url"]
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.status == 404:
                        results[item_id] = "not_found"
                        continue

                    # SPA対策: networkidle + コンテンツ待機
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # SPA未レンダリング検出→追加待機（最大2回リトライ）
                    rendered_text = await page.inner_text("body")
                    for _ in range(2):
                        if len(rendered_text.strip()) >= 1500:
                            break
                        await asyncio.sleep(3)
                        rendered_text = await page.inner_text("body")

                    content = await page.content()

                    status = _detect_status(
                        content, rendered_text,
                        item["in_stock"], item["sold_out"], item["no_page"],
                    )
                    results[item_id] = status

                except PlaywrightTimeout:
                    logger.warning(f"Playwright timeout: {url}")
                    results[item_id] = "error"
                except Exception as e:
                    logger.warning(f"Playwright error: {url}: {e}")
                    results[item_id] = "error"

            if browser:
                await browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        for item in items:
            results.setdefault(item["id"], "error")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    return results


def _run_playwright_batch(items: list[dict], headless: bool = True, use_chrome: bool = False) -> dict[int, str]:
    """同期ラッパー"""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _check_urls_batch_async(items, headless=headless, use_chrome=use_chrome)
        )
    finally:
        loop.close()


# ---- 統合チェック（httpx → Playwright batch → Chrome headed batch） ----

def check_items_batch(items_with_config: list[dict]) -> dict[int, str]:
    """
    複数アイテムを効率的にチェック。
    items_with_config: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    playwright_needed = []
    chrome_needed = []

    # Step 1: httpx で高速チェック
    for item in items_with_config:
        result = _check_with_httpx(item["url"], item["in_stock"], item["sold_out"], item["no_page"])
        if result is not None:
            results[item["id"]] = result
        else:
            playwright_needed.append(item)

    # Step 2: Playwright headless（バッチ、ブラウザ1回起動）
    if playwright_needed:
        pw_results = _run_playwright_batch(playwright_needed, headless=True, use_chrome=False)
        for item in playwright_needed:
            status = pw_results.get(item["id"], "error")
            if status not in ("error", "unknown"):
                results[item["id"]] = status
            else:
                chrome_needed.append(item)

    # Step 3: Chrome headed（最終手段、バッチ）
    if chrome_needed:
        chrome_results = _run_playwright_batch(chrome_needed, headless=False, use_chrome=True)
        for item in chrome_needed:
            results[item["id"]] = chrome_results.get(item["id"], "error")

    return results


def check_url_sync_httpx_only(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """httpxのみで単一URLをチェック（Playwrightスキップ）"""
    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
    return result or "unknown"


def check_url_sync(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """単一URL同期チェック（httpx → Playwright fallback）"""
    # Step 1: httpx で高速チェック
    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
    if result is not None:
        return result

    # Step 2: Playwright で再試行（最大3回）
    for attempt in range(3):
        try:
            pw_result = _run_playwright_batch(
                [{
                    "id": 1,
                    "url": url,
                    "in_stock": in_stock_texts,
                    "sold_out": sold_out_texts,
                    "no_page": no_page_texts,
                }],
                headless=True,
                use_chrome=False,
            )
            status = pw_result.get(1, "unknown")
            if status not in ("error", "unknown"):
                return status
        except Exception as e:
            logger.debug(f"Playwright attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                import time
                time.sleep(2)  # 次回試行前に待機
            continue

    # Step 3: Chrome headed で最終試行
    try:
        pw_result = _run_playwright_batch(
            [{
                "id": 1,
                "url": url,
                "in_stock": in_stock_texts,
                "sold_out": sold_out_texts,
                "no_page": no_page_texts,
            }],
            headless=False,
            use_chrome=True,
        )
        status = pw_result.get(1, "unknown")
        return status
    except Exception as e:
        logger.debug(f"Chrome headed attempt failed: {e}")
        return "unknown"


def check_item_by_config(item: dict, site_config: dict) -> str:
    """アイテムとサイト設定から在庫チェック（単一アイテム）"""
    source_url = item.get("source_url", "")
    if not source_url:
        return "error"
    in_stock = [site_config.get("in_stock_text1", ""), site_config.get("in_stock_text2", "")]
    sold_out = [site_config.get("sold_out_text", "")]
    no_page = [site_config.get("no_page_text", "")]
    return check_url_sync(source_url, in_stock, sold_out, no_page)


def prepare_batch_items(items: list[dict], configs_by_prefix: dict) -> list[dict]:
    """DB アイテムリストをバッチチェック用の形式に変換"""
    batch = []
    for item in items:
        sku = item.get("sku", "")
        source_url = item.get("source_url", "")
        if not source_url:
            continue
        cfg = None
        for prefix, c in configs_by_prefix.items():
            if sku.startswith(prefix):
                cfg = c
                break
        if not cfg:
            continue
        batch.append({
            "id": item["id"],
            "url": source_url,
            "in_stock": [cfg.get("in_stock_text1", ""), cfg.get("in_stock_text2", "")],
            "sold_out": [cfg.get("sold_out_text", "")],
            "no_page": [cfg.get("no_page_text", "")],
        })
    return batch
