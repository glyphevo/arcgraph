"""Prove one installed ArcGraph environment isolates two local projects.

The probe intentionally imports no ArcGraph modules.  Package readiness runs it
with the wheel-installed interpreter, outside the source tree, against two
independent stdio MCP processes.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid

import anyio
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

try:
    from scripts.arcgraph_trial_contract import EXPECTED_FEEDBACK_TOOL_NAMES
except ModuleNotFoundError:  # Direct execution adds only scripts/ to sys.path.
    from arcgraph_trial_contract import EXPECTED_FEEDBACK_TOOL_NAMES

SCHEMA_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise one installed ArcGraph environment against two isolated "
            "temporary Python projects."
        )
    )
    parser.add_argument("--server-python", required=True)
    parser.add_argument("--arcgraph", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--expect-server-version", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = anyio.run(_run_probe, args)
    except Exception as exc:  # Boundary: machine-readable package-gate failure.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "failure": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


async def _run_probe(args: argparse.Namespace) -> dict[str, Any]:
    server_python = _absolute_existing_file(args.server_python, "--server-python")
    arcgraph = _absolute_existing_file(args.arcgraph, "--arcgraph")
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    if server_python.parent != arcgraph.parent:
        raise RuntimeError(
            "The server interpreter and ArcGraph executable must use one "
            "installed environment."
        )
    if args.timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be greater than zero.")

    project_a = _project_layout(workspace, "project-a")
    project_b = _project_layout(workspace, "project-b")
    _write_python_project(project_a, package="alpha_pkg", marker="AlphaService")
    _write_python_project(project_b, package="beta_pkg", marker="BetaService")
    installation_state_before = _installation_state_markers(Path(sys.prefix))

    _build_project(arcgraph, project_a, timeout=args.timeout_seconds)
    _build_project(arcgraph, project_b, timeout=args.timeout_seconds)
    current_a = _cli_json(
        arcgraph,
        project_a,
        ["current"],
        timeout=args.timeout_seconds,
    )
    current_b = _cli_json(
        arcgraph,
        project_b,
        ["current"],
        timeout=args.timeout_seconds,
    )
    _validate_current(project_a, current_a)
    _validate_current(project_b, current_b)

    feedback_id_a = str(uuid.uuid4())
    feedback_id_b = str(uuid.uuid4())
    expected_metrics_a: Counter[str] = Counter()
    expected_metrics_b: Counter[str] = Counter()
    stderr_a = ""
    stderr_b = ""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog_b:
        async with Client(
            stdio_client(
                _server_parameters(
                    server_python,
                    project_b,
                    name="ArcGraph Trial Project B",
                ),
                errlog=errlog_b,
            ),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=args.timeout_seconds,
        ) as client_b:
            identity_b = _server_identity(client_b)
            tools_b = await _list_tools(client_b)
            tool_names_b = _validate_tool_contract(tools_b.tools)
            status_b_before = _structured(
                await _call_tool(
                    client_b,
                    "arcgraph_index_status",
                    {},
                    expected_metrics=expected_metrics_b,
                )
            )
            own_b = _structured(
                await _call_tool(
                    client_b,
                    "arcgraph_get_context",
                    {"targets": ["beta_pkg.service.BetaService.beta"]},
                    expected_metrics=expected_metrics_b,
                )
            )
            _require_contains(own_b, "BetaService", excludes="AlphaService")
            help_b = _structured(
                await _call_tool(
                    client_b,
                    "arcgraph_help",
                    {"topic": "workflow"},
                    expected_metrics=expected_metrics_b,
                )
            )
            if help_b.get("status") != "available":
                raise RuntimeError("Project B Agent help was unavailable.")

            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog_a:
                async with Client(
                    stdio_client(
                        _server_parameters(
                            server_python,
                            project_a,
                            name="ArcGraph Trial Project A",
                        ),
                        errlog=errlog_a,
                    ),
                    mode="auto",
                    raise_exceptions=True,
                    read_timeout_seconds=args.timeout_seconds,
                ) as client_a:
                    identity_a = _server_identity(client_a)
                    tools_a = await _list_tools(client_a)
                    tool_names_a = _validate_tool_contract(tools_a.tools)
                    status_a = _structured(
                        await _call_tool(
                            client_a,
                            "arcgraph_index_status",
                            {},
                            expected_metrics=expected_metrics_a,
                        )
                    )
                    own_a = _structured(
                        await _call_tool(
                            client_a,
                            "arcgraph_get_context",
                            {"targets": ["alpha_pkg.service.AlphaService.alpha"]},
                            expected_metrics=expected_metrics_a,
                        )
                    )
                    _require_contains(own_a, "AlphaService", excludes="BetaService")
                    foreign = await _call_tool(
                        client_a,
                        "arcgraph_get_context",
                        {"targets": [str(project_b["source"].resolve())]},
                        expected_metrics=expected_metrics_a,
                    )
                    _validate_foreign_path_rejected(foreign)
                    feedback_a = _structured(
                        await _call_tool(
                            client_a,
                            "arcgraph_record_trial_feedback",
                            {
                                "feedback": _feedback(
                                    feedback_id_a,
                                    issue_kind="missing_capability",
                                    fallback="grep",
                                )
                            },
                            expected_metrics=expected_metrics_a,
                        )
                    )
                    if feedback_a.get("status") != "recorded":
                        raise RuntimeError("Project A feedback was not recorded.")
                stderr_a = _read_stream(errlog_a)

            status_b_after = _structured(
                await _call_tool(
                    client_b,
                    "arcgraph_index_status",
                    {},
                    expected_metrics=expected_metrics_b,
                )
            )
            if status_b_after != status_b_before:
                raise RuntimeError(
                    "Stopping project A changed project B's index-status result."
                )
            feedback_b = _structured(
                await _call_tool(
                    client_b,
                    "arcgraph_record_trial_feedback",
                    {
                        "feedback": _feedback(
                            feedback_id_b,
                            issue_kind="latency",
                            fallback="direct_read",
                        )
                    },
                    expected_metrics=expected_metrics_b,
                )
            )
            if feedback_b.get("status") != "recorded":
                raise RuntimeError("Project B feedback was not recorded.")
        stderr_b = _read_stream(errlog_b)

    if stderr_a or stderr_b:
        raise RuntimeError("An installed multi-project MCP server wrote stderr.")
    if identity_a["name"] == identity_b["name"]:
        raise RuntimeError("The two project MCP servers reported the same name.")
    for identity in (identity_a, identity_b):
        if identity["version"] != args.expect_server_version:
            raise RuntimeError("An MCP server reported the wrong product version.")
    for status_payload in (status_a, status_b_before, status_b_after):
        if status_payload.get("repo_id") != "default":
            raise RuntimeError("A single-project server did not use repo id default.")
    if tool_names_a != tool_names_b:
        raise RuntimeError("The two feedback-enabled servers exposed different tools.")

    cli_feedback_id_a = str(uuid.uuid4())
    cli_feedback_a = _cli_json(
        arcgraph,
        project_a,
        [
            "feedback",
            "record",
            "--feedback-log",
            str(project_a["feedback"]),
            "--client-event-id",
            cli_feedback_id_a,
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
            "--freshness-status",
            "fresh",
        ],
        timeout=args.timeout_seconds,
        include_repo_args=False,
    )
    if cli_feedback_a.get("status") != "recorded":
        raise RuntimeError("Installed CLI feedback was not recorded.")

    metrics_a = _read_jsonl(project_a["metrics"])
    metrics_b = _read_jsonl(project_b["metrics"])
    _validate_separate_metrics(
        metrics_a,
        metrics_b,
        expected_a=expected_metrics_a,
        expected_b=expected_metrics_b,
    )
    feedback_a_records = _read_jsonl(project_a["feedback"])
    feedback_b_records = _read_jsonl(project_b["feedback"])
    _validate_separate_feedback(
        feedback_a_records,
        feedback_b_records,
        event_ids_a=(feedback_id_a, cli_feedback_id_a),
        event_ids_b=(feedback_id_b,),
    )
    summary_a = _cli_json(
        arcgraph,
        project_a,
        ["feedback", "summarize", str(project_a["feedback"])],
        timeout=args.timeout_seconds,
        include_repo_args=False,
    )
    summary_b = _cli_json(
        arcgraph,
        project_b,
        ["feedback", "summarize", str(project_b["feedback"])],
        timeout=args.timeout_seconds,
        include_repo_args=False,
    )
    if summary_a.get("by_issue_kind") != {
        "discoverability": 1,
        "missing_capability": 1,
    }:
        raise RuntimeError("Project A feedback summary crossed project boundaries.")
    if summary_a.get("by_surface") != {"cli": 1, "mcp": 1}:
        raise RuntimeError("Installed CLI/MCP feedback surfaces were not summarized.")
    if summary_b.get("by_issue_kind") != {"latency": 1}:
        raise RuntimeError("Project B feedback summary crossed project boundaries.")

    state_alias_policy = _validate_symlinked_state_alias(
        arcgraph,
        project_a,
        project_b,
        timeout=args.timeout_seconds,
    )
    _validate_private_modes(project_a)
    _validate_private_modes(project_b)
    installation_state_after = _installation_state_markers(Path(sys.prefix))
    if installation_state_after != installation_state_before:
        raise RuntimeError(
            "Project state markers appeared inside the installed environment."
        )
    _validate_no_client_configuration(project_a)
    _validate_no_client_configuration(project_b)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "one_installed_environment": True,
        "project_count": 2,
        "repo_ids": ["default", "default"],
        "feedback_tool_names": list(tool_names_a),
        "installed_cli_feedback": True,
        "server_names_distinct": True,
        "output_directories_distinct": True,
        "path_authorization_fail_closed": True,
        "current_and_graph_state_isolated": True,
        "metrics_isolated": True,
        "feedback_isolated": True,
        "state_alias_policy": state_alias_policy,
        "peer_survived_other_server_shutdown": True,
        "installation_state_unchanged": True,
        "client_configuration_unchanged": True,
        "permission_model": (
            "posix_private_modes_verified"
            if os.name == "posix"
            else "windows_acl_not_asserted"
        ),
    }


def _project_layout(workspace: Path, name: str) -> dict[str, Path]:
    root = workspace / name
    return {
        "root": root,
        "source": root / "src" / name.replace("-", "_") / "service.py",
        "output": root / ".arcgraph-trial" / "index",
        "metrics": root / ".arcgraph-trial" / "metrics" / "mcp.jsonl",
        "feedback": root / ".arcgraph-trial" / "feedback" / "agent.jsonl",
    }


def _write_python_project(
    layout: dict[str, Path],
    *,
    package: str,
    marker: str,
) -> None:
    source = layout["root"] / "src" / package / "service.py"
    layout["source"] = source
    source.parent.mkdir(parents=True)
    (source.parent / "__init__.py").write_text("", encoding="utf-8")
    method = "alpha" if marker == "AlphaService" else "beta"
    source.write_text(
        f"class {marker}:\n"
        f"    def {method}(self) -> str:\n"
        f'        return "{marker}"\n',
        encoding="utf-8",
    )
    (layout["root"] / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{package}-trial"\n'
        'version = "0.0.0"\n\n'
        "[tool.arcgraph]\n"
        'source_roots = ["src"]\n',
        encoding="utf-8",
    )


def _build_project(
    arcgraph: Path,
    layout: dict[str, Path],
    *,
    timeout: float,
) -> None:
    payload = _cli_json(
        arcgraph,
        layout,
        ["build"],
        timeout=timeout,
    )
    if not isinstance(payload.get("build_dir"), str) or not isinstance(
        payload.get("node_count"), int
    ):
        raise RuntimeError("An installed ArcGraph build did not succeed.")


def _cli_json(
    arcgraph: Path,
    layout: dict[str, Path],
    arguments: list[str],
    *,
    timeout: float,
    include_repo_args: bool = True,
) -> dict[str, Any]:
    command = [str(arcgraph)]
    if include_repo_args:
        command.extend(
            [
                "--repo-root",
                str(layout["root"]),
                "--output-dir",
                str(layout["output"]),
            ]
        )
    command.extend(arguments)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("NODE_PATH", None)
    completed = subprocess.run(
        command,
        cwd=layout["root"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "An installed ArcGraph command failed: " + completed.stderr[-500:]
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "An installed ArcGraph command returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("An installed ArcGraph command returned a non-object.")
    return payload


def _server_parameters(
    server_python: Path,
    layout: dict[str, Path],
    *,
    name: str,
) -> StdioServerParameters:
    return StdioServerParameters(
        command=str(server_python),
        args=[
            "-m",
            "arcgraph.interfaces.mcp_server",
            "--name",
            name,
            "--repo-root",
            str(layout["root"]),
            "--output-dir",
            str(layout["output"]),
            "--metrics-log",
            str(layout["metrics"]),
            "--feedback-log",
            str(layout["feedback"]),
        ],
        cwd=layout["root"],
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "NODE_PATH"}
        },
    )


def _feedback(
    client_event_id: str,
    *,
    issue_kind: str,
    fallback: str,
) -> dict[str, Any]:
    return {
        "client_event_id": client_event_id,
        "surface": "mcp",
        "tool_name": "arcgraph_help",
        "outcome": "partial",
        "issue_kind": issue_kind,
        "stage": "interpretation",
        "fallback": fallback,
        "result_status": "partial",
        "freshness_status": "fresh",
        "warning_kinds": [],
    }


def _structured(result: Any) -> dict[str, Any]:
    if bool(getattr(result, "is_error", False)):
        raise RuntimeError("An installed MCP tool returned a protocol error.")
    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict):
        raise RuntimeError("An installed MCP tool returned no structured content.")
    return payload


async def _list_tools(client: Client) -> Any:
    return await client.list_tools()


async def _call_tool(
    client: Client,
    name: str,
    arguments: dict[str, Any],
    *,
    expected_metrics: Counter[str],
) -> Any:
    result = await client.call_tool(name, arguments)
    expected_metrics[name] += 1
    return result


def _server_identity(client: Client) -> dict[str, str]:
    return {
        "name": str(client.server_info.name),
        "version": str(client.server_info.version),
    }


def _validate_tool_contract(tools: list[Any]) -> tuple[str, ...]:
    names = tuple(str(tool.name) for tool in tools)
    if names != EXPECTED_FEEDBACK_TOOL_NAMES:
        raise RuntimeError(
            "A feedback-enabled server did not match the exact trial tool contract."
        )
    return names


def _validate_current(layout: dict[str, Path], payload: dict[str, Any]) -> None:
    if Path(str(payload.get("repo_root", ""))).resolve() != layout["root"].resolve():
        raise RuntimeError("A current result reported the wrong repository root.")
    try:
        pointer: dict[str, Any] = json.loads(
            (layout["output"] / "current.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("A project current pointer is missing or invalid.") from exc
    build_dir = pointer.get("build_dir")
    if not isinstance(build_dir, str):
        raise RuntimeError("A current pointer did not identify its build directory.")
    resolved_build: Path = (layout["output"] / build_dir).resolve()
    if not resolved_build.is_relative_to(layout["output"].resolve()):
        raise RuntimeError("A current pointer escaped its project output directory.")
    if pointer.get("index_version") != payload.get("index_version"):
        raise RuntimeError("A current pointer and query result disagree.")


def _require_contains(
    payload: dict[str, Any],
    marker: str,
    *,
    excludes: str,
) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    if marker not in encoded or excludes in encoded:
        raise RuntimeError("A project query returned crossed or missing graph data.")


def _validate_foreign_path_rejected(result: Any) -> None:
    encoded = (
        result.model_dump_json()
        if hasattr(result, "model_dump_json")
        else json.dumps(result, sort_keys=True, default=str)
    )
    if "BetaService" in encoded:
        raise RuntimeError("Project A exposed project B graph content.")
    if bool(getattr(result, "is_error", False)):
        return
    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict):
        raise RuntimeError("A foreign project path produced no bounded result.")
    if payload.get("status") not in {"error", "unavailable", "partial"}:
        raise RuntimeError("A foreign project path was not rejected or degraded.")
    if payload.get("resolved_targets"):
        raise RuntimeError("A foreign project path resolved inside project A.")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("An isolated local log is missing or invalid.") from exc
    if not records or not all(isinstance(record, dict) for record in records):
        raise RuntimeError("An isolated local log contained no object records.")
    return records


def _validate_separate_metrics(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
    *,
    expected_a: Counter[str],
    expected_b: Counter[str],
) -> None:
    counts_a = Counter(str(record.get("tool_name")) for record in records_a)
    counts_b = Counter(str(record.get("tool_name")) for record in records_b)
    _validate_metric_counts("A", counts_a, expected_a)
    _validate_metric_counts("B", counts_b, expected_b)
    for records in (records_a, records_b):
        encoded = json.dumps(records, sort_keys=True)
        if "repo_id" in encoded or "targets" in encoded:
            raise RuntimeError("MCP metrics exposed forbidden project inputs.")


def _validate_metric_counts(
    project: str,
    observed: Counter[str],
    expected: Counter[str],
) -> None:
    unexpected = observed - expected
    if unexpected:
        raise RuntimeError(
            f"Project {project} metrics contained unexpected events; "
            "project isolation was not proven."
        )
    missing = expected - observed
    if missing:
        raise RuntimeError(
            f"Project {project} best-effort metrics evidence was incomplete."
        )


def _validate_separate_feedback(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
    *,
    event_ids_a: tuple[str, ...],
    event_ids_b: tuple[str, ...],
) -> None:
    encoded_a = json.dumps(records_a, sort_keys=True)
    encoded_b = json.dumps(records_b, sort_keys=True)
    for event_id in event_ids_a:
        if event_id not in encoded_a or event_id in encoded_b:
            raise RuntimeError("Project A feedback crossed project boundaries.")
    for event_id in event_ids_b:
        if event_id not in encoded_b or event_id in encoded_a:
            raise RuntimeError("Project B feedback crossed project boundaries.")


def _validate_private_modes(layout: dict[str, Path]) -> None:
    if os.name != "posix":
        return
    _require_private_mode(layout["metrics"])
    _require_private_mode(layout["feedback"])


def _validate_symlinked_state_alias(
    arcgraph: Path,
    project_a: dict[str, Path],
    project_b: dict[str, Path],
    *,
    timeout: float,
) -> str:
    if os.name != "posix":
        return "windows_reparse_not_asserted"

    alias = project_b["root"] / ".arcgraph-aliased-state"
    try:
        alias.symlink_to(
            project_a["root"] / ".arcgraph-trial",
            target_is_directory=True,
        )
    except OSError as exc:
        raise RuntimeError(
            "The POSIX trial could not create its state-alias security probe."
        ) from exc

    protected_log = project_a["feedback"]
    before = protected_log.read_bytes()
    command = [
        str(arcgraph),
        "feedback",
        "record",
        "--feedback-log",
        str(alias / "feedback" / "agent.jsonl"),
        "--client-event-id",
        str(uuid.uuid4()),
        "--surface",
        "cli",
        "--tool-name",
        "help",
        "--outcome",
        "not_used",
        "--issue-kind",
        "integration",
        "--stage",
        "preflight",
        "--fallback",
        "abandoned",
        "--freshness-status",
        "fresh",
    ]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("NODE_PATH", None)
    completed = subprocess.run(
        command,
        cwd=project_b["root"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The state-alias rejection did not return structured feedback failure."
        ) from exc
    if (
        completed.returncode != 2
        or payload.get("status") != "error"
        or payload.get("error_code") != "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"
    ):
        raise RuntimeError("A symlinked project state alias was not rejected.")
    if protected_log.read_bytes() != before:
        raise RuntimeError("A symlinked project state alias modified peer feedback.")
    return "posix_parent_symlinks_rejected"


def _require_private_mode(path: Path) -> None:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("A local trial log was not created with mode 0600.")
    parent: Path = path.parent
    if stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise RuntimeError(
            "A new local trial state directory was not created with mode 0700."
        )


def _installation_state_markers(prefix: Path) -> set[str]:
    markers: set[str] = set()
    for pattern in ("*.jsonl", "graph.sqlite", "current.json"):
        for path in prefix.rglob(pattern):
            markers.add(str(path.relative_to(prefix)))
    return markers


def _validate_no_client_configuration(layout: dict[str, Path]) -> None:
    for relative in (
        ".claude",
        ".cursor",
        ".vscode",
        ".mcp.json",
        "CLAUDE.md",
        "AGENTS.md",
    ):
        if (layout["root"] / relative).exists():
            raise RuntimeError("ArcGraph modified an Agent client configuration.")


def _absolute_existing_file(value: str, option: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{option} must identify an absolute existing file.")
    return path


def _read_stream(stream: Any) -> str:
    stream.seek(0)
    return str(stream.read())


if __name__ == "__main__":
    raise SystemExit(main())
