# W10 画像加工システム セットアップ

eBay 個別出品用のカバー画像合成 + 背景除去パイプライン。

## 前提となる成果物

| 種類 | パス | 取得方法 |
|------|------|---------|
| MonoHonpo ロゴ (元) | `assets/monohonpo_logo.jpg` | 既存素材をコピー |
| MonoHonpo ロゴ (透過) | `assets/monohonpo_logo_transparent.png` | Phase A スクリプトで前処理生成 |
| rembg U²-Net モデル | `models/u2net.onnx` (~170MB) | 初回実行時に自動 DL、または手動配置 |
| 加工済画像キャッシュ | `data/processed_images/<draft_id>/` | 実行時に自動生成 |

## rembg モデルの配置

rembg は既定では `~/.u2net/` にモデルを DL する。本プロジェクトでは
`settings.json:image_processing.rembg_model_dir = "models"` で
プロジェクト配下の `models/` に固定する。

### 自動 DL (推奨)

初回 `remove()` 呼び出し時に rembg が HTTP 経由で
`u2net.onnx` を取得する。`models/` ディレクトリが無ければ自動作成される。

**実装側の注意**: `image_bg_remover.py` (Phase B 実装予定) では
以下パターンで session を作ること:

```python
from rembg import new_session
from pathlib import Path
import os

MODEL_DIR = Path(__file__).parent.parent / "models"
os.environ.setdefault("U2NET_HOME", str(MODEL_DIR))
session = new_session(model_name="u2net")  # 初回 DL が走る
```

### 手動配置 (オフライン環境)

開発マシン A で DL 済のモデルを持ち歩く場合:

```bash
cp ~/.u2net/u2net.onnx tools/ebay-manager/models/u2net.onnx
```

### git 管理

`.gitignore` で `models/*.onnx` を除外済。170MB のバイナリは
リポジトリに含めない。新環境では自動 DL に任せる。

## eBay EPS (Picture Service) 制約

`settings.json:image_processing.eps_*` に制約値を記録:

| 項目 | 値 | 出典 |
|------|---|------|
| ファイルサイズ上限 | 12,582,912 bytes (12MB) | eBay Developer Docs |
| 最小寸法 | 500px 以上 | eBay Developer Docs (500 未満は reject) |
| 合計枚数 | 24 枚/出品 | `ebay_lister.py:_MAX_PICTURES` |

## 合成パラメータの調整

`settings.json:image_processing` で layout / shadow / background を設定可能。
デフォルトは Pioneer サンプル (`tests/fixtures/design_samples/sample_01_pioneer_stack.HEIC`)
を参照した studio 風:

- Canvas: 1600x1600
- Product 占有率: 70%
- Card 占有率: 22% (`layout.card_ratio` で管理、`card.width_ratio` は重複のため削除済)
- Background: vertical gradient (#ECEC → #D8D8)
- Shadow: floor shadow, blur 22px, offset_y 18px

Phase C-1 は上記固定値で実装、Phase C-2 で商品形状に応じて
Claude Opus 4.8 が layout JSON を生成する adaptive 版に拡張予定。
