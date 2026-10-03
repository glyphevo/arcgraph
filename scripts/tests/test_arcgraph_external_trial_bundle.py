from __future__ import annotations

import importlib.util
import json
import os
import py_compile
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_bundle_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "arcgraph_external_trial_bundle.py"
    spec = importlib.util.spec_from_file_location(
        "arcgraph_external_trial_bundle",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _remote_ci_evidence(root: Path, *, head_sha: str = "a" * 40) -> Path:
    path = root / "remote-ci-input.json"
    path.write_text(
        json.dumps(
            {
                "databaseId": 123456,
                "headSha": head_sha,
                "headBranch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "name": "CI",
                "url": "https://github.com/owner/repo/actions/runs/123456",
                "updatedAt": "2026-08-08T00:00:00Z",
                "jobs": [
                    {
                        "name": "Tests / ubuntu-latest / Python 3.11",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "CI Gate",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _rebuild_check(
    *,
    wheel: Path,
    sdist: Path,
    bundle: Any,
    private_marker: str,
) -> dict[str, Any]:
    wheel_sha256 = bundle._sha256_file(wheel)
    sdist_sha256 = bundle._sha256_file(sdist)
    return {
        "name": "clean-rebuild-identical",
        "status": "pass",
        "wheel": wheel.name,
        "sdist": sdist.name,
        "wheel_sha256": wheel_sha256,
        "sdist_sha256": sdist_sha256,
        "rebuilt_wheel_sha256": wheel_sha256,
        "rebuilt_sdist_sha256": sdist_sha256,
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "python": "3.12.0",
        "platform": "darwin",
        "build_frontend": "1.6.1",
        "build_backend": "hatchling 1.32.4",
        "artifact_build_backend": "hatchling 1.32.4",
        "build_requires": ["hatchling==1.32.4"],
        "detail": private_marker,
    }


def test_external_trial_bundle_script_is_syntax_valid() -> None:
    py_compile.compile(
        str(REPO_ROOT / "scripts" / "arcgraph_external_trial_bundle.py"),
        doraise=True,
    )


def test_external_trial_bundle_assembles_exact_validated_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_bundle_module()
    repo = tmp_path / "repo"
    (repo / "docs" / "release_notes").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "arcgraph"\nversion = "0.1.0rc8"\n',
        encoding="utf-8",
    )
    (repo / bundle.RELEASE_NOTES).write_text("rc8 notes\n", encoding="utf-8")
    (repo / bundle.TRIAL_GUIDE).write_text("trial guide\n", encoding="utf-8")
    (repo / bundle.TRIAL_AGENT_GUIDE).write_text(
        "agent trial guide\n", encoding="utf-8"
    )
    target = tmp_path / "candidate"
    private_marker = "/Users/test/secret-repo"

    def fake_package_smoke(
        *,
        repo: Path,
        artifacts_dir: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del repo, timeout_seconds
        artifacts_dir.mkdir(parents=True)
        wheel = artifacts_dir / "arcgraph-0.1.0rc8-py3-none-any.whl"
        sdist = artifacts_dir / "arcgraph-0.1.0rc8.tar.gz"
        wheel.write_bytes(b"wheel")
        sdist.write_bytes(b"sdist")
        return {
            "schema_version": "1.0",
            "status": "pass",
            "package_readiness_verdict": "PACKAGE_READY_CANDIDATE",
            "commit_sha": "a" * 40,
            "repo_root": private_marker,
            "platform": {
                "system": "Darwin",
                "release": "secret-release",
                "machine": "arm64",
                "platform": private_marker,
            },
            "python": {
                "host_executable": f"{private_marker}/.venv/bin/python",
                "host_version": "3.12.0",
            },
            "project_metadata": {
                "name": "arcgraph",
                "version": "0.1.0rc8",
                "requires_python": ">=3.11",
                "console_script": "arcgraph.interfaces.cli:main",
                "has_mcp_extra": True,
                "build_backend": "hatchling.build",
            },
            "package_json": {
                "name": "arcgraph",
                "version": "0.1.0",
                "private": True,
                "has_publish_script": False,
            },
            "source_provenance": {
                "head_sha": "a" * 40,
                "tree_sha": "b" * 40,
                "working_tree_clean": True,
                "working_tree_status_sha256": "c" * 64,
                "tracked_package_files": ["arcgraph/__init__.py"],
                "tracked_symlinks": [],
            },
            "install_mode": "built-wheel-temporary-venv",
            "mcp_extra_installed": True,
            "mcp_version_spec": "mcp>=2.0.0,<3.0.0",
            "artifact_dir": str(artifacts_dir),
            "artifacts": {
                "commit_sha": "a" * 40,
                "source_tree_sha": "b" * 40,
                "source_status_sha256": "c" * 64,
                "source_working_tree_clean": True,
                "wheel": wheel.name,
                "wheel_sha256": bundle._sha256_file(wheel),
                "wheel_size": wheel.stat().st_size,
                "sdist": sdist.name,
                "sdist_sha256": bundle._sha256_file(sdist),
                "sdist_size": sdist.stat().st_size,
                "project_name": "arcgraph",
                "version": "0.1.0rc8",
                "requires_python": ">=3.11",
                "wheel_tag": "py3-none-any",
                "install_mode": "built-wheel-temporary-venv",
            },
            "package_contents": {
                "status": "pass",
                "wheel_file_count": 100,
                "sdist_file_count": 200,
                "required_wheel_files_present": True,
                "wheel_tests_excluded": True,
                "sdist_tests_excluded": True,
            },
            "typescript_degradation": {
                "status": "pass",
                "warning_kind": "typescript_frontend_unavailable",
                "summary_warning_count": 1,
                "diagnostic_count": 1,
                "project_node_modules_present": False,
                "pythonpath_cleared": True,
                "node_path_cleared": True,
                "node_global_search_paths_disabled": True,
            },
            "mcp_protocol": {
                "status": "pass",
                "server_mcp_version": "2.0.0",
                "requested_server_mcp_spec": "mcp>=2.0.0,<3.0.0",
                "legacy_client_spec": "mcp==1.28.1",
                "tool_contract_sha256": "d" * 64,
                "tool_names": list(bundle.EXPECTED_DEFAULT_TOOL_NAMES),
                "clients": {
                    "mcp-v2-auto-protocol": {
                        "client_mcp_version": "2.0.0",
                        "client_major": 2,
                        "mode": "auto",
                        "protocol_version": "2026-07-28",
                        "tool_count": 14,
                        "call_count": 14,
                        "clean_shutdown": True,
                        "server_stderr_empty": True,
                        "private_probe_path": private_marker,
                    }
                },
            },
            "multi_project_isolation": {
                "schema_version": "1.0",
                "status": "pass",
                "one_installed_environment": True,
                "project_count": 2,
                "repo_ids": ["default", "default"],
                "feedback_tool_names": list(bundle.EXPECTED_FEEDBACK_TOOL_NAMES),
                "installed_cli_feedback": True,
                "server_names_distinct": True,
                "output_directories_distinct": True,
                "path_authorization_fail_closed": True,
                "current_and_graph_state_isolated": True,
                "metrics_isolated": True,
                "feedback_isolated": True,
                "peer_survived_other_server_shutdown": True,
                "installation_state_unchanged": True,
                "client_configuration_unchanged": True,
                "permission_model": (
                    "posix_private_modes_verified"
                    if os.name == "posix"
                    else "windows_acl_not_asserted"
                ),
                "state_alias_policy": (
                    "posix_parent_symlinks_rejected"
                    if os.name == "posix"
                    else "windows_reparse_not_asserted"
                ),
                "private_workspace": private_marker,
            },
            "matrix": {
                "executed": ["local_wheel_and_sdist_build"],
                "skipped": [],
            },
            "persisted_artifacts": {
                "status": "pass",
                "directory": str(artifacts_dir),
                "wheel": wheel.name,
                "wheel_size": wheel.stat().st_size,
                "wheel_sha256": bundle._sha256_file(wheel),
                "sdist": sdist.name,
                "sdist_size": sdist.stat().st_size,
                "sdist_sha256": bundle._sha256_file(sdist),
            },
            "commands": [{"command": [private_marker]}],
            "sample_repo": {"output_dir": private_marker},
            "temp_path": private_marker,
            "failures": [],
            "warnings": [],
        }

    def fake_rc_smoke(
        *,
        repo: Path,
        wheel: Path,
        sdist: Path,
        output: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del repo, timeout_seconds
        payload = {
            "schema_version": "1.2",
            "status": "pass",
            "duration_seconds": 1.25,
            "repo_root": private_marker,
            "wheel": f"{private_marker}/arcgraph.whl",
            "environment": {
                "python": "3.12.0",
                "python_executable": f"{private_marker}/.venv/bin/python",
                "platform": "darwin",
            },
            "checks": [
                _rebuild_check(
                    wheel=wheel,
                    sdist=sdist,
                    bundle=bundle,
                    private_marker=private_marker,
                ),
                {
                    "name": "arcgraph-help",
                    "status": "pass",
                    "command": [private_marker],
                    "cwd": private_marker,
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout_tail": private_marker,
                    "stderr_tail": "",
                },
            ],
            "failures": [],
            "warnings": [],
            "artifacts": {"venv_python": private_marker},
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_security(
        *,
        repo: Path,
        wheel: Path,
        output_dir: Path,
        timeout_seconds: float,
        source_provenance: dict[str, Any],
    ) -> dict[str, Any]:
        del repo, wheel, timeout_seconds
        output_dir.mkdir()
        payload = {
            "status": "pass",
            "source_commit_sha": source_provenance["head_sha"],
            "files": ["security-summary.json"],
        }
        (output_dir / "security-summary.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return payload

    monkeypatch.setattr(bundle, "_run_package_smoke", fake_package_smoke)
    monkeypatch.setattr(bundle, "_run_release_candidate_smoke", fake_rc_smoke)
    monkeypatch.setattr(bundle, "_collect_security_evidence", fake_security)
    monkeypatch.setattr(bundle, "_origin_main_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(bundle, "_remote_main_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(bundle, "_head_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(
        bundle,
        "_origin_repository",
        lambda repo: ("github.com", "owner", "repo"),
    )

    result = bundle.build_external_trial_bundle(
        repo_root=repo,
        bundle_dir=target,
        timeout_seconds=30,
        remote_ci_evidence=_remote_ci_evidence(tmp_path),
    )

    assert result["status"] == "pass"
    assert result["schema_version"] == "1.1"
    assert result["candidate_version"] == "0.1.0rc8"
    assert result["external_actions"] == {
        "scope": "bundle_assembler_execution",
        "performed": [],
    }
    assert (target / "artifacts" / result["wheel"]).read_bytes() == b"wheel"
    assert (target / "artifacts" / result["sdist"]).read_bytes() == b"sdist"
    assert (target / "v0.1.0-rc8.md").read_text(encoding="utf-8") == "rc8 notes\n"
    assert (target / "external-trial-guide.md").read_text(
        encoding="utf-8"
    ) == "trial guide\n"
    assert (target / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    ) == "agent trial guide\n"
    provenance = json.loads((target / "provenance.json").read_text(encoding="utf-8"))
    package = json.loads(
        (target / "package-readiness.json").read_text(encoding="utf-8")
    )
    assert provenance["source"]["commit_sha"] == "a" * 40
    assert provenance["schema_version"] == "1.1"
    assert provenance["validation"]["remote_ci_repository"] == {
        "host": "github.com",
        "owner": "owner",
        "name": "repo",
    }
    assert provenance["external_actions"]["performed"] == []
    assert provenance["external_actions"]["candidate_lifecycle_evidence"] == {
        "source_commit_pushed": True,
        "source_commit_pushed_to": "origin/main",
        "origin_main_matches_source": True,
        "remote_ci_verified": True,
        "remote_ci_file": "remote-ci.json",
    }
    remote_ci = json.loads((target / "remote-ci.json").read_text(encoding="utf-8"))
    assert remote_ci["head_sha"] == "a" * 40
    assert remote_ci["repository"] == {
        "host": "github.com",
        "owner": "owner",
        "name": "repo",
    }
    assert remote_ci["job_count"] == 2
    assert len(remote_ci["jobs"]) == 2
    assert remote_ci["ci_gate"]["name"] == "CI Gate"
    assert provenance["bundle_root"] == "."
    assert package["artifact_dir"] == "artifacts"
    assert package["persisted_artifacts"]["directory"] == "artifacts"
    assert package["multi_project_isolation"]["status"] == "pass"
    assert package["multi_project_isolation"]["installed_cli_feedback"] is True
    assert package["multi_project_isolation"]["repo_ids"] == ["default", "default"]
    assert package["multi_project_isolation"]["feedback_tool_names"][-1] == (
        "arcgraph_record_trial_feedback"
    )
    assert "repo_root" not in package
    rc_evidence = json.loads(
        (target / "release-candidate-smoke.json").read_text(encoding="utf-8")
    )
    rebuild, help_check = rc_evidence["checks"]
    assert help_check == {
        "duration_seconds": 0.1,
        "exit_code": 0,
        "name": "arcgraph-help",
        "status": "pass",
    }
    assert rebuild["name"] == "clean-rebuild-identical"
    assert rebuild["status"] == "pass"
    assert rebuild["wheel_sha256"] == rebuild["rebuilt_wheel_sha256"]
    assert rebuild["sdist_sha256"] == rebuild["rebuilt_sdist_sha256"]
    assert rebuild["build_backend"] == "hatchling 1.32.4"
    assert rebuild["build_requires"] == ["hatchling==1.32.4"]
    assert set(rebuild) <= {
        "name",
        "status",
        "exit_code",
        "duration_seconds",
        *bundle.REBUILD_PUBLIC_FIELDS,
    }
    distributable_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in target.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md", ".txt"}
    )
    assert private_marker not in distributable_text
    assert str(repo) not in distributable_text
    assert str(target) not in distributable_text
    manifest = (target / "SHA256SUMS").read_text(encoding="utf-8")
    assert "artifacts/arcgraph-0.1.0rc8-py3-none-any.whl" in manifest
    assert "artifacts/arcgraph-0.1.0rc8.tar.gz" in manifest
    assert "package-readiness.json" in manifest
    assert "release-candidate-smoke.json" in manifest
    assert "remote-ci.json" in manifest
    assert "provenance.json" in manifest
    assert "security/security-summary.json" in manifest
    assert "SHA256SUMS" not in manifest

    with pytest.raises(RuntimeError, match="already exists"):
        bundle.build_external_trial_bundle(
            repo_root=repo,
            bundle_dir=target,
            timeout_seconds=30,
            remote_ci_evidence=_remote_ci_evidence(tmp_path),
        )


def test_external_trial_bundle_requires_rc8_identity(tmp_path: Path) -> None:
    bundle = load_bundle_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "arcgraph"\nversion = "0.1.0rc2"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="requires 0.1.0rc8"):
        bundle.build_external_trial_bundle(
            repo_root=repo,
            bundle_dir=tmp_path / "bundle",
            timeout_seconds=30,
            remote_ci_evidence=_remote_ci_evidence(tmp_path),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(headSha="b" * 40), "headSha"),
        (
            lambda payload: payload["jobs"][0].update(conclusion="failure"),
            "did not pass",
        ),
        (
            lambda payload: payload["jobs"][0].update(conclusion="skipped"),
            "did not pass",
        ),
        (
            lambda payload: payload.update(headBranch="feature"),
            "not a push run for main",
        ),
        (
            lambda payload: payload.update(
                jobs=[job for job in payload["jobs"] if job["name"] != "CI Gate"]
            ),
            "no successful CI Gate",
        ),
        (
            lambda payload: payload.update(
                url="https://github.com/other/repo/actions/runs/123456"
            ),
            "repository does not match",
        ),
        (
            lambda payload: payload.update(
                url="https://github.com/owner/repo/actions/runs/654321"
            ),
            "run id does not match",
        ),
        (
            lambda payload: payload.update(
                url="https://not-github.example/owner/repo/actions/runs/123456"
            ),
            "invalid GitHub run URL",
        ),
        (lambda payload: payload.pop("url"), "missing its repository-bound URL"),
    ],
)
def test_remote_ci_evidence_fails_closed(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    bundle = load_bundle_module()
    evidence = _remote_ci_evidence(tmp_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    mutation(payload)
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        bundle._load_remote_ci_evidence(
            evidence,
            expected_commit="a" * 40,
            expected_repository=("github.com", "owner", "repo"),
        )


def test_remote_ci_evidence_requires_the_canonical_github_host(tmp_path: Path) -> None:
    bundle = load_bundle_module()

    payload = bundle._load_remote_ci_evidence(
        _remote_ci_evidence(tmp_path),
        expected_commit="a" * 40,
        expected_repository=("github.com", "owner", "repo"),
    )

    assert payload["repository"] == {
        "host": "github.com",
        "owner": "owner",
        "name": "repo",
    }


def test_remote_ci_evidence_normalizes_git_sha_case(tmp_path: Path) -> None:
    bundle = load_bundle_module()

    payload = bundle._load_remote_ci_evidence(
        _remote_ci_evidence(tmp_path, head_sha="A" * 40),
        expected_commit="a" * 40,
        expected_repository=("github.com", "owner", "repo"),
    )

    assert payload["head_sha"] == "a" * 40


def test_invalid_remote_evidence_fails_before_package_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_bundle_module()
    repo = tmp_path / "repo"
    (repo / "docs" / "release_notes").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "arcgraph"\nversion = "0.1.0rc8"\n',
        encoding="utf-8",
    )
    for relative in (
        bundle.RELEASE_NOTES,
        bundle.TRIAL_GUIDE,
        bundle.TRIAL_AGENT_GUIDE,
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("candidate document\n", encoding="utf-8")
    evidence = _remote_ci_evidence(tmp_path, head_sha="b" * 40)
    package_smoke_called = False

    def unexpected_package_smoke(**kwargs: Any) -> dict[str, Any]:
        nonlocal package_smoke_called
        package_smoke_called = True
        raise AssertionError("package smoke must not run after failed preflight")

    monkeypatch.setattr(bundle, "_head_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(
        bundle,
        "_origin_repository",
        lambda repo: ("github.com", "owner", "repo"),
    )
    monkeypatch.setattr(bundle, "_run_package_smoke", unexpected_package_smoke)

    with pytest.raises(RuntimeError, match="headSha"):
        bundle.build_external_trial_bundle(
            repo_root=repo,
            bundle_dir=tmp_path / "bundle",
            timeout_seconds=30,
            remote_ci_evidence=evidence,
        )

    assert package_smoke_called is False


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/Owner/Repo.git",
        "ssh://git@github.com/Owner/Repo.git",
        "git@github.com:Owner/Repo.git",
    ],
)
def test_github_repository_identity_accepts_documented_git_remote_forms(
    remote_url: str,
) -> None:
    bundle = load_bundle_module()

    assert bundle._github_repository_from_remote_url(remote_url) == (
        "github.com",
        "owner",
        "repo",
    )


@pytest.mark.parametrize(
    "remote_url",
    [
        "/local/repo",
        "file:///local/repo",
        "http://github.com/owner/repo.git",
        "https://not-github.example/owner/repo.git",
        "https://github.com/owner/extra/repo.git",
    ],
)
def test_github_repository_identity_rejects_unbound_remote_forms(
    remote_url: str,
) -> None:
    bundle = load_bundle_module()

    with pytest.raises(RuntimeError, match="supported GitHub|GitHub.com|one GitHub"):
        bundle._github_repository_from_remote_url(remote_url)


def test_github_repository_identity_resolves_ssh_alias_to_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_bundle_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "host github-work\nhostname ssh.github.com\nuser git\n",
            "",
        )

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)

    assert bundle._github_repository_from_remote_url(
        "git@github-work:Owner/Repo.git"
    ) == ("github.com", "owner", "repo")
    assert calls == [["ssh", "-G", "--", "github-work"]]


def test_github_repository_identity_rejects_ssh_alias_for_another_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_bundle_module()
    monkeypatch.setattr(
        bundle.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "hostname git.example.com\n", ""
        ),
    )

    with pytest.raises(RuntimeError, match="does not resolve to GitHub.com"):
        bundle._github_repository_from_remote_url("git@work:owner/repo.git")


def test_remote_main_commit_asks_the_remote_not_a_local_tracking_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle publishes `source_commit_pushed`, and `refs/remotes/origin/main`
    is a local ref a force-push can leave stale, so the claim is resolved
    against the remote."""

    bundle = load_bundle_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, f"{'b' * 40}\trefs/heads/main\n", ""
        )

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    assert bundle._remote_main_commit(tmp_path) == "b" * 40
    assert "ls-remote" in calls[0]
    assert "origin" in calls[0]
    assert "refs/heads/main" in calls[0]

    def unreachable(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "fatal: could not read")

    monkeypatch.setattr(bundle.subprocess, "run", unreachable)
    with pytest.raises(RuntimeError, match="Could not read refs/heads/main"):
        bundle._remote_main_commit(tmp_path)

    def garbled(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, "not-a-sha\trefs/heads/main\n", ""
        )

    monkeypatch.setattr(bundle.subprocess, "run", garbled)
    with pytest.raises(RuntimeError, match="unreadable main ref"):
        bundle._remote_main_commit(tmp_path)


def test_bundle_candidate_requires_multi_project_and_cli_feedback_evidence() -> None:
    bundle = load_bundle_module()
    payload = {
        "status": "pass",
        "package_readiness_verdict": "PACKAGE_READY_CANDIDATE",
        "project_metadata": {"version": "0.1.0rc8"},
        "source_provenance": {"working_tree_clean": True},
        "mcp_protocol": {
            "status": "pass",
            "tool_names": list(bundle.EXPECTED_DEFAULT_TOOL_NAMES),
        },
        "multi_project_isolation": {
            "status": "pass",
            "installed_cli_feedback": True,
            "feedback_tool_names": list(bundle.EXPECTED_FEEDBACK_TOOL_NAMES),
            "state_alias_policy": (
                "posix_parent_symlinks_rejected"
                if os.name == "posix"
                else "windows_reparse_not_asserted"
            ),
        },
        "persisted_artifacts": {"status": "pass"},
        "failures": [],
        "warnings": [],
    }

    bundle._require_package_candidate(payload)
    payload["multi_project_isolation"]["feedback_tool_names"][
        0
    ] = "arcgraph_unexpected_replacement"
    with pytest.raises(RuntimeError, match="exact feedback-enabled tool contract"):
        bundle._require_package_candidate(payload)
    payload["multi_project_isolation"]["feedback_tool_names"] = list(
        bundle.EXPECTED_FEEDBACK_TOOL_NAMES
    )
    payload["multi_project_isolation"]["installed_cli_feedback"] = False
    with pytest.raises(RuntimeError, match="installed CLI feedback"):
        bundle._require_package_candidate(payload)


def test_portable_frozen_requirements_excludes_candidate_and_rejects_urls() -> None:
    bundle = load_bundle_module()
    freeze = "\n".join(
        [
            "zipp==3.23.0",
            "arcgraph @ file:///private/tmp/arcgraph.whl",
            "MCP==2.0.0",
            "",
        ]
    )

    assert bundle._portable_frozen_requirements(freeze) == (
        "MCP==2.0.0\nzipp==3.23.0\n"
    )
    with pytest.raises(RuntimeError, match="non-portable requirement"):
        bundle._portable_frozen_requirements(
            "safe==1.0\nother @ https://example.invalid/other.whl\n"
        )


@pytest.mark.parametrize(
    ("serialized_path", "forbidden_path"),
    [
        ("/Users/test/secret-repo", Path("/Users/test/secret-repo")),
        (r"C:\Users\Test\Secret-Repo", Path("c:/users/test/secret-repo")),
        (r"C:\Users\Test/Secret-Repo", Path("C:/Users/Test/Secret-Repo")),
    ],
)
def test_portable_bundle_guard_rejects_cross_platform_local_path_markers(
    tmp_path: Path,
    serialized_path: str,
    forbidden_path: Path,
) -> None:
    bundle = load_bundle_module()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps({"repo": serialized_path}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="local path marker"):
        bundle._assert_portable_bundle_text(
            tmp_path,
            forbidden_values=(forbidden_path,),
        )


def test_security_evidence_freezes_candidate_runtime_before_installing_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_bundle_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    wheel = tmp_path / "arcgraph-0.1.0rc8-py3-none-any.whl"
    wheel.write_bytes(b"candidate-wheel")
    output_dir = tmp_path / "security"
    observed: list[tuple[str, list[str]]] = []

    def fake_security_command(
        *,
        name: str,
        command: list[str],
        repo: Path,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del repo, timeout_seconds
        observed.append((name, command))
        stdout = ""
        if name == "python-freeze":
            stdout = (
                f"arcgraph @ file://{wheel}\n"
                "mcp==2.0.0\n"
                "typing_extensions==4.15.0\n"
            )
        elif name == "cyclonedx-sbom":
            output = Path(command[command.index("--output-file") + 1])
            output.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "component": {
                                "name": "arcgraph",
                                "version": "0.1.0rc8",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
        elif name == "pip-audit":
            output = Path(command[command.index("--output") + 1])
            audited = [
                {"name": "mcp", "version": "2.0.0", "vulns": []},
                {"name": "Typing-Extensions", "version": "4.15.0", "vulns": []},
            ]
            output.write_text(json.dumps({"dependencies": audited}), encoding="utf-8")
        elif name == "bandit":
            output = Path(command[command.index("-o") + 1])
            output.write_text('{"results": []}', encoding="utf-8")
        elif name == "npm-audit-cli-version":
            stdout = "11.12.1\n"
        elif name == "npm-audit":
            stdout = '{"metadata": {"vulnerabilities": {"high": 0}}}'
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        bundle,
        "_run_required_security_command",
        fake_security_command,
    )

    result = bundle._collect_security_evidence(
        repo=repo,
        wheel=wheel,
        output_dir=output_dir,
        timeout_seconds=30,
        source_provenance={"head_sha": "a" * 40, "tree_sha": "b" * 40},
    )

    names = [name for name, _ in observed]
    assert names.index("install-candidate-mcp-runtime") < names.index("python-freeze")
    assert names.index("python-freeze") < names.index(
        "install-candidate-security-tooling"
    )
    assert names.index("npm-audit-cli-version") < names.index("npm-audit")
    audit_command = dict(observed)["pip-audit"]
    assert audit_command[audit_command.index("--strict") :][:3] == [
        "--strict",
        "--disable-pip",
        "--no-deps",
    ]
    runtime_install = dict(observed)["install-candidate-mcp-runtime"]
    tooling_install = dict(observed)["install-candidate-security-tooling"]
    assert runtime_install[-1].endswith(".whl[mcp]")
    assert tooling_install[-1].endswith(".whl[security]")
    requirements = (output_dir / "python-audit-requirements.txt").read_text(
        encoding="utf-8"
    )
    assert requirements == "mcp==2.0.0\ntyping_extensions==4.15.0\n"
    assert "file://" not in requirements
    assert result["candidate_wheel"] == {
        "name": wheel.name,
        "sha256": bundle._sha256_file(wheel),
        "size": wheel.stat().st_size,
    }
    assert result["npm_audit_cli"] == {
        "spec": "npm@11.12.1",
        "version": "11.12.1",
    }


def _audit_coverage(tmp_path: Path, pinned: str, dependencies: list[Any]) -> None:
    bundle = load_bundle_module()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(pinned, encoding="utf-8")
    report = tmp_path / "pip-audit.json"
    report.write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")
    bundle._require_audit_covers_requirements(report, requirements)


def test_audit_coverage_accepts_a_report_naming_every_pinned_requirement(
    tmp_path: Path,
) -> None:
    _audit_coverage(
        tmp_path,
        "packaging==26.3\ntyping_extensions==4.15.0\n",
        [{"name": "Packaging"}, {"name": "typing-extensions"}],
    )


def test_audit_coverage_rejects_a_pinned_requirement_the_report_omits(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="does not cover every pinned.*packaging"):
        _audit_coverage(
            tmp_path,
            "packaging==26.3\nmcp==2.0.0\n",
            [{"name": "mcp"}],
        )


def test_audit_coverage_rejects_a_dependency_the_audit_skipped(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="mcp"):
        _audit_coverage(
            tmp_path,
            "mcp==2.0.0\n",
            [{"name": "mcp", "skip_reason": "not on PyPI"}],
        )


def test_external_trial_bundle_rejects_a_dangling_destination_symlink(
    tmp_path: Path,
) -> None:
    bundle = load_bundle_module()
    target = tmp_path / "candidate"
    try:
        target.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("This platform does not permit symlink creation.")

    with pytest.raises(RuntimeError, match="already exists"):
        bundle.build_external_trial_bundle(
            repo_root=tmp_path,
            bundle_dir=target,
            timeout_seconds=30,
            remote_ci_evidence=_remote_ci_evidence(tmp_path),
        )


def _rc_evidence(bundle: Any, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    wheel = tmp_path / "arcgraph-0.1.0rc8-py3-none-any.whl"
    sdist = tmp_path / "arcgraph-0.1.0rc8.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    persisted = {
        "wheel_sha256": bundle._sha256_file(wheel),
        "sdist_sha256": bundle._sha256_file(sdist),
    }
    payload = {
        "schema_version": "1.2",
        "status": "pass",
        "duration_seconds": 1.0,
        "environment": {"python": "3.12.0", "platform": "darwin"},
        "checks": [
            _rebuild_check(
                wheel=wheel, sdist=sdist, bundle=bundle, private_marker="omitted"
            )
        ],
        "failures": [],
        "warnings": [],
    }
    return payload, persisted


def test_bundle_evidence_keeps_the_rebuild_facts_but_not_its_free_text(
    tmp_path: Path,
) -> None:
    bundle = load_bundle_module()
    payload, persisted = _rc_evidence(bundle, tmp_path)

    public = bundle._public_release_candidate_evidence(payload, persisted=persisted)

    (check,) = public["checks"]
    assert check["build_backend"] == "hatchling 1.32.4"
    assert "detail" not in check


@pytest.mark.parametrize(
    "mutation",
    [
        "no_rebuild_check",
        "rebuild_warned",
        "rebuild_failed",
        "rebuild_check_twice",
        "wheel_digest_differs_from_shipped",
        "sdist_rebuilt_digest_differs_from_shipped",
        "rebuilt_wheel_digest_missing",
    ],
)
def test_bundle_refuses_evidence_without_a_passing_matching_clean_rebuild(
    tmp_path: Path, mutation: str
) -> None:
    bundle = load_bundle_module()
    payload, persisted = _rc_evidence(bundle, tmp_path)
    (check,) = payload["checks"]
    if mutation == "no_rebuild_check":
        payload["checks"] = []
    elif mutation == "rebuild_warned":
        check["status"] = "warn"
    elif mutation == "rebuild_failed":
        check["status"] = "fail"
    elif mutation == "rebuild_check_twice":
        payload["checks"] = [check, dict(check)]
    elif mutation == "wheel_digest_differs_from_shipped":
        check["wheel_sha256"] = "0" * 64
    elif mutation == "sdist_rebuilt_digest_differs_from_shipped":
        check["rebuilt_sdist_sha256"] = "0" * 64
    else:
        del check["rebuilt_wheel_sha256"]

    with pytest.raises(RuntimeError, match="clean-rebuild"):
        bundle._public_release_candidate_evidence(payload, persisted=persisted)


def test_bundle_hands_the_persisted_sdist_to_the_candidate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = load_bundle_module()
    wheel = tmp_path / "arcgraph-0.1.0rc8-py3-none-any.whl"
    sdist = tmp_path / "arcgraph-0.1.0rc8.tar.gz"
    output = tmp_path / "rc.json"
    seen: list[list[str]] = []

    def fake_process(
        command: list[str], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        seen.append(command)
        output.write_text('{"status": "pass"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(bundle, "_run_process", fake_process)

    bundle._run_release_candidate_smoke(
        repo=tmp_path,
        wheel=wheel,
        sdist=sdist,
        output=output,
        timeout_seconds=30,
    )

    (command,) = seen
    assert command[command.index("--wheel") + 1] == str(wheel)
    assert command[command.index("--sdist") + 1] == str(sdist)
