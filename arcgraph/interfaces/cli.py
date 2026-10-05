"""Command line interface for ArcGraph."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, cast

from arcgraph import __version__
from arcgraph.change.errors import ChangeSafetyError
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.external_protocol import summarize_toolchain_status
from arcgraph.core.evidence_manifest import (
    evidence_health_check_status,
    evidence_plan,
    evidence_status,
    load_live_evidence_manifest,
    write_evidence_sidecar,
)
from arcgraph.core.metadata import current_commit
from arcgraph.core.ids import route_id
from arcgraph.core.query_engine import (
    QueryEngine,
    SchemaVersionError,
)
from arcgraph.core.schemas import ContextRequest
from arcgraph.core.target_resolver import is_entrypoint_target, parse_route_target
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.core.scanner import (
    DEFAULT_IGNORE_RULES,
    LEGACY_SOURCE_ROOTS,
    ResolvedSourceRoots,
    SourceRoot,
    SourceRootDetection,
    detect_source_roots,
    infer_source_roots,
)
from arcgraph.core.unresolved_classification import UNRESOLVED_CATEGORIES
from arcgraph.interfaces.ci import evidence_requirement_summary, run_ci_checks
from arcgraph.interfaces.agent_help import (
    HELP_SURFACES,
    HELP_TOPICS,
    HelpTopic,
    build_agent_help,
    format_agent_help_human,
)
from arcgraph.interfaces.docs import DOC_TOPICS, render_docs
from arcgraph.interfaces.cli_feedback import (
    add_feedback_parser,
    feedback_metrics_path_conflict_payload,
)
from arcgraph.interfaces.human_output import (
    format_build_human,
    format_doctor_human,
    format_generic_human,
    format_init_human,
)
from arcgraph.interfaces.metrics import (
    CLI_METRICS_WRITE_WARNING,
    METRICS_LOG_INVALID_ENCODING_ERROR_CODE,
    METRICS_LOG_UNREADABLE_ERROR_CODE,
    MetricsLogEncodingError,
    MetricsRecorder,
    metrics_unavailable_payload,
    summarize_metrics,
    summarize_trial_metrics,
)
from arcgraph.interfaces.local_state import (
    PrivateLocalStateError,
    local_state_paths_alias,
)
from arcgraph.interfaces.mcp_server import add_mcp_server_args, serve as serve_mcp
from arcgraph.interfaces.ops import (
    prune_output,
    rebuild_index,
    stale_alert,
)
from arcgraph.interfaces.precision import (
    convert_scip_index_to_json,
    generate_pyright_json,
    generate_scip_python_json,
)
from arcgraph.interfaces.project_probe import (
    detect_project_languages,
    source_file_counts,
)
from arcgraph.interfaces.stdio_encoding import use_utf8_stdio
from arcgraph.interfaces.reports import (
    classify_unresolved_records,
    render_ci_markdown,
    render_impact_markdown,
    render_metrics_dashboard_html,
    render_pr_markdown,
    render_static_html_report,
    render_unresolved_classification_markdown,
)
from arcgraph.interfaces.trace import (
    import_har_network,
    import_otel_spans,
    import_runtime_trace,
    run_runtime_trace,
)
from arcgraph.version_info import version_info
from arcgraph.interfaces.trial_feedback import (
    DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE,
)

# Re-exported so `from arcgraph.interfaces.cli import handle_change_*` and
# the parser wiring below keep working unchanged after the split.
from arcgraph.interfaces.cli_workspace import (  # noqa: F401
    add_benchmark_parser,
    add_workspace_parser,
    handle_workspace_status,
    handle_workspace_resolve,
    handle_workspace_context,
    handle_benchmark_agent_startup,
    handle_benchmark_suite,
)
from arcgraph.interfaces.cli_visual import (  # noqa: F401
    add_visual_parser,
    handle_visual_force,
    handle_visual_workbench,
    handle_visual_serve,
    handle_visual_smoke,
    _visual_graph_options,
)
from arcgraph.interfaces.cli_support import (  # noqa: F401
    output_location,
    print_json,
    query_engine,
    write_json_output,
    _print_cli_error_payload,
)
from arcgraph.interfaces.client_setup import add_setup_parser
from arcgraph.interfaces.cli_trial import (  # noqa: F401
    add_trial_parser,
    handle_trial_setup,
)
from arcgraph.interfaces.cli_ops import handle_sync, handle_watch  # noqa: F401
from arcgraph.interfaces.cli_change import (  # noqa: F401
    add_change_parser,
    _change_error_payload,
    _change_json_object,
    _change_output_dir,
    _change_payload,
    _change_repo_id,
    _change_repo_id_from_argv,
    _change_repo_root,
    _change_service,
    _change_target,
    _evidence_summary_payload,
    handle_change_abandon,
    handle_change_approve,
    handle_change_archive,
    handle_change_audit_export,
    handle_change_diff,
    handle_change_evidence_add,
    handle_change_evidence_list,
    handle_change_evidence_purge,
    handle_change_evidence_show,
    handle_change_list,
    handle_change_plan,
    handle_change_reconcile_pins,
    handle_change_reject,
    handle_change_report_show,
    handle_change_show,
    handle_change_verify,
)
from arcgraph.core.payload_policy import (
    TARGET_SCOPED_CLI_COMMANDS,
    apply_target_payload_contract,
)
from arcgraph.providers.context_provider import ContextProvider


def _scope_payload_capabilities(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the target-scoped read contract at the CLI boundary.

    On a minimal missing-symbol response, the complete capability table can
    dominate the serialized payload while placing no caveat on the answer. The
    shared policy keeps degraded entries, reports what was omitted, and
    independently versions the bounded read contract. It is idempotent, so
    provider-built payloads and direct QueryEngine payloads converge here.
    """
    return apply_target_payload_contract(payload)


class _ArgumentParserError(ValueError):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        self.parser = parser
        self.message = message
        super().__init__(message)


class _ArcGraphArgumentParser(argparse.ArgumentParser):
    """Let ``main`` preserve the structured Change CLI error contract."""

    def error(self, message: str) -> None:
        raise _ArgumentParserError(self, message)


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdio()
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parser.parse_args(raw_argv)
    except _ArgumentParserError as exc:
        parse_error_payload = getattr(
            exc.parser,
            "_arcgraph_parse_error_payload",
            None,
        )
        if isinstance(parse_error_payload, dict):
            print_json(dict(parse_error_payload))
            return int(
                getattr(
                    exc.parser,
                    "_arcgraph_parse_error_exit_code",
                    2,
                )
            )
        if _top_level_command(raw_argv) == "change":
            error_args = argparse.Namespace(
                command="change",
                change_repo_id=_change_repo_id_from_argv(raw_argv),
            )
            print_json(_change_error_payload(error_args, ValueError(exc.message)))
            return 1
        exc.parser.print_usage(sys.stderr)
        exc.parser.exit(2, f"{exc.parser.prog}: error: {exc.message}\n")

    if not hasattr(args, "handler"):
        parser.print_help()
        return 0

    feedback_path_error = feedback_metrics_path_conflict_payload(args)
    if feedback_path_error is not None:
        exit_code = int(feedback_path_error.pop("_exit_code", 2))
        print_json(feedback_path_error)
        return exit_code
    if (
        getattr(args, "command", None) == "mcp"
        and getattr(args, "mcp_command", None) == "serve"
        and getattr(args, "metrics_log", None)
        and getattr(args, "mcp_feedback_log", None)
        and local_state_paths_alias(
            Path(args.metrics_log),
            Path(args.mcp_feedback_log),
        )
    ):
        print(
            f"ArcGraph: {DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE}",
            file=sys.stderr,
        )
        # Rejected before the handler runs, but it is still a failure a caller
        # parses, so it gets the same envelope the handler failures get.
        _print_cli_error_payload(
            args, ValueError(DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE)
        )
        return 2

    started = time.perf_counter()
    payload: dict[str, Any] | str | None = None
    streaming_output = False
    exit_code = 0
    status = "success"
    error: str | None = None
    try:
        payload = args.handler(args)
    except BrokenPipeError:
        # Every CLI command writes to stdout, so a consumer that closes the
        # pipe early (`arcgraph ... | head`) is a normal termination for all
        # of them, not just the streaming ones. Detach stdout so interpreter
        # shutdown cannot re-raise on flush, and report the conventional
        # SIGPIPE status.
        return _exit_on_broken_pipe()
    except ChangeSafetyError as exc:
        status = "error"
        error = str(exc)
        exit_code = 1 if getattr(args, "command", None) == "change" else 2
        if getattr(args, "command", None) == "change":
            payload = _change_error_payload(args, exc)
            print_json(payload)
        else:
            print(f"ArcGraph: {exc}", file=sys.stderr)
            _print_cli_error_payload(args, exc)
    except (FileNotFoundError, RuntimeError, SchemaVersionError) as exc:
        status = "error"
        error = str(exc)
        exit_code = 1 if getattr(args, "command", None) == "change" else 2
        if getattr(args, "command", None) == "change":
            payload = _change_error_payload(args, exc)
            print_json(payload)
        else:
            print(f"ArcGraph: {exc}", file=sys.stderr)
            _print_cli_error_payload(args, exc)
    except (TypeError, ValueError) as exc:
        if getattr(args, "command", None) != "change":
            # An internal defect keeps its traceback -- swallowing it would
            # hide a bug -- but stdout still gets the envelope, so an agent is
            # told that the command failed and why rather than reading silence.
            traceback.print_exc()
            _print_cli_error_payload(args, exc)
            return 1
        status = "error"
        error = str(exc)
        exit_code = 1
        payload = _change_error_payload(args, exc)
        print_json(payload)
    else:
        if isinstance(payload, dict):
            exit_code = int(payload.pop("_exit_code", 0))
            streaming_output = bool(payload.pop("_streaming_output", False))
            if getattr(args, "command", None) == "change":
                status = str(payload.get("status", "success"))
            else:
                status = "success" if exit_code == 0 else "failed"
            # --raw promises the QueryEngine payload unchanged, so it must not
            # be shaped here either. Scoping it made `callers --raw` differ
            # from `QueryEngine.callers()`, which is the one thing --raw exists
            # to reproduce.
            if getattr(
                args, "command", None
            ) in TARGET_SCOPED_CLI_COMMANDS and not getattr(args, "raw", False):
                payload = _scope_payload_capabilities(payload)

        if payload is not None:
            try:
                _emit_cli_payload(args, payload, streaming_output, started)
            except BrokenPipeError:
                exit_code = _exit_on_broken_pipe()
    finally:
        metrics_log = getattr(args, "metrics_log", None)
        if metrics_log:
            try:
                MetricsRecorder(Path(metrics_log)).record_cli_command(
                    command=getattr(args, "command", None),
                    status=status,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    exit_code=exit_code,
                    payload=payload if isinstance(payload, dict) else None,
                    error=error,
                )
            except PrivateLocalStateError:
                sys.stderr.write(CLI_METRICS_WRITE_WARNING)
    return exit_code


