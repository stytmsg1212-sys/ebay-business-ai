#!/bin/bash
# W127/W128 hook slug 計算ロジックの抽出 (test 用、本体 .claude/hooks/session-start-load-incantation.sh と同一).
# 引数 $1 = ROOT_DIR (CLAUDE_PROJECT_DIR 相当)
# 出力 stdout = PROJECT_HASH

ROOT_DIR="$1"

if [[ "$ROOT_DIR" =~ ^/cygdrive/ ]]; then
    SLUG_INPUT="$ROOT_DIR"
elif [[ "$ROOT_DIR" =~ ^/([a-zA-Z])/(.*)$ ]]; then
    DRIVE_UPPER=$(echo "${BASH_REMATCH[1]}" | tr '[:lower:]' '[:upper:]')
    SLUG_INPUT="${DRIVE_UPPER}:\\${BASH_REMATCH[2]}"
else
    SLUG_INPUT="$ROOT_DIR"
fi

PROJECT_HASH=$(echo "$SLUG_INPUT" | sed 's/:/-/g' | sed 's/[\\/]/-/g')
printf '%s' "$PROJECT_HASH"
