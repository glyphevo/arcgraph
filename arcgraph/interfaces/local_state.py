"""Private local-file primitives for explicit ArcGraph trial state."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading
from typing import Iterator


class PrivateLocalStateError(RuntimeError):
    """A safe failure while opening or updating explicit local trial state."""


class LocalStateDirectorySyncError(PrivateLocalStateError):
    """A parent directory entry could not be flushed to stable storage.

    Distinct from the path and permission failures: the selected log is fine
    and its bytes may already be on disk.  The operator has to move the state
    to a filesystem whose directories support ``fsync``, not re-check modes.
    """


class LocalStateDirectoryInspectionError(LocalStateDirectorySyncError):
    """A parent directory could not be inspected while planning a sync.

    A sync failure identifies its own cause; this one does not.  The directory
    may have been replaced, unmounted, or had its permissions changed, so the
    operation fails closed without naming a remedy it cannot infer.
    """


_LOCAL_STATE_THREAD_LOCK = threading.Lock()


def ensure_private_parent_directories(parent: Path) -> tuple[Path, ...]:
    """Create only missing parents and never change permissions on existing ones."""

    if os.name == "posix":
        return _ensure_private_parent_directories_posix(parent)

    missing: list[Path] = []
    created: list[Path] = []
    cursor = parent
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if cursor.is_symlink():
        raise PrivateLocalStateError(
            "Local state parent path must not contain symbolic links."
        )
    if os.path.lexists(cursor) and not cursor.is_dir():
        raise PrivateLocalStateError("Local state parent must be a directory.")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            if os.name == "posix":
                os.chmod(directory, 0o700)
            created.append(directory)
        except FileExistsError:
            if not directory.is_dir():
                raise PrivateLocalStateError(
                    "Local state parent must be a directory."
                ) from None
        except OSError as exc:
            raise PrivateLocalStateError(
                "Local state parent could not be created privately."
            ) from exc
    require_private_parent_directory(parent)
    return tuple(created)


def require_private_parent_directory(parent: Path) -> os.stat_result:
    """Require an unaliased private state directory without mutating it."""

    try:
        if os.name == "posix":
            fd = _open_directory_without_symlinks(parent)
            try:
                directory_stat = os.fstat(fd)
            finally:
                os.close(fd)
        else:
            directory_stat = parent.lstat()
    except OSError as exc:
        if _is_symlink_open_error(exc):
            raise PrivateLocalStateError(
                "Local state parent path must not contain symbolic links."
            ) from exc
        raise PrivateLocalStateError(
            "Local state parent could not be inspected safely."
        ) from exc
    if stat.S_ISLNK(directory_stat.st_mode):
        raise PrivateLocalStateError(
            "Local state parent path must not contain symbolic links."
        )
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise PrivateLocalStateError("Local state parent must be a directory.")
    if os.name == "posix" and stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise PrivateLocalStateError(
            "Existing local state parent permissions are not private."
        )
    return directory_stat


@contextmanager
def private_parent_directory(parent: Path) -> Iterator[int | None]:
    """Hold the selected parent open while a state file is inspected or updated."""

    if os.name != "posix":
        require_private_parent_directory(parent)
        yield None
        return
    fd: int | None = None
    try:
        fd = _open_directory_without_symlinks(parent)
        directory_stat = os.fstat(fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise PrivateLocalStateError("Local state parent must be a directory.")
        if stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise PrivateLocalStateError(
                "Existing local state parent permissions are not private."
            )
    except PrivateLocalStateError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        if _is_symlink_open_error(exc):
            raise PrivateLocalStateError(
                "Local state parent path must not contain symbolic links."
            ) from exc
        raise PrivateLocalStateError(
            "Local state parent could not be inspected safely."
        ) from exc
    if fd is None:
        raise PrivateLocalStateError(
            "Local state parent could not be inspected safely."
        )
    try:
        yield fd
    finally:
        os.close(fd)


def open_private_append(
    path: Path,
    *,
    parent_fd: int | None = None,
) -> tuple[int, bool]:
    """Open a private regular file for atomic append without following a symlink."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    base_flags = os.O_RDWR | os.O_APPEND | no_follow
    try:
        fd = _open_state_file(
            path,
            base_flags | os.O_CREAT | os.O_EXCL,
            0o600,
            parent_fd=parent_fd,
        )
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            try:
                _unlink_state_file(path, parent_fd=parent_fd)
            except OSError:
                pass
            raise
        return fd, True
    except FileExistsError:
        return (
            open_private_existing(path, base_flags, parent_fd=parent_fd),
            False,
        )