def _exit_on_broken_pipe() -> int:
    """Detach stdout and report the conventional SIGPIPE exit status."""

    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    return 141


def _emit_cli_payload(
    args: argparse.Namespace,
    payload: dict[str, Any] | str,
    streaming_output: bool,
    started: float,
) -> None:
    if isinstance(payload, str):
        print(payload, end="")
    elif getattr(args, "human", False):
        duration_s = time.perf_counter() - started
        command = getattr(args, "command", None)
        if command == "build":
            print(format_build_human(payload, duration_s))
        elif command == "init":
            print(format_init_human(payload))
        elif command == "doctor":
            print(format_doctor_human(payload))
        elif command == "help":
            print(format_agent_help_human(payload), end="")
        else:
            print(format_generic_human(payload, command))
    elif streaming_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print_json(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArcGraphArgumentParser(
        prog="arcgraph", description="Build and query a local ArcGraph index."
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root. Defaults to current directory.",
    )
    parser.add_argument(
        "--output-dir", default="output/arcgraph", help="Index output directory."
    )
    parser.add_argument(
        "--metrics-log",
        help="Optional JSONL path for CLI latency/status metrics.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="Print human-readable output instead of JSON.",
    )
    subparsers = parser.add_subparsers(dest="command")

    def _add_legacy_roots_arg(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--legacy-roots",
            action="store_true",
            help=(
                "Use the hard-coded legacy source roots instead of "
                "auto-detection. Mutually exclusive with --root."
            ),
        )

    def _add_agent_compact_args(sub: argparse.ArgumentParser) -> None:
        shape = sub.add_mutually_exclusive_group()
        shape.add_argument(
            "--compact",
            action="store_true",
            help=(
                "Accepted for compatibility and ignored: the bounded agent "
                "payload is the default."
            ),
        )
        shape.add_argument(
            "--raw",
            action="store_true",
            help=(
                "Return the unbounded QueryEngine debug payload instead of the "
                "bounded agent payload. It carries every node property and is "
                "not size-bounded; on this repository the raw impact payload is "
                "over 10 MB. Payload-shaping options are rejected with --raw."
            ),
        )
        # ``None`` defaults let the handler tell "user asked for it" apart from
        # "argparse filled it in", so --raw can reject shaping it cannot honor
        # instead of silently discarding it.
        sub.add_argument("--max-results", type=int, default=None)
        sub.add_argument(
            "--detail-level",
            default=None,
            choices=["summary", "standard", "detailed"],
        )
        sub.add_argument(
            "--include-source",
            action="store_true",
            default=None,
            help="Include source snippets when the provider can safely expose them.",
        )

    def _add_relation_profile_arg(
        sub: argparse.ArgumentParser, *, direction: str
    ) -> None:
        sub.add_argument(
            "--profile",
            default="review_default",
            help=f"Confidence profile to use for direct {direction} evidence.",
        )

    init = subparsers.add_parser(
        "init", help="Detect source roots and add a [tool.arcgraph] config."
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the config that would be written without modifying pyproject.toml.",
    )
    init.set_defaults(handler=handle_init)

    doctor = subparsers.add_parser(
        "doctor", help="Run environment and index health checks."
    )
    doctor.set_defaults(handler=handle_doctor)

    version = subparsers.add_parser(
        "version",
        help="Report the exact runtime, artifact, and source identity.",
    )
    version.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable provenance payload (the default format).",
    )
    # The identity command must observe live working-tree state, never a
    # reused capture.
    version.set_defaults(handler=lambda _args: version_info(refresh=True))

    build = subparsers.add_parser("build", help="Build a new ArcGraph index.")
    build.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root to scan. Can be repeated.",
    )
    build.add_argument(
        "--scip-index",
        help="Optional SCIP-derived JSON occurrence file for confirmed references.",
    )
    build.add_argument(
        "--scip-graph-index",
        help=(
            "Optional scip print --json payload to ingest as a language-neutral "
            "protocol graph fragment. Distinct from --scip-index precision evidence."
        ),
    )
    build.add_argument(
        "--openapi-spec",
        help=(
            "Optional OpenAPI 3.x JSON/YAML artifact to ingest as an explicit "
            "protocol graph fragment."
        ),
    )
    build.add_argument(
        "--pyright-export",
        help="Optional Pyright-derived JSON export path for the precision input contract.",
    )
    build.add_argument(
        "--runtime-trace",
        help="Optional ArcGraph runtime trace JSON file to import as runtime-only evidence.",
    )
    build.add_argument(
        "--coverage",
        help="Optional coverage JSON or Cobertura XML file for runtime test evidence.",
    )
    build.add_argument(
        "--semantic-call-resolution",
        action="store_true",
        help=(
            "Compatibility flag retained for older scripts; V2 receiver-aware "
            "call resolution is the default at schema 1.0."
        ),
    )
    build.add_argument(
        "--legacy-call-resolution",
        action="store_true",
        help=(
            "Use the legacy AST call resolver. This escape hatch is rejected by "
            "ci --python-full."
        ),
    )
    _add_legacy_roots_arg(build)
    build.set_defaults(handler=handle_build)

    reindex = subparsers.add_parser(
        "reindex", help="Incrementally reindex changed files."
    )
    reindex.add_argument(
        "--changed",
        action="store_true",
        help="Reindex changed files using the current index as a baseline.",
    )
    reindex.set_defaults(handler=handle_reindex)

    sync = subparsers.add_parser(
        "sync", help="Explicitly publish an incremental index when needed."
    )
    sync.add_argument(
        "--if-stale",
        action="store_true",
        help="Skip synchronization when the current index is fresh.",
    )
    sync.set_defaults(handler=handle_sync)

    watch = subparsers.add_parser(
        "watch", help="Watch for stale changes and debounce incremental sync."
    )
    watch.add_argument("--poll-interval", type=float, default=0.25)
    watch.add_argument("--debounce", type=float, default=0.5)
    watch.add_argument("--max-cycles", type=int, help=argparse.SUPPRESS)
    watch.set_defaults(handler=handle_watch)

    stats = subparsers.add_parser("stats", help="Show current index statistics.")
    stats.set_defaults(handler=lambda args: query_engine(args).stats())

    semantic_stats = subparsers.add_parser(
        "semantic-stats", help="Show Python semantic callsite metrics."
    )
    semantic_stats.add_argument(
        "--output",
        help="Path to write semantic metrics JSON. Defaults to <output-dir>/metrics/semantic-stats.json.",
    )
    semantic_stats.set_defaults(handler=handle_semantic_stats)

    unresolved = subparsers.add_parser(
        "unresolved", help="Show unresolved semantic callsites."
    )
    unresolved.add_argument(
        "target",
        nargs="?",
        help="Optional file path, symbol, entrypoint, or stable id filter.",
    )
    unresolved.add_argument(
        "--limit", type=int, default=100, help="Maximum unresolved callsites to return."
    )
    unresolved.add_argument(
        "--category",
        choices=UNRESOLVED_CATEGORIES,
        help="Only return unresolved callsites in this triage category.",
    )
    unresolved.add_argument(
        "--release-blocking-only",
        action="store_true",
        help="Only return unresolved callsites that block the python_full gate.",
    )
    unresolved.set_defaults(
        handler=lambda args: query_engine(args).unresolved(
            args.target,
            limit=args.limit,
            category=args.category,
            release_blocking_only=args.release_blocking_only,
        )
    )

    bindings = subparsers.add_parser(
        "bindings", help="Show bindings attached to an indexed scope."
    )
    bindings.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    bindings.add_argument(
        "--limit", type=int, default=200, help="Maximum binding records to return."
    )
    bindings.set_defaults(
        handler=lambda args: query_engine(args).bindings(args.target, limit=args.limit)
    )

    types = subparsers.add_parser(
        "types", help="Show TypeRef facts attached to an indexed scope."
    )
    types.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    types.add_argument(
        "--limit", type=int, default=200, help="Maximum TypeRef records to return."
    )
    types.set_defaults(
        handler=lambda args: query_engine(args).types(args.target, limit=args.limit)
    )

    callsites = subparsers.add_parser(
        "callsites", help="Show extracted callsites for an indexed scope."
    )
    callsites.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    callsites.add_argument(
        "--limit", type=int, default=200, help="Maximum callsite records to return."
    )
    callsites.set_defaults(
        handler=lambda args: query_engine(args).callsites(args.target, limit=args.limit)
    )

    imports = subparsers.add_parser(
        "imports", help="Show import in/out edges for a module."
    )
    imports.add_argument("module", help="Dotted module name or mod:<name> id.")
    imports.set_defaults(handler=lambda args: query_engine(args).imports(args.module))

    callers = subparsers.add_parser(
        "callers", help="Show direct inferred callers for a symbol."
    )
    callers.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    _add_agent_compact_args(callers)
    _add_relation_profile_arg(callers, direction="caller")
    callers.set_defaults(handler=handle_callers)

    callees = subparsers.add_parser(
        "callees", help="Show direct inferred callees for a symbol."
    )
    callees.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    _add_agent_compact_args(callees)
    _add_relation_profile_arg(callees, direction="callee")
    callees.set_defaults(handler=handle_callees)

    impact = subparsers.add_parser(
        "impact", help="Show a call/import/entrypoint/resource impact report."
    )
    impact.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    impact.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Reverse call traversal depth. Defaults to the profile's default_max_depth.",
    )
    impact.add_argument(
        "--profile",
        default="review_default",
        help="Impact confidence profile label to include in the response.",
    )
    impact.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Limit impact edges to this kind. Can be repeated.",
    )
    impact.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from impact output. Can be repeated.",
    )
    _add_agent_compact_args(impact)
    impact.set_defaults(handler=handle_impact)

    report = subparsers.add_parser("report", help="Render ArcGraph reports.")
    report_subparsers = report.add_subparsers(dest="report_command")
    report_impact = report_subparsers.add_parser(
        "impact", help="Render a Markdown impact report."
    )
    report_impact.add_argument(
        "target", help="Dotted qualname, path, or stable symbol id."
    )
    report_impact.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Reverse call traversal depth. Defaults to the profile's default_max_depth.",
    )
    report_impact.add_argument(
        "--profile",
        default="review_default",
        help="Impact confidence profile label to include in the report.",
    )
    report_impact.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Limit impact edges to this kind. Can be repeated.",
    )
    report_impact.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from impact output. Can be repeated.",
    )
    report_impact.set_defaults(
        handler=lambda args: render_impact_markdown(
            query_engine(args).impact(
                args.target,
                max_depth=args.max_depth,
                profile=args.profile,
                include_edge_kinds=args.include_edge_kind,
                exclude_edge_kinds=args.exclude_edge_kind,
            )
        )
    )
    report_ci = report_subparsers.add_parser("ci", help="Render a Markdown CI summary.")
    report_ci.add_argument("--target", action="append", default=[])
    report_ci.add_argument("--changed-file", action="append", default=[])
    report_ci.add_argument("--max-results", type=int, default=30)
    report_ci.add_argument(
        "--semantic-baseline",
        help="Optional semantic-stats or CI JSON baseline for resolution trend checks.",
    )
    report_ci.add_argument(
        "--performance-budget-ms",
        action="append",
        default=[],
        metavar="OPERATION=MS",
        help="Override a CI performance budget in milliseconds. Can be repeated.",
    )
    _add_ci_runtime_trace_args(report_ci)
    report_ci.set_defaults(
        handler=lambda args: render_ci_markdown(handle_ci_payload(args))
    )
    report_pr = report_subparsers.add_parser(
        "pr", help="Render a Markdown PR impact summary."
    )
    report_pr.add_argument("--target", action="append", default=[])
    report_pr.add_argument("--changed-file", action="append", default=[])
    report_pr.add_argument("--max-results", type=int, default=30)
    report_pr.add_argument(
        "--semantic-baseline",
        help="Optional semantic-stats or CI JSON baseline for resolution trend checks.",
    )
    report_pr.add_argument(
        "--performance-budget-ms",
        action="append",
        default=[],
        metavar="OPERATION=MS",
        help="Override a CI performance budget in milliseconds. Can be repeated.",
    )
    _add_ci_runtime_trace_args(report_pr)
    report_pr.add_argument("--output", help="Path to write the Markdown report.")
    report_pr.set_defaults(handler=handle_report_pr)
    report_html = report_subparsers.add_parser(
        "html", help="Render a static HTML ArcGraph report."
    )
    report_html.add_argument("target", help="Dotted qualname, path, or symbol id.")
    report_html.add_argument("--output", help="Path to write the HTML report.")
    report_html.add_argument("--max-results", type=int, default=30)
    report_html.add_argument(
        "--profile",
        default="review_default",
        help="Impact confidence profile label to include in the report.",
    )
    report_html.add_argument(
        "--include-edge-kind",
        action="append",
        default=[],
        help="Limit impact edges to this kind. Can be repeated.",
    )
    report_html.add_argument(
        "--exclude-edge-kind",
        action="append",
        default=[],
        help="Exclude this edge kind from impact output. Can be repeated.",
    )
    report_html.set_defaults(handler=handle_report_html)
    report_metrics = report_subparsers.add_parser(
        "metrics-html", help="Render a static HTML metrics dashboard."
    )
    report_metrics.add_argument("path", help="Metrics JSONL path.")
    report_metrics.add_argument("--limit", type=int, default=1000)
    report_metrics.add_argument("--output", help="Path to write the HTML dashboard.")
    report_metrics.set_defaults(handler=handle_report_metrics_html)
    report_unresolved = report_subparsers.add_parser(
        "unresolved", help="Render a Markdown unresolved classification report."
    )
    report_unresolved.add_argument(
        "target",
        nargs="?",
        help="Optional file path, symbol, entrypoint, or stable id filter.",
    )
    report_unresolved.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Maximum unresolved diagnostics to classify.",
    )
    report_unresolved.add_argument(
        "--output",
        default="output/arcgraph/reports/unresolved-classification.md",
        help="Path to write the Markdown report.",
    )
    report_unresolved.add_argument(
        "--json-output",
        help="Optional path to write machine-readable unresolved classification JSON.",
    )
    report_unresolved.set_defaults(handler=handle_report_unresolved)

    tests = subparsers.add_parser(
        "tests", help="Recommend related tests and coverage gaps."
    )
    tests.add_argument("target", help="Dotted qualname, path, or stable symbol id.")
    tests.set_defaults(handler=lambda args: query_engine(args).tests(args.target))

    references = subparsers.add_parser(
        "references", help="Verify references with SCIP or TypeScript Language Service."
    )
    references.add_argument("target", help="Exact qualname or stable symbol id.")
    references.add_argument(
        "--backend",
        choices=("auto", "scip", "typescript_language_service"),
        default="auto",
    )
    references.add_argument("--max-results", type=int, default=200)
    references.set_defaults(
        handler=lambda args: query_engine(args).references(
            args.target,
            backend=args.backend,
            max_results=args.max_results,
        )
    )

    context = subparsers.add_parser(
        "context", help="Return canonical agent context for one or more targets."
    )
    context.add_argument(
        "target",
        nargs="+",
        help="Dotted qualname, path, stable symbol id, or entrypoint target.",
    )
    context.add_argument(
        "--task", help="Optional task description for context ranking."
    )
    context.add_argument(
        "--profile",
        default="review_default",
        help="Confidence profile to use for impact and risk grouping.",
    )
    context.add_argument("--max-results", type=int, default=30)
    context.add_argument(
        "--detail-level",
        default="summary",
        choices=["summary", "standard", "detailed"],
    )
    context.add_argument(
        "--include-source",
        action="store_true",
        help="Include source snippets when the provider can safely expose them.",
    )
    context.set_defaults(handler=handle_context)

    explain = subparsers.add_parser(
        "explain", help="Explain target resolution, direct edges, and evidence."
    )
    explain.add_argument(
        "target",
        nargs="+",
        help="Dotted qualname, path, stable symbol id, or entrypoint target.",
    )
    explain.add_argument(
        "--task", help="Optional task description for explanation ranking."
    )
    explain.add_argument(
        "--profile",
        default="review_default",
        help="Confidence profile to use for direct call evidence.",
    )
    explain.add_argument("--max-results", type=int, default=30)
    explain.add_argument(
        "--detail-level",
        default="summary",
        choices=["summary", "standard", "detailed"],
    )
    explain.add_argument(
        "--include-source",
        action="store_true",
        help="Include source snippets when the provider can safely expose them.",
    )
    explain.set_defaults(handler=handle_explain)

    similar = subparsers.add_parser(
        "similar", help="Find indexed implementations similar to a symbol."
    )
    similar.add_argument("target", help="Dotted qualname or stable symbol id.")
    similar.add_argument("--max-results", type=int, default=10)
    similar.add_argument(
        "--detail-level",
        default="summary",
        choices=["summary", "standard", "detailed"],
    )
    similar.set_defaults(
        handler=lambda args: query_engine(args).similar(
            args.target,
            max_results=args.max_results,
            detail_level=args.detail_level,
        )
    )

    route = subparsers.add_parser(
        "route", help="Trace an indexed route to handlers and middleware."
    )
    route.add_argument("method", help="HTTP method, for example GET or POST.")
    route.add_argument("path", help="Route path, for example /api/v1/memories.")
    route.set_defaults(
        handler=lambda args: query_engine(args).route(args.method, args.path)
    )

    worker = subparsers.add_parser("worker", help="Trace an ARQ worker task or queue.")
    worker.add_argument(
        "target", help="worker:<id>, queue:<name>, task name, or queue name."
    )
    worker.set_defaults(handler=lambda args: query_engine(args).worker(args.target))

    architecture = subparsers.add_parser(
        "architecture", help="Show a basic architecture report."
    )
    architecture.set_defaults(handler=lambda args: query_engine(args).architecture())

    add_visual_parser(subparsers)
    trace = subparsers.add_parser("trace", help="Import runtime trace evidence.")
    trace_subparsers = trace.add_subparsers(dest="trace_command")
    trace_import = trace_subparsers.add_parser(
        "import", help="Import a ArcGraph runtime trace JSON file."
    )
    trace_import.add_argument("path", help="Runtime trace JSON file to import.")
    trace_import.add_argument(
        "--max-events",
        type=int,
        default=50_000,
        help="Maximum trace events to import from the file.",
    )
    trace_import.add_argument(
        "--max-file-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum runtime trace JSON file size to import.",
    )
    trace_import.add_argument(
        "--max-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to spend importing runtime trace events.",
    )
    trace_import.set_defaults(handler=handle_trace_import)
    trace_import_otel = trace_subparsers.add_parser(
        "import-otel", help="Import offline OpenTelemetry span JSON/JSONL."
    )
    trace_import_otel.add_argument("path", help="OpenTelemetry span JSON/JSONL file.")
    trace_import_otel.add_argument(
        "--max-spans",
        type=int,
        default=50_000,
        help="Maximum OpenTelemetry spans to inspect from the file.",
    )
    trace_import_otel.add_argument(
        "--max-file-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum OpenTelemetry span file size to import.",
    )
    trace_import_otel.add_argument(
        "--max-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to spend importing OpenTelemetry spans.",
    )
    trace_import_otel.set_defaults(handler=handle_trace_import_otel)
    trace_import_har = trace_subparsers.add_parser(
        "import-har", help="Import offline HAR browser network JSON."
    )
    trace_import_har.add_argument("path", help="HAR 1.2 JSON file to import.")
    trace_import_har.add_argument(
        "--max-entries",
        type=int,
        default=50_000,
        help="Maximum HAR entries to inspect from the file.",
    )
    trace_import_har.add_argument(
        "--max-file-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum HAR file size to import.",
    )
    trace_import_har.add_argument(
        "--max-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to spend importing HAR entries.",
    )
    trace_import_har.set_defaults(handler=handle_trace_import_har)
    trace_run = trace_subparsers.add_parser(
        "run", help="Run pytest under a lightweight ArcGraph runtime tracer."
    )
    trace_run.add_argument(
        "--output",
        required=True,
        help="Path to write the runtime trace JSON file.",
    )
    trace_run.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root to map runtime frames. Can be repeated.",
    )
    trace_run.add_argument(
        "--max-events",
        type=int,
        default=50_000,
        help="Maximum runtime call events to record.",
    )
    trace_run.add_argument(
        "--max-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to trace each pytest test call.",
    )
    trace_run.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to pytest after an optional -- separator.",
    )
    _add_legacy_roots_arg(trace_run)
    trace_run.set_defaults(handler=handle_trace_run)

    precision = subparsers.add_parser(
        "precision", help="Generate or convert precision input artifacts."
    )
    precision_subparsers = precision.add_subparsers(dest="precision_command")
    precision_scip_json = precision_subparsers.add_parser(
        "scip-json", help="Convert a binary index.scip file to JSON."
    )
    precision_scip_json.add_argument(
        "--input",
        required=True,
        help="Path to the binary SCIP index, usually index.scip.",
    )
    precision_scip_json.add_argument(
        "--output",
        required=True,
        help="Path to write ArcGraph-readable SCIP JSON.",
    )
    precision_scip_json.add_argument(
        "--scip-command",
        default="scip",
        help="SCIP CLI command to run. Defaults to scip.",
    )
    precision_scip_json.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root represented by the SCIP index. Defaults to ArcGraph roots.",
    )
    _add_legacy_roots_arg(precision_scip_json)
    precision_scip_json.set_defaults(handler=handle_precision_scip_json)

    precision_scip_python = precision_subparsers.add_parser(
        "scip-python", help="Run scip-python and convert its index to JSON."
    )
    precision_scip_python.add_argument(
        "--output",
        required=True,
        help="Path to write ArcGraph-readable SCIP JSON.",
    )
    precision_scip_python.add_argument(
        "--index-file",
        help="Path to keep the generated binary SCIP index. Defaults beside --output.",
    )
    precision_scip_python.add_argument(
        "--project-name",
        help="Project name passed to scip-python. Defaults to the repository name.",
    )
    precision_scip_python.add_argument(
        "--project-version",
        help="Optional project version passed to scip-python.",
    )
    precision_scip_python.add_argument(
        "--target-only",
        action="append",
        default=[],
        help="Restrict scip-python to a target path. Can be repeated.",
    )
    precision_scip_python.add_argument(
        "--environment",
        help="Optional scip-python environment JSON file.",
    )
    precision_scip_python.add_argument(
        "--scip-python-command",
        default="scip-python",
        help="scip-python command to run. Defaults to scip-python.",
    )
    precision_scip_python.add_argument(
        "--scip-command",
        default="scip",
        help="SCIP CLI command to run for JSON conversion. Defaults to scip.",
    )
    precision_scip_python.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
        help="Maximum seconds to wait for scip-python.",
    )
    precision_scip_python.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root represented by the generated SCIP index. Defaults to ArcGraph roots.",
    )
    _add_legacy_roots_arg(precision_scip_python)
    precision_scip_python.set_defaults(handler=handle_precision_scip_python)

    precision_pyright = precision_subparsers.add_parser(
        "pyright", help="Generate ArcGraph-readable Pyright precision JSON."
    )
    precision_pyright.add_argument(
        "--output",
        required=True,
        help="Path to write ArcGraph-readable Pyright JSON.",
    )
    precision_pyright.add_argument(
        "--target-only",
        action="append",
        default=[],
        help="Restrict Pyright probes to a target path. Can be repeated.",
    )
    precision_pyright.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root to scan for Pyright probes. Defaults to ArcGraph roots.",
    )
    precision_pyright.add_argument(
        "--python-version",
        default="3.11",
        help="Python version passed in the Pyright LSP initialization options.",
    )
    precision_pyright.add_argument(
        "--extra-path",
        action="append",
        default=[],
        help="Extra import path passed in the Pyright LSP initialization options.",
    )
    precision_pyright.add_argument(
        "--pyright-command",
        default="pyright-langserver",
        help="Pyright language server command. Defaults to pyright-langserver.",
    )
    precision_pyright.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Maximum seconds to wait for the Pyright LSP probe.",
    )
    _add_legacy_roots_arg(precision_pyright)
    precision_pyright.set_defaults(handler=handle_precision_pyright)

    evidence = subparsers.add_parser(
        "evidence", help="Inspect and plan external evidence artifacts."
    )
    evidence_subparsers = evidence.add_subparsers(dest="evidence_command")
    evidence_status_cmd = evidence_subparsers.add_parser(
        "status", help="Report live evidence, index snapshot, and capability status."
    )
    evidence_status_cmd.set_defaults(handler=handle_evidence_status)
    evidence_plan_cmd = evidence_subparsers.add_parser(
        "plan", help="Report live evidence health and suggested generation commands."
    )
    evidence_plan_cmd.add_argument(
        "--profile",
        choices=["default", "python_full"],
        default="default",
        help="Evidence planning profile. python_full marks blockers for ci --python-full.",
    )
    evidence_plan_cmd.set_defaults(handler=handle_evidence_plan)
    evidence_stamp_cmd = evidence_subparsers.add_parser(
        "stamp", help="Write ArcGraph provenance metadata beside an evidence artifact."
    )
    evidence_stamp_cmd.add_argument(
        "--kind",
        required=True,
        choices=[
            "precision_scip",
            "precision_pyright",
            "coverage",
            "runtime_trace",
        ],
    )
    evidence_stamp_cmd.add_argument(
        "--path",
        required=True,
        help="Evidence artifact path to stamp, such as output/arcgraph/coverage.xml.",
    )
    evidence_stamp_cmd.add_argument(
        "--tool-name",
        required=True,
        help="Tool that generated the artifact, such as coverage or pytest-cov.",
    )
    evidence_stamp_cmd.add_argument("--tool-version")
    evidence_stamp_cmd.set_defaults(handler=handle_evidence_stamp)

    ci = subparsers.add_parser("ci", help="Run ArcGraph CI checks.")
    ci.add_argument("--target", action="append", default=[])
    ci.add_argument("--changed-file", action="append", default=[])
    ci.add_argument("--max-results", type=int, default=30)
    ci.add_argument(
        "--semantic-baseline",
        help="Optional semantic-stats or CI JSON baseline for resolution trend checks.",
    )
    ci.add_argument(
        "--performance-budget-ms",
        action="append",
        default=[],
        metavar="OPERATION=MS",
        help="Override a CI performance budget in milliseconds. Can be repeated.",
    )
    ci.add_argument(
        "--require-precision",
        action="store_true",
        help="Fail when SCIP/Pyright precision inputs are unavailable.",
    )
    ci.add_argument(
        "--require-coverage",
        action="store_true",
        help="Fail when coverage input is unavailable or partial.",
    )
    ci.add_argument(
        "--require-runtime-trace",
        action="store_true",
        help="Fail when runtime trace input is unavailable or partial.",
    )
    ci.add_argument(
        "--python-full",
        action="store_true",
        help=(
            "Require the full Python evidence profile: precision, coverage, "
            "and runtime trace inputs must all be available."
        ),
    )
    _add_ci_runtime_trace_args(ci)
    ci.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Return a non-zero exit code when any CI check warns.",
    )
    ci.set_defaults(handler=handle_ci)

    ops = subparsers.add_parser("ops", help="Run ArcGraph operational tasks.")
    ops_subparsers = ops.add_subparsers(dest="ops_command")
    ops_rebuild = ops_subparsers.add_parser(
        "rebuild", help="Build or refresh the current ArcGraph index."
    )
    ops_rebuild.add_argument(
        "--if-stale",
        action="store_true",
        help="Skip rebuild when the current index is fresh.",
    )
    ops_rebuild.add_argument(
        "--root",
        action="append",
        default=[],
        help="Source root to scan. Can be repeated.",
    )
    ops_rebuild.add_argument(
        "--scip-index",
        help="Optional SCIP-derived JSON occurrence file for confirmed references.",
    )
    ops_rebuild.add_argument(
        "--pyright-export",
        help="Optional Pyright-derived JSON export path for the precision input contract.",
    )
    ops_rebuild.add_argument(
        "--runtime-trace",
        help="Optional ArcGraph runtime trace JSON file to import as runtime-only evidence.",
    )
    ops_rebuild.add_argument(
        "--coverage",
        help="Optional coverage JSON or Cobertura XML file for runtime test evidence.",
    )
    ops_rebuild.add_argument(
        "--semantic-call-resolution",
        action="store_true",
        help=(
            "Compatibility flag retained for older scripts; V2 receiver-aware "
            "call resolution is the default at schema 1.0."
        ),
    )
    ops_rebuild.add_argument(
        "--legacy-call-resolution",
        action="store_true",
        help=(
            "Use the legacy AST call resolver. This escape hatch is rejected by "
            "ci --python-full."
        ),
    )
    _add_legacy_roots_arg(ops_rebuild)
    ops_rebuild.set_defaults(handler=handle_ops_rebuild)
    ops_stale = ops_subparsers.add_parser(
        "stale-alert", help="Report whether the current index is stale."
    )
    ops_stale.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="Return a non-zero exit code when the index is stale.",
    )
    ops_stale.set_defaults(handler=handle_ops_stale_alert)
    ops_prune = ops_subparsers.add_parser(
        "prune",
        help="Safely prune stale generated ArcGraph output.",
    )
    ops_prune.add_argument(
        "--keep-builds",
        type=int,
        default=3,
        help="Number of build directories to retain, always including current.",
    )
    ops_prune.add_argument(
        "--include-input-artifacts",
        action="store_true",
        help=(
            "Also delete generated root-level precision, coverage, and runtime "
            "input artifacts. Build history is the only default cleanup scope."
        ),
    )
    ops_prune.add_argument(
        "--apply",
        action="store_true",
        help="Delete planned candidates. Without this flag, prune is a dry run.",
    )
    ops_prune.set_defaults(handler=handle_ops_prune)

    metrics = subparsers.add_parser("metrics", help="Summarize ArcGraph metrics JSONL.")
    metrics.add_argument("path", help="Metrics JSONL path.")
    metrics.add_argument("--limit", type=int, default=1000)
    metrics.add_argument(
        "--trial-summary",
        action="store_true",
        help=(
            "Return a privacy-bounded trial aggregate without paths, timestamps, "
            "or raw events."
        ),
    )
    metrics.set_defaults(handler=handle_metrics_summary)

    add_change_parser(subparsers)
    mcp = subparsers.add_parser("mcp", help="Run alpha local MCP server commands.")
    mcp_subparsers = mcp.add_subparsers(dest="mcp_command")
    mcp.set_defaults(handler=lambda args, parser=mcp: parser.print_help())
    mcp_serve = mcp_subparsers.add_parser(
        "serve",
        help=(
            "Run the local stdio MCP server. Default tools are read-only; an "
            "explicit feedback log enables one local append. Requires the "
            "optional MCP runtime extra."
        ),
    )
    add_mcp_server_args(mcp_serve)
    mcp_serve.set_defaults(handler=handle_mcp_serve)

    add_trial_parser(subparsers)
    add_setup_parser(subparsers)

    symbol = subparsers.add_parser(
        "symbol", help="Find a module, class, function, or method definition."
    )
    symbol.add_argument("qualname", help="Dotted qualname or stable symbol id.")
    symbol.set_defaults(handler=lambda args: query_engine(args).symbol(args.qualname))

    current = subparsers.add_parser("current", help="Show current index metadata.")
    current.set_defaults(handler=lambda args: query_engine(args).current())
    status = subparsers.add_parser("status", help="Alias for current index metadata.")
    status.set_defaults(handler=lambda args: query_engine(args).current())

    add_workspace_parser(subparsers)
    add_benchmark_parser(subparsers)
    add_feedback_parser(subparsers)
    agent_help = subparsers.add_parser(
        "help",
        help="Show structured Agent guidance for ArcGraph capabilities.",
    )
    agent_help.add_argument(
        "--topic",
        default="overview",
        choices=HELP_TOPICS,
        help="Agent guidance topic to render.",
    )
    agent_help.add_argument(
        "--tool",
        help="Exact registered MCP tool name to explain.",
    )
    agent_help.add_argument(
        "--surface",
        default="all",
        choices=HELP_SURFACES,
        help="Capability surface to include.",
    )
    agent_help.set_defaults(
        handler=lambda args, choices=subparsers.choices: handle_agent_help(
            args,
            cli_commands=tuple(choices),
        )
    )
    docs = subparsers.add_parser(
        "docs", help="Show built-in ArcGraph user reference docs."
    )
    docs.add_argument(
        "topic",
        nargs="?",
        default="cli-reference",
        choices=DOC_TOPICS,
        help="Documentation topic to render.",
    )
    docs.add_argument(
        "--json",
        action="store_true",
        help="Render the docs topic as structured JSON.",
    )
    docs.set_defaults(handler=lambda args: render_docs(args.topic, as_json=args.json))

    return parser


