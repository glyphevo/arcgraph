from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import uuid

import pytest

from arcgraph.interfaces import local_state
from arcgraph.interfaces.cli import build_parser, main
from arcgraph.interfaces.trial_feedback import (
    FEEDBACK_V1_HISTORICAL_WARNING_KINDS,
    FEEDBACK_V1_READ_WARNING_KINDS,
    FEEDBACK_WARNING_KINDS,
    MAX_WARNING_KINDS,
    TrialFeedbackConflictError,
    TrialFeedbackInputError,
    TrialFeedbackLimitError,
    TrialFeedbackStorageError,
    TrialFeedbackStore,
    validate_feedback_request,
)

_ALLOWED = {
    "mcp": {"arcgraph_help", "arcgraph_record_trial_feedback"},
    "cli": {"help", "feedback"},
}


def _request(
    *,
    client_event_id: str | None = None,
    surface: str = "mcp",
    tool_name: str = "arcgraph_help",
    issue_kind: str = "discoverability",
    fallback: str = "none",
) -> object:
    return validate_feedback_request(
        {
            "client_event_id": client_event_id or str(uuid.uuid4()),
            "surface": surface,
            "tool_name": tool_name,
            "outcome": "partial",
            "issue_kind": issue_kind,
            "stage": "interpretation",
            "fallback": fallback,
            "result_status": "partial",
            "freshness_status": "fresh",
            "warning_kinds": ["stale_index"],
        },
        allowed_tool_names_by_surface=_ALLOWED,
    )


