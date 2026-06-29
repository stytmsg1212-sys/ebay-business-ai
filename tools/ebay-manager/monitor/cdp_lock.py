"""CDP Chrome 操作の排他制御 (W293 / 2026-06-29).

複数の mutating eBaymag 操作 (apply_site_changes / assign_policy / mirror --apply /
policy_editor save) が同一 Chrome session を競合 mutate するのを防ぐ file-based lock。
Windows msvcrt.locking による OS-level lock (process 死で自動解放)。

acquire_singleton_lock (daily_scheduler.py L1594) の汎用化版。
教訓継承 (H-1~H-3):
  H-1: lock file は 1 byte 以上を持つこと (msvcrt の 0 byte 誤成功回避)
  H-2: file system 操作失敗を明示 error で surface (Q0 silent skip 禁止)
  H-3: binary mode 'rb+' で newline translation 回避

汎用 advisory file lock: cdp_lock 以外の用途でも lock_path 引数を渡すことで再利用可能。
既定 (lock_path=None) は CDP_LOCK_FILE を使い、既存 CDP caller の挙動は完全不変。
"""
from __future__ import annotations

import errno as _errno
import msvcrt
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

# Windows msvcrt.locking が「lock 競合」で返す errno 値 (E: 真の OSError と区別する)
_LOCK_BUSY_ERRNOS = frozenset({_errno.EACCES, _errno.EDEADLK})

CDP_LOCK_FILE = Path(__file__).parent.parent / "data" / "cdp_chrome.lock"


class LockBusy(Exception):
    """CDP lock が他のプロセス/スレッドに占有中 (blocking=False 時に raise)。"""


@contextmanager
def acquire(blocking: bool = True, timeout: float = 0.0, *,
            lock_path: Optional[Path] = None) -> Generator[None, None, None]:
    """排他ロックを取得するコンテキストマネージャ。

    Args:
        blocking: True = タイムアウトまでブロック (mutating 操作用)。
                  False = 即時 skip (heartbeat 用)、timeout は無視。
        timeout: blocking=True 時の最大待機秒数。0 以下は ValueError
                 (旧実装「0.0 = 31 年ブロック」のバグを修正 / D)。
                 目安: apply=300s / assign=200s / policy_editor=240s。
        lock_path: ロックファイルのパス (keyword-only)。省略時は CDP_LOCK_FILE。
                   別パスを渡すことで CDP 以外の排他制御にも再利用可能。

    Raises:
        ValueError: blocking=True かつ timeout <= 0 (D)。
        LockBusy: blocking=False でロック取得不能、または blocking=True でタイムアウト。
        OSError: lock file の open / mkdir 失敗 (Q0 silent skip 禁止、H-2)、
                 または lock 競合以外の真の OSError (errno not in EACCES/EDEADLK) (E)。

    Note:
        プロセス死で OS が自動解放 (stale PID 判定不要、race condition フリー)。
        再入不可 (non-reentrant): 同一プロセスからの nested acquire は禁止。
        msvcrt は同一 byte を別 handle で self-deny し timeout 後に LockBusy になる (D)。
        ブロック終了時に finally で必ず release (例外発生時も)。
    """
    _path = lock_path if lock_path is not None else CDP_LOCK_FILE
    if blocking and timeout <= 0:
        raise ValueError(
            f"blocking=True の場合 timeout > 0 が必須 (timeout={timeout}). "
            "非ブロック即時試行は blocking=False を使用してください。"
        )
    # H-2: file system 操作失敗を明示 error で surface
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        # H-1: lock byte を確実に reserve するため 1 byte 書込み
        if not _path.exists() or _path.stat().st_size == 0:
            _path.write_bytes(b'\x00')
        # H-3: binary mode で newline translation 回避
        f = open(_path, 'rb+')  # noqa: WPS515
    except OSError as e:
        raise OSError(
            f"lock file open 失敗 ({_path}): {type(e).__name__}: {e}. "
            "data/ ディレクトリの permission を確認してください。"
        ) from e

    acquired = False
    try:
        if not blocking:
            # 非ブロック: 即時取得を試みて失敗なら LockBusy
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as e:
                if e.errno not in _LOCK_BUSY_ERRNOS:
                    f.close()
                    raise  # 真の OSError は再 raise (E: 誤分類防止)
                f.close()
                raise LockBusy("CDP lock 取得失敗 (busy)") from e
        else:
            # ブロック: timeout 秒まで 0.1s ポーリング (D: timeout > 0 が保証済)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as e:
                    if e.errno not in _LOCK_BUSY_ERRNOS:
                        f.close()
                        raise  # 真の OSError は再 raise (E: 誤分類防止)
                    if time.monotonic() >= deadline:
                        f.close()
                        raise LockBusy(
                            f"CDP lock タイムアウト ({timeout:.0f}s)"
                        ) from None
                    time.sleep(0.1)
        yield
    finally:
        if acquired:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # 既に解放済 or プロセス終了時は無視
        if not f.closed:
            f.close()


def is_held(*, lock_path: Optional[Path] = None) -> bool:
    """指定ロックが他プロセスに保持されているか確認 (非ブロック試行)。lock_path 省略時は CDP_LOCK_FILE。"""
    try:
        with acquire(blocking=False, lock_path=lock_path):
            return False
    except LockBusy:
        return True
    except OSError:
        return False  # ファイル不在 = lock されていない
