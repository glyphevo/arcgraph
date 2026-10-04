"""Shared helpers for the ``arcgraph`` CLI modules.

Extracted so command-group modules (``cli_change``, ``cli_visual``, ...) can
use them without importing ``cli`` itself, which imports those modules in
turn. Dependencies run one way: ``cli_support`` -> group modules -> ``cli``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from arcgraph import SCHEMA_VERSION
from arcgraph.change.errors import ChangeSafetyError
from arcgraph.core.query_engine import QueryEngine, SchemaVersionError
from arcgraph.core.utils import replace_text_file


def query_engine(args: argparse.Namespace) -> QueryEngine:
    repo_root = Path(args.repo_root).resolve()
    return QueryEngine((repo_root / args.output_dir).resolve())


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def write_json_output(
    path: Path, payload: dict[str, Any], *, named_by_user: bool
) -> None:
    """Write a command's JSON output file.

    A path the user named is written like any other ``--output`` file.  A
    default path is one ArcGraph chose, so a symlink standing there is
    replaced rather than written through.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if named_by_user:
        path.write_text(text, encoding="utf-8")
    else:
        replace_text_file(path, text)


_CLI_ERROR_CODES: dict[type[BaseException], str] = {
    FileNotFoundError: "ARCGRAPH_INPUT_NOT_FOUND",
    SchemaVersionError: "ARCGRAPH_SCHEMA_VERSION_UNSUPPORTED",
    RuntimeError: "ARCGRAPH_RUNTIME_ERROR",
    TypeError: "ARCGRAPH_INTERNAL_ERROR",
    ValueError: "ARCGRAPH_INTERNAL_ERROR",
}


def _cli_error_code(exc: BaseException) -> str:
    if isinstance(exc, ChangeSafetyError):
        return str(exc.to_payload()["code"])
    for exc_type, code in _CLI_ERROR_CODES.items():
        if type(exc) is exc_type:
            return code
    for exc_type, code in _CLI_ERROR_CODES.items():
        if isinstance(exc, exc_type):
            return code
    return "ARCGRAPH_RUNTIME_ERROR"


def _print_cli_error_payload(args: argparse.Namespace, exc: BaseException) -> None:
    """Put a machine-readable error on stdout for a non-`change` command.

    JSON is this CLI's default output mode, so before this existed a failing
    command left stdout empty and an agent parsing it received nothing at all:
    it had to fall back to prose on stderr and an exit code. The stderr line is
    unchanged, and so are the exit codes, so this only fills a hole rather than
    moving anything a caller already reads.

    The message is not redacted here, unlike `change` payloads. Those are
    contract artifacts that get stored and handed on; this is local diagnostic
    output for the operator who ran the command, and it carries the same text
    the stderr line has always carried, paths included, because that is what
    tells them where to look.
    """

    if getattr(args, "human", False):
        return
    if getattr(args, "raw", False):
        # `--raw` promises stdout carries the QueryEngine payload and nothing
        # else. That contract predates this envelope and has its own test
        # stating the reason: a caller piping raw JSON must not receive a
        # different shape alongside a refusal. It keeps the exit code and the
        # stderr line; it does not get an envelope.
        return
    code = _cli_error_code(exc)
    print_json(
        {
            "schema_version": SCHEMA_VERSION,
            "command": getattr(args, "command", None),
            "status": "error",
            "error_code": code,
            "error": {"code": code, "message": str(exc)},
        }
    )
