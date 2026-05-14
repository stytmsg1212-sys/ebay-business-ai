#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 動画学習パイプライン - 秘書用ラッパースクリプト

使い方:
  python run_learning.py "https://www.youtube.com/watch?v=xxxxx"

秘書が「YouTube URL から学習して」と指示されたら、このスクリプトを呼び出す
"""

import sys
import subprocess
import os
import io
from pathlib import Path

# Windows上でUTF-8出力を有効化
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 環境変数を .env から読み込み
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def run_learning(url):
    """YouTube URL から学習内容を抽出"""

    # スクリプトが存在するディレクトリを確認
    script_dir = Path(__file__).parent
    youtube_processor = script_dir / "youtube_processor_v2.py"

    if not youtube_processor.exists():
        print(f"❌ エラー: youtube_processor_v2.py が見つかりません")
        print(f"   期待される場所: {youtube_processor}")
        return False

    # YouTube プロセッサーを実行
    print(f"\n🚀 YouTube 学習パイプラインを開始します...")
    print(f"   URL: {url}\n")

    try:
        result = subprocess.run(
            [sys.executable, str(youtube_processor), url],
            cwd=str(script_dir),
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ エラーが発生しました: {e}")
        return False
    except Exception as e:
        print(f"❌ 予期しないエラー: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使用方法:")
        print(f"  python run_learning.py 'https://www.youtube.com/watch?v=xxxxx'")
        print("")
        print("例:")
        print(f"  python run_learning.py 'https://www.youtube.com/watch?v=3e4NWeKS2To'")
        sys.exit(1)

    url = sys.argv[1]
    success = run_learning(url)
    sys.exit(0 if success else 1)
