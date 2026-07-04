#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eBay 出品データ生成モジュール (W9 Phase 3)

仕入先スクレイプ結果 + 参考 eBay Listing + ランク + description テンプレ v4
を Claude Sonnet に投入し、以下を生成する:
  - 英語タイトル (SEO 最適化、80字以内)
  - description HTML (v4 テンプレにプレースホルダ埋込済み)
  - eBay Category ID (参考 listing があれば採用、なければ Claude 3候補)
  - Item Specifics 値 (参考 listing のキー構造に値を埋める)

設計方針:
  - 3層キャッシュ構造 (STABLE + DYNAMIC + reference) を claude_evaluator から踏襲
  - description 本文は Claude に返させない (HTML 組立は本モジュールで行う)
  - Claude は **placeholder values の JSON** のみを返す
  - テンプレ正源 (listing-description-template.md) の placeholder 仕様を厳守し、
    将来 v5 に変えても本モジュールの I/F (14種 placeholder map) は壊れない
  - Gadget Mode 判定はテンプレ正源のルール (category + brand + specs_count) を実装

正源:
  .company/ebay-knowledge/topics/listing-description-template.md (v4)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as _html_escape

# pythonw / Streamlit headless gotcha ガード
# 2026-04-26 fix: stderr も同様にケアする (個別出品「生成」で OSError [Errno 22]
# が出ていたバグ対策). Streamlit + pythonw.exe では sys.stderr が無効な handle
# を指すケースがあり、直接 print(file=sys.stderr) すると Invalid argument エラー.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _safe_stderr_print(msg: str) -> None:
    """sys.stderr が pythonw 環境で無効な場合でも例外を起こさない安全 print.

    [Errno 22] Invalid argument を防ぐ. logger.warning とは別に Streamlit ログへ
    痕跡を残す目的だが、stderr 書込失敗時はサイレントに諦める (logger 経由で
    既に記録されているため).
    """
    try:
        if sys.stderr is None:
            return
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        # stderr 書込失敗は致命ではない (logger が既に記録)
        pass

# .env ロード
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
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

# Listing 生成は複数制約 (SEO / カテゴリ推定 / Item Specifics 抽出 / 日本語→英語変換)
# を同時に満たすマルチ制約タスク。2026-07-01 Sonnet 5 へ移行。
# effort=medium (公式: Sonnet 5 medium ≈ Sonnet 4.6 high) で現行同品質かつコスト最適化。
# 品質課題が出たら opus-4-8 に切替。
CLAUDE_MODEL = "claude-sonnet-5"


# =========================================================================
# Gadget Mode 判定定数 (テンプレ正源と同期)
# =========================================================================

# Consumer Electronics (293) / Cameras & Photo (625) / Business & Industrial (12576)
GADGET_CATEGORIES: frozenset[str] = frozenset({"293", "625", "12576"})

GADGET_BRANDS: frozenset[str] = frozenset({
    "KEYENCE", "Omron", "Mitsubishi", "Hitachi",
    "Nikon", "Canon", "Sony", "Panasonic", "Zoom", "Roland",
})


# =========================================================================
# dataclass
# =========================================================================

@dataclass
class GeneratedListing:
    """Claude による eBay 出品データ生成結果。"""
    ebay_title: str = ""                                # 80字以内 SEO タイトル
    ebay_description: str = ""                           # v4 HTML placeholder 埋込完成版
    ebay_category_id: Optional[str] = None               # 参考 listing から or Claude 推定
    ebay_category_name: Optional[str] = None
    item_specifics: dict[str, str] = field(default_factory=dict)
    # #44 (2026-07-04): eBay ConditionDescription 用、65字以内の英文ランク要約。
    # 付属品欠品/傷の位置などの細部は description 本文へ (condition_description には
    # 入れない、CLAUDE.md「Quick Notes」との役割分離)。生成失敗/未出力時は空文字。
    condition_description: str = ""
    # 参考 listing が無い場合の Claude 提案 category 3候補
    # 各要素は {"category_id": "...", "category_name": "...", "reasoning": "..."}
    category_candidates: list[dict] = field(default_factory=list)
    listing_price_usd: Optional[float] = None            # 利益計算タブへの連携用 (optional)
    mode_class: str = "default"                          # 'default' or 'gadget'
    generate_error: Optional[str] = None                 # Claude 失敗時のメッセージ
    # W84 候補 D (2026-05-02): 4 区分送料切替用。 ebay_listings.primary_market を伝搬
    # (UI で DB から取得し draft 構築時に inject). None で旧挙動 (mixed_global default).
    primary_market: Optional[str] = None
    # W89 (DDP strict 化) 用 lookup key. None で warning パス無効.
    hs_code: Optional[str] = None


# =========================================================================
# テンプレ正源ヘルパ: placeholder 置換 / 行生成
# =========================================================================

def render_description(template_body: str, values: dict[str, str]) -> str:
    """`{{placeholder}}` パターンを values dict で置換する。

    - CSS の `{` `}` や `{不明placeholder}` (単波括弧) は触らない。
    - `{{missing_key}}` は空文字に置換 (テンプレ生存性重視)。

    Args:
        template_body: v4 HTML テンプレ本文
        values: placeholder name → 値 dict (既に HTML escape 済みの前提)

    Returns:
        置換後 HTML
    """
    if not template_body:
        return ""

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        v = values.get(key)
        if v is None:
            return ""
        return str(v)

    return re.sub(r"\{\{(\w+)\}\}", _replace, template_body)


