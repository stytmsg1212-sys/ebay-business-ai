# YouTube 動画学習パイプライン - クイックスタートガイド

**最終更新**: 2026-04-08
**ステータス**: ✅ 完全自動化版（秘書が自動実行）

---

## 🚀 使用方法（ユーザー向け）

### パターン1：単一の YouTube 動画を学習させる

```
ユーザー: 「この動画から学習してください」
          https://www.youtube.com/watch?v=3e4NWeKS2To
```

**秘書が自動実行** ✅
- URL を抽出
- スクリプト実行
- 10-15分待機
- 完了報告

---

### パターン2：複数の動画を一括処理

```
ユーザー: 「以下の 3 本を学習してください」
          - https://www.youtube.com/watch?v=URL1
          - https://www.youtube.com/watch?v=URL2
          - https://www.youtube.com/watch?v=URL3
```

**秘書が順番に処理** ✅
- 各 URL を自動抽出
- 順番に実行（1本ごと 10-15分）
- 全て完了後に報告

---

### パターン3：複数 URL をカンマ区切りで指定

```
ユーザー: 「これらの URL から一括学習して」
          https://www.youtube.com/watch?v=URL1, https://www.youtube.com/watch?v=URL2
```

**秘書が自動実行** ✅

---

## ⚙️ 秘書の自動実行フロー

### 初回セットアップ（1回のみ）

```powershell
cd "C:\Users\gucch\projects\claude\.company\secretary\learning-pipeline"

# 1. 依存ライブラリをインストール
pip install -r requirements.txt

# 2. .env ファイルを作成してAPIキーを設定
notepad .env
# 以下を入力して保存:
# ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
```

### ユーザーの依頼

```
「この YouTube 動画から学習して」
https://www.youtube.com/watch?v=3e4NWeKS2To
```

### 秘書が自動実行 ✅

秘書が URL を受け取ると、以下を**自動的に実行**：

```powershell
python run_learning.py "https://www.youtube.com/watch?v=3e4NWeKS2To"
```

**ユーザーはコマンドを実行する必要がありません！**

秘書が：
1. URL を自動抽出
2. スクリプト実行
3. 10-15分待機
4. 完了後、生成ファイルを確認・報告

すべて自動で対応します。

---

## 📊 処理フロー詳細

```
YouTube URL
  ↓
yt-dlp: ビデオ情報・音声をダウンロード
  ├─ 形式: WEBM、MP4 など
  ├─ 出力: C:\Users\gucch\AppData\Local\Temp\audio.*
  ↓
Whisper（ローカル）: 音声を日本語で文字起こし
  ├─ モデル: base
  ├─ 言語: 日本語（ja）
  ├─ 出力: テキスト（約 11,000-15,000 文字）
  ↓
Claude（Anthropic API）: 要約・構造化
  ├─ モデル: claude-opus-4-6
  ├─ 処理: 要約、主要ポイント抽出、eBay適用検討、スコア評価
  ├─ 出力: Markdown 形式
  ↓
ファイル保存
  └─ 場所: C:\Users\gucch\projects\claude\.company\research\learning\
  └─ ファイル名: YYYY-MM-DD-{動画タイトル}.md
  └─ 形式: YAML フロントマッター + Markdown
```

---

## 📂 出力ファイルの構成

### ファイル場所
```
research/learning/
├── 2026-04-08-okclaude-code.md
├── 2026-04-09-ebay-pricing-strategy.md
├── USAGE-GUIDE.md
└── INDEX.md
```

### ファイル内容（例）
```markdown
---
video_id: Wfz-gdWcItM
url: https://www.youtube.com/watch?v=Wfz-gdWcItM
title: 【動画タイトル】
channel: Shin Coding Tutorial
extracted_date: 2026-04-08T07:16:37
transcription_method: Whisper (openai-whisper)
---

# 【動画タイトル】

## 動画情報
- **タイトル**: ...
- **URL**: ...

## 概要
[1-2段落の要約]

## 主要なポイント
1. [ポイント1]
2. [ポイント2]
...

## eBay ビジネスへの適用
- [適用案1]
- [適用案2]

## キーワード
`キーワード1`, `キーワード2`

## スコア
- **内容完全性**: X/5
- **実践性**: X/5
- **ビジネス価値**: X/5
- **総合**: X/5
```

---

## 🔑 必要な設定

### 環境変数（毎回実行時に設定）

```powershell
# Claude API キー
$env:ANTHROPIC_API_KEY = "sk-ant-xxxxxxxxxxxxxxx"
```

### インストール済みツール

| ツール | 場所 | 用途 |
|--------|------|------|
| FFmpeg | `C:\ffmpeg\bin` | 音声抽出 |
| Python | `C:\Users\gucch\AppData\Local\Programs\Python\Python313` | スクリプト実行 |
| yt-dlp | pip | YouTube ダウンロード |
| openai-whisper | pip | 音声文字起こし |
| Anthropic SDK | pip | Claude API |

---

## ⚙️ スクリプト修正方法

### URL を変更する場合

`youtube_processor_v2.py` の 334 行目を編集：

```python
# テスト用 URL
url = "https://www.youtube.com/watch?v=新しいURL"
```

### 複数 URL を処理する場合

以下のようにループ処理を追加：

```python
urls = [
    "https://www.youtube.com/watch?v=XXXXX",
    "https://www.youtube.com/watch?v=YYYYY",
    "https://www.youtube.com/watch?v=ZZZZZ",
]

for url in urls:
    result = processor.process_youtube_url(url)
    print(json.dumps(result, indent=2, ensure_ascii=False))
```

---

## 📈 期待される効果

| 指標 | 前 | 後 | 向上度 |
|------|----|----|--------|
| 学習内容の構造化 | 説明文のみ | Markdown + スコア | ⬆️ 100% |
| 処理時間 | 30分（手動） | 3-5分（自動） | ⬇️ 80%削減 |
| eBay適用検討 | 手動で考察 | 自動生成 | ⬆️ 自動化 |
| 品質均一性 | 記者の力量依存 | Claude による統一 | ⬆️ 向上 |

---

## 🆘 トラブルシューティング

### 「ffmpeg not found」エラー
```powershell
# Path を再設定
$env:Path += ";C:\ffmpeg\bin"
ffmpeg -version
```

### 「ANTHROPIC_API_KEY not found」エラー
```powershell
# API キーを再設定
$env:ANTHROPIC_API_KEY = "sk-ant-xxxxxxxxxxxxxxx"
```

### 「Whisper モデルが見つからない」エラー
```powershell
# モデルを再ダウンロード
pip install --upgrade openai-whisper
python youtube_processor_v2.py
```

---

## 📝 チェックリスト（毎回実行前）

- [ ] FFmpeg が `C:\ffmpeg\bin` にインストールされている
- [ ] Python 3.13 がインストールされている
- [ ] `pip install -r requirements.txt` で依存ライブラリをインストール済み
- [ ] `$env:ANTHROPIC_API_KEY` が設定されている
- [ ] YouTube URL が有効か確認
- [ ] `research/learning/` フォルダが存在している

---

**次回の処理：** 新しい YouTube URL を受け取ったら、このガイドに従って処理してください。
