import subprocess, time

# Test 2: 何もflagなし
print("=== Test 2: no creationflags ===")
p = subprocess.Popen(
    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
     "-WindowStyle", "Hidden", "-File",
     "C:/Users/gucch/.claude/scripts/start-claude-loop.ps1"],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
time.sleep(3)
print(f"poll={p.poll()} pid={p.pid}")
try:
    out, err = p.communicate(timeout=2)
    print(f"exit, stdout: {out[:500]!r}")
    print(f"exit, stderr: {err[:500]!r}")
except subprocess.TimeoutExpired:
    print("still running (script loop healthy)")
    # 念のため kill (test cleanup)
    p.kill()
    p.wait()
    print("test cleanup: killed")

# Test 3: CREATE_NEW_PROCESS_GROUP のみ
print("\n=== Test 3: CREATE_NEW_PROCESS_GROUP only ===")
p = subprocess.Popen(
    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
     "-WindowStyle", "Hidden", "-File",
     "C:/Users/gucch/.claude/scripts/start-claude-loop.ps1"],
    creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
time.sleep(3)
print(f"poll={p.poll()} pid={p.pid}")
try:
    out, err = p.communicate(timeout=2)
    print(f"exit, stdout: {out[:500]!r}")
    print(f"exit, stderr: {err[:500]!r}")
except subprocess.TimeoutExpired:
    print("still running (script loop healthy)")
    p.kill()
    p.wait()
    print("test cleanup: killed")