def build_includes_rows(items: list[dict]) -> str:
    """付属品リスト HTML 行を生成。

    Args:
        items: [{'label': 'Headphones', 'detail': 'Sony WH-1000XM5'}, ...]

    Returns:
        `<div class="mh-inc">...</div>` を連結した HTML 文字列
    """
    if not items:
        return ""
    rows: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = _html_escape(str(it.get("label", "")).strip())
        detail = _html_escape(str(it.get("detail", "")).strip())
        if not label and not detail:
            continue
        rows.append(
            f'<div class="mh-inc"><strong>{label}</strong>{detail}</div>'
        )
    return "\n".join(rows)


def build_specs_rows(specs: list) -> str:
    """仕様テーブル行 HTML を生成。

    Args:
        specs: [(key, value), ...] のタプル or dict 形式を受け入れ

    Returns:
        `<tr><td>...</td><td>...</td></tr>` 連結 HTML
    """
    if not specs:
        return ""
    rows: list[str] = []
    for entry in specs:
        if isinstance(entry, dict):
            k = entry.get("key") or entry.get("name") or ""
            v = entry.get("value") or ""
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            k, v = entry[0], entry[1]
        else:
            continue
        k_esc = _html_escape(str(k).strip())
        v_esc = _html_escape(str(v).strip())
        if not k_esc:
            continue
        rows.append(f"<tr><td>{k_esc}</td><td>{v_esc}</td></tr>")
    return "\n".join(rows)


def build_spec_strip_rows(trio: list) -> str:
    """Gadget Mode spec strip (3カラム) HTML を生成。

    最大3項目。Gadget 以外は空文字呼び出しで空カラム扱い。
    """
    if not trio:
        return ""
    rows: list[str] = []
    for entry in list(trio)[:3]:
        if isinstance(entry, dict):
            k = entry.get("key") or entry.get("name") or ""
            v = entry.get("value") or ""
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            k, v = entry[0], entry[1]
        else:
            continue
        k_esc = _html_escape(str(k).strip())
        v_esc = _html_escape(str(v).strip())
        if not k_esc and not v_esc:
            continue
        rows.append(
            f'<div><div class="k">{k_esc}</div><div class="v">{v_esc}</div></div>'
        )
    return "".join(rows)


def detect_mode(
    category_id: Optional[str],
    brand: Optional[str],
    specs_count: int,
) -> str:
    """Gadget Mode 判定。テンプレ正源のルール:

      1. Category ID が GADGET_CATEGORIES 配下なら gadget
      2. Brand が GADGET_BRANDS に含まれれば gadget
      3. specs_count >= 3 なら gadget
      4. それ以外 default
    """
    # Category ID 判定: 正源の `category_id.split("/")[0]` パターンを採用
    # (実際の eBay category は数値単独だが、将来の階層表現に備える)
    if category_id:
        cat_root = str(category_id).split("/")[0].strip()
        if cat_root in GADGET_CATEGORIES:
            return "gadget"

    if brand:
        brand_norm = str(brand).strip()
        if brand_norm in GADGET_BRANDS:
            return "gadget"

    try:
        if int(specs_count) >= 3:
            return "gadget"
    except (TypeError, ValueError):
        pass

    return "default"


# =========================================================================
# Claude 呼び出し用プロンプト
# =========================================================================

