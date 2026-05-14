"""W7-A PoC (案 B: CDP attach 方式).

user が手動で起動した Chrome (--remote-debugging-port=9222) に Playwright が
CDP 経由で接続. 完全に「人間操作の Chrome」として Akamai を回避する.

実行手順:
  1. 全 Chrome を閉じる (タスクマネージャーで chrome.exe を全終了 推奨)
  2. 専用 Chrome 起動 (バッチ or 直接コマンド):
     "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" \\
       --remote-debugging-port=9222 \\
       --user-data-dir="C:/Users/gucch/projects/claude/tools/ebay-manager/data/.chrome_cdp_profile"
  3. その Chrome で eBay にログイン
  4. Terapeak Research Products ページに navigate
       https://www.ebay.com/sh/research/products?q=Audio-Technica+ATH-CKS330NC&dayRange=90&tabName=SOLD&sellerCountry=Japan
  5. filter 確認 (Seller Country=Japan / Sold tab / Last 90 days)
  6. このスクリプトを別ターミナルで実行:
       python scripts/terapeak_poc_cdp.py
  7. スクリプトが CDP attach → 現在のページからデータ抽出

ヒント:
  - 起動用バッチを scripts/start_chrome_cdp.bat に作成しておくと便利
  - ログイン Cookie は .chrome_cdp_profile に永続化される (2 回目以降ログイン省略)
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. pip install playwright")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data"
CDP_ENDPOINT = "http://localhost:9222"


def extract_buyer_locations(page) -> dict:
    """Buyer Location セクションから US / その他 件数を抽出."""
    result = {
        "us_count": None,
        "non_us_count": None,
        "extraction_method": "regex_body_text",
    }

    try:
        body_text = page.evaluate("() => document.body.innerText")
        full_html = page.content()
        result["body_text_sample"] = body_text[:500]

        # eBay Terapeak の Buyer Location filter から国別 sold 件数を抽出.
        # HTML 構造:
        #   <div data="BuyerLocation:::US" ...>
        #     ...
        #     <span class="filter-menu__text">United States (23)</span>
        #   </div>
        # `.*?` で間の HTML タグを吸収. re.DOTALL は付けない (改行を跨ぐと
        # 別国の data 属性まで吸収してしまうリスク).
        country_pattern = re.compile(
            r'data="BuyerLocation:::(\w+)".*?<span class="filter-menu__text">([^<]+?)\s*\((\d+)\)</span>'
        )
        countries = []
        for code, name, n_str in country_pattern.findall(full_html):
            n = int(n_str)
            if n > 0:
                countries.append({
                    "code": code,
                    "name": name.strip(),
                    "count": n,
                })

        if countries:
            total = sum(c["count"] for c in countries)
            us_count = sum(c["count"] for c in countries if c["code"] == "US")
            result["us_count"] = us_count
            result["non_us_count"] = total - us_count
            result["total_buyer_location"] = total
            result["countries_breakdown"] = countries
            result["us_ratio"] = us_count / total if total else 0
            result["extraction_method"] = "country_filter_aggregation"
            # 戦略判定 (3 区分)
            if total < 5:
                result["primary_market"] = "unknown"
                result["primary_market_reason"] = f"sample {total} < 5"
            elif us_count / total >= 0.70:
                result["primary_market"] = "US_only"
                result["primary_market_reason"] = f"US {us_count}/{total} = {us_count/total*100:.0f}% >= 70%"
            else:
                result["primary_market"] = "mixed_global"
                result["primary_market_reason"] = f"US {us_count}/{total} = {us_count/total*100:.0f}% < 70%"

        # 補助メトリクス
        avg_price_match = re.search(r'\$([\d,.]+)\s*(?:Avg|平均)\s*sold\s*price', body_text)
        if avg_price_match:
            result["avg_sold_price_usd"] = float(avg_price_match.group(1).replace(",", ""))

        total_sold_match = re.search(r'(\d+)\s*Total\s*sold', body_text, re.IGNORECASE)
        if total_sold_match:
            result["total_sold"] = int(total_sold_match.group(1))

        avg_ship_match = re.search(r'\$([\d,.]+)\s*Avg\s*shipping', body_text)
        if avg_ship_match:
            result["avg_shipping_usd"] = float(avg_ship_match.group(1).replace(",", ""))

        # Sell-through
        st_match = re.search(r'([\d.]+)%\s*Sell-through', body_text)
        if st_match:
            result["sell_through_pct"] = float(st_match.group(1))

        # Total sellers
        ts_match = re.search(r'(\d+)\s*Total\s*sellers', body_text, re.IGNORECASE)
        if ts_match:
            result["total_sellers"] = int(ts_match.group(1))

    except Exception as e:
        logger.warning(f"text 抽出失敗: {e}")
        result["error"] = str(e)

    return result


def run_poc_cdp(sku: str, expected_keyword: str):
    """既存 Chrome (CDP) に接続してデータ抽出."""
    output = {
        "poc_run_at": datetime.now().isoformat(),
        "sku": sku,
        "expected_keyword": expected_keyword,
        "method": "CDP attach",
        "result": None,
        "error": None,
    }

    print("\n" + "=" * 60)
    print(f"W7-A PoC (CDP attach 方式)")
    print("=" * 60)
    print(f"SKU: {sku}")
    print(f"想定 keyword: {expected_keyword}")
    print()
    print("事前準備チェック:")
    print(f"  1. Chrome を --remote-debugging-port=9222 で起動済み?")
    print(f"  2. eBay にログイン済み?")
    print(f"  3. Terapeak Research Products ページを開いて filter 適用済み?")
    print(f"     → q={expected_keyword} / Seller=Japan / Sold tab / Last 90 days")
    print()

    with sync_playwright() as p:
        try:
            logger.info(f"→ CDP 接続: {CDP_ENDPOINT}")
            browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)

            if not browser.contexts:
                output["error"] = "CDP に接続したが context がない. Chrome 起動状態を確認."
                logger.error(output["error"])
                return output

            context = browser.contexts[0]
            pages = context.pages
            if not pages:
                output["error"] = "open page が見つからない. Chrome で何か page を開いてください."
                logger.error(output["error"])
                return output

            # 一番アクティブな (= Terapeak と思しき) page を選択
            target_page = None
            for pg in pages:
                u = pg.url
                logger.info(f"   open page: {u[:120]}")
                if "ebay.com/sh/research" in u:
                    target_page = pg
                    break

            if target_page is None:
                # Terapeak ページが見つからなければ最初の page
                target_page = pages[0]
                logger.warning(f"Terapeak ページが見つからず. 1 番目の page を使用: {target_page.url}")
            else:
                logger.info(f"→ Terapeak ページ検出: {target_page.url}")

            output["page_url"] = target_page.url

            # データ抽出
            logger.info("→ Buyer Location データ抽出")
            target_page.bring_to_front()
            target_page.wait_for_timeout(1000)  # 念のため
            data = extract_buyer_locations(target_page)
            output["result"] = data

            # スナップショット
            slug = sku.replace(":", "_")
            html_path = OUTPUT_DIR / f"terapeak_poc_cdp_{slug}.html"
            html_path.write_text(target_page.content(), encoding="utf-8")
            output["html_snapshot"] = str(html_path)
            logger.info(f"HTML 保存: {html_path}")

            png_path = OUTPUT_DIR / f"terapeak_poc_cdp_{slug}.png"
            target_page.screenshot(path=str(png_path), full_page=True)
            output["screenshot"] = str(png_path)
            logger.info(f"screenshot 保存: {png_path}")

            # CDP は close しない (user の Chrome を閉じない)
            browser.close()  # Playwright 側 disconnect のみ

        except Exception as e:
            logger.error(f"PoC 失敗: {e}")
            output["error"] = str(e)
            print()
            print("ERROR ヒント:")
            print("  - 'Connection refused': Chrome が --remote-debugging-port=9222 で起動していない")
            print("  - 'Address already in use': 既に別プロセスがポート 9222 使用中")

    # JSON 出力
    slug = sku.replace(":", "_")
    json_path = OUTPUT_DIR / f"terapeak_poc_cdp_{slug}.json"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"JSON 出力: {json_path}")

    # サマリ表示
    print("\n" + "=" * 60)
    print(f"PoC 結果サマリ (SKU: {sku})")
    print("=" * 60)
    if output["result"]:
        r = output["result"]
        print(f"  page URL: {output.get('page_url', '?')[:80]}...")
        print()
        if r.get("countries_breakdown"):
            print(f"  国別 sold (上位 10):")
            for c in sorted(r["countries_breakdown"], key=lambda x: -x["count"])[:10]:
                print(f"    {c['code']:3s} {c['name']:30s} {c['count']:3d}")
            print()
        print(f"  US: {r.get('us_count')} 件")
        print(f"  非US: {r.get('non_us_count')} 件")
        print(f"  合計: {r.get('total_buyer_location')} 件")
        if r.get("us_ratio") is not None:
            print(f"  US 比率: {r['us_ratio']*100:.0f}%")
        if r.get("primary_market"):
            print(f"  → primary_market: {r['primary_market']}")
            print(f"     ({r.get('primary_market_reason')})")
        print()
        print(f"  補助メトリクス:")
        for key in ("total_sold", "avg_sold_price_usd", "avg_shipping_usd",
                    "sell_through_pct", "total_sellers"):
            if r.get(key) is not None:
                print(f"    {key}: {r[key]}")
        if r.get("us_count") is None:
            print()
            print("  ⚠️ Buyer Location 抽出失敗 — body text の冒頭:")
            print(f"  {r.get('body_text_sample', '')[:300]}")
    if output["error"]:
        print(f"  ERROR: {output['error']}")
    print("=" * 60)

    return output


if __name__ == "__main__":
    POC_SKU = "stock:01"
    POC_KEYWORD = "Audio-Technica ATH-CKS330NC"
    run_poc_cdp(POC_SKU, POC_KEYWORD)
