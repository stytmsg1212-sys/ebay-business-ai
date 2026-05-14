#!/bin/bash
# SessionStart hook: 前回セッション末で session-close skill が生成した
# _NEXT_SESSION.md を auto-load して context に inject (W59 zero-paste 設計の核心)
#
# 5 段検査:
#  Step 1: _NEXT_SESSION.md Read (file 不在 / 0 byte → silent fallback)
#  Step 2: staleness 3 段 (24h 以内 / 24h-7d / 7d 超で warning prefix)
#  Step 3: master_checklist MD5 mismatch 検出
#  Step 4: scheduler.log tail (eBay Manager のみ ERROR/locked grep)
#  Step 5: silent_skip_ongoing → 🚨 機会損失中 表示
#
# 出力: hookSpecificOutput.additionalContext (Claude Code 公式仕様)
# Issue #13650 (Windows stdout silent drop) 対策: set -u + json.dumps + exit 0
# 関連 skill: ~/.claude/skills/session-close/SKILL.md (file 生成元)
#           ~/.claude/skills/session-resume/SKILL.md (manual fallback)

set -u

# Issue #13650 / cp932 silent drop 対策: Python stdout/stderr を強制 UTF-8 化
# (Windows Git Bash で ✅ 🚨 等の絵文字を含む additionalContext を確実に inject する)
export PYTHONIOENCODING=utf-8

ROOT_DIR="${CLAUDE_PROJECT_DIR:-C:/Users/gucch/projects/claude}"
# W126 migration 後対応: hash を path から動的計算 (`:` `\` `/` を `-` に変換)
PROJECT_HASH=$(echo "$ROOT_DIR" | sed 's/:/-/g' | sed 's/[\\/]/-/g')
MEM_DIR="$HOME/.claude/projects/${PROJECT_HASH}/memory"

export ROOT_DIR
export MEM_DIR

# stderr に DEBUG ログ (journal で動作確認可、stdout には影響しない)
echo "[DEBUG session-start-load-incantation] fired @ $(date '+%Y-%m-%d %H:%M:%S')" >&2

# 5 段検査を Python でまとめて実行 (json.dumps 一発で escape 確実、Issue #13650 回避)
python <<'PYEOF'
import os, sys, json, time, hashlib, re
from pathlib import Path

# 防御的: PYTHONIOENCODING が効かない環境でも UTF-8 出力を確保
# pythonw.exe で sys.stdout が None の可能性 (CLAUDE.md 規約: hasattr ガード必須)
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        # silent skip 防止: 起動時ガードで詳細 error 不要 (Q0 例外)、UTF-8 化失敗は cp932 fallback で動作継続
        pass

ns_file = Path(os.environ['MEM_DIR']) / '_NEXT_SESSION.md'
mem_dir = Path(os.environ['MEM_DIR'])
root_dir = Path(os.environ['ROOT_DIR'])

# Step 1: _NEXT_SESSION.md Read + 不在/0 byte fallback
try:
    if not ns_file.exists() or ns_file.stat().st_size == 0:
        # 初回起動 or session-close 未実行 = silent skip 防止のため明示通知
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': '[INFO] _NEXT_SESSION.md なし (初回起動 or session-close 未実行). 通常起動 = MEMORY.md 経由で文脈把握してください.'
            }
        }, ensure_ascii=False))
        sys.exit(0)

    content = ns_file.read_text(encoding='utf-8')
except Exception as e:
    # OneDrive sync lock 等の競合 = silent skip 防止のため明示通知
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': f'[WARN] _NEXT_SESSION.md 読込失敗 ({type(e).__name__}: {e}). session-resume skill で手動 review 推奨.'
        }
    }, ensure_ascii=False))
    sys.exit(0)

# Step 2: staleness 3 段判定
mtime = ns_file.stat().st_mtime
age_hours = (time.time() - mtime) / 3600
age_days = age_hours / 24

if age_hours <= 24:
    stale_prefix = ''
elif age_days <= 7:
    stale_prefix = f'[INFO] _NEXT_SESSION.md は {age_days:.1f} 日前生成. 一次情報照合推奨.\n\n'
else:
    stale_prefix = f'[STALE WARNING] _NEXT_SESSION.md は {age_days:.0f} 日前生成. session-resume skill で意識的 review してから着手推奨.\n\n'

