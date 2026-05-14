"""Q0 audit: silent skip / fake success パターン全洗い出し.

検出パターン:
  A1_except_return_silent: except 内で即 return / return None (logger 記録無)
  A2_except_pass: except: pass / except Exception: pass
  A3_print_return_in_except: except 内で print のみ → return (pythonw で完全 silent)
  A4_success_true_in_except: except ブロックで success: True 返す (偽装成功)
  A5_print_to_stderr: print(file=sys.stderr) (pythonw で [Errno 22])

scope: tools/ebay-manager 配下の .py (tests / __pycache__ / .tmp.* 除外).
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

EXCLUDE = ('/.git/', '/__pycache__/', '/tests/', '/.venv/', '/venv/', '.tmp.')
ROOT = Path(__file__).resolve().parent.parent

candidates = {
    'A1_except_return_silent': [],
    'A2_except_pass': [],
    'A3_print_return_in_except': [],
    'A4_success_true_in_except': [],
    'A5_print_to_stderr': [],
}


def _norm(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace('\\', '/')


def audit() -> None:
    for p in ROOT.rglob('*.py'):
        sp = _norm(p)
        if any(x in '/' + sp for x in EXCLUDE):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        lines = text.split('\n')

        # A5: print(file=sys.stderr) — _safe_stderr_print 系除外
        for i, line in enumerate(lines):
            if re.search(r'print\s*\([^)]*file\s*=\s*sys\.stderr', line):
                if '_safe_stderr_print' in line or 'def _safe_stderr' in (lines[i-1] if i > 0 else ''):
                    continue
                candidates['A5_print_to_stderr'].append((sp, i+1, line.strip()[:100]))

        # except ブロック スキャン
        for i, line in enumerate(lines):
            m = re.match(r'^(\s*)except\b', line)
            if not m:
                continue
            indent = m.group(1)
            body_indent = indent + '    '
            body = []
            for j in range(i+1, min(i+10, len(lines))):
                ln = lines[j]
                if ln.strip() == '':
                    continue
                if not ln.startswith(body_indent):
                    break
                body.append((j+1, ln.strip()))
            if not body:
                continue
            stmts = [s for _, s in body]

            # A2: except: pass / except Exception: pass (body は pass のみ, comment 含まず)
            if len(stmts) == 1 and stmts[0] == 'pass':
                if 'logger' not in line and 'logging' not in line:
                    candidates['A2_except_pass'].append((sp, body[0][0], line.strip()))
                continue

            # A1: 即 return (logger 記録無し)
            first_real = stmts[0] if stmts else ''
            has_log = any('logger' in s or 'logging' in s for s in stmts[:3])
            if first_real in ('return', 'return None') and not has_log:
                candidates['A1_except_return_silent'].append((sp, body[0][0], f"{line.strip()} → {first_real}"))
                continue

            # A3: print(...) followed by return (no logger)
            for k in range(len(body) - 1):
                line_n, stmt_n = body[k]
                next_stmt = body[k+1][1]
                if stmt_n.startswith('print(') and (
                    next_stmt in ('return', 'return None') or next_stmt.startswith('return ')
                ):
                    if not has_log:
                        candidates['A3_print_return_in_except'].append(
                            (sp, line_n, f"{stmt_n[:60]} ; {next_stmt[:60]}")
                        )
                    break

            # A4: success: True in except return
            for line_n, stmt_n in body:
                if 'success' in stmt_n and 'True' in stmt_n and ('return' in stmt_n):
                    candidates['A4_success_true_in_except'].append((sp, line_n, stmt_n[:120]))
                    break

    print("=" * 70)
    print("Q0 Silent Skip Audit Report")
    print("=" * 70)

    total = sum(len(v) for v in candidates.values())
    print(f"\n総件数: {total}")
    for cat, items in candidates.items():
        print(f"\n[{cat}] {len(items)} 件")
        for f, ln, snippet in items[:30]:
            print(f"  {f}:{ln} | {snippet}")
        if len(items) > 30:
            print(f"  ... +{len(items)-30} 件 (省略)")


if __name__ == '__main__':
    audit()
