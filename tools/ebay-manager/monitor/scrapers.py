"""
仕入元サイトの在庫チェック（Playwright + httpx フォールバック）
URLに直接アクセスして在庫テキストを検出する方式
"""
import asyncio
import logging
import random
import re
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

def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
    """楽天 HIDDEN_STOCK ショップは在庫数を隠し、購入可能でも quantity:0 +
    schema.org/OutOfStock を返す。本体 purchaseInfo.purchaseBySellType 直下の
    purchaseCondition のみが信頼できる在庫信号:
      enabled                 -> available
      それ以外 (sold-out 等)  -> unavailable

    ⚠️ displayNormalCartButton は在庫信号にしない: 実 OOS サンプル
    (data/tmp/ec_direct_url_probe/rakuten_oos_raw.html) で sold-out 品でも true
    のため、true->available にすると売り切れを在庫ありと誤判定する (受注後仕入れ
    不能 = eBay Defect 直結。code-review 2026-05-28 CRITICAL-1)。

    本体スコープ限定 (purchaseBySellType アンカー) で、関連商品/レコメンド/
    バンドルの purchaseCondition 混入による mirror 誤判定を防ぐ (HIGH-1)。
    """
    if "item.rakuten" not in url.lower():
        return None

    m = re.search(
        r'"purchaseBySellType"\s*:\s*\{\s*"purchaseCondition"\s*:\s*"([^"]+)"',
        html,
    )
    if not m:
        # 本体 purchaseCondition 不在 -> None で Playwright fallback へ
        # (silent に在庫あり扱いを作らない / Q0)
        return None
    condition = m.group(1)
    if condition == "enabled":
        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
        return "available"
    logger.debug(f"Rakuten purchaseCondition={condition!r} (not enabled) -> unavailable: {url}")
    return "unavailable"


# 「出荷日X日（数量: N～M）」= オリエンタルモーターの唯一信頼できる注文可能シグナル.
# 「在庫なし」「404」「生産終了」は在庫ありページにもテンプレ/JS で常駐 = 罠なので不使用.
# tag_tolerant (code-review HIGH-3): 出荷日 と （数量 の間に HTML タグ (<span> 等) や
# 桁数増が挟まっても取りこぼさない (実機 在庫あり=match / 404=non-match を確認 2026-06-03).
_ORIENTALMOTOR_AVAIL_RE = re.compile(
    r"出荷日(?:<[^>]*>|[^（(]){0,20}[（(](?:<[^>]*>|[^）)]){0,25}数量"
)


def _detect_orientalmotor_status(url: str, html: str) -> Optional[str]:
    """オリエンタルモーター WEB ショップ (orientalmotor-shop.jp) 専用在庫判定.

    受注生産型で「在庫切れ」状態がほぼ無く、注文可否は本体の
    「出荷日X日（数量: N～M）」シグナルでのみ確実に判定できる:
      シグナル present                 -> available   (注文可能)
      シグナル absent かつ 本文体裁OK   -> unavailable (削除/生産終了/受注停止)
      シグナル absent かつ 本文不十分   -> None         (部分取得/anti-bot → fallback)

    ⚠️ 「在庫なし」「404」「生産終了」テキストは在庫ありページにもテンプレ/JS で
    常駐する (raw HTML に埋め込み) ため在庫信号にしない。使うと全件誤判定 (楽天
    HIDDEN_STOCK と同類の罠)。シグナルは raw HTML に server-side (UTF-8) で含まれ
    httpx で確実取得可。

    HIGH-1 fix (code-review 2026-06-03): signal absent を即 unavailable 確定すると、
    httpx 部分取得 / anti-bot / 空応答で **在庫あり品を false-OOS** (オーバーセル/Defect/
    機会損失) する。楽天 _detect_rakuten_purchase_status と同じ保守姿勢で、本文が商品
    ページ体裁 (orientalmotor 含む + 十分長) を取れた時のみ unavailable を確定し、取得
    異常は None (Playwright fallback) に逃がす (Q0: silent な確定を作らない)。

    出典: 2026-06-03 Playwright 実機調査。在庫あり 21 件 (カテゴリ OP 全 20 + 旧型番
    PK264-01A) で出荷日(数量)シグナル present、404 スタブ (NONEXISTENT / US590) で absent
    を確認。charset=UTF-8 で httpx デコード正常。user 承認済 (option B 専用コード)。
    """
    if "orientalmotor-shop" not in url.lower():
        return None
    if _ORIENTALMOTOR_AVAIL_RE.search(html):
        logger.debug(f"orientalmotor 出荷日(数量) signal -> available: {url}")
        return "available"
    # signal absent: 本文体裁 sanity check. 不十分なら確定せず fallback (false-OOS 防止).
    page_ok = ("orientalmotor" in html.lower()) and len(html) > 2000
    if not page_ok:
        logger.warning(
            "orientalmotor 本文不十分 (len=%d) -> 判定保留(fallback): %s",
            len(html), url,
        )
        return None
    logger.debug(f"orientalmotor 出荷日(数量) absent (本文OK) -> unavailable: {url}")
    return "unavailable"