# Step 3: master_checklist MD5 mismatch 検出 (frontmatter saved_md5 vs 現 file MD5)
# HIGH-2 (code-reviewer 2026-04-30): except Exception: pass = silent skip 違反 → 明示 warning
md5_warning = ''
md5_match = re.search(r'^master_checklist_md5:\s*(\w+)', content, re.M)
if md5_match and md5_match.group(1) != 'null':
    saved_md5 = md5_match.group(1)
    mc_file = mem_dir / 'feedback_harness_reform_master_checklist.md'
    if mc_file.exists():
        try:
            current_md5 = hashlib.md5(mc_file.read_bytes()).hexdigest()
            if current_md5 != saved_md5:
                md5_warning = f'[CHECKLIST DIVERGED] master_checklist が前回 close 後に変更. 前: {saved_md5} / 現: {current_md5}. git log 確認推奨.\n\n'
        except (OSError, ValueError) as e:
            # silent skip 防止: file 読込/hash 計算失敗を明示通知
            md5_warning = f'[CHECKLIST CHECK FAILED] {type(e).__name__}: {e}\n\n'

# Step 4: scheduler.log tail (eBay Manager 限定、ERROR/locked grep)
sched_warning = ''
sched_log = root_dir / 'tools' / 'ebay-manager' / 'logs' / 'scheduler.log'
if sched_log.exists():
    try:
        with sched_log.open(encoding='utf-8', errors='ignore') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8000))
            lines = f.readlines()[-50:]
        hits = [l.strip() for l in lines if re.search(r'ERROR|CRITICAL|database is locked|silent.skip', l, re.I)][:5]
        if hits:
            sched_warning = f'[SCHEDULER] 直近 50 行に異常行 {len(hits)} 件:\n'
            for h in hits:
                sched_warning += f'  - {h[:200]}\n'
            sched_warning += '\n'
        else:
            sched_warning = '[SCHEDULER] 直近 50 行 ERROR/locked なし ✅\n\n'
    except (OSError, UnicodeDecodeError) as e:
        # silent skip 防止: file 読込/decode 失敗を明示通知 (HIGH-A1 修正)
        sched_warning = f'[SCHEDULER] tail 失敗: {type(e).__name__}: {e}\n\n'

# Step 5: silent_skip_ongoing 配列読出 (frontmatter YAML 配列パース簡易版)
# HIGH-4 (code-reviewer 2026-04-30): 空配列 [] でも明示通知 (= 0 件確定 vs 検査壊れたを区別)
silent_skip_warning = ''
ss_block = re.search(r'^silent_skip_ongoing:\s*(\[\s*\]|\n((?:\s+-\s+\S+\s*\n)+))', content, re.M)
if ss_block:
    if ss_block.group(1).strip().startswith('['):
        # 空配列 [] = 機会損失なし、明示通知
        silent_skip_warning = '[OK] silent_skip_ongoing 0 件 (機会損失なし)\n\n'
    elif ss_block.group(2):
        tasks = re.findall(r'-\s+(\S+)', ss_block.group(2))
        if tasks:
            silent_skip_warning = f'🚨 機会損失中 ({len(tasks)} task silent skip 継続): ' + ', '.join(tasks) + '\n\n'

# 全文構築 (prefix + body)
full_context = stale_prefix + md5_warning + sched_warning + silent_skip_warning + content

# 7000 char strict 上限 enforce (defensive、公式 char 制限は未確認)
# HIGH-3 (code-reviewer 2026-04-30): 「公式 10000 char」と書いていたが一次情報未照合のため
# defensive な内部上限と明確化 (memory_staleness_2026_04_30 整合)
MAX_CHARS = 7000
if len(full_context) > MAX_CHARS:
    truncated_marker = f'\n\n[... {MAX_CHARS} char 上限超過、_NEXT_SESSION.md 直接 Read 推奨: {ns_file} ...]'
    full_context = full_context[:MAX_CHARS - len(truncated_marker)] + truncated_marker

# JSON 出力 (Claude Code 公式仕様 hookSpecificOutput.additionalContext)
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': full_context
    }
}, ensure_ascii=False))
PYEOF

# Issue #13650 対策: exit 0 を明示 (set -u 下でも確実に正常終了)
exit 0
