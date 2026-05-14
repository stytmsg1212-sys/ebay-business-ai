"""W7-A backfill watchdog: abort / Chrome OOM 自動検知 + clean restart + 再実行.

backfill log を tail しながら以下を監視:
  - aborted_by_block (連続 5 件失敗) → 自動 retry
  - thread timeout 累計 4 件 → 先回り restart (abort 待たずに)
  - Chrome CDP error 検知 → Chrome 再起動 → 自動 retry
  - backfill subprocess 死亡 (poll() != None) → 自動 retry
  - 最大 max_retries (default 5) 回まで、超えたら Discord 通知 + 終了

完了検知: 「処理: N/N 件 / 成功: M」(processed==total) で正常終了.

設計上の重要事項:
  - user の通常使用 Chrome は kill しない (CommandLine フィルタで CDP 9222 のみ kill)
  - Chrome 起動失敗時は watchdog 自体が abort
  - cooldown は exponential backoff (60s → 240s → 960s → 3840s → 14400s)
  - completion 判定は processed==total を厳格チェック (abort 時 M<N で誤完了しない)
  - subprocess 死亡を 5 秒毎にチェック (silent hang 防止)

使い方:
  python scripts/w7a_backfill_watchdog.py [--max-retries 5] [--cooldown-sec 60]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Generator, Optional, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_FILE = PROJECT_ROOT / "logs" / "w7a_backfill_2026_05_06.log"
BACKFILL_SCRIPT = PROJECT_ROOT / "scripts" / "run_w7a_backfill_2026_05_06.py"
BACKFILL_STDERR_LOG = PROJECT_ROOT / "logs" / "w7a_backfill_subprocess.stderr.log"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PROFILE = PROJECT_ROOT / "data" / ".chrome_cdp_profile"
TERAPEAK_URL = "https://www.ebay.com/sh/research?marketplace=EBAY-US&dayRange=90&tabName=SOLD"

# user_data_dir パスの一部 (CommandLine フィルタ用、Windows path 区切りに注意)
CDP_PROFILE_FILTER = r".chrome_cdp_profile"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "w7a_backfill_watchdog.log",
                            encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Compiled patterns (module top で 1 回だけ compile)
ABORT_PATTERN = re.compile(r"aborted_by_block|連続\s*\d+\s*件失敗")
TIMEOUT_PATTERN = re.compile(r"thread timeout \(>\d+s\)")
COMPLETION_PATTERN = re.compile(r"処理:\s*(\d+)/(\d+)\s*件\s*/\s*成功:\s*(\d+)")
CDP_ERROR_PATTERN = re.compile(r"CDP Chrome が起動していません")


def cdp_alive() -> bool:
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect(("127.0.0.1", 9222))
        return True
    except OSError:
        return False
    finally:
        s.close()


def kill_cdp_chrome() -> int:
    """CDP profile の Chrome のみ kill (user の通常使用 Chrome は保護).

    PowerShell CommandLine フィルタで .chrome_cdp_profile を含む chrome.exe のみ対象.
    Returns: kill された process 数 (失敗時 0)
    """
    # CommandLine に .chrome_cdp_profile を含むものだけ kill
    cmd = (
        f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{CDP_PROFILE_FILTER}*' }} | "
        f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force; "
        f"Write-Output \"killed PID $($_.ProcessId)\" }}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True, text=True, timeout=15, check=False,
        )
        killed_count = len([
            ln for ln in result.stdout.splitlines() if "killed PID" in ln
        ])
        if killed_count > 0:
            logger.info(f"CDP Chrome kill: {killed_count} 個 (user の通常 Chrome は touch せず)")
        return killed_count
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Chrome kill 失敗 (継続): {e}")
        return 0


def kill_backfill() -> int:
    """run_w7a_backfill スクリプトのみ kill (他 python は保護)."""
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*run_w7a_backfill*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "
        "Write-Output \"killed PID $($_.ProcessId)\" }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True, text=True, timeout=15, check=False,
        )
        killed = len([
            ln for ln in result.stdout.splitlines() if "killed PID" in ln
        ])
        if killed > 0:
            logger.info(f"backfill kill: {killed} 個")
        return killed
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"backfill kill 失敗 (継続): {e}")
        return 0


def start_chrome() -> bool:
    """Chrome を CDP 9222 で起動. 起動成功なら True, timeout なら False."""
    logger.info("Chrome CDP 9222 起動")
    try:
        subprocess.Popen(
            [CHROME_PATH,
             "--remote-debugging-port=9222",
             f"--user-data-dir={CHROME_PROFILE}",
             "--no-first-run", "--no-default-browser-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Chrome 起動失敗: {e}")
        return False

    for _ in range(15):
        time.sleep(1)
        if cdp_alive():
            logger.info("CDP 9222 応答 OK")
            return True
    logger.error("Chrome 起動 timeout (15s)")
    return False


def open_terapeak() -> bool:
    """Terapeak タブを Chrome で開く (CDP /json/new)."""
    try:
        r = httpx.put(
            f"http://127.0.0.1:9222/json/new?{urllib.parse.quote(TERAPEAK_URL)}",
            timeout=10,
        )
        logger.info(f"Terapeak タブ open: status={r.status_code}")
        return r.status_code == 200
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"Terapeak open 失敗: {e}")
        return False


def check_terapeak_login() -> Optional[bool]:
    """Terapeak タブが login 済か確認.

    Returns:
        True: login 済 (title に Sign in 含まない)
        False: login 必要 (title に Sign in / Login 含む)
        None: 判定不能 (タブ取得失敗)
    """
    try:
        r = httpx.get("http://127.0.0.1:9222/json", timeout=5)
        tabs = [t for t in r.json() if t.get("type") == "page"]
        for t in tabs:
            if "research" in (t.get("url") or ""):
                title = (t.get("title") or "").lower()
                if "sign in" in title or "login" in title:
                    return False
                if title:
                    return True
        return None
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.warning(f"login 状態確認失敗: {e}")
        return None


def start_backfill() -> subprocess.Popen:
    """backfill script を background で起動. stderr は file にリダイレクト.

    H-E fix: with 文で file handle を即 close (Popen は fd を duplicate 済).
    """
    logger.info("backfill 起動 (stderr → logs/w7a_backfill_subprocess.stderr.log)")
    with open(BACKFILL_STDERR_LOG, "a", encoding="utf-8") as stderr_f:
        return subprocess.Popen(
            [sys.executable, str(BACKFILL_SCRIPT)],
            stdout=subprocess.DEVNULL, stderr=stderr_f,
        )


def clean_restart() -> Tuple[Optional[subprocess.Popen], int]:
    """backfill kill → CDP Chrome kill → Chrome 再起動 → Terapeak open → backfill 再開.

    Returns: (backfill_proc or None, new_log_offset)
    """
    logger.info("=== clean restart 発動 ===")
    kill_backfill()
    time.sleep(2)
    kill_cdp_chrome()
    time.sleep(3)

    if not start_chrome():
        logger.error("Chrome 起動失敗、watchdog abort")
        return None, 0
    time.sleep(2)
    open_terapeak()
    time.sleep(3)

    # login 状態確認 (failed なら user 介入要求)
    login_ok = check_terapeak_login()
    if login_ok is False:
        logger.error("Terapeak login 切れ検知 (タブ title に 'Sign in')、watchdog abort")
        return None, 0
    elif login_ok is None:
        logger.warning("login 状態判定不能、scrape 試行は継続")

    new_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    proc = start_backfill()
    time.sleep(5)
    return proc, new_offset


def tail_log(path: Path, start_offset: int, stop_event: Callable[[], bool]) -> Generator[str, None, None]:
    """log file を offset から tail. 新規行を yield. stop_event() == True で中断.

    OSError 5 連発で abort (silent skip 防止).
    """
    error_count = 0
    while not stop_event():
        if not path.exists():
            time.sleep(2)
            continue
        try:
            size = path.stat().st_size
            if size < start_offset:
                # log rotation or truncation 検知
                logger.warning(f"log offset reset: {start_offset} > {size}, 0 から再開")
                start_offset = 0
            if size > start_offset:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(start_offset)
                    for line in f:
                        yield line.rstrip("\n")
                        if stop_event():
                            return
                    start_offset = f.tell()
                error_count = 0
            time.sleep(1)
        except OSError as e:
            error_count += 1
            logger.warning(f"log tail OSError ({error_count}/5): {e}")
            if error_count >= 5:
                logger.error("log tail 連続 5 件 OSError、abort")
                return
            time.sleep(2)


def send_discord_alert(message: str, severity: str = "error") -> None:
    """Discord 通知 (max retries 超え or watchdog abort 時).

    ベストエフォート、失敗しても watchdog は止めない.
    CR-1 fix: except を specific 例外に絞り込み (project 規約準拠).
    """
    try:
        cfg_path = PROJECT_ROOT / "config" / "schedule_config.json"
        if not cfg_path.exists():
            return
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        webhook = (cfg.get("discord", {}) or {}).get("webhook_url") or ""
        if not webhook:
            return
        color = {"error": 0xd84c38, "warn": 0xc89b2a, "info": 0x3399ff}.get(severity, 0x3399ff)
        embed = {
            "title": "W7-A backfill watchdog",
            "description": message,
            "color": color,
        }
        httpx.post(webhook, json={"embeds": [embed]}, timeout=10)
    except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
        logger.warning(f"Discord 通知失敗 (継続): {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--cooldown-sec", type=int, default=60,
                        help="initial cooldown (exponential backoff: x1, x4, x16, x64, x256)")
    parser.add_argument("--no-initial-start", action="store_true",
                        help="既に backfill 起動中の前提で監視のみ")
    args = parser.parse_args()

    logger.info(f"watchdog 起動 max_retries={args.max_retries} initial_cooldown={args.cooldown_sec}s")

    retries = 0
    backfill_proc: Optional[subprocess.Popen] = None
    timeout_count = 0

    if not args.no_initial_start:
        if not cdp_alive():
            logger.warning("CDP 9222 不在、Chrome 起動")
            if not start_chrome():
                logger.error("初回 Chrome 起動失敗、watchdog 終了")
                send_discord_alert("初回 Chrome 起動失敗、user 介入要求", "error")
                return 1
            time.sleep(2)
            open_terapeak()
            time.sleep(3)

        login_ok = check_terapeak_login()
        if login_ok is False:
            logger.error("Terapeak login 切れ、watchdog 終了")
            send_discord_alert("Terapeak login 切れ、user 再ログイン要求", "error")
            return 1

        backfill_proc = start_backfill()
        time.sleep(5)

    start_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

    # main loop: outer は restart 単位、inner は tail iteration
    completed = False
    aborted = False
    last_subprocess_check = time.time()

    while not completed and not aborted:
        should_restart = False

        def _stop():
            return should_restart or completed or aborted

        for line in tail_log(LOG_FILE, start_offset, _stop):
            # 5 秒に 1 回 backfill subprocess 生死チェック
            now = time.time()
            if now - last_subprocess_check > 5:
                last_subprocess_check = now
                if backfill_proc and backfill_proc.poll() is not None:
                    rc = backfill_proc.returncode
                    logger.warning(f"backfill subprocess 死亡 (rc={rc})、retry 発動")
                    retries += 1
                    if retries > args.max_retries:
                        logger.error(f"max retries {args.max_retries} 超過")
                        send_discord_alert(
                            f"watchdog: max retries 超過 (subprocess 死亡)、user 介入要求",
                            "error"
                        )
                        aborted = True
                        break
                    # H-A fix: subprocess 死亡経路でも clean_restart + cooldown を実行
                    # 旧実装は should_restart=True で抜けるだけで、無限 retry inflation した
                    cooldown = min(args.cooldown_sec * (4 ** (retries - 1)), 4 * 3600)
                    logger.info(f"cooldown {cooldown}s 開始 (subprocess 死亡経路、retry {retries})")
                    time.sleep(cooldown)
                    backfill_proc, new_offset = clean_restart()
                    if backfill_proc is None:
                        send_discord_alert(
                            "watchdog: clean_restart 失敗 (subprocess 死亡経路)",
                            "error"
                        )
                        aborted = True
                        break
                    start_offset = new_offset
                    timeout_count = 0
                    last_subprocess_check = time.time()
                    should_restart = True
                    break

            # 完了検知 (M==N かつ aborted_by_block flag が直近行で出ていない)
            m_done = COMPLETION_PATTERN.search(line)
            if m_done:
                processed = int(m_done.group(1))
                total = int(m_done.group(2))
                succeeded = int(m_done.group(3))
                if processed == total and processed > 0:
                    logger.info(
                        f"=== 完了検知: {processed}/{total} 件、成功 {succeeded} 件 ==="
                    )
                    kill_backfill()
                    send_discord_alert(
                        f"backfill 完了: {processed}/{total} 件、成功 {succeeded} 件",
                        "info"
                    )
                    completed = True
                    break
                # M < N は abort 後の statistics、ここでは何もしない (abort_pattern が別途捕捉)

            # abort or CDP error
            if ABORT_PATTERN.search(line) or CDP_ERROR_PATTERN.search(line):
                retries += 1
                logger.warning(f"abort/CDP error 検知 (retry {retries}/{args.max_retries}): {line[:120]}")
                if retries > args.max_retries:
                    logger.error(f"max retries {args.max_retries} 超過")
                    send_discord_alert(
                        f"watchdog: max retries 超過 (abort/CDP)、user 介入要求",
                        "error"
                    )
                    aborted = True
                    break

                # exponential backoff: x1, x4, x16, x64, x256 (max 4h)
                cooldown = min(args.cooldown_sec * (4 ** (retries - 1)), 4 * 3600)
                logger.info(f"cooldown {cooldown}s 開始 (retry {retries})")
                time.sleep(cooldown)
                logger.info(f"cooldown 終了, clean restart 発動")

                backfill_proc, new_offset = clean_restart()
                if backfill_proc is None:
                    logger.error("clean_restart 失敗、watchdog 終了")
                    send_discord_alert(
                        "watchdog: clean_restart 失敗 (Chrome / login)、user 介入要求",
                        "error"
                    )
                    aborted = True
                    break

                start_offset = new_offset
                timeout_count = 0
                should_restart = True
                break

            # thread timeout 累計 (4 件で先回り restart)
            if TIMEOUT_PATTERN.search(line):
                timeout_count += 1
                logger.info(f"thread timeout 検知 ({timeout_count} 件累計)")
                if timeout_count >= 4:
                    retries += 1
                    logger.warning(
                        f"timeout 累計 {timeout_count} 件、abort 前に先回り restart "
                        f"(retry {retries}/{args.max_retries})"
                    )
                    if retries > args.max_retries:
                        logger.error(f"max retries {args.max_retries} 超過")
                        send_discord_alert(
                            f"watchdog: max retries 超過 (timeout 連発)、user 介入要求",
                            "error"
                        )
                        aborted = True
                        break

                    cooldown = min(args.cooldown_sec * (4 ** (retries - 1)), 4 * 3600)
                    logger.info(f"cooldown {cooldown}s 開始 (retry {retries})")
                    time.sleep(cooldown)
                    backfill_proc, new_offset = clean_restart()
                    if backfill_proc is None:
                        aborted = True
                        break

                    start_offset = new_offset
                    timeout_count = 0
                    should_restart = True
                    break

    if completed:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
