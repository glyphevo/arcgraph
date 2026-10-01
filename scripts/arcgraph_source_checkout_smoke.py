"""Alpha source-checkout smoke for ArcGraph."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCGRAPH_WRAPPER = REPO_ROOT / "scripts" / "arcgraph.py"
SCHEMA_VERSION = "1.0.0"
INDEX_PAYLOAD_SCHEMA_VERSION = "1.0.0"
READ_PAYLOAD_SCHEMA_VERSION = "1.4.0"
SMOKE_TARGET = "pkg.service.Greeter.greet"

repo_root_path = str(REPO_ROOT)
sys.path[:] = [entry for entry in sys.path if entry != repo_root_path]
sys.path.insert(0, repo_root_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the alpha ArcGraph source-checkout smoke in a temporary "
            "project. Generated output is cleaned up automatically."
        )
    )
    parser.add_argument(
        "--skip-ci",
        action="store_true",
        help="Skip `arcgraph ci` when a faster smoke is needed.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary smoke project for debugging.",
    )
    args = parser.parse_args(argv)

    if args.keep_temp:
        temp_path = Path(tempfile.mkdtemp(prefix="arcgraph-source-checkout-smoke-"))
        result = _run_smoke(temp_path, skip_ci=args.skip_ci)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    with tempfile.TemporaryDirectory(prefix="arcgraph-source-checkout-smoke-") as temp:
        result = _run_smoke(Path(temp), skip_ci=args.skip_ci)
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_smoke(project: Path, *, skip_ci: bool) -> dict[str, Any]:
    _write_sample_project(project)
    output_dir = project / "output" / "arcgraph"
    commands: list[dict[str, Any]] = []

    _run_text(["--help"], commands=commands)
    _run_json(
        ["--repo-root", str(project), "--output-dir", str(output_dir), "doctor"],
        commands=commands,
    )
    _run_json(
        [
            "--repo-root",
            str(project),
            "--output-dir",
            str(output_dir),
            "init",
            "--dry-run",
        ],
        commands=commands,
    )
    _run_json(
        ["--repo-root", str(project), "--output-dir", str(output_dir), "build"],
        commands=commands,
    )
    current = _run_json(
        ["--repo-root", str(project), "--output-dir", str(output_dir), "current"],
        commands=commands,
    )
    status = _run_json(
        ["--repo-root", str(project), "--output-dir", str(output_dir), "status"],
        commands=commands,
    )
    _run_json(
        ["--repo-root", str(project), "--output-dir", str(output_dir), "stats"],
        commands=commands,
    )
    context = _run_json(
        [
            "--repo-root",
            str(project),
            "--output-dir",
            str(output_dir),
            "context",
            SMOKE_TARGET,
            "--detail-level",
            "summary",
        ],
        commands=commands,
    )
    explain = _run_json(
        [
            "--repo-root",
            str(project),
            "--output-dir",
            str(output_dir),
            "explain",
            SMOKE_TARGET,
            "--detail-level",
            "summary",
        ],
        commands=commands,
    )
    if not skip_ci:
        _run_json(
            ["--repo-root", str(project), "--output-dir", str(output_dir), "ci"],
            commands=commands,
        )
    _run_text(["docs", "agent-cli-contract"], commands=commands)
    _run_text(["docs", "source-checkout-smoke"], commands=commands)
    _run_text(["docs", "mcp-server"], commands=commands)
    _run_text(["mcp", "--help"], commands=commands)
    _run_text(["mcp", "serve", "--help"], commands=commands)

    mcp_summary = _mcp_direct_smoke(project, output_dir)
    _assert_payload_contract(current, "current")
    _assert_payload_contract(status, "status")
    _assert_payload_contract(context, "context")
    _assert_payload_contract(explain, "explain")

    return {
        "status": "pass",
        "schema_version": SCHEMA_VERSION,
        "project": str(project),
        "output_dir": str(output_dir),
        "commands": commands,
        "mcp": mcp_summary,
        "warnings": {
            "generated_output": "temporary output was created under the smoke project",
            "package_channels": "public package publishing remains unapproved",
        },
    }


def _write_sample_project(project: Path) -> None:
    package = project / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text(
        """
class Greeter:
    def greet(self, name: str) -> str:
        return f"hello {name}"


def make_greeter() -> Greeter:
    return Greeter()


