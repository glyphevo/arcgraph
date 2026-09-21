from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.change.contracts import (
    BuildIdentity,
    ChangedRegion,
    CodeIdentity,
    build_identity_digest,
)
from arcgraph.change.errors import GraphDeltaIncomplete
from arcgraph.change.graph_delta import GraphDeltaEngine
from arcgraph.change.source_provider import normalize_implementation_source
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import (
    Edge,
    Evidence,
    FactResolution,
    FileRecord,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)


def _reader(
    output_dir: Path,
    repo_root: Path,
    index_version: str,
    line: int,
    *,
    diagnostics: list[SemanticDiagnostic] | None = None,
    edges: list[Edge] | None = None,
) -> GraphStoreReader:
    metadata = IndexMetadata(
        index_version=index_version,
        repo_root=str(repo_root),
        source_roots=["src"],
    )
    files = [
        FileRecord(
            path="src/app.py",
            abs_path=str(repo_root / "src" / "app.py"),
            source_root="src",
            module="app",
            file_hash="hash",
            line_count=3,
        )
    ]
    GraphStoreWriter(output_dir).write(
        metadata,
        files,
        [
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                qualname="app.f",
                path="src/app.py",
                start_line=line,
                end_line=line + 1,
            )
        ],
        edges or [],
        [],
        diagnostics=diagnostics,
    )
    return GraphStoreReader.from_build(output_dir, index_version)


def _reader_with_nodes(
    output_dir: Path,
    repo_root: Path,
    index_version: str,
    nodes: list[Node],
    *,
    edges: list[Edge] | None = None,
) -> GraphStoreReader:
    metadata = IndexMetadata(
        index_version=index_version,
        repo_root=str(repo_root),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata,
        [
            FileRecord(
                path="src/app.py",
                abs_path=str(repo_root / "src" / "app.py"),
                source_root="src",
                module="app",
                file_hash="hash",
                line_count=3,
            )
        ],
        nodes,
        edges or [],
        [],
    )
    return GraphStoreReader.from_build(output_dir, index_version)


def _identity(index_version: str) -> BuildIdentity:
    return BuildIdentity(
        repo_id="repo",
        index_version=index_version,
        build_relative_path=f"builds/{index_version}",
        file_manifest_digest="manifest",
        summary_digest=f"summary-{index_version}",
        index_sqlite_sha256=f"sqlite-{index_version}",
        index_sqlite_size=1,
    )


def test_graph_delta_separates_location_and_implementation_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    baseline_reader = _reader(output, tmp_path, "baseline", 1)
    current_reader = _reader(output, tmp_path, "current", 2)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )
    region = ChangedRegion(
        repo_id="repo",
        path="src/app.py",
        path_comparison_key="src/app.py",
        old_start_line=1,
        old_end_line=2,
        new_start_line=2,
        new_end_line=3,
        baseline_symbol_id="fn:app.f",
        current_symbol_id="fn:app.f",
        classification="modified",
        mapping_status="mapped",
        baseline_raw_source_digest="raw-a",
        current_raw_source_digest="raw-b",
        baseline_normalized_implementation_digest="impl-a",
        current_normalized_implementation_digest="impl-b",
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
        changed_regions=[region],
    )

    assert delta.identity_changes == []
    assert delta.location_changes[0]["entity_type"] == "node"
    assert delta.implementation_changes[0]["before"] == "impl-a"


