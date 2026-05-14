#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP リクエストを使用した軽量在庫チェッカー
Playwrightの接続問題を回避し、シンプルなテキストマッチングで検出
"""

import requests
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

# リクエストのタイムアウト設定
REQUEST_TIMEOUT = 10
RETRY_ATTEMPTS = 2
RETRY_DELAY = 1


class InventoryCheckerHTTP:
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
        self.detection_rules = {
            "メルカリ": {
                "in_stock": ["購入手続きへ"],
                "out_of_stock": ["売り切れました"],
                "not_found": ["ページが見つかりません"]
            },
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
            },
            "楽天市場": {
                "in_stock": ["かごに追加", "カートに入れる"],
                "out_of_stock": [],
                "not_found": []
            },
            "Yahoo!ショッピング": {
                "in_stock": ["カートに入れる"],
                "out_of_stock": [],
                "not_found": []
            },
            "Amazon": {
                "in_stock": ["カートに入れる", "今すぐ買う"],
                "out_of_stock": ["現在在庫切れ"],
                "not_found": ["この商品は現在お取り扱いできません"]
            },
        }

        # ユーザーエージェント
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

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
            raise RuntimeError(f"InventoryCheckerHTTP load_csv failed: {e}") from e

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

    def check_inventory_status(self, url: str, source: str) -> str:
        """
        HTTP リクエストで在庫状態を判定
        戻り値: "在庫有" / "在庫無" / "ページなし" / "不明" / "エラー"
        """
        try:
            # HTTP リクエストを送信
            response = requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True
            )

            # ステータスコード確認
            if response.status_code == 404:
                return "ページなし"

            content = response.text

            # 検出ルールを確認
            rules = self.detection_rules.get(source, {})

            # "ページなし" チェック（最優先）
            for text in rules.get("not_found", []):
                if text in content:
                    return "ページなし"

            # "在庫無" チェック
            for text in rules.get("out_of_stock", []):
                if text in content:
                    return "在庫無"

            # "在庫有" チェック
            for text in rules.get("in_stock", []):
                if text in content:
                    return "在庫有"

            # デフォルト: ページは存在するが状態不明
            return "不明"

        except requests.Timeout:
            return "タイムアウト"
        except requests.ConnectionError:
            return "接続エラー"
        except Exception as e:
            return "エラー"

    def process_items(self) -> None:
        """全アイテムの在庫をチェック"""
        print("\n" + "=" * 70)
        print("在庫チェック開始 (HTTP)")
        print("=" * 70 + "\n")

        for idx, item in enumerate(self.items, 1):
            ebay_id = item.get("ebay_id")
            url = item.get("source_url")
            source = item.get("source")

            try:
                # 在庫チェック
                status = self.check_inventory_status(url, source)

                # 結果を記録
                result = {
                    "ebay_id": ebay_id,
                    "sku": item.get("sku"),
                    "source": source,
                    "url": url,
                    "status": status,
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

                # 進捗表示
                if idx % 50 == 0 or idx == len(self.items):
                    print(f"進捗: {idx}/{len(self.items)} - {status}")

                # リクエスト間の遅延（サーバー負荷軽減）
                time.sleep(0.2)

            except Exception as e:
                self.results.append({
                    "ebay_id": ebay_id,
                    "sku": item.get("sku"),
                    "source": source,
                    "url": url,
                    "status": "エラー",
                    "error": str(e),
                    "checked_at": datetime.now().isoformat()
                })
                self.stats["error"] += 1

    def print_stats(self) -> None:
        """統計情報を表示"""
        print("\n" + "=" * 70)
        print("🎉 在庫チェック完了")
        print("=" * 70)

        total = self.stats["total"]
        in_stock = self.stats["in_stock"]
        out_of_stock = self.stats["out_of_stock"]
        page_not_found = self.stats["page_not_found"]

        print(f"\n📊 総合結果 ({total}件):")
        print(f"  ✅ 在庫有: {in_stock}件 ({in_stock*100//total if total > 0 else 0}%)")
        print(f"  ❌ 在庫無: {out_of_stock}件 ({out_of_stock*100//total if total > 0 else 0}%)")
        print(f"  🚫 ページなし: {page_not_found}件 ({page_not_found*100//total if total > 0 else 0}%)")
        print(f"  ⚠️  その他エラー: {self.stats['error']}件")

        print(f"\n📍 仕入先別:")
        for source in sorted(self.stats["by_source"].keys()):
            source_stats = self.stats["by_source"][source]
            src_total = source_stats["total"]
            src_in = source_stats["in_stock"]
            pct = src_in*100//src_total if src_total > 0 else 0
            print(f"  {source}: {src_in}/{src_total}件在庫有 ({pct}%)")

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
        if not self.load_csv():
            return

        self.init_stats()
        self.process_items()
        self.print_stats()

        # 結果を保存
        output_json = self.csv_file.parent / "inventory_check_results.json"
        output_csv = self.csv_file.parent / "inventory_check_results.csv"

        print(f"\n💾 結果を保存中...\n")
        self.save_results(str(output_json))
        self.save_csv(str(output_csv))

        print("\n✨ 完了！")


def main():
    csv_file = Path(__file__).parent / "data" / "sourced_items_for_playwright.csv"

    if not csv_file.exists():
        print(f"❌ CSV ファイルが見つかりません: {csv_file}")
        print("先に sku_conversion.py を実行してください")
        sys.exit(1)

    checker = InventoryCheckerHTTP(str(csv_file))
    checker.run()


if __name__ == "__main__":
    main()