def test_feedback_record_is_private_bounded_and_idempotent(tmp_path: Path) -> None:
    feedback_log = tmp_path / "private" / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request(client_event_id="15a2debd-1745-4ed8-9fc7-f56eb7d6f128")

    assert not feedback_log.exists()
    first, duplicate = store.record(request)
    repeated, repeated_duplicate = store.record(request)

    assert duplicate is False
    assert repeated_duplicate is True
    assert repeated.feedback_id == first.feedback_id
    lines = feedback_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == {
        "schema_version",
        "feedback_id",
        "product_version",
        "timestamp",
        "client_event_id",
        "surface",
        "tool_name",
        "outcome",
        "issue_kind",
        "stage",
        "fallback",
        "result_status",
        "freshness_status",
        "warning_kinds",
    }
    assert len(lines[0].encode("utf-8")) + 1 <= 4096
    assert payload["client_event_id"] == "15a2debd-1745-4ed8-9fc7-f56eb7d6f128"
    if os.name == "posix":
        assert stat.S_IMODE(feedback_log.stat().st_mode) == 0o600
        assert stat.S_IMODE(feedback_log.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
def test_new_feedback_record_syncs_file_and_created_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    synced_kinds: list[str] = []
    real_fsync = os.fsync

    def observe_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", observe_fsync)

    TrialFeedbackStore(feedback_log).record(_request())

    assert synced_kinds[0] == "file"
    assert synced_kinds.count("directory") >= 2


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
def test_new_feedback_record_syncs_parent_directories_created_by_a_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file creator cannot assume existing private parents are durable.

    Another process can create the parent chain and stop before it opens the
    log.  The process that successfully records the first event must therefore
    sync the bounded private chain even though its own mkdir pass created
    nothing.
    """

    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    created_by_peer = local_state.ensure_private_parent_directories(feedback_log.parent)
    required_parent_inodes = {
        directory.parent.stat().st_ino for directory in created_by_peer
    }
    real_fsync = local_state.os.fsync
    synced_inodes: set[int] = set()

    def observe_fsync(fd: int) -> None:
        synced_inodes.add(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", observe_fsync)

    record, duplicate = TrialFeedbackStore(feedback_log).record(_request())

    assert record.feedback_id
    assert duplicate is False
    assert required_parent_inodes <= synced_inodes


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
def test_feedback_retry_rechecks_parent_sync_after_uncertain_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request()
    real_fsync = local_state.os.fsync
    synced_inodes: list[int] = []
    failed_inode: int | None = None

    def fail_third_sync(fd: int) -> None:
        nonlocal failed_inode
        inode = os.fstat(fd).st_ino
        synced_inodes.append(inode)
        if len(synced_inodes) == 3:
            failed_inode = inode
            raise OSError("simulated ancestor directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", fail_third_sync)

    with pytest.raises(TrialFeedbackStorageError):
        store.record(request)
    first_attempt_sync_count = len(synced_inodes)
    repeated, duplicate = store.record(request)
    retry_inodes = synced_inodes[first_attempt_sync_count:]

    assert duplicate is True
    assert repeated.client_event_id == request.client_event_id
    assert failed_inode is not None
    assert failed_inode in retry_inodes


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
def test_feedback_retry_fails_while_required_ancestor_sync_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request()
    real_fsync = local_state.os.fsync
    synced_inodes: list[int] = []
    failed_inode: int | None = None

    def fail_required_ancestor_sync(fd: int) -> None:
        nonlocal failed_inode
        inode = os.fstat(fd).st_ino
        synced_inodes.append(inode)
        if failed_inode is None and len(synced_inodes) == 3:
            failed_inode = inode
        if inode == failed_inode:
            raise OSError("required ancestor directory is still not durable")
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", fail_required_ancestor_sync)

    with pytest.raises(TrialFeedbackStorageError):
        store.record(request)
    first_attempt_sync_count = len(synced_inodes)

    with pytest.raises(TrialFeedbackStorageError):
        store.record(request)

    retry_inodes = synced_inodes[first_attempt_sync_count:]
    assert failed_inode is not None
    assert failed_inode in retry_inodes


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX gate")
def test_feedback_retry_does_not_report_durable_when_the_chain_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat failure must not shorten the chain past a still-unsynced parent.

    The walk uses ``stat`` only to find its bound, but swallowing the error
    stops the walk early -- possibly below a directory the failed first attempt
    never synced -- and the replay then reports success for a record whose
    parent entry is still not durable.
    """

    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request()
    outermost = tmp_path
    outermost_inode = outermost.stat().st_ino
    real_fsync = local_state.os.fsync
    synced_inodes: list[int] = []

    def fail_outermost_sync(fd: int) -> None:
        inode = os.fstat(fd).st_ino
        if inode == outermost_inode:
            raise OSError("outermost required ancestor is not durable")
        synced_inodes.append(inode)
        real_fsync(fd)

    monkeypatch.setattr(local_state.os, "fsync", fail_outermost_sync)
    with pytest.raises(TrialFeedbackStorageError):
        store.record(request)

    # The retry could now sync it, but an unreadable intermediate directory
    # must fail closed before the walk can claim the chain is durable.
    monkeypatch.setattr(local_state.os, "fsync", real_fsync)
    intermediate_inode = feedback_log.parent.parent.stat().st_ino
    real_fstat = local_state.os.fstat

    def fail_intermediate_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if result.st_ino == intermediate_inode:
            raise OSError("transient stat failure")
        return result

    monkeypatch.setattr(local_state.os, "fstat", fail_intermediate_fstat)
    synced_inodes.clear()

    with pytest.raises(TrialFeedbackStorageError):
        store.record(request)

    assert outermost_inode not in synced_inodes


@pytest.mark.skipif(os.name != "posix", reason="directory inspection is a POSIX gate")
def test_feedback_retry_reports_an_ancestor_open_failure_as_unproven_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_log = tmp_path / "private" / "nested" / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request()
    store.record(request)
    direct_parent_inode = feedback_log.parent.stat().st_ino
    real_open = local_state.os.open

    def fail_ancestor_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if (
            path == ".."
            and dir_fd is not None
            and os.fstat(dir_fd).st_ino == direct_parent_inode
        ):
            raise PermissionError("simulated ancestor open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        local_state.os,
        "open",
        fail_ancestor_open,
    )

    with pytest.raises(
        TrialFeedbackStorageError,
        match="parent directories could not be inspected",
    ) as exc:
        store.record(request)

    assert "mode 0700" not in str(exc.value)
    assert "file mode 0600" not in str(exc.value)


def test_feedback_duplicate_id_with_different_payload_fails_closed(
    tmp_path: Path,
) -> None:
    store = TrialFeedbackStore(tmp_path / "trial.jsonl")
    event_id = str(uuid.uuid4())
    store.record(_request(client_event_id=event_id))

    with pytest.raises(TrialFeedbackConflictError) as exc:
        store.record(
            _request(
                client_event_id=event_id,
                issue_kind="incorrect_result",
            )
        )

    assert "different feedback" in str(exc.value)
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    "field",
    [
        "message",
        "details",
        "note",
        "observation",
        "metadata",
        "path",
        "targets",
        "repo_id",
        "source",
        "prompt",
    ],
)
def test_feedback_validation_rejects_forbidden_fields_without_echoing(
    field: str,
) -> None:
    secret = "/private/project/SECRET_TARGET"
    with pytest.raises(TrialFeedbackInputError) as exc:
        validate_feedback_request(
            {
                **_request().model_dump(mode="json"),
                field: secret,
            },
            allowed_tool_names_by_surface=_ALLOWED,
        )
    assert secret not in str(exc.value)


