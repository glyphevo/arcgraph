"""Assemble a local ArcGraph external-trial candidate without publishing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

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

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_SCHEMA_VERSION = "1.1"
EXPECTED_VERSION = "0.1.0rc9"
# The candidate check compares the shipped wheel and sdist with a clean rebuild
# of the candidate commit; only the tool facts, never a host path, are shared.
REBUILD_CHECK = "clean-rebuild-identical"
REBUILD_PUBLIC_FIELDS = (
    "wheel",
    "sdist",
    "wheel_sha256",
    "sdist_sha256",
    "rebuilt_wheel_sha256",
    "rebuilt_sdist_sha256",
    "commit_sha",
    "tree_sha",
    "python",
    "platform",
    "build_frontend",
    "build_backend",
    "artifact_build_backend",
    "build_requires",
)
NPM_AUDIT_VERSION = "11.12.1"
NPM_AUDIT_SPEC = f"npm@{NPM_AUDIT_VERSION}"
RELEASE_NOTES = Path("docs/release_notes/v0.1.0-rc9.md")
TRIAL_GUIDE = Path("docs/external-trial-guide.md")
TRIAL_AGENT_GUIDE = Path("docs/agent-reading-guide.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and atomically assemble a local ArcGraph v0.1.0rc9 "
            "external-trial bundle. Nothing is published, tagged, or pushed."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="ArcGraph source repository. Defaults to this checkout.",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        required=True,
        help=(
            "New destination directory for the immutable local bundle. "
            "Existing destinations are never overwritten."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=240.0,
        help="Per-command timeout passed to package and RC smoke gates.",
    )
    parser.add_argument(
        "--remote-ci-evidence",
        type=Path,
        required=True,
        help=(
            "JSON saved from `gh run view RUN_ID --json "
            "databaseId,headSha,headBranch,event,status,conclusion,name,url,updatedAt,jobs`. "
            "The completed successful run must match the candidate repository "
            "and commit and contain a successful CI Gate job."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_external_trial_bundle(
            repo_root=args.repo_root,
            bundle_dir=args.bundle_dir,
            timeout_seconds=args.timeout_seconds,
            remote_ci_evidence=args.remote_ci_evidence,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "status": "fail",
                    "failure": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def build_external_trial_bundle(
    *,
    repo_root: Path,
    bundle_dir: Path,
    timeout_seconds: float,
    remote_ci_evidence: Path,
) -> dict[str, Any]:
    repo = repo_root.expanduser().resolve()
    requested_target = bundle_dir.expanduser().absolute()
    if timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be greater than 0.")
    if requested_target.exists() or requested_target.is_symlink():
        raise RuntimeError(f"Bundle directory already exists: {requested_target}")
    target = requested_target.resolve()
    if not repo.is_dir():
        raise RuntimeError(f"Repository root does not exist: {repo}")
    if target.is_relative_to(repo) and not _git_ignores_path(repo, target):
        raise RuntimeError(
            "Bundle directory inside the source repository must be Git-ignored."
        )

    version = _project_version(repo)
    if version != EXPECTED_VERSION:
        raise RuntimeError(
            f"External-trial bundle requires {EXPECTED_VERSION}, found {version}."
        )
    release_notes = repo / RELEASE_NOTES
    trial_guide = repo / TRIAL_GUIDE
    trial_agent_guide = repo / TRIAL_AGENT_GUIDE
    for required in (release_notes, trial_guide, trial_agent_guide):
        if not required.is_file():
            raise RuntimeError(f"Required candidate document is missing: {required}")

    # Fail on operator/remote provenance before the multi-minute package,
    # installed-wheel, and security smokes.  The package smoke independently
    # re-establishes the same commit and clean-tree facts below.
    candidate_commit = _head_commit(repo)
    origin_repository = _origin_repository(repo)
    remote_ci_payload = _load_remote_ci_evidence(
        remote_ci_evidence,
        expected_commit=candidate_commit,
        expected_repository=origin_repository,
    )
    _require_candidate_remote_state(repo, candidate_commit)

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.",
        dir=target.parent,
    ) as temp_dir:
        staging = Path(temp_dir)
        artifacts_dir = staging / "artifacts"
        package_payload = _run_package_smoke(
            repo=repo,
            artifacts_dir=artifacts_dir,
            timeout_seconds=timeout_seconds,
        )
        _require_package_candidate(package_payload)
        package_commit = str(package_payload["source_provenance"]["head_sha"])
        if package_commit != candidate_commit:
            raise RuntimeError(
                "Package smoke source commit changed after provenance preflight."
            )
        persisted = package_payload["persisted_artifacts"]
        wheel = artifacts_dir / str(persisted["wheel"])
        if not wheel.is_file():
            raise RuntimeError("Package smoke did not persist its validated wheel.")
        sdist = artifacts_dir / str(persisted["sdist"])
        if not sdist.is_file():
            raise RuntimeError("Package smoke did not persist its validated sdist.")

        raw_rc_output = staging / ".release-candidate-smoke.raw.json"
        rc_payload = _run_release_candidate_smoke(
            repo=repo,
            wheel=wheel,
            sdist=sdist,
            output=raw_rc_output,
            timeout_seconds=timeout_seconds,
        )
        if rc_payload.get("status") != "pass":
            raise RuntimeError("Installed-wheel release-candidate smoke did not pass.")
        raw_rc_output.unlink()
        security_payload = _collect_security_evidence(
            repo=repo,
            wheel=wheel,
            output_dir=staging / "security",
            timeout_seconds=timeout_seconds,
            source_provenance=package_payload["source_provenance"],
        )
        # The smokes may take minutes.  Revalidate the cheap mutable refs at
        # the publication boundary so the early preflight does not create a
        # time-of-check/time-of-use provenance gap.
        _require_candidate_remote_state(repo, candidate_commit)

        shutil.copy2(release_notes, staging / release_notes.name)
        shutil.copy2(trial_guide, staging / trial_guide.name)
        shutil.copy2(trial_agent_guide, staging / trial_agent_guide.name)

        public_package_payload = _public_package_evidence(package_payload)
        public_rc_payload = _public_release_candidate_evidence(
            rc_payload,
            persisted=persisted,
        )
        _write_json(staging / "package-readiness.json", public_package_payload)
        _write_json(staging / "release-candidate-smoke.json", public_rc_payload)
        _write_json(staging / "remote-ci.json", remote_ci_payload)

        provenance = _candidate_provenance(
            version=version,
            package_payload=package_payload,
            rc_payload=rc_payload,
            security_payload=security_payload,
            remote_ci_payload=remote_ci_payload,
        )
        _write_json(staging / "provenance.json", provenance)
        _assert_portable_bundle_text(
            staging,
            forbidden_values=(repo, target, staging, Path.home()),
        )
        _write_sha256_manifest(staging)
        staging.replace(target)

    manifest = target / "SHA256SUMS"
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "pass",
        "candidate_version": version,
        "bundle_dir": str(target),
        "commit_sha": provenance["source"]["commit_sha"],
        "tree_sha": provenance["source"]["tree_sha"],
        "wheel": persisted["wheel"],
        "wheel_sha256": persisted["wheel_sha256"],
        "sdist": persisted["sdist"],
        "sdist_sha256": persisted["sdist_sha256"],
        "manifest": manifest.name,
        "manifest_sha256": _sha256_file(manifest),
        "file_count": len([path for path in target.rglob("*") if path.is_file()]),
        "external_actions": {
            "scope": "bundle_assembler_execution",
            "performed": [],
        },
    }


def _run_package_smoke(
    *,
    repo: Path,
    artifacts_dir: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo / "scripts" / "arcgraph_package_readiness_smoke.py"),
        "--repo-root",
        str(repo),
        "--artifact-dir",
        str(artifacts_dir),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = _run_process(
        command,
        cwd=repo,
        timeout=max(timeout_seconds * 12, 600.0),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Package smoke did not return JSON evidence.") from exc
    if completed.returncode != 0:
        failure = payload.get("failures") if isinstance(payload, dict) else None
        raise RuntimeError(f"Package readiness smoke failed: {failure!r}")
    if not isinstance(payload, dict):
        raise RuntimeError("Package smoke returned a non-object JSON payload.")
    return payload


def _run_release_candidate_smoke(
    *,
    repo: Path,
    wheel: Path,
    sdist: Path,
    output: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo / "scripts" / "arcgraph_release_candidate_check.py"),
        "--wheel",
        str(wheel),
        "--sdist",
        str(sdist),
        "--repo-root",
        str(repo),
        "--output",
        str(output),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = _run_process(
        command,
        cwd=repo,
        timeout=max(timeout_seconds * 12, 600.0),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Release-candidate smoke failed: "
            f"{_tail(completed.stderr) or _tail(completed.stdout)}"
        )
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Release-candidate smoke evidence is invalid.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Release-candidate smoke returned non-object evidence.")
    return payload


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("NODE_PATH", None)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["NO_COLOR"] = "1"
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Candidate command timed out after {timeout} seconds."
        ) from exc


def _collect_security_evidence(
    *,
    repo: Path,
    wheel: Path,
    output_dir: Path,
    timeout_seconds: float,
    source_provenance: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True)
    if not wheel.is_file():
        raise RuntimeError("Candidate wheel is missing before security evidence.")
    with tempfile.TemporaryDirectory(prefix="arcgraph-candidate-security-") as temp_dir:
        security_venv = Path(temp_dir) / "venv"
        _run_required_security_command(
            name="candidate-security-venv",
            command=[sys.executable, "-m", "venv", str(security_venv)],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        candidate_python = _venv_python(security_venv)
        _run_required_security_command(
            name="install-candidate-mcp-runtime",
            command=[
                str(candidate_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"{wheel.resolve()}[mcp]",
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )

        requirements = output_dir / "python-audit-requirements.txt"
        freeze = _run_required_security_command(
            name="python-freeze",
            command=[
                str(candidate_python),
                "-m",
                "pip",
                "freeze",
                "--exclude-editable",
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        requirements.write_text(
            _portable_frozen_requirements(freeze.stdout),
            encoding="utf-8",
        )

        _run_required_security_command(
            name="install-candidate-security-tooling",
            command=[
                str(candidate_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"{wheel.resolve()}[security]",
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        sbom_project = Path(temp_dir) / "candidate-pyproject.toml"
        sbom_project.write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "arcgraph"',
                    f'version = "{EXPECTED_VERSION}"',
                    'requires-python = ">=3.11"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        sbom = output_dir / "arcgraph.cdx.json"
        _run_required_security_command(
            name="cyclonedx-sbom",
            command=[
                str(candidate_python),
                "-m",
                "cyclonedx_py",
                "requirements",
                str(requirements),
                "--pyproject",
                str(sbom_project),
                "--output-reproducible",
                "--output-format",
                "JSON",
                "--output-file",
                str(sbom),
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        pip_audit = output_dir / "pip-audit.json"
        _run_required_security_command(
            name="pip-audit",
            command=[
                str(candidate_python),
                "-m",
                "pip_audit",
                "--strict",
                "--disable-pip",
                "--no-deps",
                "--requirement",
                str(requirements),
                "--format",
                "json",
                "--output",
                str(pip_audit),
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        bandit = output_dir / "bandit.json"
        _run_required_security_command(
            name="bandit",
            command=[
                str(candidate_python),
                "-m",
                "bandit",
                "-c",
                "pyproject.toml",
                "-r",
                "arcgraph",
                "scripts",
                "-x",
                "arcgraph/tests,scripts/tests",
                "-f",
                "json",
                "-o",
                str(bandit),
            ],
            repo=repo,
            timeout_seconds=timeout_seconds,
        )

    npm_version = _run_required_security_command(
        name="npm-audit-cli-version",
        command=["npx", "--yes", NPM_AUDIT_SPEC, "--version"],
        repo=repo,
        timeout_seconds=timeout_seconds,
    )
    if npm_version.stdout.strip() != NPM_AUDIT_VERSION:
        raise RuntimeError("Pinned npm audit CLI reported an unexpected version.")
    npm_audit = output_dir / "npm-audit.json"
    npm_completed = _run_required_security_command(
        name="npm-audit",
        command=[
            "npx",
            "--yes",
            NPM_AUDIT_SPEC,
            "audit",
            "--audit-level=high",
            "--json",
        ],
        repo=repo,
        timeout_seconds=timeout_seconds,
    )
    npm_audit.write_text(npm_completed.stdout, encoding="utf-8")

    for path in (sbom, pip_audit, npm_audit, bandit):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Security evidence is missing or invalid: {path.name}"
            ) from exc
    _require_audit_covers_requirements(pip_audit, requirements)
    summary = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "pass",
        "source_commit_sha": source_provenance["head_sha"],
        "source_tree_sha": source_provenance["tree_sha"],
        "gates": [
            "candidate-wheel-mcp-runtime-environment",
            "candidate-wheel-security-tooling",
            "cyclonedx-sbom",
            "pip-audit-strict",
            "npm-audit-high-pinned-cli",
            "bandit",
        ],
        "files": sorted(
            [
                requirements.name,
                sbom.name,
                pip_audit.name,
                npm_audit.name,
                bandit.name,
            ]
        ),
        "candidate_wheel": {
            "name": wheel.name,
            "sha256": _sha256_file(wheel),
            "size": wheel.stat().st_size,
        },
        "npm_audit_cli": {
            "spec": NPM_AUDIT_SPEC,
            "version": NPM_AUDIT_VERSION,
        },
    }
    _write_json(output_dir / "security-summary.json", summary)
    return summary


def _require_audit_covers_requirements(pip_audit: Path, requirements: Path) -> None:
    """Fail unless the audit report names every pinned requirement.

    ``pip-audit --strict`` exits successfully when it silently leaves out a
    dependency that its own temporary environment already provides, so a clean
    exit does not show that everything pinned was audited.
    """

    def names(values: Iterable[str]) -> set[str]:
        return {re.sub(r"[-_.]+", "-", value).lower() for value in values}

    lines = requirements.read_text(encoding="utf-8").splitlines()
    pinned = names(line.split("==")[0] for line in lines if line.strip())
    report = json.loads(pip_audit.read_text(encoding="utf-8"))
    audited = names(
        str(entry["name"])
        for entry in report["dependencies"]
        if "skip_reason" not in entry
    )
    missing = sorted(pinned - audited)
    if missing:
        raise RuntimeError(
            "The pip-audit report does not cover every pinned requirement: "
            + ", ".join(missing)
        )


def _portable_frozen_requirements(raw_freeze: str) -> str:
    requirements: list[str] = []
    for raw_line in raw_freeze.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        distribution = re.split(r"==| @ ", line, maxsplit=1)[0]
        normalized = re.sub(r"[-_.]+", "-", distribution).lower()
        if normalized == "arcgraph":
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[^\s]+", line):
            raise RuntimeError(
                "Candidate dependency freeze contains a non-portable requirement."
            )
        requirements.append(line)
    if not requirements:
        raise RuntimeError("Candidate dependency freeze is unexpectedly empty.")
    return "\n".join(sorted(requirements, key=str.casefold)) + "\n"


def _venv_python(venv_dir: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_dir / scripts_dir / executable


def _run_required_security_command(
    *,
    name: str,
    command: list[str],
    repo: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    completed = _run_process(
        command,
        cwd=repo,
        timeout=max(timeout_seconds, 300.0),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Security gate {name!r} failed: "
            f"{_tail(completed.stderr) or _tail(completed.stdout)}"
        )
    return completed


def _require_package_candidate(payload: dict[str, Any]) -> None:
    if payload.get("status") != "pass":
        raise RuntimeError("Package readiness payload did not report status=pass.")
    if payload.get("package_readiness_verdict") != "PACKAGE_READY_CANDIDATE":
        raise RuntimeError("Package readiness verdict is not candidate-ready.")
    if payload.get("project_metadata", {}).get("version") != EXPECTED_VERSION:
        raise RuntimeError("Package readiness evidence reports the wrong version.")
    if payload.get("source_provenance", {}).get("working_tree_clean") is not True:
        raise RuntimeError("Package readiness evidence is not from clean source.")
    if payload.get("mcp_protocol", {}).get("status") != "pass":
        raise RuntimeError("Package readiness evidence lacks the MCP protocol gate.")
    if payload.get("mcp_protocol", {}).get("tool_names") != list(
        EXPECTED_DEFAULT_TOOL_NAMES
    ):
        raise RuntimeError(
            "Package readiness evidence lacks the exact default MCP tool contract."
        )
    multi_project = payload.get("multi_project_isolation", {})
    if multi_project.get("status") != "pass":
        raise RuntimeError(
            "Package readiness evidence lacks the multi-project isolation gate."
        )
    if multi_project.get("installed_cli_feedback") is not True:
        raise RuntimeError(
            "Package readiness evidence lacks installed CLI feedback coverage."
        )
    expected_alias_policy = (
        "posix_parent_symlinks_rejected"
        if os.name == "posix"
        else "windows_reparse_not_asserted"
    )
    if multi_project.get("state_alias_policy") != expected_alias_policy:
        raise RuntimeError(
            "Package readiness evidence lacks the expected state-alias policy."
        )
    feedback_tools = multi_project.get("feedback_tool_names")
    if feedback_tools != list(EXPECTED_FEEDBACK_TOOL_NAMES):
        raise RuntimeError(
            "Package readiness evidence lacks the exact feedback-enabled "
            "tool contract."
        )
    if payload.get("persisted_artifacts", {}).get("status") != "pass":
        raise RuntimeError("Package readiness did not persist exact artifacts.")
    if payload.get("failures") not in (None, []):
        raise RuntimeError("Package readiness candidate contains failures.")
    if payload.get("warnings") not in (None, []):
        raise RuntimeError("Package readiness candidate contains unresolved warnings.")


def _public_package_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload["source_provenance"]
    artifacts = payload["artifacts"]
    persisted = payload["persisted_artifacts"]
    platform_payload = payload.get("platform", {})
    python_payload = payload.get("python", {})
    metadata = payload.get("project_metadata", {})
    package_json = payload.get("package_json", {})
    package_contents = payload.get("package_contents", {})
    typescript = payload.get("typescript_degradation", {})
    matrix = payload.get("matrix", {})
    mcp_protocol = payload.get("mcp_protocol", {})
    clients = mcp_protocol.get("clients", {})
    multi_project = payload.get("multi_project_isolation", {})
    return {
        "schema_version": payload["schema_version"],
        "status": payload["status"],
        "package_readiness_verdict": payload["package_readiness_verdict"],
        "commit_sha": payload["commit_sha"],
        "source_provenance": {
            "head_sha": source["head_sha"],
            "tree_sha": source["tree_sha"],
            "working_tree_clean": source["working_tree_clean"],
            "working_tree_status_sha256": source["working_tree_status_sha256"],
            "tracked_package_file_count": len(source.get("tracked_package_files", [])),
            "tracked_symlink_count": len(source.get("tracked_symlinks", [])),
        },
        "platform": {
            "system": platform_payload.get("system"),
            "machine": platform_payload.get("machine"),
        },
        "python": {"host_version": python_payload.get("host_version")},
        "project_metadata": _select_fields(
            metadata,
            (
                "name",
                "version",
                "requires_python",
                "console_script",
                "has_mcp_extra",
                "build_backend",
            ),
        ),
        "package_json": _select_fields(
            package_json,
            ("name", "version", "private", "has_publish_script"),
        ),
        "install_mode": payload.get("install_mode"),
        "mcp_extra_installed": payload.get("mcp_extra_installed"),
        "mcp_version_spec": payload.get("mcp_version_spec"),
        "artifact_dir": "artifacts",
        "artifacts": _select_fields(
            artifacts,
            (
                "commit_sha",
                "source_tree_sha",
                "source_status_sha256",
                "source_working_tree_clean",
                "wheel",
                "wheel_sha256",
                "wheel_size",
                "sdist",
                "sdist_sha256",
                "sdist_size",
                "project_name",
                "version",
                "requires_python",
                "wheel_tag",
                "install_mode",
            ),
        ),
        "package_contents": _select_fields(
            package_contents,
            (
                "status",
                "wheel_file_count",
                "sdist_file_count",
                "required_wheel_files_present",
                "wheel_tests_excluded",
                "sdist_tests_excluded",
            ),
        ),
        "typescript_degradation": _select_fields(
            typescript,
            (
                "status",
                "warning_kind",
                "summary_warning_count",
                "diagnostic_count",
                "project_node_modules_present",
                "pythonpath_cleared",
                "node_path_cleared",
                "node_global_search_paths_disabled",
            ),
        ),
        "mcp_protocol": {
            **_select_fields(
                mcp_protocol,
                (
                    "status",
                    "server_mcp_version",
                    "requested_server_mcp_spec",
                    "legacy_client_spec",
                    "tool_contract_sha256",
                    "tool_names",
                ),
            ),
            "clients": {
                str(name): _select_fields(
                    client,
                    (
                        "client_mcp_version",
                        "client_major",
                        "mode",
                        "protocol_version",
                        "tool_count",
                        "call_count",
                        "clean_shutdown",
                        "server_stderr_empty",
                    ),
                )
                for name, client in sorted(clients.items())
                if isinstance(client, dict)
            },
        },
        "multi_project_isolation": {
            **_select_fields(
                multi_project,
                (
                    "schema_version",
                    "status",
                    "one_installed_environment",
                    "project_count",
                    "server_names_distinct",
                    "output_directories_distinct",
                    "path_authorization_fail_closed",
                    "current_and_graph_state_isolated",
                    "metrics_isolated",
                    "feedback_isolated",
                    "state_alias_policy",
                    "peer_survived_other_server_shutdown",
                    "installation_state_unchanged",
                    "client_configuration_unchanged",
                    "installed_cli_feedback",
                    "permission_model",
                ),
            ),
            "repo_ids": list(multi_project.get("repo_ids", [])),
            "feedback_tool_names": list(multi_project.get("feedback_tool_names", [])),
        },
        "matrix": {
            "executed": list(matrix.get("executed", [])),
            "skipped": list(matrix.get("skipped", [])),
        },
        "persisted_artifacts": {
            "status": persisted["status"],
            "directory": "artifacts",
            **_select_fields(
                persisted,
                (
                    "wheel",
                    "wheel_sha256",
                    "wheel_size",
                    "sdist",
                    "sdist_sha256",
                    "sdist_size",
                ),
            ),
        },
        "failures": [],
        "warnings": [],
        "distribution_redaction": {
            "host_paths": "omitted",
            "command_arguments": "omitted",
            "captured_output": "omitted",
            "temporary_workspace": "omitted",
        },
    }


def _public_release_candidate_evidence(
    payload: dict[str, Any],
    *,
    persisted: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("status") != "pass":
        raise RuntimeError("Release-candidate evidence is not passing.")
    if payload.get("failures") not in (None, []):
        raise RuntimeError("Release-candidate evidence contains failures.")
    if payload.get("warnings") not in (None, []):
        raise RuntimeError("Release-candidate evidence contains unresolved warnings.")
    environment = payload.get("environment", {})
    checks = [check for check in payload.get("checks", []) if isinstance(check, dict)]
    _require_clean_rebuild(checks, persisted=persisted)
    return {
        "schema_version": payload.get("schema_version"),
        "status": "pass",
        "duration_seconds": payload.get("duration_seconds"),
        "environment": {
            "python": environment.get("python"),
            "platform": environment.get("platform"),
        },
        "check_count": len(checks),
        "checks": [_public_check(check) for check in checks],
        "failures": [],
        "warnings": [],
        "distribution_redaction": {
            "host_paths": "omitted",
            "command_arguments": "omitted",
            "captured_output": "omitted",
            "temporary_workspace": "omitted",
        },
    }


def _require_clean_rebuild(
    checks: list[dict[str, Any]],
    *,
    persisted: dict[str, Any],
) -> None:
    """Require proof that the shipped bytes are what a clean build produces.

    A report from a checker that predates the comparison, or one that only
    warned about a difference, must not stand in for it, and the digests it
    compared must be the digests of the files the bundle actually ships.
    """
    rebuilds = [check for check in checks if check.get("name") == REBUILD_CHECK]
    if len(rebuilds) != 1:
        raise RuntimeError(
            "Release-candidate evidence lacks a passing clean-rebuild comparison."
        )
    rebuild: dict[str, Any] = rebuilds[0]
    if rebuild.get("status") != "pass":
        raise RuntimeError(
            "Release-candidate evidence lacks a passing clean-rebuild comparison."
        )
    for role in ("wheel", "sdist"):
        shipped = persisted[f"{role}_sha256"]
        compared = rebuild.get(f"{role}_sha256")
        rebuilt = rebuild.get(f"rebuilt_{role}_sha256")
        if compared != shipped or rebuilt != shipped:
            raise RuntimeError(
                f"The clean-rebuild comparison did not cover the shipped {role}."
            )


def _public_check(check: dict[str, Any]) -> dict[str, Any]:
    fields = ("name", "status", "exit_code", "duration_seconds")
    if check.get("name") == REBUILD_CHECK:
        fields += REBUILD_PUBLIC_FIELDS
    return _select_fields(check, fields)


def _select_fields(
    payload: dict[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {field: payload[field] for field in fields if field in payload}


def _candidate_provenance(
    *,
    version: str,
    package_payload: dict[str, Any],
    rc_payload: dict[str, Any],
    security_payload: dict[str, Any],
    remote_ci_payload: dict[str, Any],
) -> dict[str, Any]:
    source = package_payload["source_provenance"]
    persisted = package_payload["persisted_artifacts"]
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "candidate_version": version,
        "candidate_kind": "local_external_trial_bundle",
        "bundle_root": ".",
        "source": {
            "commit_sha": source["head_sha"],
            "tree_sha": source["tree_sha"],
            "working_tree_clean": source["working_tree_clean"],
            "working_tree_status_sha256": source["working_tree_status_sha256"],
        },
        "artifacts": {
            "wheel": {
                "name": persisted["wheel"],
                "size": persisted["wheel_size"],
                "sha256": persisted["wheel_sha256"],
            },
            "sdist": {
                "name": persisted["sdist"],
                "size": persisted["sdist_size"],
                "sha256": persisted["sdist_sha256"],
            },
        },
        "validation": {
            "package_readiness_status": package_payload["status"],
            "package_readiness_verdict": package_payload["package_readiness_verdict"],
            "mcp_protocol_status": package_payload["mcp_protocol"]["status"],
            "release_candidate_smoke_status": rc_payload["status"],
            "security_evidence_status": security_payload["status"],
            "remote_ci_status": remote_ci_payload["conclusion"],
            "remote_ci_run_id": remote_ci_payload["run_id"],
            "remote_ci_repository": remote_ci_payload["repository"],
        },
        "trial_scope": {
            "included": [
                "Python analysis through the installed CLI",
                "local stdio MCP with a default read-only analysis/change/help surface",
                "optional privacy-bounded local Agent feedback",
            ],
            "excluded": [
                "TypeScript/JavaScript acceptance",
                "package publication",
                "automatic client configuration",
                "remote MCP transport",
            ],
        },
        "external_actions": {
            "scope": "bundle_assembler_execution",
            "performed": [],
            "candidate_lifecycle_evidence": {
                "source_commit_pushed": True,
                "source_commit_pushed_to": "origin/main",
                "origin_main_matches_source": True,
                "remote_ci_verified": True,
                "remote_ci_file": "remote-ci.json",
            },
            "requiring_separate_authorization": [
                "create a tag",
                "create a GitHub Release",
                "publish PyPI/npm/Docker/GHCR artifacts",
                "change repository visibility or GitHub protection settings",
                "configure an agent client",
            ],
        },
        "distribution_redaction": {
            "host_paths": "omitted",
            "command_arguments": "omitted",
            "captured_output": "omitted",
            "temporary_workspace": "omitted",
        },
    }


def _load_remote_ci_evidence(
    path: Path,
    *,
    expected_commit: str,
    expected_repository: tuple[str, str, str],
) -> dict[str, Any]:
    evidence_path = path.expanduser().resolve()
    try:
        raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Remote CI evidence is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Remote CI evidence must be a JSON object.")
    head_sha = _normalized_git_commit(raw.get("headSha"), label="headSha")
    candidate_commit = _normalized_git_commit(
        expected_commit,
        label="candidate commit",
    )
    if head_sha != candidate_commit:
        raise RuntimeError(
            "Remote CI evidence headSha does not match the candidate commit."
        )
    if raw.get("status") != "completed" or raw.get("conclusion") != "success":
        raise RuntimeError("Remote CI run is not completed successfully.")
    if raw.get("headBranch") != "main" or raw.get("event") != "push":
        raise RuntimeError("Remote CI evidence is not a push run for main.")
    run_url = raw.get("url")
    if not isinstance(run_url, str) or not run_url:
        raise RuntimeError("Remote CI evidence is missing its repository-bound URL.")
    run_repository, embedded_run_id = _github_repository_from_run_url(run_url)
    if run_repository != expected_repository:
        raise RuntimeError("Remote CI evidence repository does not match origin.")
    run_id = raw.get("databaseId")
    if not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError("Remote CI evidence is missing a positive databaseId.")
    if embedded_run_id != run_id:
        raise RuntimeError("Remote CI evidence URL run id does not match databaseId.")
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("Remote CI evidence contains no jobs.")
    normalized_jobs: list[dict[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            raise RuntimeError("Remote CI evidence contains an invalid job record.")
        name = job.get("name")
        status = job.get("status")
        conclusion = job.get("conclusion")
        if not all(isinstance(value, str) for value in (name, status, conclusion)):
            raise RuntimeError("Remote CI evidence contains an incomplete job record.")
        if status != "completed" or conclusion != "success":
            raise RuntimeError(f"Remote CI job did not pass: {name}")
        normalized_jobs.append(
            {"name": name, "status": status, "conclusion": conclusion}
        )
    ci_gate = next((job for job in normalized_jobs if job["name"] == "CI Gate"), None)
    if ci_gate is None:
        raise RuntimeError("Remote CI evidence has no successful CI Gate job.")
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "provider": "github_actions",
        "source": "operator_supplied_gh_run_view",
        "repository": {
            "host": run_repository[0],
            "owner": run_repository[1],
            "name": run_repository[2],
        },
        "run_id": run_id,
        "head_sha": head_sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "job_count": len(normalized_jobs),
        "jobs": normalized_jobs,
        "ci_gate": ci_gate,
    }
    for source_key, target_key in (
        ("name", "workflow_name"),
        ("url", "run_url"),
        ("updatedAt", "updated_at"),
    ):
        value = raw.get(source_key)
        if isinstance(value, str) and value:
            result[target_key] = value
    return result


def _normalized_git_commit(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        raise RuntimeError(f"Remote CI evidence {label} is not a 40-digit Git SHA.")
    return value.lower()


def _origin_repository(repo: Path) -> tuple[str, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not read the origin repository URL: {exc}") from exc
    remote_url = completed.stdout.strip()
    if completed.returncode != 0 or not remote_url:
        raise RuntimeError("Could not read the origin repository URL.")
    return _github_repository_from_remote_url(remote_url)


def _github_repository_from_remote_url(value: str) -> tuple[str, str, str]:
    """Return a normalized GitHub repository identity or fail closed.

    Bundle CI evidence is GitHub-Actions-specific, so this accepts the bounded
    Git remote forms GitHub documents: URL syntax and SCP-like SSH syntax.
    Other remote shapes must be made explicit before they can support a bundle
    provenance claim.
    """

    raw = value.strip()
    is_ssh = False
    if "://" not in raw:
        match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):([^\s]+)", raw)
        if match is None:
            raise RuntimeError("Origin is not a supported GitHub repository URL.")
        host = match.group(1).lower()
        path = match.group(2)
        is_ssh = True
    else:
        parsed = urlparse(raw)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise RuntimeError("Origin is not a supported GitHub repository URL.")
        host = parsed.hostname.lower()
        path = parsed.path
        is_ssh = parsed.scheme == "ssh"
    canonical_host = _canonical_github_origin_host(host, allow_ssh_alias=is_ssh)
    return _github_repository_identity(canonical_host, path, source="origin")


def _github_repository_from_run_url(
    value: str,
) -> tuple[tuple[str, str, str], int]:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Remote CI evidence has an invalid GitHub run URL.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 5 or parts[2:4] != ["actions", "runs"] or not parts[4].isdigit():
        raise RuntimeError("Remote CI evidence has an invalid GitHub run URL.")
    return (
        _github_repository_identity(
            "github.com",
            "/".join(parts[:2]),
            source="remote CI evidence",
        ),
        int(parts[4]),
    )


def _canonical_github_origin_host(host: str, *, allow_ssh_alias: bool) -> str:
    """Bind an origin host to GitHub.com without rejecting SSH aliases.

    GitHub Actions run URLs are public GitHub.com evidence.  An SSH remote may
    legitimately use a local ``Host`` alias, so resolve that alias through
    OpenSSH's configuration-only ``-G`` mode and accept it only when the final
    hostname is GitHub.com (including GitHub's documented SSH-over-443 host).
    HTTPS aliases and GitHub Enterprise hosts are intentionally unsupported by
    this bundle contract until their evidence providers are explicit.
    """

    normalized = host.strip().lower().rstrip(".")
    if normalized in {"github.com", "ssh.github.com"}:
        return "github.com"
    if not allow_ssh_alias:
        raise RuntimeError("Origin host is not GitHub.com.")
    try:
        completed = subprocess.run(
            ["ssh", "-G", "--", normalized],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not resolve the origin SSH host alias: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError("Could not resolve the origin SSH host alias.")
    resolved_host: str | None = None
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "hostname":
            resolved_host = parts[1].strip().lower().rstrip(".")
            break
    if resolved_host not in {"github.com", "ssh.github.com"}:
        raise RuntimeError("Origin SSH host alias does not resolve to GitHub.com.")
    return "github.com"


def _github_repository_identity(
    host: str,
    path: str,
    *,
    source: str,
) -> tuple[str, str, str]:
    normalized = path.strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    parts = normalized.split("/")
    if len(parts) != 2 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        raise RuntimeError(f"{source} does not identify one GitHub owner/repository.")
    return host, parts[0].lower(), parts[1].lower()


def _origin_main_commit(repo: Path) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repo),
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not verify local origin/main: {exc}") from exc
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Could not resolve local origin/main for bundle assembly.")
    return commit


def _head_commit(repo: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not resolve the candidate HEAD commit: {exc}"
        ) from exc
    commit = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Could not resolve the candidate HEAD commit.")
    return commit


def _require_candidate_remote_state(repo: Path, candidate_commit: str) -> None:
    if _head_commit(repo) != candidate_commit:
        raise RuntimeError("Candidate HEAD changed during bundle assembly.")
    if _origin_main_commit(repo) != candidate_commit:
        raise RuntimeError(
            "Local origin/main does not match the candidate commit; fetch or "
            "push the exact candidate before assembling the external bundle."
        )
    if _remote_main_commit(repo) != candidate_commit:
        raise RuntimeError(
            "origin/main on the remote does not point at the candidate "
            "commit; the bundle would claim a publication state the remote "
            "does not have."
        )


def _remote_main_commit(repo: Path) -> str:
    """Ask the remote itself what `main` points at.

    ``refs/remotes/origin/main`` is a local, locally-writable ref: it answers
    what this checkout last heard, not what the remote holds now. The bundle
    tells external readers the candidate is on `origin/main`, so that claim
    is resolved against the remote rather than inferred from a tracking ref
    a force-push can leave stale.
    """

    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repo),
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/main",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not query origin for main: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "Could not read refs/heads/main from origin; the external bundle "
            "states the candidate is published there, so that must be checked "
            "against the remote."
        )
    commit = completed.stdout.split("\t", 1)[0].strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Origin returned an unreadable main ref.")
    return commit


def _project_version(repo: Path) -> str:
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError("pyproject.toml does not define project.version.")
    return version


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_sha256_manifest(root: Path) -> None:
    records: list[str] = []
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        if path.name == "SHA256SUMS":
            continue
        records.append(f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text("\n".join(records) + "\n", encoding="utf-8")


def _assert_portable_bundle_text(
    root: Path,
    *,
    forbidden_values: tuple[Path, ...],
) -> None:
    markers: set[str] = set()
    for value in forbidden_values:
        lexical = str(value)
        if not lexical.strip():
            continue
        resolved = value.expanduser().resolve()
        markers.update(
            _portable_bundle_scan_text(candidate)
            for candidate in (
                lexical,
                value.as_posix(),
                str(resolved),
                resolved.as_posix(),
            )
        )
    markers.add(_portable_bundle_scan_text("file://"))
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        if path.suffix.lower() not in {".json", ".md", ".txt"}:
            continue
        text = _portable_bundle_scan_text(path.read_text(encoding="utf-8"))
        for marker in markers:
            if marker and marker in text:
                raise RuntimeError(
                    f"Distributable evidence contains a local path marker: {path.name}"
                )


def _portable_bundle_scan_text(value: str) -> str:
    return re.sub(r"[\\/]+", "/", value).casefold()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _tail(value: str, *, max_chars: int = 1000) -> str:
    stripped = value.strip()
    return stripped if len(stripped) <= max_chars else stripped[-max_chars:]


if __name__ == "__main__":
    raise SystemExit(main())
