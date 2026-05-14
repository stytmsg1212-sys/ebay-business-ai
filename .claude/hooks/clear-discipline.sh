#!/bin/bash
# /clear discipline 機械化 hook (W2-D10-S3, harness 改修 W2):
#   transcript jsonl の累積 char count を計測し、token 概算 75% 超で
#   /clear or /compact 推奨 warning を 1 セッション 1 回出力する.
#
# 仕様:
#   matcher: UserPromptSubmit (user message 投稿時のみ発火 = 無駄打ち回避)
#   threshold: 3MB chars (Opus 4.7 1M context の 75% = ~750K token 概算)
#              ※ Sonnet 200K context は 600KB 想定だが本 user 主モデルは Opus 1M
#                 model 別 threshold は W43 ROADMAP (K1 Simplicity 優先)
#   dedupe: .claude/.cache/clear-warning-fired.json (session_id 一致で skip)
#   出力: stdout JSON {"systemMessage": "..."} で Claude/user に通知
#
# 入力 (stdin): {session_id, transcript_path, ...}
# 出力: 違反検出時 systemMessage で warning (exit 0, blocking なし)
#
# 本 hook は warning rehearsal mode で起動 (settings.json で `|| echo` fallback 経由)
# Python 経由 JSON parse (jq 不要、Windows Git Bash 互換)
# Q0 silent skip 防止: 全 fallback path で stderr warning + state file 記録

set -u
INPUT=$(cat)

# ------ payload parse ------
PARSED=$(python -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', ''))
    print(d.get('transcript_path', ''))
except Exception:
    pass
" <<< "$INPUT" 2>/dev/null)

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
TRANSCRIPT_PATH=$(echo "$PARSED" | sed -n '2p')

# ------ Q0 silent skip 防止: transcript_path 未設定時 ------
if [ -z "$TRANSCRIPT_PATH" ]; then
    echo "[clear-discipline] WARNING: transcript_path 未設定、turn_count check skip" >&2
    exit 0
fi

if [ ! -f "$TRANSCRIPT_PATH" ]; then
    echo "[clear-discipline] WARNING: transcript_path not found: $TRANSCRIPT_PATH" >&2
    exit 0
fi

# ------ state file 準備 ------
# CLAUDE_PROJECT_DIR 未設定時も silent skip せず stderr 警告 (Q0 違反防止)
if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    echo "[clear-discipline] WARNING: CLAUDE_PROJECT_DIR 未設定、dedupe state file 書込 skip" >&2
    STATE_FILE=""
else
    STATE_FILE="$CLAUDE_PROJECT_DIR/.claude/.cache/clear-warning-fired.json"
    mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
fi

# ------ dedupe check (state file 存在 + session_id 一致なら skip) ------
if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
    ALREADY_FIRED=$(python -c "
import sys, json
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        d = json.load(f)
    if d.get('session_id') == sys.argv[2]:
        print('1')
except Exception:
    pass
" "$STATE_FILE" "$SESSION_ID" 2>/dev/null)
    if [ "$ALREADY_FIRED" = "1" ]; then
        # 同一セッションで warning 済 → skip (連発防止)
        exit 0
    fi
fi

# ------ jsonl 累積 char count + parse error 検出 ------
RESULT=$(python -c "
import sys, json
path = sys.argv[1]
total_chars = 0
parse_errors = 0
try:
    with open(path, encoding='utf-8') as f:
        for line in f:
            total_chars += len(line)
            try:
                json.loads(line)
            except Exception:
                parse_errors += 1
    print(total_chars)
    print(parse_errors)
except Exception as e:
    print(0)
    print(-1)
    print(str(e), file=sys.stderr)
" "$TRANSCRIPT_PATH" 2>/dev/null)

TOTAL_CHARS=$(echo "$RESULT" | sed -n '1p')
PARSE_ERRORS=$(echo "$RESULT" | sed -n '2p')

# ------ Q0 silent skip 防止: jsonl 読込失敗時 ------
if [ "$PARSE_ERRORS" = "-1" ]; then
    echo "[clear-discipline] WARNING: jsonl 読込失敗、turn_count check skip" >&2
    exit 0
fi

# ------ jsonl parse error あれば stderr 1 回 (state file dedupe、threshold 未満でも記録) ------
PARSE_ERROR_FLAG=0
if [ "${PARSE_ERRORS:-0}" -gt 0 ]; then
    PARSE_WARNED=""
    if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
        PARSE_WARNED=$(python -c "
import sys, json
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        d = json.load(f)
    if d.get('session_id') == sys.argv[2] and d.get('parse_error_warned'):
        print('1')
except Exception:
    pass
" "$STATE_FILE" "$SESSION_ID" 2>/dev/null)
    fi
    if [ "$PARSE_WARNED" != "1" ]; then
        echo "[clear-discipline] WARNING: jsonl 内 ${PARSE_ERRORS} 行 parse error 検出 (R-5 30 step 毎の生ログ再読時に確認推奨)" >&2
        PARSE_ERROR_FLAG=1
    fi
fi

# ------ threshold 判定: 3MB chars (Opus 4.7 1M context 75% 概算、英語前提) ------
THRESHOLD=3145728  # 3 * 1024 * 1024

if [ "${TOTAL_CHARS:-0}" -lt "$THRESHOLD" ]; then
    # threshold 未満 = warning fire しない
    # ただし parse_error_warned だけは記録 (H-1: warning spam 防止)
    if [ "$PARSE_ERROR_FLAG" = "1" ] && [ -n "$STATE_FILE" ]; then
        python -c "
import sys, json
state = {
    'session_id': sys.argv[1],
    'fired_at': None,
    'total_chars': int(sys.argv[2]),
    'parse_error_warned': True,
}
try:
    with open(sys.argv[3], 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
except Exception as e:
    print(f'[clear-discipline] WARNING: state file write failed: {e}', file=sys.stderr)
" "$SESSION_ID" "$TOTAL_CHARS" "$STATE_FILE" 2>&1
    fi
    exit 0
fi

# ------ warning fire ------
# state file write (best effort、失敗しても warning は出す)
if [ -n "$STATE_FILE" ]; then
    python -c "
import sys, json
from datetime import datetime
state = {
    'session_id': sys.argv[1],
    'fired_at': datetime.now().isoformat(),
    'total_chars': int(sys.argv[2]),
    'parse_error_warned': bool(int(sys.argv[3])),
}
try:
    with open(sys.argv[4], 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
except Exception as e:
    print(f'[clear-discipline] WARNING: state file write failed: {e}', file=sys.stderr)
" "$SESSION_ID" "$TOTAL_CHARS" "$PARSE_ERROR_FLAG" "$STATE_FILE" 2>&1
fi

# stdout JSON で systemMessage 出力 (Claude/user に通知)
# 文言: 1 token ≒ 4 char (英語) で 75% 概算、日本語混在では 1 token ≒ 1-2 char で実態より楽観
TOTAL_MB=$(python -c "print(f'{${TOTAL_CHARS}/1024/1024:.2f}')")
cat <<EOF
{"systemMessage": "[文脈管理] transcript ${TOTAL_MB} MB (Opus 1M context 推定 75%+ / 日本語多めの場合はオーバーフロー懸念). /compact (focus 指定推奨) または /clear を検討してください. Boris Tip 18 / Cal Rueb red flag #2 (context rot 防止). 詳細: feedback_context_loss_recovery_protocol.md"}
EOF
exit 0
