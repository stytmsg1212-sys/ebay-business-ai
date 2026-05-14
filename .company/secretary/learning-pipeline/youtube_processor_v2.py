#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 動画学習パイプライン v2
実際の動画音声を Whisper で文字起こしして学習するシステム
"""

import os
import json
import re
import sys
import subprocess
import tempfile
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path

# 環境変数を .env ファイルから読み込み
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv がインストールされていない場合はスキップ

# Windows PowerShell での UTF-8 対応
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# FFmpeg を PATH に追加（Whisper が内部で使用）
ffmpeg_path = r"C:\ffmpeg\bin"
if ffmpeg_path not in os.environ.get('PATH', ''):
    os.environ['PATH'] = ffmpeg_path + os.pathsep + os.environ.get('PATH', '')

try:
    import yt_dlp
    YDLP_AVAILABLE = True
except ImportError:
    YDLP_AVAILABLE = False
    print("⚠️  yt_dlp not installed. Install with: pip install yt-dlp")

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("⚠️  openai-whisper not installed. Install with: pip install openai-whisper")

# Claude API は使用しません（コスト削減）
# 秘書とリサーチ部署が文字起こしテキストを分析します


class YouTubeProcessorV2:
    """YouTube 動画から音声を文字起こしして保存（コスト削減版）

    Whisper で完全な音声文字起こしを行い、秘書とリサーチ部署が分析します。
    Claude API は使用しません（毎回のAPI呼び出しコストを削減）。
    """

    def __init__(self):
        self.ydl_client = None
        self.whisper_model = None
        if WHISPER_AVAILABLE:
            print("🔄 Whisper モデルを読み込み中...")
            self.whisper_model = whisper.load_model("base")

        # 正しいパスを絶対パスで指定
        self.output_dir = Path(__file__).parent.parent.parent / "research" / "learning"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.mkdtemp()

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
            return {}

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
                    'url': url,
                }
        except Exception as e:
            print(f"⚠️  動画情報取得失敗: {e}")
            return {'url': url}

    def download_audio(self, url: str) -> Optional[str]:
        """YouTube から音声をダウンロード（FFmpeg不要）"""
        if not YDLP_AVAILABLE:
            print("❌ yt_dlp が必要です")
            return None

        try:
            print("⏬ 音声をダウンロード中...")
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(Path(self.temp_dir) / 'audio.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # ダウンロードされたファイルを探す
                audio_file = None
                for ext in ['m4a', 'mp3', 'wav', 'webm', 'mp4']:
                    candidate = Path(self.temp_dir) / f"audio.{ext}"
                    if candidate.exists():
                        audio_file = candidate
                        break

                if audio_file:
                    print(f"✅ 音声ダウンロード完了: {audio_file}")
                    return str(audio_file)
                else:
                    print(f"⚠️  音声ファイルが見つかりません")
                    return None
        except Exception as e:
            print(f"⚠️  音声ダウンロード失敗: {e}")
            return None

    def transcribe_audio(self, audio_file: str) -> str:
        """Whisper で音声を文字起こし（ローカル実行版）"""
        if not WHISPER_AVAILABLE:
            print("❌ openai-whisper が必要です: pip install openai-whisper")
            return "[音声文字起こしが利用できません]"

        try:
            print("🎙️  音声を文字起こし中（Whisper）...")

            # ファイルの存在確認
            if not os.path.exists(audio_file):
                print(f"⚠️  ファイルが見つかりません: {audio_file}")
                return "[音声ファイルが見つかりません]"

            print(f"📁 ファイル確認: {audio_file} ({os.path.getsize(audio_file)} bytes)")

            # Whisper で処理
            print("⏳ Whisper で処理中（CPU使用）...")
            result = self.whisper_model.transcribe(audio_file, language="ja", verbose=False)
            text = result["text"]
            print(f"✅ 文字起こし完了: {len(text)} 文字")
            return text
        except Exception as e:
            print(f"⚠️  文字起こし失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return "[音声文字起こしに失敗しました]"


    def generate_learning_content(self, video_info: Dict, transcript: str) -> str:
        """完全な文字起こしを学習コンテンツとして生成"""
        return f"""# {video_info.get('title', 'YouTube Learning')}