def open_private_existing(
    path: Path,
    flags: int,
    *,
    parent_fd: int | None = None,
) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0 and path.is_symlink():
        raise PrivateLocalStateError("Local state file must not be a symbolic link.")
    return _open_state_file(path, flags | no_follow, parent_fd=parent_fd)


def open_private_read(
    path: Path,
    *,
    parent_fd: int | None = None,
) -> int:
    """Open a private regular file for reading without blocking on a FIFO."""

    read_flags = os.O_RDWR
    if os.name == "posix":
        read_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    fd = open_private_existing(path, read_flags, parent_fd=parent_fd)
    try:
        require_private_regular_file(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def require_private_regular_file(fd: int) -> os.stat_result:
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise PrivateLocalStateError("Local state path must be a regular file.")
    if file_stat.st_nlink != 1:
        raise PrivateLocalStateError(
            "Local state file must not have multiple hard links."
        )
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise PrivateLocalStateError(
            "Existing local state file permissions are not private."
        )
    return file_stat


def sync_created_file_path(
    *,
    parent_fd: int | None,
) -> None:
    """Persist a new file name through the bounded private parent chain.

    A retry after an earlier directory-sync failure can no longer distinguish
    parents created by the failed attempt from older parents.  A concurrent
    writer can likewise observe parent directories created by a peer without
    knowing they are new.  Therefore every durability acknowledgement uses the
    same conservative chain: sync each private ancestor and the first
    non-private ancestor that holds its name.

    Ancestors are opened relative to the held direct-parent descriptor.  Do not
    reopen their configured path names: a concurrent rename or replacement
    could otherwise make ArcGraph sync an unrelated inode and falsely report
    that the recorded path is durable.
    """

    if os.name != "posix":
        return
    if parent_fd is None:
        raise PrivateLocalStateError(
            "Local state synchronization needs the selected parent directory "
            "held open."
        )
    _sync_directory_fd(parent_fd)
    _sync_ancestor_directories(parent_fd)


def _sync_ancestor_directories(
    direct_parent_fd: int,
) -> None:
    """Sync actual ancestors reached from an already verified directory fd."""

    current_fd: int | None = None
    try:
        current_fd = os.dup(direct_parent_fd)
        current_stat = os.fstat(current_fd)
    except OSError as exc:
        if current_fd is not None:
            os.close(current_fd)
        raise LocalStateDirectoryInspectionError(
            "A local state parent directory could not be inspected for "
            "synchronization."
        ) from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        while True:
            parent_fd: int | None = None
            try:
                parent_fd = os.open("..", flags, dir_fd=current_fd)
                parent_stat = os.fstat(parent_fd)
            except OSError as exc:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise LocalStateDirectoryInspectionError(
                    "A local state parent directory could not be inspected for "
                    "synchronization."
                ) from exc
            if not stat.S_ISDIR(parent_stat.st_mode):
                os.close(parent_fd)
                raise LocalStateDirectoryInspectionError(
                    "A local state parent directory is no longer a directory."
                )
            if (parent_stat.st_dev, parent_stat.st_ino) == (
                current_stat.st_dev,
                current_stat.st_ino,
            ):
                os.close(parent_fd)
                break
            try:
                _sync_directory_fd(parent_fd)
            except PrivateLocalStateError:
                os.close(parent_fd)
                raise
            os.close(current_fd)
            current_fd = parent_fd
            current_stat = parent_stat
            if stat.S_IMODE(parent_stat.st_mode) & 0o077:
                break
    finally:
        os.close(current_fd)


def _sync_directory_fd(fd: int) -> None:
    try:
        directory_stat = os.fstat(fd)
    except OSError as exc:
        raise LocalStateDirectoryInspectionError(
            "A local state parent directory could not be inspected for "
            "synchronization."
        ) from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise LocalStateDirectoryInspectionError(
            "A local state parent directory is no longer a directory."
        )
    try:
        os.fsync(fd)
    except OSError as exc:
        raise LocalStateDirectorySyncError(
            "A local state parent directory could not be synchronized."
        ) from exc


def read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def append_bytes_with_rollback(
    fd: int,
    data: bytes,
    *,
    original_size: int,
    durable: bool,
) -> None:
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError("short append")
        if durable:
            os.fsync(fd)
    except OSError:
        try:
            os.ftruncate(fd, original_size)
            if durable:
                os.fsync(fd)
        except OSError:
            pass
        raise


def append_private_bytes(
    path: Path,
    data: bytes,
) -> None:
    """Append one complete record without making a durability acknowledgement."""

    try:
        ensure_private_parent_directories(path.parent)
        with private_parent_directory(path.parent) as parent_fd:
            fd, _created = open_private_append(path, parent_fd=parent_fd)
            try:
                with exclusive_file_lock(fd):
                    file_stat = require_private_regular_file(fd)
                    append_bytes_with_rollback(
                        fd,
                        data,
                        original_size=file_stat.st_size,
                        durable=False,
                    )
            finally:
                os.close(fd)
    except PrivateLocalStateError:
        raise
    except OSError as exc:
        raise PrivateLocalStateError(
            "Local state could not be appended safely."
        ) from exc


def read_private_bytes(path: Path) -> bytes:
    """Read one private regular file without following its final component."""

    try:
        with private_parent_directory(path.parent) as parent_fd:
            fd = open_private_read(
                path,
                parent_fd=parent_fd,
            )
            try:
                with exclusive_file_lock(fd):
                    require_private_regular_file(fd)
                    return read_all(fd)
            finally:
                os.close(fd)
    except PrivateLocalStateError:
        raise
    except OSError as exc:
        raise PrivateLocalStateError("Local state could not be read safely.") from exc


def local_state_paths_alias(left: Path, right: Path) -> bool:
    """Return whether two configured log paths can name the same local file."""

    left_path = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(left))))
    right_path = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(right))))
    if os.path.normcase(left_path) == os.path.normcase(right_path):
        return True
    try:
        return os.path.samefile(left_path, right_path)
    except OSError:
        return False