def handle_agent_help(
    args: argparse.Namespace,
    *,
    cli_commands: tuple[str, ...],
) -> dict[str, Any]:
    payload = build_agent_help(
        topic=cast(HelpTopic, args.topic),
        tool_name=args.tool,
        surface=args.surface,
        cli_commands=cli_commands,
    )
    if payload["status"] == "unavailable":
        payload["_exit_code"] = 2
    return payload


def _resolve_source_roots(
    args: argparse.Namespace,
    repo_root: Path,
) -> ResolvedSourceRoots:
    """Unified source root resolution for all CLI commands.

    Priority: --root (explicit) > --legacy-roots > auto-detect.
    """
    has_root = bool(getattr(args, "root", None))
    has_legacy = getattr(args, "legacy_roots", False)

    if has_root and has_legacy:
        raise SystemExit("--root and --legacy-roots are mutually exclusive")

    if has_root:
        roots = infer_source_roots(args.root)
        return ResolvedSourceRoots(
            roots=tuple(roots),
            detection=SourceRootDetection(
                strategy="cli_explicit",
                roots=tuple(r.path for r in roots),
            ),
        )

    if has_legacy:
        return ResolvedSourceRoots(
            roots=LEGACY_SOURCE_ROOTS,
            detection=SourceRootDetection(
                strategy="legacy_roots",
                roots=tuple(r.path for r in LEGACY_SOURCE_ROOTS),
            ),
        )

    return detect_source_roots(repo_root)