def test_feedback_validation_rejects_surface_mismatch_and_unbounded_values() -> None:
    with pytest.raises(TrialFeedbackInputError):
        _request(surface="cli", tool_name="arcgraph_help")

    with pytest.raises(TrialFeedbackInputError):
        validate_feedback_request(
            {
                **_request().model_dump(mode="json"),
                "warning_kinds": ["SECRET123"],
            },
            allowed_tool_names_by_surface=_ALLOWED,
        )

    with pytest.raises(TrialFeedbackInputError):
        _request(tool_name="a" * 65)

    with pytest.raises(TrialFeedbackInputError):
        validate_feedback_request(
            {
                **_request().model_dump(mode="json"),
                "warning_kinds": ["stale_index"] * (MAX_WARNING_KINDS + 1),
            },
            allowed_tool_names_by_surface=_ALLOWED,
        )


def test_feedback_accepts_real_reported_warning_kinds() -> None:
    real_kinds = {
        "focus_target_not_found",
        "missing_test_candidate",
        "receiver_resolution_unknown",
        "unresolved_target",
        "wall_time_limit_exceeded",
    }

    request = validate_feedback_request(
        {
            **_request().model_dump(mode="json"),
            "warning_kinds": sorted(real_kinds),
        },
        allowed_tool_names_by_surface=_ALLOWED,
    )

    assert set(request.warning_kinds) == real_kinds
    assert real_kinds <= set(FEEDBACK_WARNING_KINDS)
    assert "other" in FEEDBACK_WARNING_KINDS
    assert "runtime_trace_unavailable" not in FEEDBACK_WARNING_KINDS
    assert "runtime_trace_unavailable" in FEEDBACK_V1_HISTORICAL_WARNING_KINDS
    assert set(FEEDBACK_WARNING_KINDS).isdisjoint(FEEDBACK_V1_HISTORICAL_WARNING_KINDS)
    assert set(FEEDBACK_V1_READ_WARNING_KINDS) == {
        *FEEDBACK_WARNING_KINDS,
        *FEEDBACK_V1_HISTORICAL_WARNING_KINDS,
    }
    with pytest.raises(TrialFeedbackInputError):
        validate_feedback_request(
            {
                **_request().model_dump(mode="json"),
                "warning_kinds": ["runtime_trace_unavailable"],
            },
            allowed_tool_names_by_surface=_ALLOWED,
        )