# STABLE プロンプト (prompt cache 対象)。テンプレ仕様改訂時のみ変わる。
# テンプレ正源の placeholder 仕様と Gadget Mode ルールを埋め込む。
_STABLE_SYSTEM_PROMPT = """あなたは eBay 越境EC セラーの出品データ生成アシスタントです。
日本の仕入先 (ヤフオク / メルカリ / PayPayフリマ) の商品情報を元に、
eBay 出品用の英語タイトル、Item Specifics、description placeholder 値を生成します。

## 出力フォーマット (JSON のみ、コードブロック禁止)

{
  "title": "英語タイトル (80字以内、SEO 最適化)",
  "product_sub": "商品サブタイトル (italic、70字以内、モデル/色/状態ハイライト)",
  "quick_notes": "商品個別の状態メモ (英語、2〜4文)",
  "includes_items": [
    {"label": "Main Unit", "detail": "Sony WH-1000XM5 (Black)"},
    ...
  ],
  "specs": [
    {"key": "Brand", "value": "Sony"},
    {"key": "Model", "value": "WH-1000XM5"},
    ...
  ],
  "spec_strip": [
    {"key": "BATTERY", "value": "30h"},
    ...  // 最大3項目、Gadget Mode 用の測定値 Trio、非該当なら空配列
  ],
  "category_id": "採用する eBay CategoryID (参考 listing 指定ありならその ID をそのまま、無ければ最適候補1つ)",
  "category_name": "そのカテゴリの日本語/英語名",
  "category_candidates": [
    // 参考 listing ありなら空配列
    // なしの場合は候補3件を提案
    {"category_id": "293", "category_name": "Consumer Electronics", "reasoning": "..."},
    ...
  ],
  "item_specifics": {
    "Brand": "Sony",
    "Model": "WH-1000XM5",
    ...
  },
  "condition_description": "eBay ConditionDescription 用のランク要約 (65字以内、英語)",
  "shipping_origin": "Tokyo, Japan (固定推奨)",
  "shipping_carrier": "FedEx International Priority / DHL SpeedPAK \u00b7 tracked, insured (settings.json shipping_timing.carrier_label で上書き可)",
  "shipping_handling": "(settings.json shipping_timing で上書きされる前提。fallback 1\u20133 business days)",
  "shipping_delivery_us": "(settings.json shipping_timing で上書きされる前提。fallback 6\u201310 business days typical)",
  "shipping_packaging": "Double-boxed \u00b7 bubble-wrapped \u00b7 waterproof liner",
  "shipping_notes": "商品個別の発送注意事項。無ければ空文字",
  "product_name": "英語タイトルと同じ"
}

## 英語タイトル SEO ルール

1. **80字厳守**。オーバーすると eBay が切り詰める。
2. **重要語順**: Brand → Model/Type → Key Feature → Condition hint → Color
   例: "Sony WH-1000XM5 Wireless Noise Cancelling Headphones Black Tested"
3. **禁止語**: 絵文字、「!!」、「100% Genuine」、誇大広告
4. **型番は正確**: 仕入先の型番を型崩れなく反映 (例: "WH-1000XM5" の hyphen 位置厳守)
5. **Brand トレードマーク**: 大文字小文字は正式表記 (KEYENCE / Omron / Canon など)
6. **ランク表記はタイトルに入れない** (80字圧迫防止、description で示す)

## 日本語 → 英語変換ルール

- 「美品」→ "Excellent Condition" (まれに "Mint Condition" 可)
- 「動作確認済」→ "Tested Working"
- 「付属品」→ "Accessories"
- 「箱あり」→ "Original Box Included"
- 「本体のみ」→ "Body Only"
- 「ジャンク」→ "As-Is" (titleには入れない方が無難、descriptionで補足)
- 機種依存文字や環境依存文字は除去

## Item Specifics 生成ルール (2026-04-22 改訂: 実 eBay エラーから学習)

### 値の制約

- **各 value は 65 文字以内** (eBay 制約。超過で VerifyAdd 失敗)
  特に "Seller Notes" は Claude が長文を返しがちなので注意
- **"Unknown" "N/A" "-" "Not specified" 等の placeholder 値は禁止** —
  eBay は required フィールドに placeholder を渡すと missing 扱いで reject する。
  **推定できない場合は、その field 自体を出力しない** (key 除外)
- 値は英語。例外: Brand は元言語 (Sony / KEYENCE 等の正式表記)

### Keys 選定

- **参考 listing の Keys 指定ありの場合**: そのキー配列を参照するが、
  仕入先情報から **正の確証** が持てる field だけ埋める。不確実なら key ごと省略。
  ただし "Brand" は最低限 "Unbranded" (正式な eBay 値) は OK。
- **参考 listing なしの場合**: 最低限 "Brand" + "Model" + "Type" を含める。
  "Condition" は ConditionID で別管理なので item_specifics には入れない。

### field ごとの注意

| Field | 注意 |
|---|---|
| Brand | 正式綴り (Sony, KEYENCE, OPSODIS 等)。Unbranded は OK |
| Model | 型番そのまま (WH-1000XM5 等) |
| Connectivity | Bluetooth/Wired/Wireless/HDMI/Composite 等の具体値のみ。Unknown 禁止 |
| Color | 単語 1-2 (Black / Silver 等) |
| Seller Notes | **最長 65 字厳守**。短く簡潔に。長文は description に書く |
| MPN | manufacturer part number、正確な型番 |
| Type | 商品種別 (Headphones / Speaker 等)、具体値のみ |

### 🚫 絶対禁止 Keys (原産国・製造者系、2026-07-04 追加)

**Country of Origin / Country/Region of Manufacture / Country of Manufacture /
Manufacturer は item_specifics に絶対出力しない** (大文字小文字表記ゆれ含め
一切禁止)。参考 listing の Item Specifics Keys にこれらが含まれていても、
**Keys 完全一致の指示より本禁止が優先する**。理由: eBay 出品文に原産国情報を
含めると US Customs が原産国を再計算する根拠を与え関税リスクに直結する
(tools/ebay-manager/CLAUDE.md「Country of Origin / Manufacturer の layer 分離」)。
description / condition_description と同格の絶対ガードとして扱うこと。

## Quick Notes ルール (テンプレ正源の rank_code 別仕様)

- **N/S**: 簡潔に状態確認範囲を記述
- **A/B/C/D**: 動作確認項目のリスト化
  "Tested and confirmed working (YYYY-MM)." + 「Power / Audio / Bluetooth 等」
- **PO**: "Powered on successfully, but full function not verified.
  Other operations (audio/data/Bluetooth) NOT tested."
- **As-Is**: 理由必須。"No AC adapter available for testing" / "For parts" 等

## Condition Description ルール (eBay ConditionDescription 用、65字以内、2026-07-04 追加)

`condition_description` は eBay の ConditionDescription フィールド (買い手に表示される
コンディション説明) に直接反映される **短い要約** です。quick_notes とは役割が異なります。

- **condition_description はランクの要約のみ**。65字以内・英語。
  - 例 (A): "Tested and fully working (2026-07). Minor cosmetic wear."
  - 例 (PO): "Powered on, but full function not verified."
  - 例 (As-Is): "As-Is — No AC adapter for testing" (形式: `As-Is — <reason>`)
- **付属品欠品・傷の位置・詳細な使用感などの細かい情報は condition_description に
  書かない**。それらは quick_notes / includes_items / description 本文へ記載する。
- 原産国 (Country of Origin/Manufacture) や Manufacturer に触れる語は一切含めない
  (eBay ポリシー違反、CLAUDE.md「Country of Origin / Manufacturer の layer 分離」)。
- 65字を超える場合は要約し直す (収まらない情報は description へ)。65字超過分は
  呼出側で機械的に truncate されるため、途中で意味が切れないよう先に短く書くこと。

## Category 候補提示ルール (参考 listing なし時)

- 3件の候補を category_candidates に格納
- 各候補に reasoning (日本語で50字程度) を付与
- 最もフィット度が高いと判断した候補を category_id / category_name にも同時反映

### 🚨 CRITICAL: Leaf Category Only (2026-04-22 強化)

eBay は **Leaf Category (末端サブカテゴリ)** にしか出品できない。親カテゴリ (非 leaf)
を返すと AddItem が "The category selected is not a leaf category" エラーで失敗する。

- **絶対に返してはいけない (非 leaf の代表例)**:
  - `293` (Consumer Electronics root), `625` (Cameras & Photo root),
    `12576` (Business & Industrial root), `58058` (Computers/Tablets),
    `11232` (Video Games root), `14950` (Cell Phones root),
    `260010` (Vehicle Parts root)
- **正しい選択**: 例えば変圧器 (voltage converter/transformer) なら
  `44940` (Business & Industrial > Electrical Equipment > Transformers) ではなく、
  その下の leaf (例 `42257` Industrial Power Transformers 等) を選ぶ。
- 自信がない場合でも **必ずサブカテゴリまで掘り下げた末端 ID** を返す。
  Taxonomy に存在しないかもしれない ID でも、親を返すより末端推定を返した方が UI で
  ユーザーが修正しやすい。
- `category_candidates` の3件も全て leaf であること。

## includes_items と specs の行数ガイド

- includes_items: 2〜6行推奨。1行は簡潔に。
- specs: 5〜10行推奨。key は英語大文字短語 (Brand/Model/Type/Connectivity/...)
- spec_strip: Gadget Mode 時のみ 3行。測定値 (BATTERY / NOISE CANCELLING / RANGE 等)

## 安全側原則

- タイトルで商品を過大表現しない (VeRO/Defect 対策)
- 「100% working」「Brand new」等は、入力で確証ある場合のみ
- **item_specifics の不明項目は key ごと省略する (field 自体を出力しない)**。
  "Unknown" / "N/A" / "-" / 空文字で埋めることは禁止 —
  eBay が required 扱いで reject し VerifyAdd が失敗する (2026-04-22 ユーザー実例)。
- Brand だけは例外的に "Unbranded" (eBay 公認値) で埋めてよい
"""


