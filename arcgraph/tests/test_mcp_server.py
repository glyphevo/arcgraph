from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any

import anyio
import pytest

from arcgraph import __version__
from arcgraph.interfaces import mcp_server
from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.metrics import MCP_METRICS_WRITE_WARNING
from arcgraph.interfaces.mcp_tools import ArcGraphPermissionError


def test_cli_mcp_help_and_serve_help_do_not_require_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["mcp"]) == 0
    assert "serve" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        main(["mcp", "--help"])
    assert exc.value.code == 0
    assert "serve" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        main(["mcp", "serve", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--repo-root" in output
    assert "--output-dir" in output
    assert "--transport" in output
    assert "--expose-source-snippets" in output
    assert "--metrics-log" in output
    assert "--feedback-log" in output


def test_mcp_server_module_help_does_not_require_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        mcp_server.main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "read-only" in output
    assert "--repo-root" in output
    assert "--transport" in output
    assert "--metrics-log" in output
    assert "--feedback-log" in output


def test_mcp_server_uses_only_the_public_v2_server_api() -> None:
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")

    assert "from mcp.server import MCPServer" in source
    assert "mcp.server.fastmcp" not in source
    assert "mcp.server._" not in source


def test_mcp_server_missing_runtime_error_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_runtime(
        name: str,
        *,
        metrics_recorder: object | None = None,
    ) -> object:
        assert name == mcp_server.DEFAULT_MCP_SERVER_NAME
        assert metrics_recorder is None
        raise RuntimeError(
            "MCP runtime dependency is not installed. Reinstall ArcGraph from "
            "the same checkout or wheel with its optional `mcp` extra."
        )

    monkeypatch.setattr(mcp_server, "_create_mcp_server", fail_runtime)

    with pytest.raises(SystemExit) as exc:
        mcp_server.main(["--repo-root", str(tmp_path)])

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "MCP runtime dependency is not installed" in error
    assert "same checkout or wheel" in error


def test_create_tool_group_uses_safe_defaults(tmp_path: Path) -> None:
    tools = mcp_server.create_tool_group(repo_root=tmp_path, output_dir="out")
    repo = tools._resolve_repo("default")

    assert repo.root_path == tmp_path.resolve()
    assert repo.output_dir == (tmp_path / "out").resolve()
    assert repo.expose_source_snippets is False
    assert repo.mode == "read_only"

    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_get_context(targets=["..\\outside\\secret.py"])


def test_create_mcp_app_registers_read_only_tools_and_runs_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[_FakeMCPServer] = []

    _FakeMCPServer.instances = instances

    def create_fake_server(
        name: str,
        *,
        metrics_recorder: object | None = None,
    ) -> _FakeMCPServer:
        assert metrics_recorder is None
        return _FakeMCPServer(name)

    monkeypatch.setattr(mcp_server, "_create_mcp_server", create_fake_server)

    app = mcp_server.create_mcp_app(repo_root=tmp_path, output_dir="out")

    assert isinstance(app, _FakeMCPServer)
    assert app.name == mcp_server.DEFAULT_MCP_SERVER_NAME
    assert set(mcp_capability_names()).issubset(app.handlers)
    assert "arcgraph_record_learning" in app.handlers
    assert "arcgraph_record_trial_feedback" not in app.handlers

    status = app.handlers["arcgraph_index_status"]()
    assert status["status"] == "unavailable"
    assert status["read_only"] is True
    assert status["source_snippets"] == {"requested": False, "enabled": False}

    mcp_server._run_mcp_app(app, transport="stdio")
    assert app.runs == ["stdio"]
    assert instances == [app]


def test_create_mcp_app_registers_feedback_only_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_create_mcp_server",
        lambda name, *, metrics_recorder=None: _FakeMCPServer(name),
    )
    feedback_log = tmp_path / "private" / "trial.jsonl"

    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        feedback_log=feedback_log,
    )

    assert "arcgraph_record_trial_feedback" in app.handlers
    assert not feedback_log.exists()
    help_payload = app.handlers["arcgraph_help"](topic="feedback")
    assert help_payload["feedback"] == {
        "enabled": True,
        "cli_available": True,
        "mcp_enabled": True,
        "tool_registered": True,
        "tool_name": "arcgraph_record_trial_feedback",
        "persistence": "local_append_only",
        "guidance": (
            "Call arcgraph_record_trial_feedback when ArcGraph is inaccurate, "
            "unavailable, hard to discover, truncated, slow, missing a "
            "capability, or requires fallback."
        ),
    }

    payload = app.handlers["arcgraph_record_trial_feedback"](
        feedback={
            "client_event_id": "2eabf238-2099-4807-98c4-5bb8a151fb33",
            "surface": "mcp",
            "tool_name": "arcgraph_help",
            "outcome": "partial",
            "issue_kind": "discoverability",
            "stage": "preflight",
            "fallback": "none",
        }
    )

    assert payload["status"] == "recorded"
    assert payload["read_only"] is False
    assert payload["destructive"] is False
    assert feedback_log.exists()


