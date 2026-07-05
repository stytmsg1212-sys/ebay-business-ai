# -*- coding: utf-8 -*-
"""依頼ボード#53 を awaiting_check 化 (正規 API 経由)。

原因: EbayManager-SchedulerWatchdog タスクが 5 分毎に powershell.exe を Interactive
logon で起動しており、-WindowStyle Hidden でも一瞬コンソールウィンドウが生成されて
タイピング中のフォーカスを奪っていた。
対応: Task Scheduler の Action を wscript.exe 経由 (watchdog_invisible.vbs) に変更。
wscript.exe はコンソールを持たないため WScript.Shell.Run(...,0,False) で子プロセスの
powershell.exe を起動してもウィンドウが生成されない。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import set_user_request_status  # noqa: E402

VERIFY = (
    "数時間〜1日、通常業務でタイピング中に画面が一瞬立ち上がる現象が"
    "再発しないことを確認 → 確認完了ボタン"
)

NOTE = (
    "原因はタイポではなく、EbayManager-SchedulerWatchdog タスク (5分間隔) が"
    "Interactive logon で powershell.exe を直接起動しており、-WindowStyle Hidden でも"
    "コンソールウィンドウが一瞬生成されフォーカスを奪っていたこと。"
    "既存の scheduler autostart VBS と同じパターンで watchdog_invisible.vbs を新設し、"
    "Task Scheduler の Action を wscript.exe 経由に変更 (Triggers/Settings は変更なし、"
    "diff で Action のみの変更を確認済)。手動実行で LastTaskResult=0、"
    "watchdog.log に新規エントリを確認し watchdog 本来の機能(scheduler死活監視)が"
    "壊れていないことも実証。10分間の自然実行サイクルも正常動作を確認。"
)

ok = set_user_request_status(
    53, "awaiting_check", note=NOTE, verify_steps=VERIFY, author="assistant",
)
print(f"board#53 -> awaiting_check: {ok}")
