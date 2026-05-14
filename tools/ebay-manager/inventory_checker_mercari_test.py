#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
メルカリ在庫判定の詳細テスト＆改善版
実際のページで判定ロジックをテストします
"""

import sys
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
from datetime import datetime
from pathlib import Path
import json

class MercariInventoryTester:
    def __init__(self):
        self.test_cases = {
            "メルカリ_在庫無": {
                "url": "https://jp.mercari.com/item/m81786287162",
                "expected": "在庫無"
            },
            "メルカリ_在庫有": {
                "url": "https://jp.mercari.com/item/m18924610012",
                "expected": "在庫有"
            },
            "メルカリショップ_在庫有": {
                "url": "https://jp.mercari.com/shops/product/jcPji3FggWfLFAYNJXpMnd",
                "expected": "在庫有"
            },
            "メルカリショップ_在庫無": {
                "url": "https://jp.mercari.com/shops/product/2JPfUTUh76Jm6z9LGP4p2Q",
                "expected": "在庫無"
            }
        }
        self.results = []
        self.debug_dir = Path(__file__).parent / "debug_mercari"
        self.debug_dir.mkdir(exist_ok=True)

    def check_inventory_detailed(self, url, label):
        """
        メルカリページの詳細分析
        """
        print(f"\n{'='*80}")
        print(f"テスト: {label}")
        print(f"URL: {url}")
        print('='*80)

        chrome_options = Options()
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

        driver = None
        try:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)

            driver.set_page_load_timeout(15)
            driver.get(url)

            # ページが読み込まれるまで待つ（改善点1: WebDriverWait 使用）
            print("\n⏳ ページ読み込み中（最大10秒待機）...")
            try:
                # body が存在するまで待つ
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except:
                pass

            # さらに動的コンテンツの読み込みを待つ
            time.sleep(3)

            # 【情報取得】
            title = driver.title
            visible_text = driver.find_element(By.TAG_NAME, "body").text
            page_source = driver.page_source

            print(f"\n✓ ページタイトル: {title}")

            # 【詳細分析】
            print(f"\n【重要キーワードの検出】")

            keywords_analysis = {
                "在庫有候補": ["購入手続きへ", "購入する", "ご購入手続きへ", "カートに入れる", "買う"],
                "在庫無候補": ["売り切れました", "SOLD", "削除されました", "削除", "ご購入手続きへ進む"],
                "ページ構造": ["メルカリ", "商品詳細", "価格", "配送"]
            }

            detected_keywords = {}
            for category, keywords in keywords_analysis.items():
                print(f"\n{category}:")
                detected = []
                for kw in keywords:
                    if kw in visible_text:
                        detected.append(kw)
                        print(f"  ✓ '{kw}' 検出")
                if not detected:
                    print(f"  - 検出なし")
                detected_keywords[category] = detected

            # 【HTML 要素の確認】
            print(f"\n【HTML 要素構造】")

            # ボタン要素を探す
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                print(f"  ボタン要素数: {len(buttons)}")
                for idx, btn in enumerate(buttons[:5]):
                    text = btn.text[:50] if btn.text else "[テキストなし]"
                    print(f"    Button {idx+1}: {text}")
            except Exception as e:
                print(f"  ボタン検索エラー: {e}")

            # リンク要素を探す
            try:
                links = driver.find_elements(By.TAG_NAME, "a")
                purchase_links = [l for l in links if "購入" in l.text or "買う" in l.text]
                if purchase_links:
                    print(f"  購入関連リンク: {len(purchase_links)}個")
                    for link in purchase_links[:3]:
                        print(f"    - {link.text[:50]}")
            except:
                pass

            # disabled 購入ボタンの確認
            has_disabled_purchase = False
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    if "購入手続きへ" in btn.text and btn.get_attribute("disabled") is not None:
                        has_disabled_purchase = True
                        print(f"  [重要] disabled 購入ボタン検出")
                        break
            except:
                pass

            # 【現在の判定ロジックの結果】
            print(f"\n【現在の判定ロジック結果】")

            # Latest logic: check both visible_text and page_source
            out_of_stock_patterns = [
                "売り切れました",
                "この商品は削除されました",
                "SOLD"
            ]
            in_stock_patterns = [
                "購入手続きへ",
                "ご購入手続きへ",
                "購入する",
                "カートに入れる",
                "買う"
            ]

            old_result = "不明"

            # メルカリショップ対策：disabled 購入ボタンは在庫無
            if has_disabled_purchase:
                old_result = "在庫無"
            else:
                # out_of_stock: visible_text のみで判定（false positive 削減）
                for text in out_of_stock_patterns:
                    if text in visible_text:
                        old_result = "在庫無"
                        break

                # in_stock: visible_text または page_source で判定
                if old_result == "不明":
                    for text in in_stock_patterns:
                        if text in visible_text or text in page_source:
                            old_result = "在庫有"
                            break

            print(f"  判定結果: {old_result}")

            # 【スクリーンショット保存】
            screenshot_path = self.debug_dir / f"{label}_screenshot.png"
            driver.save_screenshot(str(screenshot_path))
            print(f"  スクリーンショット保存: {screenshot_path}")

            # 【テキスト保存】
            text_path = self.debug_dir / f"{label}_visible_text.txt"
            text_path.write_text(visible_text, encoding='utf-8')
            print(f"  可視テキスト保存: {text_path}")

            # 【ページソース保存】
            source_path = self.debug_dir / f"{label}_page_source.html"
            source_path.write_text(page_source, encoding='utf-8')
            print(f"  ページソース保存: {source_path}")

            # 結果を記録
            result = {
                "label": label,
                "url": url,
                "expected": self.test_cases[label]["expected"],
                "old_logic_result": old_result,
                "title": title,
                "detected_keywords": detected_keywords,
                "visible_text_length": len(visible_text),
                "test_passed": old_result == self.test_cases[label]["expected"]
            }

            self.results.append(result)

            print(f"\n【判定結果】")
            print(f"  期待値: {result['expected']}")
            print(f"  現在の判定: {old_result}")
            print(f"  テスト結果: {'✓ PASS' if result['test_passed'] else '✗ FAIL'}")

            return result

        except Exception as e:
            print(f"❌ エラー: {e}")
            import traceback
            traceback.print_exc()
            return {
                "label": label,
                "error": str(e)
            }

        finally:
            if driver:
                driver.quit()
                time.sleep(1)

    def run_all_tests(self):
        """全テストを実行"""
        print(f"\n{'='*80}")
        print("メルカリ在庫判定テスト開始")
        print(f"{'='*80}")

        for label, test_case in self.test_cases.items():
            self.check_inventory_detailed(test_case["url"], label)
            time.sleep(2)  # リクエスト間隔

        # 結果をJSON保存
        results_file = self.debug_dir / "test_results.json"
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)

        print(f"\n\n{'='*80}")
        print("テスト完了")
        print(f"{'='*80}")

        print(f"\n【テスト結果サマリー】")
        passed = sum(1 for r in self.results if r.get("test_passed", False))
        total = len(self.results)
        print(f"  PASS: {passed}/{total}")

        for result in self.results:
            if not result.get("test_passed", True):
                print(f"\n  ✗ {result['label']}")
                print(f"    期待: {result.get('expected')}")
                print(f"    結果: {result.get('old_logic_result')}")
                print(f"    検出キーワード: {result.get('detected_keywords', {})}")

        print(f"\n詳細は以下に保存:")
        print(f"  {self.debug_dir}")

if __name__ == "__main__":
    tester = MercariInventoryTester()
    tester.run_all_tests()
