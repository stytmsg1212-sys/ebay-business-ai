#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商品説明から構造化属性 (重量/寸法/付属品) を抽出する Claude Haiku wrapper.

supplier_scraper の regex で拾えなかった自然文ケースの救済用:
- 「本体は約 1.5kg の重さです」 → weight_g=1500
- 「リモコン、ACアダプター、説明書をお付けします」 → includes_list=["リモコン", ...]
- 「サイズは約 30cm 四方で高さ 10cm です」 → length/width/depth

設計方針:
- Claude Haiku 4.5 (低コスト+高速)
- STABLE システムプロンプトで prompt cache を効かせる
- 入力は description_ja のみ (タイトルや条件は使わない)
- 確信度が低い場合は None で返す (hallucination 防止)
- API 未設定 / 失敗 時は空 dict を返して呼出側 fallback
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / '.env'
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

logger = logging.getLogger(__name__)

# Haiku で十分: 構造化抽出は有界タスク
CLAUDE_MODEL = 'claude-haiku-4-5-20251001'

_STABLE_SYSTEM_PROMPT = """あなたは商品説明から重量/寸法/付属品を構造化抽出する専門家です。

## タスク

入力された日本語商品説明から、**明記されている情報のみ** を JSON で返します。
推測や hallucination は絶対禁止 — 不明なら null / 空配列を返してください。

## 出力フォーマット (JSON のみ、コードブロック禁止)

{
  "weight_g": 1500,           // 総重量 (グラム単位、整数)。kg 表記は ×1000 で変換。不明なら null
  "length_mm": 300,           // 長さ (mm)。cm は ×10 で変換。不明なら null
  "width_mm": 200,            // 幅 (mm)。不明なら null
  "depth_mm": 100,            // 奥行/高さ (mm)。不明なら null
  "includes_list": [          // 付属品のリスト (日本語)。明示されているもののみ
    "リモコン",
    "ACアダプター",
    "取扱説明書"
  ]
}

## 厳守ルール

1. **明示されていない項目は null / 空配列**:
   「本体のみ」「付属品無し」と書かれていれば includes_list=[]、
   何も書かれていない場合も null または []。

2. **単位変換**:
   - 重量: g / kg / グラム / キログラム / KG / ㎏ → すべて g に統一
   - 長さ: mm / cm / m / ㎝ / ㎜ → すべて mm に統一

3. **寸法の語彙**:
   - 幅 / 横 / W = width_mm
   - 奥行 / D / 縦 = depth_mm
   - 高さ / H / 厚み / 厚さ = length_mm
   (ただし記載順が不明瞭な "30×20×10cm" 形式は length=30, width=20, depth=10 の順で解釈)

4. **範囲妥当性チェック**:
   - 重量: 1g 〜 50,000g (50kg) の範囲外は null
   - 寸法: 1mm 〜 5,000mm (5m) の範囲外は null

5. **付属品の粒度**:
   - 「リモコンと説明書と電源ケーブル」→ ["リモコン", "説明書", "電源ケーブル"]
   - 「元箱・元付属品完備」→ ["元箱", "元付属品"]
   - 曖昧な「その他付属品あり」→ リストに入れない
   - ブランド名等の固有名詞は含めて OK ("Sony WH-1000XM5 本体" 等)

6. **ハルシネーション禁止**:
   「スピーカーです」だけで本体重量を 「500g くらいかな」と推測してはいけない。
   本当に明記されている数値のみ返す。

## 具体例

入力: "本体重量は約 1.5kg、サイズは 30×20×10cm です。付属品としてリモコンと取扱説明書をお付けします。"
出力: {"weight_g": 1500, "length_mm": 300, "width_mm": 200, "depth_mm": 100,
       "includes_list": ["リモコン", "取扱説明書"]}

入力: "中古のスピーカーです。色は Silver。"
出力: {"weight_g": null, "length_mm": null, "width_mm": null, "depth_mm": null,
       "includes_list": []}

入力: "本体のみの出品です。重量約 2kg。"
出力: {"weight_g": 2000, "length_mm": null, "width_mm": null, "depth_mm": null,
       "includes_list": []}
"""


def _get_client() -> Optional["anthropic.Anthropic"]:
    if not _ANTHROPIC_OK:
        return None
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_json(text: str) -> Optional[str]:
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    return None


def extract_product_attrs(description_ja: Optional[str]) -> dict:
    """商品説明から重量・寸法・付属品を Claude Haiku で抽出する。

    Args:
        description_ja: 商品説明 (日本語)。短すぎる (< 30 chars) 場合はスキップ。

    Returns:
        {
            'weight_g': int | None,
            'length_mm': int | None,
            'width_mm': int | None,
            'depth_mm': int | None,
            'includes_list': list[str],  # 付属品名の配列
        }
        API 失敗時は全 None / 空配列。
    """
    empty_result = {
        'weight_g': None, 'length_mm': None, 'width_mm': None, 'depth_mm': None,
        'includes_list': [],
    }
    if not description_ja or len(description_ja.strip()) < 30:
        return empty_result

    client = _get_client()
    if not client:
        logger.debug('Anthropic API unavailable → skipping LLM extraction')
        return empty_result

    from monitor.api_logger import log_anthropic_response, _Timer

    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                system=[
                    {
                        'type': 'text',
                        'text': _STABLE_SYSTEM_PROMPT,
                        'cache_control': {'type': 'ephemeral'},
                    }
                ],
                messages=[{
                    'role': 'user',
                    'content': (
                        f'以下の商品説明から重量/寸法/付属品を JSON 抽出してください。\n\n'
                        f'---\n{description_ja[:2000]}\n---'
                    ),
                }],
            )
        log_anthropic_response(
            'product_attrs_extract', CLAUDE_MODEL, msg,
            duration_ms=t.duration_ms, success=True,
        )
    except anthropic.APIError as e:
        logger.warning(f'product_attrs API error: {e}')
        log_anthropic_response(
            'product_attrs_extract', CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        return empty_result
    except Exception as e:  # noqa: BLE001
        logger.warning(f'product_attrs unexpected: {e}')
        return empty_result

    text = ''.join(
        getattr(b, 'text', '') for b in msg.content
        if getattr(b, 'type', None) == 'text'
    )
    cand = _extract_json(text)
    if not cand:
        logger.warning(f'product_attrs: no JSON in response: {text[:120]!r}')
        return empty_result

    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f'product_attrs JSON decode: {e}')
        return empty_result

    # サニタイズ + 範囲チェック
    def _clamp_int(v, lo: int, hi: int) -> Optional[int]:
        if v is None:
            return None
        try:
            iv = int(v)
            return iv if lo <= iv <= hi else None
        except (TypeError, ValueError):
            return None

    includes_raw = data.get('includes_list') or []
    includes_list: list[str] = []
    if isinstance(includes_raw, list):
        for item in includes_raw[:20]:  # 最大 20 個
            s = str(item).strip()[:60]  # 各要素 60 字
            if s:
                includes_list.append(s)

    return {
        'weight_g': _clamp_int(data.get('weight_g'), 1, 50_000),
        'length_mm': _clamp_int(data.get('length_mm'), 1, 5_000),
        'width_mm': _clamp_int(data.get('width_mm'), 1, 5_000),
        'depth_mm': _clamp_int(data.get('depth_mm'), 1, 5_000),
        'includes_list': includes_list,
    }


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('description', help='商品説明')
    args = parser.parse_args()
    result = extract_product_attrs(args.description)
    print(json.dumps(result, ensure_ascii=False, indent=2))
