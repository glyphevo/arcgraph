"""Review E: compare position moves and real call changes in both profiles."""

import json

import pytest

from arcgraph.change.contracts import ChangedRegion
from arcgraph.change.graph_delta import GraphDeltaEngine
from arcgraph.change.identities import capture_build_identity
from arcgraph.change.source_provider import normalize_implementation_source
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.schemas import Edge, Node
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_profiles import code_identity
from arcgraph.tests.test_graph_delta import _identity, _reader_with_nodes
from arcgraph.tests.test_semantic_isolation import prepared as prepared

NAMED = "def named():\n    target()\n    target()\n\n"
BLOCKS = {
    "single": "def single():\n    return target()\n\n",
    "anonymous": "def anonymous():\n    return lambda: target()\n\n",
    "gathered": "def gathered():\n    return [target() for _ in range(1)]\n\n",
    "looped": (
        "def looped():\n    out = []\n    for i in range(2):\n"
        "        out.append(target())\n    return out\n\n"
    ),
}
HEADER = "def target():\n    return 1\n\ndef other():\n    return 2\n\n"
SOURCE = HEADER + NAMED + "".join(BLOCKS.values())


def build_delta(prepared, tmp_path, enabled, changed, *, edited_owner=None):
    project, _, _, _ = prepared
    if enabled:
        (project / "pyproject.toml").write_text(
            "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
        )
    path = project / "demo.py"
    path.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "out"
    metadata, _ = ArcGraphIndexer(project, output).build()
    if enabled:
        assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    baseline = capture_build_identity(project, output, repo_id="repo")
    path.write_text(changed, encoding="utf-8")
    metadata, _ = ArcGraphIndexer(project, output).build()
    if enabled:
        assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    current = capture_build_identity(project, output, repo_id="repo")
    assert (
        baseline.freshness["projection_profile"]
        == current.freshness["projection_profile"]
    )
    regions = []
    if edited_owner:
        old = normalize_implementation_source("demo.py", SOURCE.encode())
        new = normalize_implementation_source("demo.py", changed.encode())
        regions.append(
            ChangedRegion(
                repo_id="repo",
                path="demo.py",
                path_comparison_key="demo.py",
                baseline_symbol_id=f"fn:demo.{edited_owner}",
                current_symbol_id=f"fn:demo.{edited_owner}",
                classification="modified",
                mapping_status="mapped",
                baseline_normalized_implementation_digest=old.normalized_implementation_digest,
                current_normalized_implementation_digest=new.normalized_implementation_digest,
            )
        )
    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=GraphStoreReader.from_build(output, baseline.index_version),
        current_reader=GraphStoreReader.from_build(output, current.index_version),
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code_identity(current),
        changed_regions=regions,
    )
    (tmp_path / "delta.json").write_text(
        delta.model_dump_json(indent=2), encoding="utf-8"
    )
    return delta, output, baseline, current


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("move", ["prefix", "function", "between_calls"])
def test_position_moves_are_only_location_changes(prepared, tmp_path, enabled, move):
    changed = {
        "prefix": "\n\n" + SOURCE,
        "function": SOURCE.replace(NAMED, "") + NAMED,
        "between_calls": SOURCE.replace(
            "    target()\n    target()", "    target()\n\n    target()"
        ),
    }[move]
    delta, _, _, _ = build_delta(prepared, tmp_path, enabled, changed)
    assert delta.semantic_changes == [], delta.semantic_changes
    assert delta.identity_changes == []
    assert delta.evidence_changes == []
    assert delta.unknowns == []
    assert delta.location_changes and delta.evidence_location_changes


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("owner", BLOCKS)
@pytest.mark.parametrize("edit", ["target", "delete"])
def test_real_call_edits_remain_visible(prepared, tmp_path, enabled, owner, edit):
    block = BLOCKS[owner]
    changed_block = block.replace("target()", "other()" if edit == "target" else "1")
    delta, _, _, _ = build_delta(
        prepared,
        tmp_path,
        enabled,
        SOURCE.replace(block, changed_block),
        edited_owner=owner,
    )
    assert delta.implementation_changes
    if owner == "anonymous" and not enabled:
        # Legacy graphs omit lambda calls; change reports source implementation.
        assert any(owner in json.dumps(row) for row in delta.implementation_changes)
        return
    assert delta.semantic_changes or delta.identity_changes
    assert any(
        owner in json.dumps(row)
        for row in delta.semantic_changes + delta.identity_changes
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_deleting_one_duplicate_call_preserves_multiplicity_change(
    prepared, tmp_path, enabled
):
    changed = SOURCE.replace(NAMED, "def named():\n    target()\n\n")
    delta, output, baseline, current = build_delta(prepared, tmp_path, enabled, changed)
    edge_changes = [
        row
        for row in delta.semantic_changes
        if row["entity_type"] == "edge" and "named" in json.dumps(row)
    ]
    if enabled:
        assert edge_changes
    else:
        assert any(
            row["stable_id"] == "repo:fn:demo.named" for row in delta.semantic_changes
        )
    for identity, count in ((baseline, 2), (current, 1)):
        reader = GraphStoreReader.from_build(output, identity.index_version)
        if enabled:
            edge = next(
                e
                for e in reader.read_edges()
                if e.source == "fn:demo.named"
                and e.target == "fn:demo.target"
                and e.kind == "calls"
            )
            assert len(edge.properties["callsites"]) == count
        else:
            node = next(n for n in reader.read_nodes() if n.id == "fn:demo.named")
            assert len(node.properties["callsites"]) == count


@pytest.mark.parametrize("semantic_edit", [False, True])
def test_nested_comprehension_binding_relationships_preserve_meaning(
    tmp_path, semantic_edit
):
    def snapshot(version, line, value):
        first_id, second_id = f"binding:first:{line}", f"binding:second:{line}"
        node = Node(
            id="fn:app.f",
            kind="function",
            name="f",
            qualname="app.f",
            path="src/app.py",
            start_line=line,
            end_line=line + 3,
            properties={
                "bindings": [
                    {
                        "binding_id": first_id,
                        "scope_id": "fn:app.f",
                        "name": "i",
                        "kind": "comprehension_target",
                        "value": value,
                        "line": line,
                        "value_region": [line, line + 1],
                    },
                    {
                        "binding_id": second_id,
                        "scope_id": "fn:app.f",
                        "name": "j",
                        "kind": "comprehension_target",
                        "value": "i",
                        "line": line + 1,
                        "value_region": [line, line + 1],
                        "comprehension_inputs": {"i": first_id, "j": None},
                    },
                ],
                "comprehension_contexts": [
                    {
                        "range": [line, 0, line + 1, 8],
                        "bindings": {"i": first_id, "j": second_id},
                    }
                ],
            },
        )
        return _reader_with_nodes(tmp_path / "out", tmp_path, version, [node])

    before = snapshot("before", 3, "range(1)")
    after = snapshot("after", 20, "range(2)" if semantic_edit else "range(1)")
    baseline, current = _identity("before"), _identity("after")
    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=before,
        current_reader=after,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code_identity(current),
    )
    assert delta.identity_changes == []
    assert bool(delta.semantic_changes) is semantic_edit
    assert delta.location_changes
    if semantic_edit:
        row = delta.semantic_changes[0]
        assert (
            row["before"]["properties"]["comprehension_contexts"]
            != row["after"]["properties"]["comprehension_contexts"]
        )


