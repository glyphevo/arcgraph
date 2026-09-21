"""CLI handlers and parser wiring for the ``arcgraph visual`` group.

Split out of ``cli.py``: the group was its largest single parser block
(318 lines) plus five handlers, and it reaches into nothing else there
beyond the shared ``query_engine`` helper.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import json
from arcgraph.core.force_graph_export import (
    ForceGraphExportOptions,
    build_force_graph_export,
)
from arcgraph.interfaces.workbench import (
    AUDIT_DEFAULT_MAX_NODES,
    WorkbenchAuditOptions,
    attach_browser_open_result,
    attach_workbench_audit_index,
    attach_workbench_payload_budget,
    build_workbench_status,
    write_workbench,
)
from arcgraph.interfaces.visual_server import serve_visual_workbench
from arcgraph.interfaces.visual_smoke import (
    DEFAULT_VISUAL_SMOKE_TARGET,
    VisualSmokeOptions,
    run_visual_smoke,
)
from arcgraph.interfaces.cli_support import (  # noqa: F401
    print_json,
    query_engine,
)


def handle_visual_force(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_path = (
        Path(args.output)
        if args.output
        else output_dir / "reports" / "force-graph.json"
    )
    if not output_path.is_absolute():
        output_path = repo_root / output_path

    engine = query_engine(args)
    payload = build_force_graph_export(
        engine,
        ForceGraphExportOptions(
            max_symbol_nodes=args.max_symbol_nodes,
            min_symbol_degree=args.min_symbol_degree,
            max_module_symbols=args.max_module_symbols,
            min_module_edge_weight=args.min_module_edge_weight,
            include_tests=args.include_tests,
            include_generated=args.include_generated,
            include_external=args.include_external,
            include_structural_edges=args.include_structural_edges,
            include_edge_kinds=tuple(args.include_edge_kind or ()),
            exclude_edge_kinds=tuple(args.exclude_edge_kind or ()),
            initial_focus=tuple(args.initial_focus or ()),
            focus_depth=args.focus_depth,
            focus_direction=args.focus_direction,
        ),
    )
    attach_workbench_audit_index(
        engine,
        payload,
        WorkbenchAuditOptions(
            include=args.include_audit_index,
            max_nodes=args.audit_max_nodes,
            detail_level=args.audit_detail_level,
        ),
    )
    attach_workbench_payload_budget(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "schema": payload["schema"],
        "version": payload["version"],
        "status": payload["status"],
        "path": str(output_path.resolve()),
        "scope": payload["scope"],
        "summary": {
            "module_nodes": len(payload["module_graph"]["nodes"]),
            "module_edges": len(payload["module_graph"]["edges"]),
            "symbol_nodes": len(payload["symbol_graph"]["nodes"]),
            "symbol_edges": len(payload["symbol_graph"]["edges"]),
            "focus_nodes": payload.get("focus_index", {})
            .get("counts", {})
            .get("nodes", 0),
            "focus_edges": payload.get("focus_index", {})
            .get("counts", {})
            .get("edges", 0),
            "initial_focus_resolved": len(
                payload.get("focus", {}).get("resolved_seed_ids", [])
            ),
            "audit_nodes": payload.get("audit_index", {})
            .get("counts", {})
            .get("nodes", 0),
            "audit_truncated": payload.get("audit_index", {})
            .get("counts", {})
            .get("truncated", 0),
            "audit_estimated_bytes": payload.get("audit_index", {}).get(
                "estimated_bytes", 0
            ),
            "payload_estimated_bytes": payload.get("payload_budget", {}).get(
                "graph_data_bytes", 0
            ),
            "largest_payload_section": payload.get("payload_budget", {}).get(
                "largest_section", {}
            ),
            "payload_warnings": payload.get("payload_budget", {}).get("warnings", []),
            "expandable_modules": len(payload["module_symbols"]),
            "filters": payload["meta"]["filters"],
            "excluded_node_counts": payload["meta"]["excluded_node_counts"],
        },
        "warnings": payload.get("warnings", []),
    }


def handle_visual_workbench(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    workbench_output_dir = (
        Path(args.workbench_output_dir)
        if args.workbench_output_dir
        else output_dir / "reports" / "workbench"
    )
    if not workbench_output_dir.is_absolute():
        workbench_output_dir = repo_root / workbench_output_dir

    engine = query_engine(args)
    payload = build_force_graph_export(
        engine,
        ForceGraphExportOptions(
            max_symbol_nodes=args.max_symbol_nodes,
            min_symbol_degree=args.min_symbol_degree,
            max_module_symbols=args.max_module_symbols,
            min_module_edge_weight=args.min_module_edge_weight,
            include_tests=args.include_tests,
            include_generated=args.include_generated,
            include_external=args.include_external,
            include_structural_edges=args.include_structural_edges,
            include_edge_kinds=tuple(args.include_edge_kind or ()),
            exclude_edge_kinds=tuple(args.exclude_edge_kind or ()),
            initial_focus=tuple(args.initial_focus or ()),
            focus_depth=args.focus_depth,
            focus_direction=args.focus_direction,
        ),
    )
    attach_workbench_audit_index(
        engine,
        payload,
        WorkbenchAuditOptions(
            include=args.include_audit_index,
            max_nodes=args.audit_max_nodes,
            detail_level=args.audit_detail_level,
        ),
    )
    attach_workbench_payload_budget(payload)
    status_payload = build_workbench_status(engine)
    result = write_workbench(
        output_dir=workbench_output_dir,
        graph_payload=payload,
        status_payload=status_payload,
    )
    if args.open:
        index_path = Path(result["artifacts"]["index_html"]).resolve()
        attach_browser_open_result(result, index_path.as_uri())
    return result


def handle_visual_serve(args: argparse.Namespace) -> dict[str, Any]:
    return serve_visual_workbench(
        query_engine(args),
        host=args.host,
        port=args.port,
        graph_options=_visual_graph_options(args),
        open_browser=args.open,
    )


def handle_visual_smoke(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    smoke_output_dir = (
        Path(args.smoke_output_dir)
        if args.smoke_output_dir
        else output_dir / "reports" / "visual-smoke"
    )
    if not smoke_output_dir.is_absolute():
        smoke_output_dir = repo_root / smoke_output_dir
    return run_visual_smoke(
        query_engine(args),
        graph_options=_visual_graph_options(args),
        options=VisualSmokeOptions(
            output_dir=smoke_output_dir,
            target=args.target,
            host=args.host,
            port=args.port,
            timeout_s=args.timeout_seconds,
        ),
    )


def _visual_graph_options(args: argparse.Namespace) -> ForceGraphExportOptions:
    return ForceGraphExportOptions(
        max_symbol_nodes=args.max_symbol_nodes,
        min_symbol_degree=args.min_symbol_degree,
        max_module_symbols=args.max_module_symbols,
        min_module_edge_weight=args.min_module_edge_weight,
        include_tests=args.include_tests,
        include_generated=args.include_generated,
        include_external=args.include_external,
        include_structural_edges=args.include_structural_edges,
        include_edge_kinds=tuple(args.include_edge_kind or ()),
        exclude_edge_kinds=tuple(args.exclude_edge_kind or ()),
    )


def add_visual_parser(subparsers: Any) -> None:
    """Register the ``arcgraph visual`` command tree."""

    def _add_visual_focus_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--initial-focus",
            action="append",
            default=[],
            help=(
                "Target id, qualname, name, or path to open as the initial "
                "focus ego-graph. Can be repeated."
            ),
        )
        sub.add_argument(
            "--focus-depth",
            type=int,
            default=1,
            choices=[1, 2],
            help="Initial focus ego-graph depth. Static workbench supports 1 or 2.",
        )
        sub.add_argument(
            "--focus-direction",
            default="both",
            choices=["incoming", "outgoing", "both"],
            help="Initial focus traversal direction.",
        )

    def _add_visual_audit_args(sub: argparse.ArgumentParser) -> None:
        audit_group = sub.add_mutually_exclusive_group()
        audit_group.add_argument(
            "--include-audit-index",
            dest="include_audit_index",
            action="store_true",
            default=True,
            help="Include compact static node audit summaries in the graph payload.",
        )
        audit_group.add_argument(
            "--no-audit-index",
            dest="include_audit_index",
            action="store_false",
            help="Skip compact static node audit summaries.",
        )
        sub.add_argument(
            "--audit-max-nodes",
            type=int,
            default=AUDIT_DEFAULT_MAX_NODES,
            help=(
                "Maximum focusable nodes to audit in the static payload. "
                "Initial focus seeds are always included when resolvable."
            ),
        )
        sub.add_argument(
            "--audit-detail-level",
            default="summary",
            choices=["summary", "standard"],
            help="Audit summary density. Static audit export intentionally excludes detailed.",
        )

    visual = subparsers.add_parser(
        "visual", help="Export graph visualization payloads."
    )
    visual_subparsers = visual.add_subparsers(dest="visual_command")
    visual_force = visual_subparsers.add_parser(
        "force", help="Export a semantic force-directed graph JSON payload."
    )
    visual_force.add_argument(
        "--output",
        help=(
            "Path to write the JSON payload. Defaults to "
            "<output-dir>/reports/force-graph.json."
        ),
    )
    visual_force.add_argument(
        "--max-symbol-nodes",
        type=int,
        default=400,
        help="Maximum high-connectivity symbol nodes to include.",
    )
    visual_force.add_argument(
        "--min-symbol-degree",
        type=int,
        default=5,
        help="Minimum weighted degree for symbol view inclusion.",
    )
    visual_force.add_argument(
        "--max-module-symbols",
        type=int,
        default=50,
        help="Maximum symbols allowed for a module expansion payload.",
    )
    visual_force.add_argument(
        "--min-module-edge-weight",
        type=int,
        default=1,
        help="Minimum raw aggregated module edge count.",
    )
    visual_force.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test files and test-only nodes in the export.",
    )
    visual_force.add_argument(
        "--include-generated",
        action="store_true",
        help="Include generated, coverage, backup, and build-output files.",
    )
    visual_force.add_argument(
        "--include-external",
        action="store_true",
        help="Include external_symbol nodes in the symbol graph.",
    )
    visual_force.add_argument(
        "--include-structural-edges",
        action="store_true",
        help="Include references/uses/contains/defines/declares edges.",
    )
    visual_force.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Restrict the export to this edge kind. Can be repeated.",
    )
    visual_force.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from the export. Can be repeated.",
    )
    _add_visual_focus_args(visual_force)
    _add_visual_audit_args(visual_force)
    visual_force.set_defaults(handler=handle_visual_force)
    visual_workbench = visual_subparsers.add_parser(
        "workbench",
        help="Generate a local ArcGraph Explorer workbench directory.",
    )
    visual_workbench.add_argument(
        "--output-dir",
        dest="workbench_output_dir",
        help=(
            "Directory to write index.html, graph_data.json, and status_data.json. "
            "Defaults to <output-dir>/reports/workbench."
        ),
    )
    visual_workbench.add_argument(
        "--open",
        action="store_true",
        help="Open the generated local workbench in the system browser.",
    )
    visual_workbench.add_argument(
        "--max-symbol-nodes",
        type=int,
        default=400,
        help="Maximum high-connectivity symbol nodes to include.",
    )
    visual_workbench.add_argument(
        "--min-symbol-degree",
        type=int,
        default=5,
        help="Minimum weighted degree for symbol view inclusion.",
    )
    visual_workbench.add_argument(
        "--max-module-symbols",
        type=int,
        default=50,
        help="Maximum symbols allowed for a module expansion payload.",
    )
    visual_workbench.add_argument(
        "--min-module-edge-weight",
        type=int,
        default=1,
        help="Minimum raw aggregated module edge count.",
    )
    visual_workbench.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test files and test-only nodes in the export.",
    )
    visual_workbench.add_argument(
        "--include-generated",
        action="store_true",
        help="Include generated, coverage, backup, and build-output files.",
    )
    visual_workbench.add_argument(
        "--include-external",
        action="store_true",
        help="Include external_symbol nodes in the symbol graph.",
    )
    visual_workbench.add_argument(
        "--include-structural-edges",
        action="store_true",
        help="Include references/uses/contains/defines/declares edges.",
    )
    visual_workbench.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Restrict the export to this edge kind. Can be repeated.",
    )
    visual_workbench.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from the export. Can be repeated.",
    )
    _add_visual_focus_args(visual_workbench)
    _add_visual_audit_args(visual_workbench)
    visual_workbench.set_defaults(handler=handle_visual_workbench)
    visual_serve = visual_subparsers.add_parser(
        "serve",
        help="Serve the local ArcGraph Explorer workbench with read-only on-demand APIs.",
    )
    visual_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host to bind. Only 127.0.0.1, localhost, and ::1 are allowed.",
    )
    visual_serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local port for the read-only workbench server.",
    )
    visual_serve.add_argument(
        "--open",
        action="store_true",
        help="Open the loopback workbench URL in the system browser after startup.",
    )
    visual_serve.add_argument(
        "--max-symbol-nodes",
        type=int,
        default=400,
        help="Maximum high-connectivity symbol nodes to include in overview payloads.",
    )
    visual_serve.add_argument(
        "--min-symbol-degree",
        type=int,
        default=5,
        help="Minimum weighted degree for symbol overview inclusion.",
    )
    visual_serve.add_argument(
        "--max-module-symbols",
        type=int,
        default=50,
        help="Maximum symbols allowed for a module expansion payload.",
    )
    visual_serve.add_argument(
        "--min-module-edge-weight",
        type=int,
        default=1,
        help="Minimum raw aggregated module edge count.",
    )
    visual_serve.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test files and test-only nodes in the served graph.",
    )
    visual_serve.add_argument(
        "--include-generated",
        action="store_true",
        help="Include generated, coverage, backup, and build-output files.",
    )
    visual_serve.add_argument(
        "--include-external",
        action="store_true",
        help="Include external_symbol nodes in focus/API results.",
    )
    visual_serve.add_argument(
        "--include-structural-edges",
        action="store_true",
        help="Include references/uses/contains/defines/declares edges.",
    )
    visual_serve.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Restrict the served graph to this edge kind. Can be repeated.",
    )
    visual_serve.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from the served graph. Can be repeated.",
    )
    visual_serve.set_defaults(handler=handle_visual_serve)
    visual_smoke = visual_subparsers.add_parser(
        "smoke",
        help="Run optional browser QA smoke against the local workbench.",
    )
    visual_smoke.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host for the temporary read-only workbench server.",
    )
    visual_smoke.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port for the temporary server. Defaults to an ephemeral port.",
    )
    visual_smoke.add_argument(
        "--output-dir",
        dest="smoke_output_dir",
        help=(
            "Directory for screenshots and result.json. Defaults to "
            "<output-dir>/reports/visual-smoke."
        ),
    )
    visual_smoke.add_argument(
        "--target",
        default=DEFAULT_VISUAL_SMOKE_TARGET,
        help="Search/focus target used by the browser smoke scenario.",
    )
    visual_smoke.add_argument(
        "--timeout-seconds",
        type=float,
        default=45.0,
        help="Per-Playwright-command timeout in seconds.",
    )
    visual_smoke.add_argument(
        "--max-symbol-nodes",
        type=int,
        default=400,
        help="Maximum high-connectivity symbol nodes to include in overview payloads.",
    )
    visual_smoke.add_argument(
        "--min-symbol-degree",
        type=int,
        default=5,
        help="Minimum weighted degree for symbol overview inclusion.",
    )
    visual_smoke.add_argument(
        "--max-module-symbols",
        type=int,
        default=50,
        help="Maximum symbols allowed for a module expansion payload.",
    )
    visual_smoke.add_argument(
        "--min-module-edge-weight",
        type=int,
        default=1,
        help="Minimum raw aggregated module edge count.",
    )
    visual_smoke.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test files and test-only nodes in the served graph.",
    )
    visual_smoke.add_argument(
        "--include-generated",
        action="store_true",
        help="Include generated, coverage, backup, and build-output files.",
    )
    visual_smoke.add_argument(
        "--include-external",
        action="store_true",
        help="Include external_symbol nodes in focus/API results.",
    )
    visual_smoke.add_argument(
        "--include-structural-edges",
        action="store_true",
        help="Include references/uses/contains/defines/declares edges.",
    )
    visual_smoke.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Restrict the served graph to this edge kind. Can be repeated.",
    )
    visual_smoke.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from the served graph. Can be repeated.",
    )
    visual_smoke.set_defaults(handler=handle_visual_smoke)
