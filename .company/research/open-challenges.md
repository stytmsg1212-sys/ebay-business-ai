# 未解決課題ウォッチリスト

定時実行のニュースウォッチで以下の課題に関連する進展があれば秘書室に通知する。

## CHAL-001: ロゴプレート + 商品写真の合成品質 (60/100 点)

**確認日**: 2026-04-23
**現状**: Gemini 2.5 Flash Image (nano-banana/edit) + Photoroom で 60 点レベル
**主な違和感**:
- プレート形状がゆらぐ (Gemini の abstraction 癖)
- プレートと商品の光源方向/影の整合性が不完全
- プレートが「シーンに物理的に存在する」感が弱い
- 角度不整合で合成バレバレになるケース
- プレート上の MONO ワードマーク/enso が時々崩れる

**期待する改善材料** (ニュース観測候補):
- **multi-image reference 合成モデルの強化** (Gemini 3 Pro Image / GPT-image-2 / Flux Kontext 後継)
- **3D-aware image composition** (ground plane 認識、光源整合)
- **pixel-perfect reference preservation** (参照画像を 100% 維持して合成)
- **inpainting + mask based** で商品を触らずプレートのみ追加
- **Seedream 5.x / Seededit 4.0** (Bytedance の新バージョン、inference.sh)
- **Flux Kontext Max 後継** (multi-reference 版)
- **Ideogram v4 / Ideogram edit** の強化 (テキスト+形状保持)
- **Adobe Firefly Composition API** (もし公開)
- **新しい image-to-image editor** で pixel-accurate プレート配置

**判断基準**: 以下のいずれかが出たら CHAL-001 対応候補として秘書室に通知:
- 新 image 編集 model の pixel-accurate reference preserve 機能
- 3D 整合 (ground plane / shadow direction 自動) 機能
- 既存のコスト以下 ($0.04/compose 以下) で品質上回るサービス

**現在使用中**:
- Photoroom Basic $20/月 (1000 calls, /v2/edit)
- fal.ai Gemini 2.5 Flash Image `nano-banana/edit` ($0.04/call)
- Flux Pro Kontext Max (プレート生成専用、$0.08/call)

**関連ファイル**:
- `tools/ebay-manager/monitor/image_composer_gemini.py`
- `tools/ebay-manager/monitor/image_composer_photoroom.py`
- `tools/ebay-manager/monitor/plate_library.py`
- `tools/ebay-manager/monitor/plate_selector.py`

**回避策 (実装済)**:
- W3 pinned (毎回同じ角度)
- W4 (low foreshortening) との 2 択
- Photoroom #c0c0c0 + depth shading (奥行演出)

---

## フォロー方法

- news_check (`tasks/task_news_check.py`) が Anthropic/AI ニュースを収集する際、キーワードマッチで関連ニュースを検出したら高影響に昇格
- 検出キーワード候補:
  - "image composition", "multi-reference", "image edit", "pixel-accurate"
  - "Seedream", "Seededit", "Flux Kontext", "Gemini Image"
  - "3D-aware", "inpaint", "mask based edit"
- 関連ニュースが出たらこのファイルに日付付きで追記、秘書室 inbox にも通知