def test_create_mcp_app_rejects_one_path_for_metrics_and_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_server_called = False

    def create_server(
        name: str,
        *,
        metrics_recorder: object | None = None,
    ) -> _FakeMCPServer:
        nonlocal create_server_called
        create_server_called = True
        return _FakeMCPServer(name)

    monkeypatch.setattr(mcp_server, "_create_mcp_server", create_server)
    shared_log = tmp_path / "private" / "trial.jsonl"

    with pytest.raises(
        RuntimeError,
        match="Metrics and feedback logs must use distinct local paths",
    ):
        mcp_server.create_mcp_app(
            repo_root=tmp_path,
            output_dir="out",
            metrics_log=shared_log,
            feedback_log=shared_log,
        )

    assert create_server_called is False
    assert not shared_log.exists()


def test_cli_mcp_rejects_global_metrics_aliasing_feedback_before_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared_log = tmp_path / "private" / "trial.jsonl"

    exit_code = main(
        [
            "--metrics-log",
            str(shared_log),
            "mcp",
            "serve",
            "--repo-root",
            str(tmp_path),
            "--feedback-log",
            str(shared_log),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == (
        "ArcGraph: Metrics and feedback logs must use distinct local paths.\n"
    )
    # JSON is the default output mode, so a refusal reaches stdout as an error
    # envelope rather than as silence. The stderr line and the exit code are
    # unchanged; this only fills what a caller parsing stdout used to miss.
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == payload["error_code"]
    assert "distinct local paths" in payload["error"]["message"]
    assert not shared_log.exists()


def test_mcp_feedback_write_failure_is_visible_to_the_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_create_mcp_server",
        lambda name, *, metrics_recorder=None: _FakeMCPServer(name),
    )
    feedback_directory = tmp_path / "not-a-log"
    feedback_directory.mkdir()
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        feedback_log=feedback_directory,
    )

    payload = app.handlers["arcgraph_record_trial_feedback"](
        feedback={
            "client_event_id": "80879e36-99e9-4a66-ac6f-0187c2ec5235",
            "surface": "mcp",
            "tool_name": "arcgraph_help",
            "outcome": "failed",
            "issue_kind": "integration",
            "stage": "recovery",
            "fallback": "human",
        }
    )

    assert payload["status"] == "error"
    assert payload["error_code"] == "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"
    assert payload["network_access"] is False


