from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from typing import Any


def load_gate_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "arcgraph_release_gate.py"
    spec = importlib.util.spec_from_file_location("arcgraph_release_gate", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def passing_evidence_check() -> dict[str, Any]:
    return {
        "name": "evidence_commit_consistency",
        "status": "pass",
        "details": {
            "checked": True,
            "head_commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "index_commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "runtime_trace_commit_sha": None,
            "index_matches_head": True,
            "runtime_trace_matches_head": True,
            "consistent": True,
        },
    }


def test_release_gate_accepts_fast_local_ast_fallback_profile() -> None:
    gate = load_gate_module()

    errors = gate.validate_gate(
        {
            "freshness": {"status": "fresh", "stale": False},
            "capabilities": {
                "precision": "ast_fallback_only",
                "coverage": "unavailable",
                "runtime_trace": "unavailable",
            },
            "warnings": [],
        },
        {
            "status": "pass",
            "summary": {"fail": 0, "warn": 0, "pass": 21},
            "freshness": {"status": "fresh", "stale": False},
            "warnings": [],
            "checks": [passing_evidence_check()],
        },
    )

    assert errors == []


def test_release_gate_rejects_a_dirty_source_provenance() -> None:
    gate = load_gate_module()

    errors = gate.validate_source_provenance(
        {
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "working_tree_clean": False,
            "working_tree_status_sha256": "c" * 64,
        }
    )

    assert errors == [
        "release candidates require a clean working tree; commit or remove "
        "staged, unstaged, and untracked changes first"
    ]


def test_release_gate_rejects_tracked_symlinks() -> None:
    gate = load_gate_module()

    errors = gate.validate_source_provenance(
        {
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "working_tree_clean": True,
            "working_tree_status_sha256": "c" * 64,
            "tracked_symlinks": ["arcgraph/linked.py"],
        }
    )

    assert errors == [
        "release candidates must not contain tracked symlinks: arcgraph/linked.py"
    ]


def test_capture_source_provenance_reads_real_git_state(tmp_path: Path) -> None:
    gate = load_gate_module()
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

    clean = gate.capture_source_provenance(repo)
    (repo / "arcgraph" / "__init__.py").write_text("# changed\n", encoding="utf-8")
    dirty = gate.capture_source_provenance(repo)

    assert clean["working_tree_clean"] is True
    assert clean["tracked_package_files"] == ["arcgraph/__init__.py"]
    assert clean["tracked_symlinks"] == []
    assert dirty["working_tree_clean"] is False
    assert dirty["working_tree_status_sha256"] != clean["working_tree_status_sha256"]


def test_release_gate_rejects_stale_current_index() -> None:
    gate = load_gate_module()

    errors = gate.validate_gate(
        {"freshness": {"status": "stale", "stale": True}, "warnings": []},
        {
            "status": "pass",
            "summary": {"fail": 0, "warn": 0},
            "freshness": {"status": "fresh", "stale": False},
            "warnings": [],
            "checks": [passing_evidence_check()],
        },
    )

    assert errors == ["ArcGraph index is not fresh: {'status': 'stale', 'stale': True}"]


def test_release_gate_rejects_stale_target_warnings() -> None:
    gate = load_gate_module()

    errors = gate.validate_gate(
        {
            "freshness": {"status": "fresh", "stale": False},
            "warnings": ["target_stale: 2 stale edge(s) were detected"],
        },
        {
            "status": "warn",
            "summary": {"fail": 0, "warn": 1},
            "freshness": {"status": "fresh", "stale": False},
            "risk": {"warnings": ["Detected 2 stale edge(s)"]},
            "checks": [passing_evidence_check()],
        },
    )

    assert errors == [
        "arcgraph current reported stale target edges",
        "arcgraph ci reported stale target edges",
    ]


def test_release_gate_rejects_non_stale_ci_warnings() -> None:
    gate = load_gate_module()

    errors = gate.validate_gate(
        {"freshness": {"status": "fresh", "stale": False}, "warnings": []},
        {
            "status": "warn",
            "summary": {"fail": 0, "warn": 1},
            "freshness": {"status": "fresh", "stale": False},
            "warnings": ["Semantic quality targets below threshold"],
            "checks": [passing_evidence_check()],
        },
    )

    assert errors == ["arcgraph ci reported 1 warning check(s)"]


def test_release_gate_rejects_evidence_commit_mismatch() -> None:
    gate = load_gate_module()

    evidence_check = passing_evidence_check()
    evidence_check["details"] = {
        "checked": True,
        "head_commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "index_commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "runtime_trace_commit_sha": None,
        "index_matches_head": False,
        "runtime_trace_matches_head": True,
        "consistent": False,
    }

    errors = gate.validate_gate(
        {"freshness": {"status": "fresh", "stale": False}, "warnings": []},
        {
            "status": "pass",
            "summary": {"fail": 0, "warn": 0},
            "freshness": {"status": "fresh", "stale": False},
            "warnings": [],
            "checks": [evidence_check],
        },
    )

    assert errors == [
        "ArcGraph index was generated for a different HEAD: "
        "head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, "
        "index=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    ]


def test_validate_wheel_contents_accepts_runtime_only_wheel(tmp_path: Path) -> None:
    gate = load_gate_module()
    wheel_path = tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("arcgraph/__init__.py", "")
        wheel.writestr("arcgraph/core/schemas.py", "")
        for required_file in gate.required_wheel_files():
            wheel.writestr(required_file, "")

    assert gate.validate_wheel_contents(wheel_path) == []


def test_validate_wheel_contents_rejects_missing_typescript_helper(
    tmp_path: Path,
) -> None:
    gate = load_gate_module()
    helper_file = "arcgraph/pipeline/typescript_extractor/edges.mjs"
    assert helper_file in gate.required_wheel_files()

    wheel_path = tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("arcgraph/__init__.py", "")
        for required_file in gate.required_wheel_files():
            if required_file != helper_file:
                wheel.writestr(required_file, "")

    assert gate.validate_wheel_contents(wheel_path) == [
        "ArcGraph wheel is missing required package file "
        "arcgraph/pipeline/typescript_extractor/edges.mjs"
    ]


def test_validate_wheel_contents_rejects_missing_workbench_asset(
    tmp_path: Path,
) -> None:
    gate = load_gate_module()
    workbench_file = "arcgraph/assets/workbench/index.html"
    assert workbench_file in gate.required_wheel_files()

    wheel_path = tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("arcgraph/__init__.py", "")
        for required_file in gate.required_wheel_files():
            if required_file != workbench_file:
                wheel.writestr(required_file, "")

    assert gate.validate_wheel_contents(wheel_path) == [
        "ArcGraph wheel is missing required package file "
        "arcgraph/assets/workbench/index.html"
    ]


def test_validate_wheel_contents_rejects_packaged_tests(tmp_path: Path) -> None:
    gate = load_gate_module()
    wheel_path = tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("arcgraph/__init__.py", "")
        wheel.writestr("arcgraph/tests/test_imports.py", "")
        for required_file in gate.required_wheel_files():
            wheel.writestr(required_file, "")

    assert gate.validate_wheel_contents(wheel_path) == [
        "ArcGraph wheel includes test files under arcgraph/tests"
    ]


def test_validate_wheel_contents_binds_runtime_files_to_git_provenance(
    tmp_path: Path,
) -> None:
    gate = load_gate_module()
    wheel_path = tmp_path / "arcgraph-0.1.0rc2-py3-none-any.whl"
    expected = sorted(
        {
            "arcgraph/__init__.py",
            *(
                path
                for path in gate.required_wheel_files()
                if path not in gate.GENERATED_WHEEL_FILES
            ),
        }
    )
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for path in [*expected, *gate.GENERATED_WHEEL_FILES]:
            wheel.writestr(path, "")
        wheel.writestr("arcgraph/generated_backdoor.py", "")

    errors = gate.validate_wheel_contents(
        wheel_path,
        tracked_package_files=expected,
    )

    assert errors == [
        "ArcGraph wheel contains package files absent from Git provenance: "
        "arcgraph/generated_backdoor.py"
    ]


def test_release_gate_detects_source_mutation_after_wheel_build(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    gate = load_gate_module()
    expected = sorted({"arcgraph/__init__.py", *gate.required_wheel_files()})
    provenance = {
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "working_tree_clean": True,
        "working_tree_status_sha256": "c" * 64,
        "tracked_symlinks": [],
        "tracked_package_files": expected,
    }
    changed = {**provenance, "working_tree_status_sha256": "d" * 64}
    snapshots = iter([provenance, changed])
    monkeypatch.setattr(gate, "capture_source_provenance", lambda: next(snapshots))
    monkeypatch.setattr(
        gate,
        "run_arcgraph_json",
        lambda args: (
            {"freshness": {"status": "fresh", "stale": False}, "warnings": []}
            if args == ["current"]
            else {
                "status": "pass",
                "summary": {"fail": 0, "warn": 0},
                "freshness": {"status": "fresh", "stale": False},
                "warnings": [],
                "checks": [passing_evidence_check()],
            }
        ),
    )

    def build_wheel(dist_dir: Path) -> Path:
        wheel_path = dist_dir / "arcgraph-0.1.0rc2-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as wheel:
            for path in expected:
                wheel.writestr(path, "")
        return wheel_path

    monkeypatch.setattr(gate, "build_arcgraph_wheel", build_wheel)

    assert gate.main() == 1
    captured = capsys.readouterr()
    assert "working tree changed while the release gate was running" in captured.err
    assert "wheel_size=" in captured.out


def test_wheel_selection_names_the_version_under_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Selecting the last name alphabetically validates the wrong artifact.

    `sorted(glob("arcgraph-*.whl"))[-1]` returns rc6 when an rc10 is present,
    because the comparison is lexicographic. The gate would then report the
    contents of an older wheel under the current version label, which is the
    label-collision class this repository has already paid for once.
    """

    module = load_gate_module()
    dist = tmp_path / "dist"
    dist.mkdir()

    def no_build(*_args: Any, **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr(module.subprocess, "run", no_build)
    # rc10 is the version under release, and it is the one a lexicographic
    # selection loses: "1" sorts before "5", so rc6 wins the name comparison.
    monkeypatch.setattr(module, "declared_project_version", lambda: "0.1.0rc10")

    for name in ("0.1.0rc5", "0.1.0rc6", "0.1.0rc10"):
        (dist / f"arcgraph-{name}-py3-none-any.whl").write_bytes(b"")

    selected = module.build_arcgraph_wheel(dist)
    assert selected.name == "arcgraph-0.1.0rc10-py3-none-any.whl"
    naive = sorted(path.name for path in dist.glob("arcgraph-*.whl"))[-1]
    assert naive == "arcgraph-0.1.0rc6-py3-none-any.whl"
    assert naive != selected.name


def test_wheel_selection_refuses_when_the_declared_version_is_absent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = load_gate_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "arcgraph-0.1.0rc5-py3-none-any.whl").write_bytes(b"")

    def no_build(*_args: Any, **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr(module.subprocess, "run", no_build)
    monkeypatch.setattr(module, "declared_project_version", lambda: "0.1.0rc6")

    try:
        module.build_arcgraph_wheel(dist)
    except SystemExit as exc:
        assert exc.code == 1
    else:  # pragma: no cover - the gate must not accept a stale artifact
        raise AssertionError("a stale wheel was accepted for the current version")


def test_wheel_selection_refuses_to_guess_between_duplicates(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = load_gate_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "arcgraph-0.1.0rc6-py3-none-any.whl").write_bytes(b"")
    (dist / "arcgraph-0.1.0rc6-1-py3-none-any.whl").write_bytes(b"")

    def no_build(*_args: Any, **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr(module.subprocess, "run", no_build)
    monkeypatch.setattr(module, "declared_project_version", lambda: "0.1.0rc6")

    try:
        module.build_arcgraph_wheel(dist)
    except SystemExit as exc:
        assert exc.code == 1
    else:  # pragma: no cover
        raise AssertionError("the gate guessed between two matching wheels")


def test_declared_version_matches_the_packaging_metadata() -> None:
    """The selector's key must be the same string the wheel is named with."""

    module = load_gate_module()
    import arcgraph

    assert module.declared_project_version() == arcgraph.__version__
