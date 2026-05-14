"""
在庫監視スタンドアロン実行スクリプト
使用方法: python run_monitor.py
"""
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Windows CP932対策
sys.path.insert(0, str(BASE_DIR))
import utf8_console  # noqa: F401
SETTINGS_FILE = BASE_DIR / "settings.json"
PID_FILE = BASE_DIR / "monitor.pid"
LOG_FILE = BASE_DIR / "monitor.log"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        ],
    )


def check_already_running() -> bool:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Check if process is alive (Windows)
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
        except (ValueError, OSError, AttributeError):
            pass
        PID_FILE.unlink(missing_ok=True)
    return False


def write_pid():
    PID_FILE.write_text(str(os.getpid()))


def cleanup(_sig=None, _frame=None):
    PID_FILE.unlink(missing_ok=True)
    logging.info("Monitor stopped")
    sys.exit(0)


def main():
    setup_logging()

    if not SETTINGS_FILE.exists():
        logging.error("settings.json not found")
        sys.exit(1)

    if check_already_running():
        logging.error("Monitor is already running. Delete monitor.pid to force restart.")
        sys.exit(1)

    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings = json.load(f)

    if not settings.get("discord_webhook_url"):
        logging.warning("discord_webhook_url not configured. Notifications disabled.")

    write_pid()
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    from monitor.database import init_db
    from monitor.runner import start_monitor_loop

    init_db()

    try:
        start_monitor_loop()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
