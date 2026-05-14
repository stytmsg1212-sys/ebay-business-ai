"""W7-A PoC: Terapeak Research Products スクレイピング検証スクリプト.

目的:
  1 SKU (Audio-Technica ATH-CKS330NC) で eBay Seller Hub の Research products
  を Playwright で自動アクセス、Buyer Location 別 Sold データを取得できるか検証.

実行手順:
  1. python scripts/terapeak_poc.py
  2. 起動した Chromium で eBay Seller Hub にログイン (初回のみ)
  3. ターミナルで Enter 押下 → 自動でフィルタ適用とスクレイピング
  4. data/terapeak_poc_<sku>.json に結果出力

ログイン情報は data/.terapeak_browser_profile/ に永続化される. 2 回目以降は自動ログイン.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

if sys.platform == "win32":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. pip install playwright && playwright install chromium")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / "data" / ".terapeak_browser_profile"
OUTPUT_DIR = PROJECT_ROOT / "data"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def build_url(keyword: str, day_range: int = 90) -> str:
    """Terapeak Research Products URL を組み立てる.

    Filter params (一部):
      - dayRange: 7 / 30 / 90 (Terapeak 標準). 全期間は dayRange=All
      - tabName: SOLD / ACTIVE
      - sellerCountry: Japan
      - marketplace: EBAY-US
    """
    base = "https://www.ebay.com/sh/research/products"
    params = {
        "q": keyword,
        "dayRange": day_range,
        "tabName": "SOLD",
        "sellerCountry": "Japan",
        "marketplace": "EBAY-US",
    }
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"{base}?{qs}"


def extract_buyer_locations(page) -> dict:
    """Buyer Location セクションから US / その他 件数を抽出.

    DOM は eBay の運用で変わるので、本 PoC では HTML スナップショットも保存して
    後で解析できるようにする.
    """
    result = {
        "us_count": None,
        "non_us_count": None,
        "raw_html_path": None,
        "extraction_method": None,
    }

    # 日本語/英語の両方の表記に対応
    # Buyer Location セクション内の数値を取得
    selectors_to_try = [
        # Buyer Location ラベル直近の (数字) 表示を捕まえる
        "//div[contains(text(), 'Buyer Location') or contains(text(), 'バイヤーロケーション')]/following::div[contains(text(), 'United States')][1]",
        "//label[contains(., 'United States')]",
        "[class*='buyer'] [class*='location']",
    ]

    try:
        # シンプル: ページ全体の text content から正規表現抽出
        body_text = page.evaluate("() => document.body.innerText")
        result["body_text_sample"] = body_text[:500]

        # "us United States (14)" パターン
        import re
        us_match = re.search(r'United\s*States[\s\xa0]*\((\d+)\)', body_text)
        non_us_match = re.search(r'その他の国[\s\xa0]*\((\d+)\)', body_text)
        if not non_us_match:
            non_us_match = re.search(r'Other\s*Countries?[\s\xa0]*\((\d+)\)', body_text)

        if us_match:
            result["us_count"] = int(us_match.group(1))
            result["extraction_method"] = "regex_body_text"
        if non_us_match:
            result["non_us_count"] = int(non_us_match.group(1))

        # 詳細メトリクス: Avg sold price, total sold 等
        avg_price_match = re.search(r'\$([\d,.]+)\s*Avg\s*sold\s*price', body_text)
        if avg_price_match:
            result["avg_sold_price"] = float(avg_price_match.group(1).replace(",", ""))

        total_sold_match = re.search(r'(\d+)\s*Total\s*sold', body_text, re.IGNORECASE)
        if total_sold_match:
            result["total_sold"] = int(total_sold_match.group(1))

        avg_ship_match = re.search(r'\$([\d,.]+)\s*Avg\s*shipping', body_text)
        if avg_ship_match:
            result["avg_shipping_usd"] = float(avg_ship_match.group(1).replace(",", ""))

    except Exception as e:
        logger.warning(f"text 抽出失敗: {e}")
        result["error"] = str(e)

    return result


def run_poc(sku: str, keyword: str, day_range: int = 90):
    """PoC 本体.

    Args:
        sku: 自社 SKU 識別子 (出力ファイル名用)
        keyword: Terapeak 検索 keyword
        day_range: 90 (Last 90 days), 30, 7. 全期間は別実行.
    """
    url = build_url(keyword, day_range)
    logger.info(f"target URL: {url}")

    output = {
        "poc_run_at": datetime.now().isoformat(),
        "sku": sku,
        "keyword": keyword,
        "url": url,
        "day_range": day_range,
        "result": None,
        "error": None,
    }

    with sync_playwright() as p:
        # 永続化プロファイル → 2 回目以降のログイン省略
        # Stealth 対策:
        #   - channel='chrome' で bundled Chromium でなく実 Chrome を使用
        #   - args で AutomationControlled 検知を無効化
        #   - user_agent で通常ブラウザ偽装
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",  # 実 Chrome 使用 (bundled Chromium だと Google 検知)
            headless=False,
            viewport={"width": 1400, "height": 900},
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        # navigator.webdriver を undefined に偽装
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en']});
            """
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # Step 1: eBay ホームに遷移してログイン要求
            logger.info("→ eBay.com ホームに navigate (ログイン確認)")
            page.goto("https://www.ebay.com/", wait_until="domcontentloaded", timeout=60000)

            print("\n" + "=" * 60)
            print("Step 1/2: eBay ログイン")
            print("=" * 60)
            print("Chromium が起動しました.")
            print()
            print("画面で eBay Seller アカウントにログインしてください.")
            print("(2 回目以降は自動ログインされるはずです — その場合は次へ進んで OK)")
            print()
            print("ログインが完了したら、ターミナルで Enter を押してください.")
            input("Enter で Terapeak へ移動 > ")

            # Step 2: ログイン状態のまま Terapeak へ
            logger.info(f"→ Terapeak Research Products へ navigate")
            logger.info(f"   URL: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)  # 動的データ読込待ち

            print("\n" + "=" * 60)
            print(f"Step 2/2: Terapeak Research Products")
            print("=" * 60)
            print(f"検索: {keyword}")
            print(f"期間: Last {day_range} days")
            print()
            print("画面でフィルタを確認してください:")
            print("  - Seller Country: Japan が選択されている")
            print("  - Sold タブが選択されている")
            print("  - Buyer Location 表示が見える (US 件数 / その他件数)")
            print()
            print("もしフィルタが反映されていなければ手動で設定してください.")
            print("結果が表示されたら、ターミナルで Enter キーを押してください.")
            print("=" * 60)
            input("Enter で抽出開始 > ")

            # 結果抽出
            logger.info("→ Buyer Location データ抽出")
            data = extract_buyer_locations(page)
            output["result"] = data

            # HTML スナップショット保存 (後で DOM 構造調査用)
            html_path = OUTPUT_DIR / f"terapeak_poc_{sku.replace(':', '_')}_{day_range}d.html"
            html_path.write_text(page.content(), encoding="utf-8")
            output["html_snapshot"] = str(html_path)
            logger.info(f"HTML 保存: {html_path}")

            # スクリーンショット保存
            png_path = OUTPUT_DIR / f"terapeak_poc_{sku.replace(':', '_')}_{day_range}d.png"
            page.screenshot(path=str(png_path), full_page=True)
            output["screenshot"] = str(png_path)
            logger.info(f"screenshot 保存: {png_path}")

        except PWTimeout as e:
            logger.error(f"Playwright timeout: {e}")
            output["error"] = f"timeout: {e}"
        except Exception as e:
            logger.error(f"PoC 失敗: {e}")
            output["error"] = str(e)
        finally:
            print("\nブラウザは開いたままです. 確認後、Chromium ウィンドウを閉じてください.")
            input("Enter で context close > ")
            context.close()

    # JSON 出力
    json_path = OUTPUT_DIR / f"terapeak_poc_{sku.replace(':', '_')}_{day_range}d.json"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"JSON 出力: {json_path}")

    print("\n" + "=" * 60)
    print(f"PoC 結果サマリ (SKU: {sku} / {day_range} days)")
    print("=" * 60)
    if output["result"]:
        r = output["result"]
        if r.get("us_count") is not None or r.get("non_us_count") is not None:
            print(f"  US: {r.get('us_count')} 件")
            print(f"  その他: {r.get('non_us_count')} 件")
            if r.get("us_count") and r.get("non_us_count") is not None:
                total = (r["us_count"] or 0) + (r["non_us_count"] or 0)
                if total > 0:
                    pct = (r["us_count"] or 0) / total * 100
                    print(f"  → US 比率: {pct:.0f}%")
            if r.get("total_sold"):
                print(f"  Total sold: {r['total_sold']}")
            if r.get("avg_sold_price"):
                print(f"  Avg sold price: ${r['avg_sold_price']:.2f}")
            if r.get("avg_shipping_usd"):
                print(f"  Avg shipping: ${r['avg_shipping_usd']:.2f}")
        else:
            print("  抽出失敗 — HTML スナップショットを確認してください")
            print(f"  body sample: {r.get('body_text_sample', '')[:200]}")
    if output["error"]:
        print(f"  ERROR: {output['error']}")
    print("=" * 60)

    return output


if __name__ == "__main__":
    # PoC 用 SKU 設定
    POC_SKU = "stock:01"
    POC_KEYWORD = "Audio-Technica ATH-CKS330NC"

    # まず Last 90 days で実行
    print(f"\nW7-A PoC: Terapeak スクレイピング検証")
    print(f"SKU: {POC_SKU} / keyword: {POC_KEYWORD}")
    print()

    run_poc(POC_SKU, POC_KEYWORD, day_range=90)