def entry(name: str) -> str:
    greeter = make_greeter()
    return greeter.greet(name)
""".lstrip(),
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        """
[project]
name = "arcgraph-smoke-project"
version = "0.0.0"

[tool.arcgraph]
source_roots = ["src"]
""".lstrip(),
        encoding="utf-8",
    )


def _run_json(args: list[str], *, commands: list[dict[str, Any]]) -> dict[str, Any]:
    completed = _run_arcgraph(args)
    payload = json.loads(completed.stdout)
    commands.append(
        {
            "command": ["python", "scripts/arcgraph.py", *args],
            "exit_code": completed.returncode,
            "json": True,
        }
    )
    return payload


def _run_text(args: list[str], *, commands: list[dict[str, Any]]) -> str:
    completed = _run_arcgraph(args)
    commands.append(
        {
            "command": ["python", "scripts/arcgraph.py", *args],
            "exit_code": completed.returncode,
            "json": False,
        }
    )
    return completed.stdout


def _run_arcgraph(args: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(ARCGRAPH_WRAPPER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"arcgraph {' '.join(args)} failed with exit {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout}"
        )
    return completed


def _assert_payload_contract(payload: dict[str, Any], command: str) -> None:
    read_command = command in {"context", "explain"}
    expected_schema = (
        READ_PAYLOAD_SCHEMA_VERSION if read_command else INDEX_PAYLOAD_SCHEMA_VERSION
    )
    if payload.get("schema_version") != expected_schema:
        raise RuntimeError(f"{command} returned unsupported schema_version.")
    if "status" not in payload:
        raise RuntimeError(f"{command} did not include status.")
    if "warnings" not in payload:
        raise RuntimeError(f"{command} did not include warnings.")
    if read_command:
        if payload.get("index_schema_version") != INDEX_PAYLOAD_SCHEMA_VERSION:
            raise RuntimeError(f"{command} returned unsupported index_schema_version.")
        if "freshness" not in payload or "truncation" not in payload:
            raise RuntimeError(f"{command} did not include freshness/truncation.")
        source_snippets = payload.get("source_snippets")
        if isinstance(source_snippets, dict):
            snippets_enabled = source_snippets.get("enabled") is True
        else:
            snippets_enabled = source_snippets not in ({}, None)
        if snippets_enabled:
            raise RuntimeError(f"{command} unexpectedly exposed source snippets.")


def _mcp_direct_smoke(project: Path, output_dir: Path) -> dict[str, Any]:
    from arcgraph.interfaces.mcp_server import create_tool_group
    from arcgraph.interfaces.mcp_tools import ArcGraphPermissionError

    tools = create_tool_group(
        repo_root=project,
        output_dir=output_dir,
        repo_id="smoke",
    )
    status = tools.arcgraph_index_status(repo_id="smoke")
    context = tools.arcgraph_get_context(repo_id="smoke", targets=[SMOKE_TARGET])
    explain = tools.arcgraph_explain(repo_id="smoke", targets=[SMOKE_TARGET])
    risk = tools.arcgraph_get_risk(repo_id="smoke", targets=[SMOKE_TARGET])
    source_request = tools.arcgraph_get_context(
        repo_id="smoke",
        targets=[SMOKE_TARGET],
        include_source=True,
    )
    try:
        tools.arcgraph_get_context(repo_id="smoke", targets=["..\\outside\\secret.py"])
    except ArcGraphPermissionError:
        path_escape_rejected = True
    else:
        path_escape_rejected = False

    if status.get("status") != "available":
        raise RuntimeError("MCP status did not report available index.")
    for name, payload in {"context": context, "explain": explain, "risk": risk}.items():
        if payload.get("read_only") is not True:
            raise RuntimeError(f"MCP {name} payload was not read-only.")
        if payload.get("source_snippets", {}).get("enabled") is not False:
            raise RuntimeError(f"MCP {name} enabled source snippets by default.")
    if not path_escape_rejected:
        raise RuntimeError("MCP path escape guard did not reject outside path.")

    return {
        "status": status["status"],
        "repo_id": context["repo_id"],
        "read_only": context["read_only"],
        "source_snippets": source_request["source_snippets"],
        "path_escape_rejected": path_escape_rejected,
    }


if __name__ == "__main__":
    raise SystemExit(main())
