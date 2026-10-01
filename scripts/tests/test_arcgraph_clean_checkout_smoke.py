from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.schemas import READ_SCHEMA_VERSION
from arcgraph.core.schemas import SCHEMA_VERSION as INDEX_SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_clean_checkout_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "arcgraph_clean_checkout_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "arcgraph_clean_checkout_smoke", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_source_checkout_smoke_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "arcgraph_source_checkout_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "arcgraph_source_checkout_smoke", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_clean_checkout_smoke_script_is_syntax_valid() -> None:
    py_compile.compile(
        str(REPO_ROOT / "scripts" / "arcgraph_clean_checkout_smoke.py"),
        doraise=True,
    )


def test_clean_checkout_smoke_dry_run_plans_alpha_matrix(
    capsys: Any,
) -> None:
    smoke = load_clean_checkout_module()

    exit_code = smoke.main(["--dry-run", "--quick"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    command_names = [command["name"] for command in payload["commands"]]
    assert exit_code == 0
    assert payload["status"] == "planned"
    assert payload["install_mode"] == "source-checkout-editable"
    assert payload["mcp_extra_installed"] is True
    assert payload["quick"] is True
    assert "install-editable" in command_names
    assert "install-editable-mcp-extra" in command_names
    assert "docs-mcp-server" in command_names
    assert "mcp-serve-help" in command_names
    assert "mcp-module-help" in command_names
    assert "source-checkout-smoke" in command_names
    private_smoke = next(
        command
        for command in payload["commands"]
        if command["name"] == "source-checkout-smoke"
    )
    assert "--skip-ci" in private_smoke["command"]
    assert (
        "source_checkout_editable_install_with_mcp_extra"
        in payload["matrix"]["executed"]
    )
    assert "inner_source_checkout_smoke_arcgraph_ci" in payload["matrix"]["skipped"]
    assert "wheel_or_sdist_install_package_readiness" in payload["matrix"]["deferred"]
    assert "automatic_agent_configuration_installer" in payload["matrix"]["deferred"]
    assert "http_or_network_mcp_transport" in payload["matrix"]["deferred"]
    assert "public_release_or_package_publishing" in payload["matrix"]["deferred"]


def test_clean_checkout_smoke_dry_run_can_skip_mcp_extra(capsys: Any) -> None:
    smoke = load_clean_checkout_module()

    exit_code = smoke.main(["--dry-run", "--skip-mcp-extra"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    command_names = [command["name"] for command in payload["commands"]]
    assert exit_code == 0
    assert payload["status"] == "planned"
    assert payload["mcp_extra_installed"] is False
    assert "install-editable-mcp-extra" not in command_names
    assert (
        "source_checkout_editable_install_with_mcp_extra"
        in payload["matrix"]["skipped"]
    )


def test_clean_checkout_smoke_origin_mode_records_remote_matrix(capsys: Any) -> None:
    smoke = load_clean_checkout_module()

    exit_code = smoke.main(["--dry-run", "--clone-source", "origin"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert "origin_clean_checkout_from_git_commit" in payload["matrix"]["executed"]
    assert "remote_origin_clone" not in payload["matrix"]["deferred"]


def test_clean_checkout_smoke_tail_tolerates_missing_stream() -> None:
    smoke = load_clean_checkout_module()

    assert smoke._tail(None) == ""


def test_source_checkout_smoke_prefers_repo_root_over_scripts_path() -> None:
    original_path = list(sys.path)
    try:
        smoke = load_source_checkout_smoke_module()

        assert sys.path[0] == str(smoke.REPO_ROOT)
    finally:
        sys.path[:] = original_path


def test_source_checkout_smoke_distinguishes_read_and_index_schemas() -> None:
    smoke = load_source_checkout_smoke_module()
    index_payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "status": "available",
        "warnings": [],
    }
    read_payload = {
        "schema_version": READ_SCHEMA_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "status": "pass",
        "warnings": [],
        "freshness": {"status": "fresh"},
        "truncation": {"truncated": False},
        "source_snippets": {"enabled": False},
    }

    assert smoke.INDEX_PAYLOAD_SCHEMA_VERSION == INDEX_SCHEMA_VERSION
    assert smoke.READ_PAYLOAD_SCHEMA_VERSION == READ_SCHEMA_VERSION
    smoke._assert_payload_contract(index_payload, "current")
    smoke._assert_payload_contract(index_payload, "status")
    smoke._assert_payload_contract(read_payload, "context")
    smoke._assert_payload_contract(read_payload, "explain")

    with pytest.raises(RuntimeError, match="unsupported schema_version"):
        smoke._assert_payload_contract(index_payload, "context")
    with pytest.raises(RuntimeError, match="unsupported index_schema_version"):
        smoke._assert_payload_contract(
            {**read_payload, "index_schema_version": "0.9.0"},
            "explain",
        )
