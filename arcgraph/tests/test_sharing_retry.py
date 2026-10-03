from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from arcgraph.change.contracts import PlanDecision
from arcgraph.change.errors import ChangeStoreCorrupt, ChangeStoreNotFound
from arcgraph.change.store import (
    ChangeStateStore,
    atomic_write_json,
    read_json_object,
)
from arcgraph.core import sharing_retry
from arcgraph.core.cleanup import arcgraph_output_storage_status
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import IndexMetadata
from arcgraph.tests.test_change_lifecycle import _activated_service
from arcgraph.tests.test_change_store import _revision


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
    # Replace only the module's own reference; the process-wide time module
    # stays real for everything else the test exercises.
    monkeypatch.setattr(
        sharing_retry,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
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


def test_windows_retry_fixture_leaves_the_global_clock_alone(
    windows_retry: _FakeClock,
) -> None:
    assert sharing_retry.time.monotonic == windows_retry.monotonic
    assert time.monotonic != windows_retry.monotonic
    assert time.sleep != windows_retry.sleep


def test_storage_status_retries_a_current_json_read_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    output_dir: Path = tmp_path / "arcgraph"
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

    status = arcgraph_output_storage_status(output_dir, refresh=True)

    assert blocked == 1
    assert status["current_build"] == "builds/test-index"


def _write_current(tmp_path: Path) -> Path:
    output_dir: Path = tmp_path / "arcgraph"
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="test-index",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        [],
        [],
        [],
        [],
    )
    return output_dir


def test_from_current_reads_through_an_exists_false_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = _write_current(tmp_path)
    # Windows stat can report current.json missing while a build replaces it.
    monkeypatch.setattr(Path, "exists", lambda self: False)

    reader = GraphStoreReader.from_current(output_dir)

    assert reader.sqlite_path.parent.name == "test-index"


def test_from_current_still_reports_a_missing_index(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No ArcGraph current index"):
        GraphStoreReader.from_current(tmp_path / "arcgraph")


def test_regular_file_check_confirms_a_windows_false_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert sharing_retry.is_regular_file(target) is True
    assert windows_retry.sleeps == 0


def test_regular_file_check_answers_a_missing_file_without_waiting(
    tmp_path: Path,
    windows_retry: _FakeClock,
) -> None:
    assert sharing_retry.is_regular_file(tmp_path / "missing.json") is False
    assert windows_retry.sleeps == 0


def test_regular_file_check_rejects_a_directory_without_waiting(
    tmp_path: Path,
    windows_retry: _FakeClock,
) -> None:
    directory: Path = tmp_path / "pointer.json"
    directory.mkdir()

    assert sharing_retry.is_regular_file(directory) is False
    assert windows_retry.sleeps == 0


def test_regular_file_check_retries_an_open_blocked_by_a_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    original_open = os.open
    blocked = 0

    def open_blocked(path: object, flags: int, *args: object) -> int:
        nonlocal blocked
        if Path(str(path)) == target and blocked < 2:
            blocked += 1
            raise _sharing_violation()
        return original_open(path, flags, *args)

    monkeypatch.setattr(os, "open", open_blocked)

    assert sharing_retry.is_regular_file(target) is True
    assert blocked == 2
    assert windows_retry.sleeps == 2


def test_regular_file_check_is_plain_is_file_outside_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sharing_retry, "_RETRY_SHARING_VIOLATIONS", False)
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert sharing_retry.is_regular_file(target) is False


def test_change_store_read_survives_an_is_file_false_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target: Path = tmp_path / "plans" / "plan-1" / "current.json"
    atomic_write_json(target, {"revision": 1}, root=tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert read_json_object(target, root=tmp_path) == {"revision": 1}


def test_current_decision_survives_an_exists_false_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    pending = PlanDecision(
        repo_id="repo",
        decision_id="decision-pending",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        status="pending",
        actor="system",
        reason="initial decision",
    )
    store.write_decision(pending)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert store.read_current_decision("plan") == pending


def test_current_decision_is_none_without_a_pointer(tmp_path: Path) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    store.write_revision(_revision())

    assert store.read_current_decision("plan") is None


def test_current_decision_still_rejects_a_directory_in_place_of_the_pointer(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    store.write_revision(_revision())
    pointer: Path = store._path("plans", "plan", "decision-current.json")
    pointer.mkdir(parents=True)

    with pytest.raises(ChangeStoreNotFound):
        store.read_current_decision("plan")


def test_path_exists_confirms_a_windows_false_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert sharing_retry.path_exists(target) is True
    assert windows_retry.sleeps == 0


def test_path_exists_answers_a_missing_file_without_waiting(
    tmp_path: Path,
    windows_retry: _FakeClock,
) -> None:
    assert sharing_retry.path_exists(tmp_path / "missing.json") is False
    assert windows_retry.sleeps == 0


def test_path_exists_retries_an_open_blocked_by_a_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    original_open = os.open
    blocked = 0

    def open_blocked(path: object, flags: int, *args: object) -> int:
        nonlocal blocked
        if Path(str(path)) == target and blocked < 2:
            blocked += 1
            raise _sharing_violation()
        return original_open(path, flags, *args)

    monkeypatch.setattr(os, "open", open_blocked)

    assert sharing_retry.path_exists(target) is True
    assert windows_retry.sleeps == 2


def test_path_exists_is_plain_exists_outside_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sharing_retry, "_RETRY_SHARING_VIOLATIONS", False)
    target: Path = tmp_path / "pointer.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert sharing_retry.path_exists(target) is False


def _hide_from_exists(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    original_exists = Path.exists

    def exists(self: Path) -> bool:
        if self.name == name:
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", exists)


def test_health_check_does_not_report_a_replaced_decision_pointer_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    service, _revision_record, _output = _activated_service(tmp_path, monkeypatch)
    _hide_from_exists(monkeypatch, "decision-current.json")

    service.store.health_check()


def test_health_check_still_checks_a_replaced_current_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_retry: _FakeClock,
) -> None:
    service, _revision_record, _output = _activated_service(tmp_path, monkeypatch)
    store = service.store
    _hide_from_exists(monkeypatch, "current.json")
    original_read_model = store._read_model
    read: list[str] = []

    def read_model(path: Path, model_type: type) -> object:
        read.append(path.name)
        return original_read_model(path, model_type)

    monkeypatch.setattr(store, "_read_model", read_model)

    store.health_check()

    assert "current.json" in read
    assert "decision-current.json" in read


def test_health_check_still_rejects_a_directory_in_place_of_a_pointer(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    store.write_revision(_revision())
    store._path("plans", "plan", "decision-current.json").mkdir(parents=True)

    with pytest.raises(ChangeStoreCorrupt, match="unrecognized entry"):
        store.health_check()