def test_graph_delta_treats_embedded_binding_positions_as_location_only(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    value = 1\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    def snapshot(
        index_version: str,
        line: int,
        *,
        binding_value: str = "1",
        shadow_target: str = "old",
        inferred_target: str = "old",
    ) -> GraphStoreReader:
        binding_suffix = f"line-{line}"
        binding_ids = {
            "old": f"binding:import-old-{binding_suffix}",
            "new": f"binding:import-new-{binding_suffix}",
            "value": f"binding:value-{binding_suffix}",
        }
        type_ref_ids = {
            "old": f"type-ref:old-{binding_suffix}",
            "new": f"type-ref:new-{binding_suffix}",
            "value": f"type-ref:value-{binding_suffix}",
        }

        def binding_evidence(kind: str, record_line: int) -> dict[str, object]:
            return {
                "kind": f"ast_binding:{kind}",
                "path": "src/app.py",
                "start_line": record_line,
                "end_line": record_line,
                "column": 4,
                "detail": "value",
            }

        def type_evidence(strategy: str, record_line: int) -> dict[str, object]:
            return {
                "kind": f"ast_type_ref:{strategy}",
                "path": "src/app.py",
                "start_line": record_line,
                "end_line": record_line,
                "column": 4,
                "detail": "value",
            }

        node = Node(
            id="fn:app.f",
            kind="function",
            name="f",
            qualname="app.f",
            path="src/app.py",
            start_line=line,
            end_line=line + 2,
            properties={
                "bindings": [
                    {
                        "binding_id": binding_ids["old"],
                        "scope_id": "fn:app.f",
                        "scope_kind": "function",
                        "name": "value",
                        "kind": "import_alias",
                        "confidence": "confirmed",
                        "target_module": "pkg.old",
                        "imported_name": "pkg.old.value",
                        "path": "src/app.py",
                        "line": line,
                        "column": 4,
                        "type_ref_id": type_ref_ids["old"],
                        "evidence": binding_evidence("import_alias", line),
                    },
                    {
                        "binding_id": binding_ids["new"],
                        "scope_id": "fn:app.f",
                        "scope_kind": "function",
                        "name": "value",
                        "kind": "import_alias",
                        "confidence": "confirmed",
                        "target_module": "pkg.new",
                        "imported_name": "pkg.new.value",
                        "path": "src/app.py",
                        "line": line + 1,
                        "column": 4,
                        "type_ref_id": type_ref_ids["new"],
                        "evidence": binding_evidence("import_alias", line + 1),
                    },
                    {
                        "binding_id": binding_ids["value"],
                        "scope_id": "fn:app.f",
                        "scope_kind": "function",
                        "name": "value",
                        "kind": "assignment",
                        "confidence": "confirmed",
                        "value": binding_value,
                        "path": "src/app.py",
                        "line": line + 2,
                        "column": 4,
                        "shadows_binding_id": binding_ids[shadow_target],
                        "type_ref_id": type_ref_ids["value"],
                        "evidence": binding_evidence("assignment", line + 2),
                    },
                ],
                "binding_diagnostics": [
                    {
                        "kind": "binding_shadowing",
                        "scope_id": "fn:app.f",
                        "name": "value",
                        "binding_id": binding_ids["value"],
                        "shadowed_binding_id": binding_ids[shadow_target],
                        "path": "src/app.py",
                        "line": line + 2,
                        "column": 4,
                        "message": "Local binding 'value' shadows imported binding.",
                    }
                ],
                "type_refs": [
                    {
                        "type_ref_id": type_ref_ids["old"],
                        "binding_id": binding_ids["old"],
                        "scope_id": "fn:app.f",
                        "name": "value",
                        "subject_kind": "binding",
                        "strategy": "annotation",
                        "confidence": "confirmed",
                        "path": "src/app.py",
                        "line": line,
                        "column": 4,
                        "source_expression": "OldType",
                        "type_expression": "OldType",
                        "resolution_status": "resolved",
                        "evidence": type_evidence("annotation", line),
                    },
                    {
                        "type_ref_id": type_ref_ids["new"],
                        "binding_id": binding_ids["new"],
                        "scope_id": "fn:app.f",
                        "name": "value",
                        "subject_kind": "binding",
                        "strategy": "annotation",
                        "confidence": "confirmed",
                        "path": "src/app.py",
                        "line": line + 1,
                        "column": 4,
                        "source_expression": "NewType",
                        "type_expression": "NewType",
                        "resolution_status": "resolved",
                        "evidence": type_evidence("annotation", line + 1),
                    },
                    {
                        "type_ref_id": type_ref_ids["value"],
                        "binding_id": binding_ids["value"],
                        "inferred_from_type_ref_id": type_ref_ids[inferred_target],
                        "scope_id": "fn:app.f",
                        "name": "value",
                        "subject_kind": "binding",
                        "strategy": "assignment_propagation",
                        "confidence": "inferred",
                        "path": "src/app.py",
                        "line": line + 2,
                        "column": 4,
                        "source_expression": "value",
                        "type_expression": "ResolvedType",
                        "resolution_status": "resolved",
                        "evidence": type_evidence("assignment_propagation", line + 2),
                    },
                ],
            },
        )
        return _reader_with_nodes(output, tmp_path, index_version, [node])

    baseline_reader = snapshot("baseline", 2)
    current_reader = snapshot("current", 20)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert delta.identity_changes == []
    assert delta.semantic_changes == []
    assert len(delta.location_changes) == 1

    edited_reader = snapshot("edited", 20, binding_value="2")
    edited = _identity("edited")
    edited_code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="edited",
        build_identity_digest=build_identity_digest(edited),
    )
    edited_delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=edited_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=edited,
        current_code_identity=edited_code,
    )

    assert len(edited_delta.semantic_changes) == 1

    relationship_reader = snapshot(
        "relationship-edited",
        20,
        shadow_target="new",
        inferred_target="new",
    )
    relationship_identity = _identity("relationship-edited")
    relationship_code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="relationship-edited",
        build_identity_digest=build_identity_digest(relationship_identity),
    )
    relationship_delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=relationship_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=relationship_identity,
        current_code_identity=relationship_code,
    )

    assert len(relationship_delta.semantic_changes) == 1
    before = relationship_delta.semantic_changes[0]["before"]["properties"]
    after = relationship_delta.semantic_changes[0]["after"]["properties"]
    before_assignment = next(
        item for item in before["bindings"] if item.get("kind") == "assignment"
    )
    after_assignment = next(
        item for item in after["bindings"] if item.get("kind") == "assignment"
    )
    assert before_assignment["shadows_binding"] != after_assignment["shadows_binding"]
    before_inferred = next(
        item
        for item in before["type_refs"]
        if item.get("strategy") == "assignment_propagation"
    )
    after_inferred = next(
        item
        for item in after["type_refs"]
        if item.get("strategy") == "assignment_propagation"
    )
    assert (
        before_inferred["inferred_from_type_ref"]
        != after_inferred["inferred_from_type_ref"]
    )


