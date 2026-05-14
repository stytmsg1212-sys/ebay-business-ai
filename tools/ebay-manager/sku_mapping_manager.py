#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SKU → 仕入先URL マッピング規則の管理
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import re

logger = logging.getLogger(__name__)

MAPPINGS_FILE = Path(__file__).parent / "data" / "sku_mappings.json"


# URL から prefix+item_id を逆引きするためのパターン。
# Mercari は URL 側に `m` が付いているが SKU 側ではそれを含めずに数字だけを格納するケースと
# 含めて格納するケースが混在しうる。既存データと整合させるため、URLの `m{digits}` から
# `m` を剥がして item_id とする（pattern: "m{item_id}" 定義と対称）。
_URL_TO_PREFIX_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ebayme_", re.compile(r'mercari\.com/item/m([A-Za-z0-9]+)')),
    ("ebayMS_", re.compile(r'mercari\.com/shops/product/([A-Za-z0-9]+)')),
    ("ebayrm_", re.compile(r'item\.fril\.jp/([A-Za-z0-9]+)')),
    ("ebayPF_", re.compile(r'paypayfleamarket\.yahoo\.co\.jp/item/([A-Za-z0-9]+)')),
    ("ebayyh_", re.compile(r'auctions\.yahoo\.co\.jp/jp/auction/([A-Za-z0-9]+)')),
    ("ebayRT_", re.compile(r'item\.rakuten\.co\.jp/([^/]+)/')),
    ("ebayRB_", re.compile(r'books\.rakuten\.co\.jp/rb/([A-Za-z0-9]+)')),
    ("ebayAM_", re.compile(r'amazon\.co\.jp/(?:[^/]+/)?dp/([A-Z0-9]+)')),
    # ebayh_ は旧プレフィックス。重複するが ebayyh_ で吸収
]


def url_to_sku(url: str) -> Optional[str]:
    """仕入先URLから eBay SKU 形式(prefix + item_id)を生成。

    成功例:
      https://auctions.yahoo.co.jp/jp/auction/x1137149904 → ebayyh_x1137149904
      https://jp.mercari.com/item/m12345                  → ebayme_12345
      https://paypayfleamarket.yahoo.co.jp/item/p9999     → ebayPF_p9999

    非対応ドメインや item_id が抽出できない場合は None。
    """
    if not url:
        return None
    for prefix, pat in _URL_TO_PREFIX_PATTERNS:
        m = pat.search(url)
        if m:
            return prefix + m.group(1)
    return None

# デフォルトマッピング
DEFAULT_MAPPINGS = {
    "ebayme_": {
        "name": "メルカリ",
        "common_url": "https://jp.mercari.com/item/",
        "pattern": "m{item_id}",
        "description": "メルカリフリマアプリ（item_id は数字のみ、URLでは m を前置）"
    },
    "ebayMS_": {
        "name": "メルカリショップ",
        "common_url": "https://jp.mercari.com/shops/product/",
        "pattern": "{item_id}",
        "description": "メルカリの公式ショップ"
    },
    "ebayrm_": {
        "name": "ラクマ",
        "common_url": "https://item.fril.jp/",
        "pattern": "{item_id}",
        "description": "ラクマ（フリル）"
    },
    "ebayPF_": {
        "name": "PayPayフリマ",
        "common_url": "https://paypayfleamarket.yahoo.co.jp/item/",
        "pattern": "{item_id}",
        "description": "PayPayフリマ（Yahoo!フリマ）"
    },
    "ebayh_": {
        "name": "Yahoo Auctions",
        "common_url": "https://page.auctions.yahoo.co.jp/jp/auction/",
        "pattern": "{item_id}",
        "description": "ヤフオク！"
    },
    "ebayyh_": {
        "name": "Yahoo Auctions",
        "common_url": "https://page.auctions.yahoo.co.jp/jp/auction/",
        "pattern": "{item_id}",
        "description": "ヤフオク！（代替プリフィックス）"
    },
    "ebayRT_": {
        "name": "楽天市場",
        "common_url": "https://item.rakuten.co.jp/",
        "pattern": "{item_id}/",
        "description": "楽天市場"
    },
    "ebayRB_": {
        "name": "楽天ブックス",
        "common_url": "https://books.rakuten.co.jp/rb/",
        "pattern": "{item_id}",
        "description": "楽天ブックス"
    },
    "ebayYS_": {
        "name": "Yahoo!ショッピング",
        "common_url": "https://store.shopping.yahoo.co.jp/",
        "pattern": "{item_id}",
        "description": "Yahoo!ショッピング"
    },
    "ebayAM_": {
        "name": "Amazon",
        "common_url": "https://www.amazon.co.jp/dp/",
        "pattern": "{item_id}",
        "description": "Amazon.co.jp"
    },
}