def _detect_paypay_signals(html: str) -> tuple[Optional[str], str]:
    """PayPay フリマ raw HTML から在庫状態を判定。(status|None, signal) を返す。

    W182 gate (`_check_paypay_availability`) と定時在庫監視 (`_check_with_httpx`) の
    共有判定 (依頼ボード#14 2026-06-12: 定時監視へ配線、シグナル 2 重管理を防ぐ)。

    確実シグナル (2026-06-17 実機検証で更新):
      この商品は存在しません -> not_found
      schema.org/OutOfStock または "status":"SOLD" -> unavailable
      schema.org/InStock または "status":"OPEN"   -> available
    実機比較 (売切3件 / 在庫あり4件): 売切=OutOfStock+SOLD、在庫あり=InStock+OPEN で
    クリーンに分離。誤OOS(=オーバーセル/Defect)も誤在庫ありも出さない両側検証済。

    背景 (2026-06-17 依頼ボード: 売切候補が次々提示される事故):
      PayPay が HTML 構造を変更し、旧 server-side シグナル('購入日時'/'購入手続きへ'/
      '"SoldOut"'/'関連商品をアプリで探す')が raw HTML から消失 → 全候補が
      'no signal matched'=unknown 化 → 売切が検出されず提示され続けていた。
      schema.org availability(JSON-LD)と __NEXT_DATA__ の "status" が新しい確実シグナル。
    旧シグナルは消えても害は無いため fallback として残す(将来の HTML 揺れ対策)。
    """
    if 'この商品は存在しません' in html:
        return 'not_found', 'no_page_text'

    # --- 新・確実シグナル (2026-06-17): 売切 (unavailable) を最優先で確定 ---
    # 売切ページに stale InStock が混在しても OutOfStock/SOLD を先に見るため誤判定しない。
    if 'schema.org/OutOfStock' in html:
        return 'unavailable', 'schema OutOfStock'
    if re.search(r'"status"\s*:\s*"SOLD"', html):
        return 'unavailable', 'NEXT_DATA status SOLD'

    # 旧 sold_out signals (HTML 揺れ対策の fallback)
    if '購入日時' in html:
        return 'unavailable', '購入日時 in HTML'
    if '"SoldOut"' in html or "'SoldOut'" in html:
        return 'unavailable', 'SoldOut JSON-LD'
    if '関連商品をアプリで探す' in html:
        return 'unavailable', 'related items text'

    # --- 在庫あり (available): 売切シグナルが無いことを確認した後にのみ判定 ---
    if 'schema.org/InStock' in html:
        return 'available', 'schema InStock'
    if re.search(r'"status"\s*:\s*"OPEN"', html):
        return 'available', 'NEXT_DATA status OPEN'
    if '購入手続きへ' in html:
        return 'available', '購入手続きへ'

    return None, 'no signal matched'


