#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Launcher - Daily Scheduler 起動スクリプト
Windows Task Scheduler から実行するためのラッパー
"""

import sys
import os
from pathlib import Path

# パスを設定
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# Windows CP932 対策（子プロセスにも伝播）
import utf8_console  # noqa: F401

# ディレクトリ作成
(BASE_DIR / 'logs').mkdir(exist_ok=True)
(BASE_DIR / 'data' / 'backups').mkdir(parents=True, exist_ok=True)

if __name__ == '__main__':
    # daily_scheduler.py を実行
    from daily_scheduler import main
    main()
