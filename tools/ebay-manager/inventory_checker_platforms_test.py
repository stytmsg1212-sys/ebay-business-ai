#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
複数プラットフォーム在庫判定テスト
Yahoo Auctions, ラクマ, PayPayフリマの検出ロジックを検証
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


class PlatformsInventoryTester:
    def __init__(self):
        self.test_cases = {
            "Yahoo Auctions": {
                "在庫有": {
                    "url": "https://auctions.yahoo.co.jp/jp/auction/f1225404921",
                    "expected": "在庫有"
                },
                "在庫無": {
                    "url": "https://auctions.yahoo.co.jp/jp/auction/o1221650119",
                    "expected": "在庫無"
                }
            },
            "ラクマ": {
                "在庫有": {
                    "url": "https://item.fril.jp/f8da0517978940b30addc31183c06db6?rtg=b27b4775b0f688c980ea28ce76f7db99",
                    "expected": "在庫有"
                },
                "在庫無": {
                    "url": "https://item.fril.jp/1cc444fd6b66084eb187d9cbcfbf351c",
                    "expected": "在庫無"
                }
            },
            "PayPayフリマ": {
                "在庫有": {
                    "url": "https://paypayfleamarket.yahoo.co.jp/item/z587339852",
                    "expected": "在庫有"
                },
                "在庫無": {
                    "url": "https://paypayfleamarket.yahoo.co.jp/item/z582394556",
                    "expected": "在庫無"
                }
            }
        }
        self.results = []
        self.debug_dir = Path(__file__).parent / "debug_platforms"
        self.debug_dir.mkdir(exist_ok=True)

        # 検出ルール（inventory_checker_selenium.py から）
        self.detection_rules = {
            "Yahoo Auctions": {
                "in_stock": ["入札する", "今すぐ落札"],
                "out_of_stock": ["このオークションは終了"],
                "not_found": ["このオークションは存在しません"]
            },
            "ラクマ": {
                "in_stock": ["購入に進む"],
                "out_of_stock": ["SOLD OUT"],
                "not_found": ["ページが見つかりません"]
            },
            "PayPayフリマ": {
                "in_stock": ["購入手続きへ"],
                "out_of_stock": ["関連商品をアプリで探す"],
                "not_found": ["この商品は存在しません"]
            }
        }

    def check_inventory_detailed(self, url, platform, label, expected):
        """
        各プラットフォームのページを分析
        """
        print(f"\n{'='*80}")
        print(f"テスト: {platform} - {label}")
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

            print("⏳ ページ読み込み中（最大10秒待機）...")
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except:
                pass

            time.sleep(3)

            # 情報取得
            title = driver.title
            visible_text = driver.find_element(By.TAG_NAME, "body").text
            page_source = driver.page_source

            print(f"✓ ページタイトル: {title}")

            # 詳細分析
            print(f"\n【重要キーワードの検出】")

            rules = self.detection_rules.get(platform, {})

            print(f"\n在庫有候補:")
            for text in rules.get("in_stock", []):
                if text in visible_text or text in page_source:
                    print(f"  ✓ '{text}' 検出")
                else:
                    print(f"  ✗ '{text}' 未検出")

            print(f"\n在庫無候補:")
            for text in rules.get("out_of_stock", []):
                if text in visible_text or text in page_source:
                    print(f"  ✓ '{text}' 検出")
                else:
                    print(f"  ✗ '{text}' 未検出")

            # disabled ボタンの確認（メルカリ・PayPayフリマ対策）
            has_disabled_purchase = False
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    if platform == "メルカリ":
                        if "購入手続きへ" in btn.text and btn.get_attribute("disabled") is not None:
                            has_disabled_purchase = True
                            print(f"\n[重要] disabled 購入ボタン検出")
                            break
                    elif platform == "PayPayフリマ":
                        if btn.get_attribute("type") == "submit" and btn.get_attribute("disabled") is not None:
                            has_disabled_purchase = True
                            print(f"\n[重要] disabled submit ボタン検出")
                            break
            except:
                pass

            # 判定ロジック
            print(f"\n【現在の判定ロジック結果】")

            result = "不明"

            # メルカリ専用: disabled ボタンが優先
            if platform == "メルカリ" and has_disabled_purchase:
                result = "在庫無"
            else:
                for text in rules.get("out_of_stock", []):
                    if text in visible_text or text in page_source:
                        result = "在庫無"
                        break

            if result == "不明":
                for text in rules.get("in_stock", []):
                    if text in visible_text or text in page_source:
                        result = "在庫有"
                        break

            # PayPayフリマ特別対応：購入手続きへが見つからなければ在庫無
            if platform == "PayPayフリマ":
                if "購入手続きへ" not in visible_text and "購入手続きへ" not in page_source:
                    result = "在庫無"

            print(f"  判定結果: {result}")

            # スクリーンショット保存
            screenshot_path = self.debug_dir / f"{platform}_{label}_screenshot.png"
            driver.save_screenshot(str(screenshot_path))
            print(f"  スクリーンショット保存: {screenshot_path}")

            # テキスト保存
            text_path = self.debug_dir / f"{platform}_{label}_visible_text.txt"
            text_path.write_text(visible_text, encoding='utf-8')

            # ページソース保存
            source_path = self.debug_dir / f"{platform}_{label}_page_source.html"
            source_path.write_text(page_source, encoding='utf-8')

            # 結果記録
            test_result = {
                "platform": platform,
                "label": label,
                "url": url,
                "expected": expected,
                "result": result,
                "passed": result == expected,
                "title": title,
                "visible_text_length": len(visible_text),
            }

            self.results.append(test_result)

            print(f"\n【判定結果】")
            print(f"  期待値: {expected}")
            print(f"  現在の判定: {result}")
            print(f"  テスト結果: {'✓ PASS' if result == expected else '✗ FAIL'}")

            return test_result

        except Exception as e:
            print(f"❌ エラー: {e}")
            import traceback
            traceback.print_exc()
            return {
                "platform": platform,
                "label": label,
                "url": url,
                "error": str(e)
            }

        finally:
            if driver:
                driver.quit()
                time.sleep(1)

    def run_all_tests(self):
        """全テストを実行"""
        print(f"\n{'='*80}")
        print("複数プラットフォーム在庫判定テスト開始")
        print(f"{'='*80}")

        for platform, test_cases in self.test_cases.items():
            for label, test_info in test_cases.items():
                self.check_inventory_detailed(
                    test_info["url"],
                    platform,
                    label,
                    test_info["expected"]
                )
                time.sleep(2)

        # 結果をJSON保存
        results_file = self.debug_dir / "platforms_test_results.json"
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)

        print(f"\n\n{'='*80}")
        print("テスト完了")
        print(f"{'='*80}")

        print(f"\n【テスト結果サマリー】")
        passed = sum(1 for r in self.results if r.get("passed", False))
        total = len(self.results)
        print(f"  PASS: {passed}/{total}")

        for result in self.results:
            if not result.get("passed", True):
                print(f"\n  ✗ {result['platform']} - {result['label']}")
                print(f"    期待: {result.get('expected')}")
                print(f"    結果: {result.get('result')}")

        print(f"\n詳細は以下に保存:")
        print(f"  {self.debug_dir}")


if __name__ == "__main__":
    tester = PlatformsInventoryTester()
    tester.run_all_tests()
