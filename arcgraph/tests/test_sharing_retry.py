from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arcgraph.change.store import atomic_write_json, read_json_object
from arcgraph.core import sharing_retry
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import IndexMetadata


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def windows_retry(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Enable the Windows retry path with a deterministic clock."""

    clock = _FakeClock()
    monkeypatch.setattr(sharing_retry, "_RETRY_SHARING_VIOLATIONS", True)
    monkeypatch.setattr(sharing_retry.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sharing_retry.time, "sleep", clock.sleep)
    return clock


def _sharing_violation() -> PermissionError:
    return PermissionError(13, "The process cannot access the file")


def test_retry_returns_once_the_sharing_violation_clears(
    windows_retry: _FakeClock,
) -> None:
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _sharing_violation()
        return "ok"

    assert sharing_retry.retry_sharing_violation(operation) == "ok"
    assert attempts == 3
    assert windows_retry.sleeps == 2


def test_retry_raises_after_the_bounded_window(windows_retry: _FakeClock) -> None:
    def operation() -> None:
        raise _sharing_violation()

    with pytest.raises(PermissionError):
        sharing_retry.retry_sharing_violation(operation)
    assert windows_retry.now >= sharing_retry._RETRY_SECONDS


def test_retry_is_disabled_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sharing_retry, "_RETRY_SHARING_VIOLATIONS", False)
    attempts = 0

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise _sharing_violation()

    with pytest.raises(PermissionError):
        sharing_retry.retry_sharing_violation(operation)
    assert attempts == 1


def test_publish_current_retries_a_replace_blocked_by_a_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    output_dir = tmp_path / "arcgraph"
    metadata = IndexMetadata(
        index_version="test-index",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    original_replace = Path.replace
    blocked = 0

    def replace(self: Path, target: Path) -> Path:
        nonlocal blocked
        if Path(target).name == "current.json" and blocked < 2:
            blocked += 1
            raise _sharing_violation()
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)

    GraphStoreWriter(output_dir).write(metadata, [], [], [], [])

    assert blocked == 2
    current = json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
    assert current["build_dir"] == "builds/test-index"
    assert not (output_dir / "current.json.tmp").exists()


def test_from_current_retries_a_read_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    output_dir = tmp_path / "arcgraph"
    metadata = IndexMetadata(
        index_version="test-index",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(metadata, [], [], [], [])
    original_read_text = Path.read_text
    blocked = 0

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal blocked
        if self.name == "current.json" and blocked < 1:
            blocked += 1
            raise _sharing_violation()
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    reader = GraphStoreReader.from_current(output_dir)

    assert blocked == 1
    assert reader.sqlite_path.parent.name == "test-index"


def test_change_store_write_retries_a_replace_blocked_by_a_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target = tmp_path / "plans" / "plan-1" / "current.json"
    original_replace = os.replace
    blocked = 0

    def replace(source: object, destination: object) -> None:
        nonlocal blocked
        if Path(str(destination)).name == "current.json" and blocked < 2:
            blocked += 1
            raise _sharing_violation()
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)

    atomic_write_json(target, {"revision": 1}, root=tmp_path)

    assert blocked == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"revision": 1}
    assert sorted(path.name for path in target.parent.iterdir()) == ["current.json"]


def test_change_store_read_retries_during_pointer_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target = tmp_path / "plans" / "plan-1" / "current.json"
    atomic_write_json(target, {"revision": 1}, root=tmp_path)
    original_read_text = Path.read_text
    blocked = 0

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal blocked
        if self.name == "current.json" and blocked < 1:
            blocked += 1
            raise _sharing_violation()
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert read_json_object(target, root=tmp_path) == {"revision": 1}
    assert blocked == 1
