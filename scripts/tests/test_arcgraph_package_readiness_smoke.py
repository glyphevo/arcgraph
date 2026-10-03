from __future__ import annotations

import importlib.util
import io
import json
import py_compile
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Callable
import zipfile

import pytest

from arcgraph.core.schemas import READ_SCHEMA_VERSION
from arcgraph.core.schemas import SCHEMA_VERSION as INDEX_SCHEMA_VERSION
from arcgraph.interfaces.agent_capabilities import mcp_capability_names

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_package_smoke_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "arcgraph_package_readiness_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "arcgraph_package_readiness_smoke", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_package_readiness_smoke_script_is_syntax_valid() -> None:
    py_compile.compile(
        str(REPO_ROOT / "scripts" / "arcgraph_package_readiness_smoke.py"),
        doraise=True,
    )


def test_package_readiness_smoke_dry_run_plans_package_gate(
    capsys: Any,
) -> None:
    smoke = load_package_smoke_module()

    exit_code = smoke.main(["--dry-run"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    command_names = [command["name"] for command in payload["commands"]]
    commands_by_name = {
        command["name"]: command["command"] for command in payload["commands"]
    }
    assert exit_code == 0
    assert payload["status"] == "planned"
    assert payload["install_mode"] == "built-wheel-temporary-venv"
    assert payload["project_metadata"]["name"] == "arcgraph"
    assert payload["project_metadata"]["version"] == "0.1.0rc8"
    assert payload["project_metadata"]["console_script"] == (
        "arcgraph.interfaces.cli:main"
    )
    assert payload["project_metadata"]["has_mcp_extra"] is True
    assert payload["package_json"]["private"] is True
    assert payload["package_json"]["has_publish_script"] is False
    assert payload["source_provenance"]["head_sha"]
    assert payload["source_provenance"]["tree_sha"]
    assert len(payload["source_provenance"]["working_tree_status_sha256"]) == 64
    assert payload["mcp_extra_installed"] is True
    assert payload["mcp_version_spec"] == "mcp>=2.0.0,<3.0.0"
    assert "build-package" in command_names
    assert "install-wheel" in command_names
    assert "install-wheel-mcp-extra" in command_names
    assert commands_by_name["install-wheel-mcp-extra"][-1] == ("mcp>=2.0.0,<3.0.0")
    assert "arcgraph-version" in command_names
    assert "arcgraph-version-provenance" in command_names
    assert "agent-help-overview" in command_names
    assert "docs-package-readiness" in command_names
    assert "mcp-serve-help" in command_names
    assert "mcp-module-help" in command_names
    assert "sample-doctor" in command_names
    assert "sample-trial-setup-dry-run" in command_names
    assert "sample-init-dry-run" in command_names
    assert "sample-build" in command_names
    assert "sample-context" in command_names
    assert "sample-explain" in command_names
    assert "sample-ci" in command_names
    assert "typescript-build-without-runtime" in command_names
    assert "installed-mcp-version" in command_names
    assert "create-mcp-v1-client-venv" in command_names
    assert "install-mcp-v1-client" in command_names
    assert "mcp-v2-auto-protocol" in command_names
    assert "mcp-v2-legacy-protocol" in command_names
    assert "mcp-v1-client-protocol" in command_names
    assert "check-wheel-contents" in command_names
    assert "check-sdist-contents" in command_names
    assert "check-package-json-private" in command_names
    assert "local_wheel_and_sdist_build" in payload["matrix"]["executed"]
    assert "wheel_install_in_temporary_venv" in payload["matrix"]["executed"]
    assert "installed_trial_setup_dry_run" in payload["matrix"]["executed"]
    assert "wheel_mcp_extra_install" in payload["matrix"]["executed"]
    assert "sample_repo_arcgraph_ci" in payload["matrix"]["executed"]
    assert "typescript_runtime_degradation" in payload["matrix"]["executed"]
    assert "mcp_v2_auto_protocol_handshake" in payload["matrix"]["executed"]
    assert "mcp_v2_legacy_protocol_handshake" in payload["matrix"]["executed"]
    assert "mcp_v1_client_protocol_handshake" in payload["matrix"]["executed"]
    assert "mcp_exact_tool_surface_and_calls" in payload["matrix"]["executed"]
    assert "mcp_clean_process_shutdown" in payload["matrix"]["executed"]
    assert "full_mcp_protocol_client_handshake" not in payload["matrix"]["deferred"]
    assert "pypi_publishing" in payload["matrix"]["deferred"]
    assert "npm_package_publishing" in payload["matrix"]["deferred"]
    assert "docker_or_ghcr_image" in payload["matrix"]["deferred"]
    assert "github_release_or_tag" in payload["matrix"]["deferred"]
    assert "public_repo_visibility" in payload["matrix"]["deferred"]
    assert all(
        "scripts/arcgraph.py" not in " ".join(command)
        for command in commands_by_name.values()
    )
    server_python = commands_by_name["mcp-v2-auto-protocol"]
    server_python = server_python[server_python.index("--server-python") + 1]
    assert server_python.endswith("venv/bin/python") or server_python.endswith(
        r"venv\Scripts\python.exe"
    )
    assert commands_by_name["mcp-v2-auto-protocol"][0] == server_python
    assert commands_by_name["mcp-v2-legacy-protocol"][0] == server_python
    assert commands_by_name["mcp-v1-client-protocol"][0] != server_python
    assert commands_by_name["install-mcp-v1-client"][-1] == "mcp==1.28.1"
    trial_setup = commands_by_name["sample-trial-setup-dry-run"]
    assert trial_setup[trial_setup.index("trial") :] == [
        "trial",
        "setup",
        "--client",
        "claude",
        "--dry-run",
    ]
    expected_protocols = {
        "mcp-v2-auto-protocol": ("auto", "2026-07-28"),
        "mcp-v2-legacy-protocol": ("legacy", "2025-11-25"),
        "mcp-v1-client-protocol": ("legacy", "2025-11-25"),
    }
    for name, (mode, protocol) in expected_protocols.items():
        command = commands_by_name[name]
        assert command[command.index("--mode") + 1] == mode
        assert command[command.index("--expect-protocol") + 1] == protocol


def test_package_readiness_smoke_dry_run_binds_requested_mcp_line(
    capsys: Any,
) -> None:
    smoke = load_package_smoke_module()

    exit_code = smoke.main(["--dry-run", "--mcp-version-spec", "mcp==2.0.0"])

    payload = json.loads(capsys.readouterr().out)
    install = next(
        command
        for command in payload["commands"]
        if command["name"] == "install-wheel-mcp-extra"
    )
    assert exit_code == 0
    assert payload["mcp_version_spec"] == "mcp==2.0.0"
    assert install["command"][-1] == "mcp==2.0.0"


def test_package_readiness_smoke_dry_run_plans_artifact_persistence(
    tmp_path: Path,
    capsys: Any,
) -> None:
    smoke = load_package_smoke_module()
    target = tmp_path / "candidate-artifacts"

    exit_code = smoke.main(["--dry-run", "--artifact-dir", str(target)])

    payload = json.loads(capsys.readouterr().out)
    persist = next(
        command
        for command in payload["commands"]
        if command["name"] == "persist-package-artifacts"
    )
    assert exit_code == 0
    assert payload["artifact_dir"] == str(target.resolve())
    assert persist["command"][-1] == str(target)


def test_package_readiness_smoke_persists_exact_artifacts_atomically(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    source = tmp_path / "source"
    source.mkdir()
    wheel = source / "arcgraph-0.1.0rc6-py3-none-any.whl"
    sdist = source / "arcgraph-0.1.0rc6.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    target = tmp_path / "candidate" / "artifacts"
    expected = {
        "wheel": wheel.name,
        "wheel_sha256": smoke._sha256_file(wheel),
        "wheel_size": wheel.stat().st_size,
        "sdist": sdist.name,
        "sdist_sha256": smoke._sha256_file(sdist),
        "sdist_size": sdist.stat().st_size,
    }

    result = smoke._persist_package_artifacts(
        wheel=wheel,
        sdist=sdist,
        artifact_dir=target,
        repo_root=tmp_path / "unrelated-repo",
        expected=expected,
    )

    assert result == {"status": "pass", "directory": str(target), **expected}
    assert (target / wheel.name).read_bytes() == b"wheel"
    assert (target / sdist.name).read_bytes() == b"sdist"
    assert not any(
        path.name.startswith(".artifacts.") for path in target.parent.iterdir()
    )
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        smoke._persist_package_artifacts(
            wheel=wheel,
            sdist=sdist,
            artifact_dir=target,
            repo_root=tmp_path / "unrelated-repo",
            expected=expected,
        )


def test_package_readiness_refuses_a_dangling_artifact_symlink(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    wheel = tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl"
    sdist = tmp_path / "arcgraph-0.1.0rc6.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    target = tmp_path / "artifacts"
    try:
        target.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("This platform does not permit symlink creation.")

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        smoke._persist_package_artifacts(
            wheel=wheel,
            sdist=sdist,
            artifact_dir=target,
            repo_root=tmp_path / "unrelated-repo",
            expected={},
        )


def test_package_readiness_smoke_dry_run_can_skip_optional_paths(
    capsys: Any,
) -> None:
    smoke = load_package_smoke_module()

    exit_code = smoke.main(["--dry-run", "--skip-mcp-extra", "--skip-ci"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    command_names = [command["name"] for command in payload["commands"]]
    assert exit_code == 0
    assert payload["status"] == "planned"
    assert payload["mcp_extra_installed"] is False
    assert payload["skip_ci"] is True
    assert "install-wheel-mcp-extra" not in command_names
    assert "installed-mcp-version" not in command_names
    assert "create-mcp-v1-client-venv" not in command_names
    assert "install-mcp-v1-client" not in command_names
    assert "mcp-v2-auto-protocol" not in command_names
    assert "mcp-v2-legacy-protocol" not in command_names
    assert "mcp-v1-client-protocol" not in command_names
    assert "sample-ci" not in command_names
    assert "wheel_mcp_extra_install" in payload["matrix"]["skipped"]
    assert "mcp_v2_auto_protocol_handshake" in payload["matrix"]["skipped"]
    assert "mcp_v1_client_protocol_handshake" in payload["matrix"]["skipped"]
    assert "sample_repo_arcgraph_ci" in payload["matrix"]["skipped"]


def test_package_readiness_smoke_payload_tail_tolerates_missing_stream() -> None:
    smoke = load_package_smoke_module()

    assert smoke._tail(None) == ""


def test_package_readiness_smoke_validates_installed_version() -> None:
    smoke = load_package_smoke_module()

    smoke._validate_installed_version("arcgraph 0.1.0rc6\n", expected="0.1.0rc6")
    with pytest.raises(RuntimeError, match="--version"):
        smoke._validate_installed_version(
            "arcgraph 0.1.0rc1\n",
            expected="0.1.0rc6",
        )


def test_package_readiness_smoke_validates_installed_trial_setup(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    sample_repo = tmp_path / "sample"
    state = sample_repo / ".arcgraph-trial"
    executable = tmp_path / "venv" / "bin" / "arcgraph"
    serve = [
        str(executable.resolve()),
        "mcp",
        "serve",
        "--repo-root",
        str(sample_repo.resolve()),
        "--output-dir",
        str(state.resolve() / "index"),
    ]
    payload = {
        "status": "ready",
        "action": "dry_run",
        "claude_config_modified": False,
        "checks": [
            {"name": "repo_root", "status": "pass"},
            {
                "name": "arcgraph_executable",
                "status": "pass",
                "selection_source": "invocation",
                "value": str(executable.resolve()),
            },
        ],
        "repo_root": str(sample_repo.resolve()),
        "paths": {
            "state_root": str(state.resolve()),
            "output_dir": str(state.resolve() / "index"),
            "metrics_log": str(state.resolve() / "metrics" / "mcp.jsonl"),
            "feedback_log": str(state.resolve() / "feedback" / "agent.jsonl"),
            "git_exclude": str(sample_repo.resolve() / ".git" / "info" / "exclude"),
        },
        "serve_command": serve,
        "registration_command": [
            "claude",
            "mcp",
            "add",
            "--scope",
            "local",
            "arcgraph",
            "--",
            *serve,
        ],
    }

    trial_home = tmp_path / "trial-home"
    trial_home.mkdir()
    (sample_repo / ".git" / "info").mkdir(parents=True)
    exclude = sample_repo / ".git" / "info" / "exclude"
    exclude.write_text(smoke.SAMPLE_GIT_EXCLUDE, encoding="utf-8")

    def validate() -> None:
        smoke._validate_installed_trial_setup(
            dict(payload),
            sample_repo=sample_repo,
            trial_home=trial_home,
            expected_executable=executable,
        )

    validate()

    # Each guard gets its own refutation: a guard no test can break is a guard
    # that can be deleted with the suite still green.
    broken = dict(payload)
    broken["registration_command"] = payload["registration_command"][:-1]
    with pytest.raises(RuntimeError, match="does not wrap"):
        smoke._validate_installed_trial_setup(
            broken,
            sample_repo=sample_repo,
            trial_home=trial_home,
            expected_executable=executable,
        )

    with pytest.raises(RuntimeError, match="does not wrap"):
        smoke._validate_installed_trial_setup(
            dict(payload),
            sample_repo=sample_repo,
            trial_home=trial_home,
            expected_executable=tmp_path / "elsewhere" / "bin" / "arcgraph",
        )

    fallback = dict(payload)
    fallback["checks"] = [
        *payload["checks"][:-1],
        {
            **payload["checks"][-1],
            "status": "warn",
            "selection_source": "interpreter_scheme",
        },
    ]
    with pytest.raises(RuntimeError, match="distribution RECORD"):
        smoke._validate_installed_trial_setup(
            fallback,
            sample_repo=sample_repo,
            trial_home=trial_home,
            expected_executable=executable,
        )

    stray = trial_home / ".claude.json"
    stray.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrote client configuration"):
        validate()
    stray.unlink()

    exclude.write_text(
        smoke.SAMPLE_GIT_EXCLUDE + ".arcgraph-trial/\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match=r"modified \.git/info/exclude"):
        validate()


def test_package_readiness_smoke_validates_installed_version_provenance() -> None:
    smoke = load_package_smoke_module()
    wheel_sha256 = "a" * 64
    payload = {
        "product_version": "0.1.0rc6",
        "execution_mode": "installed_distribution",
        "provenance_status": "verified_artifact_hash",
        "artifact_provenance": {"sha256": wheel_sha256},
        "build_provenance": {
            "build_version": "0.1.0rc6",
            "source": {"commit_sha": "c" * 40},
        },
    }

    smoke._validate_installed_version_provenance(
        payload,
        expected_version="0.1.0rc6",
        expected_wheel_sha256=wheel_sha256,
        expected_commit_sha="c" * 40,
    )
    with pytest.raises(RuntimeError, match="artifact SHA-256"):
        smoke._validate_installed_version_provenance(
            payload,
            expected_version="0.1.0rc6",
            expected_wheel_sha256="b" * 64,
            expected_commit_sha="c" * 40,
        )


def test_package_readiness_smoke_validates_installed_agent_help() -> None:
    smoke = load_package_smoke_module()
    payload = {
        "status": "available",
        "topic": "overview",
        "product_version": "0.1.0rc6",
        "inventory": {
            "mcp_tools": ["arcgraph_index_status", "arcgraph_help"],
        },
        "feedback": {
            "cli_available": True,
            "mcp_enabled": False,
            "tool_registered": False,
        },
    }

    smoke._validate_installed_agent_help(payload, expected_version="0.1.0rc6")
    payload["inventory"]["mcp_tools"].append("arcgraph_record_trial_feedback")
    with pytest.raises(RuntimeError, match="feedback was registered"):
        smoke._validate_installed_agent_help(payload, expected_version="0.1.0rc6")


def _protocol_payload(
    *,
    client_version: str,
    client_major: int,
    mode: str,
    protocol: str,
    package_version: str = "0.1.0rc6",
    contract_sha: str = "a" * 64,
) -> dict[str, Any]:
    names = list(mcp_capability_names())
    return {
        "status": "pass",
        "clean_shutdown": True,
        "client": {
            "mcp_version": client_version,
            "major": client_major,
            "mode": mode,
        },
        "protocol_version": protocol,
        "server": {"name": "ArcGraph", "version": package_version},
        "tool_contract": {
            "tool_count": len(names),
            "tool_names": names,
            "sha256": contract_sha,
        },
        "calls": [{"name": name} for name in names],
        "server_stderr_empty": True,
        "server_stderr_tail": "",
    }


def test_package_readiness_smoke_validates_exact_protocol_matrix() -> None:
    smoke = load_package_smoke_module()
    payloads = {
        "mcp-v2-auto-protocol": _protocol_payload(
            client_version="2.0.0",
            client_major=2,
            mode="auto",
            protocol="2026-07-28",
        ),
        "mcp-v2-legacy-protocol": _protocol_payload(
            client_version="2.0.0",
            client_major=2,
            mode="legacy",
            protocol="2025-11-25",
        ),
        "mcp-v1-client-protocol": _protocol_payload(
            client_version="1.28.1",
            client_major=1,
            mode="legacy",
            protocol="2025-11-25",
        ),
    }

    result = smoke._validate_mcp_protocol_matrix(
        payloads,
        installed_mcp_version="2.0.0",
        requested_mcp_spec="mcp==2.0.0",
        package_version="0.1.0rc6",
    )

    assert result["status"] == "pass"
    assert result["server_mcp_version"] == "2.0.0"
    assert result["tool_contract_sha256"] == "a" * 64
    assert result["tool_names"] == list(smoke.EXPECTED_DEFAULT_TOOL_NAMES)
    assert set(result["clients"]) == set(payloads)

    payloads["mcp-v2-auto-protocol"]["tool_contract"]["tool_names"][
        0
    ] = "arcgraph_unexpected_replacement"
    payloads["mcp-v2-auto-protocol"]["calls"][0][
        "name"
    ] = "arcgraph_unexpected_replacement"
    with pytest.raises(RuntimeError, match="exact default MCP tool contract"):
        smoke._validate_mcp_protocol_matrix(
            payloads,
            installed_mcp_version="2.0.0",
            requested_mcp_spec="mcp==2.0.0",
            package_version="0.1.0rc6",
        )

    with pytest.raises(RuntimeError, match="requested exact"):
        smoke._validate_mcp_protocol_matrix(
            payloads,
            installed_mcp_version="2.1.0",
            requested_mcp_spec="mcp==2.0.0",
            package_version="0.1.0rc6",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("clean_shutdown", False), "clean shutdown"),
        (("server_stderr_empty", False), "server stderr"),
        (("protocol_version", "wrong"), "protocol version"),
    ],
)
def test_package_readiness_smoke_rejects_incomplete_protocol_evidence(
    mutation: tuple[str, Any],
    message: str,
) -> None:
    smoke = load_package_smoke_module()
    payloads = {
        "mcp-v2-auto-protocol": _protocol_payload(
            client_version="2.0.0",
            client_major=2,
            mode="auto",
            protocol="2026-07-28",
        ),
        "mcp-v2-legacy-protocol": _protocol_payload(
            client_version="2.0.0",
            client_major=2,
            mode="legacy",
            protocol="2025-11-25",
        ),
        "mcp-v1-client-protocol": _protocol_payload(
            client_version="1.28.1",
            client_major=1,
            mode="legacy",
            protocol="2025-11-25",
        ),
    }
    payloads["mcp-v2-auto-protocol"][mutation[0]] = mutation[1]

    with pytest.raises(RuntimeError, match=message):
        smoke._validate_mcp_protocol_matrix(
            payloads,
            installed_mcp_version="2.0.0",
            requested_mcp_spec="mcp==2.0.0",
            package_version="0.1.0rc6",
        )


def test_package_commands_clear_source_and_node_module_escape_paths(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    smoke = load_package_smoke_module()
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("NODE_PATH", str(REPO_ROOT / "node_modules"))
    commands: list[dict[str, Any]] = []
    script = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) for key in "
        "['PYTHONPATH', 'NODE_PATH', 'ARCGRAPH_SMOKE_MARKER']}))"
    )

    completed = smoke._run_checked(
        smoke.CommandSpec(
            "environment-isolation",
            [sys.executable, "-c", script],
            tmp_path,
            environment=(("ARCGRAPH_SMOKE_MARKER", "present"),),
        ),
        commands,
    )

    assert json.loads(completed.stdout) == {
        "PYTHONPATH": None,
        "NODE_PATH": None,
        "ARCGRAPH_SMOKE_MARKER": "present",
    }
    assert commands[0]["cleared_environment"] == ["PYTHONPATH", "NODE_PATH"]
    assert commands[0]["environment"] == {"ARCGRAPH_SMOKE_MARKER": "present"}


def test_typescript_degradation_requires_both_durable_artifacts(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    output_dir = tmp_path / "output"
    build_dir = output_dir / "builds" / "build-1"
    build_dir.mkdir(parents=True)
    warning = {
        "kind": "typescript_frontend_unavailable",
        "message": "compiler unavailable",
    }
    diagnostic = {
        "diagnostic_kind": "typescript_frontend_unavailable",
        "message": "compiler unavailable",
    }
    (build_dir / "summary.json").write_text(
        json.dumps({"warnings": [warning]}),
        encoding="utf-8",
    )
    (build_dir / "diagnostics.jsonl").write_text(
        json.dumps(diagnostic) + "\n",
        encoding="utf-8",
    )

    result = smoke._validate_typescript_degradation(
        build_payload={"build_dir": str(build_dir)},
        output_dir=output_dir,
        project_node_modules=tmp_path / "typescript-repo" / "node_modules",
    )

    assert result["status"] == "pass"
    assert result["summary_warning_count"] == 1
    assert result["diagnostic_count"] == 1
    assert result["project_node_modules_present"] is False

    (build_dir / "diagnostics.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="diagnostics.jsonl"):
        smoke._validate_typescript_degradation(
            build_payload={"build_dir": str(build_dir)},
            output_dir=output_dir,
            project_node_modules=tmp_path / "typescript-repo" / "node_modules",
        )


def test_typescript_degradation_command_forces_the_real_ts_source_root(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    repo = tmp_path / "typescript-repo"
    smoke._write_typescript_project(repo)

    spec = smoke.build_typescript_degradation_command(
        arcgraph=tmp_path / "venv" / "bin" / "arcgraph",
        typescript_repo=repo,
        output_dir=repo / "output" / "arcgraph",
        timeout_seconds=17,
    )

    assert spec.cwd == repo
    assert spec.command[-2:] == ["--root", "src"]
    assert spec.environment == (("NODE_OPTIONS", "--no-global-search-paths"),)
    assert (repo / "src" / "index.ts").exists()
    assert not (repo / "node_modules").exists()


def test_package_readiness_smoke_distinguishes_read_and_index_schemas() -> None:
    smoke = load_package_smoke_module()
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
    smoke._validate_payload_contract(index_payload, "current")
    smoke._validate_payload_contract(index_payload, "status")
    smoke._validate_payload_contract(read_payload, "context")
    smoke._validate_payload_contract(read_payload, "explain")

    with pytest.raises(RuntimeError, match="unsupported schema_version"):
        smoke._validate_payload_contract(index_payload, "context")
    with pytest.raises(RuntimeError, match="unsupported index_schema_version"):
        smoke._validate_payload_contract(
            {**read_payload, "index_schema_version": "0.9.0"},
            "context",
        )


def test_package_readiness_smoke_rejects_dirty_source_before_build(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    smoke = load_package_smoke_module()
    monkeypatch.setattr(
        smoke,
        "_source_provenance",
        lambda repo: {
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "working_tree_clean": False,
            "working_tree_status_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        smoke,
        "_read_project_metadata",
        lambda repo: {
            "name": "arcgraph",
            "version": "0.1.0rc6",
            "requires_python": ">=3.11",
            "console_script": "arcgraph.interfaces.cli:main",
            "has_mcp_extra": True,
            "build_backend": "hatchling.build",
        },
    )
    monkeypatch.setattr(
        smoke,
        "_read_package_json",
        lambda repo: {
            "name": "arcgraph-workbench-assets",
            "version": "0.1.0",
            "private": True,
            "has_publish_script": False,
        },
    )

    payload = smoke.run_package_readiness_smoke(repo_root=tmp_path)

    assert payload["status"] == "fail"
    assert payload["commands"] == []
    assert "clean working tree" in payload["failures"][0]["message"]


def test_source_provenance_reads_real_git_state(tmp_path: Path) -> None:
    smoke = load_package_smoke_module()
    repo = tmp_path / "repo"
    (repo / "arcgraph").mkdir(parents=True)
    (repo / "arcgraph" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ArcGraph Tests"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    clean = smoke._source_provenance(repo)
    (repo / "arcgraph" / "__init__.py").write_text("# changed\n", encoding="utf-8")
    dirty = smoke._source_provenance(repo)

    assert clean["working_tree_clean"] is True
    assert clean["tracked_package_files"] == ["arcgraph/__init__.py"]
    assert clean["tracked_files"] == ["arcgraph/__init__.py"]
    assert clean["tracked_symlinks"] == []
    assert dirty["working_tree_clean"] is False
    assert dirty["working_tree_status_sha256"] != clean["working_tree_status_sha256"]


DIST_INFO = "arcgraph-0.1.0rc7.dist-info"
METADATA_TEXT = (
    "Metadata-Version: 2.5\nName: arcgraph\nVersion: 0.1.0rc7\n"
    "License-File: LICENSE\n"
)


def _wheel_files(smoke: Any, metadata_text: str = METADATA_TEXT) -> dict[str, bytes]:
    files = {name: b"" for name in smoke.REQUIRED_WHEEL_FILES}
    for name in ("WHEEL", "entry_points.txt"):
        files[f"{DIST_INFO}/{name}"] = name.encode()
    files[f"{DIST_INFO}/METADATA"] = metadata_text.encode()
    files[f"{DIST_INFO}/licenses/LICENSE"] = b"LICENSE TEXT"
    return files


def _record_rows(smoke: Any, files: dict[str, bytes]) -> list[str]:
    rows = [
        f"{name},sha256={smoke._record_digest(data)},{len(data)}"
        for name, data in files.items()
    ]
    return [*rows, f"{DIST_INFO}/RECORD,,"]


def _write_archives(
    tmp_path: Path,
    smoke: Any,
    sdist_members: list[str],
    extra_members: list[tarfile.TarInfo] | None = None,
    wheel_extra: list[zipfile.ZipInfo] | None = None,
    wheel_tamper: dict[str, bytes] | None = None,
    wheel_attrs: dict[str, int] | None = None,
    edit_record: Callable[[list[str]], list[str] | None] | None = None,
    sdist_root: str = "arcgraph-0.1.0rc7",
    metadata_text: str = METADATA_TEXT,
    wheel_name: str = "arcgraph-0.1.0rc7-py3-none-any.whl",
    sdist_contents: dict[str, bytes] | None = None,
    wheel_drop: list[str] | None = None,
) -> tuple[Path, Path]:
    """Build a valid wheel (with a correct RECORD) and sdist, then tamper on demand.

    The RECORD is computed from the pristine wheel contents; ``wheel_tamper``
    changes what is stored afterwards, so the two can disagree.
    """

    tmp_path.mkdir(parents=True, exist_ok=True)
    files = _wheel_files(smoke, metadata_text)
    for name in wheel_drop or []:
        del files[name]
    rows: list[str] | None = _record_rows(smoke, files)
    if edit_record is not None:
        rows = edit_record(rows)
    stored = {**files, **(wheel_tamper or {})}
    if rows is not None:
        stored[f"{DIST_INFO}/RECORD"] = ("\n".join(rows) + "\n").encode()
    wheel = tmp_path / wheel_name
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in stored.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (wheel_attrs or {}).get(name, 0) << 16
            archive.writestr(info, data)
        for info in wheel_extra or []:
            archive.writestr(info, "x")
    sdist = tmp_path / "arcgraph-0.1.0rc7.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        contents = {"PKG-INFO": metadata_text.encode(), "LICENSE": b"LICENSE TEXT"}
        contents.update(sdist_contents or {})
        for name in sdist_members:
            data = contents.get(name, b"x")
            info = tarfile.TarInfo(f"{sdist_root}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra_members or []:
            archive.addfile(info)
    return wheel, sdist


def _zip_member(name: str, unix_type: int = 0o100000) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (unix_type | 0o644) << 16
    return info


def _tar_member(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


def test_sdist_members_must_be_tracked_or_generated(tmp_path: Path) -> None:
    smoke = load_package_smoke_module()
    tracked = ["README.md", "pyproject.toml", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]
    wheel, sdist = _write_archives(tmp_path, smoke, [*tracked, *generated])

    clean = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )

    assert clean["status"] == "pass"
    assert clean["sdist_files_absent_from_git"] == []

    leaked = tmp_path / "leaked"
    # Files a developer's global ignore hides from `git status` but the build
    # backend still packs; none of them is a fixed forbidden path.
    wheel, sdist = _write_archives(
        leaked,
        smoke,
        [*tracked, *generated, ".claude/settings.local.json", ".cursor/rules.json"],
    )

    result = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )

    assert result["status"] == "fail"
    assert result["sdist_files_absent_from_git"] == [
        ".claude/settings.local.json",
        ".cursor/rules.json",
    ]
    assert any(
        error.startswith("sdist_files_absent_from_git=") for error in result["errors"]
    )


def test_sdist_rejects_links_special_members_and_members_outside_the_root(
    tmp_path: Path,
) -> None:
    """A link or a member off the archive root must fail, not be skipped.

    A symlink hidden by ``.git/info/exclude`` was packed into an sdist by the
    build backend with an absolute ``linkname``, and every check that looked
    only at regular files passed it.
    """

    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]
    root = "arcgraph-0.1.0rc7"
    cases = {
        "symlink": (
            _tar_member(f"{root}/.cursor/rules.json", tarfile.SYMTYPE, "/etc/hosts"),
            "sdist_non_regular_members",
            ".cursor/rules.json (symlink to /etc/hosts)",
        ),
        "hardlink": (
            _tar_member(f"{root}/README.copy", tarfile.LNKTYPE, f"{root}/README.md"),
            "sdist_non_regular_members",
            "README.copy (hardlink to arcgraph-0.1.0rc7/README.md)",
        ),
        "fifo": (
            _tar_member(f"{root}/pipe", tarfile.FIFOTYPE),
            "sdist_non_regular_members",
            "pipe",
        ),
        "device": (
            _tar_member(f"{root}/dev0", tarfile.CHRTYPE),
            "sdist_non_regular_members",
            "dev0",
        ),
        "other root": (
            _tar_member("other-root/x.txt", tarfile.REGTYPE),
            "sdist_members_outside_root",
            "other-root/x.txt",
        ),
        "absolute path": (
            _tar_member("/etc/passwd", tarfile.REGTYPE),
            "sdist_members_outside_root",
            "/etc/passwd",
        ),
        "parent traversal": (
            _tar_member(f"{root}/../escape.txt", tarfile.REGTYPE),
            "sdist_members_outside_root",
            f"{root}/../escape.txt",
        ),
    }

    for index, (label, (member, key, expected)) in enumerate(cases.items()):
        wheel, sdist = _write_archives(
            tmp_path / f"sdist-{index}",
            smoke,
            [*tracked, *generated],
            extra_members=[member],
        )

        result = smoke._validate_package_contents(
            wheel=wheel, sdist=sdist, tracked_files=tracked
        )

        assert result["status"] == "fail", label
        assert any(expected in item for item in result[key]), (label, result[key])
        assert any(error.startswith(f"{key}=") for error in result["errors"]), label


def test_sdist_root_name_is_allowed_only_as_a_directory(tmp_path: Path) -> None:
    """A regular file named like the root escaped both the layout and the
    file allowlist, because the allowlist only looks below the root."""

    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]
    wheel, sdist = _write_archives(
        tmp_path,
        smoke,
        [*tracked, *generated],
        extra_members=[_tar_member("arcgraph-0.1.0rc7", tarfile.REGTYPE)],
    )

    result = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )

    assert result["status"] == "fail"
    assert result["sdist_members_outside_root"] == ["arcgraph-0.1.0rc7"]


def test_wheel_rejects_links_and_members_outside_the_layout(tmp_path: Path) -> None:
    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]

    wheel, sdist = _write_archives(tmp_path / "clean", smoke, [*tracked, *generated])
    clean = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )
    assert clean["status"] == "pass", clean["errors"]

    cases = {
        "symlink": (
            _zip_member("arcgraph/linked.py", 0o120000),
            "wheel_non_regular_members",
            "arcgraph/linked.py (unix file type 0o120000)",
        ),
        "parent traversal": (
            _zip_member("../local-secret.txt"),
            "wheel_members_outside_layout",
            "../local-secret.txt",
        ),
        "absolute path": (
            _zip_member("/etc/x"),
            "wheel_members_outside_layout",
            "/etc/x",
        ),
        "top-level file named like the package": (
            _zip_member("arcgraph"),
            "wheel_members_outside_layout",
            "arcgraph",
        ),
        "second top-level directory": (
            _zip_member("other/x.py"),
            "wheel_members_outside_layout",
            "other/x.py",
        ),
        "untracked dist-info file": (
            _zip_member(f"{DIST_INFO}/extra.txt"),
            "wheel_members_outside_layout",
            f"{DIST_INFO}/extra.txt",
        ),
        "dist-info root not bound to the wheel name": (
            _zip_member("arcgraph-9.9.9.dist-info/METADATA"),
            "wheel_members_outside_layout",
            "arcgraph-9.9.9.dist-info/METADATA",
        ),
        "file named like the package directory": (
            _zip_member("arcgraph/"),
            "wheel_non_regular_members",
            "arcgraph/ (directory entry with content or file type)",
        ),
        "dist-info directory with content": (
            _zip_member(f"{DIST_INFO}/"),
            "wheel_non_regular_members",
            f"{DIST_INFO}/ (directory entry with content or file type)",
        ),
    }
    for index, (label, (member, key, expected)) in enumerate(cases.items()):
        wheel, sdist = _write_archives(
            tmp_path / f"wheel-{index}",
            smoke,
            [*tracked, *generated],
            wheel_extra=[member],
        )

        result = smoke._validate_package_contents(
            wheel=wheel, sdist=sdist, tracked_files=tracked
        )

        assert result["status"] == "fail", label
        assert any(expected in item for item in result[key]), (label, result[key])
        assert any(error.startswith(f"{key}=") for error in result["errors"]), label


def test_wheel_type_and_name_must_agree(tmp_path: Path) -> None:
    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    wheel, sdist = _write_archives(
        tmp_path,
        smoke,
        tracked,
        wheel_attrs={"arcgraph/__init__.py": 0o040755},
    )

    result = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )

    assert result["status"] == "fail"
    assert result["wheel_non_regular_members"] == [
        "arcgraph/__init__.py (file entry marked as a directory)"
    ]


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_wheel_record_must_match_the_bytes_the_wheel_holds(tmp_path: Path) -> None:
    """pip installs the stored bytes, so a stale RECORD hides a changed file."""

    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    init = "arcgraph/__init__.py"
    cases = {
        "content changed, old RECORD": (
            {"wheel_tamper": {init: b"# LOCAL SECRET\n"}},
            "wheel_record_mismatches",
            f"{init} does not match its RECORD hash or size",
        ),
        "second member with the same name": (
            {"wheel_extra": [_zip_member(init)]},
            "wheel_duplicate_members",
            init,
        ),
        "row missing": (
            {"edit_record": lambda rows: [r for r in rows if not r.startswith(init)]},
            "wheel_record_mismatches",
            f"{init} is not listed in RECORD",
        ),
        "row for a file that is not there": (
            {"edit_record": lambda rows: [*rows, "arcgraph/ghost.py,sha256=AAAA,1"]},
            "wheel_record_mismatches",
            "RECORD lists arcgraph/ghost.py, which is not in the wheel",
        ),
        "wrong size": (
            {"edit_record": lambda rows: [r.replace(",0", ",1") for r in rows]},
            "wheel_record_mismatches",
            "does not match its RECORD hash or size",
        ),
        "RECORD lists itself with a hash": (
            {
                "edit_record": lambda rows: [
                    f"{DIST_INFO}/RECORD,sha256=abc,3" if r.endswith("RECORD,,") else r
                    for r in rows
                ]
            },
            "wheel_record_mismatches",
            "RECORD must list itself with an empty hash and size",
        ),
        "malformed row": (
            {"edit_record": lambda rows: [*rows, "only-one-field"]},
            "wheel_record_mismatches",
            "malformed RECORD row",
        ),
        "RECORD missing": (
            {"edit_record": lambda rows: None},
            "wheel_record_mismatches",
            "appears 0 times, not once",
        ),
    }
    for index, (label, (options, key, expected)) in enumerate(cases.items()):
        wheel, sdist = _write_archives(
            tmp_path / f"record-{index}", smoke, tracked, **options
        )

        result = smoke._validate_package_contents(
            wheel=wheel, sdist=sdist, tracked_files=tracked
        )

        assert result["status"] == "fail", label
        assert any(expected in item for item in result[key]), (label, result[key])


def test_record_digest_is_the_urlsafe_unpadded_sha256() -> None:
    smoke = load_package_smoke_module()

    assert smoke._record_digest(b"") == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"


def test_sdist_root_is_bound_to_the_archive_name_and_members_are_unique(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]
    for index, root in enumerate([".", "C:", "not-the-sdist-name"]):
        wheel, sdist = _write_archives(
            tmp_path / f"root-{index}", smoke, [*tracked, *generated], sdist_root=root
        )

        result = smoke._validate_package_contents(
            wheel=wheel, sdist=sdist, tracked_files=tracked
        )

        assert result["status"] == "fail", root
        assert result["sdist_members_outside_root"], root

    wheel, sdist = _write_archives(
        tmp_path / "duplicate", smoke, [*tracked, *generated, "README.md"]
    )
    result = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )
    assert result["status"] == "fail"
    assert result["sdist_duplicate_members"] == ["arcgraph-0.1.0rc7/README.md"]


def test_artifact_metadata_must_match_pyproject_and_the_license_must_ship(
    tmp_path: Path,
) -> None:
    """A regenerated RECORD cannot make a foreign project look like this one."""

    smoke = load_package_smoke_module()
    tracked = ["LICENSE", "README.md", "arcgraph/__init__.py"]
    members = [*tracked, "PKG-INFO", "arcgraph/_build_provenance.json"]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "LICENSE").write_bytes(b"LICENSE TEXT")

    def validate(name: str, **options: Any) -> dict[str, Any]:
        wheel, sdist = _write_archives(tmp_path / name, smoke, members, **options)
        return smoke._validate_package_contents(
            wheel=wheel,
            sdist=sdist,
            tracked_files=tracked,
            expected_name="arcgraph",
            expected_version="0.1.0rc7",
            repo_root=repo,
        )

    clean = validate("clean")
    assert clean["status"] == "pass", clean["errors"]

    foreign = METADATA_TEXT.replace("arcgraph", "other-project").replace(
        "0.1.0rc7", "9.9"
    )
    no_license_header = METADATA_TEXT.replace("License-File: LICENSE\n", "")
    cases = {
        "foreign name and version": (
            {"metadata_text": foreign},
            "artifact_identity_mismatches",
            "Name is ['other-project']",
        ),
        "license header removed": (
            {"metadata_text": no_license_header},
            "artifact_identity_mismatches",
            "License-File is []",
        ),
        "wheel file name": (
            {"wheel_name": "other_project-9.9-py3-none-any.whl"},
            "artifact_identity_mismatches",
            "wheel file name other_project-9.9-py3-none-any.whl",
        ),
        "license file changed in the sdist": (
            {"sdist_contents": {"LICENSE": b"other license"}},
            "license_file_problems",
            "sdist LICENSE differs from the tracked file",
        ),
        "license file removed from the wheel with a consistent RECORD": (
            {"wheel_drop": [f"{DIST_INFO}/licenses/LICENSE"]},
            "license_file_problems",
            "wheel is missing LICENSE",
        ),
    }
    for index, (label, (options, key, expected)) in enumerate(cases.items()):
        result = validate(f"case-{index}", **options)

        assert result["status"] == "fail", label
        assert any(expected in item for item in result[key]), (label, result[key])

    no_sdist_license = [name for name in members if name != "LICENSE"]
    wheel, sdist = _write_archives(
        tmp_path / "no-sdist-license", smoke, no_sdist_license
    )
    result = smoke._validate_package_contents(
        wheel=wheel,
        sdist=sdist,
        tracked_files=tracked,
        expected_name="arcgraph",
        expected_version="0.1.0rc7",
        repo_root=repo,
    )
    assert "sdist is missing LICENSE" in result["license_file_problems"]


def test_sdist_with_a_leading_root_directory_entry_still_validates(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    tracked = ["README.md", "arcgraph/__init__.py"]
    generated = ["PKG-INFO", "arcgraph/_build_provenance.json"]
    wheel, sdist = _write_archives(
        tmp_path,
        smoke,
        [*tracked, *generated],
        extra_members=[_tar_member("arcgraph-0.1.0rc7", tarfile.DIRTYPE)],
    )

    result = smoke._validate_package_contents(
        wheel=wheel, sdist=sdist, tracked_files=tracked
    )

    assert result["status"] == "pass", result["errors"]
    assert result["sdist_members_outside_root"] == []
    assert result["sdist_non_regular_members"] == []


def test_source_provenance_tracked_files_feed_the_check_but_not_the_evidence(
    tmp_path: Path,
) -> None:
    smoke = load_package_smoke_module()
    payload = smoke._base_payload(
        status="pass",
        repo_root=tmp_path,
        commit_sha="a" * 40,
        metadata={"name": "arcgraph", "version": "0.1.0rc7"},
        package_json={},
        source_provenance={
            "head_sha": "a" * 40,
            "tracked_files": ["README.md"],
            "tracked_package_files": ["arcgraph/__init__.py"],
        },
        install_mcp_extra=True,
        mcp_version_spec="mcp",
        artifact_dir=None,
        skip_ci=True,
        temp_path=None,
        temp_path_kept=False,
        commands=[],
        warnings=[],
    )

    assert "tracked_files" not in payload["source_provenance"]
    assert payload["source_provenance"]["tracked_package_files"] == [
        "arcgraph/__init__.py"
    ]


def test_package_readiness_smoke_rejects_tracked_symlink_before_build(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    smoke = load_package_smoke_module()
    provenance = {
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "working_tree_clean": True,
        "working_tree_status_sha256": "c" * 64,
        "tracked_symlinks": ["arcgraph/linked.py"],
        "tracked_package_files": ["arcgraph/__init__.py"],
        "tracked_files": ["arcgraph/__init__.py"],
    }
    monkeypatch.setattr(smoke, "_source_provenance", lambda repo: provenance)
    monkeypatch.setattr(
        smoke,
        "_read_project_metadata",
        lambda repo: {
            "name": "arcgraph",
            "version": "0.1.0rc6",
            "requires_python": ">=3.11",
            "console_script": "arcgraph.interfaces.cli:main",
            "has_mcp_extra": True,
            "build_backend": "hatchling.build",
        },
    )
    monkeypatch.setattr(
        smoke,
        "_read_package_json",
        lambda repo: {
            "name": "arcgraph-workbench-assets",
            "version": "0.1.0",
            "private": True,
            "has_publish_script": False,
        },
    )

    payload = smoke.run_package_readiness_smoke(repo_root=tmp_path)

    assert payload["status"] == "fail"
    assert payload["commands"] == []
    assert "tracked symlinks" in payload["failures"][0]["message"]


def test_package_readiness_smoke_detects_source_mutation_after_build(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    smoke = load_package_smoke_module()
    provenance = {
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "working_tree_clean": True,
        "working_tree_status_sha256": "c" * 64,
        "tracked_symlinks": [],
        "tracked_package_files": ["arcgraph/__init__.py"],
        "tracked_files": ["arcgraph/__init__.py"],
    }
    changed = {**provenance, "working_tree_status_sha256": "d" * 64}
    snapshots = iter([provenance, changed])
    monkeypatch.setattr(smoke, "_source_provenance", lambda repo: next(snapshots))
    monkeypatch.setattr(
        smoke,
        "_read_project_metadata",
        lambda repo: {
            "name": "arcgraph",
            "version": "0.1.0rc6",
            "requires_python": ">=3.11",
            "console_script": "arcgraph.interfaces.cli:main",
            "has_mcp_extra": True,
            "build_backend": "hatchling.build",
        },
    )
    monkeypatch.setattr(
        smoke,
        "_read_package_json",
        lambda repo: {
            "name": "arcgraph-workbench-assets",
            "version": "0.1.0",
            "private": True,
            "has_publish_script": False,
        },
    )
    monkeypatch.setattr(smoke, "_run_checked", lambda spec, commands: None)

    payload = smoke.run_package_readiness_smoke(repo_root=tmp_path)

    assert payload["status"] == "fail"
    assert payload["package_readiness_verdict"] == "PACKAGE_NOT_READY"
    assert (
        "changed while package artifacts were built"
        in payload["failures"][0]["message"]
    )


def test_package_readiness_smoke_detects_source_mutation_after_installed_smoke(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    smoke = load_package_smoke_module()
    provenance = {
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "working_tree_clean": True,
        "working_tree_status_sha256": "c" * 64,
        "tracked_symlinks": [],
        "tracked_package_files": ["arcgraph/__init__.py"],
        "tracked_files": ["arcgraph/__init__.py"],
    }
    changed = {**provenance, "working_tree_status_sha256": "d" * 64}
    snapshots = iter([provenance, provenance, changed])
    monkeypatch.setattr(smoke, "_source_provenance", lambda repo: next(snapshots))
    monkeypatch.setattr(
        smoke,
        "_read_project_metadata",
        lambda repo: {
            "name": "arcgraph",
            "version": "0.1.0rc6",
            "requires_python": ">=3.11",
            "console_script": "arcgraph.interfaces.cli:main",
            "has_mcp_extra": True,
            "build_backend": "hatchling.build",
        },
    )
    monkeypatch.setattr(
        smoke,
        "_read_package_json",
        lambda repo: {
            "name": "arcgraph-workbench-assets",
            "version": "0.1.0",
            "private": True,
            "has_publish_script": False,
        },
    )
    monkeypatch.setattr(smoke, "_write_sample_project", lambda repo: None)
    monkeypatch.setattr(smoke, "_write_typescript_project", lambda repo: None)
    monkeypatch.setattr(
        smoke,
        "_locate_artifacts",
        lambda dist: (dist / "arcgraph.whl", dist / "arcgraph.tar.gz"),
    )
    monkeypatch.setattr(smoke, "_artifact_summary", lambda **kwargs: {})
    monkeypatch.setattr(smoke, "_validate_package_contents", lambda **kwargs: {})
    monkeypatch.setattr(smoke, "_raise_for_content_failures", lambda result: None)
    monkeypatch.setattr(smoke, "_validate_payload_contract", lambda *args: None)
    monkeypatch.setattr(
        smoke, "_validate_installed_trial_setup", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        smoke,
        "_validate_typescript_degradation",
        lambda **kwargs: {"status": "pass"},
    )
    monkeypatch.setattr(
        smoke,
        "build_installed_command_plan",
        lambda **kwargs: [
            smoke.CommandSpec(name, ["fake"], tmp_path, 1)
            for name in (
                "sample-trial-setup-dry-run",
                "sample-current",
                "sample-status",
                "sample-context",
                "sample-explain",
            )
        ],
    )
    monkeypatch.setattr(
        smoke,
        "_run_checked",
        lambda spec, commands: subprocess.CompletedProcess(
            spec.command,
            0,
            stdout="{}",
            stderr="",
        ),
    )

    payload = smoke.run_package_readiness_smoke(
        repo_root=tmp_path,
        install_mcp_extra=False,
        skip_ci=True,
    )

    assert payload["status"] == "fail"
    assert payload["package_readiness_verdict"] == "PACKAGE_NOT_READY"
    assert (
        "changed while the package readiness smoke was running"
        in payload["failures"][0]["message"]
    )


def test_a_forged_dependency_passes_the_content_check_but_not_the_clean_rebuild(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The enumerated checks cannot see a field nobody listed; the rebuild can.

    A wheel whose METADATA gains a dependency, with its RECORD regenerated to
    match, is a consistent archive: the content validator has no rule about
    ``Requires-Dist``. Only the comparison with what the commit builds rejects it.
    """

    smoke = load_package_smoke_module()
    rc_spec = importlib.util.spec_from_file_location(
        "arcgraph_release_candidate_check",
        Path(__file__).resolve().parents[1] / "arcgraph_release_candidate_check.py",
    )
    assert rc_spec is not None and rc_spec.loader is not None
    rc = importlib.util.module_from_spec(rc_spec)
    rc_spec.loader.exec_module(rc)

    tracked = ["LICENSE", "README.md", "arcgraph/__init__.py"]
    members = [*tracked, "PKG-INFO", "arcgraph/_build_provenance.json"]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "LICENSE").write_bytes(b"LICENSE TEXT")
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling==1.32.4"]\n', encoding="utf-8"
    )
    for arguments in (
        ["init", "--quiet"],
        ["config", "user.email", "tests@example.invalid"],
        ["config", "user.name", "ArcGraph Tests"],
        ["add", "."],
        ["commit", "--quiet", "-m", "baseline"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()

    genuine_wheel, genuine_sdist = _write_archives(tmp_path / "genuine", smoke, members)
    forged_wheel, forged_sdist = _write_archives(
        tmp_path / "forged",
        smoke,
        members,
        metadata_text=METADATA_TEXT + "Requires-Dist: definitely-malicious\n",
    )

    def validate(wheel: Path, sdist: Path) -> dict[str, Any]:
        return smoke._validate_package_contents(
            wheel=wheel,
            sdist=sdist,
            tracked_files=tracked,
            expected_name="arcgraph",
            expected_version="0.1.0rc7",
            repo_root=repo,
        )

    assert validate(genuine_wheel, genuine_sdist)["status"] == "pass"
    forged = validate(forged_wheel, forged_sdist)
    assert forged["status"] == "pass", forged["errors"]

    def build_the_genuine_files(
        command: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-m", "build"]:
            return subprocess.run(command, **options)
        outdir = Path(command[command.index("--outdir") + 1])
        outdir.mkdir(parents=True)
        for original in (genuine_wheel, genuine_sdist):
            (outdir / original.name).write_bytes(original.read_bytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rc, "_run_process", build_the_genuine_files)

    def rebuild(wheel: Path, sdist: Path) -> dict[str, Any]:
        return rc._rebuild_in_clean_clone(
            repo,
            commit_sha=head,
            artifacts={"wheel": wheel, "sdist": sdist},
            timeout_seconds=60,
        )

    assert rebuild(genuine_wheel, genuine_sdist)["problems"] == []
    rejected = rebuild(forged_wheel, forged_sdist)["problems"]
    assert any(text.startswith("The wheel ") for text in rejected), rejected
    assert any(text.startswith("The sdist ") for text in rejected), rejected

    wheel_only = rebuild(forged_wheel, genuine_sdist)["problems"]
    assert len(wheel_only) == 1 and wheel_only[0].startswith("The wheel "), wheel_only