def test_feedback_v1_reader_keeps_historical_warning_kinds_compatible(
    tmp_path: Path,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    store.record(_request())
    historical = json.loads(feedback_log.read_text(encoding="utf-8"))
    historical["warning_kinds"] = ["runtime_trace_unavailable"]
    feedback_log.write_text(
        json.dumps(historical, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(feedback_log, 0o600)

    summary = store.summarize()
    current, duplicate = store.record(_request())

    assert summary["event_count"] == 1
    assert summary["by_warning_kind"] == {"runtime_trace_unavailable": 1}
    assert current.warning_kinds == ["stale_index"]
    assert duplicate is False
    assert store.summarize()["event_count"] == 2


@pytest.mark.parametrize("operation", ["summarize", "record"])
def test_feedback_rejects_an_unsupported_record_schema_distinctly(
    tmp_path: Path,
    operation: str,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    store.record(_request())
    unsupported = json.loads(feedback_log.read_text(encoding="utf-8"))
    unsupported["schema_version"] = "9.9.9"
    feedback_log.write_text(
        json.dumps(unsupported, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(feedback_log, 0o600)

    with pytest.raises(
        TrialFeedbackStorageError,
        match="unsupported record schema version",
    ) as raised:
        if operation == "summarize":
            store.summarize()
        else:
            store.record(_request())

    assert raised.value.error_code == "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"
    assert "9.9.9" not in str(raised.value)


def test_feedback_accepts_the_complete_published_warning_vocabulary(
    tmp_path: Path,
) -> None:
    request = validate_feedback_request(
        {
            **_request().model_dump(mode="json"),
            "warning_kinds": list(FEEDBACK_WARNING_KINDS),
        },
        allowed_tool_names_by_surface=_ALLOWED,
    )

    assert MAX_WARNING_KINDS == len(FEEDBACK_WARNING_KINDS)
    assert request.warning_kinds == sorted(FEEDBACK_WARNING_KINDS)
    record, duplicate = TrialFeedbackStore(tmp_path / "trial.jsonl").record(request)
    assert duplicate is False
    assert record.warning_kinds == sorted(FEEDBACK_WARNING_KINDS)


def test_feedback_summary_contains_only_shareable_aggregates(
    tmp_path: Path,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    store = TrialFeedbackStore(feedback_log)
    request = _request()
    record, _duplicate = store.record(request)

    summary = store.summarize()
    serialized = json.dumps(summary, sort_keys=True)

    assert summary["status"] == "available"
    assert summary["event_count"] == 1
    assert summary["by_tool"] == {"arcgraph_help": 1}
    assert summary["by_warning_kind"] == {"stale_index": 1}
    assert summary["privacy"]["shareable"] is True
    assert record.feedback_id not in serialized
    assert str(request.client_event_id) not in serialized
    assert record.timestamp not in serialized
    assert str(feedback_log) not in serialized
    assert "raw_events" not in serialized


def test_feedback_store_rejects_relative_symlink_and_unsafe_existing_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(TrialFeedbackStorageError):
        TrialFeedbackStore(Path("trial.jsonl"))

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(TrialFeedbackStorageError):
        TrialFeedbackStore(link).record(_request())

    if os.name == "posix":
        unsafe = tmp_path / "unsafe.jsonl"
        unsafe.write_text("", encoding="utf-8")
        unsafe.chmod(0o644)
        with pytest.raises(TrialFeedbackStorageError):
            TrialFeedbackStore(unsafe).record(_request())


def test_feedback_store_rejects_corrupt_and_oversized_logs(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("not json\n", encoding="utf-8")
    corrupt.chmod(0o600)
    with pytest.raises(TrialFeedbackStorageError):
        TrialFeedbackStore(corrupt).summarize()

    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_text(
        json.dumps(
            {
                **_request().model_dump(mode="json"),
                "schema_version": "1.0.0",
                "feedback_id": "feedback-existing",
                "product_version": "0.1.0rc5",
                "timestamp": "2026-07-30T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    incomplete.chmod(0o600)
    with pytest.raises(TrialFeedbackStorageError, match="incomplete record"):
        TrialFeedbackStore(incomplete).record(_request())

    limited = tmp_path / "limited.jsonl"
    limited.write_text("", encoding="utf-8")
    limited.chmod(0o600)
    with pytest.raises(TrialFeedbackLimitError):
        TrialFeedbackStore(limited, max_file_bytes=64).record(_request())


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-read mode contract")
def test_feedback_summary_reads_owner_read_only_log(tmp_path: Path) -> None:
    store = TrialFeedbackStore(tmp_path / "private" / "trial.jsonl")
    store.record(_request())
    os.chmod(store.path, 0o400)

    summary = store.summarize()

    assert summary["status"] == "available"
    assert summary["event_count"] == 1


def test_feedback_short_append_fails_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:-1])

    monkeypatch.setattr(os, "write", short_write)

    with pytest.raises(TrialFeedbackStorageError):
        TrialFeedbackStore(feedback_log).record(_request())

    assert feedback_log.read_bytes() == b""


def test_feedback_concurrent_writers_produce_complete_unique_records(
    tmp_path: Path,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    requests = [_request() for _ in range(24)]

    def write_one(request: object) -> str:
        record, duplicate = TrialFeedbackStore(feedback_log).record(request)
        assert duplicate is False
        return record.feedback_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        feedback_ids = list(executor.map(write_one, requests))

    lines = feedback_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(requests)
    assert len(feedback_ids) == len(set(feedback_ids))
    assert all(json.loads(line)["schema_version"] == "1.0.0" for line in lines)


def test_feedback_concurrent_duplicate_is_serialized_idempotently(
    tmp_path: Path,
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    request = _request(client_event_id=str(uuid.uuid4()))

    def write_same_event(_index: int) -> tuple[str, bool]:
        record, duplicate = TrialFeedbackStore(feedback_log).record(request)
        return record.feedback_id, duplicate

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write_same_event, range(16)))

    assert len(feedback_log.read_text(encoding="utf-8").splitlines()) == 1
    assert len({feedback_id for feedback_id, _duplicate in results}) == 1
    assert sum(duplicate for _feedback_id, duplicate in results) == 15


def test_feedback_cli_records_and_summarizes_without_exposing_local_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    event_id = str(uuid.uuid4())

    assert (
        main(
            [
                "feedback",
                "record",
                "--feedback-log",
                str(feedback_log),
                "--client-event-id",
                event_id,
                "--surface",
                "cli",
                "--tool-name",
                "help",
                "--outcome",
                "not_used",
                "--issue-kind",
                "discoverability",
                "--stage",
                "preflight",
                "--fallback",
                "human",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["status"] == "recorded"
    assert recorded["network_access"] is False

    assert main(["feedback", "summarize", str(feedback_log)]) == 0
    summarized_output = capsys.readouterr().out
    summary = json.loads(summarized_output)
    assert summary["event_count"] == 1
    assert summary["by_tool"] == {"help": 1}
    assert event_id not in summarized_output
    assert str(feedback_log) not in summarized_output


def test_feedback_cli_rejects_a_shared_metrics_path_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared_log = tmp_path / "trial.jsonl"

    exit_code = main(
        [
            "--metrics-log",
            str(shared_log),
            "feedback",
            "record",
            "--feedback-log",
            str(shared_log),
            "--client-event-id",
            str(uuid.uuid4()),
            "--surface",
            "cli",
            "--tool-name",
            "feedback",
            "--outcome",
            "failed",
            "--issue-kind",
            "integration",
            "--stage",
            "preflight",
            "--fallback",
            "none",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["error_code"] == "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"
    assert payload["message"] == (
        "Metrics and feedback logs must use distinct local paths."
    )
    assert captured.err == ""
    assert not shared_log.exists()


def test_feedback_cli_does_not_append_metrics_to_the_log_it_summarizes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feedback_log = tmp_path / "trial.jsonl"
    TrialFeedbackStore(feedback_log).record(_request())
    original = feedback_log.read_bytes()

    exit_code = main(
        [
            "--metrics-log",
            str(feedback_log),
            "feedback",
            "summarize",
            str(feedback_log),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["error_code"] == "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"
    assert feedback_log.read_bytes() == original


def test_feedback_cli_returns_structured_safe_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "/private/SECRET_feedback.jsonl"
    rc = main(
        [
            "feedback",
            "record",
            "--feedback-log",
            secret,
            "--client-event-id",
            str(uuid.uuid4()),
            "--surface",
            "cli",
            "--tool-name",
            "not-a-command",
            "--outcome",
            "failed",
            "--issue-kind",
            "integration",
            "--stage",
            "preflight",
            "--fallback",
            "abandoned",
        ]
    )
    output = capsys.readouterr().out

    assert rc == 2
    payload = json.loads(output)
    assert payload["status"] == "error"
    assert payload["error_code"] == "TRIAL_FEEDBACK_INPUT_INVALID"
    assert secret not in output


def test_feedback_cli_argparse_failures_use_the_same_machine_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "feedback",
            "record",
            "--feedback-log",
            str(tmp_path / "trial.jsonl"),
            "--client-event-id",
            str(uuid.uuid4()),
            "--surface",
            "bogus",
            "--tool-name",
            "help",
            "--outcome",
            "not_used",
            "--issue-kind",
            "discoverability",
            "--stage",
            "preflight",
            "--fallback",
            "human",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 2
    assert captured.err == ""
    assert payload["status"] == "error"
    assert payload["error_code"] == "TRIAL_FEEDBACK_INPUT_INVALID"
    assert "bogus" not in captured.out


def test_feedback_cli_requires_a_subcommand_and_returns_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["feedback"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 2
    assert captured.err == ""
    assert payload["status"] == "error"
    assert payload["error_code"] == "TRIAL_FEEDBACK_INPUT_INVALID"


def test_feedback_parsers_do_not_share_mutable_error_payloads() -> None:
    parser = build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    feedback = commands.choices["feedback"]
    feedback_commands = next(
        action for action in feedback._actions if action.dest == "feedback_command"
    )
    parsers = (
        feedback,
        feedback_commands.choices["record"],
        feedback_commands.choices["summarize"],
    )
    payloads = [parser._arcgraph_parse_error_payload for parser in parsers]

    payloads[0]["mutation_probe"] = True

    assert len({id(payload) for payload in payloads}) == len(payloads)
    assert all("mutation_probe" not in payload for payload in payloads[1:])