def _detect_yahoo_auction_status(url: str, html: str) -> Optional[str]:
    """ヤフオク __NEXT_DATA__ 埋込 JSON の status で在庫判定 (依頼ボード#17 D, 2026-06-12)。

    site_configs のテキストシグナル (入札する / 今すぐ落札 / このオークションは終了) は
    オークション形式専用で、**定額 (フリマ形式) 出品は「購入手続き」ボタンのみ** →
    全シグナル不一致で『不明』stuck していた (GS-71N5 実例: status='open' なのに不明)。
    埋込 JSON の status は出品形式に依らず server-side で必ず入るため最優先で使う。
      status='open' → available / status='closed' → unavailable / JSON 取れない → None
    (None は既定のテキスト判定 + Playwright fallback へ — 確定を作らない / Q0)
    """
    if "page.auctions.yahoo.co.jp" not in url.lower():
        return None
    try:
        from monitor.yahoo_auction_status import _extract_yahoo_item
        item = _extract_yahoo_item(html)
    except Exception as e:
        logger.debug(f"yahoo __NEXT_DATA__ parse error: {url}: {e}")
        return None
    if not item:
        return None
    status = item.get("status")
    if status == "open":
        return "available"
    if status == "closed":
        return "unavailable"
    return None


def _status_for_404(content: str, in_stock_texts: list[str], sold_out_texts: list[str], no_page_texts: list[str]) -> str:
    """404 レスポンスの本文をキーワード判定し not_found / unavailable を返す。

    ヤフオク終了ページのように HTTP 404 を返しつつ本文に「このオークションは終了」
    等の sold_out / no_page シグナルを含む場合を正しく分類する。

    規則:
      - 判定結果が unavailable または not_found → そのまま採用
      - 判定結果が available または None → not_found を返す
        (404 ページの汎用テンプレに in_stock 文字列が残っても available と誤判定しない安全弁)
    """
    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
    if result in ("unavailable", "not_found"):
        return result
    return "not_found"


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
            # 404 でも本文キーワードを先に判定 (ヤフオク終了ページ等)。
            # 明示シグナル (sold_out / no_page) があればそのまま採用。
            # キーワード無し (available/None) は not_found 確定にせず None を返し
            # Playwright fallback に逃がす — JS 描画後にのみ「このオークションは終了」
            # が出る 404 ページを在庫無と正しく拾うため (2026-06-11 実機検証で発見)。
            status_404 = _detect_status_single(
                resp.text, in_stock_texts, sold_out_texts, no_page_texts, strict=False
            )
            if status_404 in ("unavailable", "not_found"):
                return status_404
            return None
        if resp.status_code != 200:
            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
            return None

        html = resp.text
        # W183 (2026-05-28): Amazon 等の anti-bot ページ (Robot Check / CAPTCHA)
        # は在庫判定不能 = unknown 扱い (None で Playwright fallback)。在庫切れと
        # 誤認すると不要な値下げ / 出品停止に直結するため必ず unknown に倒す。
        low = html.lower()
        if "robot check" in low or "validatecaptcha" in low:
            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
            return None
        rakuten_status = _detect_rakuten_purchase_status(url, html)
        if rakuten_status is not None:
            return rakuten_status
        om_status = _detect_orientalmotor_status(url, html)
        if om_status is not None:
            return om_status
        ya_status = _detect_yahoo_auction_status(url, html)
        if ya_status is not None:
            return ya_status
        if "paypayfleamarket.yahoo.co.jp" in url.lower():
            # 依頼ボード#14 (2026-06-12): W182 の確実シグナルを定時監視にも配線。
            # site_configs シグナルは JS 描画後のみで httpx 段では全滅 → 不明 stuck。
            pp_status, pp_signal = _detect_paypay_signals(html)
            if pp_status is not None:
                logger.debug(f"PayPay raw signal ({pp_signal}) -> {pp_status}: {url}")
                return pp_status
            # シグナル無し → 既定判定 + Playwright fallback へ (確定を作らない / Q0)
        if "item.rakuten" in url.lower():
            rakuten_sold_out_texts = [
                t for t in sold_out_texts
                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
            ]
            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
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
    # W192 (2026-05-30): bool() で包む. active_is が空 (Yahoo!ショッピングは在庫有 clean
    # marker 不在で in_stock_text='') の場合 `[] and ...` が空リストを返し、後段
    # sum([found_np, found_so, found_is]) が int+list で TypeError → クラッシュしていた.
    # 既存設定は全て in_stock_text 非空でこの経路を踏まなかったため latent だった.
    found_is = bool(active_is) and any(t in content for t in active_is)

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
                    is_404 = response and response.status == 404

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

                    if is_404:
                        # 404 でも本文を取得済み: キーワード判定して unavailable/not_found のみ採用
                        results[item_id] = _status_for_404(
                            content + "\n" + rendered_text,
                            item["in_stock"], item["sold_out"], item["no_page"],
                        )
                    else:
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
    """DB アイテムリストをバッチチェック用の形式に変換。

    W183 (2026-05-28): SKU prefix に一致しない直接 URL 監視 (source_url_manual=1 の
    Amazon/楽天 等、SKU 規則性の無い EC) は source_url の url_keyword で site_config を
    解決する fallback を追加。除外したものは件数と理由をログに残す (Q0 silent-skip 防止)。
    """
    batch = []
    dropped_no_url = 0
    dropped_no_config: list[dict] = []
    for item in items:
        sku = item.get("sku", "")
        source_url = item.get("source_url", "")
        if not source_url:
            dropped_no_url += 1
            continue
        cfg = None
        # 1) SKU prefix 一致 (従来の無在庫 ebay**_ SKU)
        for prefix, c in configs_by_prefix.items():
            if prefix and sku.startswith(prefix):
                cfg = c
                break
        # 2) W183 fallback: prefix 不一致は source_url の url_keyword で site 解決
        if cfg is None:
            for c in configs_by_prefix.values():
                kw = c.get("url_keyword", "")
                if kw and kw in source_url:
                    cfg = c
                    break
        if cfg is None:
            dropped_no_config.append(
                {"id": item.get("id"), "sku": sku, "url": source_url}
            )
            continue
        batch.append({
            "id": item["id"],
            "url": source_url,
            "in_stock": [cfg.get("in_stock_text1", ""), cfg.get("in_stock_text2", "")],
            "sold_out": [cfg.get("sold_out_text", "")],
            "no_page": [cfg.get("no_page_text", "")],
        })
    if dropped_no_url or dropped_no_config:
        logger.info(
            "[prepare_batch_items] 除外: no_source_url=%d site_config_missing_url=%d (対象 %d 件)",
            dropped_no_url, len(dropped_no_config), len(items),
        )
        for d in dropped_no_config[:20]:
            logger.warning(
                "[prepare_batch_items] site_config_missing_url id=%s sku=%r url=%s",
                d["id"], d["sku"], d["url"],
            )
    return batch


