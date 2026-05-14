#!/bin/bash
# SessionStart hook: 直近編集ファイル一覧表示で文脈復元支援 (W2-D10-S2b)
# Boris Tip 18 文脈管理 / R-7 関連
#
# 出力: stdout に Claude が JSON 形式の {systemMessage: "..."} を期待
# 既存 settings.json の `echo '{...}'` 1 行版を置き換え、recent activity を追加
# Windows Git Bash で find / stat 両方動作確認済 (W2-D10-S2a Test A-D)
# Python fallback は failures 時に発火

set -u
ROOT_DIR="${CLAUDE_PROJECT_DIR:-C:/Users/gucch/projects/claude}"
# W126 migration 後対応: hash を path から動的計算
PROJECT_HASH=$(echo "$ROOT_DIR" | sed 's/:/-/g' | sed 's/[\\/]/-/g')
MEM_DIR="$HOME/.claude/projects/${PROJECT_HASH}/memory"

# 直近 60 分以内編集 memory file (最大 5 件)
RECENT_MEMORY=""
if [ -d "$MEM_DIR" ]; then
    RECENT_MEMORY=$(find "$MEM_DIR" -name '*.md' -mmin -60 2>/dev/null | head -5 | while read f; do basename "$f"; done | tr '\n' ' ')
fi

# 直近 24 時間以内編集 .claude/rules/ (最大 5 件)
RECENT_RULES=""
if [ -d "$ROOT_DIR/.claude/rules" ]; then
    RECENT_RULES=$(find "$ROOT_DIR/.claude/rules" -name '*.md' -mmin -1440 2>/dev/null | head -5 | while read f; do basename "$f"; done | tr '\n' ' ')
fi

# 直近 24 時間以内編集 .claude/hooks/ (最大 5 件)
RECENT_HOOKS=""
if [ -d "$ROOT_DIR/.claude/hooks" ]; then
    RECENT_HOOKS=$(find "$ROOT_DIR/.claude/hooks" -name '*.sh' -mmin -1440 2>/dev/null | head -5 | while read f; do basename "$f"; done | tr '\n' ' ')
fi

# fallback: find 失敗時 (Windows Git Bash 環境差異対策) は Python で代替
if [ -z "$RECENT_MEMORY" ] && [ -z "$RECENT_RULES" ] && [ -z "$RECENT_HOOKS" ]; then
    FALLBACK=$(python -c "
import os, time, pathlib
root = pathlib.Path(os.environ.get('CLAUDE_PROJECT_DIR', 'C:/Users/gucch/projects/claude'))
# W126 migration 後対応: hash を path から動的計算
import re
project_hash = re.sub(r'[:\\\\/]', '-', str(root))
mem = pathlib.Path.home() / f'.claude/projects/{project_hash}/memory'
now = time.time()
out = []
for label, path, ext, sec in [
    ('memory', mem, '*.md', 3600),
    ('rules', root / '.claude/rules', '*.md', 86400),
    ('hooks', root / '.claude/hooks', '*.sh', 86400),
]:
    try:
        files = sorted(path.glob(ext), key=lambda p: -p.stat().st_mtime)
        recent = [p.name for p in files if now - p.stat().st_mtime < sec][:5]
        if recent:
            out.append(f'{label}: {\" \".join(recent)}')
    except Exception:
        pass
print(' | '.join(out) if out else '')
" 2>/dev/null)
    RECENT_FALLBACK="$FALLBACK"
fi

# systemMessage 構築
MSG="Claude Opus 起動。CLAUDE.md Q0-Q5 + Boris 30 Tips + Karpathy 4 適用済 (quality-gate / claude-md-discipline hook 稼働中)"

if [ -n "$RECENT_MEMORY" ]; then
    MSG="$MSG | 直近 60 分編集 memory: ${RECENT_MEMORY% }"
fi
if [ -n "$RECENT_RULES" ]; then
    MSG="$MSG | 直近 24h 編集 rules: ${RECENT_RULES% }"
fi
if [ -n "$RECENT_HOOKS" ]; then
    MSG="$MSG | 直近 24h 編集 hooks: ${RECENT_HOOKS% }"
fi
if [ -n "${RECENT_FALLBACK:-}" ]; then
    MSG="$MSG | recent (fallback): $RECENT_FALLBACK"
fi

# JSON 出力 (Python で escape 確実化)
python -c "
import json, sys
print(json.dumps({'systemMessage': sys.argv[1]}, ensure_ascii=False))
" "$MSG"