def _get_client() -> Optional["anthropic.Anthropic"]:
    """Anthropic クライアント取得。API キー未設定時は None。"""
    if not _ANTHROPIC_OK:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_json(text: str) -> Optional[str]:
    """Claude 出力から JSON 文字列候補を抽出 (claude_evaluator と同ロジック)。"""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    open_brace = re.search(r'\{[\s\S]*$', text)
    if open_brace:
        return open_brace.group(0).rstrip() + "}"
    return None


def _compose_user_prompt(product, reference, rank, extra_instructions: Optional[str] = None) -> str:
    """DYNAMIC プロンプト部を構築。

    Args:
        product: ScrapedProduct 風 (duck typed)
        reference: ReferenceListing 風 or None
        rank: RankClassification
        extra_instructions: 出品者が手入力した「必ず入れたい文言/方針」。
            None/空なら無視。指定時は description に自然に反映するよう Claude に指示。
    """
    lines: list[str] = []
    lines.append("## 仕入先商品情報")
    lines.append(f"- Platform: {getattr(product, 'platform', '') or '(unknown)'}")
    lines.append(f"- URL: {getattr(product, 'url', '') or ''}")
    lines.append(f"- Title (JP): {(getattr(product, 'title_ja', None) or '(unknown)')[:300]}")
    if getattr(product, "price_jpy", None) is not None:
        lines.append(f"- Price: {product.price_jpy} JPY")
    if getattr(product, "condition_ja", None):
        lines.append(f"- Condition (JP): {product.condition_ja[:200]}")
    if getattr(product, "includes_ja", None):
        lines.append(f"- Includes (JP): {product.includes_ja[:300]}")
    if getattr(product, "weight_hint_g", None):
        lines.append(f"- Weight hint: {product.weight_hint_g} g")
    if getattr(product, "description_ja", None):
        desc = product.description_ja[:3000]
        lines.append(f"- Description (JP):\n{desc}")

    lines.append("")
    lines.append("## 8段階ランク判定結果 (rank_classifier から)")
    lines.append(f"- Rank: {rank.rank_code} ({rank.rank_label})")
    lines.append(f"- JP Hint: {rank.rank_jp}")
    lines.append(f"- eBay Condition ID: {rank.ebay_condition_id}")
    lines.append(f"- Confidence: {rank.confidence}")
    lines.append(f"- Reasoning: {rank.reasoning}")

    lines.append("")
    if reference is not None and getattr(reference, "category_id", None):
        lines.append("## 参考 eBay Listing (コピー可能な構造のみ)")
        lines.append(f"- 採用 CategoryID: {reference.category_id}")
        if getattr(reference, "category_name", None):
            lines.append(f"- Category Name: {reference.category_name}")
        keys = getattr(reference, "item_specifics_keys", None) or []
        if keys:
            lines.append(f"- Item Specifics Keys (必須埋込): {json.dumps(list(keys), ensure_ascii=False)}")
        if getattr(reference, "title_sample", None):
            lines.append(f"- Title Sample (SEO分析参考のみ、コピー禁止): {reference.title_sample}")
        lines.append(
            "\n**重要**: 上記 CategoryID をそのまま category_id に採用し、\n"
            "Item Specifics Keys の配列に完全に一致するキーで item_specifics を返すこと。\n"
            "ただし Country of Origin / Country/Region of Manufacture / "
            "Country of Manufacture / Manufacturer が Keys に含まれていても "
            "**絶対に item_specifics へ出力しない** (Keys 完全一致の指示より "
            "絶対禁止 Keys ルールが優先する、上記「🚫 絶対禁止 Keys」参照)。\n"
            "Description / Images / Price は参考 listing からコピーしない。"
        )
    else:
        lines.append("## 参考 eBay Listing: なし")
        lines.append(
            "category_candidates に 3 件候補を提案し、最も適切なものを "
            "category_id / category_name にも反映すること。"
        )

    if extra_instructions and extra_instructions.strip():
        lines.append("")
        lines.append("## 出品者からの追加指示（description に最優先で自然に反映）")
        lines.append(extra_instructions.strip()[:1500])
        lines.append(
            "↑ これは出品者が必ず description に含めたい文言・方針です。意味を理解し、"
            "eBay buyer 向けの自然な英語 description に適切に組み込むこと "
            "(そのままコピペでなく文脈に溶け込ませる)。ただし商品事実と矛盾する内容、"
            "および eBay ポリシー違反 (Country of Origin / Country of Manufacture / "
            "Manufacturer の記載) は反映せず無視すること。"
        )

    lines.append("")
    lines.append("上記を JSON で生成してください。")
    return "\n".join(lines)


