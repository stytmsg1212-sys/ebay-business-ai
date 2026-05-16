"""MonoDeck (eBay Manager Streamlit) launcher.

Why this wrapper exists
-----------------------
Python 3.13's ``platform.win32_ver()`` queries WMI
(``SELECT ... FROM Win32_OperatingSystem``) with no timeout. On this machine
the WMI subsystem intermittently hangs for minutes -- the same root cause as
the documented "Get-CimInstance / Get-WinEvent 30s+" slowness. Streamlit calls
``platform.system()`` at import time (``streamlit/env_util.py``), so a bare
``streamlit run`` hangs during bootstrap with zero output and never binds the
server port.

Setting ``platform._wmi = None`` makes ``platform._wmi_query()`` raise
``OSError`` immediately, so ``platform._win32_ver()`` takes its sanctioned
non-WMI fallback (``sys.getwindowsversion()`` + registry) which is fast and
still returns correct version info. This MUST run before ``import streamlit``.

The headless scheduler (daily_scheduler.py) does not import streamlit and is
unaffected, so this fix is scoped to the MonoDeck UI launch only.
"""
import platform

platform._wmi = None  # WMI hang workaround -- see module docstring

import sys


def main() -> None:
    extra = sys.argv[1:]
    sys.argv = [
        "streamlit", "run", "app.py",
        "--server.headless", "true",
        "--server.port", "8501",
        "--browser.gatherUsageStats", "false",
        *extra,
    ]
    from streamlit.web.cli import main as st_main

    st_main()


if __name__ == "__main__":
    main()
