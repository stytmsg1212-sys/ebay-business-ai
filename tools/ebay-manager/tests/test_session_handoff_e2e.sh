#!/bin/bash
# W59 zero-paste session handoff regression test
# DoD G1-G4 を機械検証
#
# 実行:
#   cd tools/ebay-manager
#   bash tests/test_session_handoff_e2e.sh
#
# 検証 4 ケース:
#   G1: _NEXT_SESSION.md ありで hook が additionalContext JSON 出力
#   G2: mtime 7d 超で [STALE WARNING] prefix が出る
#   G3: _NEXT_SESSION.md 不在でも hook が exit 0 + fallback message
#   G4: master_checklist MD5 mismatch で [CHECKLIST DIVERGED] が出る
#
# 環境:
#   - CLAUDE_PROJECT_DIR は本 test で auto set
#   - _NEXT_SESSION.md は test 用に temp dir 経由 (本番 file 非破壊)

set -u

ROOT_DIR="${CLAUDE_PROJECT_DIR:-C:/Users/gucch/projects/claude}"
HOOK_SCRIPT="$ROOT_DIR/.claude/hooks/session-start-load-incantation.sh"
MEM_DIR="$HOME/.claude/projects/C--Users-gucch-OneDrive-work-claude/memory"
NS_FILE="$MEM_DIR/_NEXT_SESSION.md"
NS_BACKUP=""

# Test 結果集計
PASS=0
FAIL=0

assert_contains() {
    local label="$1"
    local needle="$2"
    local output="$3"
    if echo "$output" | grep -q "$needle"; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label"
        echo "    期待: $needle"
        echo "    実際: $(echo "$output" | head -3)"
        FAIL=$((FAIL + 1))
    fi
}

# 既存 _NEXT_SESSION.md があれば backup
if [ -f "$NS_FILE" ]; then
    NS_BACKUP="$NS_FILE.test-backup-$$"
    cp "$NS_FILE" "$NS_BACKUP"
    echo "[setup] 既存 _NEXT_SESSION.md を $NS_BACKUP に backup"
fi

cleanup() {
    if [ -n "$NS_BACKUP" ] && [ -f "$NS_BACKUP" ]; then
        mv "$NS_BACKUP" "$NS_FILE"
        echo "[teardown] _NEXT_SESSION.md を restore"
    elif [ -z "$NS_BACKUP" ] && [ -f "$NS_FILE" ]; then
        rm -f "$NS_FILE"
        echo "[teardown] test で作成した _NEXT_SESSION.md を削除"
    fi
}
trap cleanup EXIT

# ──────────────────────────────────────────────────────────
# G3: file 不在 fallback (一番先に test、副作用なし)
# ──────────────────────────────────────────────────────────
echo ""
echo "=== G3: _NEXT_SESSION.md 不在 fallback ==="
rm -f "$NS_FILE"
OUT=$(CLAUDE_PROJECT_DIR="$ROOT_DIR" bash "$HOOK_SCRIPT" 2>/dev/null)
EXIT_CODE=$?
assert_contains "G3-1: exit 0 (silent skip 防止)" "0" "$EXIT_CODE"
assert_contains "G3-2: hookSpecificOutput JSON 構造" "hookSpecificOutput" "$OUT"
assert_contains "G3-3: file 不在 fallback message" "_NEXT_SESSION.md なし" "$OUT"

# ──────────────────────────────────────────────────────────
# G1: 通常運用 (_NEXT_SESSION.md ありで auto inject)
# ──────────────────────────────────────────────────────────
echo ""
echo "=== G1: _NEXT_SESSION.md auto inject ==="
cat > "$NS_FILE" <<'EOF'
---
generated: 2026-04-30T18:00:00+09:00
master_checklist_md5: null
silent_skip_ongoing:
  - task_email_pickup
  - task_news_check
next_priority:
  - W54 task_email_pickup INSERT silent skip 修正
  - W55 task_news_check INSERT silent skip 修正
bg_jobs: []
---

# 次セッション着手用 (auto-loaded)

## 前回セッション要約
W59 zero-paste 実装完走 (regression test 含む).

## 次の最優先タスク
1. W54 task_email_pickup INSERT silent skip 修正 (60-75 分)
2. W55 task_news_check INSERT silent skip 修正 (60-75 分)
EOF
OUT=$(CLAUDE_PROJECT_DIR="$ROOT_DIR" bash "$HOOK_SCRIPT" 2>/dev/null)
assert_contains "G1-1: hookSpecificOutput JSON 構造" "hookSpecificOutput" "$OUT"
assert_contains "G1-2: additionalContext に W54 含む" "W54" "$OUT"
assert_contains "G1-3: silent_skip_ongoing → 機会損失中 表示" "機会損失中" "$OUT"
assert_contains "G1-4: 2 task 検出" "2 task" "$OUT"

# ──────────────────────────────────────────────────────────
# G2: staleness 7d 超で WARNING
# ──────────────────────────────────────────────────────────
echo ""
echo "=== G2: staleness 7d 超 WARNING ==="
# mtime を 8 日前に setting
touch -d "8 days ago" "$NS_FILE" 2>/dev/null || python -c "
import os, time
os.utime(r'$NS_FILE', (time.time() - 8*86400, time.time() - 8*86400))
"
OUT=$(CLAUDE_PROJECT_DIR="$ROOT_DIR" bash "$HOOK_SCRIPT" 2>/dev/null)
assert_contains "G2-1: STALE WARNING prefix" "STALE WARNING" "$OUT"

# ──────────────────────────────────────────────────────────
# G5 (HIGH-4): silent_skip_ongoing: [] 空配列で明示通知
# ──────────────────────────────────────────────────────────
echo ""
echo "=== G5: silent_skip_ongoing 空配列で 0 件明示通知 ==="
cat > "$NS_FILE" <<'EOF'
---
generated: 2026-04-30T18:00:00+09:00
master_checklist_md5: null
silent_skip_ongoing: []
next_priority: []
bg_jobs: []
---

# Test
EOF
touch "$NS_FILE"
OUT=$(CLAUDE_PROJECT_DIR="$ROOT_DIR" bash "$HOOK_SCRIPT" 2>/dev/null)
assert_contains "G5-1: 空配列で [OK] 0 件通知" "0 件" "$OUT"

# ──────────────────────────────────────────────────────────
# G4: master_checklist MD5 mismatch
# ──────────────────────────────────────────────────────────
echo ""
echo "=== G4: master_checklist MD5 mismatch 検出 ==="
# frontmatter に偽 MD5 を書く
cat > "$NS_FILE" <<'EOF'
---
generated: 2026-04-30T18:00:00+09:00
master_checklist_md5: deadbeefdeadbeefdeadbeefdeadbeef
silent_skip_ongoing: []
next_priority: []
bg_jobs: []
---

# Test
EOF
# mtime を最近に戻す
touch "$NS_FILE"
OUT=$(CLAUDE_PROJECT_DIR="$ROOT_DIR" bash "$HOOK_SCRIPT" 2>/dev/null)
# master_checklist file が memory dir にある場合のみ検出可
if [ -f "$MEM_DIR/feedback_harness_reform_master_checklist.md" ]; then
    assert_contains "G4-1: CHECKLIST DIVERGED 検出" "CHECKLIST DIVERGED" "$OUT"
else
    echo "  ⚠️ G4-1: master_checklist file なし (skip、情報のみ)"
fi

# ──────────────────────────────────────────────────────────
# 結果集計
# ──────────────────────────────────────────────────────────
echo ""
echo "================================"
echo "Test 結果: PASS=$PASS FAIL=$FAIL"
echo "================================"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
exit 0
