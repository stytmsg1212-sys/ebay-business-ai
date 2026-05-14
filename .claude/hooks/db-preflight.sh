#!/bin/bash
# DB pre-flight check (Karpathy K0 violation prevention)
# 入力: stdin に Claude が JSON で {tool_name: "Bash", tool_input: {command}}
# 出力: 違反検出時 exit 2 + stderr (Claude に提示される)
#
# 検出対象 (2026-04-29 事故 を踏まえて選定):
#  - Bash で .db ファイルに対する cp / mv / rm / sqlite3 操作
#  - 対象 .db が 0 bytes なら BLOCK (本番 DB と誤認した可能性)
#
# 経緯: 2026-04-29 事故
#   ebay_manager.db (0 bytes 空殻) を本番と誤認して cp し、
#   実本番 monitor.db (5.9MB) を見落とした K0 違反.

set -u
INPUT=$(cat)

# JSON parse は python に委譲 (jq 不要、Windows Git Bash で確実に動く)
CMD=$(python -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    pass
" <<< "$INPUT" 2>/dev/null)

# 空コマンド or .db 言及無しは即 pass
if [ -z "$CMD" ]; then
    exit 0
fi
if ! echo "$CMD" | grep -qE '\.db(\b|"|'"'"'|[[:space:]]|$)'; then
    exit 0
fi

# Read-only 操作は除外
if echo "$CMD" | grep -qiE 'sqlite3[^|;]*"[[:space:]]*(SELECT|PRAGMA|.schema|.tables)'; then
    exit 0
fi

# .db ファイルパスを抽出 (引用符内 / 引用符無 両対応)
DB_FILES=$(echo "$CMD" \
    | grep -oE '[^[:space:]"'"'"'`]+\.db(-(shm|wal))?' \
    | sort -u)

if [ -z "$DB_FILES" ]; then
    exit 0
fi

VIOLATIONS=""
for db in $DB_FILES; do
    # 相対パスは project root 基準で解決 (W126 後 $CLAUDE_PROJECT_DIR 動的化)
    if [[ "$db" == /* ]] || [[ "$db" =~ ^[A-Za-z]: ]]; then
        db_full="$db"
    else
        db_full="${CLAUDE_PROJECT_DIR:-$(pwd)}/$db"
    fi

    # 存在しないパスは skip (新規作成系: .backup の出力先など)
    if [ ! -f "$db_full" ]; then
        continue
    fi

    SIZE=$(stat -c%s "$db_full" 2>/dev/null || stat -f%z "$db_full" 2>/dev/null || echo -1)
    if [ "$SIZE" = "0" ]; then
        VIOLATIONS="$VIOLATIONS
[BLOCK] $db is 0 bytes (空殻ファイル).
  正しい本番 DB は tools/ebay-manager/data/monitor.db の可能性.
  確認方法: grep -r DB_PATH tools/ebay-manager/monitor/ tools/ebay-manager/tasks/
  経緯: 2026-04-29 事故再発防止 (ebay_manager.db 0 bytes を本番と誤認 cp)"
    fi
done

if [ -n "$VIOLATIONS" ]; then
    echo "DB Pre-flight Check Violation (K0 prevention):$VIOLATIONS" >&2
    exit 2
fi

exit 0
