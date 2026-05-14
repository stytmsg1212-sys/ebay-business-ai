#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 動画学習パイプライン
eBay コンサル動画などから知識を自動抽出
"""

import os
import json
import re
import sys
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path

# Windows PowerShell での UTF-8 対応
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    import yt_dlp
    YDLP_AVAILABLE = True
except ImportError:
    YDLP_AVAILABLE = False
    print("⚠️  yt_dlp not installed. Install with: pip install yt-dlp")

try:
    from anthropic import Anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False
    print("⚠️  anthropic SDK not installed. Install with: pip install anthropic")


class YouTubeProcessor:
    """YouTube 動画から学習内容を抽出するプロセッサ"""

    def __init__(self):
        self.client = Anthropic() if CLAUDE_AVAILABLE else None
        # 正しいパスを絶対パスで指定
        self.output_dir = Path(__file__).parent.parent.parent / "research" / "learning"
        self.metadata_file = self.output_dir / "video_metadata.json"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def extract_video_id(self, url: str) -> str:
        """YouTube URL からビデオIDを抽出"""
        patterns = [
            r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&\n?#]+)',
            r'youtube\.com\/embed\/([^&\n?#]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        raise ValueError(f"Invalid YouTube URL: {url}")

    def get_video_info(self, url: str) -> Dict:
        """YouTube からビデオ情報を取得"""
        if not YDLP_AVAILABLE:
            print("❌ yt_dlp が必要です: pip install yt-dlp")
            return self._get_basic_info_from_url(url)

        try:
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                return {
                    'video_id': info.get('id'),
                    'title': info.get('title'),
                    'description': info.get('description'),
                    'channel': info.get('uploader'),
                    'duration': info.get('duration'),
                    'upload_date': info.get('upload_date'),
                    'subtitles': info.get('subtitles', {}),
                }
        except Exception as e:
            print(f"⚠️  動画情報取得失敗: {e}")
            return self._get_basic_info_from_url(url)

    def _get_basic_info_from_url(self, url: str) -> Dict:
        """基本的な情報を URL から抽出（オフラインモード）"""
        video_id = self.extract_video_id(url)
        return {
            'video_id': video_id,
            'title': '[動画タイトル]',
            'description': '[動画説明欄から抽出してください]',
            'channel': '[チャンネル名]',
            'duration': None,
            'upload_date': None,
            'subtitles': {},
            'url': url,
        }

    def extract_subtitles(self, video_info: Dict) -> str:
        """字幕を取得してテキスト化"""
        subtitles = video_info.get('subtitles', {})

        # 日本語字幕を優先
        if 'ja' in subtitles:
            return self._format_subtitles(subtitles['ja'])
        elif 'en' in subtitles:
            return self._format_subtitles(subtitles['en'])
        elif subtitles:
            lang = list(subtitles.keys())[0]
            return self._format_subtitles(subtitles[lang])
        else:
            return "[字幕がありません。動画説明欄を参考にしてください]"

    def _format_subtitles(self, subtitle_list: List) -> str:
        """字幕リストをテキストに変換"""
        if isinstance(subtitle_list, list):
            texts = []
            for sub in subtitle_list:
                if isinstance(sub, dict) and 'text' in sub:
                    texts.append(sub['text'])
            return '\n'.join(texts)
        return str(subtitle_list)

    def generate_learning_content(self, video_info: Dict, subtitles: str) -> str:
        """Claude で学習内容を生成"""
        if not CLAUDE_AVAILABLE:
            return self._generate_template(video_info)

        prompt = f"""
以下の YouTube 動画から、eBay ビジネスに関連する学習内容を抽出してください。

【動画情報】
- タイトル: {video_info.get('title')}
- チャンネル: {video_info.get('channel')}
- 説明: {video_info.get('description', '')[:500]}

【字幕/説明内容】
{subtitles[:3000]}

【出力形式】
以下の Markdown 形式で出力してください：

# [動画タイトル]

## 概要
[1-2段落の要約]

