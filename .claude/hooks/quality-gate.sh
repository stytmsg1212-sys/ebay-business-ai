#!/bin/bash
# Boris Tip 24 + 10: PreToolUse Write|Edit のアンチパターン検出 → block
# 入力: stdin に Claude が JSON で {tool_name, tool_input: {file_path, content/new_string}}
# 出力: 違反検出時 exit 2 + stderr メッセージ (Claude に提示される)
#
# 検出対象 (本日 2026-04-25/26 の 9 件事故 を踏まえて選定):
#  - print(file=sys.stderr) → pythonw.exe で [Errno 22]
#  - except: pass / except Exception: pass → 握り潰し
#  - ALTER TABLE が try/except sqlite3.OperationalError 無 → migration 非冪等
#  - migration ファイル内の DROP TABLE / DELETE FROM → データ消失リスク
#
# テスト/設定/.bak ファイルは除外. jq 未インストール環境でも動作 (Python 経由).

set -u
INPUT=$(cat)

# JSON parse は python に委譲 (jq 不要、Windows Git Bash で確実に動く)
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
# content は --- 区切り以降全部
CONTENT=$(echo "$PARSED" | sed -n '/^---CONTENT-DELIMITER---$/,$p' | tail -n +2)

# Python ファイルのみ対象
if [[ "$FILE_PATH" != *.py ]]; then
    exit 0
fi

# 除外パス
if [[ "$FILE_PATH" == *test_* ]] \
   || [[ "$FILE_PATH" == *_test.py ]] \
   || [[ "$FILE_PATH" == */tests/* ]] \
   || [[ "$FILE_PATH" == *.bak* ]] \
   || [[ "$FILE_PATH" == *quality-gate* ]] \
   || [[ "$FILE_PATH" == *post-edit-audit* ]]; then
    exit 0
fi

VIOLATIONS=""

# 1. print(file=sys.stderr) — _safe_stderr_print 定義/呼出は除外
if echo "$CONTENT" | grep -qE 'print\([^)]*file[[:space:]]*=[[:space:]]*sys\.stderr'; then
    if ! echo "$CONTENT" | grep -qE '_safe_stderr_print|def[[:space:]]+_safe_stderr'; then
        VIOLATIONS="$VIOLATIONS
[BLOCK] print(file=sys.stderr) 検出. pythonw.exe + Streamlit で [Errno 22] EINVAL.
  -> logger.warning() を使うか, _safe_stderr_print() ヘルパー経由で.
  詳細: feedback_no_silent_skip_no_fake_success.md 事例 2"
    fi
fi

# 2-a. bare except: 検出 (python regex で改行考慮)
HAS_BARE=$(python -c "
import sys, re
src = sys.stdin.read()
# bare 'except:' の直後 (空白のみで pass) パターン
if re.search(r'^[\t ]*except[\t ]*:[\t ]*\$', src, re.MULTILINE):
    print('1')
" <<< "$CONTENT" 2>/dev/null)
if [ "$HAS_BARE" = "1" ]; then
    VIOLATIONS="$VIOLATIONS
[BLOCK] bare 'except:' 検出. specific exception (sqlite3.OperationalError 等) で捕捉せよ."
fi

# 2-b. except Exception: pass (multiline)
HAS_PASS=$(python -c "
import sys, re
src = sys.stdin.read()
# 'except Exception' の次行 'pass' のみ (logger.exception 等が無い)
if re.search(r'except[\t ]+Exception[^:]*:[\t ]*\n[\t ]+pass[\t ]*(?:\n|\$)', src):
    print('1')
" <<< "$CONTENT" 2>/dev/null)
if [ "$HAS_PASS" = "1" ]; then
    VIOLATIONS="$VIOLATIONS
[BLOCK] except Exception: pass 検出 (握り潰し).
  -> specific exception 捕捉 + logger.exception() で必ず記録.
  詳細: feedback_no_silent_skip_no_fake_success.md"
fi

# 3. ALTER TABLE で OperationalError 言及無し
if echo "$CONTENT" | grep -qE 'ALTER[[:space:]]+TABLE'; then
    if ! echo "$CONTENT" | grep -qE 'OperationalError'; then
        VIOLATIONS="$VIOLATIONS
[BLOCK] ALTER TABLE が try/except sqlite3.OperationalError で囲まれていません.
  migration の冪等性が壊れます (再起動で OperationalError).
  詳細: feedback_db_migration_idempotency.md"
    fi
fi

# 4. migration / database ファイル内の DROP TABLE / DELETE FROM
if echo "$CONTENT" | grep -qE 'DROP[[:space:]]+TABLE|DELETE[[:space:]]+FROM'; then
    if [[ "$FILE_PATH" == *migration* ]] \
       || [[ "$FILE_PATH" == *schema* ]] \
       || [[ "$FILE_PATH" == *database.py ]]; then
        VIOLATIONS="$VIOLATIONS
[BLOCK] migration/database ファイル内に DROP TABLE / DELETE FROM 検出.
  -> 別 one-shot script に分離 (feedback_db_migration_idempotency.md).
  本番 DB データ消失事故の再発防止."
    fi
fi

if [ -n "$VIOLATIONS" ]; then
    echo "Quality Gate Violation (Boris Tip 24):$VIOLATIONS" >&2
    exit 2
fi

exit 0