# ============================================================================
# W182 (2026-05-28): 候補 URL の在庫 gate
# ============================================================================
# sold_out 商品を supplier_candidates に登録する bug の恒久対策。
# task_supplier_candidate_search.py + task_supplier_sweep.py の発見ロジックから
# 評価 / 登録の前に呼ぶ。raw HTML レベルで sold_out signal を確実に拾うため、
# PayPay / Yahoo Auctions は専用 logic、他は既存 site_configs を流用。
#
# 設計根拠 (Codex 2026-05-28 調査):
# - PayPay フリマは raw HTML に "InStock" (古い ld+json) と "SoldOut" が混在
# - 既存 site_configs の `関連商品をアプリで探す` は JS 描画後にしか出ない
# - raw HTML で確実に検出できる signal: 購入日時 (購入済の確定 signal)、SoldOut
# 詳細: .company/engineering/migration/codex-supplier-bug-investigation.md
# ============================================================================

from datetime import datetime, timezone


_AVAILABILITY_HTTPX_TIMEOUT = 10
_AVAILABILITY_HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
}


def _avail_headers() -> dict:
    h = dict(_AVAILABILITY_HEADERS_BASE)
    h["User-Agent"] = random.choice(USER_AGENTS)
    return h


def check_candidate_availability(url: str, timeout_sec: int = _AVAILABILITY_HTTPX_TIMEOUT) -> dict:
    """
    候補 URL の在庫状態を判定し、availability dict を返す。

    Returns: {
        'status':       'available' | 'unavailable' | 'not_found' | 'unknown',
        'signal':       検出 signal (debug 用),
        'checked_at':   ISO8601 UTC,
    }

    呼び出し側は `status in ('unavailable', 'not_found')` で reject する想定。
    'unknown' は判定保留 (現状は通過 = 既存挙動と互換、後段の AI 評価でカバー)。
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    if not url:
        return {'status': 'unknown', 'signal': 'empty url', 'checked_at': checked_at}
    if 'paypayfleamarket.yahoo.co.jp' in url:
        return _check_paypay_availability(url, timeout_sec, checked_at)
    if 'auctions.yahoo.co.jp' in url:
        return _check_yahoo_auctions_availability(url, timeout_sec, checked_at)
    # 依頼ボード (2026-06-17): Yahoo!ショッピングは site_config に in_stock シグナルが
    # 無く在庫あり品が unknown 化していた → schema.org 併用の専用判定に振り分け。
    # paypay/auctions は上で処理済 = 'shopping.yahoo.co.jp' とは重複しない。
    if 'shopping.yahoo.co.jp' in url:
        return _check_yahoo_shopping_availability(url, timeout_sec, checked_at)
    # mercari / fril / 他は既存 site_configs ベース
    return _check_via_site_configs(url, timeout_sec, checked_at)


def _check_paypay_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
    """PayPay フリマ raw HTML 判定 (W182、Codex 2026-05-28 検証ベース)。"""
    try:
        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
    except httpx.TimeoutException:
        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
    except httpx.HTTPError as e:
        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
    if resp.status_code == 404:
        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
    if resp.status_code != 200:
        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
    # シグナル判定は _detect_paypay_signals に一元化 (依頼ボード#14 2026-06-12:
    # 定時在庫監視 _check_with_httpx と共有、シグナル 2 重管理によるドリフト防止)
    status, signal = _detect_paypay_signals(resp.text)
    return {'status': status or 'unknown', 'signal': signal, 'checked_at': checked_at}


def _check_yahoo_auctions_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
    """ヤフオク (auctions.yahoo.co.jp) raw HTML 判定 (W182)。

    __NEXT_DATA__ status を最優先で使う (inventory_check の _detect_yahoo_auction_status と整合)。
    終了ページにも「入札する」テキストが残るため、テキストマッチだけでは終了済み auction を
    available と誤判定してしまう。__NEXT_DATA__ status='closed' → not_found で正しく弾く。
    __NEXT_DATA__ が取れない場合のみテキストマッチにフォールバックする。
    """
    try:
        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
    except httpx.TimeoutException:
        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
    except httpx.HTTPError as e:
        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
    if resp.status_code == 404:
        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
    if resp.status_code != 200:
        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
    html = resp.text
    # --- __NEXT_DATA__ status 最優先 (inventory_check と判定基準を統一) ---
    try:
        from monitor.yahoo_auction_status import _extract_yahoo_item
        item = _extract_yahoo_item(html)
    except Exception as e:
        logger.debug(f"yahoo __NEXT_DATA__ parse error ({url}): {e}")
        item = None
    if item:
        status_raw = item.get("status")
        if status_raw == "closed":
            return {'status': 'not_found', 'signal': '__NEXT_DATA__ status=closed', 'checked_at': checked_at}
        if status_raw == "open":
            return {'status': 'available', 'signal': '__NEXT_DATA__ status=open', 'checked_at': checked_at}
    # --- フォールバック: テキストマッチ (__NEXT_DATA__ が取れない場合) ---
    if 'このオークションは終了' in html or 'このオークションは存在しません' in html:
        return {'status': 'not_found', 'signal': 'auction ended/missing', 'checked_at': checked_at}
    if '入札する' in html or '今すぐ落札' in html:
        return {'status': 'available', 'signal': 'bid available', 'checked_at': checked_at}
    return {'status': 'unknown', 'signal': 'no signal matched', 'checked_at': checked_at}


def _detect_yahoo_shopping_signals(html: str) -> tuple[Optional[str], str]:
    """Yahoo!ショッピング (store.shopping.yahoo.co.jp) raw HTML から在庫判定。
    (status|None, signal) を返す。

    背景 (依頼ボード 2026-06-17): site_configs の Yahoo!ショッピング設定は
    sold_out_text='在庫がありません' のみで in_stock シグナルが空 → 在庫あり品が
    「どちらも不一致=unknown」で判定不能だった。schema.org availability を併用する。

    実機検証 (2026-06-17、httpx raw HTML、在庫あり15件): 在庫あり →
    `schema.org/InStock`(http/https 両形式) + 「カートに入れる」。売切判定は実績ある
    site_config の '在庫がありません' を最優先に使い (オーバーセル/Defect 防止)、
    schema.org/OutOfStock も併用する。
    """
    # 売切 (unavailable) を最優先で確定 — 誤『在庫あり』=オーバーセル防止。
    if '在庫がありません' in html:
        return 'unavailable', '在庫がありません'
    if 'schema.org/OutOfStock' in html:
        return 'unavailable', 'schema OutOfStock'
    # 在庫あり (available) — 売切シグナルが無いことを確認した後にのみ判定。
    if 'schema.org/InStock' in html:
        return 'available', 'schema InStock'
    return None, 'no signal matched'


def _check_yahoo_shopping_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
    """Yahoo!ショッピング (store.shopping.yahoo.co.jp) raw HTML 判定 (依頼ボード 2026-06-17)。"""
    try:
        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
    except httpx.TimeoutException:
        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
    except httpx.HTTPError as e:
        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
    if resp.status_code == 404:
        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
    if resp.status_code != 200:
        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
    status, signal = _detect_yahoo_shopping_signals(resp.text)
    return {'status': status or 'unknown', 'signal': signal, 'checked_at': checked_at}


def _check_via_site_configs(url: str, timeout_sec: int, checked_at: str) -> dict:
    """site_configs から URL に一致する site を引いて httpx 判定 (W182、mercari / fril / 他)。"""
    try:
        from monitor.database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT site_name, url_keyword, in_stock_text1, in_stock_text2, "
                "       sold_out_text, no_page_text FROM site_configs"
            ).fetchall()
    except Exception as e:
        return {'status': 'unknown', 'signal': f'site_configs read error: {type(e).__name__}', 'checked_at': checked_at}
    for r in rows:
        if r[1] and r[1] in url:
            in_stock = [x for x in (r[2], r[3]) if x]
            sold_out = [r[4]] if r[4] else []
            no_page = [r[5]] if r[5] else []
            status = _check_with_httpx(url, in_stock, sold_out, no_page)
            return {
                'status': status or 'unknown',
                'signal': f'site_config: {r[0]}',
                'checked_at': checked_at,
            }
    return {'status': 'unknown', 'signal': 'no matching site_config', 'checked_at': checked_at}
