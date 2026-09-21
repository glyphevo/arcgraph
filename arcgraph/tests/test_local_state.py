from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from arcgraph.interfaces import local_state


def test_independent_writers_share_one_os_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private" / "events.jsonl"
    real_append = local_state.append_bytes_with_rollback
    state_lock = threading.Lock()
    ready = threading.Barrier(8)
    active = 0
    max_active = 0

    def observed_append(
        fd: int,
        data: bytes,
        *,
        original_size: int,
        durable: bool,
    ) -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.005)
        try:
            real_append(
                fd,
                data,
                original_size=original_size,
                durable=durable,
            )
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        local_state,
        "append_bytes_with_rollback",
        observed_append,
    )

    def append(index: int) -> None:
        ready.wait()
        local_state.append_private_bytes(
            path,
            f'{{"index":{index}}}\n'.encode(),
        )

    threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 8


def test_concurrent_appends_never_interleave_record_bytes(tmp_path: Path) -> None:
    """Guard the real append path, with nothing about the lock mocked out.

    ``MCPMetricsRecorder`` no longer holds its own lock across the append, so
    non-interleaved JSONL depends entirely on the file lock taken inside
    ``append_private_bytes``.  Records are padded well past a page so that a
    lock which does not serialize threads -- ``fcntl.lockf`` is per-process,
    not per-descriptor -- produces torn lines instead of passing silently.
    """

    path = tmp_path / "private" / "events.jsonl"
    writer_count = 16
    records_per_writer = 12
    start = threading.Barrier(writer_count)

    def append(writer: int) -> None:
        start.wait(timeout=30)
        for sequence in range(records_per_writer):
            local_state.append_private_bytes(
                path,
                (
                    json.dumps(
                        {
                            "writer": writer,
                            "sequence": sequence,
                            "pad": "x" * 8192,
                        }
                    )
                    + "\n"
                ).encode(),
            )

    threads = [
        threading.Thread(target=append, args=(writer,))
        for writer in range(writer_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_bytes().decode("utf-8").splitlines()
    assert len(lines) == writer_count * records_per_writer
    observed = {
        (record["writer"], record["sequence"])
        for record in (json.loads(line) for line in lines)
    }
    assert observed == {
        (writer, sequence)
        for writer in range(writer_count)
        for sequence in range(records_per_writer)
    }


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
@pytest.mark.parametrize("fail_direct_parent", [True, False])
def test_directory_sync_failures_are_reported_as_their_own_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_direct_parent: bool,
) -> None:
    """Every directory in the chain reports the same distinct cause.

    The direct parent is synced through the held descriptor rather than the
    walk, so it needs the same conversion; otherwise its failure surfaces as a
    generic path/permission error the operator cannot act on.
    """

    path = tmp_path / "private" / "nested" / "events.jsonl"
    local_state.append_private_bytes(path, b'{"first":true}\n')
    # The direct parent is synced through the held descriptor; its own parent
    # is synced by the walk.  Both must report the same cause.
    target = path.parent if fail_direct_parent else path.parent.parent
    target_inode = target.stat().st_ino
    real_fsync = local_state.os.fsync

    def reject_target_sync(fd: int) -> None:
        if os.fstat(fd).st_ino == target_inode:
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", reject_target_sync)
    parent_fd = local_state._open_directory_without_symlinks(path.parent)

    try:
        with pytest.raises(
            local_state.LocalStateDirectorySyncError,
            match="could not be synchronized",
        ):
            local_state.sync_created_file_path(
                parent_fd=parent_fd,
            )
    finally:
        os.close(parent_fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
def test_created_path_sync_requires_a_held_parent_descriptor(tmp_path: Path) -> None:
    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="parent directory held open",
    ):
        local_state.sync_created_file_path(
            parent_fd=None,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
def test_created_path_sync_uses_the_held_ancestor_chain_after_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "private"
    nested = private / "nested"
    nested.mkdir(parents=True, mode=0o700)
    os.chmod(private, 0o700)
    os.chmod(nested, 0o700)
    original_private_inode = private.stat().st_ino
    parent_fd = local_state._open_directory_without_symlinks(nested)

    private.rename(tmp_path / "private-moved")
    private.mkdir(mode=0o700)
    replacement_private_inode = private.stat().st_ino
    real_fsync = local_state.os.fsync
    synced_inodes: set[int] = set()

    def observe_fsync(fd: int) -> None:
        synced_inodes.add(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", observe_fsync)
    try:
        local_state.sync_created_file_path(
            parent_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)

    assert original_private_inode in synced_inodes
    assert replacement_private_inode not in synced_inodes


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
def test_created_path_sync_stops_at_a_private_namespace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inode self-comparison must terminate an all-private ancestor walk."""

    nested = tmp_path / "private" / "nested"
    nested.mkdir(parents=True, mode=0o700)
    parent_fd = local_state._open_directory_without_symlinks(nested)
    real_fstat = local_state.os.fstat
    real_open = local_state.os.open
    root_stat = os.stat("/")
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    opened_parent_inodes: list[tuple[int, int]] = []
    synced_inodes: list[tuple[int, int]] = []
    max_parent_opens = 64

    def private_directory_fstat(fd: int) -> SimpleNamespace:
        directory_stat = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_dev=directory_stat.st_dev,
            st_ino=directory_stat.st_ino,
        )

    def bounded_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        is_parent_walk = path == ".." and dir_fd is not None
        if is_parent_walk and len(opened_parent_inodes) >= max_parent_opens:
            raise AssertionError("ancestor walk did not stop at the namespace root")
        if dir_fd is None:
            fd = real_open(path, flags, mode)
        else:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if is_parent_walk:
            directory_stat = real_fstat(fd)
            opened_parent_inodes.append((directory_stat.st_dev, directory_stat.st_ino))
        return fd

    def observe_fsync(fd: int) -> None:
        directory_stat = real_fstat(fd)
        synced_inodes.append((directory_stat.st_dev, directory_stat.st_ino))

    monkeypatch.setattr(local_state.os, "fstat", private_directory_fstat)
    monkeypatch.setattr(local_state.os, "open", bounded_open)
    monkeypatch.setattr(local_state.os, "fsync", observe_fsync)
    try:
        local_state.sync_created_file_path(parent_fd=parent_fd)
    finally:
        os.close(parent_fd)

    assert len(opened_parent_inodes) < max_parent_opens
    assert opened_parent_inodes[-2:] == [root_identity, root_identity]
    assert root_identity in synced_inodes


def test_private_state_rejects_multiply_linked_files(tmp_path: Path) -> None:
    first_parent = tmp_path / "project-a"
    second_parent = tmp_path / "project-b"
    first_parent.mkdir(mode=0o700)
    second_parent.mkdir(mode=0o700)
    first = first_parent / "events.jsonl"
    second = second_parent / "events.jsonl"
    first.write_bytes(b"")
    os.chmod(first, 0o600)
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")

    first_stat = os.stat(first)
    second_stat = os.stat(second)
    assert first_stat.st_ino == second_stat.st_ino
    assert first_stat.st_nlink == 2
    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="multiple hard links",
    ):
        local_state.append_private_bytes(first, b'{"project":"a"}\n')
    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="multiple hard links",
    ):
        local_state.append_private_bytes(second, b'{"project":"b"}\n')
    assert first.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-symlink traversal contract")
def test_private_state_rejects_symlinked_parent_components(
    tmp_path: Path,
) -> None:
    project_a_state = tmp_path / "project-a" / ".arcgraph-trial"
    project_a_log_parent = project_a_state / "feedback"
    project_a_log_parent.mkdir(parents=True, mode=0o700)
    os.chmod(project_a_log_parent, 0o700)
    protected = project_a_log_parent / "agent.jsonl"
    protected.write_bytes(b'{"project":"a"}\n')
    os.chmod(protected, 0o600)

    project_b = tmp_path / "project-b"
    project_b.mkdir(mode=0o700)
    alias = project_b / ".arcgraph-trial"
    alias.symlink_to(project_a_state, target_is_directory=True)
    aliased_path = alias / "feedback" / "agent.jsonl"

    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="must not contain symbolic links",
    ):
        local_state.append_private_bytes(
            aliased_path,
            b'{"project":"b"}\n',
        )
    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="must not contain symbolic links",
    ):
        local_state.read_private_bytes(aliased_path)

    assert protected.read_bytes() == b'{"project":"a"}\n'


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-symlink traversal contract")
def test_private_state_classifies_a_mkdir_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    real_mkdir = os.mkdir

    def race_mkdir(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "state":
            os.symlink(target, path, target_is_directory=True, dir_fd=dir_fd)
            raise FileExistsError(errno.EEXIST, "simulated mkdir race", path)
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(local_state.os, "mkdir", race_mkdir)

    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="must not contain symbolic links",
    ):
        local_state.ensure_private_parent_directories(tmp_path / "state")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
def test_private_state_closes_a_new_directory_fd_when_fchmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_fd: int | None = None

    def fail_fchmod(fd: int, mode: int) -> None:
        nonlocal captured_fd
        assert mode == 0o700
        captured_fd = fd
        raise OSError(errno.EIO, "simulated fchmod failure")

    monkeypatch.setattr(local_state.os, "fchmod", fail_fchmod)

    with pytest.raises(OSError, match="simulated fchmod failure"):
        local_state.ensure_private_parent_directories(tmp_path / "private")

    assert captured_fd is not None
    with pytest.raises(OSError) as exc:
        os.fstat(captured_fd)
    assert exc.value.errno == errno.EBADF


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-mode contract")
def test_new_private_state_overrides_restrictive_umask_and_remains_usable(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    path = parent / "events.jsonl"
    previous_umask = os.umask(0o277)
    try:
        local_state.append_private_bytes(path, b'{"index":1}\n')
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    local_state.append_private_bytes(path, b'{"index":2}\n')
    assert local_state.read_private_bytes(path).splitlines() == [
        b'{"index":1}',
        b'{"index":2}',
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX read-only mode contract")
def test_private_state_summary_reads_owner_read_only_file(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    path = parent / "events.jsonl"
    path.write_bytes(b'{"index":1}\n')
    os.chmod(path, 0o400)

    assert local_state.read_private_bytes(path) == b'{"index":1}\n'


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking-open contract")
def test_private_state_read_rejects_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    path = parent / "events.jsonl"
    os.mkfifo(path, mode=0o600)
    real_open = local_state._open_state_file

    def observed_open(
        candidate: Path,
        flags: int,
        mode: int = 0o777,
        *,
        parent_fd: int | None,
    ) -> int:
        assert flags & os.O_NONBLOCK
        return real_open(candidate, flags, mode, parent_fd=parent_fd)

    monkeypatch.setattr(local_state, "_open_state_file", observed_open)

    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="must be a regular file",
    ):
        local_state.read_private_bytes(path)


def test_local_state_path_alias_check_normalizes_and_compares_existing_files(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    first = parent / "first.jsonl"
    second = parent / "second.jsonl"
    first.write_bytes(b"")
    second.write_bytes(b"")

    assert local_state.local_state_paths_alias(
        first,
        parent / ".." / "private" / first.name,
    )
    assert not local_state.local_state_paths_alias(first, second)

    hardlink = parent / "hardlink.jsonl"
    try:
        os.link(first, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")
    assert local_state.local_state_paths_alias(first, hardlink)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-mode contract")
def test_private_state_rejects_wide_direct_parent_without_mutating_it(
    tmp_path: Path,
) -> None:
    direct_parent = tmp_path / "operator-selected"
    direct_parent.mkdir(mode=0o755)
    os.chmod(direct_parent, 0o755)
    path = direct_parent / "events.jsonl"

    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="parent permissions are not private",
    ):
        local_state.append_private_bytes(path, b"{}\n")

    assert not path.exists()
    assert stat.S_IMODE(os.stat(direct_parent).st_mode) == 0o755

    path.write_bytes(b"{}\n")
    os.chmod(path, 0o600)
    with pytest.raises(
        local_state.PrivateLocalStateError,
        match="parent permissions are not private",
    ):
        local_state.read_private_bytes(path)
    assert path.read_bytes() == b"{}\n"
