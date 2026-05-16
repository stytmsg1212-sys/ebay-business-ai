# YouTube 動画学習パイプライン - セットアップガイド

**最終更新**: 2026-04-08
**ステータス**: ✅ 自動実行対応版

---

## 🎯 セットアップのゴール

このガイドを完了すると、秘書は以下のコマンド **1行だけ**で YouTube 動画を学習できるようになります：

```powershell
cd "C:\Users\gucch\projects\claude\.company\secretary\learning-pipeline"
python youtube_processor_v2.py
```

API キーの手動設定は **不要**になります。

---

## 📋 初回セットアップ（5分で完了）

### 1️⃣ 依存ライブラリをインストール

```powershell
cd "C:\Users\gucch\projects\claude\.company\secretary\learning-pipeline"
pip install -r requirements.txt
```

**インストール対象**:
- `anthropic>=0.13.0` — Claude API SDK
- `yt-dlp>=2024.01.01` — YouTube ダウンロード
- `openai-whisper>=20230314` — 音声文字起こし
- `python-dotenv>=1.0.0` — **NEW**: 環境変数自動読み込み

---

### 2️⃣ `.env` ファイルを作成して API キーを設定

#### 方法A: Windows GUI（推奨）

```powershell
# PowerShell で以下を実行
notepad .env
```

テキストエディタが開いたら、以下を入力して保存：

```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
```

`sk-ant-xxxxxxxxxxxxxxxx` の部分を **あなたの実際の API キー**に置き換えてください。

#### 方法B: PowerShell コマンド

```powershell
# テンプレートからコピー
copy .env.example .env

# 内容確認
type .env
```

---

### 3️⃣ FFmpeg が PATH に設定されているか確認

```powershell
ffmpeg -version
```

**出力例**:
```
ffmpeg version N-113000-gxxxxxx ...
```

もし `ffmpeg: command not found` と出たら：

```powershell
$env:Path += ";C:\ffmpeg\bin"
ffmpeg -version  # 再確認
```

---

## ✅ セットアップ完了チェックリスト

- [ ] `pip install -r requirements.txt` を実行済み
- [ ] `.env` ファイルが存在し、`ANTHROPIC_API_KEY=sk-ant-...` が入っている
- [ ] `ffmpeg -version` で FFmpeg が認識される
- [ ] `python youtube_processor_v2.py` を実行できる

---

## 🚀 テスト実行

セットアップが完了したら、テスト実行してみましょう：

```powershell
cd "C:\Users\gucch\projects\claude\.company\secretary\learning-pipeline"
python youtube_processor_v2.py
```

**期待される流れ**:
```
🔄 Whisper モデルを読み込み中...
📌 処理対象: https://www.youtube.com/watch?v=3e4NWeKS2To

📹 ビデオ情報を取得中...
⏬ 音声をダウンロード中...
🎙️  音声を文字起こし中...  ← 10分程度待つ
🤖 Claude で学習内容を生成中...  ← API キーが有効なら実行
✅ 学習内容を保存しました: research/learning/2026-04-08--2026-2-14-.md
```

---

## 📝 使用方法（毎回）

### URL を変更する場合

`youtube_processor_v2.py` の 348 行目を編集：

```python
# テスト用 URL
url = "https://www.youtube.com/watch?v=新しいURL"
```

### スクリプト実行

```powershell
cd "C:\Users\gucch\projects\claude\.company\secretary\learning-pipeline"
python youtube_processor_v2.py
```

出力ファイルは自動的に `research/learning/` に保存されます。

---

## 🔐 セキュリティ上の注意

### `.env` ファイルについて

- **重要**: `.env` ファイルは `.gitignore` で保護されています（Git コミット対象外）
- **重要**: API キーを含むため、絶対にプッシュしないでください
- **重要**: 他人と共有しないでください

### API キーの管理

- `.env` は**ローカルマシンのみ**に存在
- 本番環境では環境変数を直接設定するか、Secrets Manager を使用
- API キーが漏洩した場合は、Anthropic コンソールで即座に無効化

---

## 🆘 トラブルシューティング

### Q: `ModuleNotFoundError: No module named 'dotenv'`

**原因**: `python-dotenv` がインストールされていない

**対処**:
```powershell
pip install python-dotenv
```

### Q: `.env` ファイルが見つからない

**原因**: `.env` ファイルが作成されていない

**対処**:
```powershell
notepad .env
# ANTHROPIC_API_KEY=sk-ant-... と入力して保存
```

### Q: `Could not resolve authentication method`

**原因**: `.env` が存在するが API キーが無効

**対処**:
```powershell
type .env  # 内容確認
# ANTHROPIC_API_KEY が正しく設定されているか確認
```

---

## 📞 サポート

不明な点は秘書に相談してください！

