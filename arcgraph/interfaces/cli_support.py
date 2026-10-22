"""Shared helpers for the ``arcgraph`` CLI modules.

Extracted so command-group modules (``cli_change``, ``cli_visual``, ...) can
use them without importing ``cli`` itself, which imports those modules in
turn. Dependencies run one way: ``cli_support`` -> group modules -> ``cli``.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import tomllib
from pathlib import Path
from typing import Any

from arcgraph import SCHEMA_VERSION
from arcgraph.change.errors import ChangeSafetyError
from arcgraph.core.query_engine import QueryEngine, SchemaVersionError
from arcgraph.core.project_environment import (
    ProjectEnvironment,
    project_interpreter_path,
)
from arcgraph.core.utils import replace_text_file
from arcgraph.core.structure import StructureView, STRUCTURE_FIX

# requires-python of ArcGraph itself, as pyproject.toml declares it;
# test_cli_doctor checks the two agree.
SUPPORTED_PYTHON = ((3, 11), (3, 15))
_LOWER_BOUND = re.compile(r"^\s*(?:>=|>|~=|==)\s*(\d+)\.(\d+)")


def project_minimum_python(repo_root: Path) -> tuple[str, tuple[int, int]] | None:
    """The project's requires-python and the lowest minor version it admits.

    Only a lower bound counts: >=3.14, >3.14, ~=3.14 and ==3.14.* all admit
    no version below 3.14. None when the project declares no such bound.
    """

    try:
        data = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    requires = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(requires, str):
        return None
    bounds = [
        (int(match.group(1)), int(match.group(2)))
        for clause in requires.split(",")
        if (match := _LOWER_BOUND.match(clause))
    ]
    return (requires.strip(), max(bounds)) if bounds else None


def python_checks(repo_root: Path, *, semantic_state=None) -> list[dict[str, Any]]:
    """doctor's checks of the Python that runs ArcGraph."""

    version = platform.python_version()
    running = tuple(int(part) for part in version.split(".")[:2])
    low, high = SUPPORTED_PYTHON
    supported = f">={low[0]}.{low[1]},<{high[0]}.{high[1]}"
    newest = f"{high[0]}.{high[1] - 1}"
    if low <= running < high:
        checks = [
            {
                "name": "python_version",
                "status": "pass",
                "message": f"Python {version}.",
            }
        ]
    else:
        checks = [
            {
                "name": "python_version",
                "status": "fail",
                "message": f"Python {version} (requires {supported}).",
                "fix": (
                    "Recreate the tool environment with Python "
                    f"{low[0]}.{low[1]} to {newest}."
                ),
            }
        ]
    environment = ProjectEnvironment(repo_root)
    interpreter = project_interpreter_path(repo_root)
    structure = StructureView(repo_root)
    structure.probe(all_files=True)
    if semantic_state is not None:
        semantic_state.update(structure.status())
    checks.append(
        {
            "name": "project_trace_interpreter",
            "status": "pass" if structure.trace_available else "warn",
            "message": (
                f"Project trace interpreter: {interpreter}."
                if structure.trace_available
                else (
                    f"Project trace interpreter: {interpreter}. "
                    if interpreter
                    else "No project interpreter found; trace run falls back to the ArcGraph host. "
                )
                + (
                    structure.reason
                    or f"Python structure uses project interpreter Python {structure.version} with -I -S."
                )
            ),
            "fix": "Use the project .venv, tool.arcgraph.python_environment, or trace run --python.",
        }
    )
    checks.append(
        {
            "name": "project_dependency_environment",
            "status": (
                "pass" if environment.metadata["status"] == "available" else "warn"
            ),
            "message": (
                f"Read-only dependency environment: {environment.metadata['path']} "
                f"({environment.metadata['reason'] or 'available'})."
            ),
            "fix": "Use the project .venv or set tool.arcgraph.python_environment; no tool-host fallback.",
        }
    )
    project = project_minimum_python(repo_root)
    if project is None:
        return checks
    requires, minimum = project
    wanted = f"{minimum[0]}.{minimum[1]}"
    if structure.version is not None or (interpreter and structure.reason):
        checks.append(
            {
                "name": "project_python",
                "status": "warn" if structure.reason else "pass",
                "message": structure.reason
                or (
                    f"The project requires Python {requires}; Python structure uses "
                    f"project interpreter Python {structure.version} with -I -S."
                ),
                **({"fix": STRUCTURE_FIX} if structure.reason else {}),
            }
        )
        return checks
    if running >= minimum:
        checks.append(
            {
                "name": "project_python",
                "status": "pass",
                "message": (
                    f"The project requires Python {requires}; ArcGraph runs on "
                    f"{version}, whose parser reads that syntax."
                ),
            }
        )
        return checks
    check = {
        "name": "project_python",
        "status": "warn",
        "message": (
            f"The project requires Python {requires}; ArcGraph runs on {version}, "
            "whose parser cannot read syntax added after it, so files that use "
            "it are reported as parse errors and left out of the graph."
        ),
    }
    if minimum < high:
        check["fix"] = (
            f"Run ArcGraph in a tool environment with Python {wanted} or later."
        )
    else:
        check["fix"] = (
            f"ArcGraph supports Python up to {newest}; files that use syntax "
            f"added in {wanted} cannot be parsed."
        )
    checks.append(check)
    return checks


def precision_tools_fix() -> str:
    """doctor's repair hint for missing precision tools, per platform.

    Windows has a repository script that installs the whole tool set. macOS and
    Linux install the npm tools and take scip from its official release, which
    is what the README documents for those platforms.
    """

    if platform.system() == "Windows":
        return (
            "Run scripts/install-arcgraph-precision-tools.ps1 to install "
            "scip-python, scip and pyright. Precision tools are optional; "
            "builds work without them."
        )
    return (
        "Install scip-python and pyright with `npm install -g "
        "@sourcegraph/scip-python@0.6.6 pyright@1.1.409`, and scip v0.7.1 from "
        "the official release "
        "(https://github.com/scip-code/scip/releases/tag/v0.7.1). "
        "Precision tools are optional; builds work without them."
    )


def query_engine(args: argparse.Namespace) -> QueryEngine:
    repo_root = Path(args.repo_root).resolve()
    return QueryEngine((repo_root / args.output_dir).resolve())


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def output_location(path: Path, *, named_by_user: bool) -> Path:
    """Where :func:`write_json_output` leaves the content written to *path*.

    A replaced default file stays at *path* even if a symlink stood there,
    so only its directory is resolved.
    """
    if named_by_user:
        return path.resolve()
    return path.parent.resolve() / path.name


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


def handle_export(args):
    """Write the authorized JSONL export into the selected current generation."""
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.operation_lock import arcgraph_operation_lock

    out = (Path(args.repo_root).resolve() / args.output_dir).resolve()
    with arcgraph_operation_lock(out):
        build = GraphStoreReader.from_current(out).export_jsonl()
    return {"build_dir": str(build)}
