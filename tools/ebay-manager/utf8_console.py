"""
UTF-8コンソール設定ヘルパー（Windows文字化け対策）

使い方:
    import utf8_console   # ファイル冒頭で import するだけ

効果:
    - sys.stdout / sys.stderr を UTF-8 で再構成
    - 子プロセスにも PYTHONIOENCODING=utf-8 / PYTHONUTF8=1 を継承させる
    - Windows PowerShell/CMD の CP932 既定を抑止
"""
from __future__ import annotations

import os
import sys


def configure() -> None:
    # 現プロセスのstdout/stderrをUTF-8へ
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # 子プロセスにも伝播
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")


# import時点で自動適用
configure()
