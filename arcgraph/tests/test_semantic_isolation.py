"""Real CPython whole-flow isolation and post-D2 Node-only runtime isolation."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.structure import StructureView
from arcgraph.interfaces.cli import handle_doctor, main
from arcgraph.semantic.runtime import (
    configuration,
    initialize_params,
    semantic_core_check,
    session,
    isolated_environment,
)


def test_node_environment_whitelist_and_path_exclude_every_python(
    tmp_path, monkeypatch
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated executable directory")
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    (inherited / "python").write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setenv("PYTHONHOME", "untrusted")
    monkeypatch.setenv("VIRTUAL_ENV", "untrusted")
    monkeypatch.setenv("NODE_OPTIONS", "untrusted")
    private = tmp_path / "private"
    env = isolated_environment(private, Path(node).resolve())
    assert env["PATH"] == str(
        private / "bin"
    ), "Inherited PATH must not reach the semantic child"
    assert sorted(p.name for p in (private / "bin").iterdir()) == [
        "node.exe" if os.name == "nt" else "node"
    ]
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_OPTIONS"}.intersection(
        env
    )
    assert all(
        env[key] == str(private)
        for key in ("HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE", "LOCALAPPDATA")
    )


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    archive = os.environ.get("ARCGRAPH_TEST_PYRIGHT_TGZ")
    python = os.environ.get("ARCGRAPH_TEST_PROJECT_PYTHON")
    node = shutil.which("node")
    if not archive:
        pytest.skip("ARCGRAPH_TEST_PYRIGHT_TGZ is required; this test never downloads")
    if not python:
        pytest.skip(
            "ARCGRAPH_TEST_PROJECT_PYTHON is required for a real project CPython"
        )
    if not node:
        pytest.skip("Node is required for the pinned pyright LSP tests")
    project = tmp_path / "project"
    project.mkdir()
    environment = project / ".venv"
    subprocess.run(
        [python, "-I", "-B", "-m", "venv", "--without-pip", str(environment)],
        check=True,
        timeout=30,
    )  # nosec B603
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    assert main(["setup", "semantic", "--from", archive]) == 0
    markers = {
        name: tmp_path / (name + ".marker")
        for name in (
            "pth",
            "sitecustomize",
            "project",
            "path-python",
            "venv-python",
            "node-options",
        )
    }
    site = next((environment / "lib").glob("python*/site-packages"))
    (site / "evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(markers['pth'])!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (site / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(markers['sitecustomize'])!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (site / "dep_example").mkdir()
    (site / "dep_example/__init__.py").write_text(
        "def target():\n    return 7\n", encoding="utf-8"
    )
    source = f"from pathlib import Path\nfrom dep_example import target\nPath({str(markers['project'])!r}).write_text('executed')\ndef g():\n    return target()\n"
    (project / "demo.py").write_text(source, encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname="semantic-isolation"\nversion="0.0.0"\nrequires-python=">=3.14,<3.15"\n[tool.arcgraph]\nsource_roots=["."]\n',
        encoding="utf-8",
    )
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    (inherited / "node").symlink_to(Path(node).resolve())
    for name in ("python", "python3"):
        script = inherited / name
        script.write_text(
            f"#!/bin/sh\n/usr/bin/touch '{markers['path-python']}'\nexit 3\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
    extra = tmp_path / "evil-node.js"
    extra.write_text(
        f"require('fs').writeFileSync({json.dumps(str(markers['node-options']))},'executed');",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.setenv("PYTHONHOME", str(environment))
    monkeypatch.setenv("VIRTUAL_ENV", str(environment))
    monkeypatch.setenv("NODE_OPTIONS", "--require " + str(extra))
    return project, environment, markers, source


def test_post_d2_peer_and_semantic_doctor_execute_no_python_and_resolve_venv_function(
    prepared, tmp_path
):
    project, environment, markers, source = prepared
    status = StructureView(project).status()
    executable = environment / "bin/python"
    executable.unlink()
    executable.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{markers['venv-python']}'\nexit 3\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "demo.py").write_text(source, encoding="utf-8")
    config = configuration(root, status["version"], environment)
    with session(root, config) as (peer, detail):
        assert peer.process.args[0] == str(Path(detail["node"]).resolve())
        assert peer.process.args[-1] == "--stdio"
        peer.request("initialize", initialize_params(root))
        peer.notify("initialized", {})
        peer.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": (root / "demo.py").as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": source,
                }
            },
        )
        items = peer.request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": (root / "demo.py").as_uri()},
                "position": {"line": 3, "character": 4},
            },
        )
        outgoing = [
            entry
            for item in items
            for entry in peer.request("callHierarchy/outgoingCalls", {"item": item})
        ]
        assert any(
            entry["to"]["name"] == "target"
            and "site-packages/dep_example" in entry["to"]["uri"]
            for entry in outgoing
        )
    assert (
        semantic_core_check(
            project, python_version=status["version"], environment=environment
        )["status"]
        == "pass"
    )
    assert not [name for name, path in markers.items() if path.exists()]


def test_whole_shadow_and_doctor_never_execute_site_project_or_inherited_python(
    prepared, tmp_path, monkeypatch
):
    project, environment, markers, source = prepared
    output = tmp_path / "shadow"
    script = Path(__file__).resolve().parents[2] / "scripts/semantic_shadow.py"
    # -I uses the tool's installed deps; D2 alone selects the real project Python.
    process = subprocess.run(
        [
            sys.executable,
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
        timeout=120,
        check=False,
    )  # nosec B603
    assert process.returncode == 0, process.stderr + "".join(
        p.read_text(encoding="utf-8")
        for p in (output / "semantic-shadow").glob("*-stderr.txt")
    )
    doctor = handle_doctor(
        argparse.Namespace(repo_root=str(project), output_dir=str(output))
    )
    semantic = next(c for c in doctor["checks"] if c["name"] == "semantic_core")
    assert semantic["status"] == "pass", semantic
    assert load_run(output)["complete"]
    GraphStoreReader.from_current(output).export_jsonl()
    assert not [name for name, path in markers.items() if path.exists()]

    # A competing writer wins between our precheck and acquisition of the lock.
    from arcgraph.core.graph_store import GraphStoreWriter
    from scripts.semantic_shadow import stage_write

    current = output / "current.json"
    competing = current.read_bytes()
    current.write_bytes((output / "semantic-shadow/default-current.json").read_bytes())
    original_lock = GraphStoreWriter._write_lock

    @contextmanager
    def competing_lock(writer):
        with original_lock(writer):
            current.write_bytes(competing)
            yield

    monkeypatch.setattr(GraphStoreWriter, "_write_lock", competing_lock)
    with pytest.raises(RuntimeError, match="Current build changed"):
        stage_write(
            argparse.Namespace(repo_root=project, output_dir=output),
            output / "semantic-shadow",
        )
    assert current.read_bytes() == competing


def test_runtime_uses_verified_private_package_after_cache_replacement(
    prepared, tmp_path
):
    from arcgraph.semantic.install import validate_installation
    from arcgraph.semantic.runtime import capabilities

    project, environment, markers, source = prepared
    package = validate_installation()
    entry = package / "langserver.index.js"
    original = entry.read_bytes()
    root = tmp_path / "workspace"
    root.mkdir()
    status = StructureView(project).status()
    try:
        with session(root, configuration(root, status["version"], environment)) as (
            peer,
            _,
        ):
            entry.write_text("throw new Error('replaced');", encoding="utf-8")
            assert Path(peer.process.args[1]).parent != package
            assert capabilities(
                peer.request("initialize", initialize_params(root))
            ) in ("utf-8", "utf-16")
    finally:
        entry.write_bytes(original)
    assert not [name for name, path in markers.items() if path.exists()]


def load_run(output):
    return json.loads((output / "semantic-shadow/run.json").read_text(encoding="utf-8"))