@contextmanager
def exclusive_file_lock(fd: int) -> Iterator[None]:
    """Serialize local-state read/check/append across threads and processes."""

    # Windows byte-range locks can reject contention between separate file
    # descriptors in one process as a resource deadlock.  Serialize threads
    # before taking the OS lock; the OS lock still protects other processes.
    with _LOCAL_STATE_THREAD_LOCK:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
            return
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        raise PrivateLocalStateError(
            "Local state locking is unavailable on this platform."
        )


def _ensure_private_parent_directories_posix(parent: Path) -> tuple[Path, ...]:
    absolute = _absolute_path(parent)
    created: list[Path] = []
    current = Path(absolute.anchor)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = current / component
            try:
                child_fd = _open_directory_component_without_symlinks(
                    fd,
                    component,
                    flags,
                )
            except FileNotFoundError:
                created_component = False
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    created_component = True
                except FileExistsError:
                    pass
                child_fd = _open_directory_component_without_symlinks(
                    fd,
                    component,
                    flags,
                )
                if created_component:
                    try:
                        os.fchmod(child_fd, 0o700)
                    except OSError:
                        os.close(child_fd)
                        raise
                    created.append(child)
            os.close(fd)
            fd = child_fd
            current = child
        directory_stat = os.fstat(fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise PrivateLocalStateError("Local state parent must be a directory.")
        if stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise PrivateLocalStateError(
                "Existing local state parent permissions are not private."
            )
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    return tuple(created)


def _open_directory_without_symlinks(path: Path) -> int:
    absolute = _absolute_path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child_fd = _open_directory_component_without_symlinks(
                fd,
                component,
                flags,
            )
            os.close(fd)
            fd = child_fd
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_directory_component_without_symlinks(
    parent_fd: int,
    component: str,
    flags: int,
) -> int:
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except OSError as exc:
        if _is_symlink_open_error(exc):
            raise PrivateLocalStateError(
                "Local state parent path must not contain symbolic links."
            ) from exc
        raise


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_state_file(
    path: Path,
    flags: int,
    mode: int = 0o777,
    *,
    parent_fd: int | None,
) -> int:
    if parent_fd is not None and os.name == "posix":
        return os.open(path.name, flags, mode, dir_fd=parent_fd)
    return os.open(path, flags, mode)


def _unlink_state_file(path: Path, *, parent_fd: int | None) -> None:
    if parent_fd is not None and os.name == "posix":
        os.unlink(path.name, dir_fd=parent_fd)
        return
    os.unlink(path)


def _is_symlink_open_error(exc: OSError) -> bool:
    return exc.errno in {errno.ELOOP, errno.ENOTDIR}
