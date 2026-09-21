"""CLI handlers and parser wiring for ``arcgraph workspace`` and
``arcgraph benchmark``.

Both groups are leaves: they reference nothing else in ``cli.py`` and are
referenced only by ``build_parser``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from arcgraph.interfaces.benchmark import (
    DEFAULT_AGENT_STARTUP_TARGET,
    run_agent_startup_benchmark,
    run_benchmark_suite,
)
from arcgraph.interfaces.workspace import (
    DEFAULT_WORKSPACE_CONFIG,
    WORKSPACE_CONTEXT_DETAIL_LEVELS,
    WORKSPACE_RESOLVE_KINDS,
    workspace_context,
    workspace_resolve,
    workspace_status,
)


def handle_workspace_status(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    return workspace_status(
        config_path, include_dependency_hints=args.include_dependency_hints
    )


def handle_workspace_resolve(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    return workspace_resolve(
        config_path,
        args.target,
        kind=args.kind,
        limit=args.limit,
        include_dependency_hints=args.include_dependency_hints,
    )


def handle_workspace_context(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    return workspace_context(
        config_path,
        args.target,
        kind=args.kind,
        limit=args.limit,
        detail_level=args.detail_level,
        include_dependency_hints=args.include_dependency_hints,
    )


def handle_benchmark_agent_startup(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_path = Path(args.output) if args.output else None
    if output_path is not None and not output_path.is_absolute():
        output_path = repo_root / output_path
    return run_agent_startup_benchmark(
        repo_root=repo_root,
        output_dir=output_dir,
        target=args.target,
        iterations=args.iterations,
        warmups=args.warmups,
        command_set=args.command_set,
        timeout_seconds=args.timeout_seconds,
        output_path=output_path,
    )


def handle_benchmark_suite(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_path = Path(args.output) if args.output else None
    if output_path is not None and not output_path.is_absolute():
        output_path = repo_root / output_path
    return run_benchmark_suite(
        repo_root=repo_root,
        output_dir=output_dir,
        target=args.target,
        iterations=args.iterations,
        warmups=args.warmups,
        timeout_seconds=args.timeout_seconds,
        output_path=output_path,
        include_build=args.include_build,
    )


def add_workspace_parser(subparsers: Any) -> None:
    """Register the ``arcgraph workspace`` command tree."""
    workspace = subparsers.add_parser(
        "workspace", help="Inspect an explicit multi-repository ArcGraph workspace."
    )
    workspace_subparsers = workspace.add_subparsers(dest="workspace_command")
    workspace_status_cmd = workspace_subparsers.add_parser(
        "status",
        help="Read an arcgraph.workspace.toml manifest and summarize repo index status.",
    )
    workspace_status_cmd.add_argument(
        "--config",
        default=DEFAULT_WORKSPACE_CONFIG,
        help="Workspace manifest path. Defaults to arcgraph.workspace.toml.",
    )
    workspace_status_cmd.add_argument(
        "--include-dependency-hints",
        dest="include_dependency_hints",
        action="store_true",
        default=True,
        help="Include shallow declared-dependency and import candidate hints.",
    )
    workspace_status_cmd.add_argument(
        "--no-dependency-hints",
        dest="include_dependency_hints",
        action="store_false",
        help="Omit shallow workspace dependency hints from the status payload.",
    )
    workspace_status_cmd.set_defaults(handler=handle_workspace_status)
    workspace_resolve_cmd = workspace_subparsers.add_parser(
        "resolve",
        help="Route a workspace-level target to candidate repositories.",
    )
    workspace_resolve_cmd.add_argument(
        "target",
        help="Repository id, project/package/module/import/path/dependency target.",
    )
    workspace_resolve_cmd.add_argument(
        "--config",
        default=DEFAULT_WORKSPACE_CONFIG,
        help="Workspace manifest path. Defaults to arcgraph.workspace.toml.",
    )
    workspace_resolve_cmd.add_argument(
        "--kind",
        default="auto",
        choices=WORKSPACE_RESOLVE_KINDS,
        help="Resolution kind. Defaults to auto.",
    )
    workspace_resolve_cmd.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of matches to return after sorting.",
    )
    workspace_resolve_cmd.add_argument(
        "--include-dependency-hints",
        dest="include_dependency_hints",
        action="store_true",
        default=True,
        help="Include relevant shallow dependency hints in the resolve payload.",
    )
    workspace_resolve_cmd.add_argument(
        "--no-dependency-hints",
        dest="include_dependency_hints",
        action="store_false",
        help="Omit dependency hints from the resolve payload.",
    )
    workspace_resolve_cmd.set_defaults(handler=handle_workspace_resolve)
    workspace_context_cmd = workspace_subparsers.add_parser(
        "context",
        help="Resolve a workspace target, then return compact per-repo contexts.",
    )
    workspace_context_cmd.add_argument(
        "target",
        help="Repository id, project/package/module/import/path/dependency target.",
    )
    workspace_context_cmd.add_argument(
        "--config",
        default=DEFAULT_WORKSPACE_CONFIG,
        help="Workspace manifest path. Defaults to arcgraph.workspace.toml.",
    )
    workspace_context_cmd.add_argument(
        "--kind",
        default="auto",
        choices=WORKSPACE_RESOLVE_KINDS,
        help="Resolution kind. Defaults to auto.",
    )
    workspace_context_cmd.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of repository contexts to return after routing.",
    )
    workspace_context_cmd.add_argument(
        "--detail-level",
        default="summary",
        choices=WORKSPACE_CONTEXT_DETAIL_LEVELS,
        help="Per-repo context detail level. Workspace context does not support detailed.",
    )
    workspace_context_cmd.add_argument(
        "--include-dependency-hints",
        dest="include_dependency_hints",
        action="store_true",
        default=True,
        help="Include relevant shallow dependency hints in the resolve payload.",
    )
    workspace_context_cmd.add_argument(
        "--no-dependency-hints",
        dest="include_dependency_hints",
        action="store_false",
        help="Omit dependency hints from the resolve payload.",
    )
    workspace_context_cmd.set_defaults(handler=handle_workspace_context)


def add_benchmark_parser(subparsers: Any) -> None:
    """Register the ``arcgraph benchmark`` command tree."""
    benchmark = subparsers.add_parser(
        "benchmark", help="Run local ArcGraph benchmark probes."
    )
    benchmark_subparsers = benchmark.add_subparsers(dest="benchmark_command")
    agent_startup = benchmark_subparsers.add_parser(
        "agent-startup",
        help="Measure CLI-per-call cold-start latency for agent commands.",
    )
    agent_startup.add_argument(
        "--target",
        default=DEFAULT_AGENT_STARTUP_TARGET,
        help="Target symbol or path for context/explain probes.",
    )
    agent_startup.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Measured iterations per command after warmups.",
    )
    agent_startup.add_argument(
        "--warmups",
        type=int,
        default=1,
        help="Warmup runs per command that are excluded from timing summaries.",
    )
    agent_startup.add_argument(
        "--output",
        help="Optional JSON output path for the benchmark payload.",
    )
    agent_startup.add_argument(
        "--command-set",
        choices=["minimal", "agent"],
        default="agent",
        help="Command set to benchmark. `agent` measures current/context/explain.",
    )
    agent_startup.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per subprocess timeout in seconds.",
    )
    agent_startup.set_defaults(handler=handle_benchmark_agent_startup)

    benchmark_suite = benchmark_subparsers.add_parser(
        "suite",
        help="Run the local benchmark suite and MCP serve decision baseline.",
    )
    benchmark_suite.add_argument(
        "--target",
        default=DEFAULT_AGENT_STARTUP_TARGET,
        help="Target symbol or path for symbol/context/explain probes.",
    )
    benchmark_suite.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Measured iterations per command after warmups.",
    )
    benchmark_suite.add_argument(
        "--warmups",
        type=int,
        default=1,
        help="Warmup runs per command that are excluded from timing summaries.",
    )
    benchmark_suite.add_argument(
        "--output",
        help="Optional JSON output path for the benchmark payload.",
    )
    benchmark_suite.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per subprocess timeout in seconds.",
    )
    benchmark_suite.add_argument(
        "--include-build",
        action="store_true",
        help="Also benchmark a build in an isolated temporary output directory.",
    )
    benchmark_suite.set_defaults(handler=handle_benchmark_suite)
