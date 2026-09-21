"""CLI parser and handlers for privacy-bounded local trial feedback."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping, cast

from arcgraph.interfaces.agent_capabilities import ALL_MCP_CAPABILITY_NAMES
from arcgraph.interfaces.local_state import local_state_paths_alias
from arcgraph.interfaces.trial_feedback import (
    DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE,
    FEEDBACK_FALLBACKS,
    FEEDBACK_FRESHNESS_STATUSES,
    FEEDBACK_ISSUE_KINDS,
    FEEDBACK_OUTCOMES,
    FEEDBACK_RESULT_STATUSES,
    FEEDBACK_STAGES,
    FEEDBACK_SURFACES,
    FEEDBACK_WARNING_KINDS,
    MAX_WARNING_KINDS,
    TrialFeedbackError,
    TrialFeedbackInputError,
    TrialFeedbackStorageError,
    TrialFeedbackStore,
    feedback_error_payload,
    feedback_success_payload,
    validate_feedback_request,
)


def add_feedback_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    feedback = cast(
        argparse.ArgumentParser,
        subparsers.add_parser(
            "feedback",
            help="Record or summarize privacy-bounded local Agent trial feedback.",
        ),
    )
    feedback_subparsers = cast(
        argparse._SubParsersAction,
        feedback.add_subparsers(
            dest="feedback_command",
            required=True,
        ),
    )

    record = cast(
        argparse.ArgumentParser,
        feedback_subparsers.add_parser(
            "record",
            help="Append one validated feedback event to an explicit local log.",
        ),
    )
    record.add_argument(
        "--feedback-log",
        required=True,
        help=(
            "Absolute local JSONL path selected by the trial operator. It must "
            "differ from the global --metrics-log path."
        ),
    )
    record.add_argument("--client-event-id", required=True, help="Opaque UUID.")
    record.add_argument("--surface", required=True, choices=FEEDBACK_SURFACES)
    record.add_argument("--tool-name", required=True)
    record.add_argument("--outcome", required=True, choices=FEEDBACK_OUTCOMES)
    record.add_argument("--issue-kind", required=True, choices=FEEDBACK_ISSUE_KINDS)
    record.add_argument("--stage", required=True, choices=FEEDBACK_STAGES)
    record.add_argument("--fallback", required=True, choices=FEEDBACK_FALLBACKS)
    record.add_argument("--result-status", choices=FEEDBACK_RESULT_STATUSES)
    record.add_argument("--freshness-status", choices=FEEDBACK_FRESHNESS_STATUSES)
    record.add_argument(
        "--warning-kind",
        action="append",
        default=[],
        choices=FEEDBACK_WARNING_KINDS,
        help=(
            "Allowlisted warning kind; repeat at most "
            f"{MAX_WARNING_KINDS} times. Use 'other' for an unlisted kind."
        ),
    )
    record.set_defaults(
        handler=lambda args, choices=subparsers.choices: handle_feedback_record(
            args,
            cli_commands=choices,
        )
    )

    summarize = cast(
        argparse.ArgumentParser,
        feedback_subparsers.add_parser(
            "summarize",
            help="Return privacy-bounded aggregates without raw events or local paths.",
        ),
    )
    summarize.add_argument("path", help="Absolute local feedback JSONL path.")
    summarize.set_defaults(handler=handle_feedback_summarize)
    for parser in (feedback, record, summarize):
        parser._arcgraph_parse_error_payload = feedback_cli_parse_error_payload()
        parser._arcgraph_parse_error_exit_code = 2


def handle_feedback_record(
    args: argparse.Namespace,
    *,
    cli_commands: Mapping[str, argparse.ArgumentParser],
) -> dict[str, Any]:
    try:
        request = validate_feedback_request(
            {
                "client_event_id": args.client_event_id,
                "surface": args.surface,
                "tool_name": args.tool_name,
                "outcome": args.outcome,
                "issue_kind": args.issue_kind,
                "stage": args.stage,
                "fallback": args.fallback,
                "result_status": args.result_status,
                "freshness_status": args.freshness_status,
                "warning_kinds": args.warning_kind,
            },
            allowed_tool_names_by_surface={
                "cli": cli_commands,
                "mcp": ALL_MCP_CAPABILITY_NAMES,
            },
        )
        record, duplicate = TrialFeedbackStore(Path(args.feedback_log)).record(request)
    except TrialFeedbackError as exc:
        return _cli_error_payload(exc)
    return feedback_success_payload(record, duplicate=duplicate)


def handle_feedback_summarize(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return TrialFeedbackStore(Path(args.path)).summarize()
    except TrialFeedbackError as exc:
        return _cli_error_payload(exc)


def feedback_metrics_path_conflict_payload(
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """Reject a feedback command before CLI metrics can corrupt its log."""

    if getattr(args, "command", None) != "feedback":
        return None
    metrics_log = getattr(args, "metrics_log", None)
    feedback_log = getattr(args, "feedback_log", None) or getattr(args, "path", None)
    if (
        metrics_log
        and feedback_log
        and local_state_paths_alias(Path(metrics_log), Path(feedback_log))
    ):
        return _cli_error_payload(
            TrialFeedbackStorageError(DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE)
        )
    return None


def _cli_error_payload(exc: TrialFeedbackError) -> dict[str, Any]:
    payload = feedback_error_payload(exc)
    payload["_exit_code"] = 2
    return payload


def feedback_cli_parse_error_payload() -> dict[str, Any]:
    """Return one input-free machine error for feedback argparse failures."""

    return feedback_error_payload(
        TrialFeedbackInputError(
            "Feedback command arguments must use the documented required "
            "options and enum values."
        )
    )
