"""Exercise an installed ArcGraph stdio MCP server from MCP SDK v1 or v2.

This is a local package/release probe. It does not import ArcGraph into the
client environment, so a legacy client can exercise a separately installed v2
server without creating a conflicting dependency environment.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
import hashlib
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
import tempfile
from typing import Any

import anyio
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from scripts.arcgraph_trial_contract import EXPECTED_DEFAULT_TOOL_NAMES
except ModuleNotFoundError:  # Direct execution adds only scripts/ to sys.path.
    from arcgraph_trial_contract import EXPECTED_DEFAULT_TOOL_NAMES

SCHEMA_VERSION = "1.0"
EXPECTED_TOOL_NAMES = EXPECTED_DEFAULT_TOOL_NAMES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to an installed ArcGraph stdio MCP server, validate the "
            "documented tool contract, and exercise every tool."
        )
    )
    parser.add_argument("--server-python", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo-id", default="default")
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--mode",
        choices=["auto", "legacy"],
        default="legacy",
        help="MCP v2 supports both modes; MCP v1 supports legacy only.",
    )
    parser.add_argument("--expect-protocol")
    parser.add_argument("--expect-server-version")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = anyio.run(_run_probe, args)
    except Exception as exc:  # Boundary: render a machine-readable smoke failure.
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
    client_version = importlib_metadata.version("mcp")
    client_major = _major_version(client_version)
    if client_major == 1:
        if args.mode != "legacy":
            raise RuntimeError("MCP SDK v1 clients support only --mode legacy.")
        protocol = await _probe_v1(args)
    elif client_major == 2:
        protocol = await _probe_v2(args)
    else:
        raise RuntimeError(
            f"Unsupported MCP client SDK version {client_version!r}; "
            "the probe supports major versions 1 and 2."
        )

    if args.expect_protocol and protocol["protocol_version"] != args.expect_protocol:
        raise RuntimeError(
            "Unexpected negotiated protocol: "
            f"{protocol['protocol_version']!r} != {args.expect_protocol!r}."
        )
    server_version = protocol["server"]["version"]
    if args.expect_server_version and server_version != args.expect_server_version:
        raise RuntimeError(
            f"Unexpected ArcGraph server version: {server_version!r} != "
            f"{args.expect_server_version!r}."
        )
    if not protocol["server_stderr_empty"]:
        raise RuntimeError("The installed MCP server wrote unexpected stderr output.")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        # This line is reached only after the stdio/client context has exited.
        # A child that does not terminate keeps the context open until the
        # outer command timeout fails the package gate.
        "clean_shutdown": True,
        "client": {
            "mcp_version": client_version,
            "major": client_major,
            "mode": args.mode,
        },
        **protocol,
    }


async def _probe_v2(args: argparse.Namespace) -> dict[str, Any]:
    from mcp.client import Client

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(_server_parameters(args), errlog=errlog),
            mode=args.mode,
            raise_exceptions=True,
            read_timeout_seconds=args.timeout_seconds,
        ) as client:
            listing = await client.list_tools()
            contract = _tool_contract(listing.tools)
            calls = await _exercise_all_tools_v2(client, args)
            protocol_version = str(client.protocol_version)
            server = _server_identity(client.server_info)

        server_stderr = _read_stream(errlog)
    return _protocol_payload(
        protocol_version=protocol_version,
        server=server,
        contract=contract,
        calls=calls,
        server_stderr=server_stderr,
    )


async def _probe_v1(args: argparse.Namespace) -> dict[str, Any]:
    from mcp import ClientSession

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with stdio_client(
            _server_parameters(args),
            errlog=errlog,
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=args.timeout_seconds),
            ) as session:
                initialized = await session.initialize()
                listing = await session.list_tools()
                contract = _tool_contract(listing.tools)
                calls = await _exercise_all_tools_v1(session, args)
                protocol_version = str(
                    _field(initialized, "protocol_version", "protocolVersion")
                )
                server = _server_identity(
                    _field(initialized, "server_info", "serverInfo")
                )

        server_stderr = _read_stream(errlog)
    return _protocol_payload(
        protocol_version=protocol_version,
        server=server,
        contract=contract,
        calls=calls,
        server_stderr=server_stderr,
    )


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    server_python = Path(args.server_python).expanduser()
    if not server_python.is_absolute():
        server_python = Path.cwd() / server_python
    return StdioServerParameters(
        # Do not resolve this symlink: on POSIX a venv Python commonly points to
        # the base interpreter, and resolving it would discard the venv context.
        command=str(server_python),
        args=[
            "-m",
            "arcgraph.interfaces.mcp_server",
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "--repo-id",
            args.repo_id,
        ],
        cwd=repo_root,
    )


def _tool_contract(tools: list[Any]) -> dict[str, Any]:
    names = tuple(str(_field(tool, "name")) for tool in tools)
    if names != EXPECTED_TOOL_NAMES:
        raise RuntimeError(
            f"Unexpected MCP tool surface: {names!r} != {EXPECTED_TOOL_NAMES!r}."
        )
    records = [
        {
            "name": str(_field(tool, "name")),
            "description": _field(tool, "description"),
            "input_schema": _field(tool, "input_schema", "inputSchema"),
        }
        for tool in tools
    ]
    encoded = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "tool_count": len(records),
        "tool_names": list(names),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


async def _exercise_all_tools_v2(
    client: Any,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name, arguments in _tool_calls(args).items():
        result = await client.call_tool(name, arguments)
        records.append(_result_record(name, result))
    return records


async def _exercise_all_tools_v1(
    session: Any,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name, arguments in _tool_calls(args).items():
        result = await session.call_tool(name, arguments)
        records.append(_result_record(name, result))
    return records


def _result_record(name: str, result: Any) -> dict[str, Any]:
    is_error = bool(_field(result, "is_error", "isError"))
    structured = _field(result, "structured_content", "structuredContent")
    if is_error or not isinstance(structured, dict):
        raise RuntimeError(
            f"{name} did not return a structured contract result "
            f"(is_error={is_error!r})."
        )
    if structured.get("read_only") is not True:
        raise RuntimeError(f"{name} did not preserve read_only=true.")
    snippet_policy = structured.get("source_snippets")
    if (
        not isinstance(snippet_policy, dict)
        or snippet_policy.get("enabled") is not False
    ):
        raise RuntimeError(f"{name} did not preserve disabled source snippets.")
    return {
        "name": name,
        "status": structured.get("status"),
        "error_code": structured.get("error_code"),
        "keys": sorted(structured),
    }


def _tool_calls(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    repo_id = args.repo_id
    target = args.target
    missing_plan = "arcgraph-probe-missing-plan"
    return {
        "arcgraph_index_status": {"repo_id": repo_id},
        "arcgraph_get_context": {"repo_id": repo_id, "targets": [target]},
        "arcgraph_explain": {"repo_id": repo_id, "targets": [target]},
        "arcgraph_get_risk": {"repo_id": repo_id, "targets": [target]},
        "arcgraph_entrypoint_flow": {
            "repo_id": repo_id,
            "entrypoint": target,
        },
        "arcgraph_get_why": {"repo_id": repo_id, "target": target},
        "arcgraph_find_similar": {"repo_id": repo_id, "target": target},
        "arcgraph_record_learning": {
            "repo_id": repo_id,
            "target": target,
            "observation": "ArcGraph package protocol probe.",
        },
        "arcgraph_preview_change_plan": {
            "repo_id": repo_id,
            "task": "Validate the installed ArcGraph MCP package contract.",
            "targets": [{"kind": "symbol", "value": target}],
        },
        "arcgraph_get_change_plan": {
            "repo_id": repo_id,
            "plan_id": missing_plan,
        },
        "arcgraph_list_change_plans": {"repo_id": repo_id},
        "arcgraph_get_graph_delta": {
            "repo_id": repo_id,
            "plan_id": missing_plan,
            "revision": 1,
            "plan_content_digest": "0" * 64,
        },
        "arcgraph_verify_change": {
            "repo_id": repo_id,
            "plan_id": missing_plan,
            "revision": 1,
            "plan_content_digest": "0" * 64,
        },
        "arcgraph_help": {"topic": "workflow"},
    }


def _protocol_payload(
    *,
    protocol_version: str,
    server: dict[str, str],
    contract: dict[str, Any],
    calls: list[dict[str, Any]],
    server_stderr: str,
) -> dict[str, Any]:
    return {
        "protocol_version": protocol_version,
        "server": server,
        "tool_contract": contract,
        "calls": calls,
        "server_stderr_empty": not server_stderr,
        "server_stderr_tail": server_stderr[-1000:],
    }


def _server_identity(server_info: Any) -> dict[str, str]:
    return {
        "name": str(_field(server_info, "name")),
        "version": str(_field(server_info, "version")),
    }


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    raise AttributeError(f"{type(value).__name__} has none of fields {names!r}")


def _major_version(version: str) -> int:
    release = version.split("!", 1)[-1]
    major = release.split(".", 1)[0].lstrip("v")
    if not major.isdigit():
        raise RuntimeError(f"Cannot determine MCP client major from {version!r}.")
    return int(major)


def _read_stream(stream: Any) -> str:
    stream.seek(0)
    return str(stream.read())


if __name__ == "__main__":
    raise SystemExit(main())
