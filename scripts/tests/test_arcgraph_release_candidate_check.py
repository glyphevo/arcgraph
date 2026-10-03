from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest


def load_rc_module() -> Any:
    script_path = (
        Path(__file__).resolve().parents[1] / "arcgraph_release_candidate_check.py"
    )
    spec = importlib.util.spec_from_file_location(
        "arcgraph_release_candidate_check", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrackingTemporaryDirectory:
    created_paths: list[Path] = []

    def __init__(self, *, base: Path) -> None:
        self.base = base
        self.name = str(base / f"temp-{len(self.created_paths)}")
        self.path = Path(self.name)

    def __enter__(self) -> str:
        self.path.mkdir(parents=True)
        self.created_paths.append(self.path)
        return self.name

    def __exit__(self, *_exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def fake_create_venv(venv_dir: Path) -> None:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    bin_dir = venv_dir / scripts_dir
    bin_dir.mkdir(parents=True)
    (bin_dir / f"python{suffix}").write_text("", encoding="utf-8")
    (bin_dir / f"arcgraph{suffix}").write_text("", encoding="utf-8")


HEAD_COMMIT = "3f828b0a14a26d09322cbf8c1f51abaf7273b306"
HEAD_TREE = "fcdae18eeb4091e87414b6b756cfe2c8c476f191"
OTHER_COMMIT = "75cc1bc950fb29b93b73d119356e9ccc91d55d6a"
OTHER_TREE = "6313c01dff3097c04730989d71e848db7ea6a0f3"


def write_wheel(
    path: Path,
    *,
    commit_sha: str | None,
    tree_sha: str | None,
    working_tree_clean: bool | None = True,
) -> Path:
    """Build a minimal wheel, optionally carrying embedded build provenance."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("arcgraph/__init__.py", "")
        if commit_sha is not None and tree_sha is not None:
            source: dict[str, Any] = {
                "commit_sha": commit_sha,
                "tree_sha": tree_sha,
            }
            if working_tree_clean is not None:
                source["working_tree_clean"] = working_tree_clean
            archive.writestr(
                "arcgraph/_build_provenance.json",
                json.dumps(
                    {
                        "build_version": "0.1.0rc6",
                        "schema_version": "1.0.0",
                        "source": source,
                    }
                ),
            )
    return path


def write_sdist(directory: Path) -> Path:
    path = directory / "arcgraph-0.1.0rc6.tar.gz"
    path.write_bytes(b"sdist bytes")
    return path


REBUILD_FACTS = {
    "python": "3.12.0",
    "platform": "darwin",
    "build_frontend": "1.6.1",
    "build_backend": "hatchling 1.32.4",
    "artifact_build_backend": "hatchling 1.32.4",
    "build_requires": ["hatchling==1.32.4"],
    "rebuilt_wheel_sha256": "a" * 64,
    "rebuilt_sdist_sha256": "b" * 64,
}


def fake_rebuild(
    problems: list[str] | None = None, facts: dict[str, Any] | None = None
) -> Any:
    calls: list[str] = []

    def _rebuild(
        repo: Path,
        *,
        commit_sha: str,
        artifacts: dict[str, Path],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert repo.is_dir()
        assert set(artifacts) == {"wheel", "sdist"}
        assert timeout_seconds > 0
        calls.append(commit_sha)
        return {
            "facts": dict(REBUILD_FACTS if facts is None else facts),
            "problems": list(problems or []),
        }

    _rebuild.calls = calls  # type: ignore[attr-defined]
    return _rebuild


def bind_head(monkeypatch: Any, rc: Any) -> None:
    """Pin the repository side so tests never depend on the ambient checkout."""
    monkeypatch.setattr(rc, "_repo_head_provenance", fake_head(HEAD_COMMIT, HEAD_TREE))
    monkeypatch.setattr(rc, "_rebuild_in_clean_clone", fake_rebuild())


def fake_head(commit_sha: str, tree_sha: str) -> Any:
    def _head(repo: Path) -> dict[str, str]:
        assert repo.is_dir()
        return {"commit_sha": commit_sha, "tree_sha": tree_sha}

    return _head


def create_sample_fixture(repo_root: Path) -> None:
    fixture_root = repo_root / "arcgraph" / "tests" / "fixtures" / "sample_project"
    for relative in ("src/pkg", "tests"):
        (fixture_root / relative).mkdir(parents=True, exist_ok=True)
    (fixture_root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (fixture_root / "tests" / "service_cases.py").write_text(
        "def test_placeholder():\n    assert True\n",
        encoding="utf-8",
    )


class FakeRunner:
    def __init__(
        self,
        *,
        fail_when: str | None = None,
        timeout_when: str | None = None,
    ) -> None:
        self.fail_when = fail_when
        self.timeout_when = timeout_when
        self.calls: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout > 0
        assert env["PYTHONUTF8"] == "1"
        self.calls.append(command)
        command_text = " ".join(command)
        if self.timeout_when and self.timeout_when in command_text:
            raise subprocess.TimeoutExpired(command, timeout)
        if self.fail_when and self.fail_when in command_text:
            return subprocess.CompletedProcess(
                command, 2, stdout="", stderr="simulated failure"
            )
        if "benchmark suite" in command_text:
            _write_output_arg(command, {"status": "pass"})
        if "visual workbench" in command_text:
            _write_workbench_arg(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _write_output_arg(command: list[str], payload: dict[str, Any]) -> None:
    output = Path(command[command.index("--output") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(str(payload), encoding="utf-8")


def _write_workbench_arg(command: list[str]) -> None:
    workbench_dir = Path(command[command.index("--output-dir") + 1])
    (workbench_dir / "vendor").mkdir(parents=True, exist_ok=True)
    for relative in (
        "index.html",
        "graph_data.json",
        "status_data.json",
        "vendor/d3.v7.min.js",
        "vendor/LICENSE.d3.txt",
    ):
        (workbench_dir / relative).write_text("", encoding="utf-8")


def test_release_candidate_check_runs_expected_smoke_and_cleans_temp(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    output = tmp_path / "rc-smoke.json"
    temp_base = tmp_path / "tempdirs"
    tracker = TrackingTemporaryDirectory
    tracker.created_paths = []

    def tempdir_factory(prefix: str) -> TrackingTemporaryDirectory:
        assert prefix == "arcgraph-rc-smoke-"
        return TrackingTemporaryDirectory(base=temp_base)

    monkeypatch.setattr(rc.tempfile, "TemporaryDirectory", tempdir_factory)
    bind_head(monkeypatch, rc)
    runner = FakeRunner()
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        output_path=output,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["schema_version"] == "1.2"
    assert payload["status"] == "pass"
    assert output.exists()
    check_names = [check["name"] for check in payload["checks"]]
    assert check_names == [
        "wheel-source-provenance",
        "clean-rebuild-identical",
        "create-venv",
        "install-wheel",
        "arcgraph-help",
        "docs-quickstart",
        "docs-security-model",
        "docs-release-checklist",
        "docs-schema-governance",
        "docs-frontend-contract",
        "docs-limitations",
        "docs-evidence-cookbook",
        "repo-current",
        "benchmark-suite",
        "fixture-build",
        "fixture-current",
        "fixture-workbench",
        "fixture-workbench-assets",
    ]
    assert any("benchmark" in " ".join(call) for call in runner.calls)
    assert any("visual workbench" in " ".join(call) for call in runner.calls)
    assert tracker.created_paths
    assert all(not path.exists() for path in tracker.created_paths)


def test_release_candidate_check_returns_nonzero_on_command_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    runner = FakeRunner(fail_when="--help")
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["failures"] == [
        {"check": "arcgraph-help", "message": "simulated failure"}
    ]
    failed = next(
        check for check in payload["checks"] if check["name"] == "arcgraph-help"
    )
    assert failed["exit_code"] == 2


def test_release_candidate_check_records_timeout_diagnostic(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    runner = FakeRunner(timeout_when="current")
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    failed = next(
        check for check in payload["checks"] if check["name"] == "repo-current"
    )
    assert failed["exit_code"] == 124
    assert failed["error"] == "Timed out after 5s."
    assert payload["failures"] == [
        {"check": "repo-current", "message": "Timed out after 5s."}
    ]


def test_release_candidate_check_rejects_missing_wheel(tmp_path: Path) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=tmp_path / "missing.whl",
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["checks"] == []
    assert payload["failures"][0]["check"] == "validation"


def test_release_candidate_check_rejects_wheel_built_from_another_commit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A shared version label must never let evidence describe other bytes."""
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=OTHER_COMMIT,
        tree_sha=OTHER_TREE,
    )
    bind_head(monkeypatch, rc)
    runner = FakeRunner()
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["failures"][0]["check"] == "wheel-source-provenance"
    assert [check["name"] for check in payload["checks"]] == ["wheel-source-provenance"]
    assert runner.calls == []
    check = payload["checks"][0]
    assert check["wheel_commit_sha"] == OTHER_COMMIT
    assert check["repo_commit_sha"] == HEAD_COMMIT


def test_release_candidate_check_accepts_wheel_built_from_repo_head(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["warnings"] == []
    check = payload["checks"][0]
    assert check["name"] == "wheel-source-provenance"
    assert check["status"] == "pass"
    assert payload["artifacts"]["wheel_sha256"] == check["wheel_sha256"]


def test_release_candidate_check_allows_disclosed_source_mismatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc5-py3-none-any.whl",
        commit_sha=OTHER_COMMIT,
        tree_sha=OTHER_TREE,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
        allow_source_mismatch=True,
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    check = payload["checks"][0]
    assert check["status"] == "warn"
    assert check["allowed_by"] == "--allow-source-mismatch"
    assert payload["warnings"][0]["check"] == "wheel-source-provenance"


def test_release_candidate_check_warns_when_provenance_is_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An unbindable wheel is disclosed, never silently reported as bound."""
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=None,
        tree_sha=None,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    check = payload["checks"][0]
    assert check["status"] == "warn"
    assert check["wheel_commit_sha"] is None
    assert payload["warnings"][0]["check"] == "wheel-source-provenance"


def test_release_candidate_check_ignores_a_non_git_repo_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    monkeypatch.setattr(rc, "_repo_head_provenance", lambda repo: None)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    assert payload["checks"][0]["status"] == "warn"
    assert payload["warnings"][0]["check"] == "wheel-source-provenance"


def test_release_candidate_check_rejects_a_wheel_built_from_a_dirty_tree(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Matching a commit is not the same as being the bytes that commit describes."""
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
        working_tree_clean=False,
    )
    bind_head(monkeypatch, rc)
    runner = FakeRunner()
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["failures"][0]["check"] == "wheel-source-provenance"
    assert "unclean build working tree" in payload["failures"][0]["message"]
    check = payload["checks"][0]
    assert check["wheel_commit_sha"] == HEAD_COMMIT
    assert check["wheel_working_tree_clean"] is False
    assert runner.calls == []


def test_release_candidate_check_rejects_provenance_without_a_clean_claim(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An absent cleanliness claim is unknown, never assumed clean."""
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
        working_tree_clean=None,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 1
    assert payload["checks"][0]["wheel_working_tree_clean"] is None
    assert "no working-tree cleanliness claim" in payload["failures"][0]["message"]


def test_release_candidate_check_allows_a_disclosed_dirty_build(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
        working_tree_clean=False,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
        allow_source_mismatch=True,
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    assert payload["checks"][0]["allowed_by"] == "--allow-source-mismatch"


def test_release_candidate_check_keeps_a_clean_run_free_of_warnings(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A clean bound run must stay distinguishable from a disclosed bypass."""
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
        working_tree_clean=True,
    )
    bind_head(monkeypatch, rc)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["warnings"] == []
    assert payload["checks"][0]["status"] == "pass"


def _run_with_fakes(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    rebuild: Any,
    allow_source_mismatch: bool = False,
    with_sdist: bool = True,
) -> tuple[Any, int, dict[str, Any], FakeRunner]:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    sdist = write_sdist(tmp_path)
    if not with_sdist:
        sdist.unlink()
    monkeypatch.setattr(rc, "_repo_head_provenance", fake_head(HEAD_COMMIT, HEAD_TREE))
    monkeypatch.setattr(rc, "_rebuild_in_clean_clone", rebuild)
    runner = FakeRunner()
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", runner)
    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=sdist,
        repo_root=repo_root,
        timeout_seconds=5,
        allow_source_mismatch=allow_source_mismatch,
    )
    return rc, exit_code, payload, runner


