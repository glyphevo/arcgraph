"""Operation-level file lock for ArcGraph build/reindex/import operations.

``ArcGraphOperationLock`` sits above the existing ``GraphStoreWriter._write_lock``
(which only covers the final write phase) and prevents two full build, reindex,
or trace-import operations from running concurrently — whether triggered from
CLI, backend API, or any other caller.

Lock file: ``<output_dir>/operation.lock``

Stale-lock recovery: if the lock file exists but the PID inside it belongs to a
dead process, the lock is automatically cleaned up and re-acquired.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import time

_LOCK_GRACE_SECONDS = 5.0
_LockErrorFactory = Callable[[int | None], BaseException]


class ArcGraphOperationInProgress(RuntimeError):
    """Raised when another build/reindex/import operation holds the lock."""

    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid
        detail = f" (PID {pid})" if pid else ""
        super().__init__(
            f"Another ArcGraph operation is already in progress{detail}. "
            "Wait for it to finish or remove the stale lock file."
        )


def _process_alive(pid: int) -> bool:
    """Check whether *pid* is a running process.

    Works on both Windows and POSIX.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        # On POSIX, signal 0 checks process existence without sending a signal.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        return True
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    """Return whether a Windows PID is alive without signaling it."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        # ERROR_ACCESS_DENIED means the process exists but cannot be queried.
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def arcgraph_operation_lock(output_dir: Path) -> Iterator[None]:
    """Acquire an exclusive operation lock for ``output_dir``.

    Usage::

        with arcgraph_operation_lock(output_dir):
            # perform build / reindex / trace import
            ...

    Raises :class:`ArcGraphOperationInProgress` if another operation holds
    the lock (and the holding process is still alive).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "operation.lock"

    _try_acquire(lock_path, ArcGraphOperationInProgress)

    try:
        yield
    finally:
        _release_if_owner(lock_path)


@contextmanager
def arcgraph_pid_file_lock(
    lock_path: Path,
    *,
    error_factory: _LockErrorFactory,
) -> Iterator[None]:
    """Acquire a PID-based file lock at an explicit path.

    This is used by shorter-lived internal locks such as ``write.lock`` while
    preserving the same stale-lock recovery behavior as operation locks.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _try_acquire(lock_path, error_factory)
    try:
        yield
    finally:
        _release_if_owner(lock_path)


def _try_acquire(lock_path: Path, error_factory: _LockErrorFactory) -> None:
    """Try to create the lock file, recovering from stale locks once."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return
    except FileExistsError:
        pass

    # Lock file exists — check whether the holder is still alive.
    pid = _read_lock_pid(lock_path)
    if _lock_is_active(lock_path, pid):
        raise error_factory(pid)

    # Stale lock — clean up and retry exactly once.
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise error_factory(pid) from exc

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError as exc:
        # Race: another process grabbed the lock between our unlink and open.
        raise error_factory(None) from exc


def arcgraph_operation_status(output_dir: Path) -> dict[str, object]:
    """Return lock status for UI/API preflight checks."""
    lock_path = output_dir / "operation.lock"
    if not lock_path.exists():
        return {"locked": False, "stale": False, "pid": None}
    pid = _read_lock_pid(lock_path)
    active = _lock_is_active(lock_path, pid)
    return {
        "locked": active,
        "stale": not active,
        "pid": pid,
        "path": str(lock_path),
    }


def _release_if_owner(lock_path: Path) -> None:
    """Remove the lock only when it is still owned by this process."""
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError:
        return
    if content != str(os.getpid()):
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _lock_is_active(lock_path: Path, pid: int | None) -> bool:
    if pid is not None:
        return _process_alive(pid)
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    return age_seconds < _LOCK_GRACE_SECONDS


def _read_lock_pid(lock_path: Path) -> int | None:
    """Read the PID from a lock file, returning ``None`` on any failure."""
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
        return int(content) if content else None
    except (OSError, ValueError):
        return None
