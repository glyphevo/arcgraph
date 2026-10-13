"""Doctor reports adopted build state and retained unverified evidence."""

import argparse
import json
import subprocess

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.evidence_manifest import write_evidence_sidecar
from arcgraph.core.structure import StructureView
from arcgraph.interfaces.cli import handle_doctor
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_build import configure
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def doctor(project, output):
    payload = handle_doctor(
        argparse.Namespace(repo_root=str(project), output_dir=str(output))
    )
    return next(c for c in payload["checks"] if c["name"] == "semantic_core")


def test_doctor_explains_rejected_build_when_toolchain_is_installed(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")

    def failed(*args):
        raise subprocess.CalledProcessError(7, ["project-python"])

    monkeypatch.setattr(StructureView, "_read_project", failed)
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    check = doctor(project, output)
    assert check["status"] == "warn"
    assert "host_fallback_not_semantic_evidence" in check["message"]
    assert "arcgraph build" in check["fix"]


def test_old_precision_is_retained_and_excluded_with_summary_and_doctor_notice(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    artifact = tmp_path / "explicit-pyright.json"
    artifact.write_text(
        json.dumps(
            {
                "references": [
                    {
                        "source": "fn:demo.g",
                        "symbol": "fn:demo.target",
                        "path": "demo.py",
                        "range": [1, 16, 22],
                        "kind": "call",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stamp = write_evidence_sidecar(
        repo_root=project,
        artifact_path=artifact,
        kind="precision_pyright",
        commit_sha=None,
        source_roots=["."],
        tool_name="pyright-langserver",
    )
    sidecar = artifact.with_name(artifact.name + ".arcgraph.json")
    before = artifact.read_bytes(), sidecar.read_bytes()
    assert stamp["status"] == "written"
    from arcgraph.semantic import runtime

    with monkeypatch.context() as control:
        control.setattr(
            runtime, "semantic_core_enabled", lambda root: False, raising=False
        )
        ArcGraphIndexer(
            project, tmp_path / "control", pyright_export_path=artifact
        ).build()
    assert any(
        item.kind.startswith("pyright_")
        for e in GraphStoreReader.from_current(tmp_path / "control").read_edges()
        for item in e.evidence
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output, pyright_export_path=artifact).build()
    reader = GraphStoreReader.from_current(output)
    assert artifact.read_bytes() == before[0] and sidecar.read_bytes() == before[1]
    assert not any(
        item.kind.startswith(("scip_", "pyright_"))
        or item.kind in ("precision_scip", "precision_pyright")
        for e in reader.read_edges()
        for item in e.evidence
    )
    assert "retained but excluded" in (
        reader.metadata.get("toolchain_status", {})
        .get("semantic_core", {})
        .get("reason")
        or ""
    )
    assert reader.metadata["capabilities"]["precision"] == "ast_fallback_only"
    check = doctor(project, output)
    assert "retained but excluded" in check["message"]
    assert "built-in semantic core" in check["message"]
