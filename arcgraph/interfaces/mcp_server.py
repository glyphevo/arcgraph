"""Alpha local MCP server entrypoint for ArcGraph."""

from __future__ import annotations

import argparse
from importlib import metadata as importlib_metadata
from pathlib import Path
import re
import threading
import time
from typing import Any

from arcgraph import __version__
from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from arcgraph.interfaces.local_state import local_state_paths_alias
from arcgraph.interfaces.metrics import MCPMetricsRecorder
from arcgraph.interfaces.mcp_tools import (
    ArcGraphMCPConfig,
    ArcGraphMCPToolGroup,
    register_arcgraph_mcp_tools,
)
from arcgraph.interfaces.trial_feedback import (
    DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE,
    TrialFeedbackStore,
)

DEFAULT_MCP_SERVER_NAME = "ArcGraph"
DEFAULT_MCP_TRANSPORT = "stdio"
MCP_DISTRIBUTION_NAME = "mcp"
MCP_VERSION_REQUIREMENT = ">=2.0.0,<3.0.0"
METRICS_WRITE_BOUND_SECONDS = 1.0
_MCP_RELEASE_VERSION = re.compile(
    r"^v?(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<suffix>.*)$",
    re.IGNORECASE,
)
_MCP_PRERELEASE_MARKER = re.compile(
    r"(?:a|alpha|b|beta|c|pre|preview|rc|dev)",
    re.IGNORECASE,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m arcgraph.interfaces.mcp_server",
        description=(
            "Run the alpha local ArcGraph MCP server over stdio. Its "
            "default analysis/change/help surface is read-only; an explicit "
            "feedback log enables one local append tool."
        ),
    )
    add_mcp_server_args(parser)
    return parser


def add_mcp_server_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to expose through the MCP analysis facade.",
    )
    parser.add_argument(
        "--output-dir",
        default="output/arcgraph",
        help="ArcGraph output directory. Relative paths are resolved under repo root.",
    )
    parser.add_argument(
        "--repo-id",
        default="default",
        help="MCP repo id to register for this server.",
    )
    parser.add_argument(
        "--allowed-root",
        action="append",
        default=[],
        help=(
            "Allowed filesystem root. Can be repeated. Defaults to the resolved "
            "repo root."
        ),
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_MCP_SERVER_NAME,
        help="MCP server name reported to the host runtime.",
    )
    parser.add_argument(
        "--transport",
        default=DEFAULT_MCP_TRANSPORT,
        choices=[DEFAULT_MCP_TRANSPORT],
        help="MCP transport. Only stdio is supported in this alpha preview.",
    )
    parser.add_argument(
        "--expose-source-snippets",
        action="store_true",
        help=(
            "Allow source snippets when a tool request explicitly asks for them. "
            "Disabled by default."
        ),
    )
    parser.add_argument(
        "--trusted-runner-registry",
        help=(
            "Operator-controlled trusted-runner JSON registry outside the analyzed "
            "repository. Omit for the fail-closed default of no trusted runners."
        ),
    )
    parser.add_argument(
        "--metrics-log",
        dest="mcp_metrics_log",
        help=(
            "Optional local JSONL path for privacy-bounded per-tool MCP metrics. "
            "Disabled by default and must differ from --feedback-log."
        ),
    )
    parser.add_argument(
        "--feedback-log",
        dest="mcp_feedback_log",
        help=(
            "Optional absolute local JSONL path for privacy-bounded Agent trial "
            "feedback. It must differ from --metrics-log; the feedback tool is "
            "not registered when omitted."
        ),
    )


def create_tool_group(
    *,
    repo_root: str | Path,
    output_dir: str | Path,
    repo_id: str = "default",
    allowed_roots: list[str | Path] | None = None,
    expose_source_snippets: bool = False,
    trusted_runner_registry_path: str | Path | None = None,
    feedback_log: str | Path | None = None,
) -> ArcGraphMCPToolGroup:
    root = Path(repo_root).resolve()
    resolved_allowed = (
        [Path(allowed).resolve() for allowed in allowed_roots]
        if allowed_roots
        else [root]
    )
    config = ArcGraphMCPConfig.for_single_repo(
        repo_root=root,
        repo_id=repo_id,
        output_dir=output_dir,
        allowed_roots=resolved_allowed,
        expose_source_snippets=expose_source_snippets,
        trusted_runner_registry_path=trusted_runner_registry_path,
        feedback_store=(
            TrialFeedbackStore(Path(feedback_log)) if feedback_log is not None else None
        ),
    )
    return ArcGraphMCPToolGroup(config)


