"""Local package readiness smoke for ArcGraph maintainers.

This gate builds local wheel/sdist artifacts, installs the wheel into a fresh
temporary virtual environment, and validates the installed CLI against a small
temporary sample project. It never publishes artifacts.
"""

from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any

try:
    from scripts.arcgraph_trial_contract import (
        EXPECTED_DEFAULT_TOOL_NAMES,
        EXPECTED_FEEDBACK_TOOL_NAMES,
    )
except ModuleNotFoundError:  # Direct execution adds only scripts/ to sys.path.
    from arcgraph_trial_contract import (
        EXPECTED_DEFAULT_TOOL_NAMES,
        EXPECTED_FEEDBACK_TOOL_NAMES,
    )

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 is unsupported.
    tomllib = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0"
INDEX_PAYLOAD_SCHEMA_VERSION = "1.0.0"
READ_PAYLOAD_SCHEMA_VERSION = "1.4.0"
SMOKE_TARGET = "pkg.service.Greeter.greet"
TYPESCRIPT_WARNING_KIND = "typescript_frontend_unavailable"
DEFAULT_MCP_VERSION_SPEC = "mcp>=2.0.0,<3.0.0"
LEGACY_MCP_CLIENT_SPEC = "mcp==1.28.1"
MCP_V2_AUTO_PROTOCOL = "2026-07-28"
MCP_LEGACY_PROTOCOL = "2025-11-25"
DEFAULT_TIMEOUT_SECONDS = 240.0
MAX_CAPTURE_CHARS = 2000

REQUIRED_WHEEL_FILES = (
    "arcgraph/__init__.py",
    "arcgraph/_build_provenance.json",
    "arcgraph/interfaces/agent_capabilities.py",
    "arcgraph/interfaces/agent_help.py",
    "arcgraph/interfaces/cli.py",
    "arcgraph/interfaces/cli_trial.py",
    "arcgraph/interfaces/cli_feedback.py",
    "arcgraph/interfaces/docs.py",
    "arcgraph/interfaces/local_state.py",
    "arcgraph/interfaces/mcp_server.py",
    "arcgraph/interfaces/trial_feedback.py",
    "arcgraph/interfaces/trial_setup.py",
    "arcgraph/assets/workbench/index.html",
    "arcgraph/assets/workbench/vendor/d3.v7.min.js",
    "arcgraph/assets/workbench/vendor/LICENSE.d3.txt",
    "arcgraph/pipeline/typescript_extractor.mjs",
    "arcgraph/pipeline/typescript_references.mjs",
)
GENERATED_WHEEL_FILES = frozenset({"arcgraph/_build_provenance.json"})
# Members a source distribution carries that Git does not track: the metadata
# file the build backend writes and the provenance file the build hook writes.
GENERATED_SDIST_FILES = frozenset({"PKG-INFO", "arcgraph/_build_provenance.json"})
# Files the build backend writes into the wheel's *.dist-info directory.
WHEEL_DIST_INFO_FILES = frozenset({"METADATA", "WHEEL", "RECORD", "entry_points.txt"})
# Root files the build backend declares as License-File by default.
LICENSE_FILE_PATTERNS = ("LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*")

FORBIDDEN_ARCHIVE_MARKERS = (
    "docs/_internal/",
    "output/",
    "dist/",
    "node_modules/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".coverage",
    "__pycache__/",
)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: list[str]
    cwd: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    environment: tuple[tuple[str, str | None], ...] = ()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build local ArcGraph wheel/sdist artifacts and validate the installed "
            "wheel in a temporary virtual environment. Nothing is published."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="ArcGraph repository root. Defaults to this checkout.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary build/install workspace for debugging.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help=(
            "Persist the exact validated wheel and sdist to a new directory. "
            "The directory must not already exist and, when inside the source "
            "repository, must be Git-ignored."
        ),
    )
    parser.add_argument(
        "--skip-mcp-extra",
        action="store_true",
        help="Skip installing the optional MCP extra from the built wheel.",
    )
    parser.add_argument(
        "--mcp-version-spec",
        default=DEFAULT_MCP_VERSION_SPEC,
        help=(
            "MCP SDK requirement to resolve with the wheel extra. CI uses this "
            "for both the 2.0.0 lower bound and the newest allowed 2.x."
        ),
    )
    parser.add_argument(
        "--skip-ci",
        action="store_true",
        help="Skip the inner sample-project `arcgraph ci` step.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned command matrix without building or installing.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-command timeout in seconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_package_readiness_smoke(
        repo_root=Path(args.repo_root),
        install_mcp_extra=not args.skip_mcp_extra,
        mcp_version_spec=args.mcp_version_spec,
        artifact_dir=args.artifact_dir,
        skip_ci=args.skip_ci,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] in {"pass", "planned"} else 1


