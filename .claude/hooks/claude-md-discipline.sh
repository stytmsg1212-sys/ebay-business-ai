#!/bin/bash
# CLAUDE.md discipline 検出 (W2-D9-S1, harness 改修 W2):
#  機能 1: .claude/rules/*.md に paths frontmatter 残骸が残っていないか (Issue #16853 対応)
#  機能 2: subdir CLAUDE.md (tools/**, .company/**) 編集時に root CLAUDE.md @import 漏れ
#         (Issue #24987 closed as not planned で subdir lazy load 機能不全)
#
# 入力: stdin に Claude が JSON で {tool_name, tool_input: {file_path, content/new_string}}
# 出力: 違反検出時 exit 2 + stderr メッセージ (Claude に提示される)
#
# 本 hook は warning rehearsal mode で起動 (settings.json で `|| echo` fallback 経由)
# Python 経由 JSON parse (jq 不要、Windows Git Bash 互換)

set -u
INPUT=$(cat)

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

# 対象は .md ファイルのみ
if [[ "$FILE_PATH" != *.md ]]; then
    exit 0
fi

VIOLATIONS=""

# 機能 1: .claude/rules/*.md に paths frontmatter 残骸検出
# Issue #16853 で paths-scoped lazy load 機能不全 (Claude Code 2.1.123 OPEN)
# Option D pivot 採用済 → paths frontmatter は deprecated
# Claude Code は常に forward slash で file_path を渡すため backslash 分岐は不要
if [[ "$FILE_PATH" == *.claude/rules/*.md ]]; then
    HAS_PATHS=$(python -c "
import sys, re
src = sys.stdin.read()
m = re.match(r'^---\n(.*?)\n---', src, re.DOTALL)
if m:
    fm = m.group(1)
    if re.search(r'^paths:', fm, re.MULTILINE):
        print('1')
" <<< "$CONTENT" 2>/dev/null)
    if [ "$HAS_PATHS" = "1" ]; then
        VIOLATIONS="$VIOLATIONS
[BLOCK] .claude/rules/*.md に paths frontmatter 残骸検出.
  Issue #16853 で paths-scoped lazy load は機能不全 (Claude Code 2.1.123 OPEN).
  Option D pivot 採用済 (横断 rule = paths なし常時 load / 規制 = subdir CLAUDE.md @import).
  -> paths frontmatter を削除してください.
  詳細: feedback_harness_reform_master_checklist.md (Day 2 pivot 章) / session_2026_04_30_d2_paths_pivot.md"
    fi
fi

# 機能 2: subdir CLAUDE.md @import 漏れ検出
# tools/**, .company/** 配下の CLAUDE.md は root CLAUDE.md で @import 必須
# Windows / unix path mix 対応 (Python で case insensitive 正規化)
if [[ "$FILE_PATH" == *CLAUDE.md ]]; then
    BASENAME=$(basename "$FILE_PATH")
    if [ "$BASENAME" = "CLAUDE.md" ]; then
        # CLAUDE_PROJECT_DIR 未設定時は silent skip せず stderr 警告 (Q0 違反防止)
        if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
            echo "[claude-md-discipline] WARNING: CLAUDE_PROJECT_DIR 未設定、機能 2 (subdir @import 漏れ検出) skip" >&2
        else
            ROOT_DIR="$CLAUDE_PROJECT_DIR"
            ROOT_CLAUDE_MD="$ROOT_DIR/CLAUDE.md"

            # 相対 path 計算 + tools/.company prefix 判定を Python に委譲
            REL_PATH=$(python -c "
import sys
file_path = sys.argv[1].replace('\\\\', '/')
root_dir = sys.argv[2].replace('\\\\', '/').rstrip('/')

# case insensitive prefix 一致 (Windows path の C:/ vs c:/ 対応)
if file_path.lower().startswith(root_dir.lower() + '/'):
    rel = file_path[len(root_dir) + 1:]
    # subdir 判定: tools/ または .company/ で始まる かつ /CLAUDE.md で終わる
    if (rel.startswith('tools/') or rel.startswith('.company/')) and rel.endswith('/CLAUDE.md'):
        print(rel)
" "$FILE_PATH" "$ROOT_DIR" 2>/dev/null)

            if [ -n "$REL_PATH" ] && [ -f "$ROOT_CLAUDE_MD" ]; then
                # fixed-string 検索 (regex メタ文字 . / _ 等の誤動作回避)
                HAS_IMPORT=$(python -c "
import sys
rel = sys.argv[1]
try:
    with open(sys.argv[2], encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s == f'@{rel}' or s == f'@./{rel}':
                print('1')
                break
except Exception:
    pass
" "$REL_PATH" "$ROOT_CLAUDE_MD" 2>/dev/null)
                if [ "$HAS_IMPORT" != "1" ]; then
                    VIOLATIONS="$VIOLATIONS
[BLOCK] subdir CLAUDE.md 編集中、root CLAUDE.md に @import 行が見つかりません.
  対象: $REL_PATH
  Issue #24987 で subdir CLAUDE.md lazy load は closed as not planned (Claude Code 公式).
  root CLAUDE.md に \`@${REL_PATH}\` を追記して launch load を強制してください.
  既に @import 済の場合は path 表記揺れの可能性 (空白 / @./ prefix 等) — 本 warning を無視可.
  詳細: session_2026_04_30_d2_paths_pivot.md / feedback_harness_reform_master_checklist.md"
                fi
            fi
        fi
    fi
fi

if [ -n "$VIOLATIONS" ]; then
    echo "CLAUDE.md Discipline Violation (W2-D9-S1):$VIOLATIONS" >&2
    exit 2
fi

exit 0
