#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
機密ファイル (Gmail token 等) の保存先を OneDrive 同期外に隔離.

code-reviewer CRITICAL C-1 指摘対応:
  project dir `C:\\Users\\gucch\\OneDrive\\...` は OneDrive 同期対象のため、
  refresh_token を含む Gmail token を置くと **Microsoft アカウント侵害時に
  gmail.send 含む永続 token が漏洩** する.

  本 module は %LOCALAPPDATA%\\ebay-manager\\ を secure store として使用し、
  Gmail token 等を OneDrive 外に保存する.

  既存の OneDrive 配下 token があれば **自動移行**し、旧ファイルは削除する
  (user がマニュアルで削除する必要なし).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _secure_dir() -> Path:
    """OneDrive 同期対象外のローカル専用ディレクトリ.

    Windows: %LOCALAPPDATA%\\ebay-manager\\
    Linux/Mac: ~/.local/share/ebay-manager/ (将来拡張用)
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / "ebay-manager"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _legacy_config_dir() -> Path:
    """旧配置 (OneDrive 配下 config/). 移行期間中の fallback 参照用."""
    return Path(__file__).resolve().parent.parent / "config"


def get_gmail_token_path() -> Path:
    """Gmail token の secure 保存先を返す.

    既存の OneDrive 配下 token が見つかれば自動的に secure store へ移動し、
    旧ファイルを削除する. ユーザー作業不要.
    """
    secure = _secure_dir() / "gmail_token.json"
    legacy = _legacy_config_dir() / "gmail_token.json"
    legacy_bak = _legacy_config_dir() / "gmail_token.json.bak"

    # 1. secure 側に既にあれば OK
    if secure.exists():
        # 旧ファイルが残っていれば (自動移行失敗 or 後から同期で復活したケース) 削除
        for old in (legacy, legacy_bak):
            if old.exists():
                try:
                    old.unlink()
                    logger.warning(
                        f"Removed stale legacy Gmail token on OneDrive: {old}"
                    )
                except OSError as e:
                    logger.error(f"Failed to remove legacy token {old}: {e}")
        return secure

    # 2. 旧 token が OneDrive 側にあれば secure へ移動 (1 回限りの自動移行)
    if legacy.exists():
        try:
            shutil.move(str(legacy), str(secure))
            logger.warning(
                f"Migrated Gmail token from OneDrive to secure store: "
                f"{legacy} -> {secure}"
            )
        except OSError as e:
            logger.error(f"Gmail token migration failed: {e}")
            # 移行失敗時は legacy を読ませる (後方互換)
            return legacy
        # 旧 .bak も掃除
        if legacy_bak.exists():
            try:
                legacy_bak.unlink()
                logger.warning(f"Removed legacy .bak: {legacy_bak}")
            except OSError as e:
                logger.error(f"Failed to remove {legacy_bak}: {e}")
        return secure

    # 3. 初回: 存在しない secure path を返す (呼び元が OAuth flow で作成)
    return secure


def get_gmail_credentials_path() -> Path:
    """OAuth client credentials (credentials.json) は公開鍵相当なので
    project config/ 配下のまま OK. 別 helper にして使用箇所を統一.
    """
    return _legacy_config_dir() / "credentials.json"


__all__ = [
    "get_gmail_token_path",
    "get_gmail_credentials_path",
]