def test_mcp_metrics_write_failure_does_not_change_tool_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_metrics_path = tmp_path / "metrics-directory"
    invalid_metrics_path.mkdir()
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=invalid_metrics_path,
    )

    async def call_status_twice() -> tuple[Any, Any]:
        first = await app.call_tool("arcgraph_index_status", {})
        second = await app.call_tool("arcgraph_index_status", {})
        return first, second

    results = anyio.run(call_status_twice)

    assert all(result.is_error is False for result in results)
    assert all(
        result.structured_content["status"] == "unavailable" for result in results
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == MCP_METRICS_WRITE_WARNING


def test_mcp_metrics_io_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_record(
        self: object,
        *,
        tool_name: str,
        status: str,
        duration_ms: float,
        payload: object,
    ) -> None:
        assert tool_name == "arcgraph_index_status"
        assert status == "success"
        assert duration_ms >= 0
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(
        mcp_server.MCPMetricsRecorder,
        "record_tool_call",
        block_record,
    )
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=tmp_path / "private" / "metrics.jsonl",
    )

    async def exercise() -> tuple[float, Any]:
        result: Any = None
        started = time.perf_counter()

        async def call_status() -> None:
            nonlocal result
            result = await app.call_tool("arcgraph_index_status", {})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(call_status)
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            loop_delay = time.perf_counter() - started
            release.set()
        return loop_delay, result

    watchdog = threading.Timer(0.75, release.set)
    watchdog.start()
    try:
        loop_delay, result = anyio.run(exercise)
    finally:
        watchdog.cancel()
        release.set()

    assert loop_delay < 0.25
    assert result.is_error is False


def test_mcp_metrics_backpressure_uses_one_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    call_count = 0

    def block_record(self: object, **_kwargs: object) -> None:
        nonlocal active, max_active, call_count
        with state_lock:
            active += 1
            call_count += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(timeout=2)
        with state_lock:
            active -= 1

    monkeypatch.setattr(
        mcp_server.MCPMetricsRecorder,
        "record_tool_call",
        block_record,
    )
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=tmp_path / "private" / "metrics.jsonl",
    )

    async def exercise() -> list[Any]:
        results: list[Any] = []

        async def call_status() -> None:
            results.append(await app.call_tool("arcgraph_index_status", {}))

        async with anyio.create_task_group() as tasks:
            for _ in range(8):
                tasks.start_soon(call_status)
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            await anyio.sleep(0.05)
            with state_lock:
                assert call_count == 1
                assert max_active == 1
            release.set()
        return results

    watchdog = threading.Timer(0.75, release.set)
    watchdog.start()
    try:
        results = anyio.run(exercise)
    finally:
        watchdog.cancel()
        release.set()

    assert len(results) == 8
    assert all(result.is_error is False for result in results)
    assert call_count == 8


