"""CLI parser and handler for preview-first local trial setup."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from arcgraph.interfaces.trial_setup import trial_setup


def add_trial_parser(subparsers: Any) -> None:
    trial = subparsers.add_parser("trial", help="Plan safe local external-trial setup.")
    trial_subparsers = trial.add_subparsers(dest="trial_command")
    trial.set_defaults(handler=lambda args, parser=trial: parser.print_help())
    setup = trial_subparsers.add_parser(
        "setup", help="Check and optionally create private local trial paths."
    )
    setup.add_argument("--client", choices=("claude",), required=True)
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; this is also the default behavior.",
    )
    mode.add_argument(
        "--apply-local-files",
        action="store_true",
        help=(
            "Create private logs/directories and a local Git exclude; never "
            "edits Claude config."
        ),
    )
    setup.set_defaults(handler=handle_trial_setup)


def handle_trial_setup(args: argparse.Namespace) -> dict[str, Any]:
    return trial_setup(
        repo_root=Path(args.repo_root),
        client=args.client,
        apply_local_files=args.apply_local_files,
    )