def load_mappings() -> Dict:
    """マッピング規則を読み込む（ファイルまたはデフォルト）"""
    if MAPPINGS_FILE.exists():
        try:
            with open(MAPPINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(
                f"⚠️ マッピングファイル読み込みエラー (DEFAULT_MAPPINGS にフォールバック): {e}",
                exc_info=True,
            )
            return DEFAULT_MAPPINGS.copy()
    return DEFAULT_MAPPINGS.copy()


def save_mappings(mappings: Dict) -> bool:
    """マッピング規則を保存"""
    try:
        MAPPINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MAPPINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(mappings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"❌ マッピング保存エラー: {e}", exc_info=True)
        return False


def add_mapping(prefix: str, name: str, common_url: str, pattern: str,
                description: str = "") -> Tuple[bool, str]:
    """新規マッピングを追加"""
    mappings = load_mappings()

    if prefix in mappings:
        return False, f"プリフィックス '{prefix}' は既に存在します"

    if not prefix or not name or not common_url or not pattern:
        return False, "すべてのフィールドを入力してください"

    mappings[prefix] = {
        "name": name,
        "common_url": common_url,
        "pattern": pattern,
        "description": description
    }

    if save_mappings(mappings):
        return True, f"マッピング '{name}' を追加しました"
    else:
        return False, "保存に失敗しました"


def update_mapping(prefix: str, name: str, common_url: str, pattern: str,
                   description: str = "") -> Tuple[bool, str]:
    """既存マッピングを更新"""
    mappings = load_mappings()

    if prefix not in mappings:
        return False, f"プリフィックス '{prefix}' が見つかりません"

    if not name or not common_url or not pattern:
        return False, "すべてのフィールドを入力してください"

    mappings[prefix] = {
        "name": name,
        "common_url": common_url,
        "pattern": pattern,
        "description": description
    }

    if save_mappings(mappings):
        return True, f"マッピング '{name}' を更新しました"
    else:
        return False, "保存に失敗しました"


def delete_mapping(prefix: str) -> Tuple[bool, str]:
    """マッピングを削除"""
    mappings = load_mappings()

    if prefix not in mappings:
        return False, f"プリフィックス '{prefix}' が見つかりません"

    del mappings[prefix]

    if save_mappings(mappings):
        return True, f"マッピングを削除しました"
    else:
        return False, "削除に失敗しました"


def reset_to_defaults() -> bool:
    """デフォルトマッピングにリセット"""
    return save_mappings(DEFAULT_MAPPINGS.copy())


def generate_url(prefix: str, item_id: str) -> Optional[str]:
    """SKU プリフィックスと item_id から URL を生成"""
    mappings = load_mappings()

    if prefix not in mappings:
        return None

    config = mappings[prefix]
    pattern = config.get("pattern", "{item_id}")
    url_part = pattern.format(item_id=item_id)
    return config.get("common_url", "") + url_part


def validate_sku(sku: str) -> Tuple[bool, Optional[str], Optional[str], str]:
    """SKU を検証して分類
    戻り値: (valid, prefix, item_id, message)
    """
    mappings = load_mappings()

    if not sku:
        return False, None, None, "SKU が空です"

    for prefix in mappings.keys():
        if sku.startswith(prefix):
            item_id = sku[len(prefix):]
            if not item_id:
                return False, prefix, None, f"プリフィックス '{prefix}' の後に item_id がありません"
            return True, prefix, item_id, "有効なSKUです"

    return False, None, None, "対応するプリフィックスが見つかりません"