def test_mcp_metrics_error_timeout_is_not_reported_as_a_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shielded write slower than its bound still lands and stays enabled.

    abandon_on_cancel keeps the worker running, so exceeding the bound says
    nothing about whether the append succeeded.  Latching the recorder off
    there would claim a successful write failed and discard every later event.
    """

    from mcp.server.mcpserver.exceptions import ToolError

    metrics_log = tmp_path / "private" / "metrics.jsonl"
    state_lock = threading.Lock()
    first_writer_entered = threading.Event()
    release_first_writer = threading.Event()
    active = 0
    max_active = 0
    append_index = 0
    real_append = mcp_server.MCPMetricsRecorder._append_serialized_event

    def held_then_successful_append(self: Any, serialized_event: str) -> None:
        """Hold the first writer open until the test releases it, then succeed."""

        nonlocal active, max_active, append_index
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            index = append_index
            append_index += 1
        try:
            if index == 0:
                first_writer_entered.set()
                assert release_first_writer.wait(timeout=30)
            real_append(self, serialized_event)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        mcp_server.MCPMetricsRecorder,
        "_append_serialized_event",
        held_then_successful_append,
    )
    monkeypatch.setattr(mcp_server, "METRICS_WRITE_BOUND_SECONDS", 0.05)
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=metrics_log,
    )

    async def exercise() -> None:
        with pytest.raises(ToolError, match="Unknown tool"):
            await app.call_tool("arcgraph_not_registered", {})
        with anyio.fail_after(10):
            while not first_writer_entered.is_set():
                await anyio.sleep(0.001)
        # The abandoned writer is provably still inside its append here, so
        # this is the window where a success path outside the gate would start
        # a second writer and stall behind it.  A bounded wait fails loudly
        # instead of silently passing if the call ever queues.
        with anyio.fail_after(5):
            await app.call_tool("arcgraph_index_status", {})
        with state_lock:
            assert active == 1

        release_first_writer.set()
        with anyio.fail_after(10):
            while active:
                await anyio.sleep(0.001)
        with anyio.fail_after(10):
            await app.call_tool("arcgraph_index_status", {})

    try:
        anyio.run(exercise)
    finally:
        release_first_writer.set()

    captured = capsys.readouterr()
    assert captured.out == ""
    # Exceeding the bound is not a failure, so no write warning is emitted...
    assert captured.err == ""
    events = [
        json.loads(line)
        for line in metrics_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    # ...the abandoned worker still completed its append...
    assert any(event["status"] == "error" for event in events)
    # ...the recorder was never latched off, so later events still record...
    assert any(event["status"] == "success" for event in events)
    # ...and abandoned workers stay bounded even though the limiter token is
    # released the moment the bound expires.
    assert max_active == 1


def test_mcp_metrics_success_timeout_does_not_stall_tool_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contended best-effort log cannot hold completed tool responses."""

    first_writer_entered = threading.Event()
    release_first_writer = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    append_count = 0

    def hold_first_append(self: Any, _serialized_event: str) -> None:
        nonlocal active, max_active, append_count
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            append_count += 1
            current_append = append_count
        try:
            if current_append == 1:
                first_writer_entered.set()
                assert release_first_writer.wait(timeout=30)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        mcp_server.MCPMetricsRecorder,
        "_append_serialized_event",
        hold_first_append,
    )
    monkeypatch.setattr(mcp_server, "METRICS_WRITE_BOUND_SECONDS", 0.05)
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=tmp_path / "private" / "metrics.jsonl",
    )

    async def exercise() -> tuple[Any, Any, Any]:
        with anyio.fail_after(5):
            first = await app.call_tool("arcgraph_index_status", {})
        assert first_writer_entered.is_set()

        # The first worker still owns the gate.  The second tool result must
        # return after dropping its metrics event rather than waiting on the
        # abandoned worker or the shared thread limiter.
        with anyio.fail_after(5):
            second = await app.call_tool("arcgraph_help", {"topic": "overview"})
        with state_lock:
            assert active == 1
            assert max_active == 1
            assert append_count == 1

        release_first_writer.set()
        with anyio.fail_after(10):
            while active:
                await anyio.sleep(0.001)
        with anyio.fail_after(5):
            third = await app.call_tool("arcgraph_index_status", {})
        return first, second, third

    try:
        results = anyio.run(exercise)
    finally:
        release_first_writer.set()

    assert all(result.is_error is False for result in results)
    assert append_count == 2
    assert max_active == 1


def test_mcp_metrics_cancellation_does_not_replace_the_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    entered = threading.Event()
    release = threading.Event()

    def block_record(self: object, **_kwargs: object) -> None:
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(
        mcp_server.MCPMetricsRecorder,
        "record_tool_call",
        block_record,
    )
    app = mcp_server.create_mcp_app(
        repo_root=tmp_path,
        output_dir="out",
        metrics_log=tmp_path / "private" / "metrics.jsonl",
    )

    async def exercise() -> list[BaseException]:
        outcomes: list[BaseException] = []
        scopes: list[anyio.CancelScope] = []

        async def call_unknown_tool() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    await app.call_tool("arcgraph_not_registered", {})
                except BaseException as exc:
                    outcomes.append(exc)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(call_unknown_tool)
            with anyio.fail_after(2):
                while not entered.is_set() or not scopes:
                    await anyio.sleep(0.001)
            scopes[0].cancel()
            await anyio.sleep(0.01)
            release.set()
        return outcomes

    watchdog = threading.Timer(0.75, release.set)
    watchdog.start()
    try:
        outcomes = anyio.run(exercise)
    finally:
        watchdog.cancel()
        release.set()

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], ToolError)
    assert "Unknown tool" in str(outcomes[0])


def test_create_mcp_server_reports_arcgraph_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server.importlib_metadata,
        "version",
        lambda distribution: "2.0.0",
    )
    monkeypatch.setattr(
        mcp_server,
        "_create_public_mcp_server",
        lambda *, name, version, metrics_recorder=None: _FakeMCPServer(
            name,
            version=version,
        ),
    )

    app = mcp_server._create_mcp_server("ArcGraph Test")

    assert app.name == "ArcGraph Test"
    assert app.version == __version__