def run_package_readiness_smoke(
    *,
    repo_root: Path,
    install_mcp_extra: bool = True,
    mcp_version_spec: str = DEFAULT_MCP_VERSION_SPEC,
    artifact_dir: Path | None = None,
    skip_ci: bool = False,
    dry_run: bool = False,
    keep_temp: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    repo = repo_root.resolve()
    source_provenance = _source_provenance(repo)
    commit_sha = str(source_provenance["head_sha"])
    metadata = _read_project_metadata(repo)
    package_json = _read_package_json(repo)
    plan = build_command_plan(
        repo_root=repo,
        dist_dir=Path("<temp>") / "dist",
        venv_dir=Path("<temp>") / "venv",
        sample_repo=Path("<temp>") / "sample-repo",
        trial_home=Path("<temp>") / "trial-home",
        typescript_repo=Path("<temp>") / "typescript-repo",
        legacy_client_venv=Path("<temp>") / "mcp-v1-client",
        wheel_path=Path("<temp>") / "dist" / _planned_wheel_name(metadata),
        package_version=str(metadata["version"]),
        install_mcp_extra=install_mcp_extra,
        mcp_version_spec=mcp_version_spec,
        skip_ci=skip_ci,
        timeout_seconds=timeout_seconds,
    )
    if artifact_dir is not None:
        plan.append(
            CommandSpec(
                "persist-package-artifacts",
                ["<python>", "<internal-copy>", str(artifact_dir)],
                repo,
                timeout_seconds,
            )
        )
    if dry_run:
        return _base_payload(
            status="planned",
            repo_root=repo,
            commit_sha=commit_sha,
            metadata=metadata,
            package_json=package_json,
            source_provenance=source_provenance,
            install_mcp_extra=install_mcp_extra,
            mcp_version_spec=mcp_version_spec,
            artifact_dir=artifact_dir,
            skip_ci=skip_ci,
            temp_path=None,
            temp_path_kept=False,
            commands=[_planned_command_record(command) for command in plan],
            warnings=[
                "dry_run_only: no build, virtualenv, install, or sample smoke was executed.",
                *(
                    [
                        "working_tree_dirty: a real package readiness run would "
                        "fail before building artifacts."
                    ]
                    if not source_provenance["working_tree_clean"]
                    else []
                ),
            ],
        )

    workspace_path: Path | None
    temp_context: tempfile.TemporaryDirectory[str] | None = None
    if keep_temp:
        workspace_path = Path(tempfile.mkdtemp(prefix="arcgraph-package-smoke-"))
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="arcgraph-package-smoke-")
        workspace_path = Path(temp_context.name)

    commands: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    warnings: list[str] = []
    payload = _base_payload(
        status="pass",
        repo_root=repo,
        commit_sha=commit_sha,
        metadata=metadata,
        package_json=package_json,
        source_provenance=source_provenance,
        install_mcp_extra=install_mcp_extra,
        mcp_version_spec=mcp_version_spec,
        artifact_dir=artifact_dir,
        skip_ci=skip_ci,
        temp_path=str(workspace_path) if keep_temp else None,
        temp_path_kept=keep_temp,
        commands=commands,
        warnings=warnings,
    )

    try:
        if timeout_seconds <= 0:
            raise RuntimeError("--timeout-seconds must be greater than 0.")
        if not source_provenance["working_tree_clean"]:
            raise RuntimeError(
                "Package candidates require a clean working tree; commit or "
                "remove staged, unstaged, and untracked changes first."
            )
        if source_provenance["tracked_symlinks"]:
            raise RuntimeError(
                "Package candidates must not contain tracked symlinks: "
                + ", ".join(source_provenance["tracked_symlinks"][:10])
            )
        dist_dir = workspace_path / "dist"
        sample_repo = workspace_path / "sample-repo"
        typescript_repo = workspace_path / "typescript-repo"
        venv_dir = workspace_path / "venv"
        legacy_client_venv = workspace_path / "mcp-v1-client"
        # The trial-setup preview runs against this empty configuration home
        # so "changed no Claude configuration" can be observed rather than
        # read back from the payload's own self-report.
        trial_home = workspace_path / "trial-home"
        trial_home.mkdir()
        _write_sample_project(sample_repo)
        _write_typescript_project(typescript_repo)

        build_spec = CommandSpec(
            "build-package",
            [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
            repo,
            timeout_seconds,
        )
        _run_checked(build_spec, commands)
        if _source_provenance(repo) != source_provenance:
            raise RuntimeError(
                "Source tree changed while package artifacts were built."
            )
        wheel, sdist = _locate_artifacts(dist_dir)
        payload["artifacts"] = _artifact_summary(
            wheel=wheel,
            sdist=sdist,
            metadata=metadata,
            source_provenance=source_provenance,
        )

        payload["package_contents"] = _validate_package_contents(
            wheel=wheel,
            sdist=sdist,
            tracked_package_files=source_provenance["tracked_package_files"],
            tracked_files=source_provenance["tracked_files"],
            expected_name=str(metadata["name"]),
            expected_version=str(metadata["version"]),
            repo_root=repo,
        )
        _raise_for_content_failures(payload["package_contents"])

        _run_checked(
            CommandSpec(
                "create-venv",
                [sys.executable, "-m", "venv", str(venv_dir)],
                workspace_path,
                timeout_seconds,
            ),
            commands,
        )
        venv_python = _venv_executable(venv_dir, "python")
        arcgraph = _venv_executable(venv_dir, "arcgraph")
        _run_checked(
            CommandSpec(
                "install-wheel",
                [str(venv_python), "-m", "pip", "install", str(wheel)],
                workspace_path,
                timeout_seconds,
            ),
            commands,
        )
        if install_mcp_extra:
            _run_checked(
                CommandSpec(
                    "install-wheel-mcp-extra",
                    build_mcp_install_command(
                        python=venv_python,
                        wheel=wheel,
                        mcp_version_spec=mcp_version_spec,
                    ),
                    workspace_path,
                    timeout_seconds,
                ),
                commands,
            )

        installed_commands = build_installed_command_plan(
            arcgraph=arcgraph,
            venv_python=venv_python,
            sample_repo=sample_repo,
            output_dir=sample_repo / "output" / "arcgraph",
            trial_home=trial_home,
            skip_ci=skip_ci,
            timeout_seconds=timeout_seconds,
        )
        command_payloads: dict[str, dict[str, Any]] = {}
        for spec in installed_commands:
            completed = _run_checked(spec, commands)
            if spec.name == "arcgraph-version":
                _validate_installed_version(
                    completed.stdout,
                    expected=str(metadata["version"]),
                )
            if spec.name == "arcgraph-version-provenance":
                installed_provenance = json.loads(completed.stdout)
                _validate_installed_version_provenance(
                    installed_provenance,
                    expected_version=str(metadata["version"]),
                    expected_wheel_sha256=str(payload["artifacts"]["wheel_sha256"]),
                    expected_commit_sha=str(payload["source_provenance"]["head_sha"]),
                )
                payload["installed_version_provenance"] = installed_provenance
            if spec.name == "agent-help-overview":
                _validate_installed_agent_help(
                    json.loads(completed.stdout),
                    expected_version=str(metadata["version"]),
                )
            if spec.name in {
                "sample-doctor",
                "sample-trial-setup-dry-run",
                "sample-init-dry-run",
                "sample-build",
                "sample-current",
                "sample-status",
                "sample-context",
                "sample-explain",
                "sample-ci",
            }:
                command_payloads[spec.name] = json.loads(completed.stdout)

        _validate_installed_trial_setup(
            command_payloads["sample-trial-setup-dry-run"],
            sample_repo=sample_repo,
            trial_home=trial_home,
            expected_executable=arcgraph,
        )
        _validate_payload_contract(command_payloads["sample-current"], "current")
        _validate_payload_contract(command_payloads["sample-status"], "status")
        _validate_payload_contract(command_payloads["sample-context"], "context")
        _validate_payload_contract(command_payloads["sample-explain"], "explain")
        if not skip_ci:
            ci_payload = command_payloads.get("sample-ci", {})
            if ci_payload.get("status") not in {"pass", "warn"}:
                raise RuntimeError(
                    "sample `arcgraph ci` returned an unexpected status."
                )

        typescript_output_dir = typescript_repo / "output" / "arcgraph"
        typescript_completed = _run_checked(
            build_typescript_degradation_command(
                arcgraph=arcgraph,
                typescript_repo=typescript_repo,
                output_dir=typescript_output_dir,
                timeout_seconds=timeout_seconds,
            ),
            commands,
        )
        typescript_build = json.loads(typescript_completed.stdout)
        payload["typescript_degradation"] = _validate_typescript_degradation(
            build_payload=typescript_build,
            output_dir=typescript_output_dir,
            project_node_modules=typescript_repo / "node_modules",
        )

        if install_mcp_extra:
            installed_mcp = _installed_distribution_version(
                python=venv_python,
                distribution="mcp",
                cwd=workspace_path,
                timeout_seconds=timeout_seconds,
                commands=commands,
            )
            legacy_setup = build_legacy_client_setup_plan(
                legacy_client_venv=legacy_client_venv,
                cwd=workspace_path,
                timeout_seconds=timeout_seconds,
            )
            for spec in legacy_setup:
                _run_checked(spec, commands)
            legacy_python = _venv_executable(legacy_client_venv, "python")
            protocol_payloads: dict[str, dict[str, Any]] = {}
            for spec in build_mcp_protocol_command_plan(
                repo_root=repo,
                server_python=venv_python,
                v2_client_python=venv_python,
                legacy_client_python=legacy_python,
                sample_repo=sample_repo,
                output_dir=sample_repo / "output" / "arcgraph",
                package_version=str(metadata["version"]),
                timeout_seconds=timeout_seconds,
            ):
                completed = _run_checked(spec, commands)
                protocol_payloads[spec.name] = json.loads(completed.stdout)
            payload["mcp_protocol"] = _validate_mcp_protocol_matrix(
                protocol_payloads,
                installed_mcp_version=installed_mcp,
                requested_mcp_spec=mcp_version_spec,
                package_version=str(metadata["version"]),
            )
            multi_project_completed = _run_checked(
                build_multi_project_trial_command(
                    repo_root=repo,
                    server_python=venv_python,
                    arcgraph=arcgraph,
                    workspace=workspace_path / "multi-project-trial",
                    package_version=str(metadata["version"]),
                    timeout_seconds=timeout_seconds,
                ),
                commands,
            )
            payload["multi_project_isolation"] = _validate_multi_project_trial_payload(
                json.loads(multi_project_completed.stdout)
            )

        payload["sample_repo"] = {
            "status": "pass",
            "target": SMOKE_TARGET,
            "output_dir": str(sample_repo / "output" / "arcgraph"),
            "source_snippets_disabled": True,
            "ci_executed": not skip_ci,
        }
        if _source_provenance(repo) != source_provenance:
            raise RuntimeError(
                "Source tree changed while the package readiness smoke was running."
            )
        if artifact_dir is not None:
            payload["persisted_artifacts"] = _persist_package_artifacts(
                wheel=wheel,
                sdist=sdist,
                artifact_dir=artifact_dir,
                repo_root=repo,
                expected=payload["artifacts"],
            )
            if _source_provenance(repo) != source_provenance:
                raise RuntimeError(
                    "Source tree changed while package artifacts were persisted."
                )
        payload["package_readiness_verdict"] = "PACKAGE_READY_CANDIDATE"
    except Exception as exc:
        payload["status"] = "fail"
        payload["package_readiness_verdict"] = "PACKAGE_NOT_READY"
        failures.append({"check": "package-readiness-smoke", "message": str(exc)})
    finally:
        payload["failures"] = failures
        if temp_context is not None:
            temp_context.cleanup()

    return payload


def build_command_plan(
    *,
    repo_root: Path,
    dist_dir: Path,
    venv_dir: Path,
    sample_repo: Path,
    trial_home: Path,
    typescript_repo: Path,
    legacy_client_venv: Path,
    wheel_path: Path,
    package_version: str,
    install_mcp_extra: bool,
    mcp_version_spec: str,
    skip_ci: bool,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[CommandSpec]:
    python = _venv_executable(venv_dir, "python")
    arcgraph = _venv_executable(venv_dir, "arcgraph")
    commands = [
        CommandSpec(
            "build-package",
            [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
            repo_root,
            timeout_seconds,
        ),
        CommandSpec(
            "create-venv",
            [sys.executable, "-m", "venv", str(venv_dir)],
            repo_root,
            timeout_seconds,
        ),
        CommandSpec(
            "install-wheel",
            [str(python), "-m", "pip", "install", str(wheel_path)],
            repo_root,
            timeout_seconds,
        ),
    ]
    if install_mcp_extra:
        commands.append(
            CommandSpec(
                "install-wheel-mcp-extra",
                build_mcp_install_command(
                    python=python,
                    wheel=wheel_path,
                    mcp_version_spec=mcp_version_spec,
                ),
                repo_root,
                timeout_seconds,
            )
        )
    commands.extend(
        build_installed_command_plan(
            arcgraph=arcgraph,
            venv_python=python,
            sample_repo=sample_repo,
            output_dir=sample_repo / "output" / "arcgraph",
            trial_home=trial_home,
            skip_ci=skip_ci,
            timeout_seconds=timeout_seconds,
        )
    )
    commands.append(
        build_typescript_degradation_command(
            arcgraph=arcgraph,
            typescript_repo=typescript_repo,
            output_dir=typescript_repo / "output" / "arcgraph",
            timeout_seconds=timeout_seconds,
        )
    )
    if install_mcp_extra:
        legacy_python = _venv_executable(legacy_client_venv, "python")
        commands.extend(
            [
                CommandSpec(
                    "installed-mcp-version",
                    _distribution_version_command(python, "mcp"),
                    repo_root,
                    timeout_seconds,
                ),
                *build_legacy_client_setup_plan(
                    legacy_client_venv=legacy_client_venv,
                    cwd=repo_root,
                    timeout_seconds=timeout_seconds,
                ),
                *build_mcp_protocol_command_plan(
                    repo_root=repo_root,
                    server_python=python,
                    v2_client_python=python,
                    legacy_client_python=legacy_python,
                    sample_repo=sample_repo,
                    output_dir=sample_repo / "output" / "arcgraph",
                    package_version=package_version,
                    timeout_seconds=timeout_seconds,
                ),
                build_multi_project_trial_command(
                    repo_root=repo_root,
                    server_python=python,
                    arcgraph=arcgraph,
                    workspace=repo_root / "<temp>" / "multi-project-trial",
                    package_version=package_version,
                    timeout_seconds=timeout_seconds,
                ),
            ]
        )
    commands.extend(
        [
            CommandSpec(
                "check-wheel-contents", ["<python>", "<internal-check>"], repo_root
            ),
            CommandSpec(
                "check-sdist-contents", ["<python>", "<internal-check>"], repo_root
            ),
            CommandSpec(
                "check-package-json-private",
                ["<python>", "<internal-check>"],
                repo_root,
            ),
        ]
    )
    return commands


def build_installed_command_plan(
    *,
    arcgraph: Path,
    venv_python: Path,
    sample_repo: Path,
    output_dir: Path,
    trial_home: Path,
    skip_ci: bool,
    timeout_seconds: float,
) -> list[CommandSpec]:
    repo_args = ["--repo-root", str(sample_repo), "--output-dir", str(output_dir)]
    commands = [
        CommandSpec("arcgraph-help", [str(arcgraph), "--help"], sample_repo),
        CommandSpec(
            "agent-help-overview",
            [str(arcgraph), "help", "--topic", "overview"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "arcgraph-version",
            [str(arcgraph), "--version"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "arcgraph-version-provenance",
            [str(arcgraph), "version", "--json"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "docs-quickstart", [str(arcgraph), "docs", "quickstart"], sample_repo
        ),
        CommandSpec(
            "docs-agent-cli-contract",
            [str(arcgraph), "docs", "agent-cli-contract"],
            sample_repo,
        ),
        CommandSpec(
            "docs-mcp-server", [str(arcgraph), "docs", "mcp-server"], sample_repo
        ),
        CommandSpec(
            "docs-source-checkout-smoke",
            [str(arcgraph), "docs", "source-checkout-smoke"],
            sample_repo,
        ),
        CommandSpec(
            "docs-package-readiness",
            [str(arcgraph), "docs", "package-readiness"],
            sample_repo,
        ),
        CommandSpec("mcp-help", [str(arcgraph), "mcp", "--help"], sample_repo),
        CommandSpec(
            "mcp-serve-help",
            [str(arcgraph), "mcp", "serve", "--help"],
            sample_repo,
        ),
        CommandSpec(
            "mcp-module-help",
            [str(venv_python), "-m", "arcgraph.interfaces.mcp_server", "--help"],
            sample_repo,
        ),
        CommandSpec(
            "sample-doctor",
            [str(arcgraph), *repo_args, "doctor"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-trial-setup-dry-run",
            [
                str(arcgraph),
                "--repo-root",
                str(sample_repo),
                "trial",
                "setup",
                "--client",
                "claude",
                "--dry-run",
            ],
            sample_repo,
            timeout_seconds,
            environment=(
                ("HOME", str(trial_home)),
                ("USERPROFILE", str(trial_home)),
                ("XDG_CONFIG_HOME", str(trial_home)),
            ),
        ),
        CommandSpec(
            "sample-init-dry-run",
            [str(arcgraph), *repo_args, "init", "--dry-run"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-build",
            [str(arcgraph), *repo_args, "build"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-current",
            [str(arcgraph), *repo_args, "current"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-status",
            [str(arcgraph), *repo_args, "status"],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-context",
            [
                str(arcgraph),
                *repo_args,
                "context",
                SMOKE_TARGET,
                "--detail-level",
                "summary",
            ],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "sample-explain",
            [
                str(arcgraph),
                *repo_args,
                "explain",
                SMOKE_TARGET,
                "--detail-level",
                "summary",
            ],
            sample_repo,
            timeout_seconds,
        ),
    ]
    if not skip_ci:
        commands.append(
            CommandSpec(
                "sample-ci",
                [str(arcgraph), *repo_args, "ci"],
                sample_repo,
                timeout_seconds,
            )
        )
    return commands


def build_mcp_protocol_command_plan(
    *,
    repo_root: Path,
    server_python: Path,
    v2_client_python: Path,
    legacy_client_python: Path,
    sample_repo: Path,
    output_dir: Path,
    package_version: str,
    timeout_seconds: float,
) -> list[CommandSpec]:
    probe = repo_root / "scripts" / "arcgraph_mcp_protocol_probe.py"
    common = [
        "--server-python",
        str(server_python),
        "--repo-root",
        str(sample_repo),
        "--output-dir",
        str(output_dir),
        "--repo-id",
        "default",
        "--target",
        SMOKE_TARGET,
        "--expect-server-version",
        package_version,
        "--timeout-seconds",
        str(min(timeout_seconds, 60.0)),
    ]
    return [
        CommandSpec(
            "mcp-v2-auto-protocol",
            [
                str(v2_client_python),
                str(probe),
                *common,
                "--mode",
                "auto",
                "--expect-protocol",
                MCP_V2_AUTO_PROTOCOL,
            ],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "mcp-v2-legacy-protocol",
            [
                str(v2_client_python),
                str(probe),
                *common,
                "--mode",
                "legacy",
                "--expect-protocol",
                MCP_LEGACY_PROTOCOL,
            ],
            sample_repo,
            timeout_seconds,
        ),
        CommandSpec(
            "mcp-v1-client-protocol",
            [
                str(legacy_client_python),
                str(probe),
                *common,
                "--mode",
                "legacy",
                "--expect-protocol",
                MCP_LEGACY_PROTOCOL,
            ],
            sample_repo,
            timeout_seconds,
        ),
    ]


def build_multi_project_trial_command(
    *,
    repo_root: Path,
    server_python: Path,
    arcgraph: Path,
    workspace: Path,
    package_version: str,
    timeout_seconds: float,
) -> CommandSpec:
    return CommandSpec(
        "installed-wheel-multi-project-trial",
        [
            str(server_python),
            str(repo_root / "scripts" / "arcgraph_multi_project_trial_probe.py"),
            "--server-python",
            str(server_python),
            "--arcgraph",
            str(arcgraph),
            "--workspace",
            str(workspace),
            "--expect-server-version",
            package_version,
            "--timeout-seconds",
            str(min(timeout_seconds, 60.0)),
        ],
        workspace.parent,
        timeout_seconds,
    )


def _validate_multi_project_trial_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    required_true = {
        "one_installed_environment",
        "installed_cli_feedback",
        "server_names_distinct",
        "output_directories_distinct",
        "path_authorization_fail_closed",
        "current_and_graph_state_isolated",
        "metrics_isolated",
        "feedback_isolated",
        "peer_survived_other_server_shutdown",
        "installation_state_unchanged",
        "client_configuration_unchanged",
    }
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Multi-project trial used an unsupported schema version.")
    if payload.get("status") != "pass":
        raise RuntimeError("Installed-wheel multi-project trial did not pass.")
    if payload.get("project_count") != 2:
        raise RuntimeError("Multi-project trial did not exercise exactly two projects.")
    if payload.get("repo_ids") != ["default", "default"]:
        raise RuntimeError("Multi-project trial did not keep repo id default.")
    feedback_tools = payload.get("feedback_tool_names")
    if feedback_tools != list(EXPECTED_FEEDBACK_TOOL_NAMES):
        raise RuntimeError(
            "Multi-project trial did not prove its exact feedback-enabled "
            "tool contract."
        )
    if any(payload.get(field) is not True for field in required_true):
        raise RuntimeError("Multi-project trial omitted an isolation guarantee.")
    expected_permission_model = (
        "posix_private_modes_verified"
        if os.name == "posix"
        else "windows_acl_not_asserted"
    )
    if payload.get("permission_model") != expected_permission_model:
        raise RuntimeError("Multi-project trial overstated its permission guarantee.")
    expected_alias_policy = (
        "posix_parent_symlinks_rejected"
        if os.name == "posix"
        else "windows_reparse_not_asserted"
    )
    if payload.get("state_alias_policy") != expected_alias_policy:
        raise RuntimeError(
            "Multi-project trial did not prove the expected state-alias policy."
        )
    return payload


def build_legacy_client_setup_plan(
    *,
    legacy_client_venv: Path,
    cwd: Path,
    timeout_seconds: float,
) -> list[CommandSpec]:
    legacy_python = _venv_executable(legacy_client_venv, "python")
    return [
        CommandSpec(
            "create-mcp-v1-client-venv",
            [
                sys.executable,
                "-m",
                "venv",
                str(legacy_client_venv),
            ],
            cwd,
            timeout_seconds,
        ),
        CommandSpec(
            "install-mcp-v1-client",
            [
                str(legacy_python),
                "-m",
                "pip",
                "install",
                LEGACY_MCP_CLIENT_SPEC,
            ],
            cwd,
            timeout_seconds,
        ),
    ]


def build_typescript_degradation_command(
    *,
    arcgraph: Path,
    typescript_repo: Path,
    output_dir: Path,
    timeout_seconds: float,
) -> CommandSpec:
    """Build a TS project while deliberately denying ambient compiler lookup."""

    return CommandSpec(
        "typescript-build-without-runtime",
        [
            str(arcgraph),
            "--repo-root",
            str(typescript_repo),
            "--output-dir",
            str(output_dir),
            "build",
            "--root",
            "src",
        ],
        typescript_repo,
        timeout_seconds,
        environment=(("NODE_OPTIONS", "--no-global-search-paths"),),
    )


SAMPLE_GIT_EXCLUDE = "# package smoke local excludes\n"


def _write_sample_project(project: Path) -> None:
    git_info = project / ".git" / "info"
    git_info.mkdir(parents=True)
    (git_info / "exclude").write_text(SAMPLE_GIT_EXCLUDE, encoding="utf-8")
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
name = "arcgraph-package-smoke-project"
version = "0.0.0"

[tool.arcgraph]
source_roots = ["src"]
""".lstrip(),
        encoding="utf-8",
    )


def _write_typescript_project(project: Path) -> None:
    source = project / "src"
    source.mkdir(parents=True)
    (source / "index.ts").write_text(
        """
export interface Greeting {
  message: string;
}

export function greet(name: string): Greeting {
  return { message: `hello ${name}` };
}
""".lstrip(),
        encoding="utf-8",
    )


def _validate_typescript_degradation(
    *,
    build_payload: dict[str, Any],
    output_dir: Path,
    project_node_modules: Path,
) -> dict[str, Any]:
    """Require the warning in durable build artifacts, not a top-level view."""

    if project_node_modules.exists():
        raise RuntimeError(
            "TypeScript degradation probe unexpectedly has project node_modules."
        )
    build_dir_value = build_payload.get("build_dir")
    if not isinstance(build_dir_value, str):
        raise RuntimeError("TypeScript degradation build did not report build_dir.")
    build_dir = Path(build_dir_value).resolve()
    resolved_output = output_dir.resolve()
    if not build_dir.is_relative_to(resolved_output):
        raise RuntimeError(
            "TypeScript degradation build_dir escaped the isolated output directory."
        )

    summary_path = build_dir / "summary.json"
    diagnostics_path = build_dir / "diagnostics.jsonl"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        diagnostics = [
            json.loads(line)
            for line in diagnostics_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "TypeScript degradation evidence is missing or invalid."
        ) from exc

    summary_matches = [
        warning
        for warning in summary.get("warnings", [])
        if isinstance(warning, dict) and warning.get("kind") == TYPESCRIPT_WARNING_KIND
    ]
    diagnostic_matches = [
        diagnostic
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict)
        and diagnostic.get("diagnostic_kind") == TYPESCRIPT_WARNING_KIND
    ]
    if not summary_matches:
        raise RuntimeError(
            f"{TYPESCRIPT_WARNING_KIND} is missing from build summary.json."
        )
    if not diagnostic_matches:
        raise RuntimeError(
            f"{TYPESCRIPT_WARNING_KIND} is missing from build diagnostics.jsonl."
        )

    return {
        "status": "pass",
        "warning_kind": TYPESCRIPT_WARNING_KIND,
        "summary_warning_count": len(summary_matches),
        "diagnostic_count": len(diagnostic_matches),
        "project_node_modules_present": False,
        "pythonpath_cleared": True,
        "node_path_cleared": True,
        "node_global_search_paths_disabled": True,
    }


def _validate_installed_version(output: str, *, expected: str) -> None:
    actual = output.strip()
    if actual != f"arcgraph {expected}":
        raise RuntimeError(
            f"Installed `arcgraph --version` returned {actual!r}; "
            f"expected 'arcgraph {expected}'."
        )


def _validate_installed_version_provenance(
    payload: dict[str, Any],
    *,
    expected_version: str,
    expected_wheel_sha256: str,
    expected_commit_sha: str,
) -> None:
    if payload.get("product_version") != expected_version:
        raise RuntimeError(
            "Installed version provenance reports the wrong product version."
        )
    if payload.get("execution_mode") != "installed_distribution":
        raise RuntimeError(
            "Installed version provenance did not identify a wheel install."
        )
    artifact = payload.get("artifact_provenance", {})
    if artifact.get("sha256") != expected_wheel_sha256:
        raise RuntimeError(
            "Installed version provenance does not match the wheel artifact SHA-256."
        )
    if payload.get("provenance_status") != "verified_artifact_hash":
        raise RuntimeError(
            "Installed version provenance did not verify the artifact hash."
        )
    build = payload.get("build_provenance", {})
    if build.get("build_version") != expected_version:
        raise RuntimeError("Installed build provenance reports the wrong version.")
    source = build.get("source", {})
    if source.get("commit_sha") != expected_commit_sha:
        raise RuntimeError(
            "Installed build provenance reports the wrong source commit."
        )


def _validate_installed_agent_help(
    payload: dict[str, Any],
    *,
    expected_version: str,
) -> None:
    if payload.get("status") != "available" or payload.get("topic") != "overview":
        raise RuntimeError("Installed ArcGraph Agent help was unavailable.")
    if payload.get("product_version") != expected_version:
        raise RuntimeError("Installed ArcGraph Agent help reported the wrong version.")
    inventory = payload.get("inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("Installed ArcGraph Agent help omitted its inventory.")
    tools = inventory.get("mcp_tools")
    if not isinstance(tools, list) or "arcgraph_help" not in tools:
        raise RuntimeError("Installed ArcGraph Agent help omitted its MCP help tool.")
    if "arcgraph_record_trial_feedback" in tools:
        raise RuntimeError(
            "Default CLI Agent help claimed MCP feedback was registered."
        )
    feedback = payload.get("feedback")
    if not isinstance(feedback, dict) or feedback.get("cli_available") is not True:
        raise RuntimeError(
            "Installed ArcGraph Agent help omitted CLI feedback guidance."
        )


def build_mcp_install_command(
    *,
    python: Path,
    wheel: Path,
    mcp_version_spec: str,
) -> list[str]:
    return [
        str(python),
        "-m",
        "pip",
        "install",
        f"{wheel}[mcp]",
        mcp_version_spec,
    ]


def _distribution_version_command(python: Path, distribution: str) -> list[str]:
    script = (
        "from importlib import metadata; " f"print(metadata.version({distribution!r}))"
    )
    return [str(python), "-c", script]


def _installed_distribution_version(
    *,
    python: Path,
    distribution: str,
    cwd: Path,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
) -> str:
    completed = _run_checked(
        CommandSpec(
            f"installed-{distribution}-version",
            _distribution_version_command(python, distribution),
            cwd,
            timeout_seconds,
        ),
        commands,
    )
    version = completed.stdout.strip()
    if not version:
        raise RuntimeError(
            f"Installed distribution {distribution!r} did not report a version."
        )
    return version


def _validate_mcp_protocol_matrix(
    payloads: dict[str, dict[str, Any]],
    *,
    installed_mcp_version: str,
    requested_mcp_spec: str,
    package_version: str,
) -> dict[str, Any]:
    installed_mcp_major = _distribution_major(installed_mcp_version)
    if installed_mcp_major != 2:
        raise RuntimeError(
            "The installed ArcGraph server environment did not resolve MCP SDK v2."
        )
    exact_requested = re.fullmatch(r"mcp==([^,;\s]+)", requested_mcp_spec)
    if exact_requested and installed_mcp_version != exact_requested.group(1):
        raise RuntimeError(
            "The installed MCP SDK version did not satisfy the requested exact "
            "package-matrix line."
        )
    expected = {
        "mcp-v2-auto-protocol": {
            "client_major": 2,
            "mode": "auto",
            "protocol": MCP_V2_AUTO_PROTOCOL,
        },
        "mcp-v2-legacy-protocol": {
            "client_major": 2,
            "mode": "legacy",
            "protocol": MCP_LEGACY_PROTOCOL,
        },
        "mcp-v1-client-protocol": {
            "client_major": 1,
            "mode": "legacy",
            "protocol": MCP_LEGACY_PROTOCOL,
        },
    }
    if set(payloads) != set(expected):
        raise RuntimeError(
            "MCP protocol smoke did not execute the exact required client matrix."
        )

    contract_hashes: set[str] = set()
    summaries: dict[str, dict[str, Any]] = {}
    for name, requirements in expected.items():
        payload = payloads[name]
        client = payload.get("client", {})
        contract = payload.get("tool_contract", {})
        calls = payload.get("calls", [])
        server = payload.get("server", {})
        if payload.get("status") != "pass":
            raise RuntimeError(f"{name} did not report status=pass.")
        if payload.get("clean_shutdown") is not True:
            raise RuntimeError(f"{name} did not prove clean shutdown.")
        if payload.get("server_stderr_empty") is not True:
            raise RuntimeError(f"{name} observed unexpected server stderr.")
        if client.get("major") != requirements["client_major"]:
            raise RuntimeError(f"{name} used the wrong MCP client major.")
        if name == "mcp-v1-client-protocol" and (client.get("mcp_version") != "1.28.1"):
            raise RuntimeError(
                "The legacy compatibility probe did not use real mcp 1.28.1."
            )
        if requirements["client_major"] == 2 and (
            client.get("mcp_version") != installed_mcp_version
        ):
            raise RuntimeError(
                f"{name} did not use the installed server environment's MCP SDK."
            )
        if client.get("mode") != requirements["mode"]:
            raise RuntimeError(f"{name} used the wrong MCP client mode.")
        if payload.get("protocol_version") != requirements["protocol"]:
            raise RuntimeError(f"{name} negotiated the wrong protocol version.")
        if server.get("version") != package_version:
            raise RuntimeError(f"{name} observed the wrong server package version.")
        if server.get("name") != "ArcGraph":
            raise RuntimeError(f"{name} observed the wrong MCP server identity.")
        tool_names = contract.get("tool_names")
        if tool_names != list(EXPECTED_DEFAULT_TOOL_NAMES):
            raise RuntimeError(
                f"{name} did not prove the exact default MCP tool contract."
            )
        if contract.get("tool_count") != len(EXPECTED_DEFAULT_TOOL_NAMES):
            raise RuntimeError(f"{name} reported the wrong MCP tool count.")
        call_names = [call.get("name") for call in calls if isinstance(call, dict)]
        if call_names != tool_names:
            raise RuntimeError(f"{name} did not call every MCP tool in order.")
        contract_hash = contract.get("sha256")
        if not isinstance(contract_hash, str) or len(contract_hash) != 64:
            raise RuntimeError(f"{name} did not report a valid contract digest.")
        contract_hashes.add(contract_hash)
        summaries[name] = {
            "client_mcp_version": client.get("mcp_version"),
            "client_major": client.get("major"),
            "mode": client.get("mode"),
            "protocol_version": payload.get("protocol_version"),
            "tool_count": contract.get("tool_count"),
            "call_count": len(calls),
            "clean_shutdown": True,
            "server_stderr_empty": True,
        }
    if len(contract_hashes) != 1:
        raise RuntimeError("MCP clients observed different tool contracts.")

    return {
        "status": "pass",
        "server_mcp_version": installed_mcp_version,
        "requested_server_mcp_spec": requested_mcp_spec,
        "legacy_client_spec": LEGACY_MCP_CLIENT_SPEC,
        "tool_contract_sha256": next(iter(contract_hashes)),
        "tool_names": list(EXPECTED_DEFAULT_TOOL_NAMES),
        "clients": summaries,
    }


def _distribution_major(version: str) -> int:
    match = re.match(r"^(?:v)?(?P<major>\d+)(?:[.!+-]|$)", version)
    if match is None:
        raise RuntimeError(f"Cannot determine distribution major from {version!r}.")
    return int(match.group("major"))


def _read_project_metadata(repo: Path) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError("tomllib is required; ArcGraph supports Python 3.11+.")
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    optional = project.get("optional-dependencies", {})
    scripts = project.get("scripts", {})
    return {
        "name": project.get("name"),
        "version": project.get("version"),
        "requires_python": project.get("requires-python"),
        "console_script": scripts.get("arcgraph"),
        "has_mcp_extra": "mcp" in optional,
        "build_backend": pyproject.get("build-system", {}).get("build-backend"),
    }


def _read_package_json(repo: Path) -> dict[str, Any]:
    package_json = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    return {
        "name": package_json.get("name"),
        "version": package_json.get("version"),
        "private": package_json.get("private") is True,
        "has_publish_script": "publish" in package_json.get("scripts", {}),
    }


def _planned_wheel_name(metadata: dict[str, Any]) -> str:
    name = str(metadata.get("name") or "arcgraph").replace("-", "_")
    version = str(metadata.get("version") or "0.0.0")
    return f"{name}-{version}-py3-none-any.whl"


def _locate_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("arcgraph-*.whl"))
    sdists = sorted(dist_dir.glob("arcgraph-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            "Expected exactly one ArcGraph wheel and one sdist; found "
            f"wheels={len(wheels)} sdists={len(sdists)}."
        )
    return wheels[0], sdists[0]


def _artifact_summary(
    *,
    wheel: Path,
    sdist: Path,
    metadata: dict[str, Any],
    source_provenance: dict[str, Any],
) -> dict[str, Any]:
    wheel_metadata = _read_wheel_metadata(wheel)
    sdist_metadata = _read_sdist_metadata(sdist)
    return {
        "commit_sha": source_provenance["head_sha"],
        "source_tree_sha": source_provenance["tree_sha"],
        "source_status_sha256": source_provenance["working_tree_status_sha256"],
        "source_working_tree_clean": source_provenance["working_tree_clean"],
        "wheel": wheel.name,
        "wheel_sha256": _sha256_file(wheel),
        "wheel_size": wheel.stat().st_size,
        "sdist": sdist.name,
        "sdist_sha256": _sha256_file(sdist),
        "sdist_size": sdist.stat().st_size,
        "project_name": wheel_metadata.get("Name") or metadata.get("name"),
        "version": wheel_metadata.get("Version") or metadata.get("version"),
        "requires_python": (
            wheel_metadata.get("Requires-Python")
            or sdist_metadata.get("Requires-Python")
            or metadata.get("requires_python")
        ),
        "wheel_tag": _wheel_tag(wheel.name),
        "install_mode": "built-wheel-temporary-venv",
    }


def _persist_package_artifacts(
    *,
    wheel: Path,
    sdist: Path,
    artifact_dir: Path,
    repo_root: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    requested_target = artifact_dir.expanduser().absolute()
    if requested_target.exists() or requested_target.is_symlink():
        raise RuntimeError(
            "Artifact directory already exists; refusing to overwrite: "
            f"{requested_target}"
        )
    target = requested_target.resolve()
    if target.is_relative_to(repo_root) and not _git_ignores_path(repo_root, target):
        raise RuntimeError(
            "Artifact directory inside the source repository must be Git-ignored."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.",
        dir=target.parent,
    ) as temp_dir:
        staging = Path(temp_dir)
        copied_wheel = staging / wheel.name
        copied_sdist = staging / sdist.name
        shutil.copy2(wheel, copied_wheel)
        shutil.copy2(sdist, copied_sdist)
        persisted = {
            "wheel": copied_wheel.name,
            "wheel_sha256": _sha256_file(copied_wheel),
            "wheel_size": copied_wheel.stat().st_size,
            "sdist": copied_sdist.name,
            "sdist_sha256": _sha256_file(copied_sdist),
            "sdist_size": copied_sdist.stat().st_size,
        }
        for field in (
            "wheel",
            "wheel_sha256",
            "wheel_size",
            "sdist",
            "sdist_sha256",
            "sdist_size",
        ):
            if persisted[field] != expected.get(field):
                raise RuntimeError(
                    f"Persisted package artifact changed field {field!r}."
                )
        staging.replace(target)
    return {
        "status": "pass",
        "directory": str(target),
        **persisted,
    }


def _git_ignores_path(repo: Path, path: Path) -> bool:
    relative = path.relative_to(repo)
    completed = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "--quiet", str(relative)],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode not in {0, 1}:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git check-ignore failed: {detail}")
    return completed.returncode == 0


def _read_wheel_metadata(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        text = archive.read(metadata_name).decode("utf-8", errors="replace")
    return dict(Parser().parsestr(text).items())


def _read_sdist_metadata(sdist: Path) -> dict[str, str]:
    with tarfile.open(sdist, "r:gz") as archive:
        metadata_name = next(
            name for name in archive.getnames() if name.endswith("/PKG-INFO")
        )
        member = archive.extractfile(metadata_name)
        if member is None:
            return {}
        text = member.read().decode("utf-8", errors="replace")
    return dict(Parser().parsestr(text).items())


def _wheel_tag(filename: str) -> str:
    match = re.match(r".+?-.+?-(?P<tag>[^-]+-[^-]+-[^-]+)\.whl$", filename)
    return match.group("tag") if match else "unknown"


def _validate_package_contents(
    *,
    wheel: Path,
    sdist: Path,
    tracked_package_files: list[str] | None = None,
    tracked_files: list[str] | None = None,
    expected_name: str | None = None,
    expected_version: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    wheel_entries = _read_wheel_members(wheel)
    wheel_names = [name for name, _, _ in wheel_entries]
    # The archive file names bind the two roots: <name>-<version>.dist-info in
    # the wheel and <name>-<version> in the sdist.
    dist_info = "-".join(wheel.name.split("-")[:2]) + ".dist-info"
    wheel_non_regular, wheel_outside_layout = _wheel_layout(wheel_entries, dist_info)
    wheel_duplicates = _duplicate_names(wheel_names)
    wheel_record_problems = _wheel_record_problems(wheel_entries, dist_info)
    sdist_entries = _read_sdist_members(sdist)
    sdist_names = [name for name, _, _ in sdist_entries]
    sdist_prefix, sdist_outside_root = _sdist_layout(
        sdist_entries, sdist.name[: -len(".tar.gz")]
    )
    sdist_duplicates = _duplicate_names(sdist_names)
    # Only regular files and directories are legitimate source distribution
    # members: a link or special file can point outside the archive.
    sdist_non_regular = sorted(
        f"{name} ({kind})"
        for name, kind, _ in sdist_entries
        if kind not in ("file", "dir")
    )
    sdist_relative = [
        name[len(sdist_prefix) :] if name.startswith(sdist_prefix) else name
        for name in sdist_names
    ]
    sdist_relative_files = [
        name[len(sdist_prefix) :]
        for name, kind, _ in sdist_entries
        if kind == "file" and name.startswith(sdist_prefix)
    ]

    identity_problems: list[str] = []
    license_problems: list[str] = []
    if expected_name is not None and expected_version is not None:
        identity_problems, license_problems = _identity_problems(
            wheel=wheel,
            sdist=sdist,
            wheel_files={name: data for name, _, data in wheel_entries},
            sdist_files={
                name: data for name, kind, data in sdist_entries if kind == "file"
            },
            dist_info=dist_info,
            name=expected_name,
            version=expected_version,
            expected_licenses=_expected_license_files(tracked_files or []),
            repo_root=repo_root,
        )

    required_missing = [
        required for required in REQUIRED_WHEEL_FILES if required not in wheel_names
    ]
    wheel_forbidden = _forbidden_archive_hits(wheel_names)
    sdist_forbidden = _forbidden_archive_hits(sdist_relative)
    wheel_tests = [
        name
        for name in wheel_names
        if name.startswith("arcgraph/tests/") or name == "arcgraph/tests"
    ]
    sdist_tests = [
        name
        for name in sdist_relative
        if name.startswith("arcgraph/tests/")
        or name == "arcgraph/tests"
        or name.startswith("scripts/tests/")
        or name == "scripts/tests"
    ]
    errors: list[str] = []
    if required_missing:
        errors.append(f"wheel_missing_required_files={required_missing}")
    if wheel_forbidden:
        errors.append(f"wheel_forbidden_entries={wheel_forbidden[:10]}")
    if sdist_forbidden:
        errors.append(f"sdist_forbidden_entries={sdist_forbidden[:10]}")
    if wheel_tests:
        errors.append("wheel_includes_tests")
    if sdist_tests:
        errors.append("sdist_includes_tests")
    missing_tracked: list[str] = []
    unexpected_runtime: list[str] = []
    if tracked_package_files is not None:
        expected = set(tracked_package_files)
        packaged_runtime = {
            name
            for name in wheel_names
            if name.startswith("arcgraph/")
            and not name.endswith("/")
            and not name.startswith("arcgraph/tests/")
        }
        missing_tracked = sorted(expected - packaged_runtime)
        unexpected_runtime = sorted(packaged_runtime - expected - GENERATED_WHEEL_FILES)
        if missing_tracked:
            errors.append(f"wheel_missing_tracked_files={missing_tracked[:10]}")
        if unexpected_runtime:
            errors.append(f"wheel_files_absent_from_git={unexpected_runtime[:10]}")
    # Build backends pick sdist files from the repository .gitignore, so a file
    # hidden only by a developer's global ignore rules is packed although Git
    # never tracked it. Any member that is neither tracked nor generated fails.
    unexpected_sdist: list[str] = []
    if tracked_files is not None:
        unexpected_sdist = sorted(
            set(sdist_relative_files) - set(tracked_files) - GENERATED_SDIST_FILES
        )
        if unexpected_sdist:
            errors.append(f"sdist_files_absent_from_git={unexpected_sdist[:10]}")
    if identity_problems:
        errors.append(f"artifact_identity_mismatches={identity_problems[:10]}")
    if license_problems:
        errors.append(f"license_file_problems={license_problems[:10]}")
    if wheel_duplicates:
        errors.append(f"wheel_duplicate_members={wheel_duplicates[:10]}")
    if wheel_record_problems:
        errors.append(f"wheel_record_mismatches={wheel_record_problems[:10]}")
    if sdist_duplicates:
        errors.append(f"sdist_duplicate_members={sdist_duplicates[:10]}")
    if wheel_non_regular:
        errors.append(f"wheel_non_regular_members={wheel_non_regular[:10]}")
    if wheel_outside_layout:
        errors.append(f"wheel_members_outside_layout={wheel_outside_layout[:10]}")
    if sdist_outside_root:
        errors.append(f"sdist_members_outside_root={sdist_outside_root[:10]}")
    if sdist_non_regular:
        errors.append(f"sdist_non_regular_members={sdist_non_regular[:10]}")
    return {
        "status": "pass" if not errors else "fail",
        "wheel_file_count": len(wheel_names),
        "sdist_file_count": len(sdist_names),
        "required_wheel_files_present": not required_missing,
        "required_wheel_files_missing": required_missing,
        "wheel_tests_excluded": not wheel_tests,
        "sdist_tests_excluded": not sdist_tests,
        "wheel_forbidden_entries": wheel_forbidden[:20],
        "sdist_forbidden_entries": sdist_forbidden[:20],
        "wheel_missing_tracked_files": missing_tracked[:20],
        "wheel_files_absent_from_git": unexpected_runtime[:20],
        "sdist_files_absent_from_git": unexpected_sdist[:20],
        "artifact_identity_mismatches": identity_problems[:20],
        "license_file_problems": license_problems[:20],
        "wheel_duplicate_members": wheel_duplicates[:20],
        "wheel_record_mismatches": wheel_record_problems[:20],
        "sdist_duplicate_members": sdist_duplicates[:20],
        "wheel_non_regular_members": wheel_non_regular[:20],
        "wheel_members_outside_layout": wheel_outside_layout[:20],
        "sdist_members_outside_root": sdist_outside_root[:20],
        "sdist_non_regular_members": sdist_non_regular[:20],
        "errors": errors,
    }


def _read_wheel_members(wheel: Path) -> list[tuple[str, int, bytes]]:
    """Return ``(name, unix file type, content)`` for every wheel member.

    Zip entries carry a Unix file type in the high half of ``external_attr``;
    zero means the writer recorded none, which is how ordinary files appear.
    Names are kept exactly as stored so a backslash or a ``..`` is visible.
    """

    with zipfile.ZipFile(wheel) as archive:
        return [
            (info.filename, (info.external_attr >> 16) & 0o170000, archive.read(info))
            for info in archive.infolist()
        ]


def _read_sdist_members(sdist: Path) -> list[tuple[str, str, bytes]]:
    """Return ``(name, kind, content)`` for every sdist member.

    ``kind`` is ``file`` or ``dir`` for the two legitimate types and a short
    description for anything else, so no member type is silently dropped. The
    content is filled in for regular files only.
    """

    entries: list[tuple[str, str, bytes]] = []
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            data = b""
            # Same tests as TarInfo.isfile/isdir/issym/islnk, read from the type.
            if member.type in tarfile.REGULAR_TYPES:
                kind = "file"
                handle = archive.extractfile(member)
                data = handle.read() if handle is not None else b""
            elif member.type == tarfile.DIRTYPE:
                kind = "dir"
            elif member.type == tarfile.SYMTYPE:
                kind = f"symlink to {member.linkname}"
            elif member.type == tarfile.LNKTYPE:
                kind = f"hardlink to {member.linkname}"
            else:
                kind = f"tar type {member.type!r}"
            entries.append((member.name, kind, data))
    return entries


def _expected_license_files(tracked_files: list[str]) -> list[str]:
    """Return the root license files the build backend declares by default."""

    return sorted(
        name
        for name in tracked_files
        if "/" not in name
        and any(fnmatch.fnmatchcase(name, pattern) for pattern in LICENSE_FILE_PATTERNS)
    )


def _metadata_headers(text: str) -> dict[str, list[str]]:
    """Return the header block of a METADATA or PKG-INFO file.

    Keys are lower-cased; each key keeps every value in order. Continuation
    lines (starting with whitespace) belong to the previous value and are
    ignored, since none of the fields checked here is multi-line.
    """

    headers: dict[str, list[str]] = {}
    for line in text.split("\n\n", 1)[0].splitlines():
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers.setdefault(key.strip().lower(), []).append(value.strip())
    return headers


def _identity_problems(
    *,
    wheel: Path,
    sdist: Path,
    wheel_files: dict[str, bytes],
    sdist_files: dict[str, bytes],
    dist_info: str,
    name: str,
    version: str,
    expected_licenses: list[str],
    repo_root: Path | None,
) -> tuple[list[str], list[str]]:
    """Bind the archives to pyproject.toml: file names, embedded metadata and
    the license files the metadata declares must all agree.

    ``pyproject.toml`` names the project and version. The wheel and sdist file
    names, the ``METADATA`` and ``PKG-INFO`` headers, and the declared
    ``License-File`` entries (with the files themselves, identical to the
    tracked ones) must match it; a rebuilt RECORD cannot make a foreign
    project look like this one.
    """

    normalized = re.sub(r"[-_.]+", "_", name).lower()
    sdist_root = sdist.name[: -len(".tar.gz")]
    identity: list[str] = []
    if not wheel.name.startswith(f"{normalized}-{version}-"):
        identity.append(f"wheel file name {wheel.name} is not {normalized}-{version}-*")
    if sdist.name != f"{normalized}-{version}.tar.gz":
        identity.append(f"sdist file name {sdist.name} is not {normalized}-{version}")
    for label, path, files in (
        ("wheel METADATA", f"{dist_info}/METADATA", wheel_files),
        ("sdist PKG-INFO", f"{sdist_root}/PKG-INFO", sdist_files),
    ):
        if path not in files:
            identity.append(f"{label} is missing")
            continue
        headers = _metadata_headers(str(files[path], "utf-8", "replace"))
        names = headers["name"] if "name" in headers else []
        versions = headers["version"] if "version" in headers else []
        declared = sorted(headers["license-file"] if "license-file" in headers else [])
        if [re.sub(r"[-_.]+", "-", value).lower() for value in names] != [
            re.sub(r"[-_.]+", "-", name).lower()
        ]:
            identity.append(f"{label} Name is {names}, not {name!r}")
        if versions != [version]:
            identity.append(f"{label} Version is {versions}, not {version!r}")
        if declared != expected_licenses:
            identity.append(
                f"{label} License-File is {declared}, not {expected_licenses}"
            )
    licenses: list[str] = []
    for label, files, folder in (
        ("wheel", wheel_files, f"{dist_info}/licenses"),
        ("sdist", sdist_files, sdist_root),
    ):
        for license_file in expected_licenses:
            path = f"{folder}/{license_file}"
            if path not in files:
                licenses.append(f"{label} is missing {license_file}")
            elif (
                repo_root is not None
                and files[path] != (repo_root / license_file).read_bytes()
            ):
                licenses.append(f"{label} {license_file} differs from the tracked file")
    return identity, licenses


def _noncanonical_names(names: list[str]) -> list[str]:
    """Return names that are not plain relative paths.

    A canonical member name has no empty, ``.`` or ``..`` segment, no
    backslash, no drive letter or other colon in its first segment, and only
    printable characters. One trailing slash marks a zip directory entry.
    """

    bad: list[str] = []
    for name in names:
        body = name[:-1] if name.endswith("/") else name
        segments = body.split("/")
        if (
            not body
            or "\\" in body
            or ":" in segments[0]
            or not body.isprintable()
            or any(segment in ("", ".", "..") for segment in segments)
        ):
            bad.append(name)
    return bad


def _duplicate_names(names: list[str]) -> list[str]:
    """Return names that occur more than once, ignoring a trailing slash."""

    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        key = name.rstrip("/")
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def _sdist_layout(
    entries: list[tuple[str, str, bytes]], root: str
) -> tuple[str, list[str]]:
    """Return the sdist prefix and the members that are not below the root.

    The root is bound to the archive file name (``<name>-<version>``), so an
    archive rewritten under ``./``, a drive letter or another name fails. The
    root itself may appear only as a directory, because the file allowlist only
    looks below it. Non-canonical names are reported with the rest, never
    dropped.
    """

    prefix = f"{root}/"
    outside: set[str] = set(_noncanonical_names([name for name, _, _ in entries]))
    for name, kind, _ in entries:
        if not (name.startswith(prefix) or (name == root and kind == "dir")):
            outside.add(name)
    return prefix, sorted(outside)


def _wheel_layout(
    entries: list[tuple[str, int, bytes]], dist_info: str
) -> tuple[list[str], list[str]]:
    """Return wheel members whose type contradicts them and members off layout.

    A wheel holds the ``arcgraph`` package and one ``<name>-<version>.dist-info``
    directory with a fixed set of metadata files, and nothing else. A directory
    entry ends with ``/`` and has no content; any other entry does not end with
    ``/`` and is not marked as a directory or as a link or special file.
    """

    incoherent: list[str] = []
    outside: set[str] = set(_noncanonical_names([name for name, _, _ in entries]))
    for name, file_type, data in entries:
        is_directory = name.endswith("/")
        if file_type not in (0, 0o100000, 0o040000):
            incoherent.append(f"{name} (unix file type {file_type:#o})")
        elif is_directory and (data or file_type == 0o100000):
            incoherent.append(f"{name} (directory entry with content or file type)")
        elif not is_directory and file_type == 0o040000:
            incoherent.append(f"{name} (file entry marked as a directory)")
        parts = (name[:-1] if is_directory else name).split("/", 1)
        rest = parts[1] if len(parts) > 1 else ""
        in_package = parts[0] == "arcgraph" and (rest != "" or is_directory)
        in_dist_info = parts[0] == dist_info and (
            (rest == "" and is_directory)
            or rest in WHEEL_DIST_INFO_FILES
            or rest.startswith("licenses/")
            or (rest == "licenses" and is_directory)
        )
        if not (in_package or in_dist_info):
            outside.add(name)
    return sorted(set(incoherent)), sorted(outside)


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return encoded.rstrip(b"=").decode("ascii")


def _wheel_record_problems(
    entries: list[tuple[str, int, bytes]], dist_info: str
) -> list[str]:
    """Check the wheel's RECORD against the bytes the wheel actually holds.

    Every non-directory member except RECORD must be listed exactly once with
    the SHA-256 and size of its content, RECORD lists itself with an empty hash
    and size, and nothing that is not in the wheel may be listed.
    """

    record_name = f"{dist_info}/RECORD"
    records = [data for name, _, data in entries if name == record_name]
    if len(records) != 1:
        return [f"{record_name} appears {len(records)} times, not once"]
    problems: list[str] = []
    listed: dict[str, tuple[str, str]] = {}
    for row in csv.reader(str(records[0], "utf-8", "replace").splitlines()):
        if len(row) != 3:
            problems.append(f"malformed RECORD row {row!r}")
            continue
        if row[0] in listed:
            problems.append(f"RECORD lists {row[0]} more than once")
        listed[row[0]] = (row[1], row[2])
    contents = {name: data for name, _, data in entries if not name.endswith("/")}
    for name, data in contents.items():
        if name == record_name:
            if listed.get(name) != ("", ""):
                problems.append("RECORD must list itself with an empty hash and size")
        elif name not in listed:
            problems.append(f"{name} is not listed in RECORD")
        elif listed[name] != (f"sha256={_record_digest(data)}", str(len(data))):
            problems.append(f"{name} does not match its RECORD hash or size")
    problems.extend(
        f"RECORD lists {name}, which is not in the wheel"
        for name in listed
        if name not in contents
    )
    return problems


def _forbidden_archive_hits(names: list[str]) -> list[str]:
    hits: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        for marker in FORBIDDEN_ARCHIVE_MARKERS:
            if normalized.startswith(marker) or f"/{marker}" in normalized:
                hits.append(normalized)
                break
    return hits


def _raise_for_content_failures(content: dict[str, Any]) -> None:
    if content.get("status") != "pass":
        raise RuntimeError(
            "Package content validation failed: "
            + ", ".join(str(error) for error in content.get("errors", []))
        )


def _validate_payload_contract(payload: dict[str, Any], command: str) -> None:
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


def _run_checked(
    spec: CommandSpec, commands: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("NODE_PATH", None)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["NO_COLOR"] = "1"
    for name, value in spec.environment:
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    try:
        completed = subprocess.run(
            spec.command,
            cwd=spec.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        record = _command_record(
            spec,
            exit_code=124,
            duration_seconds=time.perf_counter() - started,
            stderr=f"Timed out after {spec.timeout_seconds}s.",
            stdout=_tail(exc.stdout),
        )
        commands.append(record)
        raise RuntimeError(record["stderr_tail"]) from exc
    record = _command_record(
        spec,
        exit_code=completed.returncode,
        duration_seconds=time.perf_counter() - started,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    commands.append(record)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{spec.name} failed with exit {completed.returncode}: "
            f"{record['stderr_tail'] or record['stdout_tail']}"
        )
    return completed


def _git_stdout(repo: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _source_provenance(repo: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git status failed: {detail}")
    tracked_entries = _git_index_entries(repo)
    return {
        "head_sha": _git_stdout(repo, ["rev-parse", "HEAD"]),
        "tree_sha": _git_stdout(repo, ["rev-parse", "HEAD^{tree}"]),
        "working_tree_clean": not completed.stdout,
        "working_tree_status_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "tracked_symlinks": sorted(
            path for mode, path in tracked_entries if mode == "120000"
        ),
        "tracked_package_files": sorted(
            path
            for mode, path in tracked_entries
            if mode != "120000"
            and path.startswith("arcgraph/")
            and not path.startswith("arcgraph/tests/")
        ),
        "tracked_files": sorted(
            path for mode, path in tracked_entries if mode != "120000"
        ),
    }


def _git_index_entries(repo: Path) -> list[tuple[str, str]]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "-z"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {detail}")
    entries: list[tuple[str, str]] = []
    for raw_record in completed.stdout.split(b"\0"):
        if not raw_record:
            continue
        header, separator, raw_path = raw_record.partition(b"\t")
        if not separator:
            raise RuntimeError("git ls-files returned an invalid index record")
        mode = header.split(b" ", 1)[0].decode("ascii", errors="strict")
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append((mode, path))
    return entries


def _validate_installed_trial_setup(
    payload: dict[str, Any],
    *,
    sample_repo: Path,
    trial_home: Path,
    expected_executable: Path,
) -> None:
    if payload.get("action") != "dry_run":
        raise RuntimeError("Installed-wheel trial setup did not remain a dry run.")
    if payload.get("status") not in {"ready", "review"}:
        raise RuntimeError("Installed-wheel trial setup did not produce a usable plan.")
    if payload.get("claude_config_modified") is not False:
        raise RuntimeError("Installed-wheel trial setup modified Claude configuration.")
    checks = payload.get("checks")
    if not isinstance(checks, list) or any(
        isinstance(check, dict) and check.get("status") == "fail" for check in checks
    ):
        raise RuntimeError(
            "Installed-wheel trial setup reported a failed prerequisite."
        )
    executable_check = next(
        (
            check
            for check in checks
            if isinstance(check, dict) and check.get("name") == "arcgraph_executable"
        ),
        None,
    )
    if (
        not isinstance(executable_check, dict)
        or executable_check.get("status") != "pass"
        or executable_check.get("selection_source")
        not in {"invocation", "distribution_record"}
    ):
        raise RuntimeError(
            "Installed-wheel trial setup did not bind its executable to the "
            "invocation or distribution RECORD."
        )
    command = payload.get("registration_command")
    if not isinstance(command, list) or command[:6] != [
        "claude",
        "mcp",
        "add",
        "--scope",
        "local",
        "arcgraph",
    ]:
        raise RuntimeError(
            "Installed-wheel trial setup returned an invalid registration command."
        )
    serve = payload.get("serve_command")
    if (
        not isinstance(serve, list)
        or not serve
        or Path(str(serve[0])).resolve() != expected_executable.resolve()
        or command[6:7] != ["--"]
        or command[7:] != serve
    ):
        raise RuntimeError(
            "Installed-wheel trial setup registration does not wrap its serve command."
        )
    expected_root = str(sample_repo.resolve())
    expected_state = sample_repo.resolve() / ".arcgraph-trial"
    expected_paths = {
        "state_root": str(expected_state),
        "output_dir": str(expected_state / "index"),
        "metrics_log": str(expected_state / "metrics" / "mcp.jsonl"),
        "feedback_log": str(expected_state / "feedback" / "agent.jsonl"),
        "git_exclude": str(sample_repo.resolve() / ".git" / "info" / "exclude"),
    }
    if (
        payload.get("repo_root") != expected_root
        or payload.get("paths") != expected_paths
    ):
        raise RuntimeError(
            "Installed-wheel trial setup returned paths for another project."
        )
    if (sample_repo / ".arcgraph-trial").exists():
        raise RuntimeError("Installed-wheel trial setup dry run created local state.")
    # The payload's own claude_config_modified is a constant, so it cannot be
    # the evidence for the claim. The preview ran against an empty
    # configuration home and a known git exclude; both are read back here
    # because those are the only writes a dry run could plausibly make.
    stray_config = sorted(item.name for item in trial_home.iterdir())
    if stray_config:
        raise RuntimeError(
            "Installed-wheel trial setup dry run wrote client configuration: "
            f"{stray_config}."
        )
    exclude = sample_repo / ".git" / "info" / "exclude"
    if exclude.read_text(encoding="utf-8") != SAMPLE_GIT_EXCLUDE:
        raise RuntimeError(
            "Installed-wheel trial setup dry run modified .git/info/exclude."
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _venv_executable(venv_dir: Path, name: str) -> Path:
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    return bin_dir / f"{name}{suffix}"


def _base_payload(
    *,
    status: str,
    repo_root: Path,
    commit_sha: str,
    metadata: dict[str, Any],
    package_json: dict[str, Any],
    source_provenance: dict[str, Any],
    install_mcp_extra: bool,
    mcp_version_spec: str,
    artifact_dir: Path | None,
    skip_ci: bool,
    temp_path: str | None,
    temp_path_kept: bool,
    commands: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "package_readiness_verdict": (
            "PACKAGE_READY_CANDIDATE" if status == "pass" else "not_executed"
        ),
        "commit_sha": commit_sha,
        "source_provenance": {
            key: value
            for key, value in source_provenance.items()
            if key != "tracked_files"
        },
        "repo_root": str(repo_root),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "python": {
            "host_executable": sys.executable,
            "host_version": platform.python_version(),
        },
        "project_metadata": metadata,
        "package_json": package_json,
        "install_mode": "built-wheel-temporary-venv",
        "mcp_extra_installed": install_mcp_extra,
        "mcp_version_spec": mcp_version_spec,
        "artifact_dir": (
            str(artifact_dir.expanduser().resolve())
            if artifact_dir is not None
            else None
        ),
        "skip_ci": skip_ci,
        "commands": commands,
        "matrix": _matrix_summary(
            install_mcp_extra=install_mcp_extra,
            skip_ci=skip_ci,
        ),
        "temp_path": temp_path,
        "temp_path_kept": temp_path_kept,
        "warnings": warnings,
    }


def _matrix_summary(*, install_mcp_extra: bool, skip_ci: bool) -> dict[str, Any]:
    executed = [
        "local_wheel_and_sdist_build",
        "wheel_install_in_temporary_venv",
        "installed_cli_help_and_docs",
        "installed_trial_setup_dry_run",
        "installed_mcp_help_and_module_help",
        "package_contents_validation",
        "sample_repo_doctor_init_build_current_status",
        "sample_repo_context_and_explain",
        "typescript_runtime_degradation",
        "package_json_private_dev_only",
    ]
    skipped: list[str] = []
    if install_mcp_extra:
        executed.extend(
            [
                "wheel_mcp_extra_install",
                "mcp_v2_auto_protocol_handshake",
                "mcp_v2_legacy_protocol_handshake",
                "mcp_v1_client_protocol_handshake",
                "mcp_exact_tool_surface_and_calls",
                "mcp_clean_process_shutdown",
                "installed_wheel_two_project_isolation",
            ]
        )
    else:
        skipped.extend(
            [
                "wheel_mcp_extra_install",
                "mcp_v2_auto_protocol_handshake",
                "mcp_v2_legacy_protocol_handshake",
                "mcp_v1_client_protocol_handshake",
                "mcp_exact_tool_surface_and_calls",
                "mcp_clean_process_shutdown",
                "installed_wheel_two_project_isolation",
            ]
        )
    if skip_ci:
        skipped.append("sample_repo_arcgraph_ci")
    else:
        executed.append("sample_repo_arcgraph_ci")
    return {
        "executed": executed,
        "skipped": skipped,
        "deferred": [
            "package_publishing_approval",
            "pypi_publishing",
            "npm_package_publishing",
            "docker_or_ghcr_image",
            "github_release_or_tag",
            "public_repo_visibility",
            "public_packaged_mcp_distribution",
            "full_multi_os_package_matrix",
            "pipx_uv_homebrew_scoop_winget_installs",
        ],
    }


def _planned_command_record(spec: CommandSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "command": spec.command,
        "cwd": str(spec.cwd),
        "timeout_seconds": spec.timeout_seconds,
        "environment": dict(spec.environment),
        "cleared_environment": ["PYTHONPATH", "NODE_PATH"],
        "planned": True,
    }


def _command_record(
    spec: CommandSpec,
    *,
    exit_code: int,
    duration_seconds: float,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "command": spec.command,
        "cwd": str(spec.cwd),
        "environment": dict(spec.environment),
        "cleared_environment": ["PYTHONPATH", "NODE_PATH"],
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _tail(value: Any, *, max_chars: int = MAX_CAPTURE_CHARS) -> str:
    if value is None:
        return ""
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[-max_chars:]


if __name__ == "__main__":
    raise SystemExit(main())
