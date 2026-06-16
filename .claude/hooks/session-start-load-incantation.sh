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
# W128 (2026-05-15): Git Bash で CLAUDE_PROJECT_DIR が unix-style /c/Users/... で渡されると
# sed パイプが PROJECT_HASH=-c-Users-... と崩壊し _NEXT_SESSION.md を miss する事故 (5/14-15) の修正.
# unix-style を Windows-style C:\Users\... に正規化してから slug 化. ROOT_DIR 本体は元値保持
# (mkdir / Python Path() 等への副作用回避).
if [[ "$ROOT_DIR" =~ ^/cygdrive/ ]]; then
    # H-4 (code-reviewer 2026-05-15): Cygwin /cygdrive/c/... を明示除外.
    # 元 regex は match してしまい "C--ygdrive-c-..." と silent corrupt するため別 path に流す.
    # MEM_DIR は不在となり SessionStart hook の Step 1 fallback (MEMORY.md 経由) が起動 = healthy.
    SLUG_INPUT="$ROOT_DIR"
elif [[ "$ROOT_DIR" =~ ^/([a-zA-Z])/(.*)$ ]]; then
    DRIVE_UPPER=$(echo "${BASH_REMATCH[1]}" | tr '[:lower:]' '[:upper:]')
    SLUG_INPUT="${DRIVE_UPPER}:\\${BASH_REMATCH[2]}"
else
    SLUG_INPUT="$ROOT_DIR"
fi
PROJECT_HASH=$(echo "$SLUG_INPUT" | sed 's/:/-/g' | sed 's/[\\/]/-/g')
MEM_DIR="$HOME/.claude/projects/${PROJECT_HASH}/memory"

export ROOT_DIR
export MEM_DIR

# stderr に DEBUG ログ (journal で動作確認可、stdout には影響しない)
echo "[DEBUG session-start-load-incantation] fired @ $(date '+%Y-%m-%d %H:%M:%S')" >&2

# W127 #1 (2026-05-14): 永続 debug log (次回 hook 不具合再現時の root cause 特定用)
HOOK_LOG="$ROOT_DIR/tools/ebay-manager/logs/sessionstart_hook.log"
mkdir -p "$(dirname "$HOOK_LOG")" 2>/dev/null
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  echo "  CLAUDE_PROJECT_DIR='${CLAUDE_PROJECT_DIR:-<unset>}'"
  echo "  HOME='${HOME:-<unset>}'"
  echo "  ROOT_DIR='${ROOT_DIR}'"
  echo "  PROJECT_HASH='${PROJECT_HASH}'"
  echo "  MEM_DIR='${MEM_DIR}'"
  echo "  PWD='$(pwd)'"
} >> "$HOOK_LOG" 2>/dev/null || true

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

# W127 #1 (2026-05-14): 永続 debug log 追記用 helper
hook_log = root_dir / 'tools' / 'ebay-manager' / 'logs' / 'sessionstart_hook.log'

def _log_debug(msg: str) -> None:
    """次回不具合再現時の root cause 特定のため stat/分岐を永続記録. 失敗は無視 (起動を妨げない)."""
    try:
        with hook_log.open('a', encoding='utf-8') as f:
            f.write(f'  py: {msg}\n')
    except Exception:
        pass

# W127 #3 (2026-05-14): hookSpecificOutput 冒頭に hook 解決状態を明示 (assistant 即視用)
debug_header = f'[hook] ROOT_DIR={root_dir} MEM_DIR={mem_dir}'

# Step 1: _NEXT_SESSION.md Read + 不在/0 byte fallback
try:
    if not ns_file.exists():
        _log_debug(f'ns_file NOT EXISTS path={ns_file}')
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': f'{debug_header}\n[INFO] _NEXT_SESSION.md なし (初回起動 or session-close 未実行). 通常起動 = MEMORY.md 経由で文脈把握してください.'
            }
        }, ensure_ascii=False))
        sys.exit(0)

    ns_stat = ns_file.stat()
    _log_debug(f'ns_file size={ns_stat.st_size} mtime={time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ns_stat.st_mtime))}')

    if ns_stat.st_size == 0:
        _log_debug('ns_file size=0 -> fallback')
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': f'{debug_header}\n[INFO] _NEXT_SESSION.md が 0 byte (session-close 中断 / 書き込み失敗の可能性). 通常起動 = MEMORY.md 経由で文脈把握してください.'
            }
        }, ensure_ascii=False))
        sys.exit(0)

    content = ns_file.read_text(encoding='utf-8')
    _log_debug(f'ns_file content_len={len(content)}')
except Exception as e:
    _log_debug(f'ns_file read EXCEPTION {type(e).__name__}: {e}')
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': f'{debug_header}\n[WARN] _NEXT_SESSION.md 読込失敗 ({type(e).__name__}: {e}). session-resume skill で手動 review 推奨.'
        }
    }, ensure_ascii=False))
    sys.exit(0)