# =========================================================================
# Placeholder values 組立 (Claude JSON → 14種 placeholder map)
# =========================================================================

def _resolve_shipping_timing(config: Optional[dict], in_stock: bool) -> tuple[str, str]:
    """settings.json の shipping_timing セクションから、実 shipping policy に沿った
    (handling_label, delivery_label) を返す。

    未設定時は Claude 生成側のデフォルトに倒せるよう空文字を返す
    (_compose_placeholder_values 側で Claude の値/フォールバック文言が使われる)。

    Args:
        config: settings.json 全体の dict (shipping_timing キーを参照)
        in_stock: 出品者側の在庫状況。True なら in_stock 系、False なら out_of_stock 系。

    Returns:
        (handling_label, delivery_label) タプル。設定なしなら ("", "")。
    """
    if not isinstance(config, dict):
        return ("", "")
    block = config.get("shipping_timing")
    if not isinstance(block, dict):
        return ("", "")
    key = "in_stock" if in_stock else "out_of_stock"
    tier = block.get(key)
    if not isinstance(tier, dict):
        return ("", "")
    handling = str(tier.get("handling_label") or "").strip()
    delivery = str(tier.get("delivery_label") or "").strip()
    return (handling, delivery)


def _resolve_shipping_carrier(config: Optional[dict], in_stock: bool) -> str:
    """settings.json の shipping_timing セクションから carrier_label を解決.

    W157 fix (2026-05-22 PM): 旧コードは _compose_placeholder_values 内で
    "DHL SpeedPAK · tracked, insured" を hardcode → user 業務では FedEx も使う
    ため誤表示. settings.json shipping_timing.{in_stock|out_of_stock}.carrier_label
    が設定されていれば優先, 未設定なら Claude 生成値 → ("") を返し
    _compose_placeholder_values 側 fallback が使われる.

    Returns:
        carrier_label string. 未設定なら "" (caller 側 fallback に委譲).
    """
    if not isinstance(config, dict):
        return ""
    block = config.get("shipping_timing")
    if not isinstance(block, dict):
        return ""
    key = "in_stock" if in_stock else "out_of_stock"
    tier = block.get(key)
    if not isinstance(tier, dict):
        return ""
    return str(tier.get("carrier_label") or "").strip()