def create_mcp_app(
    *,
    name: str = DEFAULT_MCP_SERVER_NAME,
    repo_root: str | Path = ".",
    output_dir: str | Path = "output/arcgraph",
    repo_id: str = "default",
    allowed_roots: list[str | Path] | None = None,
    expose_source_snippets: bool = False,
    trusted_runner_registry_path: str | Path | None = None,
    metrics_log: str | Path | None = None,
    feedback_log: str | Path | None = None,
) -> Any:
    if (
        metrics_log is not None
        and feedback_log is not None
        and local_state_paths_alias(Path(metrics_log), Path(feedback_log))
    ):
        raise RuntimeError(DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE)
    tool_group = create_tool_group(
        repo_root=repo_root,
        output_dir=output_dir,
        repo_id=repo_id,
        allowed_roots=allowed_roots,
        expose_source_snippets=expose_source_snippets,
        trusted_runner_registry_path=trusted_runner_registry_path,
        feedback_log=feedback_log,
    )
    metrics_recorder = (
        MCPMetricsRecorder(
            Path(metrics_log),
            allowed_tool_names=frozenset(
                mcp_capability_names(feedback_enabled=tool_group.feedback_enabled)
            ),
        )
        if metrics_log is not None
        else None
    )
    mcp = _create_mcp_server(name, metrics_recorder=metrics_recorder)
    register_arcgraph_mcp_tools(mcp, tool_group)
    return mcp


def serve(args: argparse.Namespace) -> int:
    allowed_roots = [Path(root).resolve() for root in args.allowed_root]
    app = create_mcp_app(
        name=args.name,
        repo_root=Path(args.repo_root).resolve(),
        output_dir=args.output_dir,
        repo_id=args.repo_id,
        allowed_roots=allowed_roots or None,
        expose_source_snippets=args.expose_source_snippets,
        trusted_runner_registry_path=args.trusted_runner_registry,
        metrics_log=args.mcp_metrics_log,
        feedback_log=args.mcp_feedback_log,
    )
    _run_mcp_app(app, transport=args.transport)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return serve(args)
    except RuntimeError as exc:
        parser.exit(2, f"ArcGraph MCP: {exc}\n")


def _create_mcp_server(
    name: str,
    *,
    metrics_recorder: MCPMetricsRecorder | None = None,
) -> Any:
    try:
        installed_version = importlib_metadata.version(MCP_DISTRIBUTION_NAME)
    except importlib_metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "MCP runtime dependency is not installed. Reinstall ArcGraph from "
            "the same checkout or wheel with its optional `mcp` extra; from a "
            "checkout use `python -m pip install -e '.[mcp]'` "
            f"(requires mcp{MCP_VERSION_REQUIREMENT})."
        ) from exc

    if not _is_supported_mcp_version(installed_version):
        raise RuntimeError(
            f"installed mcp version {installed_version!r} is unsupported; "
            f"ArcGraph requires mcp{MCP_VERSION_REQUIREMENT}."
        )

    try:
        return _create_public_mcp_server(
            name=name,
            version=__version__,
            metrics_recorder=metrics_recorder,
        )
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"installed mcp version {installed_version!r} is in the supported "
            f"range mcp{MCP_VERSION_REQUIREMENT}, but its public MCPServer API "
            "could not be loaded; reinstall the ArcGraph MCP extra in a clean "
            "environment."
        ) from exc


def _is_supported_mcp_version(version: str) -> bool:
    match = _MCP_RELEASE_VERSION.match(version)
    if match is None or int(match.group("epoch") or 0) != 0:
        return False
    release = tuple(int(part) for part in match.group("release").split("."))
    normalized_release = (*release, 0, 0)[:3]
    if not (normalized_release >= (2, 0, 0) and normalized_release < (3, 0, 0)):
        return False
    if normalized_release == (2, 0, 0):
        public_suffix = match.group("suffix").split("+", 1)[0]
        return _MCP_PRERELEASE_MARKER.search(public_suffix) is None
    return True


