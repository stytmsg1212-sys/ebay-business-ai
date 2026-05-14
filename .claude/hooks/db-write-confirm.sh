#!/bin/bash
# DB write confirmation (Q2/Q4 機械強制)
# 入力: stdin に Claude が JSON で {tool_name: "Bash", tool_input: {command}}
# 出力: 違反検出時 exit 2 + stderr
#
# 検出対象:
#  - sqlite3 CLI / python sqlite3 経由の UPDATE / DELETE / DROP / ALTER
#  - data/backups/ 配下に直近 30 分以内の .db.bak* が無ければ BLOCK
#
# 経緯: 2026-04-29 事故 + feedback_post_db_modification_review.md
#   本番 DB 直接書込は backup 必須. 後付け retrospective review (Q4) より事前防止が確実.

set -u
INPUT=$(cat)

CMD=$(python -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    pass
" <<< "$INPUT" 2>/dev/null)

if [ -z "$CMD" ]; then
    exit 0
fi

# 破壊的 SQL 検出
DESTRUCTIVE=0
# sqlite3 CLI 経由
if echo "$CMD" | grep -qiE 'sqlite3[^|;]*"[^"]*\b(UPDATE|DELETE[[:space:]]+FROM|DROP[[:space:]]+TABLE|ALTER[[:space:]]+TABLE)\b'; then
    DESTRUCTIVE=1
fi
# python -c で sqlite3 module + 破壊 SQL (PYEOF/heredoc 含む雑 grep だが false positive 許容)
if echo "$CMD" | grep -qiE 'python.*sqlite3' \
   && echo "$CMD" | grep -qiE '\b(UPDATE[[:space:]]+\w|DELETE[[:space:]]+FROM|DROP[[:space:]]+TABLE|ALTER[[:space:]]+TABLE)\b'; then
    DESTRUCTIVE=1
fi

if [ "$DESTRUCTIVE" -eq 0 ]; then
    exit 0
fi

# tmp / test 環境は除外
if echo "$CMD" | grep -qE '(/tmp/|/temp/|tempfile|test_|fixture_|:memory:)'; then
    exit 0
fi

# backup 確認 (W126 migration 後 path 動的化: $CLAUDE_PROJECT_DIR 使用)
BACKUP_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}/tools/ebay-manager/data/backups"
THRESHOLD_MIN=30

if [ ! -d "$BACKUP_DIR" ]; then
    echo "DB Write Confirm Violation (Q2/Q4):" >&2
    echo "[BLOCK] 破壊的 SQL を検出. backup ディレクトリが存在しません." >&2
    echo "  作成: mkdir -p tools/ebay-manager/data/backups" >&2
    echo "  Backup: sqlite3 tools/ebay-manager/data/monitor.db \".backup 'tools/ebay-manager/data/backups/monitor.db.bak-\$(date +%Y%m%d-%H%M).db'\"" >&2
    exit 2
fi

RECENT=$(find "$BACKUP_DIR" -maxdepth 2 \( -name "*.db.bak*" -o -name "*.bak.db*" -o -name "*.db-bak*" \) -mmin -$THRESHOLD_MIN 2>/dev/null | head -1)
if [ -z "$RECENT" ]; then
    echo "DB Write Confirm Violation (Q2/Q4):" >&2
    echo "[BLOCK] 破壊的 SQL (UPDATE/DELETE/DROP/ALTER) を検出. 直近 ${THRESHOLD_MIN}min 以内の backup が見つかりません." >&2
    echo "  Required: sqlite3 tools/ebay-manager/data/monitor.db \".backup 'tools/ebay-manager/data/backups/monitor.db.bak-\$(date +%Y%m%d-%H%M).db'\"" >&2
    echo "  Reason: feedback_post_db_modification_review.md / 2026-04-29 事故再発防止" >&2
    exit 2
fi

exit 0
