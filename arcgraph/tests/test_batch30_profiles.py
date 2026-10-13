"""Projection profiles bind baselines; anonymous line moves stay comparable."""

import json

import pytest

from arcgraph.change.contracts import CodeIdentity, build_identity_digest
from arcgraph.change.errors import BaselineIntegrityMismatch, GraphDeltaIncomplete
from arcgraph.change.graph_delta import GraphDeltaEngine
from arcgraph.change.identities import (
    capture_build_identity,
    stable_node_identity,
    verify_build_identity,
)
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.schemas import Node
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def code_identity(identity):
    return CodeIdentity(
        repo_id=identity.repo_id,
        working_tree_clean=True,
        working_tree_status_digest="test",
        file_manifest_digest=identity.file_manifest_digest,
        index_version=identity.index_version,
        build_identity_digest=build_identity_digest(identity),
    )


def compare(output, baseline, current):
    return GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=GraphStoreReader.from_build(output, baseline.index_version),
        current_reader=GraphStoreReader.from_build(output, current.index_version),
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code_identity(current),
    )


def anonymous(identity, line):
    return Node(
        id="fn:demo.f.<lambda>#" + identity * 64,
        kind="function",
        name="<lambda>",
        qualname="demo.f.<lambda>",
        path="demo.py",
        start_line=line,
        end_line=line,
        properties={
            "params": [],
            "callsites": [
                {
                    "call_expression": "target()",
                    "context": "body",
                    "line": line,
                    "column": 12,
                }
            ],
        },
    )


def test_anonymous_stable_identity_does_not_include_the_definition_line():
    old = stable_node_identity("repo", anonymous("a", 2))
    new = stable_node_identity("repo", anonymous("b", 5))
    assert old["stable_identity"] == new["stable_identity"]
    assert old["stability_profile"] == new["stability_profile"]


def test_enabled_projection_versions_require_rebuilt_change_baselines(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.semantic import build

    project, _, _, _ = prepared
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    with monkeypatch.context() as old_profile:
        old_profile.setattr(build, "PROFILE", "semantic-core/1")
        ArcGraphIndexer(project, output).build()
        baseline = capture_build_identity(project, output, repo_id="repo")
    ArcGraphIndexer(project, output).build()
    current = capture_build_identity(project, output, repo_id="repo")
    assert baseline.freshness["projection_profile"] == "semantic-core/1"
    assert current.freshness["projection_profile"] == "semantic-core/2"
    with pytest.raises(
        (GraphDeltaIncomplete, BaselineIntegrityMismatch), match="projection"
    ):
        compare(output, baseline, current)


def test_profile_is_bound_to_build_and_cross_profile_comparison_requires_rebuild(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    (project / "demo.py").write_text(
        "def target(): return 1\ndef f(): return target()\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    baseline = capture_build_identity(project, output, repo_id="repo")
    assert baseline.freshness.get("projection_profile") == "legacy/1"
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    current = capture_build_identity(project, output, repo_id="repo")
    assert current.freshness["projection_profile"] == "semantic-core/2"
    assert current.freshness["projection_profile_digest"]
    with pytest.raises(
        GraphDeltaIncomplete, match="projection profiles differ.*rebuild"
    ):
        compare(output, baseline, current)
    with pytest.raises(
        GraphDeltaIncomplete, match="projection profiles differ.*rebuild"
    ):
        compare(output, current, baseline)
    verify_build_identity(output, current)
    profile = directory / "projection-profile.json"
    profile.write_text(json.dumps({"projection_profile": "legacy/1"}), encoding="utf-8")
    with pytest.raises(BaselineIntegrityMismatch, match="projection profile"):
        verify_build_identity(output, current)


def test_real_anonymous_line_move_preserves_nodes_edges_and_callsite_subjects(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    path = project / "demo.py"
    source = "def target(): return 1\ndef f():\n    return lambda: target()\n"
    path.write_text(source, encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    baseline = capture_build_identity(project, output, repo_id="repo")
    path.write_text("\n\n" + source, encoding="utf-8")
    ArcGraphIndexer(project, output).build()
    current = capture_build_identity(project, output, repo_id="repo")
    delta = compare(output, baseline, current)
    assert delta.identity_changes == []
    assert delta.semantic_changes == []
    assert delta.unknowns == []
    assert delta.location_changes


def test_repeated_anonymous_expressions_report_blocking_ambiguity(prepared, tmp_path):
    project, _, _, _ = prepared
    (project / "demo.py").write_text(
        "def target(): return 1\ndef f():\n    return (lambda: target(), lambda: target())\n",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    baseline = capture_build_identity(project, output, repo_id="repo")
    ArcGraphIndexer(project, output).build()
    current = capture_build_identity(project, output, repo_id="repo")
    delta = compare(output, baseline, current)
    assert any(
        "ambiguous" in row["reason"] and row["blocking"] for row in delta.unknowns
    )


def test_named_and_anonymous_calls_line_move_is_only_location(prepared, tmp_path):
    project, _, _, _ = prepared
    path = project / "demo.py"
    source = (
        "def target(): return 1\n"
        "def named():\n    return target()\n"
        "def anonymous():\n    return lambda: target()\n"
    )
    path.write_text(source, encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    metadata, _ = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    baseline = capture_build_identity(project, output, repo_id="repo")
    path.write_text("\n\n" + source, encoding="utf-8")
    ArcGraphIndexer(project, output).build()
    current = capture_build_identity(project, output, repo_id="repo")
    delta = compare(output, baseline, current)
    assert delta.semantic_changes == []
    assert delta.identity_changes == []
    assert delta.unknowns == []
    assert delta.location_changes and delta.evidence_location_changes