def test_sdist_argument_is_required() -> None:
    rc = load_rc_module()

    try:
        rc.build_parser().parse_args(["--wheel", "arcgraph.whl"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - the assertion is the failure path
        raise AssertionError("--sdist must be required")


def test_clean_rebuild_check_records_the_files_and_the_tools(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rebuild = fake_rebuild()
    _, exit_code, payload, _ = _run_with_fakes(tmp_path, monkeypatch, rebuild=rebuild)

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert rebuild.calls == [HEAD_COMMIT]
    check = payload["checks"][1]
    assert check["name"] == "clean-rebuild-identical"
    assert check["status"] == "pass"
    assert check["commit_sha"] == HEAD_COMMIT
    assert check["tree_sha"] == HEAD_TREE
    assert check["wheel"] == "arcgraph-0.1.0rc6-py3-none-any.whl"
    assert check["sdist"] == "arcgraph-0.1.0rc6.tar.gz"
    assert len(check["wheel_sha256"]) == 64
    assert check["sdist_sha256"] == payload["artifacts"]["sdist_sha256"]
    assert check["build_backend"] == "hatchling 1.32.4"
    assert check["build_requires"] == ["hatchling==1.32.4"]


def test_a_rebuild_difference_never_passes_and_stops_before_the_install(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rebuild = fake_rebuild(["The wheel differs from the rebuild."])
    _, exit_code, payload, runner = _run_with_fakes(
        tmp_path, monkeypatch, rebuild=rebuild
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert [check["name"] for check in payload["checks"]] == [
        "wheel-source-provenance",
        "clean-rebuild-identical",
    ]
    assert payload["checks"][1]["status"] == "fail"
    assert payload["failures"][0]["check"] == "clean-rebuild-identical"
    assert "The wheel differs from the rebuild." in payload["failures"][0]["message"]
    assert runner.calls == []


def test_a_rebuild_without_both_hashes_never_passes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    facts = {
        key: value
        for key, value in REBUILD_FACTS.items()
        if key != "rebuilt_sdist_sha256"
    }
    rebuild = fake_rebuild(facts=facts)
    _, exit_code, payload, runner = _run_with_fakes(
        tmp_path, monkeypatch, rebuild=rebuild
    )

    assert exit_code == 1
    assert payload["checks"][1]["status"] == "fail"
    assert "No rebuilt sdist was hashed" in payload["failures"][0]["message"]
    assert runner.calls == []


def test_a_disclosed_rebuild_difference_is_a_warning_never_a_pass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rebuild = fake_rebuild(["The sdist differs from the rebuild."])
    _, exit_code, payload, _ = _run_with_fakes(
        tmp_path, monkeypatch, rebuild=rebuild, allow_source_mismatch=True
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    assert payload["checks"][1]["status"] == "warn"
    assert payload["checks"][1]["allowed_by"] == "--allow-source-mismatch"
    assert payload["warnings"][0]["check"] == "clean-rebuild-identical"


def test_a_missing_sdist_is_rejected_before_any_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rebuild = fake_rebuild()
    _, exit_code, payload, _ = _run_with_fakes(
        tmp_path, monkeypatch, rebuild=rebuild, with_sdist=False
    )

    assert exit_code == 1
    assert payload["checks"] == []
    assert payload["failures"][0]["check"] == "validation"
    assert "Sdist does not exist" in payload["failures"][0]["message"]
    assert rebuild.calls == []


def test_without_a_repository_head_nothing_is_rebuilt_and_nothing_passes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rc = load_rc_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    create_sample_fixture(repo_root)
    wheel = write_wheel(
        tmp_path / "arcgraph-0.1.0rc6-py3-none-any.whl",
        commit_sha=HEAD_COMMIT,
        tree_sha=HEAD_TREE,
    )
    rebuild = fake_rebuild()
    monkeypatch.setattr(rc, "_repo_head_provenance", lambda repo: None)
    monkeypatch.setattr(rc, "_rebuild_in_clean_clone", rebuild)
    monkeypatch.setattr(rc, "_create_virtualenv", fake_create_venv)
    monkeypatch.setattr(rc, "_run_process", FakeRunner())

    exit_code, payload = rc.run_release_candidate_check(
        wheel_path=wheel,
        sdist_path=write_sdist(tmp_path),
        repo_root=repo_root,
        timeout_seconds=5,
    )

    assert exit_code == 0
    assert payload["status"] == "warn"
    assert rebuild.calls == []
    assert payload["checks"][1]["name"] == "clean-rebuild-identical"
    assert payload["checks"][1]["status"] == "warn"


WHEEL_MEMBER = "arcgraph-0.1.0rc7.dist-info/WHEEL"


def _wheel_bytes(generator: str, extra: str = "") -> bytes:
    """Return wheel bytes that depend only on the arguments.

    ``writestr`` given a name stamps the current time into the archive, so two
    calls a couple of seconds apart would differ and the tests that compare
    "the same wheel" would fail at random.
    """
    members = {
        "arcgraph/__init__.py": extra,
        WHEEL_MEMBER: f"Wheel-Version: 1.0\nGenerator: {generator}\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2020, 2, 2, 0, 0, 0)), text
            )
    return buffer.getvalue()


def _committed_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
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
    # Neither an untracked file nor an edit may reach the rebuild.
    (repo / "stray.txt").write_text("not committed", encoding="utf-8")
    (repo / "pyproject.toml").write_text("edited after the commit", encoding="utf-8")
    return repo, head


def _rebuild_with(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    produced: dict[str, bytes],
    shipped: dict[str, bytes],
    build_returncode: int = 0,
    commit: str | None = None,
    special: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Run the real clone and checkout with a build that writes ``produced``.

    ``special`` may add entries that are not regular files to the output.
    """
    rc = load_rc_module()
    repo, head = _committed_repo(tmp_path)
    artifacts = {}
    for role, (name, data) in {
        "wheel": ("arcgraph-0.1.0rc7-py3-none-any.whl", shipped["wheel"]),
        "sdist": ("arcgraph-0.1.0rc7.tar.gz", shipped["sdist"]),
    }.items():
        artifacts[role] = tmp_path / name
        artifacts[role].write_bytes(data)
    seen: dict[str, Any] = {}

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-m", "build"]:
            return subprocess.run(command, **options)
        source = Path(options["cwd"])
        seen["cwd"] = source
        seen["stray_present"] = (source / "stray.txt").exists()
        seen["pyproject"] = (source / "pyproject.toml").read_text(encoding="utf-8")
        seen["head"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        if build_returncode:
            return subprocess.CompletedProcess(command, build_returncode, "", "boom")
        outdir = Path(command[command.index("--outdir") + 1])
        outdir.mkdir(parents=True)
        for name, data in produced.items():
            (outdir / name).write_bytes(data)
        if special is not None:
            special(outdir)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rc, "_run_process", run)
    outcome = rc._rebuild_in_clean_clone(
        repo,
        commit_sha=commit or head,
        artifacts=artifacts,
        timeout_seconds=60,
    )
    outcome["seen"] = seen
    outcome["head"] = head
    outcome["repo"] = repo
    return outcome


def _archives(*, wheel_extra: str = "", sdist: bytes = b"sdist") -> dict[str, bytes]:
    return {
        "arcgraph-0.1.0rc7-py3-none-any.whl": _wheel_bytes(
            "hatchling 1.32.4", wheel_extra
        ),
        "arcgraph-0.1.0rc7.tar.gz": sdist,
    }


def test_the_rebuild_uses_a_clean_clone_of_the_commit_not_the_working_tree(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shipped = _archives()
    outcome = _rebuild_with(
        tmp_path,
        monkeypatch,
        produced=shipped,
        shipped={
            "wheel": shipped["arcgraph-0.1.0rc7-py3-none-any.whl"],
            "sdist": shipped["arcgraph-0.1.0rc7.tar.gz"],
        },
    )

    assert outcome["problems"] == []
    seen = outcome["seen"]
    assert seen["cwd"] != outcome["repo"]
    assert seen["head"] == outcome["head"]
    assert seen["stray_present"] is False
    assert "hatchling==1.32.4" in seen["pyproject"]
    facts = outcome["facts"]
    assert facts["build_backend"] == "hatchling 1.32.4"
    assert facts["artifact_build_backend"] == "hatchling 1.32.4"
    assert facts["build_requires"] == ["hatchling==1.32.4"]
    assert len(facts["rebuilt_wheel_sha256"]) == 64
    assert len(facts["rebuilt_sdist_sha256"]) == 64
    assert facts["python"]
    assert facts["platform"]


def test_a_rebuild_reports_each_kind_of_difference(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shipped = _archives()
    shipped_by_role = {
        "wheel": shipped["arcgraph-0.1.0rc7-py3-none-any.whl"],
        "sdist": shipped["arcgraph-0.1.0rc7.tar.gz"],
    }

    changed_wheel = _rebuild_with(
        tmp_path / "wheel",
        monkeypatch,
        produced=_archives(wheel_extra="changed"),
        shipped=shipped_by_role,
    )
    assert len(changed_wheel["problems"]) == 1
    assert "The wheel arcgraph-0.1.0rc7-py3-none-any.whl differs" in (
        changed_wheel["problems"][0]
    )

    changed_sdist = _rebuild_with(
        tmp_path / "sdist",
        monkeypatch,
        produced=_archives(sdist=b"other"),
        shipped=shipped_by_role,
    )
    assert len(changed_sdist["problems"]) == 1
    assert "The sdist arcgraph-0.1.0rc7.tar.gz differs" in changed_sdist["problems"][0]

    renamed = _rebuild_with(
        tmp_path / "renamed",
        monkeypatch,
        produced={
            "arcgraph-0.1.0rc7-py3-none-any.whl": shipped_by_role["wheel"],
            "arcgraph-0.1.0rc8.tar.gz": shipped_by_role["sdist"],
        },
        shipped=shipped_by_role,
    )
    assert "The rebuild produced" in renamed["problems"][0]

    extra = _rebuild_with(
        tmp_path / "extra",
        monkeypatch,
        produced={**shipped, "arcgraph-0.1.0rc7.extra": b"x"},
        shipped=shipped_by_role,
    )
    assert "The rebuild produced" in extra["problems"][0]


WHEEL_NAME = "arcgraph-0.1.0rc7-py3-none-any.whl"
SDIST_NAME = "arcgraph-0.1.0rc7.tar.gz"


def _as_directories(outdir: Path) -> None:
    (outdir / WHEEL_NAME).mkdir()
    (outdir / SDIST_NAME).mkdir()


def _as_links_to_identical_files(outdir: Path) -> None:
    shipped = _archives()
    for name, data in shipped.items():
        target = outdir.parent / f"real-{name}"
        target.write_bytes(data)
        (outdir / name).symlink_to(target)


def _as_pipes(outdir: Path) -> None:
    os.mkfifo(outdir / WHEEL_NAME)
    os.mkfifo(outdir / SDIST_NAME)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX special files")
@pytest.mark.parametrize(
    "special",
    [_as_directories, _as_links_to_identical_files, _as_pipes],
    ids=["directory", "link-to-identical-file", "named-pipe"],
)
def test_a_rebuild_output_that_is_not_a_regular_file_is_never_accepted(
    tmp_path: Path, monkeypatch: Any, special: Callable[[Path], None]
) -> None:
    shipped = _archives()
    outcome = _rebuild_with(
        tmp_path,
        monkeypatch,
        produced={},
        shipped={"wheel": shipped[WHEEL_NAME], "sdist": shipped[SDIST_NAME]},
        special=special,
    )

    assert len(outcome["problems"]) == 2
    assert all("no regular file named" in problem for problem in outcome["problems"])
    assert "rebuilt_wheel_sha256" not in outcome["facts"]
    assert "rebuilt_sdist_sha256" not in outcome["facts"]


def test_a_rebuild_missing_one_file_is_a_problem(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shipped = _archives()
    outcome = _rebuild_with(
        tmp_path,
        monkeypatch,
        produced={WHEEL_NAME: shipped[WHEEL_NAME]},
        shipped={"wheel": shipped[WHEEL_NAME], "sdist": shipped[SDIST_NAME]},
    )

    assert any("The rebuild produced" in problem for problem in outcome["problems"])
    assert any("no regular file named" in problem for problem in outcome["problems"])


def test_a_rebuild_that_cannot_run_is_a_problem_not_a_pass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shipped = _archives()
    shipped_by_role = {
        "wheel": shipped["arcgraph-0.1.0rc7-py3-none-any.whl"],
        "sdist": shipped["arcgraph-0.1.0rc7.tar.gz"],
    }

    failed_build = _rebuild_with(
        tmp_path / "build",
        monkeypatch,
        produced={},
        shipped=shipped_by_role,
        build_returncode=2,
    )
    assert failed_build["problems"] == ["The clean rebuild step 'build' failed: boom"]

    unknown_commit = _rebuild_with(
        tmp_path / "commit",
        monkeypatch,
        produced=shipped,
        shipped=shipped_by_role,
        commit="0" * 40,
    )
    assert len(unknown_commit["problems"]) == 1
    assert "'checkout' failed" in unknown_commit["problems"][0]
    assert unknown_commit["seen"] == {}


def test_the_generator_line_names_the_backend_a_wheel_was_built_with(
    tmp_path: Path,
) -> None:
    rc = load_rc_module()
    wheel = tmp_path / "arcgraph-0.1.0rc7-py3-none-any.whl"
    wheel.write_bytes(_wheel_bytes("hatchling 9.9.9"))

    assert rc._wheel_generator(wheel) == "hatchling 9.9.9"
    assert rc._wheel_generator(tmp_path / "missing.whl") is None
    assert rc._generator_line("Wheel-Version: 1.0\n") is None
    assert rc._build_requires(tmp_path / "missing.toml") is None
