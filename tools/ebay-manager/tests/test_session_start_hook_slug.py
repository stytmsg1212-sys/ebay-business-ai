"""W127/W128 (2026-05-14/15) SessionStart hook unix-style path slug regression test.

2026-05-14 W126 OneDrive→projects/claude 移行後、5/14-15 で SessionStart hook の
PROJECT_HASH 計算が Git Bash unix-style /c/Users/... を slug 崩壊 (-c-Users-) させ
_NEXT_SESSION.md auto-load 失敗が 2 回連続再発した. 本 test は同事象の機械的再発防止.

検証する slug 化ロジック (.claude/hooks/session-start-load-incantation.sh L31-39):
- /c/Users/... (Git Bash unix-style) → C--Users-... (正常化)
- C:/Users/... (Windows-style forward slash) → C--Users-... (passthrough)
- C:\\Users\\... (Windows-style backslash) → C--Users-... (passthrough)
- /C/Users/... (大文字 drive) → C--Users-... (大文字保持)
- /cygdrive/c/Users/... (Cygwin) → silent corrupt しない (H-4 修正)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "slug_extract.sh"


def _run_slug(env_value: str) -> str:
    """fixture script (hook 本体と同一 logic) を呼んで PROJECT_HASH を返す.

    bash -c inline 方式は `${BASH_REMATCH[N]}` parameter expansion が parser に
    認識されない bug があるため fixture file 経由 (shebang 動作) に統一.
    """
    out = subprocess.run(
        ['bash', str(FIXTURE), env_value],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def test_unix_style_lowercase_drive_normalized():
    """Git Bash unix-style /c/Users/... が C--Users-... に正常化される (W128 修正の核心)."""
    assert _run_slug("/c/Users/gucch/projects/claude") == "C--Users-gucch-projects-claude"


def test_unix_style_uppercase_drive_preserved():
    """大文字 drive /C/Users/... も同 slug を返す."""
    assert _run_slug("/C/Users/gucch/projects/claude") == "C--Users-gucch-projects-claude"


def test_windows_style_forward_slash_passthrough():
    """Windows-style forward slash C:/Users/... も同 slug."""
    assert _run_slug("C:/Users/gucch/projects/claude") == "C--Users-gucch-projects-claude"


def test_windows_style_backslash_passthrough():
    r"""Windows-style backslash C:\Users\... も同 slug."""
    assert _run_slug(r"C:\Users\gucch\projects\claude") == "C--Users-gucch-projects-claude"


def test_cygdrive_excluded_no_silent_corrupt():
    """H-4: /cygdrive/c/Users/... が silently corrupted slug を返さない.

    修正前 regex `^/([a-zA-Z])/(.*)$` は cygdrive にも match して "C--ygdrive-c-..."
    という不正 slug を返した. 修正後は ^/cygdrive/ を明示除外し、元 path ベースの
    slug (cygdrive- prefix 含む) になる. C--ygdrive のような corrupt は出ない.
    """
    result = _run_slug("/cygdrive/c/Users/gucch/projects/claude")
    assert "C--ygdrive" not in result, (
        f"Cygdrive silent corruption detected: {result}"
    )
