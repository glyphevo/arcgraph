"""Host fallback is visible but never semantic evidence from project CPython."""

from pathlib import Path
import subprocess
import sys

from arcgraph.core.structure import StructureView
from arcgraph.semantic.snapshot import capture
from arcgraph.semantic.structure_provider import analyze, admit_structure


def test_whole_host_fallback_is_not_admitted(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    environment = project / ".venv"
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("version = 3.11\n", encoding="utf-8")
    binary = environment / "bin/python"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o700)
    parser = StructureView(project)
    assert parser.status()["status"] == "host_fallback"
    snapshot = capture(
        project,
        ["demo.py"],
        tmp_path / "frozen",
        target_python=".".join(map(str, sys.version_info[:2])),
        platform=sys.platform,
        environment=(("dependencies", "test"),),
    )
    bundle = analyze(snapshot, tmp_path / "frozen", structure_view=parser)
    admitted, reason = admit_structure(bundle, snapshot)
    assert admitted is False, reason
    assert "host_fallback" in reason


def test_partial_host_files_have_no_admitted_facts(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    names = [f"part{i:03d}.py" for i in range(257)]
    for name in names:
        (project / name).write_text("def f(): return 1\n", encoding="utf-8")
    parser = StructureView(project, Path(sys.prefix))
    assert parser.status()["status"] == "project"
    original = parser._read_project

    def selective(paths):
        if "part256.py" in paths:
            raise subprocess.CalledProcessError(1, ["selected-project-python"])
        return original(paths)

    monkeypatch.setattr(parser, "_read_project", selective)
    snapshot = capture(
        project,
        names,
        tmp_path / "frozen",
        target_python=".".join(map(str, sys.version_info[:2])),
        platform=sys.platform,
        environment=(("dependencies", "test"),),
    )
    bundle = analyze(snapshot, tmp_path / "frozen", structure_view=parser)
    assert bundle.availability == "partial"
    covered = {f.path for f in bundle.files if f.status == "parsed"}
    assert "part000.py" in covered and "part256.py" not in covered
    missing = next(f for f in bundle.files if f.path == "part256.py")
    assert missing.diagnostic["reason"] == "host_fallback"
    assert not any(r.path == "part256.py" for r in bundle.records)
    assert admit_structure(bundle, snapshot) == (True, "experimental_partial")
