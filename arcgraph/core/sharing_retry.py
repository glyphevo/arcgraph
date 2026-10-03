"""Bounded retry for Windows sharing violations on atomically replaced files.

On Windows, Python opens files without ``FILE_SHARE_DELETE``.  While a reader
holds a pointer file such as ``current.json`` open, replacing it fails with
``PermissionError``; a reader that opens it during the replace can fail the
same way.  Both windows last only as long as the other side's short open, so a
brief retry resolves them.  On POSIX a ``PermissionError`` is a real access
problem and is raised immediately.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat
import time
from typing import TypeVar

T = TypeVar("T")

_RETRY_SHARING_VIOLATIONS = os.name == "nt"
_RETRY_SECONDS = 2.0
_RETRY_INTERVAL_SECONDS = 0.01


def retry_sharing_violation(operation: Callable[[], T]) -> T:
    """Run ``operation``, retrying ``PermissionError`` briefly on Windows."""

    if not _RETRY_SHARING_VIOLATIONS:
        return operation()
    deadline = time.monotonic() + _RETRY_SECONDS
    while True:
        try:
            return operation()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_INTERVAL_SECONDS)


def is_regular_file(path: Path) -> bool:
    """Return ``path.is_file()``, confirming a negative answer on Windows.

    While another process replaces a file, Windows ``stat`` can report it as
    missing, whereas opening it fails with ``PermissionError`` rather than
    ``FileNotFoundError``.  On Windows a negative ``is_file()`` is therefore
    settled by opening the path: a missing file still answers at once, and a
    replace in progress is retried like any other sharing violation.
    """

    if path.is_file():
        return True
    if not _RETRY_SHARING_VIOLATIONS or path.is_dir():
        return False
    mode = retry_sharing_violation(lambda: _opened_mode(path))
    return mode is not None and stat.S_ISREG(mode)


def path_exists(path: Path) -> bool:
    """Return ``path.exists()``, confirming a negative answer on Windows.

    Same reasoning as :func:`is_regular_file`, for callers that must keep
    treating a non-file in that place as present.
    """

    if path.exists():
        return True
    if not _RETRY_SHARING_VIOLATIONS:
        return False
    return retry_sharing_violation(lambda: _opened_mode(path)) is not None


def _opened_mode(path: Path) -> int | None:
    """Open ``path`` and return its mode, or ``None`` when it is missing."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return os.fstat(descriptor).st_mode
    finally:
        os.close(descriptor)
