"""MCP command arguments, independent of the optional server runtime."""

from __future__ import annotations

import argparse

DEFAULT_MCP_SERVER_NAME = "ArcGraph"
DEFAULT_MCP_TRANSPORT = "stdio"


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
        help="MCP transport. Only stdio is supported.",
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