def test_graph_delta_fails_closed_on_dangling_embedded_relationship(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    output = tmp_path / "output"

    def snapshot(index_version: str, line: int) -> GraphStoreReader:
        return _reader_with_nodes(
            output,
            tmp_path,
            index_version,
            [
                Node(
                    id="fn:app.f",
                    kind="function",
                    name="f",
                    qualname="app.f",
                    path="src/app.py",
                    start_line=line,
                    end_line=line,
                    properties={
                        "bindings": [
                            {
                                "binding_id": f"binding:value:{line}",
                                "name": "value",
                                "kind": "assignment",
                                "line": line,
                                "shadows_binding_id": "binding:missing",
                            }
                        ]
                    },
                )
            ],
        )

    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    with pytest.raises(
        GraphDeltaIncomplete,
        match="relationship references an identity without a semantic subject",
    ):
        GraphDeltaEngine().compare(
            repo_id="repo",
            baseline_reader=snapshot("baseline", 1),
            current_reader=snapshot("current", 2),
            baseline_build_identity=baseline,
            baseline_source_identity={"repo_id": "repo"},
            current_build_identity=current,
            current_code_identity=code,
        )


@pytest.mark.parametrize(
    ("frontend_name", "node_id", "kind"),
    [
        ("openapi-protocol", "openapi_operation:GET:/items", "operation"),
        ("scip-protocol", "extsym:scip:item", "function"),
        ("third-party-semantic", "custom:item", "symbol"),
    ],
)
def test_graph_delta_accepts_language_neutral_frontend_identity_fallback(
    tmp_path: Path,
    frontend_name: str,
    node_id: str,
    kind: str,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("item\n", encoding="utf-8")
    node = Node(
        id=node_id,
        kind=kind,
        name="item",
        qualname="api.item",
        path="src/app.py",
        properties={"frontend_name": frontend_name},
    )
    output = tmp_path / "output"
    baseline_reader = _reader_with_nodes(
        output,
        tmp_path,
        "baseline",
        [node],
    )
    current_reader = _reader_with_nodes(
        output,
        tmp_path,
        "current",
        [node],
    )
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert delta.identity_changes == []
    assert delta.semantic_changes == []
    assert delta.unknowns == []


def test_graph_delta_keeps_valid_nodes_when_one_identity_is_unresolved(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    valid = Node(
        id="fn:app.f",
        kind="function",
        name="f",
        qualname="app.f",
        path="src/app.py",
    )
    added = valid.model_copy(
        update={"id": "fn:app.g", "name": "g", "qualname": "app.g"}
    )
    unresolved = Node(
        id="foreign-project-v9::function:app.bad",
        kind="function",
        name="bad",
        qualname="app.bad",
        path="src/app.py",
    )
    output = tmp_path / "output"
    baseline_reader = _reader_with_nodes(
        output, tmp_path, "baseline", [valid, unresolved]
    )
    current_reader = _reader_with_nodes(
        output, tmp_path, "current", [valid, added, unresolved]
    )
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert any(
        item["change"] == "added" and item["stable_id"].endswith(":fn:app.g")
        for item in delta.identity_changes
    )
    assert [item["code"] for item in delta.unknowns].count(
        "NODE_IDENTITY_UNRESOLVED"
    ) == 2


def test_graph_delta_explains_a_mapped_symbol_identity_move_as_manual_review(
    tmp_path: Path,
) -> None:
    """A one-to-one move cannot degrade into unrelated Added/Removed only."""

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    baseline_reader = _reader_with_nodes(
        output,
        tmp_path,
        "baseline",
        [
            Node(
                id="fn:old_module.f",
                kind="function",
                name="f",
                qualname="old_module.f",
                path="src/old_module.py",
            )
        ],
    )
    current_reader = _reader_with_nodes(
        output,
        tmp_path,
        "current",
        [
            Node(
                id="fn:new_module.f",
                kind="function",
                name="f",
                qualname="new_module.f",
                path="src/new_module.py",
            )
        ],
    )
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )
    moved_region = ChangedRegion(
        repo_id="repo",
        path="src/new_module.py",
        path_comparison_key="src/new_module.py",
        baseline_symbol_id="fn:old_module.f",
        current_symbol_id="fn:new_module.f",
        classification="renamed",
        mapping_status="mapped",
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
        changed_regions=[moved_region],
    )

    assert any(
        item["code"] == "NODE_IDENTITY_MOVE_REQUIRES_REVIEW" for item in delta.unknowns
    )


def test_graph_delta_requires_validated_node_identities_for_edge_endpoints(
    tmp_path: Path,
) -> None:
    """A dangling edge endpoint cannot inherit a stable identity from raw text."""

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    dangling = Edge(
        source="fn:app.f",
        target="worker:celery:missing",
        kind="enqueues",
    )
    baseline_reader = _reader(output, tmp_path, "baseline", 1, edges=[dangling])
    current_reader = _reader(output, tmp_path, "current", 1, edges=[dangling])
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert [item["code"] for item in delta.unknowns].count(
        "EDGE_IDENTITY_UNRESOLVED"
    ) == 2


def test_graph_delta_tracks_position_derived_callsites_by_semantic_witness(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    client.send()\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    subject = {
        "source_scope": "fn:app.f",
        "raw_expression": "client.send",
        "context": "function_body",
        "call_expression": "client.send()",
        "receiver_expression": "client",
        "attribute": "send",
    }

    def snapshot(
        index_version: str,
        *,
        raw_suffix: str,
        line: int,
    ) -> GraphStoreReader:
        source = Node(
            id="fn:app.f",
            kind="function",
            name="f",
            qualname="app.f",
            path="src/app.py",
            start_line=1,
        )
        target = Node(
            id=f"unresolved:{raw_suffix}",
            kind="diagnostic",
            name="client.send",
            path="src/app.py",
            start_line=line,
            properties={
                "diagnostic_kind": "unresolved_dynamic_callsite",
                "callsite_id": f"callsite:{raw_suffix}",
                "reason": "dynamic_dispatch",
                "source_scope": source.id,
                "raw_expression": "client.send",
                "stable_callsite_subject": subject,
                "stable_callsite_occurrence": 1,
            },
        )
        edge = Edge(
            source=source.id,
            target=target.id,
            kind="dynamic_call",
            confidence="unresolved",
            resolution=FactResolution(
                status="unresolved",
                strategy="dynamic_dispatch",
                candidate_count=0,
                callsite_id=f"callsite:{raw_suffix}",
            ),
            evidence=[
                Evidence(
                    kind="ast_dynamic_call",
                    path="src/app.py",
                    start_line=line,
                    column=4,
                    detail="client.send",
                )
            ],
            properties={
                "callsite": {
                    "callsite_id": f"callsite:{raw_suffix}",
                    "raw_expression": "client.send",
                    "stable_callsite_subject": subject,
                    "stable_callsite_occurrence": 1,
                }
            },
        )
        return _reader_with_nodes(
            output,
            tmp_path,
            index_version,
            [source, target],
            edges=[edge],
        )

    baseline_reader = snapshot("baseline", raw_suffix="line-10", line=10)
    current_reader = snapshot("current", raw_suffix="line-30", line=30)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert delta.identity_changes == []
    assert delta.semantic_changes == []
    assert len(delta.location_changes) == 1
    assert len(delta.evidence_location_changes) == 1


def test_graph_delta_rejects_unrecognized_projection_version() -> None:
    with pytest.raises(GraphDeltaIncomplete):
        GraphDeltaEngine(comparison_projection_version="999")


def test_normalization_ignores_comments_and_line_endings_but_keeps_literals() -> None:
    baseline = normalize_implementation_source(
        "src/app.py", b"def f():\n    return 1\n"
    )
    comment_only = normalize_implementation_source(
        "src/app.py", b"# retained as source text only\r\ndef f():\r\n return 1\r\n"
    )
    literal_change = normalize_implementation_source(
        "src/app.py", b"def f():\n    return 2\n"
    )
    unsupported = normalize_implementation_source("src/app.txt", b"value = 2\n")

    assert baseline.status == "normalized"
    assert (
        baseline.normalized_implementation_digest
        == comment_only.normalized_implementation_digest
    )
    assert (
        baseline.normalized_implementation_digest
        != literal_change.normalized_implementation_digest
    )
    assert unsupported.status == "unknown"
    assert unsupported.normalized_implementation_digest is None


def test_graph_delta_marks_unknown_normalization_and_truncation_as_blocking(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "output"
    baseline_reader = _reader(output, tmp_path, "baseline", 1)
    current_reader = _reader(output, tmp_path, "current", 1)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )
    unknown_region = ChangedRegion(
        repo_id="repo",
        path="src/app.txt",
        path_comparison_key="src/app.txt",
        classification="modified",
        mapping_status="unmapped",
        baseline_raw_source_digest="raw-a",
        current_raw_source_digest="raw-b",
    )

    delta = GraphDeltaEngine(max_findings=0).compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
        changed_regions=[unknown_region],
    )

    assert delta.truncation["truncated"]
    assert any(item["code"] == "GRAPH_DELTA_INCOMPLETE" for item in delta.unknowns)


def test_graph_delta_treats_a_mapped_hunk_addition_as_implementation_change(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    return 1\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    baseline_reader = _reader(output, tmp_path, "baseline", 1)
    current_reader = _reader(output, tmp_path, "current", 1)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )
    added_region = ChangedRegion(
        repo_id="repo",
        path="src/app.py",
        path_comparison_key="src/app.py",
        new_start_line=1,
        new_end_line=2,
        current_symbol_id="fn:app.f",
        classification="added",
        mapping_status="mapped",
        current_raw_source_digest="raw-added",
        current_normalized_implementation_digest="impl-added",
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
        changed_regions=[added_region],
    )

    assert delta.implementation_changes[0]["classification"] == "added"
    assert not any(
        item["code"] == "SOURCE_NORMALIZATION_UNKNOWN" for item in delta.unknowns
    )


def test_graph_delta_coalesces_split_move_and_edit_hunks_for_one_symbol(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def f():\n    return 2\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    baseline_reader = _reader(output, tmp_path, "baseline", 1)
    current_reader = _reader(output, tmp_path, "current", 1)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )
    common = {
        "repo_id": "repo",
        "path": "src/app.py",
        "path_comparison_key": "src/app.py",
        "old_path": "src/app.py",
        "old_path_comparison_key": "src/app.py",
        "new_path": "src/app.py",
        "new_path_comparison_key": "src/app.py",
        "baseline_symbol_id": "fn:app.f",
        "current_symbol_id": "fn:app.f",
        "mapping_status": "mapped",
        "baseline_normalized_implementation_digest": "implementation-before",
        "current_normalized_implementation_digest": "implementation-after",
    }
    regions = [
        ChangedRegion(
            **common,
            old_start_line=1,
            old_end_line=2,
            classification="deleted",
        ),
        ChangedRegion(
            **common,
            new_start_line=10,
            new_end_line=11,
            classification="added",
        ),
    ]

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
        changed_regions=regions,
    )

    assert len(delta.implementation_changes) == 1
    assert delta.implementation_changes[0]["classification"] == "moved_and_modified"
    assert delta.implementation_changes[0]["before"] == "implementation-before"
    assert delta.implementation_changes[0]["after"] == "implementation-after"


def test_graph_delta_rejects_cross_repository_baseline_source_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "output"
    baseline_reader = _reader(output, tmp_path, "baseline", 1)
    current_reader = _reader(output, tmp_path, "current", 1)
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    with pytest.raises(GraphDeltaIncomplete):
        GraphDeltaEngine().compare(
            repo_id="repo",
            baseline_reader=baseline_reader,
            current_reader=current_reader,
            baseline_build_identity=baseline,
            baseline_source_identity={"repo_id": "other"},
            current_build_identity=current,
            current_code_identity=code,
        )


def test_graph_delta_separates_diagnostic_semantics_from_lifecycle_metadata(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "output"
    common = {
        "repo_id": "repo",
        "diagnostic_kind": "unresolved_call",
        "severity": "warning",
        "frontend_name": "python",
        "fact_id": "fact:call",
    }
    baseline_reader = _reader(
        output,
        tmp_path,
        "baseline",
        1,
        diagnostics=[
            SemanticDiagnostic(
                diagnostic_id="diagnostic:baseline",
                index_version="baseline",
                message="cannot resolve call",
                path="src/app.py",
                start_line=1,
                end_line=1,
                first_seen_index="baseline",
                last_seen_index="baseline",
                seen_count=1,
                properties={
                    "diagnostic_lifecycle_status": "new",
                    "seen_count": 1,
                },
                **common,
            )
        ],
    )
    current_reader = _reader(
        output,
        tmp_path,
        "current",
        1,
        diagnostics=[
            SemanticDiagnostic(
                diagnostic_id="diagnostic:current",
                index_version="current",
                message="cannot resolve updated call",
                path="src/app.py",
                start_line=9,
                end_line=9,
                first_seen_index="baseline",
                last_seen_index="current",
                seen_count=2,
                properties={
                    "diagnostic_lifecycle_status": "existing",
                    "seen_count": 2,
                },
                **common,
            )
        ],
    )
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine().compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert delta.identity_changes == []
    assert delta.semantic_changes[0]["entity_type"] == "diagnostic"
    assert delta.lifecycle_metadata_changes[0]["entity_type"] == "diagnostic"
    assert (
        delta.lifecycle_metadata_changes[0]["before"]["lifecycle_properties"][
            "seen_count"
        ]
        == 1
    )


def test_graph_delta_streams_diagnostics_in_stable_identity_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "output"
    common = {
        "repo_id": "repo",
        "diagnostic_kind": "unresolved_call",
        "severity": "warning",
        "frontend_name": "python",
    }
    baseline_reader = _reader(
        output,
        tmp_path,
        "baseline",
        1,
        diagnostics=[
            SemanticDiagnostic(
                diagnostic_id="diagnostic:alpha:baseline",
                index_version="baseline",
                fact_id="fact:alpha",
                message="zulu baseline message",
                **common,
            ),
            SemanticDiagnostic(
                diagnostic_id="diagnostic:omega:baseline",
                index_version="baseline",
                fact_id="fact:omega",
                message="alpha baseline message",
                **common,
            ),
        ],
    )
    current_reader = _reader(
        output,
        tmp_path,
        "current",
        1,
        diagnostics=[
            SemanticDiagnostic(
                diagnostic_id="diagnostic:alpha:current",
                index_version="current",
                fact_id="fact:alpha",
                message="alpha current message",
                **common,
            ),
            SemanticDiagnostic(
                diagnostic_id="diagnostic:omega:current",
                index_version="current",
                fact_id="fact:omega",
                message="zulu current message",
                **common,
            ),
        ],
    )
    baseline = _identity("baseline")
    current = _identity("current")
    code = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="manifest",
        index_version="current",
        build_identity_digest=build_identity_digest(current),
    )

    delta = GraphDeltaEngine(diagnostic_sort_chunk_size=1).compare(
        repo_id="repo",
        baseline_reader=baseline_reader,
        current_reader=current_reader,
        baseline_build_identity=baseline,
        baseline_source_identity={"repo_id": "repo"},
        current_build_identity=current,
        current_code_identity=code,
    )

    assert delta.identity_changes == []
    assert len(delta.semantic_changes) == 2
    assert {item["entity_type"] for item in delta.semantic_changes} == {"diagnostic"}