# Step 2: staleness 3 段判定 + W127 #4 mtime 未来 sanity check
mtime = ns_file.stat().st_mtime
now_epoch = time.time()
age_hours = (now_epoch - mtime) / 3600
age_days = age_hours / 24

sanity_warning = ''
if mtime > now_epoch + 60:
    # 60 秒以上未来 = システム時計ズレ or file timestamp 改ざんの異常
    sanity_warning = f'[SANITY] _NEXT_SESSION.md mtime ({time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))}) が現在時刻より {(mtime - now_epoch):.0f} 秒未来. system clock 確認推奨.\n\n'
    _log_debug(f'sanity FUTURE mtime delta={(mtime - now_epoch):.0f}s')

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
        # W127 (2026-05-14): 「別のスケジューラーが稼働中 ... 多重起動防止」ERROR は正常動作のため除外.
        # 他の ERROR / Traceback / locked / silent.skip / CRITICAL は引き続き検出.
        hits = []
        for l in lines:
            if re.search(r'CRITICAL|Traceback|database is locked|silent.skip|RESTART_FAILED', l, re.I):
                hits.append(l.strip())
            elif re.search(r'ERROR', l) and '多重起動防止' not in l and 'concurrent_run_skip' not in l:
                hits.append(l.strip())
        hits = hits[:5]
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

# Step 4.5: claude auto-restart loop health check (W131 P5 watcher-of-watcher / 2026-05-16)
#   出典: 5/15 夜 W124 P4 v2 + 5/16 -NoExit regression fix セッションで判明したリスク:
#     - 1h 20回 crash loop guard → 長時間ネット断で loop suicide
#     - logon しないまま PC 起動で Startup folder shortcut 未発火
#   対処: heartbeat 60s 超 stale なら auto-recovery (KillSwitch 尊重).
claude_loop_warning = ''
import subprocess
hb_file = Path('C:/Users/gucch/.claude/scripts/claude-loop.heartbeat')
killswitch_file = Path('C:/Users/gucch/.claude/scripts/claude-loop.STOP')
loop_script = 'C:/Users/gucch/.claude/scripts/start-claude-loop.ps1'

