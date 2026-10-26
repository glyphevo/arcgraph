"""CLI discovery must not load command-specific runtime dependencies."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "argv",
    [
        None,
        [],
        ["--version"],
        ["--help"],
        ["change", "plan", "--help"],
        ["mcp", "serve", "--help"],
        ["mcp"],
    ],
)
def test_cli_discovery_does_not_import_command_runtimes(argv) -> None:
    script = """
import json, sys
from arcgraph.interfaces import cli
argv = json.loads(sys.argv[1])
if argv is None:
    cli.build_parser()
else:
    try:
        assert cli.main(argv) == 0
    except SystemExit as exc:
        assert exc.code == 0
prefixes = ("mcp", "arcgraph.change", "arcgraph.semantic", "arcgraph.providers",
            "arcgraph.pipeline.indexer", "arcgraph.pipeline.reindexer")
loaded = sorted(name for name in sys.modules
                if any(name == prefix or name.startswith(prefix + ".")
                       for prefix in prefixes))
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(argv)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# These names were explicitly re-exported by cli.py before lazy imports.
REEXPORTS = {
    "arcgraph.interfaces.cli_workspace": [
        "add_benchmark_parser",
        "add_workspace_parser",
        "handle_workspace_status",
        "handle_workspace_resolve",
        "handle_workspace_context",
        "handle_benchmark_agent_startup",
        "handle_benchmark_suite",
    ],
    "arcgraph.interfaces.cli_visual": [
        "add_visual_parser",
        "handle_visual_force",
        "handle_visual_workbench",
        "handle_visual_serve",
        "handle_visual_smoke",
        "_visual_graph_options",
    ],
    "arcgraph.interfaces.cli_support": [
        "output_location",
        "handle_export",
        "precision_tools_fix",
        "print_json",
        "python_checks",
        "query_engine",
        "_emit_cli_payload",
        "write_json_output",
        "_print_cli_error_payload",
    ],
    "arcgraph.interfaces.cli_trial": ["add_trial_parser", "handle_trial_setup"],
    "arcgraph.interfaces.cli_ops": ["handle_sync", "handle_watch"],
    "arcgraph.interfaces.cli_change": [
        "add_change_parser",
        "_change_error_payload",
        "_change_json_object",
        "_change_output_dir",
        "_change_payload",
        "_change_repo_id",
        "_change_repo_id_from_argv",
        "_change_repo_root",
        "_change_service",
        "_change_target",
        "_evidence_summary_payload",
        "handle_change_abandon",
        "handle_change_approve",
        "handle_change_archive",
        "handle_change_audit_export",
        "handle_change_diff",
        "handle_change_evidence_add",
        "handle_change_evidence_list",
        "handle_change_evidence_purge",
        "handle_change_evidence_show",
        "handle_change_list",
        "handle_change_plan",
        "handle_change_reconcile_pins",
        "handle_change_reject",
        "handle_change_report_show",
        "handle_change_show",
        "handle_change_verify",
    ],
}


@pytest.mark.parametrize("module,names", REEXPORTS.items())
def test_cli_command_group_reexports_keep_their_identity(module, names) -> None:
    from arcgraph.interfaces import cli

    source = importlib.import_module(module)
    for name in names:
        namespace = {}
        exec(f"from arcgraph.interfaces.cli import {name}", namespace)
        assert namespace[name] is getattr(source, name)
        assert getattr(cli, name) is getattr(source, name)
