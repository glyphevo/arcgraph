"""Host fallback is visible but never semantic evidence from project CPython."""

from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("failed_offset", [0, 64], ids=["first", "middle"])
def test_semantic_reads_continue_after_failed_block(
    tmp_path, monkeypatch, failed_offset
):
    project = tmp_path / "project"
    project.mkdir()
    names = [f"m{i:03d}.py" for i in range(128)] + ["zzz.py"]
    for name in names:
        (project / name).write_text("def f(): return 1\n", encoding="utf-8")
    parser = StructureView(project, Path(sys.prefix))
    selected = parser.interpreter
    assert selected is not None
    original = parser._read_project
    attempted = []

    def selective(paths):
        attempted.append(tuple(paths))
        if names[failed_offset] in paths:
            raise subprocess.CalledProcessError(1, ["selected-project-python"])
        assert parser.interpreter == selected
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
    statuses = {f.path: f.status for f in bundle.files}
    assert statuses["zzz.py"] == "parsed"
    assert any(r.path == "zzz.py" and r.kind == "definition" for r in bundle.records)
    assert attempted == [tuple(names[:64]), tuple(names[64:128]), ("zzz.py",)]
    failed = set(names[failed_offset : failed_offset + 64])
    assert {
        name for name, status in statuses.items() if status == "host_fallback"
    } == failed
    assert not any(r.path in failed for r in bundle.records)
    assert parser.interpreter == selected
    state = parser.status()
    assert state["status"] == "partial_fallback"
    assert state["interpreter"] == str(selected)
    assert "affected block" in state["reason"]
    identity = dict(bundle.producer.interpreter)
    assert identity["parser_status"] == "partial_fallback"
    assert identity["d2_parser_status"] == "partial_fallback"
    assert admit_structure(bundle, snapshot) == (True, "experimental_partial")


def test_semantic_reads_all_failed_blocks_remain_host_fallback(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    names = [f"m{i:03d}.py" for i in range(128)] + ["zzz.py"]
    for name in names:
        (project / name).write_text("def f(): return 1\n", encoding="utf-8")
    parser = StructureView(project, Path(sys.prefix))
    assert parser.interpreter is not None
    attempted = []

    def failing(paths):
        attempted.append(tuple(paths))
        raise subprocess.CalledProcessError(1, ["selected-project-python"])

    monkeypatch.setattr(parser, "_read_project", failing)
    snapshot = capture(
        project,
        names,
        tmp_path / "frozen",
        target_python=".".join(map(str, sys.version_info[:2])),
        platform=sys.platform,
        environment=(("dependencies", "test"),),
    )
    bundle = analyze(snapshot, tmp_path / "frozen", structure_view=parser)
    assert {f.status for f in bundle.files} == {"host_fallback"}
    assert not bundle.records
    assert parser.interpreter is None
    assert parser.status()["status"] == "host_fallback"
    identity = dict(bundle.producer.interpreter)
    assert identity["parser_status"] == "host_fallback"
    assert identity["d2_parser_status"] == "host_fallback"
    assert admit_structure(bundle, snapshot) == (
        False,
        "host_fallback_not_semantic_evidence",
    )
    assert attempted == [tuple(names[:64]), tuple(names[64:128]), ("zzz.py",)]


@pytest.mark.parametrize("failed_offset", [0, 64], ids=["first", "middle"])
def test_default_read_keeps_whole_256_file_block_fallback(
    tmp_path, monkeypatch, failed_offset
):
    names = [f"m{i:03d}.py" for i in range(128)] + ["zzz.py"]
    paths = {name: tmp_path / name for name in names}
    for path in paths.values():
        path.write_text("def f(): return 1\n", encoding="utf-8")
    parser = StructureView(tmp_path, Path(sys.prefix))
    attempted = []

    def failing(block):
        attempted.append(tuple(block))
        assert names[failed_offset] in block
        raise subprocess.CalledProcessError(1, ["selected-project-python"])

    monkeypatch.setattr(parser, "_read_project", failing)
    trees, notices = parser.read(paths)
    assert set(trees) == set(names)
    assert not notices
    assert attempted == [tuple(names)]
    state = parser.status()
    assert state["status"] == "host_fallback"
    assert state["interpreter"] == sys.executable
    assert "other blocks" not in state["reason"]
    # Default subsequent reads keep the historical whole-fallback policy.
    parser.read({"zzz.py": paths["zzz.py"]})
    assert attempted == [tuple(names)]
