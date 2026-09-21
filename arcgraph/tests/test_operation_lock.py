from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from arcgraph.core.operation_lock import (
    ArcGraphOperationInProgress,
    _lock_is_active,
    _process_alive,
    _release_if_owner,
    arcgraph_operation_lock,
    arcgraph_operation_status,
    arcgraph_pid_file_lock,
)


def test_operation_lock_blocks_nested_acquire(tmp_path: Path) -> None:
    with arcgraph_operation_lock(tmp_path):
        status = arcgraph_operation_status(tmp_path)
        assert status["locked"] is True
        assert status["pid"] is not None
        with pytest.raises(ArcGraphOperationInProgress):
            with arcgraph_operation_lock(tmp_path):
                pass

    assert arcgraph_operation_status(tmp_path)["locked"] is False


def test_operation_lock_recovers_dead_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("-1", encoding="utf-8")

    with arcgraph_operation_lock(tmp_path):
        status = arcgraph_operation_status(tmp_path)
        assert status["locked"] is True
        assert status["pid"] is not None


def test_operation_status_treats_recent_corrupt_lock_as_active(tmp_path: Path) -> None:
    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("not-a-pid", encoding="utf-8")

    status = arcgraph_operation_status(tmp_path)

    assert status["locked"] is True
    assert status["stale"] is False
    assert status["pid"] is None


def test_process_alive_does_not_signal_child_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        assert process.poll() is None
        assert _process_alive(process.pid) is True
        time.sleep(0.2)
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_pid_file_lock_recovers_dead_pid_for_explicit_lock_path(tmp_path: Path) -> None:
    lock_path = tmp_path / "write.lock"
    lock_path.write_text("-1", encoding="utf-8")

    with arcgraph_pid_file_lock(
        lock_path,
        error_factory=lambda pid: RuntimeError(f"busy {pid}"),
    ):
        assert lock_path.exists()

    assert not lock_path.exists()


# `_process_alive` dispatches to `_windows_process_alive` before it reaches
# `os.kill`, so patching `os.kill` on Windows patches a call that is never
# made. These tests state POSIX semantics; running them there would assert
# what the platform does not do.
posix_signal_semantics = pytest.mark.skipif(
    os.name == "nt",
    reason="_process_alive uses the Windows API, not os.kill, on this platform",
)


@posix_signal_semantics
def test_process_alive_treats_an_unsignalable_process_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live process we may not signal still holds its lock.

    `os.kill(pid, 0)` raises PermissionError for a process owned by another
    user. Reading that as "gone" would let a second operation delete a live
    holder's lock file and run concurrently with it, which is the one outcome
    this lock exists to prevent.
    """

    def deny(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr("arcgraph.core.operation_lock.os.kill", deny)
    assert _process_alive(4321) is True


@posix_signal_semantics
def test_process_alive_rejects_a_missing_or_unusable_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gone(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("arcgraph.core.operation_lock.os.kill", gone)
    assert _process_alive(4321) is False

    def broken(_pid: int, _signal: int) -> None:
        raise OSError

    monkeypatch.setattr("arcgraph.core.operation_lock.os.kill", broken)
    assert _process_alive(4321) is False


def test_process_alive_rejects_a_nonpositive_pid() -> None:
    """A PID that cannot name a process is not a holder, on any platform.

    This is checked before either platform branch, so it is deliberately kept
    out of the skipped tests above: Windows should still run it.
    """

    assert _process_alive(0) is False
    assert _process_alive(-1) is False


def test_release_leaves_a_lock_owned_by_another_process(tmp_path: Path) -> None:
    """Releasing must never remove a lock this process does not own.

    The release path runs in a `finally`, so it also runs when acquisition
    raised because someone else held the lock. Deleting the file there would
    hand the lock to whoever asks next while the real holder is still working.
    """

    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("999999", encoding="utf-8")

    _release_if_owner(lock_path)

    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8") == "999999"


def test_release_tolerates_a_lock_that_is_already_gone(tmp_path: Path) -> None:
    _release_if_owner(tmp_path / "missing.lock")


def test_lock_stays_held_when_its_age_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable lock is treated as held, not as free.

    With no PID to check, age decides. If the age cannot be read either, the
    remaining choice is between refusing an operation and running two, and only
    one of those is recoverable.
    """

    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("", encoding="utf-8")

    def unreadable(_self: Path) -> None:
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "stat", unreadable)
    assert _lock_is_active(lock_path, None) is True


def test_acquire_reports_a_lock_taken_during_stale_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the race after clearing a stale lock must refuse, not proceed.

    Stale recovery unlinks the file and re-creates it. Another process can win
    that gap, and continuing would run two operations against one output
    directory.
    """

    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("999999", encoding="utf-8")

    real_open = os.open
    state = {"calls": 0}

    def contended(path: object, flags: int, *args: object) -> int:
        if str(path) == str(lock_path):
            state["calls"] += 1
            if state["calls"] == 2:
                raise FileExistsError
        return real_open(path, flags, *args)

    monkeypatch.setattr("arcgraph.core.operation_lock.os.open", contended)

    with pytest.raises(ArcGraphOperationInProgress) as excinfo:
        with arcgraph_operation_lock(tmp_path):
            pass

    assert state["calls"] == 2
    # The winner is unknown in this path, so no PID is claimed.
    assert excinfo.value.pid is None


def test_acquire_reports_a_stale_lock_that_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale lock that will not unlink still blocks the operation."""

    lock_path = tmp_path / "operation.lock"
    lock_path.write_text("999999", encoding="utf-8")

    def undeletable(_self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", undeletable)

    with pytest.raises(ArcGraphOperationInProgress) as excinfo:
        with arcgraph_operation_lock(tmp_path):
            pass

    assert excinfo.value.pid == 999999
