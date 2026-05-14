#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Selenium を使用した在庫チェッカー
eStocks と同じ手法を採用：実際のブラウザでJavaScriptを実行してからHTMLを検査

# DEPRECATED 2026-04-30 (W50 統合): tasks/task_inventory_check.run_inventory_check が
# monitor/scrapers.check_items_batch (httpx + Playwright) に統合されたため未使用.
# scheduler 経路 (cron) と Streamlit 経路 (button) は両方とも同じ統合本体を呼ぶ.
# 物理削除は W51 で 2026-05-07 以降 (1 週間 staging 後) に予定.
# それまで本ファイルは触らないこと (system_improvements.json id=143 参照).
"""

import csv
import json
import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import time

logger = logging.getLogger(__name__)

# Windows UTF-8 対応 (pythonw.exe では sys.stdout/stderr が None)
if sys.platform == 'win32':
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.webdriver import WebDriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("❌ Selenium がインストールされていません")
    print("以下を実行してください: pip install selenium webdriver-manager")
    sys.exit(1)


# グローバルドライバー管理（シングルトン）
_driver_instance: Optional[WebDriver] = None
_driver_lock = None  # スレッドセーフティ用

def get_profile_dir() -> str:
    """ブラウザプロファイルディレクトリを取得/作成"""
    profile_dir = Path(__file__).parent / "data" / ".selenium_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return str(profile_dir)

def init_driver() -> WebDriver:
    """新しいドライバーを初期化"""
    chrome_options = Options()
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    # Windowsではユーザープロファイルを指定せず、ゲストモードで動作させる
    chrome_options.add_argument("--guest")
    # chrome_options.add_argument("--headless")  # ヘッドレスモード

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e:
        print(f"❌ Chrome初期化エラー: {e}")
        raise

def get_driver() -> WebDriver:
    """グローバルドライバーを取得（シングルトン）"""
    global _driver_instance
    if _driver_instance is None:
        print("🚀 Selenium ドライバーを初期化中...")
        _driver_instance = init_driver()
    return _driver_instance

def reset_driver():
    """ドライバーをリセット（エラー時用）"""
    global _driver_instance
    try:
        if _driver_instance is not None:
            _driver_instance.quit()
    except:
        pass
    _driver_instance = None

def close_driver():
    """ドライバーをクローズ（終了時用）"""
    global _driver_instance
    try:
        if _driver_instance is not None:
            _driver_instance.quit()
            print("✅ Selenium ドライバーをクローズしました", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ ドライバークローズエラー: {e}", file=sys.stderr)
    finally:
        _driver_instance = None

def get_platform_wait_time(source: str) -> int:
    """プラットフォーム別の推奨待機時間を取得"""
    wait_times = {
        "メルカリ": 3,
        "メルカリショップ": 3,
        "PayPayフリマ": 2,
        "Yahoo Auctions": 5,  # 増加：1秒 → 5秒（動的コンテンツ対応）
        "ラクマ": 2,
        "楽天市場": 2,  # 増加：1秒 → 2秒
        "Yahoo!ショッピング": 2,  # 増加：1秒 → 2秒
        "Amazon": 2,
    }
    return wait_times.get(source, 2)

def get_platform_page_load_timeout(source: str) -> int:
    """プラットフォーム別のページロードタイムアウトを取得"""
    timeouts = {
        "Yahoo Auctions": 60,  # Yahoo Auctionsは特に遅い
        "メルカリ": 30,
        "メルカリショップ": 30,
        "PayPayフリマ": 30,
        "ラクマ": 30,
        "楽天市場": 30,
        "Yahoo!ショッピング": 30,
        "Amazon": 30,
    }
    return timeouts.get(source, 30)


class InventoryCheckerSelenium:
    def __init__(self, csv_file: str):
        self.csv_file = Path(csv_file)
        self.items = []
        self.results = []
        self.stats = {
            "total": 0,
            "in_stock": 0,
            "out_of_stock": 0,
            "page_not_found": 0,
            "error": 0,
            "by_source": {}
        }

        # 各プラットフォームの検出ルール
        # メルカリは複数パターンに対応（動的コンテンツ対策）
        self.detection_rules = {
            "メルカリ": {
                # 在庫有パターン（複数パターンに対応）
                "in_stock": [
                    "購入手続きへ",
                    "ご購入手続きへ",
                    "購入する",
                    "カートに入れる",
                    "買う"
                ],
                # 在庫無パターン（複数パターンに対応）
                # より特定的なパターンのみ使用（false positive を削減）
                "out_of_stock": [
                    "売り切れました",
                    "この商品は削除されました",
                    "SOLD"
                ],
                "not_found": ["ページが見つかりません", "該当する商品"]
            },
            "Yahoo Auctions": {
                "in_stock": ["入札", "入札する", "今すぐ落札", "もうすぐ終了"],
                "out_of_stock": ["終了しました", "このオークションは終了", "落札されました"],
                "not_found": ["このオークションは存在しません", "指定されたページは存在しません",
                              "ページが見つかりません", "404"]
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
            },
            "楽天市場": {
                "in_stock": ["かごに追加", "買い物かごに入れる", "購入手続きへ", "お気に入りに追加"],
                "out_of_stock": ["売り切れ", "在庫切れ", "品切れ", "SOLD OUT",
                                 "現在販売していません", "この商品は売り切れです"],
                "not_found": ["ページが見つかりません", "指定されたページは存在しません",
                              "ショップが見つかりません", "404"]
            },
            "Yahoo!ショッピング": {
                "in_stock": ["カートに入れる", "今すぐ買う", "お気に入りに追加"],
                "out_of_stock": ["売り切れ", "在庫切れ", "品切れ", "SOLD OUT",
                                 "現在お取り扱いしておりません"],
                "not_found": ["ページが見つかりません", "指定されたページがありません",
                              "このストアは存在しません", "404"]
            },
            "Amazon": {
                "in_stock": ["カートに入れる", "今すぐ買う"],
                "out_of_stock": ["現在在庫切れ", "在庫切れ", "入荷時期は未定"],
                "not_found": ["この商品は現在お取り扱いできません", "お探しのページは見つかりません",
                              "ページが見つかりません"]
            },
        }

    def load_csv(self) -> bool:
        """CSV ファイルを読み込む"""
        try:
            with open(self.csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                self.items = list(reader)

            logger.info(f"✅ {len(self.items)} 件のアイテムを読み込みました")
            return True

        except Exception as e:
            logger.error(f"❌ CSV 読み込みエラー: {e}", exc_info=True)
            raise RuntimeError(f"InventoryCheckerSelenium load_csv failed: {e}") from e

    def init_stats(self) -> None:
        """統計初期化"""
        self.stats["total"] = len(self.items)
        for item in self.items:
            source = item.get("source", "Unknown")
            if source not in self.stats["by_source"]:
                self.stats["by_source"][source] = {
                    "total": 0,
                    "in_stock": 0,
                    "out_of_stock": 0,
                    "page_not_found": 0,
                    "error": 0
                }
            self.stats["by_source"][source]["total"] += 1

    def check_with_retry(self, url: str, source: str, max_retries: int = 3) -> tuple:
        """
        リトライロジック付き在庫チェック
        戻り値: (status, retry_count, error_msg)
        """
        for attempt in range(max_retries):
            try:
                driver = get_driver()

                # ドライバーのヘルスチェック（簡易的な確認）
                try:
                    driver.current_url  # ドライバーが生存してるか確認
                except:
                    reset_driver()  # 死んでたらリセット
                    driver = get_driver()

                status = self.check_inventory_status(url, source, driver)
                return (status, attempt, None)
            except TimeoutException as e:
                if attempt < max_retries - 1:
                    wait_seconds = 2 ** attempt  # 指数バックオフ: 1s, 2s, 4s
                    print(f"  ⚠️ タイムアウト（試行 {attempt + 1}/{max_retries}）、{wait_seconds}秒待機...")
                    time.sleep(wait_seconds)
                else:
                    return ("エラー", attempt, f"Timeout: {str(e)[:50]}")
            except WebDriverException as e:
                # ドライバーが不安定 → リセット
                print(f"  🔄 ドライバーをリセット（試行 {attempt + 1}/{max_retries}）...")
                reset_driver()
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return ("エラー", attempt, f"WebDriver: {str(e)[:50]}")
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  ⚠️ エラー（試行 {attempt + 1}/{max_retries}）...")
                    time.sleep(1)
                else:
                    return ("エラー", attempt, f"Other: {str(e)[:50]}")

        return ("エラー", max_retries - 1, "Max retries exceeded")

    def check_inventory_status(self, url: str, source: str, driver) -> str:
        """
        Selenium でページにアクセスして在庫状態を判定
        可視テキスト（実際に表示されているテキスト）で判定する

        改善点:
        - WebDriverWait で確実なページ読み込み完了を待つ（特にメルカリ対策）
        - 複数パターンの判定キーワードに対応
        - 詳細なエラーハンドリング

        戻り値: "在庫有" / "在庫無" / "ページなし" / "不明" / "エラー"
        """
        try:
            # ページを開く（プラットフォーム別タイムアウト）
            page_timeout = get_platform_page_load_timeout(source)
            driver.set_page_load_timeout(page_timeout)
            try:
                driver.get(url)
            except TimeoutException:
                # ページ読み込みタイムアウト時も続行（JavaScriptが実行中かもしれない）
                pass
            except Exception as e:
                return "エラー"

            # ページが読み込まれるまで確実に待機（改善点）
            # プラットフォーム別に最適な待機時間を使用
            wait_timeout = min(page_timeout // 2, 30)  # ページロードタイムアウトの半分、最大30秒
            try:
                WebDriverWait(driver, wait_timeout).until(
                    EC.presence_of_element_located(("tag name", "body"))
                )
            except:
                pass  # タイムアウト時も続行

            # JavaScriptの動的コンテンツ読み込みを待つ
            # プラットフォーム別に最適な待機時間を使用
            wait_time = get_platform_wait_time(source)
            time.sleep(wait_time)

            # 可視テキスト（実際に表示されているテキスト）を取得
            try:
                visible_text = driver.find_element("tag name", "body").text
            except:
                visible_text = ""

            # ページソース（HTMLコンテンツ）も取得
            content = driver.page_source

            # メルカリ・PayPayフリマの場合：ボタン要素の詳細確認
            buttons_text = ""
            has_disabled_purchase_button = False
            if source in ["メルカリ", "PayPayフリマ"]:
                try:
                    buttons = driver.find_elements("tag name", "button")
                    buttons_text = " ".join([btn.text for btn in buttons if btn.text])

                    # 購入ボタンが disabled 状態かチェック
                    # メルカリ: "購入手続きへ" テキストを持つボタン
                    # PayPayフリマ: type="submit" で disabled なボタン
                    for btn in buttons:
                        if source == "メルカリ":
                            if "購入手続きへ" in btn.text and btn.get_attribute("disabled") is not None:
                                has_disabled_purchase_button = True
                                break
                        elif source == "PayPayフリマ":
                            if btn.get_attribute("type") == "submit" and btn.get_attribute("disabled") is not None:
                                has_disabled_purchase_button = True
                                break
                except:
                    pass

            # 検出ルールを確認
            rules = self.detection_rules.get(source, {})

            # "在庫無" チェック（最優先：可視テキストまたはボタン要素で売り切れを確認）
            # メルカリショップ対策：disabled 購入ボタンは在庫無を示す
            if source == "メルカリ" and has_disabled_purchase_button:
                return "在庫無"

            for text in rules.get("out_of_stock", []):
                if text in visible_text or text in buttons_text:
                    return "在庫無"

            # "在庫有" チェック（次優先：可視テキストで購入可能を確認）
            # ※ HTMLソースも確認して、ボタンが存在する場合は在庫有と判定
            for text in rules.get("in_stock", []):
                if text in visible_text or text in content:
                    return "在庫有"

            # PayPayフリマ特別対応：購入手続きへが見つからなければ在庫無
            # PayPayフリマは売り切れ時に明示的なテキストを表示しないため、
            # 購入手続きボタンが存在しない＝在庫無として判定
            if source == "PayPayフリマ":
                if "購入手続きへ" not in visible_text and "購入手続きへ" not in content:
                    return "在庫無"

            # "ページなし" チェック（ページが存在しない）
            for text in rules.get("not_found", []):
                if text in visible_text or text in content:
                    return "ページなし"

            # フォールバック判定: ルール未定義プラットフォームの汎用パターン
            # 一般的な「購入可能」ボタンがあれば在庫有
            generic_in_stock = ["カートに入れる", "購入手続きへ", "今すぐ買う",
                                "買い物かごに入れる", "かごに追加", "購入する", "入札する"]
            for text in generic_in_stock:
                if text in visible_text:
                    return "在庫有"

            # 一般的な「売り切れ」表示があれば在庫無
            generic_out_of_stock = ["売り切れ", "在庫切れ", "品切れ", "SOLD OUT",
                                    "sold out", "SOLD", "終了しました", "販売終了"]
            for text in generic_out_of_stock:
                if text in visible_text:
                    return "在庫無"

            # 一般的な「ページなし」表示
            generic_not_found = ["404", "ページが見つかりません", "お探しのページ",
                                 "Not Found", "Page Not Found"]
            for text in generic_not_found:
                if text in visible_text:
                    return "ページなし"

            # デフォルト: ページは存在するが状態不明
            return "不明"

        except Exception as e:
            return "エラー"

    def process_items(self) -> None:
        """全アイテムの在庫をチェック（プラットフォーム別グループ化 + リトライロジック）"""
        import logging
        _logger = logging.getLogger(__name__)
        _logger.info("=" * 70)
        _logger.info("在庫チェック開始 (Selenium) - グローバルドライバー + リトライロジック")
        _logger.info("=" * 70)

        # プラットフォーム別にグループ化
        by_source = {}
        for item in self.items:
            source = item.get("source", "Unknown")
            if source not in by_source:
                by_source[source] = []
            by_source[source].append(item)

        # グローバルドライバーを初期化（1回だけ）
        # Q0 ルール準拠: 失敗時 silent skip ではなく例外 raise (上位 task が failed status で記録)
        # 旧 print + return 経路は pythonw.exe で stdout=None のため完全 silent
        # → 348件 0結果で偽装成功を返す事故 (2026-04-29 発覚)
        try:
            driver = get_driver()
            _logger.info("ドライバー初期化完了")
        except Exception as e:
            _logger.error(
                f"ドライバー初期化失敗 (silent skip 防止のため例外 raise): {e!r}",
                exc_info=True,
            )
            raise RuntimeError(
                f"InventoryCheckerSelenium driver init failed: {e}"
            ) from e

        total_processed = 0
        start_time = datetime.now()

        try:
            # 各プラットフォーム内で順序処理
            for source in sorted(by_source.keys()):
                source_items = by_source[source]
                print(f"\n📍 {source} ({len(source_items)}件)")

                for idx, item in enumerate(source_items, 1):
                    # メモリ・接続リーク防止：50件ごとにドライバーをリセット
                    if idx > 1 and idx % 50 == 1:
                        try:
                            reset_driver()
                            time.sleep(2)
                            driver = get_driver()
                        except Exception as e:
                            logger.warning(f"driver reset 失敗 (継続): {e}", exc_info=True)
                    ebay_id = item.get("ebay_id")
                    url = item.get("source_url")

                    try:
                        # リトライロジック付き在庫チェック
                        status, retry_count, error_msg = self.check_with_retry(url, source)

                        # 結果を記録
                        result = {
                            "ebay_id": ebay_id,
                            "sku": item.get("sku"),
                            "source": source,
                            "url": url,
                            "status": status,
                            "retry_count": retry_count,
                            "error": error_msg,
                            "checked_at": datetime.now().isoformat()
                        }
                        self.results.append(result)

                        # 統計更新
                        if status == "在庫有":
                            self.stats["in_stock"] += 1
                            if source in self.stats["by_source"]:
                                self.stats["by_source"][source]["in_stock"] += 1
                        elif status == "在庫無":
                            self.stats["out_of_stock"] += 1
                            if source in self.stats["by_source"]:
                                self.stats["by_source"][source]["out_of_stock"] += 1
                        elif status == "ページなし":
                            self.stats["page_not_found"] += 1
                            if source in self.stats["by_source"]:
                                self.stats["by_source"][source]["page_not_found"] += 1
                        else:
                            self.stats["error"] += 1
                            if source in self.stats["by_source"]:
                                self.stats["by_source"][source]["error"] += 1

                        total_processed += 1

                        # 進捗表示（10件ごと）
                        if idx % 10 == 0 or idx == len(source_items):
                            elapsed = (datetime.now() - start_time).total_seconds()
                            print(f"  進捗: {idx}/{len(source_items)} - {status}" +
                                  (f" (試行 {retry_count + 1})" if retry_count > 0 else ""))

                        # レート制限対策：アクセス間隔を設定
                        if idx < len(source_items):
                            time.sleep(0.5)

                    except Exception as e:
                        self.results.append({
                            "ebay_id": ebay_id,
                            "sku": item.get("sku"),
                            "source": source,
                            "url": url,
                            "status": "エラー",
                            "error": str(e)[:100],
                            "checked_at": datetime.now().isoformat()
                        })
                        self.stats["error"] += 1

            # ── 2nd pass: 「不明」アイテムの再チェック ──
            unknown_indices = [
                i for i, r in enumerate(self.results) if r.get("status") == "不明"
            ]
            if unknown_indices:
                print(f"\n🔄 不明アイテム再チェック ({len(unknown_indices)}件)...")
                # ドライバーをリセットしてクリーンな状態で再試行
                reset_driver()
                time.sleep(2)
                driver = get_driver()

                resolved = 0
                for idx in unknown_indices:
                    item_result = self.results[idx]
                    url = item_result.get("url", "")
                    source = item_result.get("source", "")
                    if not url:
                        continue

                    # 長めの待機時間で再チェック
                    original_wait = get_platform_wait_time(source)
                    try:
                        # 一時的に待機時間を増やすため、直接チェック
                        driver.set_page_load_timeout(60)
                        try:
                            driver.get(url)
                        except TimeoutException:
                            pass
                        except Exception:
                            continue

                        # 長めに待機（元の2倍 + 2秒）
                        time.sleep(original_wait * 2 + 2)

                        try:
                            visible_text = driver.find_element("tag name", "body").text
                        except:
                            visible_text = ""
                        content = driver.page_source

                        # 検出ルールで再判定
                        rules = self.detection_rules.get(source, {})
                        new_status = None

                        for text in rules.get("out_of_stock", []):
                            if text in visible_text:
                                new_status = "在庫無"
                                break
                        if not new_status:
                            for text in rules.get("in_stock", []):
                                if text in visible_text or text in content:
                                    new_status = "在庫有"
                                    break
                        if not new_status:
                            for text in rules.get("not_found", []):
                                if text in visible_text or text in content:
                                    new_status = "ページなし"
                                    break

                        # 汎用フォールバック
                        if not new_status:
                            for text in ["カートに入れる", "購入手続きへ", "今すぐ買う",
                                         "かごに追加", "購入する", "入札する"]:
                                if text in visible_text:
                                    new_status = "在庫有"
                                    break
                        if not new_status:
                            for text in ["売り切れ", "在庫切れ", "品切れ", "SOLD OUT",
                                         "SOLD", "終了しました", "販売終了"]:
                                if text in visible_text:
                                    new_status = "在庫無"
                                    break

                        if new_status:
                            old = self.results[idx]["status"]
                            self.results[idx]["status"] = new_status
                            self.results[idx]["retry_count"] += 1
                            resolved += 1
                            # 統計を修正
                            self.stats["error"] -= 1
                            src = self.results[idx].get("source", "")
                            if new_status == "在庫有":
                                self.stats["in_stock"] += 1
                                if src in self.stats["by_source"]:
                                    self.stats["by_source"][src]["in_stock"] += 1
                                    self.stats["by_source"][src]["error"] -= 1
                            elif new_status == "在庫無":
                                self.stats["out_of_stock"] += 1
                                if src in self.stats["by_source"]:
                                    self.stats["by_source"][src]["out_of_stock"] += 1
                                    self.stats["by_source"][src]["error"] -= 1
                            elif new_status == "ページなし":
                                self.stats["page_not_found"] += 1
                                if src in self.stats["by_source"]:
                                    self.stats["by_source"][src]["page_not_found"] += 1
                                    self.stats["by_source"][src]["error"] -= 1

                        time.sleep(1)

                    except Exception as e:
                        continue

                print(f"  → {resolved}/{len(unknown_indices)}件を解決")

        except Exception as e:
            # Q0 ルール準拠: silent skip 禁止 (2026-04-30 0件偽装成功事故修正)
            # 旧 print は pythonw.exe / -WindowStyle Hidden 起動で stdout なし → 完全 silent
            # → 348件 0結果 success:True 偽装成功 (4/29 02:45 / 11:07 で発生)
            _logger.error(
                f"process_items 処理中にエラー発生 (silent skip 防止のため例外 raise): {e!r}",
                exc_info=True,
            )
            raise
        finally:
            # グローバルドライバーは保持（再利用できるように）
            elapsed = (datetime.now() - start_time).total_seconds()
            print(f"\n処理完了: {total_processed}件 ({elapsed:.1f}秒)")

    def print_stats(self) -> None:
        """統計情報を表示"""
        print("\n" + "=" * 70)
        print("🎉 在庫チェック完了")
        print("=" * 70)

        total = self.stats["total"]
        in_stock = self.stats["in_stock"]
        out_of_stock = self.stats["out_of_stock"]
        page_not_found = self.stats["page_not_found"]
        error_count = self.stats["error"]

        # 不明件数をカウント（resultsから直接集計）
        unknown_count = sum(1 for r in self.results if r.get("status") == "不明")
        pure_error = error_count - unknown_count  # エラーから不明を除外

        success_count = in_stock + out_of_stock + page_not_found
        success_rate = (success_count * 100 // total) if total > 0 else 0

        print(f"\n📊 総合結果 ({total}件):")
        print(f"  ✅ 在庫有: {in_stock}件 ({in_stock*100//total if total > 0 else 0}%)")
        print(f"  ❌ 在庫無: {out_of_stock}件 ({out_of_stock*100//total if total > 0 else 0}%)")
        print(f"  🚫 ページなし: {page_not_found}件 ({page_not_found*100//total if total > 0 else 0}%)")
        if unknown_count > 0:
            print(f"  ❓ 不明: {unknown_count}件 ({unknown_count*100//total}%)")
        if pure_error > 0:
            print(f"  ⚠️  エラー: {pure_error}件 ({pure_error*100//total}%)")
        print(f"\n  📈 判定成功率: {success_rate}%")

        print(f"\n📍 仕入先別処理結果:")
        for source in sorted(self.stats["by_source"].keys()):
            source_stats = self.stats["by_source"][source]
            src_total = source_stats["total"]
            src_in = source_stats["in_stock"]
            src_out = source_stats["out_of_stock"]
            src_error = source_stats["error"]
            src_success = src_in + src_out + source_stats["page_not_found"]
            src_rate = (src_success * 100 // src_total) if src_total > 0 else 0
            print(f"  {source}: {src_in}有/{src_out}無 ({src_success}/{src_total}成功, エラー:{src_error}, 成功率:{src_rate}%)")

    def save_results(self, output_file: str) -> None:
        """結果を JSON で保存"""
        try:
            output_path = Path(output_file)
            data = {
                "checked_at": datetime.now().isoformat(),
                "total_items": len(self.results),
                "stats": {
                    "in_stock": self.stats["in_stock"],
                    "out_of_stock": self.stats["out_of_stock"],
                    "page_not_found": self.stats["page_not_found"],
                    "error": self.stats["error"],
                },
                "by_source": self.stats["by_source"],
                "results": self.results
            }

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            print(f"✅ 結果を保存: {output_path}")

        except Exception as e:
            print(f"❌ 保存エラー: {e}")

    def save_csv(self, output_file: str) -> None:
        """結果を CSV で保存"""
        try:
            output_path = Path(output_file)

            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'ebay_id', 'sku', 'source', 'status'
                ])
                writer.writeheader()

                for result in self.results:
                    writer.writerow({
                        'ebay_id': result['ebay_id'],
                        'sku': result['sku'],
                        'source': result['source'],
                        'status': result['status']
                    })

            print(f"✅ CSV保存: {output_path}")

        except Exception as e:
            print(f"❌ CSV保存エラー: {e}")

    def run(self) -> None:
        """実行"""
        try:
            if not self.load_csv():
                return

            self.init_stats()
            print("Processing items...", flush=True)
            self.process_items()
            print("Printing stats...", flush=True)
            self.print_stats()

            # 結果を保存
            output_json = self.csv_file.parent / "inventory_check_results.json"
            output_csv = self.csv_file.parent / "inventory_check_results.csv"

            print(f"\n💾 結果を保存中...\n")
            sys.stdout.flush()
            self.save_results(str(output_json))
            sys.stdout.flush()
            self.save_csv(str(output_csv))
            sys.stdout.flush()

            print("\n✨ 完了！")
            sys.stdout.flush()

        except Exception as e:
            print(f"\n❌ 実行エラー: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            raise


def main():
    csv_file = Path(__file__).parent / "data" / "sourced_items_for_playwright.csv"

    if not csv_file.exists():
        print(f"❌ CSV ファイルが見つかりません: {csv_file}")
        print("先に sku_conversion.py を実行してください")
        sys.exit(1)

    try:
        checker = InventoryCheckerSelenium(str(csv_file))
        checker.run()
    finally:
        print("クローズ処理中...")
        sys.stdout.flush()
        close_driver()
        sys.stderr.flush()


if __name__ == "__main__":
    main()
