"""The structure interpreter and static search inputs stay bound until publication."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from arcgraph.tests.time_limits import SUBPROCESS_HANG_SECONDS
from arcgraph.semantic.frozen import environment_identity, verify_environment
from arcgraph.tests.test_semantic_isolation import prepared as prepared
from arcgraph.tests.project_environment_support import isolated_test_python
from scripts.semantic_shadow import stage_structure, stage_write


@pytest.fixture
def completed(prepared, tmp_path):
    project, environment, markers, _ = prepared
    output = tmp_path / "shadow"
    script = Path(__file__).resolve().parents[2] / "scripts/semantic_shadow.py"
    process = subprocess.run(
        [
            isolated_test_python(tmp_path),
            "-I",
            "-B",
            str(script),
            "--repo-root",
            str(project),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_HANG_SECONDS,
        check=False,
    )  # nosec B603
    assert process.returncode == 0, process.stderr
    evidence = output / "semantic-shadow"
    (output / "current.json").write_bytes(
        (evidence / "default-current.json").read_bytes()
    )
    return project, environment, markers, output, evidence


def refused(action):
    try:
        action()
    except (ValueError, RuntimeError):
        return True
    return False


def test_same_path_and_realpath_changed_bytes_during_structure_are_refused(tmp_path):
    from arcgraph.semantic.snapshot import capture
    from arcgraph.semantic.structure_provider import analyze

    root = tmp_path / "source"
    root.mkdir()
    (root / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    binary = tmp_path / "interpreter-input"
    binary.write_bytes(b"initial interpreter bytes")

    class Parser:
        def status(self):
            return {
                "status": "project",
                "version": sys.version.split()[0],
                "interpreter": str(binary),
            }

        def read(self, paths):
            binary.write_bytes(b"changed interpreter bytes")
            return {
                name: ast.parse(path.read_text(encoding="utf-8"))
                for name, path in paths.items()
            }, []

    snapshot = capture(
        root,
        ["demo.py"],
        tmp_path / "frozen",
        target_python=".".join(map(str, sys.version_info[:2])),
        platform=sys.platform,
        environment=(("dependencies", "test"),),
    )
    assert refused(
        lambda: analyze(snapshot, tmp_path / "frozen", structure_view=Parser())
    ), "Byte drift with unchanged selected and real paths must invalidate structure"


def test_interpreter_path_realpath_and_bytes_are_frozen(completed):
    _, environment, _, _, evidence = completed
    executable = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    snapshot = json.loads((evidence / "snapshot.json").read_text(encoding="utf-8"))
    identity = dict(snapshot["environment"])
    assert identity.get("interpreter_path") == str(executable.absolute())
    assert identity.get("interpreter_real_path") == str(executable.resolve())
    assert (
        identity.get("interpreter_sha256")
        == hashlib.sha256(executable.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("phase", ["structure", "publication"])
def test_interpreter_replacement_is_refused_without_executing_it(completed, phase):
    project, environment, markers, output, evidence = completed
    executable = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    executable.unlink()
    executable.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{markers['venv-python']}'\nexit 1\n",
        encoding="utf-8",
        newline="\n",
    )
    executable.chmod(0o700)
    action = stage_structure if phase == "structure" else stage_write
    args = argparse.Namespace(
        repo_root=project, output_dir=output, project_environment=None
    )
    denied = refused(lambda: action(args, evidence))
    assert not markers[
        "venv-python"
    ].exists(), "Identity drift must be rejected before D2"
    assert denied, "Changed interpreter must not reach semantic publication"


def test_same_bytes_new_real_interpreter_path_is_refused(completed, tmp_path):
    project, environment, _, output, evidence = completed
    executable = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    replacement = tmp_path / "same-python-bytes"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o700)
    executable.unlink()
    executable.symlink_to(replacement)
    assert refused(
        lambda: stage_write(
            argparse.Namespace(repo_root=project, output_dir=output), evidence
        )
    ), "Retargeting to identical interpreter bytes still changes the selected identity"


def test_pth_change_refuses_publication(completed):
    project, environment, _, output, evidence = completed
    before = environment_identity(environment)[0]
    pth = next(
        (environment / "lib").glob("python*/site-packages/evil.pth"),
        environment / "Lib/site-packages/evil.pth",
    )
    pth.write_text("# changed static search input\n", encoding="utf-8")
    assert (
        environment_identity(environment)[0] != before
    ), ".pth is a static search input"
    assert refused(
        lambda: stage_write(
            argparse.Namespace(repo_root=project, output_dir=output), evidence
        )
    ), "Changed .pth must not reach semantic publication"
    frozen = json.loads((evidence / "environment.json").read_text(encoding="utf-8"))
    verify_environment(evidence / "environment", frozen["dependency_digest"])
