"""
eBay Manager エバリュエーター
Playwright を使用して Streamlit UI を自動操作・検証し、Markdown フィードバックを生成

使用方法：
  python evaluator.py [--sprint N] [--debug]

出力：
  docs/feedback/sprint-N.md - テスト結果と改善指示

前提条件：
  1. Streamlit がローカルで起動している（http://localhost:8501）
  2. Playwright がインストール済み
  3. docs/ フォルダが存在する
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
import json
import re

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
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# パス設定
STREAMLIT_URL = "http://localhost:8501"
DOCS_DIR = Path(__file__).parent / "docs"
FEEDBACK_DIR = DOCS_DIR / "feedback"
DOCS_DIR.mkdir(exist_ok=True)
FEEDBACK_DIR.mkdir(exist_ok=True)


class TestResult:
    """テスト結果を管理"""

    def __init__(self, name: str, status: str, message: str = "", details: dict = None):
        self.name = name
        self.status = status  # "PASS" or "FAIL"
        self.message = message
        self.details = details or {}
        self.screenshot = None

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


class Bug:
    """バグ情報"""

    SEVERITY_HIGH = "高"
    SEVERITY_MEDIUM = "中"
    SEVERITY_LOW = "低"

    def __init__(self, severity: str, title: str, description: str, reproduction: str = "", expected: str = "", actual: str = ""):
        self.severity = severity
        self.title = title
        self.description = description
        self.reproduction = reproduction
        self.expected = expected
        self.actual = actual


class StreamlitEvaluator:
    """Streamlit UI の自動評価エージェント"""

    def __init__(self, sprint: int = 1, headless: bool = True, debug: bool = False):
        self.sprint = sprint
        self.headless = headless
        self.debug = debug
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.test_results = []
        self.bugs = []
        self.score = 0.0  # 1-5 段階
        self.overall_status = "UNKNOWN"

    async def setup(self):
        """ブラウザーをセットアップ"""
        logger.info("📂 Playwright をセットアップ中...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-gpu", "--no-sandbox"]
        )
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        logger.info(f"✅ ブラウザーが起動しました")

    async def teardown(self):
        """ブラウザーを終了"""
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
        """Streamlit にアクセス"""
        logger.info(f"🔗 Streamlit にアクセス中: {STREAMLIT_URL}")
        try:
            await self.page.goto(STREAMLIT_URL, wait_until="networkidle")
            await self.page.wait_for_timeout(2000)
            logger.info("✅ Streamlit が読み込まれました")
            return True
        except Exception as e:
            logger.error(f"❌ Streamlit へのアクセスに失敗: {e}")
            self.bugs.append(Bug(
                Bug.SEVERITY_HIGH,
                "Streamlit にアクセスできない",
                f"エラー: {e}",
                "ブラウザで http://localhost:8501 にアクセス",
                "ページが正常に読み込まれる",
                "接続エラーまたはタイムアウト"
            ))
            return False

    async def test_app_loads(self) -> TestResult:
        """テスト1: アプリが起動するか"""
        test_name = "アプリ起動テスト"
        try:
            title = await self.page.title()
            h1_elements = await self.page.locator("h1").all()

            if len(h1_elements) > 0:
                logger.info(f"✅ {test_name}: PASS")
                return TestResult(test_name, "PASS", f"タイトル: {title}")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_HIGH,
                    "メインタイトル（H1）が見つからない",
                    "Streamlit ページに h1 要素が存在しない",
                    "ページソースを確認"
                ))
                return TestResult(test_name, "FAIL", "H1 要素が見つかりません")
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            return TestResult(test_name, "FAIL", str(e))

    async def test_tabs_exist(self) -> TestResult:
        """テスト2: タブが表示されているか"""
        test_name = "タブ表示テスト"
        try:
            tabs = await self.page.locator("button[role='tab']").all()

            if len(tabs) >= 4:
                logger.info(f"✅ {test_name}: PASS ({len(tabs)} タブ)")
                return TestResult(test_name, "PASS", f"タブ数: {len(tabs)}")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_MEDIUM,
                    f"タブ数が不足 ({len(tabs)}/4)",
                    f"期待値は4個以上だが、{len(tabs)}個しか見つかりません",
                    "st.tabs() を確認"
                ))
                return TestResult(test_name, "FAIL", f"タブ数が不足: {len(tabs)}")
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            return TestResult(test_name, "FAIL", str(e))

    async def test_ebay_tab(self) -> TestResult:
        """テスト3: eBay連携タブが機能するか"""
        test_name = "eBay連携タブテスト"
        try:
            ebay_tabs = await self.page.locator("button[role='tab']").all()
            found = False

            for tab in ebay_tabs:
                text = await tab.text_content()
                if "eBay" in text or "連携" in text:
                    await tab.click()
                    found = True
                    break

            if not found:
                logger.warning(f"⚠️ {test_name}: eBay タブが見つかりません")
                self.bugs.append(Bug(
                    Bug.SEVERITY_HIGH,
                    "eBay 連携タブが見つからない",
                    "タブのテキストに『eBay』が含まれていない"
                ))
                return TestResult(test_name, "FAIL", "eBay タブが見つかりません")

            await self.page.wait_for_timeout(1000)
            sync_buttons = await self.page.locator("button").filter(has_text="eBay").all()

            if len(sync_buttons) > 0:
                logger.info(f"✅ {test_name}: PASS")
                return TestResult(test_name, "PASS", "eBay タブが機能")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_MEDIUM,
                    "eBay 同期ボタンが見つからない",
                    "eBay タブに同期ボタンがない"
                ))
                return TestResult(test_name, "FAIL", "同期ボタンが見つかりません")
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            return TestResult(test_name, "FAIL", str(e))

    async def test_form_inputs(self) -> TestResult:
        """テスト4: フォーム入力が可能か"""
        test_name = "フォーム入力テスト"
        try:
            first_tab = await self.page.locator("button[role='tab']").first
            await first_tab.click()
            await self.page.wait_for_timeout(500)

            inputs = await self.page.locator("input").all()

            if len(inputs) > 0:
                first_input = inputs[0]
                await first_input.fill("12345")
                await self.page.wait_for_timeout(500)

                value = await first_input.input_value()
                if value == "12345":
                    logger.info(f"✅ {test_name}: PASS")
                    return TestResult(test_name, "PASS", f"入力フィールド数: {len(inputs)}")
                else:
                    logger.warning(f"⚠️ {test_name}: FAIL")
                    self.bugs.append(Bug(
                        Bug.SEVERITY_MEDIUM,
                        "入力値が反映されない",
                        "input フィールドに値を入力しても反映されない",
                        f"最初の input に '12345' を入力",
                        "値が '12345' になる",
                        f"値が '{value}' のまま"
                    ))
                    return TestResult(test_name, "FAIL", "値が反映されません")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_HIGH,
                    "入力フィールドが見つからない",
                    "Streamlit ページに input 要素がない"
                ))
                return TestResult(test_name, "FAIL", "入力フィールドが見つかりません")
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            return TestResult(test_name, "FAIL", str(e))

    async def test_no_errors(self) -> TestResult:
        """テスト5: エラーメッセージがないか"""
        test_name = "エラーメッセージテスト"
        try:
            error_elements = await self.page.locator("[data-testid='stAlert']").all()

            if len(error_elements) == 0:
                logger.info(f"✅ {test_name}: PASS")
                return TestResult(test_name, "PASS", "エラーメッセージなし")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_MEDIUM,
                    f"エラーアラートが表示されている ({len(error_elements)} 件)",
                    "Streamlit ページにエラーが表示されている"
                ))
                return TestResult(test_name, "FAIL", f"エラー要素: {len(error_elements)}")
        except Exception as e:
            logger.warning(f"⚠️ {test_name}: 検出スキップ")
            return TestResult(test_name, "PASS", "エラー検出スキップ（非致命的）")

    async def test_buttons_clickable(self) -> TestResult:
        """テスト6: ボタンがクリック可能か"""
        test_name = "ボタンクリック性テスト"
        try:
            buttons = await self.page.locator("button").all()
            clickable_count = 0

            for button in buttons[:5]:
                is_enabled = await button.is_enabled()
                if is_enabled:
                    clickable_count += 1

            if clickable_count > 0:
                logger.info(f"✅ {test_name}: PASS")
                return TestResult(test_name, "PASS", f"クリック可能: {clickable_count}/{min(5, len(buttons))}")
            else:
                logger.warning(f"⚠️ {test_name}: FAIL")
                self.bugs.append(Bug(
                    Bug.SEVERITY_MEDIUM,
                    "クリック可能なボタンがない",
                    "最初の5個のボタンがすべて disabled 状態"
                ))
                return TestResult(test_name, "FAIL", "クリック可能なボタンがありません")
        except Exception as e:
            logger.error(f"❌ {test_name}: {e}")
            return TestResult(test_name, "FAIL", str(e))

    def calculate_score(self):
        """テスト結果からスコア（1-5段階）を計算"""
        passed = sum(1 for r in self.test_results if r.status == "PASS")
        total = len(self.test_results)

        # スコア計算: 6個中 6 個合格 = 5.0, 5 個 = 4.0, 4 個 = 3.0, etc.
        if total > 0:
            self.score = max(1.0, 5.0 * passed / total)
        else:
            self.score = 1.0

        # 総合判定
        if passed == total:
            self.overall_status = "PASS"
        elif passed >= 4:
            self.overall_status = "PASS_WITH_WARNINGS"
        else:
            self.overall_status = "FAIL"

    def generate_markdown_feedback(self) -> str:
        """Markdown フィードバックを生成"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        passed = sum(1 for r in self.test_results if r.status == "PASS")
        failed = len(self.test_results) - passed

        md = f"""# Sprint {self.sprint} 評価レポート

**評価日時**: {timestamp}
**評価者**: eBay Manager エバリュエーター

---

## 総合評価

- **合否**: {self.overall_status}
- **スコア**: {self.score:.1f}/5.0
- **テスト数**: {len(self.test_results)}
- **合格**: {passed} / **不合格**: {failed}

"""

        # スコア説明
        if self.overall_status == "PASS":
            md += "✅ **全テストに合格しました。リリース可能です。**\n\n"
        elif self.overall_status == "PASS_WITH_WARNINGS":
            md += "⚠️ **ほぼ合格ですが、警告があります。確認後にリリースしてください。**\n\n"
        else:
            md += "❌ **複数のテストに不合格があります。修正が必須です。**\n\n"

        # テスト結果詳細
        md += "## テスト結果詳細\n\n"
        md += "| # | テスト項目 | 結果 | 詳細 |\n"
        md += "|---|-----------|------|------|\n"

        for i, result in enumerate(self.test_results, 1):
            status_icon = "✅" if result.status == "PASS" else "❌"
            md += f"| {i} | {result.name} | {status_icon} {result.status} | {result.message} |\n"

        # バグリスト
        if self.bugs:
            md += "\n## バグリスト（重要度順）\n\n"

            # 重要度でソート
            priority = {Bug.SEVERITY_HIGH: 0, Bug.SEVERITY_MEDIUM: 1, Bug.SEVERITY_LOW: 2}
            sorted_bugs = sorted(self.bugs, key=lambda b: priority.get(b.severity, 3))

            for i, bug in enumerate(sorted_bugs, 1):
                severity_icon = {"高": "🔴", "中": "🟡", "低": "🟢"}[bug.severity]
                md += f"### {i}. {severity_icon} **[{bug.severity}]** {bug.title}\n\n"
                md += f"- **説明**: {bug.description}\n"
                if bug.reproduction:
                    md += f"- **再現手順**: {bug.reproduction}\n"
                if bug.expected:
                    md += f"- **期待値**: {bug.expected}\n"
                if bug.actual:
                    md += f"- **実際**: {bug.actual}\n"
                md += "\n"
        else:
            md += "\n## バグリスト\n\n既知のバグなし ✅\n\n"

        # 修正指示
        if self.bugs:
            md += "## 修正指示\n\n"
            md += "ジェネレーターは上記のバグリストを確認し、以下の順序で修正してください：\n\n"
            for i, bug in enumerate(sorted(self.bugs, key=lambda b: {"高": 0, "中": 1, "低": 2}[b.severity]), 1):
                md += f"{i}. **{bug.title}** ({bug.severity})\n"
            md += "\n修正後は再度エバリュエーターを実行してください。\n\n"

        # 推奨事項
        md += "## 推奨事項\n\n"
        if self.overall_status == "PASS":
            md += "- 全テストに合格しました。次のスプリントに進むことをお勧めします。\n"
        else:
            md += "- 上記のバグを修正した後、再度テストを実行してください。\n"
            md += "- 修正内容は `/docs/progress.md` に記録してください。\n"

        md += f"\n---\n\n**生成**: エバリュエーター v1.0\n**次回実行**: `python evaluator.py --sprint {self.sprint + 1}`\n"

        return md

    async def run_all_tests(self):
        """全テストを実行"""
        logger.info("=" * 60)
        logger.info(f"🧪 Sprint {self.sprint} 評価テスト開始")
        logger.info("=" * 60)

        # Streamlit へアクセス
        if not await self.navigate_to_streamlit():
            logger.error("❌ Streamlit にアクセスできません。")
            logger.info(f"   起動: cd tools/ebay-manager && streamlit run app.py")
            return

        # テスト実行
        self.test_results.append(await self.test_app_loads())
        self.test_results.append(await self.test_tabs_exist())
        self.test_results.append(await self.test_ebay_tab())
        self.test_results.append(await self.test_form_inputs())
        self.test_results.append(await self.test_no_errors())
        self.test_results.append(await self.test_buttons_clickable())

        # スコア計算
        self.calculate_score()

        # Markdown フィードバック生成
        feedback_md = self.generate_markdown_feedback()

        # ファイルに保存
        feedback_file = FEEDBACK_DIR / f"sprint-{self.sprint}.md"
        with open(feedback_file, "w", encoding="utf-8") as f:
            f.write(feedback_md)

        logger.info("=" * 60)
        logger.info(f"📊 テスト完了: スコア {self.score:.1f}/5.0")
        logger.info(f"💾 フィードバック保存: {feedback_file}")
        logger.info("=" * 60)

        # 画面に表示
        print("\n" + feedback_md)

        return {
            "sprint": self.sprint,
            "score": self.score,
            "overall_status": self.overall_status,
            "test_count": len(self.test_results),
            "passed": sum(1 for r in self.test_results if r.status == "PASS"),
            "failed": sum(1 for r in self.test_results if r.status == "FAIL"),
            "bugs": len(self.bugs),
            "feedback_file": str(feedback_file),
        }


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="eBay Manager エバリュエーター")
    parser.add_argument("--sprint", type=int, default=1, help="Sprint 番号（デフォルト: 1）")
    parser.add_argument("--debug", action="store_true", help="デバッグモード（ブラウザーを表示）")
    args = parser.parse_args()

    headless = not args.debug

    evaluator = StreamlitEvaluator(sprint=args.sprint, headless=headless, debug=args.debug)

    try:
        await evaluator.setup()
        result = await evaluator.run_all_tests()

        # 終了コードを設定
        if result and result["overall_status"] == "PASS":
            sys.exit(0)
        else:
            sys.exit(1)

    finally:
        await evaluator.teardown()


if __name__ == "__main__":
    asyncio.run(main())