@pytest.mark.parametrize("semantic_edit", [False, True])
def test_receiver_type_relationship_keeps_its_semantic_subject(tmp_path, semantic_edit):
    def snapshot(version, line, expression):
        raw_id = f"type_ref:{version}"
        node = Node(
            id="fn:app.f",
            kind="function",
            name="f",
            qualname="app.f",
            path="src/app.py",
            start_line=line,
            end_line=line + 2,
            properties={
                "type_refs": [
                    {
                        "type_ref_id": raw_id,
                        "name": "out",
                        "type_expression": "list",
                        "source_expression": expression,
                        "line": line,
                    }
                ]
            },
        )
        edge = Edge(
            source=node.id,
            target=node.id,
            kind="uses",
            properties={
                "callsite": {
                    "call_expression": "out.append(1)",
                    "receiver_type_ref_id": raw_id,
                    "line": line,
                }
            },
        )
        return _reader_with_nodes(
            tmp_path / "out", tmp_path, version, [node], edges=[edge]
        )

    before = snapshot("before", 3, "[]")
    after = snapshot("after", 20, "list(values)" if semantic_edit else "[]")
    baseline, current = _identity("before"), _identity("after")
    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=before,
        current_reader=after,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code_identity(current),
    )
    edge_changes = [r for r in delta.semantic_changes if r["entity_type"] == "edge"]
    assert bool(edge_changes) is semantic_edit