## 動画情報
- **タイトル**: {video_info.get('title')}
- **URL**: {video_info.get('url')}
- **チャンネル**: {video_info.get('channel')}
- **文字起こし方式**: Whisper による自動音声認識
- **文字数**: {len(transcript):,} 文字

## 完全な音声文字起こし

{transcript}

---

## 学習ノート（秘書・リサーチ部署用）

### 主要なポイント
[秘書またはリサーチ部署が記入]

### eBay ビジネスへの適用
[秘書またはリサーチ部署が記入]

### 実践アクション
[秘書またはリサーチ部署が記入]

## 注記
- このファイルは Whisper による自動音声認識から生成されました
- 秘書とリサーチ部署が文字起こしテキストを分析して、学習ノートを記入します
- 分析完了後、このセクションに学習内容を追記してください
"""

    def save_learning_content(self, video_info: Dict, content: str) -> str:
        """学習内容をファイルに保存"""
        video_id = video_info.get('video_id', 'unknown')
        title = video_info.get('title', 'learning').lower()
        title = re.sub(r'[^a-z0-9_-]', '', title.replace(' ', '-'))[:30]

        filename = f"{datetime.now().strftime('%Y-%m-%d')}-{title}.md"
        filepath = self.output_dir / filename

        full_content = f"""---
video_id: {video_info.get('video_id')}
url: {video_info.get('url')}
title: {video_info.get('title')}
channel: {video_info.get('channel')}
extracted_date: {datetime.now().isoformat()}
transcription_method: Whisper (OpenAI Audio API)
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

        # 2. 音声ダウンロード
        print("⏬ 音声をダウンロード中...")
        audio_file = self.download_audio(url)

        if not audio_file:
            return {
                'status': '❌ 失敗',
                'error': '音声ダウンロードに失敗しました',
            }

        # 3. 音声を文字起こし
        transcript = self.transcribe_audio(audio_file)

        if transcript == "[音声文字起こしに失敗しました]":
            return {
                'status': '❌ 失敗',
                'error': '音声文字起こしに失敗しました',
            }

        # 4. 学習コンテンツ生成（完全な文字起こし + テンプレート）
        print("📝 学習コンテンツを生成中...")
        learning_content = self.generate_learning_content(video_info, transcript)

        # 5. ファイルに保存
        print("💾 ファイルに保存中...")
        filepath = self.save_learning_content(video_info, learning_content)

        # クリーンアップ
        try:
            os.remove(audio_file)
        except:
            pass

        return {
            'status': '✅ 完了',
            'video_info': video_info,
            'filepath': filepath,
            'transcript_length': len(transcript),
            'content_preview': learning_content[:500] + "...",
        }


def main():
    """メイン実行"""
    print("=" * 60)
    print("YouTube 動画学習パイプライン v2")
    print("（Whisper 文字起こし + 秘書分析版）")
    print("=" * 60)

    processor = YouTubeProcessorV2()

    # コマンドライン引数から URL を取得、ない場合はデフォルト
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        # デフォルト URL（テスト用）
        url = "https://www.youtube.com/watch?v=3e4NWeKS2To"

    print(f"\n📌 処理対象: {url}\n")

    result = processor.process_youtube_url(url)

    print("\n" + "=" * 60)
    print("処理結果")
    print("=" * 60)
    print(json.dumps({
        'status': result['status'],
        'video_title': result.get('video_info', {}).get('title'),
        'saved_file': result.get('filepath'),
        'transcript_length': result.get('transcript_length', 0),
        'preview': result.get('content_preview'),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