def _create_public_mcp_server(
    *,
    name: str,
    version: str,
    metrics_recorder: MCPMetricsRecorder | None = None,
) -> Any:
    from mcp.server import MCPServer

    if metrics_recorder is None:
        # Anticipated tool failures are logged by the SDK at INFO on stderr.
        # A stdio server must keep stderr clean, so only WARNING and above are
        # emitted; genuine crashes still surface at ERROR with a traceback.
        return MCPServer(name, version=version, log_level="WARNING")

    metrics_limiter: Any | None = None
    # abandon_on_cancel releases the limiter token as soon as the shield's
    # bound expires, so the limiter cannot bound abandoned workers.  This gate
    # does, and the worker owns it end to end so an abandoned write still
    # releases it.
    abandoned_write_gate = threading.Semaphore(1)

    class ArcGraphMeasuredMCPServer(MCPServer):
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            context: Any | None = None,
        ) -> Any:
            nonlocal metrics_limiter
            if metrics_limiter is None:
                import anyio

                metrics_limiter = anyio.CapacityLimiter(1)
            started = time.perf_counter()
            try:
                result = await super().call_tool(name, arguments, context)
            except Exception:
                await _record_mcp_metrics(
                    metrics_recorder,
                    tool_name=name,
                    status="error",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    payload=None,
                    limiter=metrics_limiter,
                    preserve_tool_error=True,
                    abandoned_write_gate=abandoned_write_gate,
                )
                raise
            payload = getattr(result, "structured_content", None)
            await _record_mcp_metrics(
                metrics_recorder,
                tool_name=name,
                status=_mcp_result_status(result, payload),
                duration_ms=(time.perf_counter() - started) * 1000,
                payload=payload,
                limiter=metrics_limiter,
                abandoned_write_gate=abandoned_write_gate,
            )
            return result

    return ArcGraphMeasuredMCPServer(name, version=version, log_level="WARNING")


async def _record_mcp_metrics(
    recorder: MCPMetricsRecorder,
    *,
    tool_name: str,
    status: str,
    duration_ms: float,
    payload: Any | None,
    limiter: Any | None = None,
    preserve_tool_error: bool = False,
    abandoned_write_gate: threading.Semaphore | None = None,
) -> None:
    """Run best-effort local metrics I/O off the MCP event loop.

    Every metrics write has a hard bound because a peer process can hold the
    local file lock indefinitely.  Metrics are best-effort, so the completed
    tool result takes priority over that event.  On the tool-error path the
    bounded wait is additionally shielded so an arriving cancellation cannot
    replace the tool error already in flight.

    Reaching the bound is not a write failure.  ``abandon_on_cancel`` leaves the
    worker running, so the append may still land and the worker reports its own
    outcome through ``record_tool_call``.  Latching the recorder off here would
    report a slow write as a failed one and discard every later event.
    Abandoned workers are bounded by ``abandoned_write_gate`` instead, which
    the worker holds for its whole lifetime -- the limiter cannot do it,
    because its token is released the moment the bound expires.

    Both paths share that gate.  Because an abandoned worker gives up its
    limiter token while it is still holding the file lock, a success-path write
    that skipped the gate would start a second concurrent worker and then stall
    its own tool response behind the abandoned one.  Dropping the event is the
    best-effort contract; delaying a tool result for it is not.
    """

    import anyio

    try:
        with anyio.CancelScope(shield=preserve_tool_error):
            with anyio.move_on_after(METRICS_WRITE_BOUND_SECONDS):
                await _offload_mcp_metrics(
                    recorder,
                    tool_name,
                    status,
                    duration_ms,
                    payload,
                    limiter,
                    abandoned_write_gate,
                )
    except Exception:
        # record_tool_call already latches and warns for its own write
        # failures, so anything reaching here is an offload failure that would
        # otherwise disable metrics for the process without any diagnostic.
        recorder.disable_after_write_failure()


async def _offload_mcp_metrics(
    recorder: MCPMetricsRecorder,
    tool_name: str,
    status: str,
    duration_ms: float,
    payload: Any | None,
    limiter: Any | None,
    gate: threading.Semaphore | None,
) -> None:
    import anyio

    await anyio.to_thread.run_sync(
        _record_mcp_metrics_sync,
        recorder,
        tool_name,
        status,
        duration_ms,
        payload,
        gate,
        abandon_on_cancel=True,
        limiter=limiter,
    )


def _record_mcp_metrics_sync(
    recorder: MCPMetricsRecorder,
    tool_name: str,
    status: str,
    duration_ms: float,
    payload: Any | None,
    gate: threading.Semaphore | None = None,
) -> None:
    if gate is not None and not gate.acquire(blocking=False):
        # An earlier shielded write is still running after its bound expired.
        # Dropping this event is the best-effort contract; queueing it would
        # strand one abandoned worker per tool error.
        return
    try:
        recorder.record_tool_call(
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            payload=payload,
        )
    finally:
        if gate is not None:
            gate.release()


def _mcp_result_status(result: Any, payload: Any) -> str:
    if getattr(result, "is_error", False):
        return "error"
    if isinstance(payload, dict) and payload.get("status") in {"error", "failed"}:
        return "error"
    return "success"


def _run_mcp_app(app: Any, *, transport: str) -> None:
    app.run(transport=transport)


if __name__ == "__main__":
    raise SystemExit(main())
