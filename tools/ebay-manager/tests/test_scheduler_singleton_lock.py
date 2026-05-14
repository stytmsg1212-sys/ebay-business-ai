#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W95: scheduler singleton lock test.

Windows msvcrt.locking ベースの多重起動防止が以下を満たすことを verify:
1. lock 取得成功時 = file handle が返り PID が別ファイルに書かれる
2. lock 競合時 = sys.exit(1) で終了 + 診断用 PID 読み取り (別ファイル)
3. lock 解放後 = 別 instance が再取得可能
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# daily_scheduler は project root 直下なので path 追加
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def test_acquire_singleton_lock_succeeds_when_no_other_instance(tmp_path, monkeypatch):
    """初回起動時: lock 取得成功 + PID file に PID 書込み + handle 返却."""
    import daily_scheduler as ds

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    handle = ds.acquire_singleton_lock()
    try:
        assert handle is not None
        assert not handle.closed
        assert fake_lock.exists()
        # PID は別ファイルに書かれていること
        assert fake_pid.exists()
        assert fake_pid.read_text(encoding="ascii").strip() == str(os.getpid())
    finally:
        handle.close()


def test_acquire_singleton_lock_exits_when_locked_by_another_handle(tmp_path, monkeypatch):
    """別 instance が既に lock 保持中: sys.exit(1) で終了 + PID 読み取れる."""
    import daily_scheduler as ds
    import msvcrt

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    # 先に lock を取得 (= 既存 instance 模擬) + 別 PID 書込み
    fake_lock.touch()
    fake_pid.write_text("99999", encoding="ascii")
    f1 = open(fake_lock, "r+")
    msvcrt.locking(f1.fileno(), msvcrt.LK_NBLCK, 1)

    try:
        # 第 2 instance の取得試行 → SystemExit(1)
        with pytest.raises(SystemExit) as excinfo:
            ds.acquire_singleton_lock()
        assert excinfo.value.code == 1
        # PID file は変更されない (= 99999 のまま) ことを verify
        # 注: ロック失敗時は acquire 側が PID を上書きしない設計
        assert fake_pid.read_text(encoding="ascii").strip() == "99999"
    finally:
        msvcrt.locking(f1.fileno(), msvcrt.LK_UNLCK, 1)
        f1.close()


def test_acquire_singleton_lock_reusable_after_release(tmp_path, monkeypatch):
    """lock 解放後は別 instance が再取得可能 (process 死を模擬する場合の挙動)."""
    import daily_scheduler as ds

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    # 1 回目 acquire → close (= 解放)
    handle1 = ds.acquire_singleton_lock()
    handle1.close()

    # 2 回目 acquire は成功する (PID 上書き済)
    handle2 = ds.acquire_singleton_lock()
    try:
        assert handle2 is not None
        assert not handle2.closed
        # 2 回目 acquire 後も PID 自身が書かれている
        assert fake_pid.read_text(encoding="ascii").strip() == str(os.getpid())
    finally:
        handle2.close()


def test_pid_file_unaffected_when_lock_held_externally(tmp_path, monkeypatch):
    """ロック取得失敗時は PID file を上書きしない (race condition 予防)."""
    import daily_scheduler as ds
    import msvcrt

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    # 既存 instance が lock + PID 書込み (lock byte を確保するため 1 byte 書込み)
    fake_lock.write_bytes(b"\x00")
    fake_pid.write_text("12345", encoding="ascii")
    f1 = open(fake_lock, "rb+")
    msvcrt.locking(f1.fileno(), msvcrt.LK_NBLCK, 1)

    try:
        with pytest.raises(SystemExit):
            ds.acquire_singleton_lock()
        # 既存 PID 12345 が残っている = 上書きしていない
        assert fake_pid.read_text(encoding="ascii").strip() == "12345"
    finally:
        msvcrt.locking(f1.fileno(), msvcrt.LK_UNLCK, 1)
        f1.close()


def test_lock_file_has_at_least_one_byte_for_locking(tmp_path, monkeypatch):
    """H-1: msvcrt.locking が確実に動作するため lock file は 1 byte 以上を持つ."""
    import daily_scheduler as ds

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    handle = ds.acquire_singleton_lock()
    try:
        assert fake_lock.stat().st_size >= 1, "lock byte が確保されていない"
    finally:
        handle.close()


def test_lock_file_open_failure_logs_and_exits(tmp_path, monkeypatch, caplog):
    """H-2: lock file open 失敗時も Q0 silent skip せず logger.error + sys.exit(1)."""
    import logging
    import daily_scheduler as ds

    # 実在するファイルを SCHEDULER_LOCK_FILE.parent に指定 → mkdir が NotADirectoryError
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a directory", encoding="ascii")
    fake_lock = blocker / "scheduler.lock"
    fake_pid = blocker / "scheduler.pid"
    monkeypatch.setattr(ds, "SCHEDULER_LOCK_FILE", fake_lock)
    monkeypatch.setattr(ds, "SCHEDULER_PID_FILE", fake_pid)

    with pytest.raises(SystemExit) as excinfo:
        with caplog.at_level(logging.ERROR, logger=ds.logger.name):
            ds.acquire_singleton_lock()
    assert excinfo.value.code == 1
    # 「多重起動防止が機能しません」を含む error log が出ること = Q0 通知
    assert any("singleton lock file open 失敗" in r.message for r in caplog.records), \
        f"silent skip risk: error log 未出力 / records={[r.message for r in caplog.records]}"


def test_two_subprocesses_only_one_acquires_lock(tmp_path):
    """W95 production reproducer: subprocess 2 並列起動で 1 つだけ lock 取得.

    本 W95 が実際に防ぐ 5/3 早朝事故 (PID 65408 / 1308 / 82880 の 3 並列稼働) を
    pytest で reproduce + 物理 BLOCK 確認.
    """
    import subprocess
    import textwrap
    import time

    fake_lock = tmp_path / "scheduler.lock"
    fake_pid = tmp_path / "scheduler.pid"

    helper = tmp_path / "helper.py"
    helper.write_text(textwrap.dedent(f'''
        import sys
        import time
        from pathlib import Path
        sys.path.insert(0, r"{_ROOT}")
        import daily_scheduler as ds
        ds.SCHEDULER_LOCK_FILE = Path(r"{fake_lock}")
        ds.SCHEDULER_PID_FILE = Path(r"{fake_pid}")
        h = ds.acquire_singleton_lock()
        print("ACQUIRED", flush=True)
        time.sleep(15)
    '''), encoding="utf-8")

    p1 = subprocess.Popen(
        [sys.executable, str(helper)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        # p1 が ACQUIRED を出力するのを最大 10 秒待機
        deadline = time.time() + 10.0
        line = ""
        while time.time() < deadline:
            line = p1.stdout.readline()
            if "ACQUIRED" in line:
                break
            if p1.poll() is not None:
                # p1 が予期せず exit した場合は test 失敗
                stderr_dump = p1.stderr.read()
                pytest.fail(f"p1 が起動失敗: returncode={p1.returncode}, stderr={stderr_dump!r}")
        assert "ACQUIRED" in line, "p1 が lock 取得 ACQUIRED ログを出さなかった"

        # p2 は lock 取得失敗で exit code 1 になるはず
        p2 = subprocess.run(
            [sys.executable, str(helper)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert p2.returncode == 1, \
            f"p2 が二重 lock 取得した可能性 (returncode={p2.returncode}, " \
            f"stdout={p2.stdout!r}, stderr={p2.stderr!r})"
    finally:
        p1.terminate()
        try:
            p1.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p1.kill()
            p1.wait(timeout=5)
