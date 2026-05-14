#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SKU から仕入先URLを生成し、在庫状態を確認するツール
仕入先プラットフォームからSKU形式で仕入れた商品に対応
"""

import sqlite3
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Windowsでも正常にUTF-8出力できるよう設定 (pythonw.exe では sys.stdout/stderr が None)
if sys.platform == 'win32':
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# sku_mapping_manager からマッピングを動的に読み込む
from sku_mapping_manager import load_mappings

# NOTE: SOURCE_MAPPINGS はプログラム実行時に load_mappings() で読み込まれます
# これにより、UI で設定を変更した場合、自動的に反映されます
SOURCE_MAPPINGS = None  # 遅延初期化

class SKUConverter:
    def __init__(self):
        global SOURCE_MAPPINGS
        # マッピングを動的に読み込む
        SOURCE_MAPPINGS = load_mappings()

        self.db_path = Path(__file__).parent / "data" / "monitor.db"
        if not self.db_path.exists():
            print(f"❌ データベースが見つかりません: {self.db_path}")
            sys.exit(1)

        self.results = {
            "total_items": 0,
            "self_stock": [],
            "sourced": [],
            "unknown": [],
            "summary": {
                "total": 0,
                "self_stock_count": 0,
                "sourced_count": 0,
                "unknown_count": 0,
                "mapped_sources": {}
            }
        }

    def read_ebay_listings(self) -> List[Dict]:
        """eBayデータベースから全ての出品を読み込む"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # eBay出品情報を取得（SKU, ItemID）
            cursor.execute("""
                SELECT ebay_item_id, sku, title, current_price
                FROM ebay_listings
                ORDER BY ebay_item_id
            """)

            items = [dict(row) for row in cursor.fetchall()]
            conn.close()

            logger.info(f"✅ {len(items)}件のeBay出品を読み込みました")
            return items

        except Exception as e:
            logger.error(f"❌ データベース読み込みエラー: {e}", exc_info=True)
            raise RuntimeError(f"SKUConverter DB read failed: {e}") from e

    def classify_sku(self, sku: str) -> Tuple[str, Optional[str], Optional[str]]:
        """
        SKUを分類して、仕入先とアイテムIDを抽出
        戻り値: (分類, 仕入先, アイテムID)
        """
        if not sku:
            return ("unknown", None, None)

        # 自有在庫チェック（"stock"を含む）
        if "stock" in sku.lower():
            return ("self_stock", None, None)

        # 仕入先別のプレフィックスをチェック
        for prefix, config in SOURCE_MAPPINGS.items():
            if sku.startswith(prefix):
                # プレフィックスを除いた部分がアイテムID
                item_id = sku[len(prefix):]
                return ("sourced", config["name"], item_id)

        # どのプレフィックスにも一致しない
        return ("unknown", None, None)

    def generate_source_url(self, source_name: str, item_id: str) -> Optional[str]:
        """仕入先URLを生成"""
        for prefix, config in SOURCE_MAPPINGS.items():
            if config["name"] == source_name:
                # URLパターンを組み立て
                pattern = config["pattern"]
                url_part = pattern.format(item_id=item_id)
                full_url = config["common_url"] + url_part
                return full_url
        return None

    def process_listings(self, items: List[Dict]) -> None:
        """全ての出品を処理"""
        print("\n" + "=" * 70)
        print("SKU→仕入先URL 変換開始")
        print("=" * 70 + "\n")

        self.results["total_items"] = len(items)

        for idx, item in enumerate(items, 1):
            item_id = item.get("ebay_item_id")
            sku = item.get("sku", "")
            title = item.get("title", "")

            classification, source_name, source_item_id = self.classify_sku(sku)

            result_entry = {
                "ebay_id": item_id,
                "sku": sku,
                "title": title,
                "classification": classification,
                "source": source_name,
                "item_id": source_item_id,
                "source_url": None
            }

            if classification == "self_stock":
                self.results["self_stock"].append(result_entry)
                self.results["summary"]["self_stock_count"] += 1

            elif classification == "sourced" and source_name:
                source_url = self.generate_source_url(source_name, source_item_id)
                result_entry["source_url"] = source_url
                self.results["sourced"].append(result_entry)
                self.results["summary"]["sourced_count"] += 1

                # 仕入先別カウント
                if source_name not in self.results["summary"]["mapped_sources"]:
                    self.results["summary"]["mapped_sources"][source_name] = 0
                self.results["summary"]["mapped_sources"][source_name] += 1

            else:
                self.results["unknown"].append(result_entry)
                self.results["summary"]["unknown_count"] += 1

            # 進捗表示
            if idx % 50 == 0 or idx == len(items):
                print(f"処理中... {idx}/{len(items)}")

        self.results["summary"]["total"] = len(items)

    def print_summary(self) -> None:
        """結果サマリーを表示"""
        summary = self.results["summary"]

        print("\n" + "=" * 70)
        print("🎉 処理完了 - SKU分類結果")
        print("=" * 70)
        print(f"""
Total Items: {summary['total']}件

📊 分類結果:
  - 自有在庫（"stock"を含む）: {summary['self_stock_count']}件
  - 仕入先商品（プレフィックスマッチ）: {summary['sourced_count']}件
  - 未分類/不明: {summary['unknown_count']}件

📍 仕入先別（仕入先商品のみ）:
""")
        for source, count in sorted(summary['mapped_sources'].items(), key=lambda x: x[1], reverse=True):
            print(f"  - {source}: {count}件")

        print("\n" + "=" * 70)

    def save_json(self, output_file: str) -> None:
        """結果をJSONファイルに保存"""
        try:
            output_path = Path(output_file)

            # 前処理: サマリーを最後に追加
            self.results["summary"]["exported_at"] = datetime.now().isoformat()

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)

            print(f"✅ JSON保存完了: {output_path}")

        except Exception as e:
            print(f"❌ JSON保存エラー: {e}")

    def save_csv_for_playwright(self, output_file: str) -> None:
        """Playwrite用のCSV（仕入先商品のみ）を生成"""
        try:
            import csv
            output_path = Path(output_file)

            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'ebay_id', 'sku', 'source', 'item_id', 'source_url'
                ])
                writer.writeheader()

                for item in self.results["sourced"]:
                    writer.writerow({
                        'ebay_id': item['ebay_id'],
                        'sku': item['sku'],
                        'source': item['source'],
                        'item_id': item['item_id'],
                        'source_url': item['source_url']
                    })

            print(f"✅ CSV保存完了: {output_path} ({len(self.results['sourced'])}件)")

        except Exception as e:
            print(f"❌ CSV保存エラー: {e}")

    def run(self) -> None:
        """実行"""
        print("🔄 eBay出品データの読み込み中...\n")
        items = self.read_ebay_listings()

        if not items:
            print("❌ 出品データを読み込めません")
            return

        self.process_listings(items)
        self.print_summary()

        # 出力ファイルを保存
        output_json = Path(__file__).parent / "data" / "sku_conversion_results.json"
        output_csv = Path(__file__).parent / "data" / "sourced_items_for_playwright.csv"

        print(f"\n💾 結果を保存中...\n")
        self.save_json(str(output_json))
        self.save_csv_for_playwright(str(output_csv))

        print("\n✨ 全ての処理が完了しました！")
        print(f"\n📋 次のステップ:")
        print(f"  1. Playwright でソース在庫をチェック")
        print(f"  2. 在庫なし商品をeBayで出品停止")
        print(f"  3. ダッシュボードに結果を反映")


if __name__ == "__main__":
    converter = SKUConverter()
    converter.run()