def _compose_placeholder_values(
    claude_data: dict,
    rank,
    shipping_override: Optional[tuple[str, str]] = None,
    carrier_override: str = "",
) -> dict[str, str]:
    """Claude が返した構造化 JSON を 14種 placeholder map に変換する。

    rank は rank_classifier.RankClassification (rank_code / rank_label / rank_jp が必要)。

    HTML escape は build_* ヘルパが行う箇所 / ここで適用する箇所を分離:
      - spec/inc/strip は build_* ヘルパ内で escape 済みの HTML 断片
      - その他 (product_name / product_sub / quick_notes / shipping_*) は
        ここで個別に escape
    """
    def _s(v: Any, max_len: int = 500) -> str:
        return str(v or "")[:max_len]

    def _esc(v: Any, max_len: int = 500) -> str:
        return _html_escape(_s(v, max_len))

    includes_items = claude_data.get("includes_items") or []
    specs = claude_data.get("specs") or []
    spec_strip = claude_data.get("spec_strip") or []

    values: dict[str, str] = {
        # masthead
        "product_name": _esc(
            claude_data.get("product_name") or claude_data.get("title"),
            max_len=200,
        ),
        "product_sub": _esc(claude_data.get("product_sub"), max_len=150),

        # rank block
        "rank": _esc(rank.rank_code, max_len=20),
        "rank_label": _esc(rank.rank_label, max_len=60),
        "rank_jp": _esc(rank.rank_jp, max_len=120),
        "quick_notes": _esc(claude_data.get("quick_notes"), max_len=1500),

        # includes / specs / spec_strip (build_* が escape 済)
        "includes_rows": build_includes_rows(includes_items),
        "specs_rows": build_specs_rows(specs),
        "spec_strip_rows": build_spec_strip_rows(spec_strip),

        # shipping block
        "shipping_origin": _esc(
            claude_data.get("shipping_origin") or "Tokyo, Japan", 80,
        ),
        # W157 fix (2026-05-22 PM): carrier_override \u3092\u6700\u512a\u5148. settings.json
        # shipping_timing.{tier}.carrier_label \u3067\u300cFedEx / DHL\u300d\u4e21\u5bfe\u5fdc\u6587\u8a00\u3092
        # user \u304c\u8a2d\u5b9a\u53ef\u80fd\u5316. fallback "DHL SpeedPAK" \u56fa\u5b9a\u306f\u30d0\u30b0 (user \u306f FedEx \u3082\u4f7f\u3046).
        "shipping_carrier": _esc(
            (carrier_override.strip() if carrier_override and carrier_override.strip()
             else claude_data.get("shipping_carrier")
             or "FedEx International Priority / DHL SpeedPAK \u00b7 tracked, insured"),
            120,
        ),
        # 2026-04-22: 実 shipping_policy (in_stock/out_of_stock) に合わせた日付を
        # settings.json から override できるように。Claude のデフォルト文言は
        # fallback (未設定時) としてのみ使う。
        "shipping_handling": _esc(
            (shipping_override[0] if shipping_override and shipping_override[0]
             else claude_data.get("shipping_handling") or "1\u20133 business days"),
            60,
        ),
        "shipping_delivery_us": _esc(
            (shipping_override[1] if shipping_override and shipping_override[1]
             else claude_data.get("shipping_delivery_us") or "6\u201310 business days typical"),
            80,
        ),
        "shipping_packaging": _esc(
            claude_data.get("shipping_packaging")
            or "Double-boxed \u00b7 bubble-wrapped \u00b7 waterproof liner",
            120,
        ),
        "shipping_notes": _esc(claude_data.get("shipping_notes"), max_len=500),

        # mode_class (detect_mode で上書きされる)
        "mode_class": "default",
    }
    return values


# =========================================================================
# 公開 API
# =========================================================================

