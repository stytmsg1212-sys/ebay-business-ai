#!/bin/bash
# Boris Tip 10: PostToolUse Write|Edit. 警告のみ (exit 0). stderr 経由で気付かせる.
# block しない (post なので変更は既に書込まれている). 警告で次の修正を促す.

set -u
INPUT=$(cat)

# JSON parse は python に委譲 (jq 不要)
PARSED=$(python -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {})
    print(ti.get('file_path', ''))
    print('---CONTENT-DELIMITER---')
    print(ti.get('content', '') or ti.get('new_string', '') or '')
except Exception:
    pass
" <<< "$INPUT" 2>/dev/null)

FILE_PATH=$(echo "$PARSED" | sed -n '1p')
CONTENT=$(echo "$PARSED" | sed -n '/^---CONTENT-DELIMITER---$/,$p' | tail -n +2)

if [[ "$FILE_PATH" != *.py ]]; then
    exit 0
fi

# fake success: except ブロック内に "success": True / 'success': True
HAS_FAKE=$(python -c "
import sys, re
src = sys.stdin.read()
if re.search(r'except[^:]*:\s*\n[^}]*?[\"\']success[\"\']\s*[:=]\s*True', src):
    print('1')
" <<< "$CONTENT" 2>/dev/null)
if [ "$HAS_FAKE" = "1" ]; then
    echo "[WARN post-edit] except 内で success: True 検出. fake success 可能性. 確認: feedback_no_silent_skip_no_fake_success.md" >&2
fi

# INSERT OR IGNORE rowcount 未チェック
if echo "$CONTENT" | grep -qE 'INSERT[[:space:]]+OR[[:space:]]+IGNORE'; then
    if ! echo "$CONTENT" | grep -qE 'rowcount|lastrowid'; then
        echo "[WARN post-edit] INSERT OR IGNORE で rowcount/lastrowid チェック無し. silent insert failure 可能性." >&2
    fi
fi

# eBay API 関連変更検出 (PreCommit hook で再確認させる目的)
if echo "$CONTENT" | grep -qE 'ReviseItem|RelistItem|AddItem|ShippingType|ShippingServiceCost'; then
    if [[ "$FILE_PATH" == *ebay* ]] || [[ "$FILE_PATH" == *trading* ]]; then
        echo "[REMIND post-edit] eBay API 変更検出. VerifyRelistItem dry-run + GetItem で実反映確認したか?" >&2
    fi
fi

exit 0
