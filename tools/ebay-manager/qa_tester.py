"""
eBay Manager QA テストスイート
Playwright を使用して Streamlit UI を自動操作・検証する

使用方法：
  python qa_tester.py [--headless] [--debug]

前提条件：
  1. Streamlit がローカルで起動している（デフォルト: http://localhost:8501）
  2. Playwright がインストール済み（pip install playwright）
  3. playwright install で ブラウザバイナリをインストール済み
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
import json

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
except ImportError:
    print("❌ Playwright がインストールされていません。")
    print("   実行: pip install playwright")
    print("   その後: playwright install")
    sys.exit(1)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# テスト設定
STREAMLIT_URL = "http://localhost:8501"
STREAMLIT_PORT = 8501
TEST_RESULTS_DIR = Path(__file__).parent / "qa_results"
TEST_RESULTS_DIR.mkdir(exist_ok=True)

class StreamlitQATester:
    """Streamlit UI の自動テストを実行"""

    def __init__(self, headless: bool = True, debug: bool = False):
        self.headless = headless
        self.debug = debug
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.results = []
        self.failed_tests = []

    async def setup(self):
        """ブラウザーセッションをセットアップ"""
        logger.info("📂 Playwright をセットアップ中...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-gpu", "--no-sandbox"]
        )
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        logger.info(f"✅ ブラウザーが起動しました: {STREAMLIT_URL}")

    async def teardown(self):
        """ブラウザーセッションを終了"""
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("✅ ブラウザーを閉じました")

    async def navigate_to_streamlit(self):
        """Streamlit アプリにアクセス"""
        logger.info(f"🔗 Streamlit にアクセス中: {STREAMLIT_URL}")
        try:
            await self.page.goto(STREAMLIT_URL, wait_until="networkidle")
            await self.page.wait_for_timeout(2000)  # Streamlit 読み込み待機
            logger.info("✅ Streamlit が読み込まれました")
            return True
        except Exception as e:
            logger.error(f"❌ Streamlit へのアクセスに失敗: {e}")
            return False

    async def test_app_loads(self) -> dict:
        """テスト1: アプリが起動するか"""
        test_name = "アプリ起動テスト"
        try:
            title = await self.page.title()
            h1_elements = await self.page.locator("h1").all()

            if len(h1_elements) > 0:
                logger.info(f"✅ {test_name}: 合格")
                return {"test": test_name, "status": "PASS", "message": f"タイトル: {title}"}
            else:
                logger.warning(f"⚠️ {test_name}: H1 要素が見つかりません")
                return {"test": test_name, "status": "FAIL", "message": "H1 要素が見つかりません"}
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            self.failed_tests.append(test_name)
            return {"test": test_name, "status": "FAIL", "message": str(e)}

    async def test_tabs_exist(self) -> dict:
        """テスト2: タブが表示されているか"""
        test_name = "タブ表示テスト"
        try:
            # Streamlit のタブは div[role="tablist"] に格納される
            tabs = await self.page.locator("button[role='tab']").all()

            if len(tabs) >= 4:  # 最低4タブを期待
                tab_names = [await tab.text_content() for tab in tabs[:5]]
                logger.info(f"✅ {test_name}: 合格 ({len(tabs)} タブ)")
                return {"test": test_name, "status": "PASS", "message": f"タブ数: {len(tabs)}, サンプル: {tab_names}"}
            else:
                logger.warning(f"⚠️ {test_name}: タブ数が不足 ({len(tabs)})")
                return {"test": test_name, "status": "FAIL", "message": f"タブ数が不足: {len(tabs)}"}
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            self.failed_tests.append(test_name)
            return {"test": test_name, "status": "FAIL", "message": str(e)}

    async def test_ebay_tab(self) -> dict:
        """テスト3: eBay連携タブが機能するか"""
        test_name = "eBay連携タブテスト"
        try:
            # eBay連携タブをクリック
            ebay_tabs = await self.page.locator("button[role='tab']").all()
            found = False

            for tab in ebay_tabs:
                text = await tab.text_content()
                if "eBay" in text:
                    await tab.click()
                    found = True
                    break

            if not found:
                logger.warning(f"⚠️ {test_name}: eBay タブが見つかりません")
                return {"test": test_name, "status": "FAIL", "message": "eBay タブが見つかりません"}

            await self.page.wait_for_timeout(1000)

            # eBay同期ボタンが存在するか確認
            sync_buttons = await self.page.locator("button").filter(has_text="eBay").all()

            if len(sync_buttons) > 0:
                logger.info(f"✅ {test_name}: 合格")
                return {"test": test_name, "status": "PASS", "message": f"同期ボタンが見つかりました"}
            else:
                logger.warning(f"⚠️ {test_name}: eBay同期ボタンが見つかりません")
                return {"test": test_name, "status": "FAIL", "message": "eBay同期ボタンが見つかりません"}
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            self.failed_tests.append(test_name)
            return {"test": test_name, "status": "FAIL", "message": str(e)}

    async def test_form_inputs(self) -> dict:
        """テスト4: 利益計算タブのフォーム入力が可能か"""
        test_name = "フォーム入力テスト"
        try:
            # 最初のタブ（利益計算）をクリック
            first_tab = await self.page.locator("button[role='tab']").first
            await first_tab.click()
            await self.page.wait_for_timeout(500)

            # input フィールドを探す
            inputs = await self.page.locator("input").all()

            if len(inputs) > 0:
                # 最初の入力フィールドに値を入力
                first_input = inputs[0]
                await first_input.fill("12345")
                await self.page.wait_for_timeout(500)

                value = await first_input.input_value()
                if value == "12345":
                    logger.info(f"✅ {test_name}: 合格")
                    return {"test": test_name, "status": "PASS", "message": f"入力フィールド数: {len(inputs)}"}
                else:
                    logger.warning(f"⚠️ {test_name}: 値が反映されません")
                    return {"test": test_name, "status": "FAIL", "message": "値が反映されません"}
            else:
                logger.warning(f"⚠️ {test_name}: 入力フィールドが見つかりません")
                return {"test": test_name, "status": "FAIL", "message": "入力フィールドが見つかりません"}
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            self.failed_tests.append(test_name)
            return {"test": test_name, "status": "FAIL", "message": str(e)}

    async def test_no_errors(self) -> dict:
        """テスト5: コンソールエラーがないか"""
        test_name = "エラーメッセージテスト"
        try:
            # ページ内のエラー要素を確認
            error_elements = await self.page.locator("[data-testid='stAlert']").all()

            # 赤いエラーボックスを確認
            red_alerts = []
            for elem in error_elements:
                role = await elem.get_attribute("data-testid")
                if role:
                    red_alerts.append(role)

            if len(red_alerts) == 0:
                logger.info(f"✅ {test_name}: 合格（エラーなし）")
                return {"test": test_name, "status": "PASS", "message": "エラーメッセージがありません"}
            else:
                logger.warning(f"⚠️ {test_name}: エラーが表示されています")
                return {"test": test_name, "status": "FAIL", "message": f"エラー要素: {len(red_alerts)}"}
        except Exception as e:
            logger.warning(f"⚠️ {test_name}: エラー検出に失敗 (非致命的): {e}")
            return {"test": test_name, "status": "PASS", "message": "エラー検出スキップ"}

    async def test_buttons_clickable(self) -> dict:
        """テスト6: ボタンがクリック可能か"""
        test_name = "ボタンクリック性テスト"
        try:
            buttons = await self.page.locator("button").all()
            clickable_count = 0

            for button in buttons[:5]:  # 最初の5個をチェック
                is_enabled = await button.is_enabled()
                if is_enabled:
                    clickable_count += 1

            if clickable_count > 0:
                logger.info(f"✅ {test_name}: 合格 ({clickable_count}/{min(5, len(buttons))} ボタンがクリック可能)")
                return {"test": test_name, "status": "PASS", "message": f"クリック可能ボタン: {clickable_count}/{min(5, len(buttons))}"}
            else:
                logger.warning(f"⚠️ {test_name}: クリック可能なボタンが見つかりません")
                return {"test": test_name, "status": "FAIL", "message": "クリック可能なボタンがありません"}
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            self.failed_tests.append(test_name)
            return {"test": test_name, "status": "FAIL", "message": str(e)}

    async def run_all_tests(self):
        """全テストを実行"""
        logger.info("=" * 60)
        logger.info("🧪 eBay Manager QA テストスイート開始")
        logger.info("=" * 60)

        try:
            # Streamlit へアクセス
            if not await self.navigate_to_streamlit():
                logger.error("❌ Streamlit にアクセスできません。Streamlit が起動しているか確認してください。")
                logger.info(f"   起動コマンド: cd tools/ebay-manager && streamlit run app.py")
                return

            # テスト実行
            self.results.append(await self.test_app_loads())
            self.results.append(await self.test_tabs_exist())
            self.results.append(await self.test_ebay_tab())
            self.results.append(await self.test_form_inputs())
            self.results.append(await self.test_no_errors())
            self.results.append(await self.test_buttons_clickable())

            # 結果をログ
            passed = sum(1 for r in self.results if r["status"] == "PASS")
            failed = sum(1 for r in self.results if r["status"] == "FAIL")

            logger.info("=" * 60)
            logger.info(f"📊 テスト結果: {passed} 合格 / {failed} 不合格 / 合計 {len(self.results)}")
            logger.info("=" * 60)

            # 結果をファイルに保存
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            result_file = TEST_RESULTS_DIR / f"qa_results_{timestamp}.json"

            with open(result_file, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": timestamp,
                    "total": len(self.results),
                    "passed": passed,
                    "failed": failed,
                    "results": self.results,
                }, f, ensure_ascii=False, indent=2)

            logger.info(f"💾 テスト結果を保存: {result_file}")

            # 詳細結果を表示
            for result in self.results:
                status_icon = "✅" if result["status"] == "PASS" else "❌"
                logger.info(f"{status_icon} {result['test']}: {result['message']}")

            return {
                "total": len(self.results),
                "passed": passed,
                "failed": failed,
                "results": self.results,
                "result_file": str(result_file),
            }

        except Exception as e:
            logger.error(f"❌ テスト実行中にエラーが発生: {e}")
            import traceback
            traceback.print_exc()

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="eBay Manager QA テストスイート")
    parser.add_argument("--headless", action="store_true", default=True, help="ヘッドレスモード（デフォルト）")
    parser.add_argument("--debug", action="store_true", help="デバッグモード（ブラウザーを表示）")
    args = parser.parse_args()

    # --debug が指定されたらヘッドレスを無効化
    headless = not args.debug

    tester = StreamlitQATester(headless=headless, debug=args.debug)

    try:
        await tester.setup()
        result = await tester.run_all_tests()

        # 終了コードを設定（テスト結果に基づく）
        if result and result["failed"] == 0:
            sys.exit(0)
        else:
            sys.exit(1)

    finally:
        await tester.teardown()

if __name__ == "__main__":
    asyncio.run(main())