def generate_listing(
    product,
    reference,
    rank,
    template_body: str,
    template_md_path: Optional[Path] = None,
    *,
    in_stock: bool = False,
    config: Optional[dict] = None,
    extra_instructions: Optional[str] = None,
) -> GeneratedListing:
    """仕入先商品 + 参考 listing + rank → eBay 出品データ生成。

    Args:
        product: ScrapedProduct (monitor.supplier_scraper から)
        reference: Optional[ReferenceListing] (monitor.ebay_reference_fetcher から)
        rank: RankClassification (monitor.rank_classifier から)
        template_body: v4 HTML テンプレ本文 (listing-description-template.md の
                       HTML ブロックそのもの。description_templates テーブルに
                       保存された値を渡す)
        template_md_path: 未使用 (将来の差分キャッシュ用予約引数)
        in_stock: 在庫ありフラグ (Step 3 の「即時出荷」チェック)。keyword-only。
            True なら in_stock 系、False なら out_of_stock 系の shipping_timing を
            description に反映。デフォルト False (無在庫出品が業務デフォルトのため)。
        config: settings.json 全体 dict。keyword-only。shipping_timing セクションを
            参照して handling_label / delivery_label を取得する。未指定なら Claude 生成
            デフォルト ('1-3 business days' / '6-10 business days typical') が使われる。
        extra_instructions: 出品者が手入力した「必ず入れたい文言/方針」(keyword-only)。
            None/空なら従来挙動。指定時は Claude が意味を理解し description に自然反映
            (eBay ポリシー違反 [Country of Origin 等] は無視)。

    Returns:
        GeneratedListing (Claude 失敗時は generate_error に詳細)
    """
    result = GeneratedListing()

    # 入力防御
    if product is None:
        result.generate_error = "product is None"
        return result
    if rank is None:
        result.generate_error = "rank is None"
        return result
    if not template_body:
        result.generate_error = "template_body is empty"
        return result

    client = _get_client()
    if not client:
        result.generate_error = "ANTHROPIC_API_KEY not set or anthropic package missing"
        logger.warning(result.generate_error)
        return result

    user_prompt = _compose_user_prompt(product, reference, rank, extra_instructions)

    from monitor.api_logger import log_anthropic_response, _Timer
    from monitor.claude_evaluator import _supports_effort

    msg = None
    try:
        with _Timer() as t:
            _extra_kwargs: dict = (
                {"output_config": {"effort": "medium"}} if _supports_effort(CLAUDE_MODEL) else {}
            )
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4000,
                **_extra_kwargs,
                system=[
                    {
                        "type": "text",
                        "text": _STABLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
        log_anthropic_response(
            "listing_generate", CLAUDE_MODEL, msg,
            duration_ms=t.duration_ms, success=True,
        )
    except anthropic.APIError as e:
        logger.warning(f"listing_generate API error: {e}")
        log_anthropic_response(
            "listing_generate", CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        result.generate_error = f"api_error: {e}"
        return result
    except Exception as e:  # noqa: BLE001 — UI を絶対に止めない
        logger.warning(f"listing_generate unexpected: {e}")
        log_anthropic_response(
            "listing_generate", CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        result.generate_error = f"unexpected: {e}"
        return result

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _extract_json(text)
    if not cand:
        result.generate_error = f"no JSON in response (head: {text[:120]!r})"
        logger.warning(result.generate_error)
        return result

    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        result.generate_error = f"JSON decode: {e}, raw={cand[:160]!r}"
        logger.warning(result.generate_error)
        return result

    # --- フィールド抽出 ---
    title = str(data.get("title", "")).strip()[:80]
    result.ebay_title = title

    # #44 (2026-07-04): condition_description (65字以内、ランク要約のみ)
    result.condition_description = str(data.get("condition_description", "")).strip()[:65]

    # category
    cat_id = data.get("category_id")
    if cat_id is not None:
        result.ebay_category_id = str(cat_id).strip() or None
    cat_name = data.get("category_name")
    if cat_name is not None:
        result.ebay_category_name = str(cat_name).strip() or None

    # 旧 reference override は削除 (Taxonomy v2 が reference の扱いも包含)

    # 2026-04-22 v2: eBay Taxonomy API で実際の有効 leaf カテゴリを **無条件で** 検証する。
    # v1 (条件付き) では reference.category_id の扱いが複雑で、LD-S9 等のケースで
    # Taxonomy が呼ばれずユーザーが「The category is not valid」を踏むバグが残っていた。
    # v2 設計:
    #   1. タイトルで Taxonomy API を常に引く
    #   2. 参考 listing の category_id が存在 かつ Taxonomy 結果に含まれる → それを採用
    #   3. Claude の category_id が Taxonomy 結果に含まれる → Claude を採用
    #   4. いずれも Taxonomy 結果に無い → Taxonomy 先頭 (eBay 推奨スコア順) に強制上書き
    #   5. Taxonomy API 失敗時のみ Claude の値を残す
    # print() でも出力して Streamlit ログに確実に痕跡を残す (logger と二重)。
    ref_cat_id = (
        str(getattr(reference, "category_id", "") or "").strip()
        if reference is not None else ""
    )
    try:
        from monitor.ebay_taxonomy import get_category_suggestions
        query_for_taxonomy = (title or "").strip()
        taxonomy_suggestions = get_category_suggestions(
            query_for_taxonomy, config=config, max_results=5,
        ) if query_for_taxonomy else []
        _tx_ids = [s["category_id"] for s in taxonomy_suggestions]
        _tx_msg = (
            f"[Taxonomy v2] q={query_for_taxonomy[:80]!r} "
            f"claude={result.ebay_category_id!r} reference={ref_cat_id!r} "
            f"taxonomy={_tx_ids}"
        )
        logger.warning(_tx_msg)  # warning にして確実にログへ
        _safe_stderr_print(_tx_msg)

        if taxonomy_suggestions:
            claude_cat_id = str(result.ebay_category_id or "").strip()
            chosen = None
            pick_reason = ""
            # Priority 1: reference の ID が Taxonomy に含まれていれば採用
            if ref_cat_id:
                for s in taxonomy_suggestions:
                    if s["category_id"] == ref_cat_id:
                        chosen = s
                        pick_reason = "reference match"
                        break
            # Priority 2: Claude の ID が Taxonomy に含まれていれば採用
            if chosen is None and claude_cat_id:
                for s in taxonomy_suggestions:
                    if s["category_id"] == claude_cat_id:
                        chosen = s
                        pick_reason = "claude match"
                        break
            # Priority 3: Taxonomy 先頭 (eBay 推奨スコア順)
            if chosen is None:
                chosen = taxonomy_suggestions[0]
                pick_reason = "taxonomy top (claude/ref were invalid)"

            _override_msg = (
                f"[Taxonomy v2] chose {chosen['category_id']} "
                f"({chosen['category_name']}) — reason={pick_reason}"
            )
            logger.warning(_override_msg)
            _safe_stderr_print(_override_msg)

            result.ebay_category_id = chosen["category_id"]
            result.ebay_category_name = chosen["category_name"] or result.ebay_category_name
            # category_candidates を Taxonomy の全候補で上書き
            result.category_candidates = [
                {
                    "category_id": s["category_id"],
                    "category_name": (
                        " > ".join(s["ancestors_names"][::-1])
                        + (" > " if s["ancestors_names"] else "")
                        + s["category_name"]
                    ),
                    "reasoning": "eBay Taxonomy API 推奨 (有効 leaf)",
                }
                for s in taxonomy_suggestions
            ]
        else:
            # Taxonomy 結果ゼロ → reference があれば尊重、無ければ Claude 値のまま
            if ref_cat_id:
                result.ebay_category_id = ref_cat_id
                if getattr(reference, "category_name", None):
                    result.ebay_category_name = reference.category_name
    except Exception as e:  # noqa: BLE001 — Claude 推定値にフォールバック
        _err_msg = f"[Taxonomy v2] API 呼出失敗 (Claude/reference 値を使用): {e!r}"
        logger.warning(_err_msg)
        _safe_stderr_print(_err_msg)
        # reference 優先
        if ref_cat_id and result.ebay_category_id != ref_cat_id:
            result.ebay_category_id = ref_cat_id
            if getattr(reference, "category_name", None):
                result.ebay_category_name = reference.category_name

    # item_specifics
    # #44 (2026-07-04) 原産国混入チェーン封鎖 (3点封鎖の2、generator パース層):
    # プロンプト guard (「🚫 絶対禁止 Keys」) だけでは LLM が確実に守る保証がない
    # ため、parse 結果からも Country of Origin / Country/Region of Manufacture /
    # Country of Manufacture / Manufacturer を機械的に除外する (G2 の
    # revise_item_specifics と同一の禁止 Name 集合を共有 import、多層防御)。
    # 除外は Q0 (silent skip 禁止) のため logger.warning で痕跡を残す。
    from monitor.ebay_client import _is_forbidden_specific_name

    raw_specifics = data.get("item_specifics") or {}
    if isinstance(raw_specifics, dict):
        result.item_specifics = {}
        for k, v in raw_specifics.items():
            name = str(k).strip()
            if not name:
                continue
            if _is_forbidden_specific_name(name):
                logger.warning(
                    "generate_listing: 禁止 item_specifics Name '%s' を除外 "
                    "(原産国/Manufacturer 系、CLAUDE.md 規約)", name,
                )
                continue
            result.item_specifics[name] = str(v).strip()

    # category_candidates (参考 listing なし時のみ意味を持つ)
    # 2026-04-22 FIX (code-reviewer HIGH-1): Taxonomy v2 が既に有効 leaf 候補をセット
    # 済みのときは Claude の category_candidates で上書きしない。UI ラジオが無効 ID を
    # 提示して AddItem 失敗 → 金銭損失につながる重大バグ対策。
    _taxonomy_populated = bool(result.category_candidates) and all(
        c.get('reasoning', '').startswith('eBay Taxonomy API')
        for c in result.category_candidates
    )
    if not _taxonomy_populated:
        raw_cands = data.get("category_candidates") or []
        if isinstance(raw_cands, list):
            cleaned: list[dict] = []
            for c in raw_cands:
                if not isinstance(c, dict):
                    continue
                cleaned.append({
                    "category_id": str(c.get("category_id", "")).strip(),
                    "category_name": str(c.get("category_name", "")).strip(),
                    "reasoning": str(c.get("reasoning", "")).strip()[:200],
                })
            result.category_candidates = cleaned

    # --- Gadget Mode 判定 ---
    brand = ""
    if result.item_specifics:
        brand = result.item_specifics.get("Brand", "") or result.item_specifics.get("brand", "")
    specs_list = data.get("specs") or []
    try:
        specs_count = len(specs_list) if isinstance(specs_list, list) else 0
    except TypeError:
        specs_count = 0
    mode_class = detect_mode(result.ebay_category_id, brand, specs_count)
    result.mode_class = mode_class

    # --- Placeholder values 組立 & render ---
    shipping_override = _resolve_shipping_timing(config, in_stock)
    carrier_override = _resolve_shipping_carrier(config, in_stock)  # W157
    values = _compose_placeholder_values(
        data, rank,
        shipping_override=shipping_override,
        carrier_override=carrier_override,
    )
    values["mode_class"] = mode_class

    try:
        result.ebay_description = render_description(template_body, values)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"render_description failed: {e}")
        result.generate_error = f"render_description: {e}"
        # title/category 等は保持したまま返す
        return result

    return result


if __name__ == "__main__":
    import json as _json

    # 手動テスト用ダミー
    class _P:
        url = "https://jp.mercari.com/item/m1"
        platform = "mercari"
        title_ja = "Sony WH-1000XM5 ブラック 美品"
        price_jpy = 32000
        condition_ja = "中古 美品"
        includes_ja = "箱あり、説明書あり"
        image_urls: list[str] = []
        description_ja = "動作確認済。使用に伴う小キズあり。"
        weight_hint_g = 500

    class _R:
        category_id = "293"
        category_name = "Consumer Electronics"
        item_specifics_keys = ["Brand", "Model", "Type", "Color", "Connectivity"]
        title_sample = "Sony WH-1000XM5 Wireless Headphones"
        condition_id = "3000"

    # rank_classifier を使わずハードコード (依存を減らす)
    class _Rank:
        rank_code = "A"
        rank_label = "Excellent"
        rank_jp = "Tested \u00b7 Minor Wear"
        ebay_condition_id = "3000"
        confidence = 0.9
        reasoning = "美品表記 + 動作確認済"

    tpl = "<div class=\"mh-wrap {{mode_class}}\"><h1>{{product_name}}</h1><p>{{quick_notes}}</p></div>"
    gl = generate_listing(_P(), _R(), _Rank(), tpl)
    print(_json.dumps({
        "ebay_title": gl.ebay_title,
        "mode_class": gl.mode_class,
        "category_id": gl.ebay_category_id,
        "specifics_count": len(gl.item_specifics),
        "condition_description": gl.condition_description,
        "generate_error": gl.generate_error,
        "description_head": gl.ebay_description[:200],
    }, ensure_ascii=False, indent=2))