## 主要なポイント
1. [ポイント1]
2. [ポイント2]
3. [ポイント3]
（4個以上あれば追加）

## eBay ビジネスへの適用
- [実践方法1]
- [実践方法2]
- [注意点]

## キーワード
`キーワード1`, `キーワード2`, `キーワード3`

## 関連部署への連携
- **リサーチ部門**: [活用方法]
- **eBay知識部門**: [知識化の方法]
- **日々業務部門**: [出品戦略への反映]

## スコア
- **内容完全性**: X/5
- **実践性**: X/5
- **ビジネス価値**: X/5
- **総合**: X/5 ✅

注意: スコアは動画の内容充実度によって判定してください。
"""

        try:
            response = self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=2000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            return response.content[0].text
        except Exception as e:
            print(f"⚠️  Claude 処理失敗: {e}")
            return self._generate_template(video_info)

    def _generate_template(self, video_info: Dict) -> str:
        """テンプレート生成（Claude 未使用時）"""
        return f"""# {video_info.get('title', 'YouTube Learning')}

## 動画情報
- **タイトル**: {video_info.get('title')}
- **チャンネル**: {video_info.get('channel')}
- **説明**: {video_info.get('description', '')[:200]}

## 概要
[動画の概要をここに記入してください]

## 主要なポイント
1. [ポイント1]
2. [ポイント2]
3. [ポイント3]

## eBay ビジネスへの適用
- [実践方法を記入してください]

## スコア（手動評価）
- **内容完全性**: __/5
- **実践性**: __/5
- **総合**: __/5
"""

    def save_learning_content(self, video_info: Dict, content: str) -> str:
        """学習内容をファイルに保存"""
        # ファイル名生成
        video_id = video_info.get('video_id', 'unknown')
        title = video_info.get('title', 'learning').lower()
        title = re.sub(r'[^a-z0-9_-]', '', title.replace(' ', '-'))[:30]

        filename = f"{datetime.now().strftime('%Y-%m-%d')}-{title}.md"
        filepath = self.output_dir / filename

        # メタデータ付きで保存
        full_content = f"""---
video_id: {video_info.get('video_id')}
url: https://youtube.com/watch?v={video_info.get('video_id')}
title: {video_info.get('title')}
channel: {video_info.get('channel')}
extracted_date: {datetime.now().isoformat()}
---

{content}
"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(full_content)

        print(f"✅ 学習内容を保存しました: {filepath}")
        return str(filepath)

    def process_youtube_url(self, url: str) -> Dict:
        """YouTube URL を処理するメイン関数"""
        print(f"🔗 URL を処理中: {url}")

        # 1. ビデオ情報取得
        print("📹 ビデオ情報を取得中...")
        video_info = self.get_video_info(url)

        # 2. 字幕抽出
        print("📝 字幕/説明を抽出中...")
        subtitles = self.extract_subtitles(video_info)

        # 3. 学習内容生成
        print("🤖 Claude で学習内容を生成中...")
        learning_content = self.generate_learning_content(video_info, subtitles)

        # 4. ファイルに保存
        print("💾 ファイルに保存中...")
        filepath = self.save_learning_content(video_info, learning_content)

        return {
            'status': '✅ 完了',
            'video_info': video_info,
            'filepath': filepath,
            'content_preview': learning_content[:500] + "...",
        }


def main():
    """メイン実行"""
    print("=" * 60)
    print("YouTube 動画学習パイプライン")
    print("=" * 60)

    processor = YouTubeProcessor()

    # テスト用 URL
    url = "https://www.youtube.com/watch?v=Wfz-gdWcItM"

    print(f"\n📌 処理対象: {url}\n")

    result = processor.process_youtube_url(url)

    print("\n" + "=" * 60)
    print("処理結果")
    print("=" * 60)
    print(json.dumps({
        'status': result['status'],
        'video_title': result['video_info'].get('title'),
        'saved_file': result['filepath'],
        'preview': result['content_preview'],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