def _spawn_loop_with_verify(prev_hb_mtime: float) -> tuple[bool, str]:
    """start-claude-loop.ps1 を Windows で親プロセス独立起動 + spawn 後 verify.

    Codex Round 1 fix HIGH-2 + LOW-6 (2026-05-16): Popen 成功 = RECOVERED は
    false positive (script 即 exit / claude PATH 不在で silent fail). spawn 前 hb mtime
    と比較し、20 秒以内に hb 更新 + Popen 生存で真の RECOVERED. powershell PATH も絶対化.

    DETACHED_PROCESS|CREATE_NO_WINDOW は組み合わせると powershell.exe が即 exit する
    (2026-05-16 verify で判明). CREATE_NEW_PROCESS_GROUP のみで親と独立できる.

    Args:
        prev_hb_mtime: spawn 前の heartbeat mtime (float). 不在なら -1.

    Returns:
        (success, pid_or_reason_str)
    """
    try:
        if sys.platform != 'win32':
            return (False, 'non-Windows platform')
        import os as _os
        system_root = _os.environ.get('SystemRoot', r'C:\Windows')
        powershell_exe = rf'{system_root}\System32\WindowsPowerShell\v1.0\powershell.exe'
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        p = subprocess.Popen(
            [powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-WindowStyle', 'Hidden', '-File', loop_script],
            creationflags=CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError, PermissionError) as e:
        return (False, f'{type(e).__name__}: {e}')

    # Codex Round 2 HIGH (2026-05-16): hb mtime 更新だけでは false positive.
    # `child=<pid>` 付きの hb (= claude 起動成功 signal) を待つ.
    pid = p.pid
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if p.poll() is not None:
            return (False, f'PID={pid} exited early (rc={p.returncode})')
        if hb_file.exists():
            cur_mtime = hb_file.stat().st_mtime
            if cur_mtime > prev_hb_mtime:
                hb_text = hb_file.read_text(encoding='utf-8', errors='ignore')
                if 'child=' in hb_text:
                    return (True, f'PID={pid} (hb has child=)')
        time.sleep(2.0)
    if p.poll() is None:
        return (False, f'PID={pid} alive but no child= hb within 20s')
    return (False, f'PID={pid} exited (rc={p.returncode}) and no child= hb')

try:
    if killswitch_file.exists():
        claude_loop_warning = '[CLAUDE-LOOP] KillSwitch active (claude-loop.STOP). auto-recovery skip.\n\n'
        _log_debug('claude-loop: KillSwitch active, skip')
    elif not hb_file.exists():
        ok, info = _spawn_loop_with_verify(prev_hb_mtime=-1.0)
        if ok:
            claude_loop_warning = f'[CLAUDE-LOOP] DOWN: heartbeat 不在 → auto-recovery 成功 ({info})\n\n'
            _log_debug(f'claude-loop: hb absent, spawn+verify OK {info}')
        else:
            claude_loop_warning = f'[CLAUDE-LOOP] DOWN: heartbeat 不在 + auto-recovery 失敗 ({info})\n\n'
            _log_debug(f'claude-loop: hb absent, spawn FAIL {info}')
    else:
        prev_hb_mtime = hb_file.stat().st_mtime
        hb_age_sec = time.time() - prev_hb_mtime
        hb_content = hb_file.read_text(encoding='utf-8', errors='ignore').strip()
        if hb_age_sec > 60:
            ok, info = _spawn_loop_with_verify(prev_hb_mtime=prev_hb_mtime)
            if ok:
                claude_loop_warning = f'[CLAUDE-LOOP] DOWN: heartbeat stale ({hb_age_sec:.0f}s) → auto-recovery 成功 ({info})\n  last: {hb_content[:100]}\n\n'
                _log_debug(f'claude-loop: stale {hb_age_sec:.0f}s, spawn+verify OK {info}')
            else:
                claude_loop_warning = f'[CLAUDE-LOOP] DOWN: heartbeat stale ({hb_age_sec:.0f}s) + auto-recovery 失敗 ({info})\n\n'
                _log_debug(f'claude-loop: stale {hb_age_sec:.0f}s, spawn FAIL {info}')
        else:
            claude_loop_warning = f'[CLAUDE-LOOP] ALIVE (hb age={hb_age_sec:.0f}s, {hb_content[:100]})\n\n'
            _log_debug(f'claude-loop: alive {hb_age_sec:.0f}s')
except Exception as e:
    # silent skip 防止: Step 4.5 自体の例外も明示 (Q0)
    claude_loop_warning = f'[CLAUDE-LOOP] CHECK FAILED: {type(e).__name__}: {e}\n\n'
    _log_debug(f'claude-loop: check exception {type(e).__name__}: {e}')

# Step 4.7 (W266 2026-06-12): 依頼ボード (user_requests) 未完了件数を注入
# read-only URI で開く (本番 DB を hook から絶対に変更しない)。
# 失敗時も silent 化せず明示 (Q0)。旧 DB (v72 未適用) は no such table → 明示 skip 文言。
request_board_info = ''
try:
    import sqlite3
    _urq_db = root_dir / 'tools' / 'ebay-manager' / 'data' / 'monitor.db'
    if _urq_db.exists():
        _uri = 'file:' + _urq_db.as_posix() + '?mode=ro'
        _conn = sqlite3.connect(_uri, uri=True, timeout=3)
        try:
            _rows = _conn.execute(
                "SELECT status, COUNT(*) FROM user_requests "
                "WHERE status != 'done' GROUP BY status"
            ).fetchall()
            _cnt = dict(_rows)
            _open_n = _cnt.get('open', 0) + _cnt.get('on_hold', 0)
            _wip_n = _cnt.get('in_progress', 0)
            _wait_n = _cnt.get('waiting_user', 0)
            _chk_n = _cnt.get('awaiting_check', 0)
            _total = _open_n + _wip_n + _wait_n + _chk_n
            if _total:
                _titles = _conn.execute(
                    "SELECT id, status, title FROM user_requests "
                    "WHERE status IN ('open','in_progress','waiting_user') "
                    "ORDER BY id DESC LIMIT 5"
                ).fetchall()
                _lines = ''.join(f'\n  #{i} [{s}] {t[:40]}' for i, s, t in _titles)
                request_board_info = (
                    f'[依頼ボード W266] 未完了 {_total} 件 (受付{_open_n}/対応中{_wip_n}/'
                    f'回答待ち{_wait_n}/user確認待ち{_chk_n}) — MonoDeck「依頼ボード」タブから吸い上げ対応せよ'
                    f'{_lines}\n\n'
                )
            else:
                request_board_info = '[依頼ボード W266] 未完了 0 件\n\n'
        finally:
            _conn.close()
    else:
        request_board_info = '[依頼ボード W266] monitor.db 不在で読込 skip\n\n'
except Exception as e:
    request_board_info = f'[依頼ボード W266] 読込失敗 ({type(e).__name__}: {e}) — 手動で確認要\n\n'
    _log_debug(f'request_board: check exception {type(e).__name__}: {e}')

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

# 全文構築 (W127 #3: debug_header を冒頭、#4: sanity_warning を直後)
full_context = f'{debug_header}\n\n' + sanity_warning + stale_prefix + md5_warning + sched_warning + claude_loop_warning + request_board_info + silent_skip_warning + content

# 7000 char strict 上限 enforce (defensive、公式 char 制限は未確認)
# HIGH-3 (code-reviewer 2026-04-30): 「公式 10000 char」と書いていたが一次情報未照合のため
# defensive な内部上限と明確化 (memory_staleness_2026_04_30 整合)
MAX_CHARS = 7000
if len(full_context) > MAX_CHARS:
    truncated_marker = f'\n\n[... {MAX_CHARS} char 上限超過、_NEXT_SESSION.md 直接 Read 推奨: {ns_file} ...]'
    full_context = full_context[:MAX_CHARS - len(truncated_marker)] + truncated_marker

_log_debug(f'OK full_context_len={len(full_context)}')

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