def handle_init(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    pyproject_path = repo_root / "pyproject.toml"
    resolved = detect_source_roots(repo_root)
    snippet = _arcgraph_config_snippet(resolved.roots)
    base_payload: dict[str, Any] = {
        "status": "available",
        "repo_root": str(repo_root),
        "pyproject_path": str(pyproject_path),
        "pyproject_exists": pyproject_path.exists(),
        "source_root_detection": {
            "strategy": resolved.detection.strategy,
            "roots": list(resolved.detection.roots),
            "non_package_dirs": list(resolved.detection.non_package_dirs),
        },
        "source_roots": [
            {"path": root.path, "module_prefix": root.module_prefix}
            for root in resolved.roots
        ],
        "config_snippet": snippet,
        "wrote": False,
    }

    if not pyproject_path.exists():
        base_payload.update(
            {
                "action": "manual_config",
                "message": (
                    "No pyproject.toml found. Add the config snippet manually, "
                    "or create pyproject.toml first."
                ),
                "next_steps": [
                    "Add the shown [tool.arcgraph] section to pyproject.toml.",
                    "Run arcgraph build.",
                ],
            }
        )
        return base_payload

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    if _pyproject_has_arcgraph_config(pyproject_text):
        base_payload.update(
            {
                "action": "already_configured",
                "message": "pyproject.toml already contains [tool.arcgraph] source_roots.",
                "next_steps": ["Run arcgraph build."],
            }
        )
        return base_payload

    if args.dry_run:
        base_payload.update(
            {
                "action": "dry_run",
                "message": "Dry run only; pyproject.toml was not modified.",
                "next_steps": [
                    "Rerun without --dry-run to write the config.",
                    "Run arcgraph build.",
                ],
            }
        )
        return base_payload

    separator = "" if pyproject_text.endswith("\n") else "\n"
    pyproject_path.write_text(
        f"{pyproject_text}{separator}\n{snippet}\n",
        encoding="utf-8",
    )
    base_payload.update(
        {
            "action": "written",
            "message": "Added [tool.arcgraph] to pyproject.toml.",
            "wrote": True,
            "next_steps": ["Run arcgraph build."],
        }
    )
    return base_payload


def handle_doctor(args: argparse.Namespace) -> dict[str, Any]:
    """Run environment and index health checks."""
    import shutil

    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    checks: list[dict[str, Any]] = []
    detected_source_roots: list[str] = []
    project_languages = detect_project_languages(repo_root)

    # 1. Project config -------------------------------------------------------
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists() and project_languages == {"node"}:
        checks.append(
            {
                "name": "project_config",
                "status": "pass",
                "message": (
                    "Node/TypeScript project detected from package.json or tsconfig; "
                    "pyproject.toml is not required."
                ),
            }
        )
    elif not pyproject_path.exists():
        checks.append(
            {
                "name": "project_config",
                "status": "warn",
                "message": "No pyproject.toml found.",
                "fix": "Run arcgraph init or create pyproject.toml manually.",
            }
        )
    else:
        text = pyproject_path.read_text(encoding="utf-8")
        if _pyproject_has_arcgraph_config(text):
            checks.append(
                {
                    "name": "project_config",
                    "status": "pass",
                    "message": "pyproject.toml has [tool.arcgraph] source_roots config.",
                }
            )
        else:
            checks.append(
                {
                    "name": "project_config",
                    "status": "warn",
                    "message": "pyproject.toml exists but has no [tool.arcgraph] source_roots config.",
                    "fix": "Run arcgraph init to add it.",
                }
            )

    # 2. Source roots ----------------------------------------------------------
    external_toolchains: dict[str, Any] | None = None
    try:
        resolved = detect_source_roots(repo_root)
        detected_source_roots = [root.path for root in resolved.roots]
        root_paths = [repo_root / r.path for r in resolved.roots]
        missing = [r.path for r in resolved.roots if not (repo_root / r.path).is_dir()]
        if missing:
            checks.append(
                {
                    "name": "source_roots",
                    "status": "fail",
                    "message": f"{len(missing)} source root(s) missing on disk: {', '.join(missing)}.",
                    "fix": "Check your [tool.arcgraph] source_roots or project layout.",
                }
            )
        else:
            source_counts = source_file_counts(root_paths)
            source_count = sum(source_counts.values())
            checks.append(
                {
                    "name": "source_roots",
                    "status": "pass" if source_count > 0 else "warn",
                    "message": (
                        f"{len(resolved.roots)} root(s) detected ({resolved.detection.strategy}), "
                        f"{source_count} supported source file(s) "
                        f"({', '.join(f'{name}={count}' for name, count in source_counts.items() if count)})."
                    ),
                    "fix": "Check source_roots config." if source_count == 0 else None,
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "source_roots",
                "status": "fail",
                "message": f"Source root detection failed: {exc}",
            }
        )

    # 3. Index -----------------------------------------------------------------
    current_path = output_dir / "current.json"
    if not current_path.exists():
        checks.append(
            {
                "name": "index",
                "status": "warn",
                "message": f"No index found at {output_dir}.",
                "fix": "Run arcgraph build.",
            }
        )
    else:
        try:
            engine = QueryEngine(output_dir)
            cur = engine.current()
            raw_toolchains = cur.get("toolchain_status", {})
            external_toolchains = (
                raw_toolchains if isinstance(raw_toolchains, dict) else {}
            )
            freshness = cur.get("freshness", {})
            fresh_status = freshness.get("status", "unknown")
            schema = cur.get("schema_version", "?")
            nodes = cur.get("node_count", 0)
            edges = cur.get("edge_count", 0)
            if fresh_status == "fresh":
                checks.append(
                    {
                        "name": "index",
                        "status": "pass",
                        "message": f"Index is fresh (schema {schema}, {nodes:,} nodes, {edges:,} edges).",
                    }
                )
            else:
                stale_files = freshness.get("stale_files", [])
                checks.append(
                    {
                        "name": "index",
                        "status": "warn",
                        "message": (
                            f"Index is {fresh_status} (schema {schema}, "
                            f"{len(stale_files)} file(s) changed)."
                        ),
                        "fix": (
                            "Run arcgraph sync --if-stale; use arcgraph build "
                            "when the index is missing or incompatible."
                        ),
                    }
                )
        except SchemaVersionError as exc:
            checks.append(
                {
                    "name": "index",
                    "status": "fail",
                    "message": str(exc),
                    "fix": "Run arcgraph build to rebuild with the current schema.",
                }
            )
        except FileNotFoundError as exc:
            checks.append(
                {
                    "name": "index",
                    "status": "fail",
                    "message": str(exc),
                    "fix": "Run arcgraph build.",
                }
            )

    # 4. External semantic extractor toolchains -------------------------------
    if external_toolchains:
        toolchain_summary = summarize_toolchain_status(external_toolchains)
        required_missing = toolchain_summary["required_unavailable"]
        checks.append(
            {
                "name": "external_toolchains",
                "status": "warn" if required_missing else "pass",
                "message": (
                    "External semantic extractor toolchains are available or optional."
                    if not required_missing
                    else "Required external semantic extractor toolchains are unavailable."
                ),
                "details": toolchain_summary,
            }
        )
    else:
        checks.append(
            {
                "name": "external_toolchains",
                "status": "pass",
                "message": "No external semantic extractor toolchains recorded.",
            }
        )

    # 5. Evidence health ------------------------------------------------------
    try:
        manifest = load_live_evidence_manifest(
            repo_root=repo_root,
            output_dir=output_dir,
            commit_sha=current_commit(repo_root),
            source_roots=detected_source_roots or _detect_source_root_paths(repo_root),
        )
        evidence_status = evidence_health_check_status(manifest)
        checks.append(
            {
                "name": "evidence_health",
                "status": "pass" if evidence_status == "available" else "warn",
                "message": (
                    "All external evidence artifacts are available."
                    if evidence_status == "available"
                    else f"External evidence health is {evidence_status}."
                ),
                "fix": (
                    None
                    if evidence_status == "available"
                    else "Run arcgraph evidence plan for generation commands."
                ),
                "details": manifest.get("summary", {}),
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "evidence_health",
                "status": "warn",
                "message": f"Evidence health could not be evaluated: {exc}",
                "fix": "Run arcgraph build to regenerate evidence manifest metadata.",
            }
        )

    # 6. Precision tools -------------------------------------------------------
    tool_status: list[tuple[str, str | None]] = [
        ("scip-python", shutil.which("scip-python")),
        ("scip", shutil.which("scip")),
        ("pyright-langserver", shutil.which("pyright-langserver")),
    ]
    found = [(name, path) for name, path in tool_status if path]
    missing_tools = [name for name, path in tool_status if not path]
    if not missing_tools:
        checks.append(
            {
                "name": "precision_tools",
                "status": "pass",
                "message": f"All precision tools available ({', '.join(n for n, _ in found)}).",
            }
        )
    else:
        checks.append(
            {
                "name": "precision_tools",
                "status": "warn",
                "message": f"Missing: {', '.join(missing_tools)}.",
                "fix": (
                    "Run scripts/install-arcgraph-precision-tools.ps1 (Windows) or "
                    "install scip-python, scip, pyright manually. "
                    "Precision tools are optional; builds work without them."
                ),
            }
        )

    import platform

    py_ver = platform.python_version()
    py_tuple = tuple(int(x) for x in py_ver.split(".")[:2])
    if (3, 11) <= py_tuple < (3, 13):
        checks.append(
            {
                "name": "python_version",
                "status": "pass",
                "message": f"Python {py_ver}.",
            }
        )
    else:
        checks.append(
            {
                "name": "python_version",
                "status": "fail",
                "message": f"Python {py_ver} (requires >=3.11,<3.13).",
                "fix": "Recreate the tool environment with Python 3.11 or 3.12.",
            }
        )

    if "node" in project_languages:
        node_command = shutil.which("node")
        if node_command is None:
            checks.append(
                {
                    "name": "node_runtime",
                    "status": "fail",
                    "message": "Node/TypeScript project detected but Node.js is unavailable.",
                    "fix": "Install a supported Node.js runtime (20, 22, or 24).",
                }
            )
        else:
            try:
                completed = subprocess.run(
                    [node_command, "--version"],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                completed = None
            checks.append(
                {
                    "name": "node_runtime",
                    "status": (
                        "pass"
                        if completed is not None and completed.returncode == 0
                        else "fail"
                    ),
                    "message": (
                        f"Node.js {completed.stdout.strip()}."
                        if completed is not None and completed.returncode == 0
                        else (
                            "Node.js version probe timed out."
                            if completed is None
                            else "Node.js version probe failed."
                        )
                    ),
                }
            )
            try:
                compiler = subprocess.run(
                    [node_command, "-e", "require.resolve('typescript')"],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                compiler = None
            checks.append(
                {
                    "name": "typescript_compiler",
                    "status": (
                        "pass"
                        if compiler is not None and compiler.returncode == 0
                        else "warn"
                    ),
                    "message": (
                        "TypeScript compiler API is resolvable from the project."
                        if compiler is not None and compiler.returncode == 0
                        else (
                            "TypeScript compiler probe timed out."
                            if compiler is None
                            else "TypeScript compiler API is not resolvable from the project."
                        )
                    ),
                    "fix": (
                        None
                        if compiler is not None and compiler.returncode == 0
                        else "Install the project's locked TypeScript dependency."
                    ),
                }
            )
    fail_count = sum(1 for c in checks if c["status"] == "fail")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    overall = "fail" if fail_count > 0 else "warn" if warn_count > 0 else "pass"

    return {
        "status": overall,
        "project_languages": sorted(project_languages),
        "checks": checks,
        "summary": {
            "pass": sum(1 for c in checks if c["status"] == "pass"),
            "warn": warn_count,
            "fail": fail_count,
        },
    }


def handle_build(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    # Merge [tool.arcgraph] exclude patterns with default ignore_rules
    exclude = resolved.detection.exclude
    ignore_rules: list[str] | None = (
        list(DEFAULT_IGNORE_RULES) + list(exclude) if exclude else None
    )
    indexer = ArcGraphIndexer(
        repo_root,
        output_dir,
        resolved.roots,
        ignore_rules=ignore_rules,
        scip_index_path=args.scip_index,
        scip_graph_index_path=args.scip_graph_index,
        openapi_path=args.openapi_spec,
        pyright_export_path=args.pyright_export,
        runtime_trace_path=args.runtime_trace,
        coverage_path=args.coverage,
        enable_v2_call_resolution=not args.legacy_call_resolution,
        source_root_detection=resolved.detection,
    )
    metadata, build_dir = indexer.build()
    payload = metadata.model_dump(mode="json")
    payload["build_dir"] = str(build_dir)
    if indexer.last_cleanup_result is not None:
        payload["cleanup"] = indexer.last_cleanup_result
    return payload


def handle_reindex(args: argparse.Namespace) -> dict[str, Any]:
    if not args.changed:
        raise RuntimeError("Phase 3 only supports ArcGraph reindex --changed")
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return ArcGraphReindexer(repo_root, output_dir).reindex_changed()


def handle_semantic_stats(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    payload = query_engine(args).semantic_stats()
    output_path = (
        Path(args.output)
        if args.output
        else output_dir / "metrics" / "semantic-stats.json"
    )
    named = bool(args.output)
    payload["metrics_path"] = str(output_location(output_path, named_by_user=named))
    write_json_output(output_path, payload, named_by_user=named)
    return payload


def handle_ci(args: argparse.Namespace) -> dict[str, Any]:
    payload = handle_ci_payload(args)
    payload["_exit_code"] = 1 if payload["status"] == "fail" else 0
    if args.fail_on_warnings and payload["status"] == "warn":
        payload["_exit_code"] = 1
    return payload


def handle_ci_payload(args: argparse.Namespace) -> dict[str, Any]:
    _import_runtime_trace_for_ci(args)
    return run_ci_checks(
        query_engine(args),
        targets=args.target,
        changed_files=args.changed_file,
        max_results=args.max_results,
        semantic_baseline=_load_json_arg(args.semantic_baseline),
        performance_budgets_ms=_load_performance_budgets_arg(
            args.performance_budget_ms
        ),
        require_precision=args.require_precision,
        require_coverage=args.require_coverage,
        require_runtime_trace=args.require_runtime_trace,
        require_python_full=args.python_full,
    )


_SHAPING_OPTIONS = (
    ("max_results", "--max-results"),
    ("detail_level", "--detail-level"),
    ("include_source", "--include-source"),
)


def _agent_payload_request(args: argparse.Namespace) -> ContextRequest:
    """Build the bounded-payload request, applying only explicit options.

    Options left at their ``None`` default fall through to the ``ContextRequest``
    defaults rather than pinning them here, so the schema stays the single
    source of truth for payload bounds.
    """
    fields: dict[str, Any] = {"targets": [args.target], "profile": args.profile}
    for attr, _flag in _SHAPING_OPTIONS:
        value = getattr(args, attr)
        if value is not None:
            fields[attr] = value
    return ContextRequest(**fields)


def _reject_raw_shaping(args: argparse.Namespace) -> None:
    """Fail loudly when ``--raw`` is combined with options it cannot apply.

    ``--raw`` returns the QueryEngine payload unchanged, so it can honour none
    of the shaping options. Accepting them and dropping them silently is what
    let ``callers TARGET --max-results 5`` return all 132 callers.
    """
    supplied = [
        flag for attr, flag in _SHAPING_OPTIONS if getattr(args, attr) is not None
    ]
    if supplied:
        # RuntimeError, not ValueError: main() re-raises ValueError for
        # non-change commands, which would surface this as a traceback.
        raise RuntimeError(
            f"--raw returns the unbounded debug payload and cannot apply "
            f"{', '.join(supplied)}. Drop --raw to use them."
        )


def handle_callers(args: argparse.Namespace) -> dict[str, Any]:
    if args.raw:
        _reject_raw_shaping(args)
        return query_engine(args).callers(args.target, profile=args.profile)
    request = _agent_payload_request(args)
    return ContextProvider(query_engine(args)).compact_callers(args.target, request)


def handle_callees(args: argparse.Namespace) -> dict[str, Any]:
    if args.raw:
        _reject_raw_shaping(args)
        return query_engine(args).callees(args.target, profile=args.profile)
    request = _agent_payload_request(args)
    return ContextProvider(query_engine(args)).compact_callees(args.target, request)


def handle_impact(args: argparse.Namespace) -> dict[str, Any]:
    if args.raw:
        _reject_raw_shaping(args)
        return query_engine(args).impact(
            args.target,
            max_depth=args.max_depth,
            profile=args.profile,
            include_edge_kinds=args.include_edge_kind,
            exclude_edge_kinds=args.exclude_edge_kind,
        )
    request = _agent_payload_request(args)
    return ContextProvider(query_engine(args)).compact_impact(
        args.target,
        request,
        max_depth=args.max_depth,
        include_edge_kinds=args.include_edge_kind,
        exclude_edge_kinds=args.exclude_edge_kind,
    )


def handle_report_pr(args: argparse.Namespace) -> dict[str, Any] | str:
    repo_root = Path(args.repo_root).resolve()
    changed_files = args.changed_file or _infer_changed_files(repo_root)
    _import_runtime_trace_for_ci(args)
    ci_result = run_ci_checks(
        query_engine(args),
        targets=args.target,
        changed_files=changed_files,
        max_results=args.max_results,
        semantic_baseline=_load_json_arg(args.semantic_baseline),
        performance_budgets_ms=_load_performance_budgets_arg(
            args.performance_budget_ms
        ),
    )
    markdown = render_pr_markdown(ci_result)
    if not args.output:
        return markdown
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return {
        "schema_version": ci_result["schema_version"],
        "index_version": ci_result.get("index_version"),
        "status": ci_result.get("status", "unknown"),
        "path": str(output_path.resolve()),
        "changed_files": changed_files,
    }


def handle_report_html(args: argparse.Namespace) -> dict[str, Any] | str:
    engine = query_engine(args)
    query_target = _normalized_report_target(args.target)
    impact = engine.impact(
        query_target,
        profile=args.profile,
        include_edge_kinds=args.include_edge_kind,
        exclude_edge_kinds=args.exclude_edge_kind,
    )
    similar = engine.similar(query_target, max_results=args.max_results)
    architecture = engine.architecture()
    entrypoint_flow = (
        engine.entrypoint_flow(args.target, max_results=args.max_results)
        if _is_entrypoint_target(args.target)
        else None
    )
    ci_result = run_ci_checks(
        engine,
        targets=[query_target],
        max_results=args.max_results,
    )
    html = render_static_html_report(
        target=args.target,
        impact=impact,
        similar=similar,
        architecture=architecture,
        entrypoint_flow=entrypoint_flow,
        ci_result=ci_result,
    )
    if not args.output:
        return html
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return {
        "schema_version": impact["schema_version"],
        "index_version": impact.get("index_version"),
        "status": "available",
        "path": str(output_path.resolve()),
    }


def handle_report_metrics_html(args: argparse.Namespace) -> dict[str, Any] | str:
    metrics_path = Path(args.path)
    try:
        metrics = summarize_metrics(metrics_path, limit=args.limit)
    except MetricsLogEncodingError:
        return _metrics_report_error_payload(metrics_path, invalid_encoding=True)
    except PrivateLocalStateError:
        return _metrics_report_error_payload(metrics_path)
    html = render_metrics_dashboard_html(metrics)
    if not args.output:
        return html
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return {
        "status": metrics.get("status", "unknown"),
        "path": str(output_path.resolve()),
        "event_count": metrics.get("event_count", 0),
    }


def handle_report_unresolved(args: argparse.Namespace) -> dict[str, Any] | str:
    payload = query_engine(args).unresolved(args.target, limit=args.limit)
    classification = classify_unresolved_records(payload)
    markdown = render_unresolved_classification_markdown(payload)
    if not args.output:
        return markdown
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    json_output = Path(args.json_output) if args.json_output else None
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(classification, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
    return {
        "schema_version": payload["schema_version"],
        "index_version": payload.get("index_version"),
        "status": payload.get("status", "unknown"),
        "path": str(output_path.resolve()),
        "json_path": str(json_output.resolve()) if json_output else None,
        "total": payload.get("summary", {}).get("total", 0),
        "classified": payload.get("summary", {}).get("returned", 0),
        "category_counts": classification["category_counts"],
    }


def handle_context(args: argparse.Namespace) -> dict[str, Any]:
    request = ContextRequest(
        task=args.task,
        targets=args.target,
        max_results=args.max_results,
        detail_level=args.detail_level,
        profile=args.profile,
        include_source=args.include_source,
    )
    return ContextProvider(query_engine(args)).get_context(request)


def handle_explain(args: argparse.Namespace) -> dict[str, Any]:
    request = ContextRequest(
        task=args.task,
        targets=args.target,
        max_results=args.max_results,
        detail_level=args.detail_level,
        profile=args.profile,
        include_source=args.include_source,
    )
    return ContextProvider(query_engine(args)).explain(request)


def handle_ops_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    return rebuild_index(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=list(resolved.roots),
        if_stale=args.if_stale,
        scip_index_path=args.scip_index,
        pyright_export_path=args.pyright_export,
        runtime_trace_path=args.runtime_trace,
        coverage_path=args.coverage,
        enable_v2_call_resolution=not args.legacy_call_resolution,
        source_root_detection=resolved.detection,
    )


def handle_ops_stale_alert(args: argparse.Namespace) -> dict[str, Any]:
    return stale_alert(query_engine(args), fail_on_stale=args.fail_on_stale)


def handle_ops_prune(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return prune_output(
        output_dir=output_dir,
        keep_builds=args.keep_builds,
        include_input_artifacts=args.include_input_artifacts,
        apply=args.apply,
    )


def handle_trace_import(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=args.path,
        max_events=args.max_events,
        max_file_bytes=args.max_file_bytes,
        max_seconds=args.max_seconds,
    )


def handle_trace_import_otel(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return import_otel_spans(
        repo_root=repo_root,
        output_dir=output_dir,
        span_path=args.path,
        max_spans=args.max_spans,
        max_file_bytes=args.max_file_bytes,
        max_seconds=args.max_seconds,
    )


def handle_trace_import_har(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return import_har_network(
        repo_root=repo_root,
        output_dir=output_dir,
        har_path=args.path,
        max_entries=args.max_entries,
        max_file_bytes=args.max_file_bytes,
        max_seconds=args.max_seconds,
    )


def _add_ci_runtime_trace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-trace",
        help=(
            "Explicit runtime trace JSON to import before CI checks. "
            "CI never records or imports runtime evidence unless this is set."
        ),
    )
    parser.add_argument(
        "--runtime-trace-max-events",
        type=int,
        default=50_000,
        help="Maximum runtime trace events to import for CI.",
    )
    parser.add_argument(
        "--runtime-trace-max-file-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum runtime trace JSON file size to import for CI.",
    )
    parser.add_argument(
        "--runtime-trace-max-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to spend importing runtime trace events for CI.",
    )


def _import_runtime_trace_for_ci(args: argparse.Namespace) -> None:
    trace_path = getattr(args, "runtime_trace", None)
    if not trace_path:
        return
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=trace_path,
        max_events=args.runtime_trace_max_events,
        max_file_bytes=args.runtime_trace_max_file_bytes,
        max_seconds=args.runtime_trace_max_seconds,
    )


def handle_trace_run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    pytest_args = list(args.pytest_args or [])
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    return run_runtime_trace(
        repo_root=repo_root,
        output_path=args.output,
        pytest_args=pytest_args,
        source_roots=list(resolved.roots),
        max_events=args.max_events,
        max_seconds=args.max_seconds,
    )


def handle_precision_scip_json(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    return convert_scip_index_to_json(
        repo_root=repo_root,
        scip_index_path=args.input,
        output_path=args.output,
        scip_command=args.scip_command,
        source_roots=list(resolved.roots),
        commit_sha=current_commit(repo_root),
    )


def handle_precision_scip_python(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    return generate_scip_python_json(
        repo_root=repo_root,
        output_path=args.output,
        index_file=args.index_file,
        project_name=args.project_name,
        project_version=args.project_version,
        target_only=args.target_only,
        environment_path=args.environment,
        scip_python_command=args.scip_python_command,
        scip_command=args.scip_command,
        timeout_seconds=args.timeout_seconds,
        source_roots=list(resolved.roots),
        commit_sha=current_commit(repo_root),
    )


def handle_precision_pyright(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    resolved = _resolve_source_roots(args, repo_root)
    return generate_pyright_json(
        repo_root=repo_root,
        output_path=args.output,
        target_only=args.target_only,
        source_roots=list(resolved.roots),
        python_version=args.python_version,
        extra_path=args.extra_path,
        pyright_command=args.pyright_command,
        timeout_seconds=args.timeout_seconds,
        commit_sha=current_commit(repo_root),
    )


def handle_evidence_status(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    current = _current_index_or_empty(args)
    return evidence_status(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=current_commit(repo_root),
        source_roots=_detect_source_root_paths(repo_root),
        snapshot_manifest=(
            current.get("evidence_manifest") if isinstance(current, dict) else None
        ),
        capabilities=(
            current.get("capabilities", {}) if isinstance(current, dict) else {}
        ),
    )


def handle_evidence_plan(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    current = _current_index_or_empty(args)
    requirements = (
        evidence_requirement_summary(current, require_python_full=True)["requirements"]
        if args.profile == "python_full" and current
        else None
    )
    return evidence_plan(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=current_commit(repo_root),
        source_roots=_detect_source_root_paths(repo_root),
        profile=args.profile,
        project_name=_project_name_for_evidence(repo_root, current),
        python_full_requirements=requirements,
    )


def handle_evidence_stamp(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    return write_evidence_sidecar(
        repo_root=repo_root,
        artifact_path=args.path,
        kind=args.kind,
        commit_sha=current_commit(repo_root),
        source_roots=_detect_source_root_paths(repo_root),
        tool_name=args.tool_name,
        tool_version=args.tool_version,
    )


def handle_metrics_summary(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.path)
    try:
        if args.trial_summary:
            return summarize_trial_metrics(path, limit=args.limit)
        return summarize_metrics(path, limit=args.limit)
    except MetricsLogEncodingError:
        return _metrics_read_error_payload(
            path,
            trial_summary=args.trial_summary,
            invalid_encoding=True,
        )
    except PrivateLocalStateError:
        return _metrics_read_error_payload(
            path,
            trial_summary=args.trial_summary,
        )


def _metrics_read_error_warning(*, invalid_encoding: bool) -> str:
    if invalid_encoding:
        return "Metrics log is not valid UTF-8. Start a new private metrics log."
    return "Metrics log could not be read safely. Use a new canonical private path."


def _metrics_read_error_payload(
    path: Path,
    *,
    trial_summary: bool,
    invalid_encoding: bool = False,
) -> dict[str, Any]:
    payload = metrics_unavailable_payload(
        path,
        trial_summary=trial_summary,
        warning=_metrics_read_error_warning(invalid_encoding=invalid_encoding),
    )
    payload["_exit_code"] = 2
    return payload


def _metrics_report_error_payload(
    metrics_path: Path,
    *,
    invalid_encoding: bool = False,
) -> dict[str, Any]:
    """Report a failed metrics read without borrowing the summary result shape.

    ``report metrics-html`` uses ``path`` for the report it wrote, and its
    success result can itself carry ``status: "unavailable"`` for an empty log.
    Reusing the summary payload here would make an unwritten report
    indistinguishable from a written one on every field a caller inspects.
    """

    return {
        "status": "error",
        "error_code": (
            METRICS_LOG_INVALID_ENCODING_ERROR_CODE
            if invalid_encoding
            else METRICS_LOG_UNREADABLE_ERROR_CODE
        ),
        "metrics_path": str(metrics_path),
        "event_count": 0,
        "warnings": [_metrics_read_error_warning(invalid_encoding=invalid_encoding)],
        "_exit_code": 2,
    }


def _top_level_command(argv: list[str]) -> str | None:
    options_with_values = {"--repo-root", "--output-dir", "--metrics-log"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in options_with_values:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in options_with_values):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def handle_mcp_serve(args: argparse.Namespace) -> None:
    serve_mcp(args)
    return None


def _detect_source_root_paths(repo_root: Path) -> list[str]:
    return [root.path for root in detect_source_roots(repo_root).roots]


def _current_index_or_empty(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return query_engine(args).current()
    except (FileNotFoundError, SchemaVersionError, RuntimeError, ValueError):
        return {}


def _project_name_for_evidence(repo_root: Path, current: dict[str, Any] | None) -> str:
    if isinstance(current, dict):
        current_name = current.get("project_name")
        if isinstance(current_name, str) and current_name:
            return current_name
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            import tomllib

            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            project = data.get("project")
            if isinstance(project, dict):
                name = project.get("name")
                if isinstance(name, str) and name:
                    return name
        except Exception:
            pass
    return repo_root.name


def _pyproject_has_arcgraph_config(text: str) -> bool:
    try:
        import tomllib

        data = tomllib.loads(text)
    except Exception as exc:
        raise RuntimeError(f"Unable to parse pyproject.toml: {exc}") from exc

    tool = data["tool"] if "tool" in data else None
    if not isinstance(tool, dict):
        return False
    arcgraph = tool["arcgraph"] if "arcgraph" in tool else None
    if not isinstance(arcgraph, dict):
        return False
    source_roots = arcgraph["source_roots"] if "source_roots" in arcgraph else None
    if not isinstance(source_roots, list):
        return False

    def path_value(entry: dict[str, Any]) -> Any:
        return entry["path"] if "path" in entry else None

    return any(
        (isinstance(entry, str) and bool(entry))
        or (
            isinstance(entry, dict)
            and isinstance(path_value(entry), str)
            and bool(path_value(entry))
        )
        for entry in source_roots
    )


def _arcgraph_config_snippet(roots: tuple[SourceRoot, ...]) -> str:
    lines = ["[tool.arcgraph]"]
    if all(not root.module_prefix for root in roots):
        values = ", ".join(_toml_string(root.path) for root in roots)
        if len(values) <= 72:
            lines.append(f"source_roots = [{values}]")
            return "\n".join(lines)

    lines.append("source_roots = [")
    for root in roots:
        if root.module_prefix:
            lines.append(
                "  { path = "
                f"{_toml_string(root.path)}, "
                f"module_prefix = {_toml_string(root.module_prefix)}"
                " },"
            )
        else:
            lines.append(f"  {_toml_string(root.path)},")
    lines.append("]")
    return "\n".join(lines)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# The classifier lives beside parse_route_target so every surface shares one
# definition. A real def rather than an alias assignment: ArcGraph's own
# resolver cannot follow calls through an alias binding, and the unresolved
# callsite would trip the release gate.
def _is_entrypoint_target(target: str) -> bool:
    return is_entrypoint_target(target)


def _normalized_report_target(target: str) -> str:
    if target.startswith("mcp:"):
        return f"mcp_tool:{target.split(':', 1)[1]}"
    parsed = parse_route_target(target)
    if parsed is not None:
        method, path = parsed
        return route_id(method, path)
    return target


def _infer_changed_files(repo_root: Path) -> list[str]:
    status_files = _git_status_files(repo_root)
    if status_files:
        return status_files

    candidates: list[list[str]] = []
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        candidates.append(["diff", "--name-only", f"origin/{base_ref}...HEAD"])
    candidates.extend(
        [
            ["diff", "--name-only", "HEAD~1", "HEAD"],
            ["diff", "--name-only", "HEAD"],
        ]
    )
    for args in candidates:
        files = _git_changed_files(repo_root, args)
        if files:
            return files
    return []


def _load_json_arg(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_performance_budgets_arg(items: list[str]) -> dict[str, float] | None:
    if not items:
        return None
    budgets: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise RuntimeError(
                "Performance budget must use OPERATION=MS, " f"got {item!r}."
            )
        operation, raw_value = item.split("=", 1)
        operation = operation.strip()
        if not operation:
            raise RuntimeError("Performance budget operation name must not be empty.")
        try:
            budgets[operation] = float(raw_value)
        except ValueError as exc:
            raise RuntimeError(
                f"Performance budget for {operation!r} must be a number."
            ) from exc
    return budgets


def _git_changed_files(repo_root: Path, args: list[str]) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return []
    return _python_files(completed.stdout.splitlines())


def _git_status_files(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for row in completed.stdout.splitlines():
        path = row[3:] if len(row) > 3 else ""
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        paths.append(path)
    return _python_files(paths)


def _python_files(paths: list[str]) -> list[str]:
    return sorted(
        dict.fromkeys(
            path.replace("\\", "/")
            for path in paths
            if path.replace("\\", "/").endswith(".py")
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
