from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import anyio
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any

import pytest

from arcgraph.interfaces.metrics import (
    CLI_METRICS_WRITE_WARNING,
    MCP_METRICS_WRITE_WARNING,
    MCPMetricsRecorder,
    summarize_metrics,
    summarize_trial_metrics,
)
from arcgraph.interfaces.local_state import PrivateLocalStateError
from arcgraph.interfaces.cli import main as cli_main
from arcgraph.interfaces.mcp_server import build_parser

EXPECTED_EVENT_KEYS = {
    "timestamp",
    "tool_name",
    "status",
    "duration_ms",
    "payload_bytes",
    "estimated_tokens",
    "truncated",
    "freshness_status",
}


class _Result:
    def __init__(self, *, is_error: bool) -> None:
        self.is_error = is_error


def test_mcp_metrics_are_disabled_by_default() -> None:
    args = build_parser().parse_args([])

    assert args.mcp_metrics_log is None


def test_mcp_metrics_record_only_bounded_derived_fields(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics" / "mcp.jsonl"
    recorder = MCPMetricsRecorder(metrics_path)
    payload = {
        "status": "available",
        "repo_id": "secret-repo",
        "target": "SECRET_TARGET",
        "path": "/etc/passwd",
        "source": "do not record",
        "freshness": {
            "status": "stale",
            "reason": "SECRET_FRESHNESS_REASON",
            "stale_files": ["/private/secret.py"],
        },
        "truncation": {"truncated": True},
    }

    recorder.record_tool_call(
        tool_name="arcgraph_get_context",
        status="success",
        duration_ms=12.34567,
        payload=payload,
    )

    raw = metrics_path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert set(event) == EXPECTED_EVENT_KEYS
    assert event["tool_name"] == "arcgraph_get_context"
    assert event["status"] == "success"
    assert event["duration_ms"] == 12.346
    assert event["payload_bytes"] > 0
    assert event["estimated_tokens"] > 0
    assert event["truncated"] is True
    assert event["freshness_status"] == "stale"
    for forbidden in (
        "secret-repo",
        "SECRET_TARGET",
        "/etc/passwd",
        "do not record",
        "repo_id",
        "target",
        "path",
        "source",
        "SECRET_FRESHNESS_REASON",
        "/private/secret.py",
    ):
        assert forbidden not in raw

    summary = summarize_metrics(metrics_path)
    assert summary["event_count"] == 1
    assert summary["commands"] == {"arcgraph_get_context": 1}


def test_mcp_metrics_jsonl_is_intact_under_concurrent_calls(tmp_path: Path) -> None:
    metrics_path = tmp_path / "mcp.jsonl"
    recorder = MCPMetricsRecorder(metrics_path)

    def record(index: int) -> None:
        recorder.record_tool_call(
            tool_name="arcgraph_index_status",
            status="success",
            duration_ms=float(index),
            payload={"status": "available", "sequence": index},
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(record, range(100)))

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert len(events) == 100
    assert all(set(event) == EXPECTED_EVENT_KEYS for event in events)
    assert all(event["tool_name"] == "arcgraph_index_status" for event in events)
    assert all(event["freshness_status"] == "not_reported" for event in events)


def test_mcp_metrics_independent_recorders_share_the_file_lock(
    tmp_path: Path,
) -> None:
    metrics_path = tmp_path / "mcp.jsonl"

    def record(index: int) -> None:
        MCPMetricsRecorder(metrics_path).record_tool_call(
            tool_name="arcgraph_index_status",
            status="success",
            duration_ms=float(index),
            payload={"status": "available", "sequence": index},
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(record, range(100)))

    events = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 100
    assert all(set(event) == EXPECTED_EVENT_KEYS for event in events)


def test_mcp_metrics_create_private_state_without_rewriting_existing_parent(
    tmp_path: Path,
) -> None:
    existing_parent = tmp_path / "operator-selected"
    existing_parent.mkdir(mode=0o755)
    if os.name == "posix":
        existing_parent.chmod(0o755)
    private_parent = existing_parent / "arcgraph"
    metrics_path = private_parent / "mcp.jsonl"

    MCPMetricsRecorder(metrics_path).record_tool_call(
        tool_name="arcgraph_index_status",
        status="success",
        duration_ms=1.0,
        payload={"status": "available"},
    )

    assert metrics_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(existing_parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(private_parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(metrics_path.stat().st_mode) == 0o600


def test_mcp_metrics_reject_unsafe_existing_file_and_final_symlink(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode semantics do not describe Windows ACLs")

    unsafe = tmp_path / "unsafe.jsonl"
    unsafe.write_text("", encoding="utf-8")
    unsafe.chmod(0o644)
    recorder = MCPMetricsRecorder(unsafe)
    recorder.record_tool_call(
        tool_name="arcgraph_index_status",
        status="success",
        duration_ms=1.0,
        payload={"status": "available"},
    )
    assert capsys.readouterr().err == MCP_METRICS_WRITE_WARNING
    assert unsafe.read_bytes() == b""
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o644
    with pytest.raises(PrivateLocalStateError):
        summarize_trial_metrics(unsafe)

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    MCPMetricsRecorder(link).record_tool_call(
        tool_name="arcgraph_index_status",
        status="success",
        duration_ms=1.0,
        payload={"status": "available"},
    )
    assert capsys.readouterr().err == MCP_METRICS_WRITE_WARNING
    assert target.read_bytes() == b""


def test_cli_metrics_failure_is_visible_without_changing_command_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe = tmp_path / "metrics-directory"
    unsafe.mkdir()

    assert cli_main(["--metrics-log", str(unsafe), "help"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "available"
    assert captured.err == CLI_METRICS_WRITE_WARNING


@pytest.mark.parametrize(
    ("freshness_status", "expected"),
    [
        ("fresh", "fresh"),
        ("stale", "stale"),
        ("unknown", "unknown"),
        ("SECRET_/private/repo", "not_reported"),
        (None, "not_reported"),
    ],
)
def test_mcp_metrics_freshness_is_enum_bounded(
    tmp_path: Path,
    freshness_status: str | None,
    expected: str,
) -> None:
    metrics_path = tmp_path / "mcp.jsonl"
    recorder = MCPMetricsRecorder(metrics_path)
    payload = (
        {"freshness": {"status": freshness_status}}
        if freshness_status is not None
        else {}
    )

    recorder.record_tool_call(
        tool_name="arcgraph_index_status",
        status="success",
        duration_ms=1.0,
        payload=payload,
    )

    raw = metrics_path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert event["freshness_status"] == expected
    assert "SECRET_" not in raw
    assert "/private/repo" not in raw


def test_trial_metrics_summary_is_aggregated_and_shareable(tmp_path: Path) -> None:
    metrics_path = tmp_path / "private" / "SECRET_PROJECT.jsonl"
    recorder = MCPMetricsRecorder(metrics_path)
    for duration_ms, freshness_status, truncated in (
        (10.0, "fresh", False),
        (20.0, "stale", True),
        (30.0, "unknown", False),
    ):
        recorder.record_tool_call(
            tool_name="arcgraph_get_context",
            status="success",
            duration_ms=duration_ms,
            payload={
                "freshness": {"status": freshness_status},
                "truncation": {"truncated": truncated},
                "target": "SECRET_TARGET",
                "path": "/private/source.py",
            },
        )
    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-29T00:00:00Z",
                    "tool_name": "SECRET_TOOL_/private/repo",
                    "status": "SECRET_STATUS",
                    "duration_ms": 40,
                    "payload_bytes": 80,
                    "estimated_tokens": 20,
                    "truncated": False,
                    "freshness_status": "SECRET_FRESHNESS",
                    "repo_id": "SECRET_REPO",
                }
            )
            + "\n"
        )

    summary = summarize_trial_metrics(metrics_path)
    serialized = json.dumps(summary, sort_keys=True)
    context = summary["by_tool"]["arcgraph_get_context"]

    assert summary["status"] == "available"
    assert summary["event_count"] == 4
    assert summary["privacy"]["shareable"] is True
    assert context["count"] == 3
    assert context["duration_ms"]["p50"] == 20.0
    assert context["estimated_tokens"]["count"] == 3
    assert context["truncation"] == {
        "observed_count": 3,
        "truncated_count": 1,
        "truncated_rate": 0.3333,
    }
    assert context["freshness_statuses"] == {
        "fresh": 1,
        "stale": 1,
        "unknown": 1,
    }
    assert summary["by_tool"]["unknown_tool"]["statuses"] == {"unknown": 1}
    for forbidden in (
        str(metrics_path),
        "2026-07-29",
        "SECRET_",
        "/private",
        "repo_id",
    ):
        assert forbidden not in serialized


def test_cli_trial_metrics_summary_uses_shareable_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metrics_path = tmp_path / "SECRET_PROJECT.jsonl"
    MCPMetricsRecorder(metrics_path).record_tool_call(
        tool_name="arcgraph_index_status",
        status="success",
        duration_ms=1.0,
        payload={"freshness": {"status": "fresh"}},
    )

    assert (
        cli_main(["metrics", str(metrics_path), "--trial-summary", "--limit", "10"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["by_tool"]["arcgraph_index_status"]["freshness_statuses"] == {
        "fresh": 1
    }
    assert payload["privacy"]["shareable"] is True
    assert str(metrics_path) not in serialized
    assert "SECRET_PROJECT" not in serialized


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-mode contract")
@pytest.mark.parametrize("trial_summary", [False, True])
def test_cli_metrics_storage_failures_return_structured_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    trial_summary: bool,
) -> None:
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o755)
    os.chmod(unsafe_parent, 0o755)
    metrics_path = unsafe_parent / "metrics.jsonl"
    metrics_path.write_text("", encoding="utf-8")
    os.chmod(metrics_path, 0o600)
    arguments = ["metrics", str(metrics_path)]
    if trial_summary:
        arguments.append("--trial-summary")

    assert cli_main(arguments) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == "unavailable"
    assert payload["event_count"] == 0
    assert payload["warnings"] == [
        "Metrics log could not be read safely. Use a new canonical private path."
    ]
    if trial_summary:
        assert str(metrics_path) not in captured.out
        assert payload["privacy"]["shareable"] is True
    else:
        assert payload["path"] == str(metrics_path)


@pytest.mark.parametrize(
    ("arguments", "expected_status", "expected_path_key"),
    [
        (("metrics", "--trial-summary"), "unavailable", None),
        (("metrics",), "unavailable", "path"),
        (("report", "metrics-html"), "error", "metrics_path"),
    ],
)
def test_cli_metrics_invalid_utf8_returns_structured_unavailable_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    expected_status: str,
    expected_path_key: str | None,
) -> None:
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    if os.name == "posix":
        private_parent.chmod(0o700)
    metrics_path = private_parent / "metrics.jsonl"
    metrics_path.write_bytes(b"\xff\xfe\xff\n")
    if os.name == "posix":
        metrics_path.chmod(0o600)
    command = [*arguments, str(metrics_path)]

    assert cli_main(command) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == expected_status
    assert payload["warnings"] == [
        "Metrics log is not valid UTF-8. Start a new private metrics log."
    ]
    assert "Traceback" not in captured.out
    if expected_path_key is None:
        assert str(metrics_path) not in captured.out
    else:
        assert payload[expected_path_key] == str(metrics_path)


def test_report_metrics_html_read_error_is_not_a_written_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    if os.name == "posix":
        private_parent.chmod(0o700)
    empty_log = private_parent / "empty.jsonl"
    corrupt_log = private_parent / "corrupt.jsonl"
    corrupt_log.write_bytes(b"\xff\xfe\xff\n")
    if os.name == "posix":
        corrupt_log.chmod(0o600)
    written_report = tmp_path / "written.html"
    skipped_report = tmp_path / "skipped.html"

    assert (
        cli_main(
            ["report", "metrics-html", str(empty_log), "--output", str(written_report)]
        )
        == 0
    )
    written = json.loads(capsys.readouterr().out)
    written_html = written_report.read_text(encoding="utf-8")

    assert (
        cli_main(
            [
                "report",
                "metrics-html",
                str(corrupt_log),
                "--output",
                str(skipped_report),
            ]
        )
        == 2
    )
    skipped = json.loads(capsys.readouterr().out)

    # The success result already uses "unavailable" for an empty log, so the
    # failure must not reuse that status or put a metrics log in "path".
    assert written["status"] == "unavailable"
    assert written["path"] == str(written_report.resolve())
    assert written_report.exists()
    assert str(empty_log) not in written_html
    assert "Metrics log does not exist." not in written_html
    assert "warning_count" in written_html
    assert skipped["status"] == "error"
    assert skipped["error_code"] == "METRICS_LOG_INVALID_ENCODING"
    assert "path" not in skipped
    assert skipped["metrics_path"] == str(corrupt_log)
    assert not skipped_report.exists()


def test_mcp_metrics_status_lock_does_not_cover_file_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = MCPMetricsRecorder(tmp_path / "mcp.jsonl")
    state_lock = threading.Lock()
    both_entered = threading.Barrier(2)
    appended: list[str] = []
    active = 0
    max_active = 0

    def observe_append(serialized_event: str) -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # record_tool_call swallows every exception from this callback, so
            # a scheduling timeout must not surface as an append assertion.
            both_entered.wait(timeout=30)
        except threading.BrokenBarrierError:  # pragma: no cover - load only
            pytest.fail("the second metrics append never started concurrently")
        with state_lock:
            appended.append(serialized_event)
            active -= 1

    monkeypatch.setattr(recorder, "_append_serialized_event", observe_append)

    def record(index: int) -> None:
        recorder.record_tool_call(
            tool_name="arcgraph_index_status",
            status="success",
            duration_ms=float(index),
            payload={"status": "available"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(record, range(2)))

    assert len(appended) == 2
    assert max_active == 2


def test_mcp_metrics_write_failure_is_best_effort_and_warns_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metrics_path = tmp_path / "not-a-file"
    metrics_path.mkdir()
    recorder = MCPMetricsRecorder(metrics_path)

    for _ in range(3):
        recorder.record_tool_call(
            tool_name="arcgraph_index_status",
            status="success",
            duration_ms=1.0,
            payload={"status": "available"},
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == MCP_METRICS_WRITE_WARNING


@pytest.mark.parametrize(
    ("result", "payload", "expected"),
    [
        (_Result(is_error=True), None, "error"),
        (_Result(is_error=False), {"status": "error"}, "error"),
        (_Result(is_error=False), {"status": "failed"}, "error"),
        (_Result(is_error=False), {"status": "blocked"}, "success"),
        (_Result(is_error=False), {"status": "available"}, "success"),
    ],
)
def test_mcp_metric_status_distinguishes_protocol_and_payload_errors(
    result: Any,
    payload: Any,
    expected: str,
) -> None:
    from arcgraph.interfaces.mcp_server import _mcp_result_status

    assert _mcp_result_status(result, payload) == expected


def test_mcp_metric_tool_name_rejects_names_outside_server_mode(
    tmp_path: Path,
) -> None:
    from arcgraph.interfaces.agent_capabilities import mcp_capability_names

    default_path = tmp_path / "default" / "mcp.jsonl"
    default_recorder = MCPMetricsRecorder(default_path)
    for tool_name in (
        "arcgraph_get_context",
        "arcgraph_record_trial_feedback",
        "SECRET_TOOL_/etc/passwd",
    ):
        default_recorder.record_tool_call(
            tool_name=tool_name,
            status="error",
            duration_ms=1.0,
            payload=None,
        )
    default_names = [
        json.loads(line)["tool_name"]
        for line in default_path.read_text(encoding="utf-8").splitlines()
    ]
    assert default_names == [
        "arcgraph_get_context",
        "unknown_tool",
        "unknown_tool",
    ]

    feedback_path = tmp_path / "feedback" / "mcp.jsonl"
    feedback_recorder = MCPMetricsRecorder(
        feedback_path,
        allowed_tool_names=frozenset(mcp_capability_names(feedback_enabled=True)),
    )
    feedback_recorder.record_tool_call(
        tool_name="arcgraph_record_trial_feedback",
        status="success",
        duration_ms=1.0,
        payload={},
    )
    assert json.loads(feedback_path.read_text(encoding="utf-8"))["tool_name"] == (
        "arcgraph_record_trial_feedback"
    )


def test_default_mcp_metrics_redact_unregistered_feedback_tool(
    tmp_path: Path,
) -> None:
    from arcgraph.interfaces.mcp_server import create_mcp_app
    from mcp.server.mcpserver.exceptions import ToolError

    metrics_path = tmp_path / "mcp.jsonl"
    app = create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=metrics_path,
    )

    async def call_unregistered_feedback() -> None:
        with pytest.raises(ToolError, match="Unknown tool"):
            await app.call_tool("arcgraph_record_trial_feedback", {})

    anyio.run(call_unregistered_feedback)

    event = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert event["tool_name"] == "unknown_tool"