def test_create_mcp_server_distinguishes_missing_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(distribution: str) -> str:
        assert distribution == mcp_server.MCP_DISTRIBUTION_NAME
        raise mcp_server.importlib_metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(mcp_server.importlib_metadata, "version", missing)

    with pytest.raises(RuntimeError, match="dependency is not installed") as exc:
        mcp_server._create_mcp_server("ArcGraph Test")

    assert mcp_server.MCP_VERSION_REQUIREMENT in str(exc.value)


@pytest.mark.parametrize("version", ["1.28.1", "3.0.0", "invalid"])
def test_create_mcp_server_reports_unsupported_installed_version(
    version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server.importlib_metadata,
        "version",
        lambda distribution: version,
    )

    with pytest.raises(RuntimeError, match="is unsupported") as exc:
        mcp_server._create_mcp_server("ArcGraph Test")

    assert repr(version) in str(exc.value)
    assert mcp_server.MCP_VERSION_REQUIREMENT in str(exc.value)


def test_create_mcp_server_distinguishes_broken_supported_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server.importlib_metadata,
        "version",
        lambda distribution: "2.0.0",
    )

    def broken_import(
        *,
        name: str,
        version: str,
        metrics_recorder: object | None = None,
    ) -> object:
        raise ImportError("public API missing")

    monkeypatch.setattr(mcp_server, "_create_public_mcp_server", broken_import)

    with pytest.raises(RuntimeError, match="public MCPServer API") as exc:
        mcp_server._create_mcp_server("ArcGraph Test")

    assert "'2.0.0'" in str(exc.value)
    assert "clean environment" in str(exc.value)


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("2.0.0", True),
        ("2.9.1", True),
        ("2.0.0.post1", True),
        ("2.0.0+vendor.1", True),
        ("2.0.0+beta", True),
        ("2.1.0rc1", True),
        ("2.0.0rc1", False),
        ("2.0.0.dev1", False),
        ("1!2.0.0", False),
        ("1.28.1", False),
        ("3.0.0", False),
        ("3.0.0rc1", False),
        ("not-a-version", False),
    ],
)
def test_supported_mcp_version_tracks_supported_major(
    version: str,
    supported: bool,
) -> None:
    assert mcp_server._is_supported_mcp_version(version) is supported


class _FakeMCPServer:
    instances: list["_FakeMCPServer"] = []

    def __init__(self, name: str, *, version: str = "") -> None:
        self.name = name
        self.version = version
        self.handlers: dict[str, Any] = {}
        self.tool_metadata: dict[str, dict[str, object]] = {}
        self.runs: list[str] = []
        self.instances.append(self)

    def tool(
        self,
        *,
        name: str,
        title: str,
        description: str,
        annotations: object,
    ) -> object:
        assert title
        assert description
        self.tool_metadata[name] = {
            "title": title,
            "description": description,
            "annotations": annotations,
        }

        def decorator(func: object) -> object:
            self.handlers[name] = func
            return func

        return decorator

    def run(self, *, transport: str) -> None:
        self.runs.append(transport)


def test_anticipated_permission_error_uses_the_sdk_tool_error_when_available() -> None:
    """Anticipated tool failures must not be reported as SDK crashes."""

    assert issubclass(ArcGraphPermissionError, ValueError)
    exceptions = pytest.importorskip("mcp.server.mcpserver.exceptions")
    assert issubclass(ArcGraphPermissionError, exceptions.ToolError)


def test_public_mcp_server_suppresses_info_tool_failure_logs() -> None:
    """stdio stderr stays clean: SDK INFO-level failures are suppressed."""

    pytest.importorskip("mcp")
    for recorder in (None, object()):
        server = mcp_server._create_public_mcp_server(
            name=mcp_server.DEFAULT_MCP_SERVER_NAME,
            version=__version__,
            metrics_recorder=recorder,
        )
        assert server.settings.log_level == "WARNING"
